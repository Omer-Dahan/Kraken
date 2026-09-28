#!/usr/bin/env python3
"""
Media filename parsing (guessit) and Jellyfin folder organization logic.
"""

import functools
import os
import re
import unicodedata

from guessit import guessit
from rapidfuzz import fuzz, process

import learned_names

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

# Links and @handles that channels stamp into names. guessit files them under
# alternative_title at best, or swallows them into the title itself. A link runs to the
# next space, but stops short of a trailing ".mkv".
_LINK_OR_HANDLE = re.compile(r"(?:https?://|www\.|t\.me/)\S+?(?=\s|\.\w{2,4}$|$)|@\w+", re.IGNORECASE)

# Hebrew letters that attach to the front of a word ("מזירה מדיה", "והשימיה"), so an
# ignored phrase is still caught when one of them is glued on.
_HEBREW_PREFIX = "[ובלמהשכ]?"

# Separator runs left behind once something is cut out of the middle of a name.
_EMPTY_BRACKETS = re.compile(r"[\[({]\s*[\])}]")
_DASH_RUN = re.compile(r"(?:\s*[-–—|]\s*){2,}")
_EDGE_SEPARATORS = re.compile(r"^[\s._\-–—|:]+|[\s_\-–—|:]+(?=\.\w{2,4}$)|[\s._\-–—|:]+$")

# guessit knows "S02E05" but no Hebrew at all. These rewrite the common Hebrew spellings
# into that form, most specific first so "עונה 2 פרק 5" doesn't become "S02 פרק 5".
_SEP = r"[\s._\-'׳,]*"
_WORD_START = rf"(?<![^\W_]){_HEBREW_PREFIX}"
_HEBREW_EPISODE_MARKERS = [
    (re.compile(rf"{_WORD_START}עונה{_SEP}(\d{{1,2}}){_SEP}פרק{_SEP}(\d{{1,3}})(?!\d)"), "S{0:02d}E{1:02d}"),
    (re.compile(rf"(?<![^\W_])ע{_SEP}(\d{{1,2}}){_SEP}פ{_SEP}(\d{{1,3}})(?!\d)"), "S{0:02d}E{1:02d}"),
    (re.compile(rf"{_WORD_START}עונה{_SEP}(\d{{1,2}})(?!\d)"), "S{0:02d}"),
    (re.compile(rf"{_WORD_START}פרק{_SEP}(\d{{1,3}})(?!\d)"), "E{0:02d}"),
]


@functools.lru_cache(maxsize=8)
def _ignore_pattern(ignore_words):
    """
    One alternation for the whole ignore list, longest phrase first so "לולו סרטים" wins
    over a shorter "לולו". Inside a phrase any separator run - or none at all - matches, so
    "ז.מ" also catches "ז מ" and "לולו סרטים" also catches "לולו.סרטים" and "לולוסרטים".
    """
    alternatives = []
    for phrase in sorted(ignore_words, key=len, reverse=True):
        words = [re.escape(w) for w in re.split(r"[\s._\-]+", phrase) if w]
        if words:
            alternatives.append(r"[\s._\-]*".join(words))
    if not alternatives:
        return None
    return re.compile(rf"(?<![^\W_]){_HEBREW_PREFIX}(?:{'|'.join(alternatives)})(?![^\W_])", re.IGNORECASE)


def _hebrew_to_episode_markers(name):
    for pattern, template in _HEBREW_EPISODE_MARKERS:
        name = pattern.sub(lambda m: f" {template.format(*map(int, m.groups()))} ", name)
    return name


def strip_release_noise(name, ignore_words=()):
    """
    Cuts what isn't the title out of a name before guessit sees it: ignored phrases (channel
    tags and the like), links and @handles, and emoji. Hebrew season/episode wording is
    rewritten as S01E02 on the way.

    This matters beyond tidiness: in "זירה מדיה - הסרט שלי 2020" guessit takes the part
    before the dash as the title and demotes the real title to alternative_title.
    """
    name = _BIDI_AND_ZERO_WIDTH.sub("", name or "")
    name = _LINK_OR_HANDLE.sub(" ", name)
    name = "".join(c for c in name if unicodedata.category(c) != "So" and c != "️")
    pattern = _ignore_pattern(tuple(ignore_words))
    if pattern:
        name = pattern.sub(" ", name)
    name = _hebrew_to_episode_markers(name)
    name = _EMPTY_BRACKETS.sub(" ", name)
    name = _DASH_RUN.sub(" - ", name)
    name = _EDGE_SEPARATORS.sub("", name)
    return _WHITESPACE.sub(" ", name).strip()


def _first(value):
    """guessit returns a list for ranges (e.g. S01E01E02) - take the first."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def parse_media_name(filename_or_title):
    """
    Parses a filename (or bare title) into normalized media info.

    Returns {"kind": "episode"|"movie"|"unknown", "title", "detected_title", "year", "season",
    "episode", "series_marker"}. `series_marker` says the name literally spells out a season, which is
    what propose_folder trusts over the user's own movies/tv choice.

    A title the user has corrected before (see learned_names) comes back as the corrected one;
    `detected_title` keeps what the name itself said, which is what a new correction is keyed on.
    """
    memory = learned_names.memory
    name = strip_release_noise(filename_or_title, memory.ignore_words())
    guess = guessit(name) if name else {}
    title = guess.get("title")
    year = guess.get("year")

    if not title and year:
        # "1917.1080p.BluRay.x264.mkv": guessit reads the leading 1917 as the year and is
        # left with no title at all, so the file would fall into the "unrecognized" bucket.
        # A name that IS just a four-digit number is a real movie title far more often than
        # it is nothing, so take it as the title and drop the year.
        stem = os.path.splitext(os.path.basename(name))[0]
        numeric = _NUMERIC_TITLE.match(stem)
        if numeric and int(numeric.group(1)) == year:
            title, year = numeric.group(1), None

    if not title:
        return {
            "kind": "unknown", "title": None, "detected_title": None, "year": None,
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

    title = detected_title = title.strip()
    alias = memory.alias_for(detected_title)
    if alias:
        title, alias_year = alias
        year = alias_year if alias_year is not None else year

    return {
        "kind": kind,
        "title": title,
        "detected_title": detected_title,
        "year": year,
        "season": season,
        "episode": episode,
        "series_marker": bool(_SEASON_MARKER.search(name)),
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
