"""
Interactive downloads and qBittorrent manager screen for Telethon UI.
Displays active/waiting torrents and Telegram download queue.
"""

import asyncio
import time
import logging
from collections import Counter

from telethon import Button, errors

import bot_state as state
import keyboards
from bot_config import ALLOWED_USER_IDS, STAGING_DIR, TORRENT_HASH_PREFIX_LEN, TORRENTS_PAGE_SIZE
from download_engine import _queue_status_text
from media_organizer import parse_media_name, propose_folder, sanitize_folder_name
from qbit_client import (
    is_in_staging,
    qbit_get_torrents,
    qbit_set_priority,
    qbit_pause_torrent,
    qbit_resume_torrent,
    qbit_delete_torrent,
    _torrent_lock,
)
from confirmation_flow import _new_pending_action, send_confirmation, _clear_awaiting_text

_ERROR_STATES = {"error", "missingFiles"}
# Includes both pre-5.0 ("pausedUP") and 5.0+ ("stoppedUP") qBittorrent state names.
_COMPLETED_STATES = {"uploading", "stalledUP", "queuedUP", "pausedUP", "stoppedUP", "forcedUP", "checkingUP"}
_ACTIVE_STATES = {"downloading", "forcedDL", "metaDL", "allocating", "checkingDL", "checkingResumeData", "moving"}
_PAUSED_STATES = {"pausedDL", "stoppedDL", "pausedUP", "stoppedUP"}

# "queue" is not a qBittorrent state - it's the bot's own Telegram download queue, shown here
# as a fifth tab because "what is downloading right now" is one question to the user even
# though it's two systems underneath.
TORRENT_FILTER_TABS = [
    ("active", "פעילים"),
    ("waiting", "ממתינים"),
    ("completed", "הושלמו"),
    ("error", "שגיאה"),
    ("queue", "תור טלגרם"),
]
TORRENT_FILTER_LABELS = dict(TORRENT_FILTER_TABS)
# Hidden while empty, so the usual case is a single row of three tabs instead of a fixed four
# spread over two - on a phone that's a whole torrent's worth of list back.
_OPTIONAL_TABS = {"error", "queue"}
TORRENT_STATE_LABELS = {"active": "🔽 פעיל", "waiting": "⏳ ממתין", "completed": "✅ הושלם", "error": "⚠️ שגיאה"}


def _classify_torrent(t):
    """Buckets a qBittorrent /torrents/info entry into one of the 4 screen filters."""
    state_ = t.get("state", "")
    progress = t.get("progress") or 0
    if state_ in _ERROR_STATES:
        return "error"
    if progress >= 1 or state_ in _COMPLETED_STATES:
        return "completed"
    if state_ in _ACTIVE_STATES:
        return "active"
    return "waiting"  # queuedDL, stalledDL, paused/stoppedDL, or any unrecognized state


def _format_eta(seconds):
    """qBittorrent uses a large sentinel (~8640000s / 100 days) for 'no real ETA' - shown as infinity."""
    if seconds is None or seconds < 0 or seconds >= 8640000:
        return "∞"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _format_speed(bytes_per_sec):
    return f"{(bytes_per_sec or 0) / (1024 * 1024):.2f} MB/s"


def _tor_short_hash(hash_id):
    return (hash_id or "")[:TORRENT_HASH_PREFIX_LEN]


def _tor_resolve_hash(short_hash, torrents):
    """
    Resolves a callback's short hash prefix back to a full torrent hash against a freshly
    fetched torrent list. Returns None if no torrent matches (already deleted) or - as a
    safety net against a theoretical prefix collision - more than one does, so a caller never
    silently acts on the wrong torrent.
    """
    if not short_hash:
        return None
    matches = [t.get("hash") for t in torrents if t.get("hash", "").startswith(short_hash)]
    return matches[0] if len(matches) == 1 else None


def _tor_new_session(msg_id=None):
    return {"filter": "active", "page": 0, "msg_id": msg_id, "mode": "list", "current_hash": None, "gen": 0}


