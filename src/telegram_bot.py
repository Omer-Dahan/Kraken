#!/usr/bin/env python3
"""
Telegram Hybrid Bot & Userbot Downloader entry point.
Wires up Bot/Userbot Telegram clients and handles top-level commands/events.
"""

import os
import time
import asyncio
import logging
import tempfile

from telethon import TelegramClient, events, errors
from telethon.sessions import StringSession

from fast_download import premium_connection_count
from media_organizer import parse_media_name, build_target_dir, fix_permissions, sanitize_file_name

import bot_state as state
from bot_config import (
    API_ID, API_HASH, BOT_TOKEN, SESSION_STRING, SESSION_PATH, BASE_DIR, SESSION_DIR,
    ALLOWED_USER_IDS, MEDIA_ROOT, STAGING_DIR, DOWNLOAD_CONNECTIONS, NON_PREMIUM_CONNECTIONS,
)
from keyboards import get_main_keyboard, send_menu_anchor, show_main_menu, main_menu_text, DESTINATIONS
from qbit_client import (
    add_torrent_to_qbit, qbit_set_location, qbit_delete_torrent,
    check_completed_torrents, _torrent_lock,
)
from download_engine import link_bot_and_userbot, start_download_workers, enqueue_group_downloads
from confirmation_flow import (
    _set_awaiting_text, _clear_awaiting_text, _handle_rename_reply, _add_to_batch,
    send_confirmation, adjust_season, flip_base, item_season,
)
from file_manager_ui import _fb_open, _fb_handle_callback, _fb_handle_rename_text, _fb_handle_mkdir_text
from torrents_screen import _tor_open, _tor_handle_callback, watch_staged_torrents
from jellyfin_screen import _jf_open, _jf_handle_callback, _jf_handle_search_text

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Extensions that mark a document as library media even when its mime type doesn't
# (plenty of releases arrive as application/octet-stream).
VIDEO_EXTENSIONS = frozenset({".mkv", ".mp4", ".avi", ".m4v", ".mov", ".iso", ".ts", ".webm"})


async def resolve_session():
    """Resolves the Telethon user session source (StringSession or .session file).

    Candidates are probed (connect + is_user_authorized) and the first ALREADY
    AUTHORIZED one wins, instead of trusting file existence/os.listdir() order.
    An unauthorized (fresh/empty) .session file - e.g. left over from a prior
    failed login attempt - must never be handed to the real client: Telethon's
    client.start() would then fall back to an interactive phone/code prompt,
    which has no stdin under systemd and crash-loops the service (EOFError).

    Every relative path here is resolved against BASE_DIR (the project root, one
    level above this src/ directory), never against the process's current working
    directory - do not reintroduce bare relative paths like "session" or ".".
    """
    if SESSION_STRING:
        logging.info("Using Telethon StringSession from environment.")
        return StringSession(SESSION_STRING)

    # Known filenames first, in priority order - deterministic, no directory-scan surprises.
    # Per-account files (userbot_<phone>.session) are picked up by the scan below instead of
    # being listed here, so no phone number ever ends up in the source.
    candidates = [
        os.path.join(SESSION_DIR, "userbot.session"),
        os.path.join(BASE_DIR, "userbot.session"),
    ]

    # Fallback: scan known directories for any other *.session file (sorted for determinism).
    for s_dir in [SESSION_DIR, BASE_DIR]:
        try:
            if os.path.isdir(s_dir):
                for f in sorted(os.listdir(s_dir)):
                    if f.endswith(".session") and not f.startswith("bot_session"):
                        p = os.path.join(s_dir, f)
                        if p not in candidates:
                            candidates.append(p)
        except Exception as e:
            logging.warning(f"Error checking directory {s_dir} for session files: {e}")

    candidates.append(SESSION_PATH if os.path.isabs(SESSION_PATH) else os.path.join(BASE_DIR, SESSION_PATH))

    for s_path in candidates:
        if not os.path.exists(s_path):
            continue
        session_name = s_path.rsplit(".session", 1)[0]
        probe = TelegramClient(session_name, API_ID, API_HASH, entity_cache_limit=100)
        try:
            await probe.connect()
            if await probe.is_user_authorized():
                logging.info(f"Using authorized Telethon user session: {s_path}")
                return session_name
            logging.warning(f"Session file '{s_path}' exists but is not authorized - skipping.")
        except Exception as e:
            logging.warning(f"Could not probe session file '{s_path}': {e}")
        finally:
            if probe.is_connected():
                await probe.disconnect()

    logging.error(
        "No authorized Telethon user session found among candidates: "
        f"{candidates}. Run generate_session.py interactively (in an actual "
        "terminal, not under systemd) to create one."
    )
    return SESSION_PATH.rsplit(".session", 1)[0]


