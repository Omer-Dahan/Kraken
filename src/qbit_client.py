"""qBittorrent Web API HTTP client wrappers and torrent completion handlers."""

import asyncio
import logging
import os

import requests

import bot_state as state
from bot_config import ALLOWED_USER_IDS, QBIT_URL, STAGING_DIR
from download_engine import attach_jellyfin_link
from media_organizer import parse_media_name

import threading

# Thread-local storage for thread-safe HTTP connection pooling across worker threads
_thread_local = threading.local()


def _get_http_session():
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        _thread_local.session = session
    return session


def is_in_staging(save_path):
    """Checks if a torrent save_path matches STAGING_DIR via normalized path comparison."""
    return bool(save_path) and os.path.normpath(save_path) == os.path.normpath(STAGING_DIR)


async def add_torrent_to_qbit(urls=None, torrent_file_path=None, save_path="/media/movies"):
    """Sends a magnet URL or .torrent file to qBittorrent API without blocking."""
    try:
        data = {"savepath": save_path, "autoTCM": "false"}

        def _post_request():
            sess = _get_http_session()
            if torrent_file_path and os.path.exists(torrent_file_path):
                with open(torrent_file_path, "rb") as f:
                    return sess.post(f"{QBIT_URL}/api/v2/torrents/add", data=data, files={"torrents": f}, timeout=15)
            else:
                if urls:
                    data["urls"] = urls
                return sess.post(f"{QBIT_URL}/api/v2/torrents/add", data=data, timeout=15)

        res = await asyncio.to_thread(_post_request)
        logging.info(f"qBit add torrent status: {res.status_code}, text: {res.text}")
        # qBittorrent returns 200 with body "Fails." when it rejects the torrent (e.g.
        # an already-added info-hash, or a malformed .torrent file). Some versions return
        # an empty body on success, so only a body that positively says "fail" is treated
        # as a failure.
        body = (res.text or "").strip().lower()
        return res.status_code == 200 and not body.startswith("fail")
    except Exception as e:
        logging.error(f"qBittorrent error: {e}")
        return False


async def qbit_get_torrents(filter_="all"):
    """Fetches qBittorrent's torrent list without blocking, optionally filtered.

    Returns [] when no torrents match, None when the API itself is unreachable or
    returns an error - callers should tell the user "can't reach qBittorrent" on None
    rather than silently showing an empty list.
    """
    try:
        def _get():
            return _get_http_session().get(f"{QBIT_URL}/api/v2/torrents/info?filter={filter_}", timeout=10)
        res = await asyncio.to_thread(_get)
        if res.status_code == 200:
            return res.json()
        if res.status_code in (401, 403):
            logging.error(
                f"qBittorrent returned {res.status_code} - authentication required. "
                f"Set 'Bypass authentication for clients on localhost' in "
                f"qBittorrent -> Options -> Web UI -> Authentication, or configure "
                f"QBIT_URL to match the bypass address."
            )
        else:
            logging.error(f"qBittorrent /torrents/info returned {res.status_code}: {res.text[:200]}")
    except Exception as e:
        logging.error(f"Error fetching qBit torrents: {e}")
    return None


async def qbit_set_location(hash_id, location):
    """Moves an existing torrent's save path; qBittorrent creates the directory itself."""
    try:
        def _post():
            return _get_http_session().post(
                f"{QBIT_URL}/api/v2/torrents/setLocation",
                data={"hashes": hash_id, "location": location},
                timeout=15,
            )
        res = await asyncio.to_thread(_post)
        return res.status_code == 200
    except Exception as e:
        logging.error(f"qBittorrent setLocation error: {e}")
        return False


async def qbit_delete_torrent(hash_id, delete_files=True):
    """Removes a torrent from qBittorrent, optionally deleting its files (used on cancel)."""
    try:
        def _post():
            return _get_http_session().post(
                f"{QBIT_URL}/api/v2/torrents/delete",
                data={"hashes": hash_id, "deleteFiles": "true" if delete_files else "false"},
                timeout=15,
            )
        res = await asyncio.to_thread(_post)
        return res.status_code == 200
    except Exception as e:
        logging.error(f"qBittorrent delete error: {e}")
        return False


# qBittorrent 5.0+ renamed the pause/resume endpoints to stop/start (keeping the old names as
# aliases, at least for a transition period). Cached module-wide once the first call succeeds,
# so only the very first pause/resume of the process pays for probing both names - not every tap.
_qbit_verb_style = None  # None (unknown yet) | "modern" ("stop"/"start") | "legacy" ("pause"/"resume")


