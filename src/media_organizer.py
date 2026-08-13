#!/usr/bin/env python3
"""
Media filename parsing (guessit) and Jellyfin folder organization logic.
"""

import os
import re

from guessit import guessit
from rapidfuzz import fuzz, process

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Bidi controls and zero-width characters, which Telegram sprinkles through Hebrew
# captions to force display order. Python's \s matches none of them, so before this they
# survived clean_name, went into the real filename, and made the words look glued
# together: a Hebrew name displaying as one long word is usually real spaces plus RTL
# marks, not missing spaces. They are dropped rather than replaced with a space, since a
# mark sits beside a real space and converting would double every gap.
#
# WARNING: the character class below contains the literal control characters, which your
# editor will not render. Do not "tidy up" the invisible characters out of it - that empties
# the class silently and the bug comes straight back. test_media_organizer covers each range.
_BIDI_AND_ZERO_WIDTH = re.compile(
    "["
    r"​-‏"   # zero-width space/non-joiner/joiner, LRM, RLM
    r"‪-‮"   # LRE, RLE, PDF, LRO, RLO
    r"⁦-⁩"   # LRI, RLI, FSI, PDI
    r"﻿"          # zero-width no-break space (BOM)
    "]"
)
_WHITESPACE = re.compile(r"\s+")

