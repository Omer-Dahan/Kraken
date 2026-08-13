"""Hybrid Bot/Userbot download engine: progress tracking, the Bot<->Userbot media
handover, per-file download execution, and the bounded-concurrency download queue.
"""

import os
import time
import uuid
import asyncio
import logging

from telethon import errors
from telethon.errors import FileReferenceExpiredError
from telethon.tl.types import InputPeerUser

from telethon.tl.types import KeyboardButtonUrl

from fast_download import fast_download_to_path
from media_organizer import fix_permissions, parse_media_name
import bot_state as state
import jellyfin_client as jf
from bot_config import QUIET_GROUP_SIZE, DOWNLOAD_QUEUE_CONCURRENCY, BOT_QUEUE_CONCURRENCY


class ProgressTracker:
    """Helper class to track and display live file download progress in Telegram."""
    global_flood_wait_until = 0.0

    def __init__(self, bot_client, chat_id, status_msg, file_name):
        self.bot_client = bot_client
        self.chat_id = chat_id
        self.status_msg = status_msg
        self.file_name = file_name
        self.start_time = time.time()
        self.last_update_time = time.time()
        self.throttled_seconds = 0
        self.hit_premium_limit = False
        self.flood_wait_until = 0.0

    def mark_throttled(self, seconds, is_premium_limit):
        """Called from the download senders whenever Telegram rate-limits a chunk."""
        self.throttled_seconds += seconds
        if is_premium_limit:
            self.hit_premium_limit = True

    def throttle_notice(self):
        if not self.throttled_seconds:
            return ""
        if self.hit_premium_limit:
            return (
                f"\n⚠️ *טלגרם מאיט את ההורדה* - החשבון אינו Premium "
                f"(המתנה מצטברת: {int(self.throttled_seconds)} שניות)"
            )
        return f"\n⚠️ *הגבלת קצב זמנית מטלגרם* (המתנה מצטברת: {int(self.throttled_seconds)} שניות)"

    async def callback(self, current, total):
        # Quiet downloads (large batches) have no message to edit at all.
        if not self.status_msg:
            return

        now = time.time()
        # Skip progress edits while an active Telegram FloodWait penalty is in effect
        if now < ProgressTracker.global_flood_wait_until or now < self.flood_wait_until:
            return

        # Increased delay to 6.0 seconds to prevent Telegram FloodWait errors on EditMessageRequest
        if now - self.last_update_time < 6.0 and current < total:
            return

        self.last_update_time = now
        elapsed = now - self.start_time
        speed = (current / (1024 * 1024)) / elapsed if elapsed > 0 else 0
        percentage = round((current / total) * 100, 1) if total > 0 else 0

        mb_downloaded = round(current / (1024 * 1024), 1)
        mb_total = round(total / (1024 * 1024), 1)

        eta_seconds = int((total - current) / (current / elapsed)) if current > 0 and elapsed > 0 else 0
        eta_str = f"{eta_seconds // 60}m {eta_seconds % 60}s" if eta_seconds > 0 else "..."

        progress_bar_len = 10
        filled = int(progress_bar_len * (current / total)) if total > 0 else 0
        bar = "█" * filled + "░" * (progress_bar_len - filled)

        text = (
            f"⏳ *מוריד קובץ מטלגרם לשרת ({state.active_connections} חיבורים מקביליים)*\n\n"
            f"📁 *שם:* `{self.file_name}`\n"
            f"📊 *התקדמות:* `[{bar}]` {percentage}%\n"
            f"💾 *נפח:* {mb_downloaded} MB / {mb_total} MB\n"
            f"🚀 *מהירות:* {speed:.2f} MB/s | ⏱️ *זמן נותר:* {eta_str}"
            f"{self.throttle_notice()}"
        )
        try:
            await self.bot_client.edit_message(self.chat_id, self.status_msg.id, text)
        except (errors.FloodWaitError, errors.FloodPremiumWaitError) as e:
            wait = getattr(e, "seconds", 60) or 60
            logging.warning(f"Telegram FloodWait on edit_message! Pausing edits for {wait}s: {e}")
            ProgressTracker.global_flood_wait_until = max(ProgressTracker.global_flood_wait_until, now + wait + 5)
            self.flood_wait_until = max(self.flood_wait_until, now + wait + 5)
            self.mark_throttled(wait, isinstance(e, errors.FloodPremiumWaitError))
        except Exception as e:
            logging.warning(f"Failed to edit progress message: {e}")