async def _qbit_toggle(modern_verb, legacy_verb, hash_id):
    """POSTs to whichever of the modern/legacy torrent verb endpoints works, caching the choice."""
    global _qbit_verb_style
    order = [(modern_verb, "modern"), (legacy_verb, "legacy")]
    if _qbit_verb_style == "legacy":
        order.reverse()
    for verb, style in order:
        try:
            def _post():
                return _get_http_session().post(f"{QBIT_URL}/api/v2/torrents/{verb}", data={"hashes": hash_id}, timeout=15)
            res = await asyncio.to_thread(_post)
            if res.status_code == 200:
                _qbit_verb_style = style
                return True
        except Exception as e:
            logging.error(f"qBittorrent {verb} error: {e}")
    return False


async def qbit_pause_torrent(hash_id):
    """Pauses/stops a torrent - tries both qBittorrent 5.0+ and pre-5.0 endpoint names."""
    return await _qbit_toggle("stop", "pause", hash_id)


async def qbit_resume_torrent(hash_id):
    """Resumes/starts a torrent - tries both qBittorrent 5.0+ and pre-5.0 endpoint names."""
    return await _qbit_toggle("start", "resume", hash_id)


async def qbit_set_priority(hash_id, direction):
    """direction is 'increasePrio' or 'decreasePrio' - endpoint names stable across versions."""
    try:
        def _post():
            return _get_http_session().post(
                f"{QBIT_URL}/api/v2/torrents/{direction}", data={"hashes": hash_id}, timeout=15
            )
        res = await asyncio.to_thread(_post)
        return res.status_code == 200
    except Exception as e:
        logging.error(f"qBittorrent {direction} error: {e}")
        return False


async def check_completed_torrents(bot_client):
    """Periodically checks for completed torrents in qBittorrent and sends notifications."""
    first_run = True
    while True:
        try:
            torrents = await qbit_get_torrents("completed")
            if torrents is not None:
                # Prune hashes of completed torrents that no longer exist in qBittorrent
                current_completed_hashes = {t.get("hash") for t in torrents if t.get("hash")}
                state.notified_completed.intersection_update(current_completed_hashes)

                for t in torrents:
                    hash_id = t.get("hash")
                    name = t.get("name")
                    save_path = t.get("save_path", "")
                    if is_in_staging(save_path):
                        continue  # still awaiting folder confirmation - watch_staged_torrents owns this one
                    if hash_id and hash_id not in state.notified_completed:
                        state.notified_completed.add(hash_id)
                        # Skip sending notifications on the very first run to prevent restart spam
                        if not first_run and ALLOWED_USER_IDS:
                            done_text = f"🎬 *הורדת הטורנט הושלמה!*\n\nהתוכן *{name}* ירד בהצלחה וזמין לצפייה ב-Jellyfin!"
                            for user_id in ALLOWED_USER_IDS:
                                try:
                                    msg = await bot_client.send_message(user_id, done_text)
                                except Exception as notify_err:
                                    logging.warning(f"Failed to notify user {user_id}: {notify_err}")
                                    continue
                                # Detached: Jellyfin's scan takes minutes, and this loop still
                                # has other completed torrents to report.
                                asyncio.create_task(attach_jellyfin_link(
                                    bot_client, user_id, msg.id, done_text,
                                    parse_media_name(name).get("title"),
                                ))
                first_run = False
        except Exception as e:
            logging.error(f"Error checking completed torrents: {e}")
        await asyncio.sleep(15)


# Serializes the two kinds of qBittorrent-mutating call that can target the SAME torrent hash
# from two different places: the folder-confirmation flow's relocate (qbit_set_location) and
# the torrents screen's delete (qbit_delete_torrent).
_torrent_hash_locks = {}


def _torrent_lock(hash_id):
    if len(_torrent_hash_locks) > 200:
        # Prune un-locked hash locks to avoid memory leaks
        unlocked = [h for h, l in _torrent_hash_locks.items() if not l.locked()]
        for h in unlocked:
            _torrent_hash_locks.pop(h, None)

    lock = _torrent_hash_locks.get(hash_id)
    if lock is None:
        lock = asyncio.Lock()
        _torrent_hash_locks[hash_id] = lock
    return lock