# Matches literal season markers in names (e.g. S03, S01E02, Season 4, 3x07, עונה 2)
_SEASON_MARKER = re.compile(
    r"(?:^|[^a-z0-9])"
    r"(?:s\d{1,2}(?:[\s._-]?e\d{1,3})?|season[\s._-]?\d{1,2}|עונה[\s._-]?\d{1,2}|\d{1,2}x\d{2})"
    r"(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)

# Matches 4-digit number movie titles (e.g. "1917")
_NUMERIC_TITLE = re.compile(r"^(\d{4})(?=\D|$)")

# Default season fallback for series files
DEFAULT_SEASON = 1


def _first(value):
    """guessit returns a list for ranges (e.g. S01E01E02) - take the first."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def parse_media_name(filename_or_title):
    """
    Parses a filename (or bare title) into normalized media info.

    Returns {"kind": "episode"|"movie"|"unknown", "title", "year", "season", "episode",
    "series_marker"}. `series_marker` says the name literally spells out a season, which is
    what propose_folder trusts over the user's own movies/tv choice.
    """
    guess = guessit(filename_or_title)
    title = guess.get("title")
    year = guess.get("year")

    if not title and year:
        # "1917.1080p.BluRay.x264.mkv": guessit reads the leading 1917 as the year and is
        # left with no title at all, so the file would fall into the "unrecognized" bucket.
        # A name that IS just a four-digit number is a real movie title far more often than
        # it is nothing, so take it as the title and drop the year.
        stem = os.path.splitext(os.path.basename(filename_or_title))[0]
        numeric = _NUMERIC_TITLE.match(stem)
        if numeric and int(numeric.group(1)) == year:
            title, year = numeric.group(1), None

    if not title:
        return {
            "kind": "unknown", "title": None, "year": None,
            "season": None, "episode": None, "series_marker": False,
        }

    season = _first(guess.get("season"))
    episode = _first(guess.get("episode"))

    if guess.get("type") == "episode" or season is not None:
        kind = "episode"
    elif guess.get("type") == "movie":
        kind = "movie"
    else:
        kind = "unknown"

    return {
        "kind": kind,
        "title": title.strip(),
        "year": year,
        "season": season,
        "episode": episode,
        "series_marker": bool(_SEASON_MARKER.search(filename_or_title)),
    }


def clean_name(name):
    """Strips invalid or awkward characters from filesystem names."""
    cleaned = _INVALID_CHARS.sub("", name or "")
    # Bidi marks go before the whitespace collapse: dropping one can leave two spaces
    # adjacent ("word RLM SPACE word"), which the collapse below then folds back into one.
    cleaned = _BIDI_AND_ZERO_WIDTH.sub("", cleaned)
    return _WHITESPACE.sub(" ", cleaned).strip(" .")


def sanitize_folder_name(name):
    """clean_name with a fallback string for folder path creation."""
    return clean_name(name) or "Unknown"


def sanitize_file_name(name):
    """Sanitizes filename for safe path joining to prevent directory traversal."""
    return clean_name(os.path.basename(name or ""))


def propose_folder(parsed, preferred_base=None):
    """Turns parsed media info into (media_base, folder_name)."""
    if parsed["kind"] == "unknown" or not parsed.get("title"):
        return None, None

    base = "tv" if parsed["kind"] == "episode" else "movies"
    if preferred_base in ("movies", "tv") and not parsed.get("series_marker"):
        base = preferred_base

    title = sanitize_folder_name(parsed["title"])
    year = parsed.get("year")
    folder = f"{title} ({year})" if year else title

    return base, folder


def find_exact_existing(base_dir, title, year=None):
    """
    Finds an existing subfolder matching target title via exact parsed comparison,
    bypassing fuzzy match false positives (e.g. "The Matrix" vs "The Matrix Reloaded").
    """
    if not os.path.isdir(base_dir) or not title:
        return None

    target = title.strip().lower()
    for name in os.listdir(base_dir):
        if not os.path.isdir(os.path.join(base_dir, name)):
            continue
        existing = parse_media_name(name)
        if (existing.get("title") or "").strip().lower() != target:
            continue
        existing_year = existing.get("year")
        if year and existing_year and year != existing_year:
            continue  # same title, different year - e.g. a remake - don't conflate them
        return name
    return None


def find_similar_existing(base_dir, proposed_name, threshold=87):
    """
    Fuzzy-matches proposed_name against existing subfolders of base_dir, to avoid
    creating a near-duplicate (e.g. "Breaking Bad" vs "Breaking.Bad.2008").

    Always returns the best match found (even below threshold) so the caller can
    still offer it as a manual alternative - `threshold` is only a hint for the
    caller about whether to treat it as a likely duplicate.
    """
    if not os.path.isdir(base_dir):
        return None, 0

    existing = [d for d in os.listdir(base_dir) if os.path.isdir(os.path.join(base_dir, d))]
    if not existing:
        return None, 0

    result = process.extractOne(proposed_name, existing, scorer=fuzz.WRatio)
    if not result:
        return None, 0

    match, score, _ = result
    return match, int(score)


def build_target_dir(media_root, base, folder_name, season=None):
    """Builds the target Jellyfin directory path ("Movies/Title (Year)" or "TV/Title/Season NN")."""
    series_dir = os.path.join(media_root, base, folder_name)
    if base != "tv":
        return series_dir
    return os.path.join(series_dir, f"Season {int(DEFAULT_SEASON if season is None else season):02d}")


def fix_permissions(path):
    """Best-effort chmod 775/664 to allow Jellyfin group access to downloaded media."""
    import logging
    failed = 0
    try:
        if os.path.isdir(path):
            _safe_chmod(path, 0o775)
            for root, dirs, files in os.walk(path):
                for d in dirs:
                    if not _safe_chmod(os.path.join(root, d), 0o775):
                        failed += 1
                for f in files:
                    if not _safe_chmod(os.path.join(root, f), 0o664):
                        failed += 1
        elif os.path.isfile(path):
            _safe_chmod(path, 0o664)
    except Exception as e:
        logging.warning(f"fix_permissions failed on '{path}': {e}")
    if failed:
        logging.warning(f"fix_permissions: {failed} item(s) under '{path}' could not be chmod'd (likely foreign ownership).")


def _safe_chmod(path, mode):
    """Returns True on success, False on a permissions error that should be ignored."""
    try:
        os.chmod(path, mode)
        return True
    except (PermissionError, OSError):
        return False
