"""Telethon UI rendering layer for mini file manager (MEDIA_ROOT)."""

import asyncio
import time
import posixpath
import logging

from telethon import Button, errors

import bot_state as state
import file_browser
import keyboards
from bot_config import MEDIA_ROOT, FB_PAGE_SIZE
from confirmation_flow import _set_awaiting_text


def _fb_new_session(cwd=""):
    return {"cwd": cwd, "mode": "browse", "entries": [], "page": 0, "selected": None, "msg_id": None, "epoch": 0, "last_accessed": time.time()}


async def _fb_open(bot_client, chat_id, msg_id=None):
    """Opens the file browser at MEDIA_ROOT on target chat/msg_id."""
    session = state.fb_sessions.get(chat_id)
    if session is None:
        session = state.fb_sessions[chat_id] = _fb_new_session()
    session["cwd"] = ""
    session["mode"] = "browse"
    session["selected"] = None
    session["page"] = 0
    session["msg_id"] = msg_id
    session["last_accessed"] = time.time()
    if msg_id:
        keyboards.claim_message(chat_id, msg_id)
    await _fb_render(bot_client, chat_id)


def _fb_list(cwd):
    abs_dir = file_browser.safe_join(MEDIA_ROOT, cwd)
    dirs, files = file_browser.list_entries(abs_dir)
    return [(True, d) for d in dirs] + [(False, f) for f in files]


def _fb_breadcrumb(cwd):
    return f"/media/{cwd}" if cwd else "/media"


