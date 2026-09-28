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
import learned_names
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

# The longest Telegram rate limit a first-time confirmation send will sit out before retrying.
FLOOD_RETRY_MAX_SECONDS = 60

# How many existing folders the ✏️ screen lists per page.
FOLDER_PICK_PAGE_SIZE = 8


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
        "detected_title": parsed.get("detected_title"),
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
        "detected_title": items[0]["parsed"].get("detected_title") if items else None,
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


def folder_exists(base, folder):
    """Whether confirming would file into a folder already in the library, not create one."""
    return os.path.isdir(os.path.join(MEDIA_ROOT, base, folder))


def _existing_folder_line(action):
    # Without this the confirmation read "create" even when the name matched a folder that
    # was already there, so nothing said the files would join it.
    if not folder_exists(action["base"], action["folder"]):
        return ""
    return "\n📂 התיקייה הזו כבר קיימת בספרייה - התוכן יתווסף אליה, לא תיווצר חדשה."


def _confirmation_text(action):
    base_label = "📺 סדרה" if action["base"] == "tv" else "🎬 סרט"
    season_line = _season_line(action)

    if action["type"] == "torrent":
        episode = action["parsed"].get("episode")
        if season_line and episode:
            season_line += f" · פרק {episode}"
        text = (
            f"{base_label} זוהתה: *{action['folder']}*{season_line}\n"
            f"📁 יעד מוצע: `{_target_display(action)}`{_existing_folder_line(action)}"
        )
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
        f"📁 יעד מוצע: `{_target_display(action)}`{_existing_folder_line(action)}\n{names_block}"
    )
    if _has_worthwhile_candidate(action):
        text += f"\n📂 נמצאה תיקייה קיימת דומה ({action['candidate_score']}%): `{action['candidate']}`"
    return text


def _confirmation_buttons(pending_id, action):
    count = len(action["items"]) if action["type"] == "video_group" else 1
    folder = action["folder"]
    if folder_exists(action["base"], folder):
        confirm_label = f'✅ {count} קבצים לתיקייה הקיימת "{folder}"' if count > 1 else f'✅ לתיקייה הקיימת "{folder}"'
    else:
        confirm_label = f'✅ אשר {count} קבצים: "{folder}"' if count > 1 else f'✅ צור: "{folder}"'
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

    # The phrase itself stays in the action, not in the button: callback data is capped at
    # 64 bytes, and Hebrew spends two of them per letter.
    for index, phrase in enumerate(action.get("ignore_suggestions", [])):
        if phrase:
            rows.append([Button.inline(f'🚫 להתעלם תמיד מ-"{phrase}"', data=f"ignore:{pending_id}:{index}")])

    rows.append([
        Button.inline("✏️ שנה שם", data=f"rename:{pending_id}"),
        Button.inline("❌ ביטול", data=f"cancel:{pending_id}"),
    ])
    return rows


async def send_confirmation(bot_client, chat_id, pending_id, status_line=None):
    """Sends (or, on a rename/merge, re-edits) the folder-confirmation message for a pending action.

    `status_line` is a one-off line shown above the confirmation for this render only - the
    reason a confirm didn't go through, with the buttons left in place to try again.
    """
    action = state.pending_actions.get(pending_id)
    if not action:
        return

    text = _confirmation_text(action)
    if status_line:
        text = f"{status_line}\n\n{text}"
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
        try:
            action["status_msg"] = await bot_client.send_message(chat_id, text, buttons=buttons)
        except (errors.FloodWaitError, errors.FloodPremiumWaitError) as e:
            # Nothing else will ever show this confirmation, and without it the files behind
            # it can't be confirmed - so a short rate limit is waited out once, not given up on.
            wait = getattr(e, "seconds", 0) or 0
            if wait > FLOOD_RETRY_MAX_SECONDS:
                raise
            logging.warning(f"FloodWait sending confirmation {pending_id}, retrying in {wait}s")
            await asyncio.sleep(wait + 1)
            action["status_msg"] = await bot_client.send_message(chat_id, text, buttons=buttons)
    finally:
        action.pop("_sending", None)
    # The new question is now what the chat is waiting on; a menu left above it only competes.
    await keyboards.remove_menu_anchor(bot_client, chat_id)


async def _set_awaiting_text(bot_client, chat_id, kind, target):
    """
    Sets the single "waiting for a text reply" slot for this chat, notifying the user if
    it's stomping an older, still-open prompt (group rename vs. file-manager rename/mkdir
    can otherwise silently steal each other's typed reply if both are ever left open).
    """
    previous = state.awaiting_text_input.get(chat_id)
    # A second tap on the same ✏️ is the same prompt, not a new one replacing it.
    if previous and (previous["kind"], previous["target"]) != (kind, target):
        await _drop_awaiting_text(bot_client, chat_id)
        try:
            await bot_client.send_message(chat_id, "⏹️ הבקשה הקודמת בוטלה.")
        except Exception as e:
            logging.warning(f"Failed to send awaiting-text override notice: {e}")
    state.awaiting_text_input[chat_id] = {"kind": kind, "target": target, "created_at": time.time()}
    await keyboards.remove_menu_anchor(bot_client, chat_id)


