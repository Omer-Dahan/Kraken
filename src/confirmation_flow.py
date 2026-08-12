"""Batch-aware folder-confirmation flow: files/torrents get grouped and offered ONE
folder-confirmation per detected show/movie (not one per file), with near-certain
matches to an already-existing folder skipping confirmation entirely.
"""

import os
import time
import uuid
import asyncio
import logging

from telethon import Button, errors

import bot_state as state
import keyboards
from bot_config import MEDIA_ROOT, BATCH_DEBOUNCE_SECONDS, SIMILARITY_MENTION_THRESHOLD
from media_organizer import (
    DEFAULT_SEASON,
    parse_media_name,
    propose_folder,
    sanitize_folder_name,
    find_similar_existing,
    find_exact_existing,
    build_target_dir,
)
from download_engine import enqueue_group_downloads

# Jellyfin uses Season 00 for specials, so 0 is a legitimate choice, not an off-by-one.
MIN_SEASON = 0
MAX_SEASON = 99


def _new_pending_action(action_type, chat_id, base, folder, parsed, **extra):
    """Registers a pending torrent folder-confirmation action and returns its short id."""
    pending_id = uuid.uuid4().hex[:8]
    candidate, score = find_similar_existing(os.path.join(MEDIA_ROOT, base), folder)
    action = {
        "type": action_type,
        "chat_id": chat_id,
        "parsed": parsed,
        "base": base,
        "folder": folder,
        "season": parsed.get("season"),
        "candidate": candidate,
        "candidate_score": score,
        "status_msg": None,
        "created_at": time.time(),
    }
    action.update(extra)
    _sync_season(action)
    state.pending_actions[pending_id] = action
    return pending_id


def _new_group_action(chat_id, base, folder, items):
    """Registers a pending video-group folder-confirmation action (a batch of 1+ files)."""
    pending_id = uuid.uuid4().hex[:8]
    candidate, score = find_similar_existing(os.path.join(MEDIA_ROOT, base), folder)
    action = {
        "type": "video_group",
        "chat_id": chat_id,
        "base": base,
        "folder": folder,
        "season": None,
        "candidate": candidate,
        "candidate_score": score,
        "items": items,
        "status_msg": None,
        "created_at": time.time(),
    }
    _sync_season(action)
    state.pending_actions[pending_id] = action
    return pending_id



def _detected_seasons(action):
    """The distinct season numbers the filenames themselves gave up, lowest first."""
    if action["type"] == "video_group":
        return sorted({
            item["parsed"].get("season") for item in action["items"]
            if item["parsed"].get("season") is not None
        })
    season = action["parsed"].get("season")
    return [season] if season is not None else []


def _sync_season(action):
    """
    Recomputes action["season"] - the single season the ➕/➖ picker edits - after anything
    that can change what the action means (creation, a base flip, a rename, a merge).

    A group whose files carry MORE than one season keeps season=None on purpose: those
    per-file numbers are already right, and one picker value would flatten S01+S02 into a
    single folder. Everything else gets one editable number, defaulting to Season 01 when
    nothing in the name said otherwise.
    """
    if action["base"] != "tv":
        action["season"] = None
        return
    seasons = _detected_seasons(action)
    if len(seasons) > 1:
        action["season"] = None
    elif action.get("season") is None:
        action["season"] = seasons[0] if seasons else DEFAULT_SEASON


def item_season(action, item_parsed=None):
    """
    Which Season NN a single file ends up in: the picker's value when the whole action shares
    one season, otherwise the season that file's own name carried.
    """
    if action["base"] != "tv":
        return None
    if action.get("season") is not None:
        return action["season"]
    return (item_parsed or {}).get("season")


def _has_season_picker(action):
    return action["base"] == "tv" and action.get("season") is not None


def adjust_season(action, delta):
    """Moves the season picker by delta, clamped. Returns the new value (None: no picker)."""
    if not _has_season_picker(action):
        return None
    action["season"] = max(MIN_SEASON, min(MAX_SEASON, action["season"] + delta))
    return action["season"]


def flip_base(action):
    """
    Switches a pending action between /media/movies and /media/tv.

    The similar-folder candidate is re-scored against the OTHER library, since "a folder that
    already looks like this" is only meaningful within the base we're actually writing to.
    """
    action["base"] = "tv" if action["base"] == "movies" else "movies"
    _sync_season(action)
    action["candidate"], action["candidate_score"] = find_similar_existing(
        os.path.join(MEDIA_ROOT, action["base"]), action["folder"]
    )
    return action["base"]


