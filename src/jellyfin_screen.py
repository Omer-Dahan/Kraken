"""Jellyfin management screen: menu -> library scan / active viewers / search results ->
item card (watch link + metadata repair).

The Telethon rendering layer for jellyfin_client.py, following the same shape as
torrents_screen.py: one message per chat, edited in place, with a `gen` counter so a slower
render started by an earlier tap can't overwrite a newer one.
"""

import time
import logging

from telethon import Button, errors
from telethon.tl.types import KeyboardButtonUrl

import bot_state as state
import jellyfin_client as jf
import keyboards
from confirmation_flow import _set_awaiting_text

# Jellyfin item ids are 32-char hex, so "jf:item:<id>" is 40 bytes - comfortably inside
# Telegram's 64-byte callback_data cap, which is why items are addressed by id here rather
# than by list index the way the file manager has to.
SEARCH_RESULT_LIMIT = 8


def _jf_new_session(msg_id=None):
    return {"msg_id": msg_id, "mode": "menu", "results": [], "gen": 0, "last_accessed": time.time()}


def _strip_url_buttons(rows):
    kept = [[b for b in row if not isinstance(b, KeyboardButtonUrl)] for row in rows]
    return [row for row in kept if row]


async def _jf_send_or_edit(bot_client, chat_id, session, text, rows):
    """
    Renders the screen in place, or sends it if this chat has no Jellyfin message yet.

    Telegram validates URL buttons server-side and can reject a host outright, which fails
    the whole message - so a rejected keyboard is retried once without its link buttons. The
    watch URL is always in the message text too (Telegram auto-links it), so nothing is lost.
    """
    import time
    session["last_accessed"] = time.time()
    async def _deliver(buttons):
        if session.get("msg_id"):
            try:
                return await bot_client.edit_message(chat_id, session["msg_id"], text, buttons=buttons)
            except (errors.FloodWaitError, errors.FloodPremiumWaitError):
                raise
            except errors.ButtonUrlInvalidError:
                raise
            except errors.MessageNotModifiedError:
                raise
            except Exception as e:
                logging.warning(f"Failed to edit Jellyfin screen, sending a new one: {e}")
        return await bot_client.send_message(chat_id, text, buttons=buttons)

    try:
        msg = await _deliver(rows)
    except (errors.FloodWaitError, errors.FloodPremiumWaitError) as e:
        logging.warning(f"Telegram FloodWait rendering the Jellyfin screen: {e}")
        return
    except errors.MessageNotModifiedError:
        return  # a refresh that found nothing new - the screen already shows it
    except errors.ButtonUrlInvalidError:
        logging.warning("Telegram rejected the Jellyfin URL button - falling back to the link in the text.")
        try:
            msg = await _deliver(_strip_url_buttons(rows))
        except Exception as e:
            logging.error(f"Failed to render the Jellyfin screen: {e}")
            return
    except Exception as e:
        logging.error(f"Failed to render the Jellyfin screen: {e}")
        return
    session["msg_id"] = msg.id


async def _jf_render_menu(bot_client, chat_id, status_line=None):
    session = state.jellyfin_sessions.get(chat_id)
    if not session:
        return
    session["last_accessed"] = time.time()
    session["mode"] = "menu"
    session["gen"] += 1

    text = "🍿 *ניהול Jellyfin*"
    if not jf.is_configured():
        text += (
            "\n\n⚠️ לא מוגדר מפתח API.\n"
            "צור מפתח ב-Jellyfin (Dashboard ← Advanced ← API Keys) והגדר את משתנה הסביבה "
            "`JELLYFIN_API_KEY` בשירות."
        )
    else:
        info = await jf.system_info()
        if isinstance(info, dict):
            text += f"\n🖥 שרת: *{info.get('ServerName', '?')}* (גרסה {info.get('Version', '?')})"
        else:
            text += "\n⚠️ השרת לא מגיב כרגע."
    if status_line:
        text = f"{status_line}\n\n{text}"

    rows = [
        [
            Button.inline("🔄 רענון ספריות", data="jf:scan"),
            Button.inline("🔍 חיפוש תוכן", data="jf:search"),
        ],
        [Button.inline("📊 צופים פעילים", data="jf:sessions")],
        [Button.inline("🏠 תפריט ראשי", data="jf:home")],
    ]
    await _jf_send_or_edit(bot_client, chat_id, session, text, rows)