def _pop_pending_for_hash(hash_id):
    """
    Synchronously claims (pops) any open folder-confirmation pending_actions entry for this
    torrent hash - called with NO preceding await, from this screen's delete handler, so a
    near-simultaneous tap on the torrent's original confirm/cancel message can't race it (see
    the matching synchronous-pop fix in telegram_bot.py's confirm/use_existing/cancel handler).
    """
    for pid, a in list(state.pending_actions.items()):
        if a.get("type") == "torrent" and a.get("hash") == hash_id:
            return pid, state.pending_actions.pop(pid, None)
    return None, None


async def _tor_send_or_edit(bot_client, chat_id, session, text, rows):
    if session.get("msg_id"):
        try:
            msg = await bot_client.edit_message(chat_id, session["msg_id"], text, buttons=rows)
        except (errors.FloodWaitError, errors.FloodPremiumWaitError) as e:
            logging.warning(f"Telegram FloodWait editing torrents-screen message: {e}")
            return
        except errors.MessageNotModifiedError:
            # A refresh that found nothing had changed - which the Telegram-queue tab, being a
            # static read-out, hits routinely. The screen is already showing the right thing;
            # falling through to the generic handler below would post a duplicate of it.
            return
        except Exception as e:
            logging.warning(f"Failed to edit torrents-screen message, sending a new one: {e}")
            msg = await bot_client.send_message(chat_id, text, buttons=rows)
    else:
        msg = await bot_client.send_message(chat_id, text, buttons=rows)
    if 'msg' in locals():
        session["msg_id"] = msg.id


def _tab_rows(counts, selected):
    """
    The tab strip, with each tab carrying its own count so the numbers are readable without
    visiting every tab. Empty optional tabs drop out entirely; the selected one always stays,
    even at zero, so the header and the strip can never disagree about where you are.
    """
    tabs = []
    for key, label in TORRENT_FILTER_TABS:
        count = counts.get(key, 0)
        if key in _OPTIONAL_TABS and not count and key != selected:
            continue
        text = f"{label} {count}"
        tabs.append(Button.inline(f"▶ {text}" if key == selected else text, data=f"tor:filter:{key}"))

    # Two across, never more. Telegram divides a row's width equally, so a four-tab strip
    # squeezed each Hebrew label plus its count into a quarter of a phone screen and let it
    # clip. Pairs give every tab half a row, which the longest label ("תור טלגרם 12") fits
    # comfortably. An odd count leaves the last tab alone on its own full-width row.
    return [tabs[i:i + 2] for i in range(0, len(tabs), 2)]