async def attach_jellyfin_link(bot_client, chat_id, msg_id, text, title):
    """
    Asks Jellyfin to scan, waits for the new title to be indexed, then adds a watch link to
    the message that announced the download.

    Runs detached (asyncio.create_task) because the scan is measured in minutes on a real
    library and a download worker must not sit on a queue slot waiting for it. Everything in
    here is best-effort: if Jellyfin is unconfigured, down, or slow, the completion message
    the user already has simply stays as it is.
    """
    if not jf.is_configured() or not title:
        return
    try:
        await jf.refresh_library()
        item = await jf.wait_for_item(title)
        if not item:
            logging.info(f"Jellyfin has not indexed '{title}' yet - leaving the message without a link.")
            return

        url = jf.item_url(item["Id"])
        linked_text = f"{text}\n🔗 {url}"
        try:
            await bot_client.edit_message(
                chat_id, msg_id, linked_text, buttons=[[KeyboardButtonUrl("🎬 צפה ב-Jellyfin", url)]]
            )
        except errors.ButtonUrlInvalidError:
            # Telegram validates URL buttons server-side and rejects some hosts. The link is
            # in the text as well, where Telegram auto-links it, so this still works.
            logging.warning(f"Telegram rejected the Jellyfin URL button for {url}; keeping the plain link.")
            await bot_client.edit_message(chat_id, msg_id, linked_text)
    except Exception as e:
        logging.warning(f"Could not attach a Jellyfin link for '{title}': {e}")


def _media_id(message):
    """Returns the document/photo ID of a message's media, or None."""
    if not message.media:
        return None
    if hasattr(message.media, "document"):
        return message.media.document.id
    if hasattr(message.media, "photo"):
        return message.media.photo.id
    return None


async def handover_to_userbot(bot_client, user_client, message, timeout=20):
    """
    Forwards a message the Bot received to the Userbot and returns the Userbot's own copy.

    A file_reference is issued per account, so the Userbot cannot download from the Bot's
    message object - it needs a copy delivered to itself to get a reference it may use.
    """
    if state.userbot_peer is None or state.bot_peer is None:
        raise Exception("Hybrid Bot <-> Userbot link is not established.")

    target_media_id = _media_id(message)
    if not target_media_id:
        raise Exception("Could not extract media ID from message.")

    await bot_client.forward_messages(state.userbot_peer, message)

    # Delivery is not instant; poll the Userbot's chat with the Bot until the copy shows up.
    deadline = time.time() + timeout
    while time.time() < deadline:
        async for msg in user_client.iter_messages(state.bot_peer, limit=15):
            if _media_id(msg) == target_media_id:
                return msg
        await asyncio.sleep(1)

    raise Exception(f"Forwarded message did not arrive in the Userbot chat within {timeout}s.")


async def download_message_media(client, message, target_file_path, tracker):
    """
    Downloads a message's media, using parallel connections when the media is a document.

    Falls back to Telethon's single-connection download for anything without a Document
    (photos, and any media type the parallel path does not cover).
    """
    try:
        document = getattr(message, "document", None)
        if document is None:
            logging.info("Media is not a document; using single-connection download.")
            await client.download_media(message, file=target_file_path, progress_callback=tracker.callback)
            return

        logging.info(
            f"Downloading {document.size} bytes over {state.active_connections} parallel connections "
            f"(premium: {state.userbot_is_premium})."
        )
        await fast_download_to_path(
            client,
            document,
            target_file_path,
            progress_callback=tracker.callback,
            connection_count=state.active_connections,
            on_throttle=tracker.mark_throttled,
        )
    except BaseException:
        # A failed/cancelled attempt must not leave a 0-byte or partial file behind -
        # open(path, "wb") inside fast_download_to_path creates it before any bytes
        # arrive, and the caller retries the SAME target_file_path via a fallback client.
        if os.path.exists(target_file_path):
            os.remove(target_file_path)
        raise