async def _fb_render(bot_client, chat_id, status_line=None):
    """
    Renders (or re-renders in place) the file-browser message. `status_line` is an optional
    one-off line prepended to the header for this single render - used to fold a "✅ created"/
    "❌ error" result into the same message instead of sending a separate new one.
    """
    session = state.fb_sessions.get(chat_id)
    if not session:
        return
    session["last_accessed"] = time.time()


    entries = _fb_list(session["cwd"])
    moving_name = None
    if session["mode"] == "move_dest":
        entries = [e for e in entries if e[0]]  # can only move INTO a folder
        if session["selected"]:
            moving_name = posixpath.basename(session["selected"])
            moving_parent = posixpath.dirname(session["selected"])
            if moving_parent == session["cwd"]:
                entries = [e for e in entries if e[1] != moving_name]

    session["entries"] = entries
    session["epoch"] += 1
    epoch = session["epoch"]

    total_pages = max(1, -(-len(entries) // FB_PAGE_SIZE))
    session["page"] = min(session["page"], total_pages - 1)
    start = session["page"] * FB_PAGE_SIZE
    page_entries = list(enumerate(entries))[start:start + FB_PAGE_SIZE]

    rows = []
    if session["mode"] == "move_dest":
        rows.append([Button.inline("✅ העבר לכאן", data=f"fb:domove:{epoch}")])

    for i, (is_dir, name) in page_entries:
        icon = "📂" if is_dir else "📄"
        if session["mode"] == "move_dest":
            rows.append([Button.inline(f"{icon} {name}", data=f"fb:nav:{epoch}:{i}")])
        elif is_dir:
            rows.append([
                Button.inline(f"{icon} {name}", data=f"fb:nav:{epoch}:{i}"),
                Button.inline("⚙️", data=f"fb:act:{epoch}:{i}"),
            ])
        else:
            rows.append([Button.inline(f"{icon} {name}", data=f"fb:act:{epoch}:{i}")])

    if total_pages > 1:
        page_row = []
        if session["page"] > 0:
            page_row.append(Button.inline("◀️ הקודם", data=f"fb:page:{epoch}:{session['page'] - 1}"))
        if session["page"] < total_pages - 1:
            page_row.append(Button.inline("➡️ הבא", data=f"fb:page:{epoch}:{session['page'] + 1}"))
        if page_row:
            rows.append(page_row)

    # Navigation (up / new folder) is available in BOTH modes - move-destination browsing
    # needs it just as much as normal browsing does. Without "up", the only reachable
    # destinations were subfolders of wherever the item already lives, with no way to cross
    # into a sibling branch (e.g. tv -> movies) or go anywhere else; without "new folder",
    # there was no way to move into a destination that didn't already exist.
    footer = []
    if session["cwd"]:
        footer.append(Button.inline("⬆️ למעלה", data=f"fb:up:{epoch}"))
    footer.append(Button.inline("🆕 תיקייה", data=f"fb:mkdir:{epoch}"))
    if session["mode"] == "move_dest":
        footer.append(Button.inline("🔙 חזרה", data=f"fb:cancelmove:{epoch}"))
    else:
        footer.append(Button.inline("🏠 תפריט ראשי", data=f"fb:home:{epoch}"))
    rows.append(footer)

    if session["mode"] == "move_dest":
        header = f"📦 בחר יעד להעברת: *{moving_name}*\n📁 {_fb_breadcrumb(session['cwd'])}"
    else:
        header = f"🗂 *מנהל קבצים*\n📁 {_fb_breadcrumb(session['cwd'])}"
        if not entries:
            header += "\n_(תיקייה ריקה)_"

    if status_line:
        header = f"{status_line}\n\n{header}"

    if session.get("msg_id"):
        try:
            msg = await bot_client.edit_message(chat_id, session["msg_id"], header, buttons=rows)
        except (errors.FloodWaitError, errors.FloodPremiumWaitError) as e:
            logging.warning(f"Telegram FloodWait editing file-browser message: {e}")
            return
        except Exception as e:
            logging.warning(f"Failed to edit file-browser message, sending a new one: {e}")
            msg = await bot_client.send_message(chat_id, header, buttons=rows)
    else:
        msg = await bot_client.send_message(chat_id, header, buttons=rows)
    if 'msg' in locals():
        session["msg_id"] = msg.id


async def _fb_render_action_menu(bot_client, chat_id, entry_index):
    session = state.fb_sessions.get(chat_id)
    if not session:
        return
    try:
        is_dir, name = session["entries"][entry_index]
    except IndexError:
        return

    session["selected"] = f"{session['cwd']}/{name}" if session["cwd"] else name
    session["epoch"] += 1
    epoch = session["epoch"]

    header = f"מה לעשות עם: *{name}*?"
    rows = [
        [Button.inline("➡️ העבר", data=f"fb:move:{epoch}")],
        [Button.inline("✏️ שנה שם", data=f"fb:rename:{epoch}")],
        # Move and rename act on session["selected"], which was just set above; delete
        # re-resolves the entry from the listing (it needs is_dir for its warning text),
        # so it has to carry the index the way fb:nav / fb:act do.
        [Button.inline("🗑 מחק", data=f"fb:delete:{epoch}:{entry_index}")],
        [Button.inline("🔙 חזרה", data=f"fb:back:{epoch}")],
    ]
    try:
        msg = await bot_client.edit_message(chat_id, session["msg_id"], header, buttons=rows)
    except (errors.FloodWaitError, errors.FloodPremiumWaitError) as e:
        logging.warning(f"Telegram FloodWait editing file-browser action menu: {e}")
        return
    except Exception as e:
        # Same fallback as _fb_render: an edit can fail (e.g. the message is too old to
        # edit), and without a fallback the tap would silently do nothing at all.
        logging.warning(f"Failed to edit file-browser action menu, sending a new one: {e}")
        msg = await bot_client.send_message(chat_id, header, buttons=rows)
    if 'msg' in locals():
        session["msg_id"] = msg.id


async def _fb_render_delete_confirm(bot_client, chat_id, entry_index):
    """A one-step-removed confirmation screen for delete - it's the only irreversible action
    in the file manager (move/rename/mkdir can all be undone by doing the opposite action;
    a deleted file is just gone), so it doesn't fire straight from the action menu."""
    session = state.fb_sessions.get(chat_id)
    if not session:
        return
    try:
        is_dir, name = session["entries"][entry_index]
    except IndexError:
        return

    session["selected"] = f"{session['cwd']}/{name}" if session["cwd"] else name
    session["epoch"] += 1
    epoch = session["epoch"]

    kind_label = "התיקייה על כל תוכנה" if is_dir else "הקובץ"
    header = f"⚠️ למחוק לצמיתות את {kind_label} *{name}*?\nלא ניתן לשחזר את זה."
    rows = [
        [Button.inline("🗑 כן, מחק לצמיתות", data=f"fb:dodelete:{epoch}")],
        [Button.inline("🔙 חזרה", data=f"fb:back:{epoch}")],
    ]
    try:
        msg = await bot_client.edit_message(chat_id, session["msg_id"], header, buttons=rows)
    except (errors.FloodWaitError, errors.FloodPremiumWaitError) as e:
        logging.warning(f"Telegram FloodWait editing file-browser delete-confirm: {e}")
        return
    except Exception as e:
        logging.warning(f"Failed to edit file-browser delete-confirm, sending a new one: {e}")
        msg = await bot_client.send_message(chat_id, header, buttons=rows)
    if 'msg' in locals():
        session["msg_id"] = msg.id


async def _fb_handle_mkdir_text(bot_client, chat_id, target_cwd, text):
    try:
        file_browser.make_dir(MEDIA_ROOT, target_cwd, text)
        status = f"✅ נוצרה תיקייה: *{text}*"
    except file_browser.FileManagerError as e:
        status = f"❌ {e}"
    # Folds the result into the same browser message instead of sending a separate new one -
    # only falls back to a plain message if the browser session itself is already gone
    # (e.g. the user closed it while the "send a name" prompt was open).
    if chat_id in state.fb_sessions:
        await _fb_render(bot_client, chat_id, status_line=status)
    else:
        await bot_client.send_message(chat_id, status)


async def _fb_handle_rename_text(bot_client, chat_id, target_rel, text):
    try:
        file_browser.rename_entry(MEDIA_ROOT, target_rel, text)
        status = f"✅ שונה השם ל-*{text}*"
    except file_browser.FileManagerError as e:
        status = f"❌ {e}"
    session = state.fb_sessions.get(chat_id)
    if session:
        session["mode"] = "browse"
        session["selected"] = None
        await _fb_render(bot_client, chat_id, status_line=status)
    else:
        await bot_client.send_message(chat_id, status)


def _int_part(parts, index):
    """The integer at parts[index], or None when the callback data doesn't carry one.

    Telegram keeps old inline keyboards tappable forever, so a button drawn by an older
    version of this screen can arrive with fewer parts than the handler expects. Indexing
    blindly turned that into an IndexError swallowed by telegram_bot's top-level callback
    guard - i.e. a button that spins and then silently does nothing at all.
    """
    try:
        return int(parts[index])
    except (IndexError, ValueError):
        return None


async def _fb_handle_callback(bot_client, event, chat_id, cq_data):
    parts = cq_data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "open":
        await event.answer()
        # The browser takes over the message that was tapped (usually the main menu), so
        # opening it is an in-place transition rather than a new message in the chat.
        await _fb_open(bot_client, chat_id, msg_id=event.message_id)
        return

    session = state.fb_sessions.get(chat_id)
    if not session:
        await event.answer("⚠️ הסשן פג, פותח מחדש.", alert=True)
        await _fb_open(bot_client, chat_id, msg_id=event.message_id)
        return

    # Every remaining action carries the epoch of the render it was drawn from [R3]:
    # Telegram never invalidates old inline keyboards, so without this a double-tap, a tap
    # on a stale/previous page, or a listing that changed underneath could make an index
    # resolve to a different file/folder than what's on screen - the worst failure mode for
    # a UI that moves and renames real files. Rejecting any mismatch (epoch OR message id)
    # forces a fresh render instead of trusting stale button data.
    epoch = _int_part(parts, 2)
    if epoch is None:
        await event.answer()
        return
    if epoch != session["epoch"] or event.message_id != session.get("msg_id"):
        await event.answer("⚠️ התצוגה לא מעודכנת, מרענן...", alert=True)
        await _fb_render(bot_client, chat_id)
        return

    # Every index-carrying action shares one guard rather than four copies of it.
    arg = _int_part(parts, 3)
    if action in ("nav", "act", "page", "delete") and arg is None:
        await event.answer("⚠️ התצוגה לא מעודכנת, מרענן...", alert=True)
        await _fb_render(bot_client, chat_id)
        return

    if action == "nav":
        i = arg
        try:
            is_dir, name = session["entries"][i]
        except IndexError:
            await event.answer()
            return
        if session["mode"] == "move_dest" or is_dir:
            session["cwd"] = f"{session['cwd']}/{name}" if session["cwd"] else name
            session["page"] = 0
        await event.answer()
        await _fb_render(bot_client, chat_id)
        return

    if action == "act":
        await event.answer()
        await _fb_render_action_menu(bot_client, chat_id, arg)
        return

    if action == "page":
        session["page"] = arg
        await event.answer()
        await _fb_render(bot_client, chat_id)
        return

    if action == "up":
        session["cwd"] = posixpath.dirname(session["cwd"])
        session["page"] = 0
        await event.answer()
        await _fb_render(bot_client, chat_id)
        return

    if action == "home":
        state.fb_sessions.pop(chat_id, None)
        await event.answer()
        await keyboards.show_main_menu(bot_client, chat_id, event.message_id)
        return

    if action == "back":
        session["mode"] = "browse"
        session["selected"] = None
        await event.answer()
        await _fb_render(bot_client, chat_id)
        return

    if action == "mkdir":
        await event.answer()
        await _set_awaiting_text(bot_client, chat_id, "fb_mkdir", session["cwd"])
        await bot_client.edit_message(
            chat_id, session["msg_id"],
            f"🆕 שלח/י שם לתיקייה החדשה בתוך `{_fb_breadcrumb(session['cwd'])}`."
        )
        return

    if action == "rename":
        await event.answer()
        await _set_awaiting_text(bot_client, chat_id, "fb_rename", session["selected"])
        await bot_client.edit_message(
            chat_id, session["msg_id"],
            f"✏️ שלח/י שם חדש עבור `{posixpath.basename(session['selected'])}`."
        )
        return

    if action == "move":
        session["mode"] = "move_dest"
        session["page"] = 0
        await event.answer()
        await _fb_render(bot_client, chat_id)
        return

    if action == "cancelmove":
        session["mode"] = "browse"
        session["selected"] = None
        session["page"] = 0
        await event.answer()
        await _fb_render(bot_client, chat_id)
        return

    if action == "domove":
        moved_name = posixpath.basename(session["selected"]) if session["selected"] else ""
        try:
            await asyncio.to_thread(file_browser.move_entry, MEDIA_ROOT, session["selected"], session["cwd"])
            session["mode"] = "browse"
            session["selected"] = None
            session["page"] = 0
            await event.answer("✅ הועבר בהצלחה.")
        except file_browser.FileManagerError as e:
            await event.answer(str(e), alert=True)
            return
        await _fb_render(bot_client, chat_id, status_line=f"✅ *{moved_name}* הועבר לכאן.")
        return

    if action == "delete":
        await event.answer()
        await _fb_render_delete_confirm(bot_client, chat_id, arg)
        return

    if action == "dodelete":
        deleted_name = posixpath.basename(session["selected"]) if session["selected"] else ""
        try:
            # A large show folder can be gigabytes - shutil.rmtree runs on the single asyncio
            # event loop otherwise, stalling every other chat's callbacks and the qBittorrent
            # polling loops for the whole operation. Unlike this file's other file_browser.*
            # calls (rename/move/mkdir are near-instant metadata operations), delete's blocking
            # cost is unbounded, so it alone gets offloaded to a thread.
            await asyncio.to_thread(file_browser.delete_entry, MEDIA_ROOT, session["selected"])
            session["mode"] = "browse"
            session["selected"] = None
            await event.answer("🗑 נמחק.")
        except file_browser.FileManagerError as e:
            await event.answer(str(e), alert=True)
            return
        await _fb_render(bot_client, chat_id, status_line=f"🗑 *{deleted_name}* נמחק לצמיתות.")
        return

    await event.answer()