def _format_session(s):
    """One viewer line: who, on what device, and what they're watching right now."""
    user = s.get("UserName") or "?"
    device = s.get("DeviceName") or s.get("Client") or "?"
    now_playing = s.get("NowPlayingItem")
    if not now_playing:
        return f"• *{user}* ({device}) — לא מנגן כרגע"

    name = now_playing.get("Name") or "?"
    season, episode = now_playing.get("ParentIndexNumber"), now_playing.get("IndexNumber")
    if now_playing.get("SeriesName"):
        marker = f" S{season:02d}E{episode:02d}" if season is not None and episode is not None else ""
        name = f"{now_playing['SeriesName']}{marker} — {name}"

    # Jellyfin reports durations and positions in ticks (100-nanosecond units).
    runtime = (now_playing.get("RunTimeTicks") or 0) // 10_000_000
    position = ((s.get("PlayState") or {}).get("PositionTicks") or 0) // 10_000_000
    progress = f"  [{position // 60}:{position % 60:02d} / {runtime // 60}:{runtime % 60:02d}]" if runtime else ""
    paused = "⏸" if (s.get("PlayState") or {}).get("IsPaused") else "▶️"
    return f"• {paused} *{user}* ({device})\n  🎬 {name}{progress}"


async def _jf_render_sessions(bot_client, chat_id):
    session = state.jellyfin_sessions.get(chat_id)
    if not session:
        return
    session["last_accessed"] = time.time()
    session["mode"] = "sessions"
    session["gen"] += 1
    gen = session["gen"]

    sessions = await jf.get_sessions()
    if session.get("gen") != gen:
        return  # a newer tap already started rendering something else

    watching = [s for s in sessions if s.get("NowPlayingItem")]
    idle_count = len(sessions) - len(watching)

    lines = [f"📊 *צופים פעילים* ({len(watching)})\n"]
    lines.extend(_format_session(s) for s in watching)
    if not watching:
        lines.append("_אף אחד לא צופה כרגע._")
    if idle_count:
        lines.append(f"\n💤 מחוברים ולא מנגנים: {idle_count}")

    rows = [
        [Button.inline("🔄 רענן", data="jf:sessions")],
        [Button.inline("🔙 חזרה", data="jf:menu")],
    ]
    await _jf_send_or_edit(bot_client, chat_id, session, "\n".join(lines), rows)


async def _jf_render_results(bot_client, chat_id, term, status_line=None):
    session = state.jellyfin_sessions.get(chat_id)
    if not session:
        return
    session["last_accessed"] = time.time()
    session["mode"] = "results"
    session["gen"] += 1
    gen = session["gen"]

    items = await jf.search(term, limit=SEARCH_RESULT_LIMIT)
    if session.get("gen") != gen:
        return
    session["results"] = items

    rows = []
    for item in items:
        icon = "📺" if item.get("Type") == "Series" else "🎬"
        year = f" ({item['ProductionYear']})" if item.get("ProductionYear") else ""
        rows.append([Button.inline(f"{icon} {item.get('Name') or '?'}{year}", data=f"jf:item:{item.get('Id')}")])
    rows.append([Button.inline("🔍 חיפוש נוסף", data="jf:search"), Button.inline("🔙 חזרה", data="jf:menu")])

    text = f"🔍 *תוצאות עבור:* `{term}`"
    if not items:
        text += "\n\n_לא נמצא כלום. אם התוכן ירד ממש עכשיו, נסה קודם 🔄 רענון ספריות._"
    if status_line:
        text = f"{status_line}\n\n{text}"
    await _jf_send_or_edit(bot_client, chat_id, session, text, rows)


async def _jf_render_item(bot_client, chat_id, item_id, status_line=None):
    session = state.jellyfin_sessions.get(chat_id)
    if not session:
        return
    session["last_accessed"] = time.time()

    item = next((i for i in session.get("results", []) if i.get("Id") == item_id), None)
    if not item:
        await _jf_render_menu(bot_client, chat_id, status_line="⚠️ הפריט כבר לא בתוצאות, נסה לחפש שוב.")
        return

    session["mode"] = "item"
    session["gen"] += 1

    icon = "📺" if item.get("Type") == "Series" else "🎬"
    year = f" ({item['ProductionYear']})" if item.get("ProductionYear") else ""
    url = jf.item_url(item_id)

    text = f"{icon} *{item.get('Name') or '?'}*{year}"
    if item.get("Path"):
        text += f"\n📁 `{item['Path']}`"
    text += f"\n🔗 {url}"
    if status_line:
        text = f"{status_line}\n\n{text}"

    rows = [
        [Button.url("🎬 צפה ב-Jellyfin", url)],
        [Button.inline("🛠 רענן מטא-דאטה", data=f"jf:meta:{item_id}")],
        [Button.inline("🔁 זהה מחדש (מחליף הכל)", data=f"jf:reid:{item_id}")],
        [Button.inline("🔙 חזרה", data="jf:menu")],
    ]
    await _jf_send_or_edit(bot_client, chat_id, session, text, rows)