async def link_bot_and_userbot(bot_client, user_client, bot_info, user_info):
    """
    Establishes the two-way peer link between the Bot and the Userbot.

    Telethon can only address a peer it has an access_hash for. A bare user_id is not
    enough, which is why both sides must learn about each other before any forwarding:
      1. The Userbot resolves the Bot by @username (works with no prior contact) and
         sends /start, which is also what allows a bot to message a user at all.
      2. That incoming message teaches the Bot's session who the Userbot is, so the Bot
         can forward media to it later.
    """
    # --- Userbot side: resolve the Bot by username, never by raw ID ---
    if bot_info.username:
        try:
            state.bot_peer = await user_client.get_input_entity(f"@{bot_info.username}")
            logging.info(f"Userbot resolved Bot peer via username @{bot_info.username}")
        except Exception as e:
            logging.warning(f"Userbot could not resolve Bot by username: {e}")
    else:
        logging.warning("Bot has no username; Userbot cannot resolve it reliably.")

    if state.bot_peer is None:
        try:
            state.bot_peer = await user_client.get_input_entity(bot_info.id)
            logging.info("Userbot resolved Bot peer from session cache.")
        except Exception as e:
            logging.error(f"Userbot cannot address the Bot at all: {e}")
            return

    # Starting the bot is what opens the channel for bot -> user messages.
    try:
        await user_client.send_message(state.bot_peer, "/start")
        logging.info("Userbot sent /start to the Bot (hybrid link handshake).")
    except Exception as e:
        logging.warning(f"Userbot failed to /start the Bot: {e}")

    # Give the Bot a moment to receive the update and cache the Userbot entity.
    await asyncio.sleep(2)

    # --- Bot side: resolve the Userbot ---
    try:
        state.userbot_peer = await bot_client.get_input_entity(user_info.id)
        logging.info(f"Bot resolved Userbot peer [ID: {user_info.id}] from session cache.")
    except Exception as e:
        # Bots may address a user that has started them using access_hash=0.
        logging.warning(f"Bot could not resolve Userbot from cache ({e}); falling back to access_hash=0.")
        state.userbot_peer = InputPeerUser(user_info.id, 0)

    # Verify the link end-to-end so failures surface at startup, not mid-download.
    try:
        await bot_client.send_message(state.userbot_peer, "hybrid link ready")
        logging.info("Hybrid Bot <-> Userbot link verified successfully.")
    except Exception as e:
        logging.error(f"Hybrid link verification FAILED - large downloads will fall back to the Bot client: {e}")
        state.userbot_peer = None


async def _download_via(client_kind, bot_client, user_client, message, target_file_path, tracker):
    """
    Pulls the file's bytes through the given account. "user" hands the message over to the
    Userbot first (a file_reference is per-account, so the Userbot needs its own copy of the
    message to get one it can use) and downloads with it; "bot" downloads directly with the
    Bot's own message object and file_reference - no handover needed since it's already the
    Bot's message.

    On a FileReferenceExpiredError (the reference is per-account and expires after ~1 hour),
    the original message is re-fetched to obtain a fresh reference and the download is retried
    once. Without this, both accounts would fail with the same stale-reference error and the
    user would get a generic failure with no actionable hint.
    """
    try:
        if client_kind == "user":
            user_msg = await handover_to_userbot(bot_client, user_client, message)
            await download_message_media(user_client, user_msg, target_file_path, tracker)
        else:
            await download_message_media(bot_client, message, target_file_path, tracker)
    except FileReferenceExpiredError:
        logging.info(f"file_reference expired for {client_kind}-account, re-fetching message via bot_client and retrying once.")
        refreshed = await bot_client.get_messages(message.chat_id, ids=message.id)
        if not refreshed:
            raise
        if client_kind == "user":
            user_msg = await handover_to_userbot(bot_client, user_client, refreshed)
            await download_message_media(user_client, user_msg, target_file_path, tracker)
        else:
            await download_message_media(bot_client, refreshed, target_file_path, tracker)