async def _tor_render_list(bot_client, chat_id, status_line=None):
    session = state.torrent_sessions.get(chat_id)
    if not session:
        return
    session["last_accessed"] = time.time()
    session["gen"] += 1
    gen = session["gen"]

    all_torrents = await qbit_get_torrents("all")
    if session.get("gen") != gen:
        return  # a newer render (another tap) already started - abandon this stale one

    if all_torrents is None:
        # API unreachable - show the error in the screen itself rather than silently
        # pretending every tab is empty.
        session["mode"] = "list"
        session["current_hash"] = None
        error_text = "❌ *לא ניתן להתחבר ל-qBittorrent.*\nבדוק שהשירות פועל ושה-Web UI נגיש."
        rows = [[Button.inline("🔄 נסה שוב", data="tor:refresh"), Button.inline("🏠 תפריט ראשי", data="tor:home")]]
        await _tor_send_or_edit(bot_client, chat_id, session, error_text, rows)
        return

    session["mode"] = "list"
    session["current_hash"] = None

    counts = Counter(_classify_torrent(t) for t in all_torrents)
    counts["queue"] = len(state.queued_downloads)

    bucket = session["filter"]
    rows = []

    if bucket == "queue":
        # No per-item buttons here: an in-flight Telegram download can't be paused, reordered
        # or cancelled, so this tab is a read-out. It renders through the same formatter /queue
        # has always used, rather than growing a second way to describe the same queue.
        body = _queue_status_text()
    else:
        filtered = [t for t in all_torrents if _classify_torrent(t) == bucket]
        total_pages = max(1, -(-len(filtered) // TORRENTS_PAGE_SIZE))
        session["page"] = min(session["page"], total_pages - 1)
        start = session["page"] * TORRENTS_PAGE_SIZE

        for t in filtered[start:start + TORRENTS_PAGE_SIZE]:
            name = t.get("name") or "?"
            short_name = name if len(name) <= 35 else name[:32] + "..."
            pct = round((t.get("progress") or 0) * 100, 1)
            pause_hint = "⏸ " if t.get("state") in _PAUSED_STATES else ""
            label = (
                f"{pause_hint}🎬 {short_name} - {pct}% "
                f"⬇️{_format_speed(t.get('dlspeed'))} ⏱{_format_eta(t.get('eta'))} 🌱{t.get('num_seeds', 0)}"
            )
            rows.append([Button.inline(label, data=f"tor:card:{_tor_short_hash(t.get('hash'))}")])

        if total_pages > 1:
            page_row = []
            if session["page"] > 0:
                page_row.append(Button.inline("◀️ הקודם", data=f"tor:page:{session['page'] - 1}"))
            if session["page"] < total_pages - 1:
                page_row.append(Button.inline("➡️ הבא", data=f"tor:page:{session['page'] + 1}"))
            if page_row:
                rows.append(page_row)

        body = f"📥 *הורדות - {TORRENT_FILTER_LABELS[bucket]}* ({len(filtered)})"
        if not filtered:
            body += "\n_(אין טורנטים בקטגוריה הזו)_"

    rows.extend(_tab_rows(counts, bucket))
    rows.append([Button.inline("🔄 רענן", data="tor:refresh"), Button.inline("🏠 תפריט ראשי", data="tor:home")])

    if status_line:
        body = f"{status_line}\n\n{body}"

    await _tor_send_or_edit(bot_client, chat_id, session, body, rows)


async def _tor_render_card(bot_client, chat_id, short_hash, status_line=None):
    session = state.torrent_sessions.get(chat_id)
    if not session:
        return
    session["last_accessed"] = time.time()
    session["gen"] += 1
    gen = session["gen"]

    all_torrents = await qbit_get_torrents("all")
    if session.get("gen") != gen:
        return

    if all_torrents is None:
        await _tor_render_list(bot_client, chat_id, status_line="❌ לא ניתן להתחבר ל-qBittorrent.")
        return

    full_hash = _tor_resolve_hash(short_hash, all_torrents)
    t = next((x for x in all_torrents if x.get("hash") == full_hash), None) if full_hash else None
    if not t:
        await _tor_render_list(bot_client, chat_id, status_line="⚠️ הטורנט הזה כבר לא קיים.")
        return

    session["mode"] = "card"
    session["current_hash"] = short_hash

    bucket = _classify_torrent(t)
    state_ = t.get("state", "")
    pct = round((t.get("progress") or 0) * 100, 1)
    seeds = t.get("num_seeds", 0)
    seeds_total = t.get("num_complete", seeds)
    size_gb = round((t.get("size") or 0) / (1024 ** 3), 2)
    priority = t.get("priority", -1)

    text = (
        f"🎬 *{t.get('name') or '?'}*\n\n"
        f"{TORRENT_STATE_LABELS.get(bucket, state_)} (`{state_}`)\n"
        f"📊 התקדמות: {pct}%\n"
        f"🚀 מהירות: ⬇️ {_format_speed(t.get('dlspeed'))} | ⬆️ {_format_speed(t.get('upspeed'))}\n"
        f"⏱ זמן נותר: {_format_eta(t.get('eta'))}\n"
        f"🌱 Seeders: {seeds} ({seeds_total} בסך הכל)\n"
        f"💾 גודל: {size_gb} GB"
    )
    queueing_on = priority >= 0
    if queueing_on:
        text += f"\n🔢 עדיפות: {priority}"
    if status_line:
        text = f"{status_line}\n\n{text}"

    is_paused = state_ in _PAUSED_STATES
    pause_btn = (
        Button.inline("▶️ המשך", data=f"tor:resume:{short_hash}") if is_paused
        else Button.inline("⏸ השהה", data=f"tor:pause:{short_hash}")
    )

    # Delete sits up with the other actions rather than on the bottom row, which is kept for
    # things that can't change anything (refresh / back) - the row you tap without reading.
    rows = [[pause_btn, Button.inline("🗑 מחק", data=f"tor:delask:{short_hash}")]]
    # qBittorrent reports priority -1 when queue management is off, and the buttons then do
    # nothing at all. They used to be shown anyway with a line of text explaining that they
    # wouldn't work; hiding them is the same information, minus a row and a disappointment.
    if queueing_on:
        rows.append([
            Button.inline("⬆️ עדיפות", data=f"tor:prioup:{short_hash}"),
            Button.inline("⬇️ עדיפות", data=f"tor:priodown:{short_hash}"),
        ])
    rows.append([
        Button.inline("🔄 רענן", data=f"tor:card:{short_hash}"),
        Button.inline("🔙 חזרה לרשימה", data="tor:back"),
    ])

    await _tor_send_or_edit(bot_client, chat_id, session, text, rows)


async def _tor_render_delete_confirm(bot_client, chat_id, short_hash):
    session = state.torrent_sessions.get(chat_id)
    if not session:
        return
    session["last_accessed"] = time.time()
    session["gen"] += 1
    gen = session["gen"]

    all_torrents = await qbit_get_torrents("all")
    if session.get("gen") != gen:
        return

    if all_torrents is None:
        await _tor_render_list(bot_client, chat_id, status_line="❌ לא ניתן להתחבר ל-qBittorrent.")
        return

    full_hash = _tor_resolve_hash(short_hash, all_torrents)
    t = next((x for x in all_torrents if x.get("hash") == full_hash), None) if full_hash else None
    if not t:
        await _tor_render_list(bot_client, chat_id, status_line="⚠️ הטורנט הזה כבר לא קיים.")
        return

    session["mode"] = "delete_confirm"
    session["current_hash"] = short_hash

    text = f"🗑 *מחיקת טורנט*\n\n*{t.get('name') or '?'}*\n\nבחר/י איך למחוק:"
    rows = [
        [Button.inline("⏏️ הסר מהתור, שמור קבצים", data=f"tor:delkeep:{short_hash}")],
        [Button.inline("🗑⚠️ מחק גם את הקבצים (בלתי הפיך)", data=f"tor:delfiles:{short_hash}")],
        [Button.inline("❌ ביטול", data=f"tor:delcancel:{short_hash}")],
    ]
    await _tor_send_or_edit(bot_client, chat_id, session, text, rows)


async def _tor_open(bot_client, chat_id, msg_id=None, filter_="active"):
    """
    Opens/reopens the torrents screen on `msg_id` - the message this screen lives on for the
    rest of the session. Mirrors _fb_open: inline-button entry points pass the message that
    was tapped so the transition happens in place, while a typed /torrents passes None and
    gets a fresh message at the bottom of the chat.

    Re-anchoring on every open also keeps the previous message from being orphaned. An orphan
    stays fully tappable (Telegram never invalidates old inline keyboards) while every action
    keys off torrent_sessions rather than which message was tapped, so a tap on it would
    silently mutate whatever the CURRENTLY tracked message shows - including confirming a
    delete on a torrent the user wasn't even looking at. See the event.message_id guard below
    for the other half of this.
    """
    session = state.torrent_sessions.get(chat_id)
    if session is None:
        session = state.torrent_sessions[chat_id] = _tor_new_session()
    session["filter"] = filter_
    session["page"] = 0
    session["msg_id"] = msg_id
    if msg_id:
        keyboards.claim_message(chat_id, msg_id)
    await _tor_render_list(bot_client, chat_id)


async def _tor_handle_callback(bot_client, event, chat_id, cq_data):
    parts = cq_data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else None

    if action == "open":
        await event.answer()
        await _tor_open(bot_client, chat_id, msg_id=event.message_id)
        return

    session = state.torrent_sessions.get(chat_id)
    if not session:
        # Bot restart wiped the session - reopen in place on the SAME message rather than
        # sending a brand new one and leaving this one's buttons looking alive but dead.
        await event.answer("⚠️ הסשן פג, פותח מחדש.", alert=True)
        await _tor_open(bot_client, chat_id, msg_id=event.message_id)
        return

    if event.message_id != session.get("msg_id"):
        # This tap landed on a message that's no longer the one torrent_sessions is tracking
        # for this chat (e.g. an orphaned older screen after a re-open - see _tor_open's
        # docstring) - without this check, the action below would silently apply to whatever
        # the CURRENTLY tracked message shows instead of what this tap's own message displays.
        await event.answer("⚠️ התצוגה לא מעודכנת, מרענן...", alert=True)
        if session["mode"] in ("card", "delete_confirm") and session.get("current_hash"):
            await _tor_render_card(bot_client, chat_id, session["current_hash"])
        else:
            await _tor_render_list(bot_client, chat_id)
        return

    if action == "filter":
        session["filter"] = arg
        session["page"] = 0
        await event.answer()
        await _tor_render_list(bot_client, chat_id)
        return

    if action == "page":
        try:
            session["page"] = int(arg)
        except (TypeError, ValueError):
            pass
        await event.answer()
        await _tor_render_list(bot_client, chat_id)
        return

    if action == "refresh":
        await event.answer("מרענן...")
        if session["mode"] in ("card", "delete_confirm") and session.get("current_hash"):
            await _tor_render_card(bot_client, chat_id, session["current_hash"])
        else:
            await _tor_render_list(bot_client, chat_id)
        return

    if action == "card":
        await event.answer()
        await _tor_render_card(bot_client, chat_id, arg)
        return

    if action == "back":
        await event.answer()
        await _tor_render_list(bot_client, chat_id)
        return

    if action == "home":
        state.torrent_sessions.pop(chat_id, None)
        await event.answer()
        await keyboards.show_main_menu(bot_client, chat_id, event.message_id)
        return

    if action in ("pause", "resume", "prioup", "priodown"):
        await event.answer("משהה..." if action == "pause" else "ממשיך..." if action == "resume" else "")
        all_t = await qbit_get_torrents("all")
        if all_t is None:
            await _tor_render_list(bot_client, chat_id, status_line="❌ לא ניתן להתחבר ל-qBittorrent.")
            return
        full_hash = _tor_resolve_hash(arg, all_t)
        if full_hash:
            if action == "pause":
                await qbit_pause_torrent(full_hash)
            elif action == "resume":
                await qbit_resume_torrent(full_hash)
            else:
                await qbit_set_priority(full_hash, "increasePrio" if action == "prioup" else "decreasePrio")
        await _tor_render_card(bot_client, chat_id, arg)
        return

    if action == "delask":
        await event.answer()
        await _tor_render_delete_confirm(bot_client, chat_id, arg)
        return

    if action == "delcancel":
        await event.answer("בוטל.")
        await _tor_render_card(bot_client, chat_id, arg)
        return

    if action in ("delkeep", "delfiles"):
        delete_files = action == "delfiles"
        all_torrents = await qbit_get_torrents("all")
        if all_torrents is None:
            await event.answer("❌ qBittorrent לא נגיש.", alert=True)
            return
        full_hash = _tor_resolve_hash(arg, all_torrents)
        # Claim any open folder-confirmation pending action for this torrent SYNCHRONOUSLY -
        # no await between resolving full_hash above and this pop - so a near-simultaneous tap
        # on the torrent's original confirm/cancel message can't race this delete.
        pending_pid, pending_action = _pop_pending_for_hash(full_hash) if full_hash else (None, None)

        await event.answer("מוחק..." if delete_files else "מסיר מהתור...")

        if not full_hash:
            await _tor_render_list(bot_client, chat_id, status_line="⚠️ הטורנט הזה כבר לא קיים.")
            return

        # Locked against the folder-confirmation flow's relocate (qbit_set_location, in
        # telegram_bot.py's confirm/use_existing handler) for the same hash - the pop above
        # only claims the pending_actions bookkeeping entry, it does NOT by itself stop this
        # delete from firing while a relocate for the same torrent is in flight elsewhere
        # (e.g. a second allowed user, or the same user acting on two open chats/messages for
        # the same torrent).
        async with _torrent_lock(full_hash):
            ok = await qbit_delete_torrent(full_hash, delete_files=delete_files)
        state.watched_torrent_hashes.discard(full_hash)
        state.notified_completed.discard(full_hash)

        if pending_action:
            _clear_awaiting_text(pending_action["chat_id"], pending_pid)
            if pending_action.get("status_msg"):
                stale_text = (
                    "❌ הטורנט נמחק (כולל קבצים) דרך מסך הטורנטים." if delete_files
                    else "❌ הטורנט הוסר מהתור דרך מסך הטורנטים - הקבצים שהורדו עד כה עדיין "
                         f"נמצאים ב-`{STAGING_DIR}`, לא יאורגנו אוטומטית."
                )
                try:
                    await bot_client.edit_message(
                        pending_action["chat_id"], pending_action["status_msg"].id, stale_text,
                    )
                except Exception as e:
                    logging.warning(f"Failed to clear stale pending confirmation for deleted torrent: {e}")

        status = (
            ("✅ הטורנט וכל הקבצים נמחקו." if delete_files else "✅ הטורנט הוסר מהתור, הקבצים נשמרו.")
            if ok else "❌ שגיאה במחיקת הטורנט."
        )
        await _tor_render_list(bot_client, chat_id, status_line=status)
        return

    await event.answer()


async def watch_staged_torrents(bot_client):
    """
    Polls qBittorrent for torrents parked in STAGING_DIR whose metadata has arrived
    (name/size known), and prompts the owner to confirm the real Movies/TV folder.

    Also naturally covers a bot restart: watched_torrent_hashes starts empty, so
    anything still sitting in STAGING_DIR gets re-offered on the next poll.
    """
    while True:
        try:
            if not ALLOWED_USER_IDS:
                await asyncio.sleep(5)
                continue

            torrents = await qbit_get_torrents("all")
            # Piggy-backed on this poll instead of the main menu making its own qBittorrent
            # call - see bot_state.active_download_count.
            if torrents is not None:
                state.active_download_count = (
                    sum(1 for t in torrents if _classify_torrent(t) == "active")
                    + sum(1 for d in state.queued_downloads.values() if d["status"] == "downloading")
                )
                # Prune watched hashes that are no longer in staging or no longer in qBittorrent
                active_staged_hashes = {
                    t.get("hash") for t in torrents
                    if t.get("hash") and is_in_staging(t.get("save_path", ""))
                }
                state.watched_torrent_hashes.intersection_update(active_staged_hashes)
            else:
                # API down - update only the Telegram queue count, leave the qBittorrent
                # half stale rather than resetting it to zero.
                state.active_download_count = sum(
                    1 for d in state.queued_downloads.values() if d["status"] == "downloading"
                )
                await asyncio.sleep(5)
                continue

            for t in torrents:
                hash_id = t.get("hash")
                save_path = t.get("save_path", "")
                if not hash_id or hash_id in state.watched_torrent_hashes:
                    continue
                if not is_in_staging(save_path):
                    continue
                if not t.get("total_size"):
                    continue  # metadata not fetched yet

                # Per-torrent try/except: one failing torrent must not block the rest.
                try:
                    name = t.get("name", "")
                    owner_id = ALLOWED_USER_IDS[0]
                    parsed = parse_media_name(name)
                    # Torrent names are noisier than filenames (release groups, tracker tags), so
                    # the destination the owner has selected carries even more weight here.
                    base, folder = propose_folder(parsed, preferred_base=state.user_modes.get(owner_id, "movies"))
                    if not base:
                        base, folder = "movies", sanitize_folder_name(name)
                        parsed["kind"] = "movie"

                    pending_id = _new_pending_action("torrent", owner_id, base, folder, parsed, hash=hash_id, name=name)
                    await send_confirmation(bot_client, owner_id, pending_id)
                    # Only mark as watched AFTER the confirmation was sent successfully, so a
                    # failure (e.g. Telegram FloodWait) gets retried on the next poll.
                    state.watched_torrent_hashes.add(hash_id)
                except Exception as torrent_err:
                    logging.error(f"Failed to process staged torrent {hash_id}: {torrent_err}")
                    # Clean up the pending action that was created but never confirmed.
                    for pid, a in list(state.pending_actions.items()):
                        if a.get("type") == "torrent" and a.get("hash") == hash_id:
                            state.pending_actions.pop(pid, None)
                            break
        except Exception as e:
            logging.error(f"Error in watch_staged_torrents: {e}")
        try:
            state.clean_stale_state()
        except Exception as e:
            logging.warning(f"Error cleaning stale state: {e}")
        await asyncio.sleep(5)