def is_library_media(file_name, mime_type, has_document, is_sticker):
    """Whether an incoming media message is a file to put in the media library.

    The "no extension" branch exists for large releases sent without one, but on its own
    it also caught everything else that arrives with no filename: a photo carries no
    document at all, and voice notes and stickers are documents whose mime type gives them
    away. All three were being filed as invented .mp4s under /media, so each is now ruled
    out explicitly. Video stickers are checked first because their mime type really is
    video/webm - the extension alone can't tell them from a real release.
    """
    if is_sticker:
        return False
    ext = os.path.splitext(file_name)[1].lower()
    if mime_type.startswith("video/") or ext in VIDEO_EXTENSIONS:
        return True
    return has_document and not ext and not mime_type.startswith(("image/", "audio/"))


def is_user_authorized(user_id):
    """Checks if the user ID is in the whitelist."""
    if not ALLOWED_USER_IDS:
        logging.warning(f"Security: ALLOWED_USER_IDS is empty! Rejecting user {user_id}.")
        return False
    return user_id in ALLOWED_USER_IDS


async def maintenance_loop():
    """Periodic background task to clean stale runtime state and force Python GC to free memory."""
    import gc
    while True:
        await asyncio.sleep(1800)  # Runs every 30 minutes
        try:
            state.clean_stale_state()
            collected = gc.collect()
            logging.info(f"Memory maintenance task completed: pruned state, GC freed {collected} objects.")
        except Exception as e:
            logging.warning(f"Memory maintenance task error: {e}")