async def start_video_download(bot_client, user_client, message, chat_id, target_dir, file_name, notify=True, primary="user"):
    """
    Downloads message's media into target_dir/file_name, reporting live progress in chat_id
    unless notify=False (used for large auto-organized batches to avoid chat spam - see
    enqueue_group_downloads). Returns True on success, False if both download paths failed
    (which is already reported to the chat before returning either way).

    `primary` picks which Telegram account's connections actually fetch the bytes ("user" or
    "bot") - the other account is tried as a fallback if the primary fails. This is what lets
    the download queue run a Userbot download and a Bot download at the same time: each pool
    calls in with a different `primary`, so they're never both waiting on the same account's
    per-account rate limit.
    """
    os.makedirs(target_dir, exist_ok=True)
    target_file_path = os.path.join(target_dir, file_name)

    # Refuse to silently overwrite an existing file - replacing an episode requires an
    # explicit delete first. Consistent with _reject_collision in file_browser.py.
    if os.path.exists(target_file_path):
        collision_text = f"⚠️ הקובץ `{file_name}` כבר קיים ב-`{target_dir}`. כדי להחליף, מחק אותו קודם דרך 🗂 מנהל קבצים."
        if notify:
            await bot_client.send_message(chat_id, collision_text)
        else:
            logging.warning(f"Collision: {target_file_path} already exists, skipping download.")
        return False

    status_msg = None
    if notify:
        status_msg = await bot_client.send_message(chat_id, f"⏳ מתחיל הורדת קובץ *{file_name}* ל-`{target_dir}`...")
    tracker = ProgressTracker(bot_client, chat_id, status_msg, file_name)

    fallback = "bot" if primary == "user" else "user"
    downloaded = False
    try:
        await _download_via(primary, bot_client, user_client, message, target_file_path, tracker)
        downloaded = True
    except Exception as primary_err:
        logging.warning(f"{primary}-account download failed, falling back to {fallback}: {primary_err}")

    if not downloaded:
        try:
            await _download_via(fallback, bot_client, user_client, message, target_file_path, tracker)
            downloaded = True
        except Exception as fallback_err:
            logging.error(f"{fallback}-account download failed as well: {fallback_err}")
            error_text = (
                f"❌ שגיאה: הורדת הקובץ *{file_name}* נכשלה גם דרך ה-Userbot וגם דרך הבוט.\n`{fallback_err}`"
            )
            if status_msg:
                await bot_client.edit_message(chat_id, status_msg.id, error_text)
            else:
                await bot_client.send_message(chat_id, error_text)
            return False

    fix_permissions(target_dir)

    if not notify:
        return True

    elapsed = max(time.time() - tracker.start_time, 0.001)
    avg_speed = (os.path.getsize(target_file_path) / (1024 * 1024)) / elapsed

    premium_hint = ""
    if tracker.hit_premium_limit:
        premium_hint = (
            f"\n⚠️ טלגרם האט את ההורדה ב-{int(tracker.throttled_seconds)} שניות "
            f"כי החשבון אינו Premium."
        )

    done_text = (
        f"✅ הקובץ *{file_name}* ירד בהצלחה מטלגרם ונשמר ב-`{target_file_path}`!\n"
        f"🚀 *מהירות ממוצעת:* {avg_speed:.2f} MB/s ({state.active_connections} חיבורים במקביל)"
        f"{premium_hint}"
    )
    await bot_client.edit_message(chat_id, status_msg.id, done_text)

    # Detached on purpose - see attach_jellyfin_link. The user has their "done" message the
    # moment the bytes land; the watch link arrives on the same message once Jellyfin's scan
    # has caught up, which can take minutes.
    state.create_background_task(attach_jellyfin_link(
        bot_client, chat_id, status_msg.id, done_text, parse_media_name(file_name).get("title")
    ))
    return True


def enqueue_group_downloads(bot_client, user_client, chat_id, label, targets):
    """
    Puts every (message, target_dir, file_name) in `targets` on the shared download_queue
    without blocking the caller - start_download_workers' two pools (one per Telegram
    account) pull from it concurrently, so this never blocks regardless of how many items
    are already queued. Groups bigger than QUIET_GROUP_SIZE get one digest message at the
    end instead of per-file play-by-play.
    """
    quiet = len(targets) > QUIET_GROUP_SIZE
    group_id = uuid.uuid4().hex[:8] if quiet else None
    if group_id:
        state.download_groups[group_id] = {"chat_id": chat_id, "label": label, "total": len(targets), "done": 0, "failed": 0}

    for message, target_dir, file_name in targets:
        item_id = uuid.uuid4().hex[:8]
        state.queued_downloads[item_id] = {
            "file_name": file_name, "target_dir": target_dir, "chat_id": chat_id,
            "status": "waiting", "via": None, "enqueued_at": time.time(),
        }
        state.download_queue.put_nowait((item_id, chat_id, message, target_dir, file_name, not quiet, group_id))
    state.record_queue_count()