def _find_open_group(chat_id, base, folder):
    """
    Finds an already-open (unconfirmed) video_group action for this chat targeting the
    same show/movie, so a burst that got split across two debounce windows merges into
    one confirmation instead of prompting twice for the same thing. Both folder strings
    were built the same way (propose_folder from a guessit title+year), so a case-insensitive
    exact match is enough - no need for fuzzy scoring (see find_exact_existing for why fuzzy
    scoring is unsafe for decisions that aren't reviewed by a human before acting).
    """
    target = folder.strip().lower()
    for pid, action in state.pending_actions.items():
        if (action["type"] == "video_group" and action["chat_id"] == chat_id
                and action["base"] == base
                and action["folder"].strip().lower() == target):
            return pid
    return None


def _has_worthwhile_candidate(action):
    return (
        action["candidate"]
        and action["candidate"] != action["folder"]
        and action["candidate_score"] >= SIMILARITY_MENTION_THRESHOLD
    )


def _target_display(action):
    """
    The destination path shown in the confirmation message.

    A mixed-season group has no single destination, so it shows "Season XX" rather than
    picking one of them and quietly implying every file lands there.
    """
    if action["base"] == "tv" and action.get("season") is None:
        return os.path.join(MEDIA_ROOT, action["base"], action["folder"], "Season XX")
    return build_target_dir(MEDIA_ROOT, action["base"], action["folder"], action.get("season"))


def _season_line(action):
    if action["base"] != "tv":
        return ""
    if action.get("season") is not None:
        return f"\n🔢 עונה: {action['season']}"
    seasons = _detected_seasons(action)
    return f"\n🔢 עונות {seasons[0]}-{seasons[-1]} (כל קובץ לפי שמו)"


def _confirmation_text(action):
    base_label = "📺 סדרה" if action["base"] == "tv" else "🎬 סרט"
    season_line = _season_line(action)

    if action["type"] == "torrent":
        episode = action["parsed"].get("episode")
        if season_line and episode:
            season_line += f" · פרק {episode}"
        text = f"{base_label} זוהתה: *{action['folder']}*{season_line}\n📁 יעד מוצע: `{_target_display(action)}`"
        if _has_worthwhile_candidate(action):
            text += f"\n📂 נמצאה תיקייה קיימת דומה ({action['candidate_score']}%): `{action['candidate']}`"
        return text

    # video_group - possibly many files, so this is deliberately a summary, not a list.
    items = action["items"]
    count = len(items)
    shown = items[:5]
    names_block = "\n".join(f"  • `{i['file_name']}`" for i in shown)
    if count > len(shown):
        names_block += f"\n  • ועוד {count - len(shown)} נוספים"

    text = (
        f"{base_label} זוהתה: *{action['folder']}* ({count} קבצים){season_line}\n"
        f"📁 יעד מוצע: `{_target_display(action)}`\n{names_block}"
    )
    if _has_worthwhile_candidate(action):
        text += f"\n📂 נמצאה תיקייה קיימת דומה ({action['candidate_score']}%): `{action['candidate']}`"
    return text


def _confirmation_buttons(pending_id, action):
    count = len(action["items"]) if action["type"] == "video_group" else 1
    confirm_label = f'✅ אשר {count} קבצים: "{action["folder"]}"' if count > 1 else f'✅ צור: "{action["folder"]}"'
    rows = [[Button.inline(confirm_label, data=f"confirm:{pending_id}")]]
    if _has_worthwhile_candidate(action):
        rows.append([Button.inline(f'📂 השתמש בקיימת: "{action["candidate"]}"', data=f"use_existing:{pending_id}")])

    # One tap to overrule the detection, in both directions - guessit reads plain numbers in
    # a name as episode numbers, and the folder it picks is otherwise only fixable by renaming.
    flip_label = "🔄 שנה ל-🎬 סרט" if action["base"] == "tv" else "🔄 שנה ל-📺 סדרה"
    rows.append([Button.inline(flip_label, data=f"flip:{pending_id}")])

    if _has_season_picker(action):
        rows.append([
            Button.inline("➕ עונה", data=f"season:{pending_id}:1"),
            Button.inline(f"🔢 עונה: {action['season']}", data=f"season:{pending_id}:0"),
            Button.inline("➖ עונה", data=f"season:{pending_id}:-1"),
        ])

    rows.append([
        Button.inline("✏️ שנה שם", data=f"rename:{pending_id}"),
        Button.inline("❌ ביטול", data=f"cancel:{pending_id}"),
    ])
    return rows