async def _drop_awaiting_text(bot_client, chat_id):
    """
    Abandons this chat's open text prompt without an answer.

    A prompt replaces its message's buttons with its own question, so abandoning a rename
    used to leave the confirmation with no buttons at all - its files could then never be
    confirmed, and a staged torrent sat there for good. The confirmation is redrawn instead.
    File-manager and Jellyfin prompts keep a ↩️ button of their own, and their screens can be
    reopened from the menu anyway.
    """
    prompt = state.awaiting_text_input.pop(chat_id, None)
    if prompt and prompt["kind"] == "rename":
        await send_confirmation(bot_client, chat_id, prompt["target"])


def _clear_awaiting_text(chat_id, target):
    """Clears the awaiting-text slot only if it still points at `target` (avoids clobbering a newer one)."""
    current = state.awaiting_text_input.get(chat_id)
    if current and current.get("target") == target:
        state.awaiting_text_input.pop(chat_id, None)


def _clear_awaiting_kind(chat_id, *kinds):
    """Clears the awaiting-text slot if it is one of `kinds` - for a screen's own ↩️ button."""
    current = state.awaiting_text_input.get(chat_id)
    if current and current.get("kind") in kinds:
        state.awaiting_text_input.pop(chat_id, None)


def existing_folders(base):
    """The show/movie folders already in one library, in the order a person would look for them."""
    base_dir = os.path.join(MEDIA_ROOT, base)
    try:
        names = os.listdir(base_dir)
    except OSError:
        return []
    return sorted(
        (n for n in names if not n.startswith(".") and os.path.isdir(os.path.join(base_dir, n))),
        key=str.casefold,
    )


def open_folder_picker(action, base=None):
    """
    Snapshots the folders the ✏️ screen offers. The buttons carry an index into this list,
    not the name - callback data is capped at 64 bytes - so it must not shift under them.
    """
    action["pick_base"] = base or action["base"]
    action["folder_choices"] = existing_folders(action["pick_base"])


