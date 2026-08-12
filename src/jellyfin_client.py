"""
Jellyfin Web API HTTP client wrappers for library scans, item lookup, and metadata refresh.
"""

import asyncio
import logging
import time

import requests

from bot_config import (
    JELLYFIN_URL, JELLYFIN_PUBLIC_URL, JELLYFIN_API_KEY,
    JELLYFIN_LOOKUP_ATTEMPTS, JELLYFIN_LOOKUP_DELAY, JELLYFIN_REFRESH_COOLDOWN,
)

# Item types worth linking to. An episode's own page is far less useful than its series page
# (which is where you actually pick what to watch), so episodes are looked up as their series.
SEARCH_ITEM_TYPES = "Movie,Series"

_last_refresh_at = 0.0
_cached_user_id = None

import threading

# Thread-local storage for thread-safe HTTP connection pooling across worker threads
_thread_local = threading.local()


def _get_http_session():
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        _thread_local.session = session
    return session


def is_configured():
    return bool(JELLYFIN_API_KEY)


def item_url(item_id):
    """The deep link that opens an item's page in the Jellyfin web client."""
    return f"{JELLYFIN_PUBLIC_URL}/web/index.html#!/details?id={item_id}"


def _request(method, path, params=None, timeout=15):
    """Blocking - always called through asyncio.to_thread by the async wrappers below."""
    return _get_http_session().request(
        method,
        f"{JELLYFIN_URL}{path}",
        params=params,
        headers={"X-Emby-Token": JELLYFIN_API_KEY, "Accept": "application/json"},
        timeout=timeout,
    )


async def _call(method, path, params=None, timeout=15):
    """Returns the parsed JSON body, True for an empty 2xx (Jellyfin answers 204 a lot), or None."""
    if not is_configured():
        return None
    try:
        res = await asyncio.to_thread(_request, method, path, params, timeout)
    except Exception as e:
        logging.error(f"Jellyfin {method} {path} failed: {e}")
        return None

    if not res.ok:
        logging.error(f"Jellyfin {method} {path} returned {res.status_code}: {res.text[:200]}")
        return None
    if not res.content:
        return True
    try:
        return res.json()
    except ValueError:
        return True


async def _user_id():
    """
    Resolves an admin user id once and caches it.

    /Items is user-scoped in some Jellyfin versions and server-scoped in others; passing a
    userId works on both, so one lookup at first use buys compatibility across versions.
    """
    global _cached_user_id
    if _cached_user_id is not None:
        return _cached_user_id
    users = await _call("GET", "/Users")
    if not isinstance(users, list) or not users:
        return None
    admin = next((u for u in users if (u.get("Policy") or {}).get("IsAdministrator")), users[0])
    _cached_user_id = admin.get("Id")
    return _cached_user_id


async def system_info():
    """Server name/version, or None if unreachable."""
    return await _call("GET", "/System/Info")


async def refresh_library(force=False):
    """
    Kicks off a library scan. Returns True if the scan was requested, False on error, and
    None when it was skipped by the cooldown (a batch of finished downloads asks for this
    once per file otherwise).
    """
    global _last_refresh_at
    now = time.monotonic()
    if not force and now - _last_refresh_at < JELLYFIN_REFRESH_COOLDOWN:
        return None
    _last_refresh_at = now
    return bool(await _call("POST", "/Library/Refresh", timeout=30))


async def search(term, limit=8):
    """Searches movies and series by name. Returns a (possibly empty) list of item dicts."""
    if not term:
        return []
    params = {
        "searchTerm": term,
        "Recursive": "true",
        "IncludeItemTypes": SEARCH_ITEM_TYPES,
        "Limit": limit,
        "Fields": "ProductionYear,Path",
    }
    user_id = await _user_id()
    if user_id:
        params["userId"] = user_id
    payload = await _call("GET", "/Items", params)
    if not isinstance(payload, dict):
        return []
    return payload.get("Items") or []


async def find_item(title):
    """
    Best single match for a title, preferring an exact (case-insensitive) name over
    Jellyfin's own fuzzy ordering - "The Office" must not resolve to "The Office Blooper
    Reel" just because that one indexed first.
    """
    items = await search(title, limit=8)
    if not items:
        return None
    wanted = title.strip().lower()
    return next((i for i in items if (i.get("Name") or "").strip().lower() == wanted), items[0])


async def wait_for_item(title, attempts=None, delay=None):
    """
    Polls for a title that was just downloaded, until Jellyfin's scan has indexed it.

    A scan started seconds ago hasn't finished yet, so the first lookup almost always misses
    - this is the difference between handing the user a working link and telling them the
    file isn't there.
    """
    attempts = JELLYFIN_LOOKUP_ATTEMPTS if attempts is None else attempts
    delay = JELLYFIN_LOOKUP_DELAY if delay is None else delay
    for attempt in range(attempts):
        if attempt:
            await asyncio.sleep(delay)
        item = await find_item(title)
        if item:
            return item
    return None


async def get_sessions():
    """Active client sessions. Returns a list (empty on error - the caller shows "no viewers")."""
    payload = await _call("GET", "/Sessions")
    return payload if isinstance(payload, list) else []


async def refresh_item_metadata(item_id, replace_all=False):
    """
    Re-scrapes metadata/images for one item.

    replace_all=True is the "you identified this as the wrong show" escape hatch: it throws
    away what Jellyfin already stored and re-identifies from scratch, instead of only filling
    in what is missing.
    """
    params = {
        "metadataRefreshMode": "FullRefresh",
        "imageRefreshMode": "FullRefresh",
        "replaceAllMetadata": "true" if replace_all else "false",
        "replaceAllImages": "false",
    }
    return bool(await _call("POST", f"/Items/{item_id}/Refresh", params, timeout=30))
