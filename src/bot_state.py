"""Shared mutable runtime state for the Telegram Hybrid Bot & Userbot Downloader.

Every other module accesses this exclusively via `import bot_state as state` and
`state.xxx` attribute access - NEVER `from bot_state import xxx`. A `from` import
binds a snapshot of the name at import time, so it would silently never observe a
later `state.xxx = ...` reassignment done by another module (e.g. `userbot_peer`
being set once the hybrid link is established in download_engine.py, or
`active_connections` being set at startup in telegram_bot.py's main()).
"""

import asyncio

from bot_config import DOWNLOAD_CONNECTIONS

# State management
user_modes = {}  # chat_id -> "movies" or "tv"
notified_completed = set()

# Folder-organizing confirmation flow (see confirmation_flow.py)
pending_actions = {}  # pending_id -> {"type": "torrent"|"video_group", "chat_id", ...}
watched_torrent_hashes = set()  # torrent hashes already offered for confirmation

# Files arriving close together are buffered here and decided on as one unit once the
# burst goes quiet - see confirmation_flow._add_to_batch / _finalize_batch.
incoming_batches = {}  # chat_id -> {"items": [...], "timer": asyncio.Task}

# A single source of truth for "the next plain-text message from this chat is a reply to
# a prompt", shared by the group-rename flow and the file manager's rename/mkdir prompts,
# so the two can never silently steal each other's typed reply.
awaiting_text_input = {}  # chat_id -> {"kind": "rename"|"fb_rename"|"fb_mkdir", "target": ...}

# Bounded-concurrency download queue: one shared queue, two independent worker pools (one
# per Telegram account) - see download_engine.enqueue_group_downloads / start_download_workers.
download_queue = asyncio.Queue()
queued_downloads = {}  # item_id -> {"file_name", "target_dir", "chat_id", "status", "via", "enqueued_at"}
download_groups = {}   # group_id -> {"chat_id", "label", "total", "done", "failed"}

# Mini file manager (see file_manager_ui.py).
fb_sessions = {}  # chat_id -> {"cwd", "mode", "entries", "page", "selected", "msg_id", "epoch"}

# Interactive torrent management screen (see torrents_screen.py).
torrent_sessions = {}  # chat_id -> {"filter", "page", "msg_id", "mode", "current_hash", "gen"}

# qBittorrent downloads + Telegram queue downloads currently in flight, refreshed by
# torrents_screen.watch_staged_torrents' existing 5-second poll. Read synchronously by the
# main menu's "📥 הורדות" badge, which is why it's cached at all: a menu render must never
# wait on (or fail with) the qBittorrent API. Deliberately allowed to be up to 5s stale.
active_download_count = 0

# Jellyfin management screen (see jellyfin_screen.py).
jellyfin_sessions = {}  # chat_id -> {"msg_id", "mode", "results", "gen"}

# The main-menu message currently sitting at the bottom of each chat. Tracked so a new
# anchor can delete the previous one instead of stacking menus - see keyboards.send_menu_anchor.
menu_anchors = {}  # chat_id -> message id

# Resolved peers for the hybrid Bot <-> Userbot link (populated at startup by
# download_engine.link_bot_and_userbot)
userbot_peer = None  # How the Bot addresses the Userbot (needed to forward media)
bot_peer = None      # How the Userbot addresses the Bot (needed to read forwarded media)

# Effective connection count, decided at startup once the Userbot's Premium status is known
active_connections = DOWNLOAD_CONNECTIONS
userbot_is_premium = False

# #18: Track user IDs of unauthorized users who have already received a rejection message,
# so the bot answers once and then stays silent to avoid flood vectors.
unauthorized_notified_users = set()

# Strong references to running background tasks to prevent premature GC or memory task leaks
background_tasks = set()


def create_background_task(coro):
    """Creates a background task with a strong reference and auto-discards it upon completion."""
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task



def record_queue_count():
    """Writes the current count of queued downloads to a local disk file."""
    try:
        from bot_config import BASE_DIR
        path = BASE_DIR / ".queue_count"
        count = len(queued_downloads)
        if count > 0:
            with open(path, "w", encoding="utf-8") as f:
                f.write(str(count))
        elif path.exists():
            os.remove(path)
    except Exception:
        pass


def pop_persisted_queue_count():
    """Reads and removes the persisted queue count from disk. Returns 0 if none existed."""
    import os
    try:
        from bot_config import BASE_DIR
        path = BASE_DIR / ".queue_count"
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                val = int(f.read().strip() or "0")
            os.remove(path)
            return val
    except Exception:
        pass
    return 0


def clean_stale_state():
    """Prunes old pending actions, idle UI sessions, stale text prompts, and bounds unauthorized users set."""
    import time
    now = time.time()

    # 1. Prune pending actions older than 24 hours (86400 seconds) - safely copy list to prevent iteration mutation errors
    stale_pids = [
        pid for pid, action in list(pending_actions.items())
        if now - action.get("created_at", now) > 86400
    ]
    for pid in stale_pids:
        pending_actions.pop(pid, None)

    # 2. Prune idle UI sessions older than 1 hour (3600 seconds)
    for sessions_dict in (fb_sessions, torrent_sessions, jellyfin_sessions):
        stale_chats = [
            chat_id for chat_id, sess in list(sessions_dict.items())
            if now - sess.get("last_accessed", now) > 3600
        ]
        for chat_id in stale_chats:
            sessions_dict.pop(chat_id, None)

    # 3. Prune stale text input prompts older than 1 hour
    stale_prompts = [
        chat_id for chat_id, prompt in list(awaiting_text_input.items())
        if now - prompt.get("created_at", now) > 3600
    ]
    for chat_id in stale_prompts:
        awaiting_text_input.pop(chat_id, None)

    # 4. Cap unauthorized_notified_users (safe to trim arbitrary entries)
    if len(unauthorized_notified_users) > 100:
        to_remove = len(unauthorized_notified_users) - 100
        for _ in range(to_remove):
            unauthorized_notified_users.pop()