def rename_prompt(pending_id, action, page=0):
    """
    The ✏️ screen: type a new name, or pick a folder already in the library that the
    detection didn't match - a new name can only ever create a folder, not reach one.
    Returns (text, buttons).
    """
    base = action.get("pick_base", action["base"])
    choices = action.get("folder_choices", [])
    total_pages = max(1, -(-len(choices) // FOLDER_PICK_PAGE_SIZE))
    page = min(max(page, 0), total_pages - 1)
    start = page * FOLDER_PICK_PAGE_SIZE
    library = os.path.join(MEDIA_ROOT, base)

    text = f"✏️ שלח/י הודעת טקסט עם השם החדש עבור *{action['folder']}*"
    if choices:
        text += f",\nאו בחר/י תיקייה קיימת מ-`{library}`:"
        if total_pages > 1:
            text += f" (עמוד {page + 1}/{total_pages})"
    else:
        text += f".\n(אין עדיין תיקיות ב-`{library}`.)"

    rows = [
        [Button.inline(f"📂 {name}", data=f"pick:{pending_id}:{index}")]
        for index, name in enumerate(choices[start:start + FOLDER_PICK_PAGE_SIZE], start)
    ]
    page_row = []
    if page > 0:
        page_row.append(Button.inline("◀️ הקודם", data=f"pickpage:{pending_id}:{page - 1}"))
    if page < total_pages - 1:
        page_row.append(Button.inline("➡️ הבא", data=f"pickpage:{pending_id}:{page + 1}"))
    if page_row:
        rows.append(page_row)

    other = "movies" if base == "tv" else "tv"
    other_label = "🔄 הצג תיקיות סרטים" if other == "movies" else "🔄 הצג תיקיות סדרות"
    rows.append([Button.inline(other_label, data=f"pickbase:{pending_id}")])
    rows.append([Button.inline("↩️ חזרה", data=f"rename_back:{pending_id}")])
    return text, rows


def pick_existing_folder(action, index):
    """
    Points the action at the index-th folder the ✏️ screen offered, switching movies/tv if
    it came from the other library. Returns the folder, or None for a stale button.
    """
    choices = action.get("folder_choices", [])
    base = action.get("pick_base", action["base"])
    if not 0 <= index < len(choices) or not folder_exists(base, choices[index]):
        return None
    folder = choices[index]
    if base != action["base"]:
        flip_base(action)
    action["folder"] = folder
    action["candidate"], action["candidate_score"] = find_similar_existing(os.path.join(MEDIA_ROOT, base), folder)
    action.pop("folder_choices", None)
    action.pop("pick_base", None)
    return folder


def _matching_existing_folder(base, name):
    """The folder already in the library that `name` names, ignoring case - or None."""
    wanted = name.strip().casefold()
    return next((f for f in existing_folders(base) if f.casefold() == wanted), None)


def _current_title(action):
    if action["type"] == "video_group":
        return action["items"][0]["parsed"].get("title") if action["items"] else None
    return action["parsed"].get("title")


def accept_ignore_suggestion(action, index):
    """
    Adds the index-th offered phrase to the ignore list. Returns the phrase, or None when that
    button is stale. The slot is blanked rather than removed so the other buttons' indexes,
    already sent to Telegram, keep pointing at the same phrases.
    """
    suggestions = action.get("ignore_suggestions", [])
    if not 0 <= index < len(suggestions) or not suggestions[index]:
        return None
    phrase, suggestions[index] = suggestions[index], None
    try:
        learned_names.memory.add_ignore_word(phrase)
    except OSError as e:
        logging.warning(f"Could not save ignore word {phrase!r}: {e}")
        return None
    return phrase


def learn_from_confirmation(action, folder):
    """
    Remembers which folder the name the bot detected was finally filed under, so the same
    name is recognized as that show/movie next time - and, when the folder already exists,
    auto-assigned to it without asking.
    """
    detected = action.get("detected_title")
    # The folder is the user's own choice - reading it through older aliases could chain
    # one correction onto another.
    chosen = parse_media_name(folder, apply_aliases=False)
    if not detected or not chosen["title"]:
        return
    try:
        if learned_names.memory.remember_alias(detected, chosen["title"], chosen["year"]):
            logging.info(f"Learned title alias: {detected!r} -> {chosen['title']!r} ({chosen['year']})")
    except OSError as e:
        logging.warning(f"Could not save title alias for {detected!r}: {e}")


async def _handle_rename_reply(bot_client, chat_id, pending_id, text):
    """Applies a typed replacement title to a pending torrent or video_group action."""
    action = state.pending_actions.get(pending_id)
    if not action:
        return

    shown_title = _current_title(action)
    # What the user typed is the title they want, so no learned alias gets to replace it.
    reparsed = parse_media_name(text, apply_aliases=False)
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

    # Typing a folder that's already there - in any case - means that folder. Taken as typed,
    # "the office" would sit next to "The Office" as a second, separate show.
    existing = _matching_existing_folder(action["base"], action["folder"])
    if existing:
        action["folder"] = existing

    # Whatever the user deleted from the title we showed is likely a channel tag - offer to
    # drop it from every future name too. Measured against what was on screen, not the raw
    # detection, so a second rename doesn't re-offer words the first one already removed.
    action["ignore_suggestions"] = learned_names.dropped_phrases(
        shown_title, _current_title(action), known=learned_names.memory.ignore_words()
    )

    # A season typed into the rename is an explicit instruction, so it overrides the picker
    # rather than being merged with what the filenames said. A rename with no season in it
    # only fixes the title - it must not throw away a ➕/➖ choice the user already made.
    if typed_season is not None:
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

    # One show failing to be offered (Telegram refusing a send, a folder that can't be listed)
    # used to abort the loop, silently dropping every show after it in the same burst.
    for (base, folder), group_items in grouped.items():
        try:
            await _offer_group(bot_client, user_client, chat_id, base, folder, group_items)
        except Exception as e:
            logging.error(f"Could not offer {len(group_items)} file(s) for {base}/{folder}: {e}", exc_info=True)

    # The burst has been fully accounted for. A menu only follows if none of it is waiting
    # on the user - otherwise it comes once the last confirmation is answered.
    await keyboards.send_menu_when_idle(bot_client, chat_id)


async def _offer_group(bot_client, user_client, chat_id, base, folder, group_items):
    """Auto-files one detected show/movie into its existing folder, or asks where it goes."""
    rep_parsed = group_items[0]["parsed"]
    exact_match = find_exact_existing(os.path.join(MEDIA_ROOT, base), rep_parsed.get("title") or folder, rep_parsed.get("year"))

    if exact_match:
        target_dir_display = os.path.join(MEDIA_ROOT, base, exact_match)
        targets = [
            (i["message"], build_target_dir(MEDIA_ROOT, base, exact_match, i["parsed"].get("season")), i["file_name"])
            for i in group_items
        ]
        enqueue_group_downloads(bot_client, user_client, chat_id, folder, targets)
        try:
            await bot_client.send_message(
                chat_id,
                f'📥 *{len(targets)} קבצים* מ-*{folder}* שויכו לתיקייה קיימת '
                f'(`{exact_match}`) ונוספו לתור ההורדה אוטומטית ל-`{target_dir_display}`.'
            )
        except Exception as e:
            logging.warning(f"Failed to send auto-assign notice: {e}")
        return

    existing_pid = _find_open_group(chat_id, base, folder)
    if existing_pid:
        merged = state.pending_actions[existing_pid]
        merged["items"].extend(group_items)
        _sync_season(merged)  # the new files may have widened the group to several seasons
        await send_confirmation(bot_client, chat_id, existing_pid)
    else:
        pending_id = _new_group_action(chat_id, base, folder, group_items)
        await send_confirmation(bot_client, chat_id, pending_id)