async def send_confirmation(bot_client, chat_id, pending_id):
    """Sends (or, on a rename/merge, re-edits) the folder-confirmation message for a pending action."""
    action = state.pending_actions.get(pending_id)
    if not action:
        return

    text = _confirmation_text(action)
    buttons = _confirmation_buttons(pending_id, action)

    if action.get("status_msg"):
        try:
            await bot_client.edit_message(chat_id, action["status_msg"].id, text, buttons=buttons)
            return
        except errors.MessageNotModifiedError:
            # The ➕/➖ season picker at its clamp, or a flip that changed nothing visible.
            # Falling through to "send a new one" here would post a duplicate confirmation.
            return
        except (errors.FloodWaitError, errors.FloodPremiumWaitError) as e:
            logging.warning(f"Telegram FloodWait editing confirmation message: {e}")
            return
        except Exception as e:
            logging.warning(f"Failed to edit confirmation message, sending a new one: {e}")
    elif action.get("_sending"):
        # A debounce-boundary merge (_find_open_group) can call this again for the same
        # pending_id while the FIRST send_message for it is still in flight (status_msg
        # isn't set until that call returns) - without this guard both calls would send
        # their own message, leaving a duplicate confirmation on screen. The merged items
        # are already in action["items"] regardless, so confirming still downloads all of
        # them; only the display catches up once this in-flight send finishes.
        return
    action["_sending"] = True

    try:
        action["status_msg"] = await bot_client.send_message(chat_id, text, buttons=buttons)
    finally:
        action.pop("_sending", None)


async def _set_awaiting_text(bot_client, chat_id, kind, target):
    """
    Sets the single "waiting for a text reply" slot for this chat, notifying the user if
    it's stomping an older, still-open prompt (group rename vs. file-manager rename/mkdir
    can otherwise silently steal each other's typed reply if both are ever left open).
    """
    if chat_id in state.awaiting_text_input:
        try:
            await bot_client.send_message(chat_id, "⏹️ הבקשה הקודמת בוטלה.")
        except Exception as e:
            logging.warning(f"Failed to send awaiting-text override notice: {e}")
    state.awaiting_text_input[chat_id] = {"kind": kind, "target": target, "created_at": time.time()}


def _clear_awaiting_text(chat_id, target):
    """Clears the awaiting-text slot only if it still points at `target` (avoids clobbering a newer one)."""
    current = state.awaiting_text_input.get(chat_id)
    if current and current.get("target") == target:
        state.awaiting_text_input.pop(chat_id, None)


async def _handle_rename_reply(bot_client, chat_id, pending_id, text):
    """Applies a typed replacement title to a pending torrent or video_group action."""
    action = state.pending_actions.get(pending_id)
    if not action:
        return

    reparsed = parse_media_name(text)
    # The action's current base is what the rename is measured against, so typing a new title
    # doesn't quietly undo a 🔄 flip the user just made. An explicit "S02" in the typed text
    # still wins - propose_folder only defers to the preferred base without a season marker.
    typed_season = reparsed.get("season")

    if action["type"] == "torrent":
        if reparsed["kind"] == "unknown":
            # A bare title (no S/E/year typed) shouldn't downgrade an
            # already-detected episode/movie back to "unknown".
            reparsed = dict(action["parsed"], title=text)
        action["parsed"] = reparsed
        base, folder = propose_folder(reparsed, preferred_base=action["base"])
        action["base"] = base or action["base"]
        action["folder"] = folder or sanitize_folder_name(text)
    else:
        # video_group: apply the new title/year to every item, but keep each item's own
        # season/episode - a group rename fixes the show's name, not per-episode metadata.
        baseline = action["items"][0]["parsed"] if action["items"] else {}
        if reparsed["kind"] == "unknown":
            reparsed = dict(baseline, title=text)
        base, folder = propose_folder(reparsed, preferred_base=action["base"])
        action["base"] = base or action["base"]
        action["folder"] = folder or sanitize_folder_name(text)
        new_title = reparsed.get("title") or text
        new_year = reparsed.get("year")
        for item in action["items"]:
            item["parsed"] = dict(
                item["parsed"],
                title=new_title,
                year=new_year if new_year is not None else item["parsed"].get("year"),
            )

    # A season typed into the rename is an explicit instruction, so it overrides the picker
    # rather than being merged with what the filenames said.
    action["season"] = typed_season
    _sync_season(action)
    action["candidate"], action["candidate_score"] = find_similar_existing(
        os.path.join(MEDIA_ROOT, action["base"]), action["folder"]
    )
    await send_confirmation(bot_client, chat_id, pending_id)