async def main():
    # 1. Initialize Bot Client (handles UI & interaction in Telegram Bot chat)
    # entity_cache_limit=500 prevents Telethon's internal entity cache from growing indefinitely in RAM
    os.makedirs(SESSION_DIR, exist_ok=True)
    bot_session_path = os.path.join(SESSION_DIR, "bot_session")
    bot_client = TelegramClient(bot_session_path, API_ID, API_HASH, entity_cache_limit=500)
    logging.info("Starting Telegram Bot Client...")
    await bot_client.start(bot_token=BOT_TOKEN)
    bot_info = await bot_client.get_me()
    logging.info(f"Bot connected successfully as: {bot_info.first_name} (@{bot_info.username})")

    # 2. Initialize Userbot Client (handles 4GB MTProto file downloads under the hood).
    # This must never take the whole service down: if the session isn't authorized (or
    # any other startup error occurs), the download queue's existing per-file fallback
    # (see download_engine._download_via/start_video_download) already runs everything
    # through the Bot client alone when userbot_peer stays None - so the Bot keeps
    # working in that case, just without the Userbot's higher throughput/no-20MB-limit
    # downloads.
    user_session = await resolve_session()
    user_client = None
    user_info = None
    try:
        candidate = TelegramClient(user_session, API_ID, API_HASH, entity_cache_limit=500)
        logging.info("Starting Telethon Userbot Engine...")
        await candidate.start()
        user_info = await candidate.get_me()
        user_client = candidate
        logging.info(f"Userbot Engine connected successfully as: {user_info.first_name} (@{user_info.username}) [ID: {user_info.id}]")
    except Exception as e:
        logging.error(f"Userbot Engine failed to start - continuing with Bot-only downloads: {e}")
        for uid in ALLOWED_USER_IDS:
            try:
                await bot_client.send_message(
                    uid,
                    "⚠️ *מנוע ה-Userbot נכשל בהתחברות* - הבוט ימשיך לעבוד, אבל הורדות "
                    "יתבצעו רק דרך חשבון הבוט הרגיל (איטי יותר, ומוגבל ל-20MB לקובץ), "
                    "עד שהבעיה תתוקן.\n"
                    f"פרטי השגיאה: `{e}`\n"
                    "כדי לתקן: הרץ `generate_session.py` בטרמינל אינטראקטיבי בשרת כדי ליצור "
                    "session מאושר מחדש, ואז הפעל מחדש את השירות."
                )
            except Exception as notify_err:
                logging.warning(f"Could not notify user {uid} about Userbot failure: {notify_err}")

    # When the Userbot never came up there is no id to recognise its handshake messages
    # by. Reading it straight off user_info made every single incoming message raise
    # AttributeError - which took the Bot-only fallback above down with it, since a
    # Telethon event handler that raises just logs and answers nothing.
    userbot_id = user_info.id if user_info else None

    # Telegram Premium lifts the per-account download throttle, so the connection budget
    # depends on which kind of account the Userbot is.
    state.userbot_is_premium = bool(getattr(user_info, "premium", False))
    state.active_connections = premium_connection_count(
        state.userbot_is_premium, DOWNLOAD_CONNECTIONS, NON_PREMIUM_CONNECTIONS
    )
    if user_client is None:
        logging.warning("Skipping Bot<->Userbot link - Userbot Engine unavailable.")
    elif state.userbot_is_premium:
        logging.info(f"Userbot account has Telegram Premium - using {state.active_connections} parallel connections.")
    else:
        logging.warning(
            f"Userbot account is NOT Telegram Premium - Telegram throttles download speed "
            f"(FLOOD_PREMIUM_WAIT). Capping to {state.active_connections} parallel connections "
            f"(configured: {DOWNLOAD_CONNECTIONS})."
        )

    # Establish the two-way peer link so the Bot can hand media over to the Userbot
    if user_client is not None:
        await link_bot_and_userbot(bot_client, user_client, bot_info, user_info)

    # Start background task for qBittorrent completion notifications
    state.create_background_task(check_completed_torrents(bot_client))

    # Start background task that offers folder-confirmation once a staged torrent's metadata arrives
    state.create_background_task(watch_staged_torrents(bot_client))

    # Start background task for periodic state cleanup and garbage collection
    state.create_background_task(maintenance_loop())

    # Start the two download-queue worker pools (Userbot account + Bot account) so they can
    # pull files concurrently - see start_download_workers.
    start_download_workers(bot_client, user_client)

    # Notify admin if a previous service restart wiped a non-empty download queue
    persisted_queue_count = state.pop_persisted_queue_count()
    if persisted_queue_count > 0 and ALLOWED_USER_IDS:
        for uid in ALLOWED_USER_IDS:
            try:
                await bot_client.send_message(
                    uid,
                    f"⚠️ *הודעת מערכת:* הבוט הופעל מחדש כאשר {persisted_queue_count} קבצים היו בתור ההורדה. "
                    f"הקבצים לא ירדו אוטומטית ויש לשלוח אותם מחדש במידת הצורך."
                )
            except Exception as notify_err:
                logging.warning(f"Could not send restart queue notice: {notify_err}")

    # Event handler for Inline Button callbacks
    @bot_client.on(events.CallbackQuery)
    async def callback_handler(event):
        sender_id = event.sender_id
        chat_id = event.chat_id
        if not is_user_authorized(sender_id):
            if sender_id not in state.unauthorized_notified_users:
                state.unauthorized_notified_users.add(sender_id)
                try:
                    await event.answer("⛔ שגיאת הרשאה! אינך מורשה להשתמש בבוט זה.", alert=True)
                except Exception:
                    pass
            return

        try:
            cq_data = event.data.decode("utf-8")
            current_mode = state.user_modes.get(chat_id, "movies")

            if cq_data.startswith("fb:"):
                await _fb_handle_callback(bot_client, event, chat_id, cq_data)
                return

            if cq_data.startswith("tor:"):
                await _tor_handle_callback(bot_client, event, chat_id, cq_data)
                return

            if cq_data.startswith("jf:"):
                await _jf_handle_callback(bot_client, event, chat_id, cq_data)
                return

            if cq_data in ("set_movies", "set_tv"):
                mode = "movies" if cq_data == "set_movies" else "tv"
                await event.answer(f"יעד ההורדה: {DESTINATIONS[mode][0]}")
                if mode == current_mode:
                    # Tapping the destination that's already active - the ✅ is already where it
                    # belongs, and re-editing identical content only earns a MessageNotModified.
                    return
                state.user_modes[chat_id] = mode
                await event.edit(main_menu_text(mode), buttons=get_main_keyboard(mode))

            elif cq_data == "help":
                await event.answer()
                help_text = (
                    "ℹ️ *איך משתמשים בבוט?*\n\n"
                    "1. **הורדת הטורנטים:** שלחו לי קישור `magnet:` או קובץ `.torrent`.\n"
                    "2. **הורדת וידאו ישיר מטלגרם:** שלחו קובץ וידאו (`.mkv`, `.mp4`, `.avi`) כקובץ/מדיה - עד 4GB!\n"
                    "   שליחת כמה קבצים ברצף מטופלת כאצווה אחת עם אישור אחד לכל סדרה/סרט שזוהו.\n"
                    "3. **שינוי יעד:** שורת הכפתורים העליונה - ה-✅ מסמן לאן יישמר מה שתשלחו.\n"
                    "   היעד שבחרתם גובר על הזיהוי האוטומטי, אלא אם השם עצמו מכיל עונה (`S02`, `Season 2`, `3x07`).\n"
                    "4. **בהודעת האישור:** `🔄` מעביר בין סרט לסדרה, ו-`➕/➖ עונה` קובע לאיזו `Season XX` הקובץ ייכנס.\n"
                    "5. **הורדות:** כפתור 📥 (או `/downloads`) - כל מה שיורד עכשיו, בלשוניות: טורנטים פעילים, "
                    "ממתינים, הושלמו, ותור ההורדות של טלגרם. המספר שעל הכפתור הוא כמה רצות ברגע זה.\n"
                    "6. **מנהל קבצים:** כפתור 🗂 מאפשר להעביר/לשנות שם/ליצור/למחוק תיקיות וקבצים תחת /media.\n"
                    "7. **Jellyfin:** כפתור 🍿 (או `/jellyfin`) - רענון ספריות, מי צופה עכשיו, חיפוש תוכן "
                    "וקישור ישיר לצפייה, ותיקון מטא-דאטה לתוכן שלא זוהה נכון."
                )
                await event.edit(help_text, buttons=get_main_keyboard(current_mode))

            elif cq_data.startswith(("flip:", "season:")):
                # Neither of these finalizes anything - they only adjust what the still-open
                # confirmation proposes - so unlike confirm/cancel below they must NOT claim
                # the pending action by popping it.
                kind, _, rest = cq_data.partition(":")
                pending_id, _, delta_str = rest.partition(":")
                action = state.pending_actions.get(pending_id)
                if not action:
                    await event.answer("⚠️ הפעולה כבר לא זמינה (אולי כבר טופלה).", alert=True)
                    return
                if action.get("chat_id") != chat_id:
                    await event.answer("⛔ אין לך הרשאה לבצע פעולה זו.", alert=True)
                    return

                if kind == "flip":
                    await event.answer("📺 סדרה" if flip_base(action) == "tv" else "🎬 סרט")
                else:
                    delta = int(delta_str) if delta_str.lstrip("-").isdigit() else 0
                    if not delta:
                        await event.answer()  # the middle button is the read-out, not a control
                        return
                    season = adjust_season(action, delta)
                    await event.answer(f"עונה {season}" if season is not None else "")
                await send_confirmation(bot_client, chat_id, pending_id)

            elif cq_data.startswith(("confirm:", "use_existing:", "rename:", "cancel:")):
                action_type, _, pending_id = cq_data.partition(":")

                if action_type == "rename":
                    # Doesn't finalize the action - just peeks at it to prompt for new text, so it
                    # doesn't need (and must not do) the claim-by-pop below.
                    action = state.pending_actions.get(pending_id)
                    if not action:
                        await event.answer("⚠️ הפעולה כבר לא זמינה (אולי כבר טופלה).", alert=True)
                        return
                    if action.get("chat_id") != chat_id:
                        await event.answer("⛔ אין לך הרשאה לבצע פעולה זו.", alert=True)
                        return
                    await _set_awaiting_text(bot_client, chat_id, "rename", pending_id)
                    await event.answer()
                    await event.edit(f"✏️ שלח/י הודעת טקסט עם השם החדש עבור *{action['folder']}*.")
                    return

                # cancel / confirm / use_existing all finalize the pending action, so claim it by
                # popping BEFORE any await (asyncio only switches tasks at await points - see the
                # confirmation_flow._add_to_batch/_finalize_batch comments for the same pattern
                # elsewhere in this codebase). This prevents a double-tap on this same button (or
                # the "טורנטים פעילים" screen's delete landing on the same pending_id) from acting
                # on it twice; the separate _torrent_lock below (see qbit_set_location/
                # qbit_delete_torrent calls) is what actually prevents a relocate racing a delete
                # on the same qBittorrent hash - the pop alone only protects this dict entry, not
                # the physical file operation. Whichever callback's synchronous prefix runs first
                # here wins the pending_actions entry; the other finds it already gone and exits
                # cleanly below.
                action = state.pending_actions.get(pending_id)
                if not action:
                    await event.answer("⚠️ הפעולה כבר לא זמינה (אולי כבר טופלה).", alert=True)
                    return
                if action.get("chat_id") != chat_id:
                    await event.answer("⛔ אין לך הרשאה לבצע פעולה זו.", alert=True)
                    return
                state.pending_actions.pop(pending_id, None)
                _clear_awaiting_text(chat_id, pending_id)

                if action_type == "cancel":
                    await event.answer("בוטל.")
                    if action["type"] == "torrent":
                        async with _torrent_lock(action["hash"]):
                            await qbit_delete_torrent(action["hash"], delete_files=True)
                        # Otherwise the same magnet/torrent re-added later (same info-hash) would
                        # be silently skipped forever by watch_staged_torrents - see design notes.
                        state.watched_torrent_hashes.discard(action["hash"])
                        await event.edit(f"❌ בוטל. הטורנט *{action['name']}* נמחק מ-qBittorrent.")
                    else:
                        await event.edit(f"❌ בוטל. {len(action['items'])} קבצים לא יורדו.")
                    await send_menu_anchor(bot_client, chat_id)
                    return

                # confirm / use_existing
                folder = action["candidate"] if (action_type == "use_existing" and action["candidate"]) else action["folder"]

                if action["type"] == "torrent":
                    await event.answer("מאשר...")
                    target_dir = build_target_dir(MEDIA_ROOT, action["base"], folder, action.get("season"))
                    os.makedirs(target_dir, exist_ok=True)
                    fix_permissions(target_dir)
                    # Locked against the torrents-screen delete handler, which targets the
                    # same qBittorrent hash - see _torrent_lock for why the pending_actions pop
                    # above isn't enough on its own to prevent a delete-with-files racing this
                    # relocate and wiping the files this call is about to organize.
                    async with _torrent_lock(action["hash"]):
                        if await qbit_set_location(action["hash"], target_dir):
                            await event.edit(f"✅ הטורנט *{action['name']}* יוצב ב-`{target_dir}`.")
                        else:
                            await event.edit(f"❌ שגיאה בהעברת הטורנט ל-`{target_dir}`.")
                else:
                    # #15: Build targets BEFORE await event.answer so no race window exists
                    targets = [
                        (item["message"],
                         build_target_dir(MEDIA_ROOT, action["base"], folder, item_season(action, item["parsed"])),
                         item["file_name"])
                        for item in action["items"]
                    ]
                    target_dir_display = build_target_dir(MEDIA_ROOT, action["base"], folder, action.get("season"))
                    await event.answer("מאשר...")
                    await event.edit(f"✅ {len(targets)} קבצים נוספו לתור ההורדה, יעד: `{target_dir_display}`")
                    # #16: Pass the actual folder chosen (which may be action["candidate"])
                    enqueue_group_downloads(bot_client, user_client, chat_id, folder, targets)

                # The confirmation message stays where it is (edited into its own result), so
                # the refreshed menu goes to the bottom of the chat where the user is looking.
                await send_menu_anchor(bot_client, chat_id)

            else:
                # A button from a menu drawn by an older version - the standalone status and
                # queue buttons the downloads screen replaced, say. Telegram keeps old inline
                # keyboards alive and tappable forever, and an unanswered callback just spins,
                # so turn whatever was tapped back into a current menu.
                await event.answer("התפריט הזה מיושן, מרענן.", alert=True)
                await show_main_menu(bot_client, chat_id, event.message_id)

        except (errors.FloodWaitError, errors.FloodPremiumWaitError) as e:
            wait = getattr(e, "seconds", 60) or 60
            logging.warning(f"FloodWait error during callback_handler for chat {chat_id}: {e}")
            try:
                await event.answer(f"⏳ טלגרם הגביל בקשות זמנית ({wait} שניות). אנא נסה שוב מאוחר יותר.", alert=True)
            except Exception:
                pass
        except Exception as e:
            logging.error(f"Unhandled exception on callback_handler: {e}", exc_info=True)

    # Event handler for incoming messages sent to the BOT.
    #
    # Split into a thin wrapper plus the real body so that anything escaping the body
    # is logged and answered instead of vanishing: an exception raised inside a Telethon
    # event handler only reaches Telethon's own logger, so from the chat's point of view
    # the bot simply stops replying, with no clue as to why. Same shape as
    # callback_handler above, which has had this guard from the start.
    @bot_client.on(events.NewMessage)
    async def bot_message_handler(event):
        try:
            await handle_incoming_message(event)
        except (errors.FloodWaitError, errors.FloodPremiumWaitError) as e:
            logging.warning(f"FloodWait during bot_message_handler for chat {event.chat_id}: {e}")
        except Exception as e:
            logging.error(f"Unhandled exception on bot_message_handler: {e}", exc_info=True)
            try:
                await event.respond(f"❌ שגיאה בלתי צפויה בטיפול בהודעה:\n`{e}`")
            except Exception as reply_err:
                logging.warning(f"Could not report the handler error to the chat: {reply_err}")

    async def handle_incoming_message(event):
        sender_id = event.sender_id
        chat_id = event.chat_id

        # The Userbot is internal plumbing, not a user: swallow its handshake messages
        # instead of answering them with an "access denied" reply.
        if userbot_id is not None and sender_id == userbot_id:
            return

        logging.info(f"Bot received message from sender {sender_id} in chat {chat_id}")

        if not is_user_authorized(sender_id):
            if sender_id not in state.unauthorized_notified_users:
                state.unauthorized_notified_users.add(sender_id)
                logging.info(f"Security: Notified unauthorized user ID: {sender_id}")
                await event.respond("⛔ *גישה נדחתה.*\nאין לך הרשאה להשתמש בבוט זה.")
            else:
                logging.info(f"Security: Silently ignored message from unauthorized user ID: {sender_id}")
            return

        text = (event.message.message or "").strip()

        # A pending rename/mkdir text reply is waiting - but ONLY if this message has no
        # media. A file arriving while a text prompt is open must fall through to the
        # normal batching logic below instead of being silently swallowed as "the reply".
        awaiting = state.awaiting_text_input.get(chat_id)
        if awaiting and not event.message.media:
            if text.startswith("/"):
                # A slash-command typed while a rename/mkdir prompt is open almost
                # certainly means the user wants the command, not to name something
                # "/status" - drop the pending prompt and fall through to normal command
                # handling below instead of consuming it as the literal new name.
                state.awaiting_text_input.pop(chat_id, None)
            else:
                state.awaiting_text_input.pop(chat_id, None)
                if text:
                    kind, target = awaiting["kind"], awaiting["target"]
                    if kind == "rename":
                        await _handle_rename_reply(bot_client, chat_id, target, text)
                    elif kind == "fb_rename":
                        await _fb_handle_rename_text(bot_client, chat_id, target, text)
                    elif kind == "fb_mkdir":
                        await _fb_handle_mkdir_text(bot_client, chat_id, target, text)
                    elif kind == "jf_search":
                        await _jf_handle_search_text(bot_client, chat_id, target, text)
                return

        current_mode = state.user_modes.get(chat_id, "movies")
        target_path = f"/media/{current_mode}"

        # Commands. Anything that answers with the main keyboard goes through send_menu_anchor,
        # which drops the reply at the bottom of the chat and clears the previous menu - see
        # keyboards.send_menu_anchor. The screens (/files, /torrents, /jellyfin) pass no message
        # id, so a typed command starts a fresh screen at the bottom instead of quietly editing
        # an older one further up the chat.
        if text in ["/start", "/menu", "/help"]:
            await send_menu_anchor(bot_client, chat_id)
            return

        # The redrawn menu is the confirmation here - it names the new destination in its
        # header and moves the ✅ - so these don't need a message of their own any more.
        if text in ["/movies", "/set_movies"]:
            state.user_modes[chat_id] = "movies"
            await send_menu_anchor(bot_client, chat_id)
            return

        if text in ["/tv", "/set_tv"]:
            state.user_modes[chat_id] = "tv"
            await send_menu_anchor(bot_client, chat_id)
            return

        if text in ["/status", "/downloads"]:
            await _tor_open(bot_client, chat_id)
            return

        if text == "/queue":
            await _tor_open(bot_client, chat_id, filter_="queue")
            return

        if text == "/files":
            await _fb_open(bot_client, chat_id)
            return

        if text == "/torrents":
            await _tor_open(bot_client, chat_id)
            return

        if text == "/jellyfin":
            await _jf_open(bot_client, chat_id)
            return

        # Magnet Links
        if text.startswith("magnet:") or text.startswith("http://") or text.startswith("https://"):
            os.makedirs(STAGING_DIR, exist_ok=True)
            if await add_torrent_to_qbit(urls=text, save_path=STAGING_DIR):
                await send_menu_anchor(
                    bot_client, chat_id,
                    "📥 *הקישור נשלח ל-qBittorrent.*\nממתין למידע (metadata) על התוכן כדי להציע תיקייה מתאימה..."
                )
            else:
                await send_menu_anchor(bot_client, chat_id, "❌ שגיאה בשליחת הטורנט ל-qBittorrent.")
            return

        # Check for media (Documents / Videos / Torrent files)
        if event.message.media:
            # DocumentAttributeFilename is whatever the sender put there, and this value
            # is later joined onto a target directory - so it is reduced to a bare name,
            # with no separators and no "..", the moment it enters the bot.
            file_name = sanitize_file_name(getattr(event.message.file, "name", None))

            if file_name.endswith(".torrent"):
                # mkstemp rather than a timestamped name under /tmp: two .torrent files
                # sent in the same second used to land on the same path, and the name
                # itself no longer has any say in where the file is written.
                temp_fd, temp_torrent_path = tempfile.mkstemp(suffix=".torrent")
                os.close(temp_fd)
                try:
                    await bot_client.download_media(event.message, file=temp_torrent_path)
                    os.makedirs(STAGING_DIR, exist_ok=True)
                    if await add_torrent_to_qbit(torrent_file_path=temp_torrent_path, save_path=STAGING_DIR):
                        await send_menu_anchor(
                            bot_client, chat_id,
                            f"📥 *קובץ הטורנט `{file_name}` נשלח ל-qBittorrent.*\nממתין למידע על התוכן כדי להציע תיקייה מתאימה..."
                        )
                    else:
                        await send_menu_anchor(bot_client, chat_id, "❌ שגיאה בשליחת קובץ הטורנט ל-qBittorrent.")
                except Exception as e:
                    logging.error(f"Error downloading or processing torrent file: {e}")
                    await send_menu_anchor(bot_client, chat_id, "❌ שגיאה בהורדת קובץ הטורנט.")
                finally:
                    if os.path.exists(temp_torrent_path):
                        os.remove(temp_torrent_path)
                return

            # Direct Video / Large Document Download (ANY SIZE up to 4GB!) - buffered into
            # a batch so a burst of files gets organized as one unit (see confirmation_flow._add_to_batch).
            if is_library_media(
                file_name,
                getattr(event.message.file, "mime_type", "") or "",
                has_document=getattr(event.message, "document", None) is not None,
                is_sticker=getattr(event.message, "sticker", None) is not None,
            ):
                if not file_name:
                    file_name = f"telegram_video_{int(time.time())}.mp4"

                parsed = parse_media_name(file_name)
                await _add_to_batch(bot_client, user_client, chat_id, event.message, file_name, parsed, target_path)
                return

        # Fallback for unrecognized plain text messages
        if text:
            await send_menu_anchor(
                bot_client, chat_id,
                "💡 שלח קישור Magnet, קובץ `.torrent`, או קובץ וידאו.\nלהצגת התפריט לחץ /menu."
            )

    logging.info("Hybrid Bot & Userbot Downloader is running and listening...")
    if user_client is not None:
        await asyncio.gather(
            bot_client.run_until_disconnected(),
            user_client.run_until_disconnected()
        )
    else:
        await bot_client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