async def _download_worker(pool_kind, bot_client, user_client):
    """
    One persistent worker in one of the two account pools (see start_download_workers).
    `pool_kind` is which account this worker prefers as PRIMARY for whatever it dequeues -
    start_video_download still falls back to the other account if the primary fails, so a
    worker in the "bot" pool downloading a file whose Bot-side attempt fails still tries the
    Userbot for that same file rather than giving up.
    """
    while True:
        item_id, chat_id, message, target_dir, file_name, notify, group_id = await state.download_queue.get()
        try:
            entry = state.queued_downloads.get(item_id)
            if entry is None:
                continue  # nothing to do - shouldn't happen, but never crash a worker over it
            entry["status"] = "downloading"
            entry["via"] = pool_kind
            failed = False
            try:
                succeeded = await start_video_download(
                    bot_client, user_client, message, chat_id, target_dir, file_name,
                    notify=notify, primary=pool_kind,
                )
                failed = not succeeded
            except Exception as e:
                # Anything that escapes start_video_download's own error handling (e.g. a
                # permissions error from os.makedirs, or the final edit_message call itself
                # failing) must still reach the user - never fail a queued download silently.
                failed = True
                logging.error(f"Queued download crashed for {file_name}: {e}")
                try:
                    await bot_client.send_message(chat_id, f"❌ שגיאה בלתי צפויה בהורדת *{file_name}*: {e}")
                except Exception as notify_err:
                    logging.warning(f"Failed to report queued-download crash: {notify_err}")
            finally:
                state.queued_downloads.pop(item_id, None)
                state.record_queue_count()
                if group_id:
                    group = state.download_groups.get(group_id)
                    if group:
                        group["failed" if failed else "done"] += 1
                        if group["done"] + group["failed"] >= group["total"]:
                            state.download_groups.pop(group_id, None)
                            summary = f"✅ *{group['label']}*: {group['done']}/{group['total']} קבצים ירדו בהצלחה"
                            if group["failed"]:
                                summary += f", {group['failed']} נכשלו"
                            summary += "."
                            # In quiet mode this digest is the ONLY success notification for
                            # the whole group - a single failed send here would silently lose
                            # every successfully-downloaded file's outcome, so it gets a retry.
                            digest_msg = None
                            for attempt in range(2):
                                try:
                                    digest_msg = await bot_client.send_message(chat_id, summary)
                                    break
                                except Exception as e:
                                    logging.warning(f"Failed to send group digest (attempt {attempt + 1}/2): {e}")
                            if digest_msg and group["done"]:
                                # One scan + one link for the whole group, instead of per file.
                                state.create_background_task(attach_jellyfin_link(
                                    bot_client, chat_id, digest_msg.id, summary,
                                    parse_media_name(group["label"]).get("title"),
                                ))
        except Exception as e:
            # A bug in the bookkeeping above (group counting, digest send) - as opposed to a
            # download failure, which start_video_download's own try/except already reports
            # to the user - must not permanently kill this worker; the same log-and-keep-going
            # pattern check_completed_torrents/watch_staged_torrents use for their loops. Left
            # unguarded, this would silently and permanently shrink this pool by one slot.
            logging.error(f"Download worker ({pool_kind}) hit an unexpected error: {e}")
        finally:
            state.download_queue.task_done()


def start_download_workers(bot_client, user_client):
    """
    Spins up the two account pools once at startup. Both pull from the same download_queue,
    so whichever account has a free slot next picks up the next file - a Userbot download and
    a Bot download can genuinely run at the same time instead of the Bot only ever being a
    fallback for a failed Userbot attempt.
    """
    for _ in range(DOWNLOAD_QUEUE_CONCURRENCY):
        state.create_background_task(_download_worker("user", bot_client, user_client))
    for _ in range(BOT_QUEUE_CONCURRENCY):
        state.create_background_task(_download_worker("bot", bot_client, user_client))


def _queue_status_text():
    if not state.queued_downloads:
        return "📭 אין קבצים בתור ההורדה כרגע."
    active = [v for v in state.queued_downloads.values() if v["status"] == "downloading"]
    waiting = [v for v in state.queued_downloads.values() if v["status"] == "waiting"]
    lines = ["📥 *תור הורדות טלגרם:*\n"]
    if active:
        lines.append(f"🔽 *מוריד כרגע ({len(active)}):*")
        for v in active:
            via = "Userbot" if v.get("via") == "user" else "Bot"
            lines.append(f"  • `{v['file_name']}` ({via})")
    lines.append(f"⏳ *ממתינים בתור:* {len(waiting)}")
    return "\n".join(lines)