async def _add_to_batch(bot_client, user_client, chat_id, message, file_name, parsed, target_path):
    """
    Buffers an incoming video/document message instead of deciding on it immediately, so a
    burst of files landing close together gets organized as one unit - see _finalize_batch.
    """
    # The whole check/create/append/timer sequence below must run with no `await` in
    # between: asyncio only switches tasks at await points, so keeping it synchronous is
    # what makes "cancel the old timer, append, start a new one" race-free when a second
    # file arrives mid-call. An earlier version awaited the ack message before setting the
    # timer, which let a second file see the batch already created but its timer still
    # None - each call then created its own timer and the later one silently overwrote the
    # earlier one's reference (an unreferenced asyncio.Task is even eligible for GC).
    batch = state.incoming_batches.get(chat_id)
    is_new_batch = batch is None
    if is_new_batch:
        batch = {"items": [], "timer": None}
        state.incoming_batches[chat_id] = batch
    elif batch["timer"]:
        batch["timer"].cancel()

    batch["items"].append({"message": message, "file_name": file_name, "parsed": parsed, "target_path": target_path})
    batch["timer"] = state.create_background_task(_finalize_batch(bot_client, user_client, chat_id))

    if is_new_batch:
        try:
            await bot_client.send_message(chat_id, "📥 מתקבלים קבצים, רגע...")
        except Exception as e:
            logging.warning(f"Failed to send batch-received ack: {e}")


async def _finalize_batch(bot_client, user_client, chat_id):
    """
    Runs BATCH_DEBOUNCE_SECONDS after the last file in a burst. Popping incoming_batches
    must stay the very first statement with no preceding await: asyncio only switches
    tasks at await points, so this makes the cancel-and-restart dance in _add_to_batch
    race-free - either a new file's .cancel() lands before this resumes (safe, this
    function returns immediately below), or this has already popped and moved on before
    the handler even looks (in which case the handler correctly starts a fresh batch,
    and _find_open_group below merges it back into an existing confirmation if the split
    landed on the same show/movie).
    """
    try:
        await asyncio.sleep(BATCH_DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return

    batch = state.incoming_batches.pop(chat_id, None)
    if not batch or not batch["items"]:
        return
    items = batch["items"]

    # The 🎯 destination the user has selected decides movies-vs-tv for anything whose name
    # doesn't literally spell out a season - see propose_folder.
    preferred_base = state.user_modes.get(chat_id, "movies")

    unknown_items = []
    grouped = {}  # (base, folder) -> [items]
    for item in items:
        base, folder = propose_folder(item["parsed"], preferred_base=preferred_base)
        if not base:
            unknown_items.append(item)
        else:
            grouped.setdefault((base, folder), []).append(item)

    if unknown_items:
        targets = [(i["message"], i["target_path"], i["file_name"]) for i in unknown_items]
        try:
            await bot_client.send_message(
                chat_id,
                f'⚠️ {len(unknown_items)} קבצים לא זוהו ויישמרו ביעד הנוכחי.\n'
                f'ניתן להעביר אותם אחר כך עם 🗂 מנהל קבצים.'
            )
        except Exception as e:
            logging.warning(f"Failed to send unknown-bucket notice: {e}")
        enqueue_group_downloads(bot_client, user_client, chat_id, "קבצים לא מזוהים", targets)

    for (base, folder), group_items in grouped.items():
        rep_parsed = group_items[0]["parsed"]
        exact_match = find_exact_existing(os.path.join(MEDIA_ROOT, base), rep_parsed.get("title") or folder, rep_parsed.get("year"))

        if exact_match:
            target_dir_display = os.path.join(MEDIA_ROOT, base, exact_match)
            targets = [
                (i["message"], build_target_dir(MEDIA_ROOT, base, exact_match, i["parsed"].get("season")), i["file_name"])
                for i in group_items
            ]
            try:
                await bot_client.send_message(
                    chat_id,
                    f'📥 *{len(targets)} קבצים* מ-*{folder}* שויכו לתיקייה קיימת '
                    f'(`{exact_match}`) ונוספו לתור ההורדה אוטומטית ל-`{target_dir_display}`.'
                )
            except Exception as e:
                logging.warning(f"Failed to send auto-assign notice: {e}")
            enqueue_group_downloads(bot_client, user_client, chat_id, folder, targets)
            continue

        existing_pid = _find_open_group(chat_id, base, folder)
        if existing_pid:
            merged = state.pending_actions[existing_pid]
            merged["items"].extend(group_items)
            _sync_season(merged)  # the new files may have widened the group to several seasons
            await send_confirmation(bot_client, chat_id, existing_pid)
        else:
            pending_id = _new_group_action(chat_id, base, folder, group_items)
            await send_confirmation(bot_client, chat_id, pending_id)

    # The burst has been fully accounted for, so leave a usable menu at the bottom of the
    # chat rather than making the user scroll back up past the confirmations to find one.
    await keyboards.send_menu_anchor(bot_client, chat_id)
