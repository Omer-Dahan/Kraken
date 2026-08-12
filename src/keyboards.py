"""Shared inline-keyboard builders and main-menu UI components."""

import logging

from telethon import Button

import posixpath

import bot_state as state
from bot_config import MEDIA_ROOT

DESTINATIONS = {
    "movies": ("🎬 סרטים", posixpath.join(MEDIA_ROOT, "movies")),
    "tv": ("📺 סדרות", posixpath.join(MEDIA_ROOT, "tv")),
}


def main_menu_text(mode="movies"):
    label, path = DESTINATIONS.get(mode, DESTINATIONS["movies"])
    return (
        "👋 *בוט ההורדות של Jellyfin*\n"
        f"🎯 יעד נוכחי: {label} — `{path}`\n\n"
        "שלחו קישור או קובץ להורדה, או בחרו פעולה:"
    )


def get_main_keyboard(current_mode="movies"):
    """Returns main menu inline keyboard with dynamic active downloads counter."""
    busy = state.active_download_count
    if busy > 1:
        downloads = f"📥 הורדות · {busy} פעילות"
    elif busy:
        downloads = "📥 הורדות · 1 פעילה"
    else:
        downloads = "📥 הורדות"

    return [
        [
            Button.inline(("✅ " if current_mode == "movies" else "") + DESTINATIONS["movies"][0], data=b"set_movies"),
            Button.inline(("✅ " if current_mode == "tv" else "") + DESTINATIONS["tv"][0], data=b"set_tv"),
        ],
        [Button.inline(downloads, data=b"tor:open")],
        [
            Button.inline("🗂 קבצים", data=b"fb:open"),
            Button.inline("🍿 Jellyfin", data=b"jf:open"),
        ],
        [Button.inline("❓ עזרה", data=b"help")],
    ]


async def show_main_menu(bot_client, chat_id, msg_id):
    """Edits an existing screen message back into the main menu in place."""
    mode = state.user_modes.get(chat_id, "movies")
    try:
        await bot_client.edit_message(chat_id, msg_id, main_menu_text(mode), buttons=get_main_keyboard(mode))
    except Exception as e:
        logging.warning(f"Failed to edit message {msg_id} back into the main menu: {e}")
        await send_menu_anchor(bot_client, chat_id)
        return
    state.menu_anchors[chat_id] = msg_id


async def send_menu_anchor(bot_client, chat_id, text=None):
    """Sends a new main menu anchor at the bottom of the chat and deletes the previous anchor."""
    previous = state.menu_anchors.pop(chat_id, None)
    mode = state.user_modes.get(chat_id, "movies")
    try:
        msg = await bot_client.send_message(chat_id, text or main_menu_text(mode), buttons=get_main_keyboard(mode))
    except Exception as e:
        logging.warning(f"Failed to send the main-menu anchor: {e}")
        return
    state.menu_anchors[chat_id] = msg.id

    if previous:
        try:
            await bot_client.delete_messages(chat_id, previous)
        except Exception as e:
            logging.warning(f"Failed to delete the previous main-menu anchor: {e}")


def claim_message(chat_id, msg_id):
    """
    Marks the anchor message as no longer being a menu, because a screen just took it over.

    Without this, the next anchor would delete a message that is now showing the user's open
    file manager / torrents / Jellyfin screen.
    """
    if state.menu_anchors.get(chat_id) == msg_id:
        state.menu_anchors.pop(chat_id, None)
