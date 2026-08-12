"""Runtime configuration loaded from a local .env file or process environment."""

import os
from pathlib import Path

from dotenv import load_dotenv

from fast_download import MAX_CONNECTIONS


# Project root path (parent of src/)
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# Telethon session directory
SESSION_DIR = BASE_DIR / "session"


def _required(name):
    value = os.getenv(name)
    if value:
        return value
    raise RuntimeError(f"Missing {name}. Copy .env.example to .env and set it before starting Kraken.")


def _int_env(name, default, minimum=None, maximum=None):
    """Reads an integer from the environment with bounds clamping and a clear error.

    Returns `default` when the variable is absent or empty. Raises RuntimeError with the
    variable name when the value isn't a valid integer - the default Python ValueError
    ("invalid literal for int()") gives no clue WHICH variable needs fixing.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"{name}={raw!r} is not a valid integer.")
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _parse_allowed_user_ids():
    """Parses ALLOWED_USER_IDS, accepting both positive user IDs and negative group/channel IDs."""
    raw = os.getenv("ALLOWED_USER_IDS", "")
    ids = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        # Accept negative IDs (groups/channels) and explicit + signs - lstrip("-+") + isdigit handles all.
        if part.lstrip("-+").isdigit():
            ids.append(int(part))
    if not ids:
        raise RuntimeError(
            "ALLOWED_USER_IDS is empty or missing. At least one Telegram user ID is required, "
            "otherwise the bot rejects everyone. Copy .env.example to .env and set it."
        )
    return ids


API_ID = int(_required("API_ID"))
API_HASH = _required("API_HASH")
BOT_TOKEN = _required("BOT_TOKEN")
SESSION_STRING = os.getenv("SESSION_STRING", "")
SESSION_PATH = os.getenv("SESSION_PATH", str(BASE_DIR / "userbot"))
DOWNLOAD_CONNECTIONS = _int_env("DOWNLOAD_CONNECTIONS", 10, minimum=1, maximum=MAX_CONNECTIONS)
NON_PREMIUM_CONNECTIONS = _int_env("NON_PREMIUM_CONNECTIONS", 4, minimum=1)
QBIT_URL = os.getenv("QBIT_URL", "http://localhost:8080")
JELLYFIN_URL = os.getenv("JELLYFIN_URL", "http://localhost:8096").rstrip("/")
JELLYFIN_PUBLIC_URL = os.getenv("JELLYFIN_PUBLIC_URL", JELLYFIN_URL).rstrip("/")
JELLYFIN_API_KEY = os.getenv("JELLYFIN_API_KEY", "")
JELLYFIN_LOOKUP_ATTEMPTS = _int_env("JELLYFIN_LOOKUP_ATTEMPTS", 6, minimum=1)
JELLYFIN_LOOKUP_DELAY = float(os.getenv("JELLYFIN_LOOKUP_DELAY", "10"))
JELLYFIN_REFRESH_COOLDOWN = float(os.getenv("JELLYFIN_REFRESH_COOLDOWN", "45"))
ALLOWED_USER_IDS = _parse_allowed_user_ids()
MEDIA_ROOT = os.getenv("MEDIA_ROOT", "/media")
STAGING_DIR = os.getenv("STAGING_DIR", "/media/.incoming")
BATCH_DEBOUNCE_SECONDS = float(os.getenv("BATCH_DEBOUNCE_SECONDS", "4"))
DOWNLOAD_QUEUE_CONCURRENCY = _int_env("DOWNLOAD_QUEUE_CONCURRENCY", 2, minimum=1)
BOT_QUEUE_CONCURRENCY = _int_env("BOT_QUEUE_CONCURRENCY", 1, minimum=1)
QUIET_GROUP_SIZE = _int_env("QUIET_GROUP_SIZE", 8, minimum=1)
FB_PAGE_SIZE = _int_env("FB_PAGE_SIZE", 10, minimum=1)
TORRENTS_PAGE_SIZE = _int_env("TORRENTS_PAGE_SIZE", 6, minimum=1)
TORRENT_HASH_PREFIX_LEN = _int_env("TORRENT_HASH_PREFIX_LEN", 8, minimum=4)
SIMILARITY_MENTION_THRESHOLD = _int_env("SIMILARITY_MENTION_THRESHOLD", 75, minimum=0, maximum=100)