async def _jf_open(bot_client, chat_id, msg_id=None):
    """
    Opens the Jellyfin screen on `msg_id` - the message that was tapped, so the transition
    from the main menu happens in place. A typed /jellyfin passes None and gets a fresh
    message at the bottom of the chat. Mirrors _fb_open / _tor_open.
    """
    session = state.jellyfin_sessions.get(chat_id)
    if session is None:
        session = state.jellyfin_sessions[chat_id] = _jf_new_session()
    session["mode"] = "menu"
    session["msg_id"] = msg_id
    if msg_id:
        keyboards.claim_message(chat_id, msg_id)
    await _jf_render_menu(bot_client, chat_id)


async def _jf_handle_search_text(bot_client, chat_id, prompt_msg_id, text):
    """Handles the typed reply to the 🔍 prompt (routed here by the awaiting-text slot)."""
    session = state.jellyfin_sessions.get(chat_id)
    if session is None:
        # The screen was closed (or the bot restarted) while the prompt was open - render the
        # results onto the prompt message that is still sitting in the chat.
        session = state.jellyfin_sessions[chat_id] = _jf_new_session(msg_id=prompt_msg_id)
    await _jf_render_results(bot_client, chat_id, text.strip())


async def _jf_handle_callback(bot_client, event, chat_id, cq_data):
    parts = cq_data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else None

    if action == "open":
        await event.answer()
        await _jf_open(bot_client, chat_id, msg_id=event.message_id)
        return

    session = state.jellyfin_sessions.get(chat_id)
    if not session:
        await event.answer("⚠️ הסשן פג, פותח מחדש.", alert=True)
        await _jf_open(bot_client, chat_id, msg_id=event.message_id)
        return

    if event.message_id != session.get("msg_id"):
        # This tap landed on an older Jellyfin message the session no longer tracks; acting on
        # it would apply to whatever the tracked message currently shows instead.
        await event.answer("⚠️ התצוגה לא מעודכנת, מרענן...", alert=True)
        await _jf_render_menu(bot_client, chat_id)
        return

    if action == "home":
        state.jellyfin_sessions.pop(chat_id, None)
        await event.answer()
        await keyboards.show_main_menu(bot_client, chat_id, event.message_id)
        return

    if action == "menu":
        await event.answer()
        await _jf_render_menu(bot_client, chat_id)
        return

    if not jf.is_configured():
        await event.answer("⚠️ Jellyfin לא מוגדר (חסר JELLYFIN_API_KEY).", alert=True)
        return

    if action == "scan":
        await event.answer("מפעיל סריקה...")
        # force=True: an explicit tap is someone waiting for this scan, not the automatic
        # post-download refresh that the cooldown exists to throttle.
        ok = await jf.refresh_library(force=True)
        status = "🔄 סריקת ספריות הופעלה. זה עשוי לקחת כמה דקות." if ok else "❌ הפעלת הסריקה נכשלה."
        await _jf_render_menu(bot_client, chat_id, status_line=status)
        return

    if action == "sessions":
        await event.answer()
        await _jf_render_sessions(bot_client, chat_id)
        return

    if action == "search":
        await event.answer()
        await _set_awaiting_text(bot_client, chat_id, "jf_search", session["msg_id"])
        await bot_client.edit_message(chat_id, session["msg_id"], "🔍 שלח/י שם של סרט או סדרה לחיפוש ב-Jellyfin.")
        return

    if action == "item":
        await event.answer()
        await _jf_render_item(bot_client, chat_id, arg)
        return

    if action in ("meta", "reid"):
        replace_all = action == "reid"
        await event.answer("מזהה מחדש..." if replace_all else "מרענן מטא-דאטה...")
        ok = await jf.refresh_item_metadata(arg, replace_all=replace_all)
        status = (
            "🛠 בקשת הרענון נשלחה. Jellyfin יעדכן את הפריט ברקע."
            if ok else "❌ בקשת הרענון נכשלה."
        )
        await _jf_render_item(bot_client, chat_id, arg, status_line=status)
        return

    await event.answer()
