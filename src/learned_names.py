"""What the bot has learned about naming from its user: phrases that are never part of a
title (channel tags such as "לולו סרטים"), and titles the user corrected by hand.

Both live in one small JSON file next to the code. It grows by a line each time a name is
fixed, so it's simply loaded once and rewritten whole on every change.
"""

import json
import logging
import os
import re
from pathlib import Path

LEARNED_NAMES_PATH = Path(__file__).resolve().parent.parent / "learned_names.json"

# What the list starts as before the user has touched it. Once the file exists it is the
# only source, so /unignore can remove any of these too.
DEFAULT_IGNORE_WORDS = [
    # Channel and uploader tags
    "זירה מדיה",
    "השימיה",
    "לולו סרטים",
    "ז.מ",
    # Release notes that end up glued to the title
    "מתורגם",
    "תרגום מובנה",
    "כתוביות מובנות",
    "מדובב",
    "לצפייה ישירה",
    "איכות גבוהה",
    "בלעדי",
]

_SEPARATORS = re.compile(r"[\s._\-]+")

# How many "always ignore X?" buttons a single rename can offer, so a completely retyped
# name doesn't bury the confirmation under a button per word.
MAX_SUGGESTIONS = 3

# The shortest single word worth offering as a tag - below this it's an article or a
# preposition ("The", "של"), not a channel name.
MIN_SINGLE_WORD_TAG = 4


def _key(text):
    """Casefolded, separator-insensitive form: "Lulu.Movies" and "lulu movies" are one key."""
    return _SEPARATORS.sub(" ", text or "").strip().casefold()


class NameMemory:
    def __init__(self, path):
        self.path = Path(path)
        self._ignore = list(DEFAULT_IGNORE_WORDS)
        self._aliases = {}
        self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            # Running on defaults beats refusing to start, but the next save would overwrite
            # the broken file - keep a copy so a hand edit gone wrong isn't lost with it.
            logging.warning(f"Could not read {self.path} ({e}); keeping it as .bak and starting from defaults.")
            try:
                os.replace(self.path, self.path.with_suffix(".json.bak"))
            except OSError:
                pass
            return
        self._ignore = [w for w in data.get("ignore", []) if isinstance(w, str) and w.strip()]
        self._aliases = {k: v for k, v in data.get("aliases", {}).items() if isinstance(v, dict) and v.get("title")}

    def _save(self):
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ignore": self._ignore, "aliases": self._aliases}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def ignore_words(self):
        return tuple(self._ignore)

    def add_ignore_word(self, phrase):
        """Returns False when the phrase (or its separator-insensitive twin) is already there."""
        phrase = _SEPARATORS.sub(" ", phrase or "").strip()
        if not phrase or any(_key(w) == _key(phrase) for w in self._ignore):
            return False
        self._ignore.append(phrase)
        self._save()
        return True

    def remove_ignore_word(self, phrase):
        remaining = [w for w in self._ignore if _key(w) != _key(phrase)]
        if len(remaining) == len(self._ignore):
            return False
        self._ignore = remaining
        self._save()
        return True

    def alias_for(self, title):
        """The (title, year) the user filed this detected title under last time, or None."""
        alias = self._aliases.get(_key(title))
        return (alias["title"], alias.get("year")) if alias else None

    def remember_alias(self, detected_title, title, year=None):
        """Returns True if something new was learned."""
        key = _key(detected_title)
        if not key or not title or key == _key(title):
            return False
        entry = {"title": title, "year": year}
        if self._aliases.get(key) == entry:
            return False
        self._aliases[key] = entry
        self._save()
        return True


def dropped_phrases(detected_title, typed_title, known=()):
    """
    The runs of words the user cut from the start or end when retyping a title - each one a
    candidate for the ignore list. "לולו סרטים הבית" retyped as "הבית" gives ["לולו סרטים"].

    Digit-only runs are left out (a year or a season number, not a channel tag), and so is
    anything `known` already covers. A retype that shares no word with the original is a
    replacement - "הבית של הדרקון" as "House of the Dragon" - not a trim, so it offers nothing.
    """
    kept = set(_key(typed_title).split())
    words = [w for w in _SEPARATORS.split(detected_title or "") if w]
    if not any(w.casefold() in kept for w in words):
        return []

    # Only the words cut from either end: that's where channels stamp their tags. A word
    # dropped from the middle is an edit to the title itself, and an ignore phrase applies to
    # every future name - "Spider Man No Way Home" retyped without "No" must not offer it.
    first_kept = next(i for i, w in enumerate(words) if w.casefold() in kept)
    last_kept = max(i for i, w in enumerate(words) if w.casefold() in kept)
    runs = [words[:first_kept], words[last_kept + 1:]]

    known_keys = {_key(k) for k in known}
    phrases = []
    for run in runs:
        phrase = " ".join(run)
        if not run or phrase.replace(" ", "").isdigit() or _key(phrase) in known_keys or phrase in phrases:
            continue
        # "The Matrix" retyped as "Matrix" would otherwise offer "The" - one tap and every
        # title in the library loses its article. Real tags are longer, or more than one word.
        if len(run) == 1 and len(phrase) < MIN_SINGLE_WORD_TAG:
            continue
        phrases.append(phrase)
    return phrases[:MAX_SUGGESTIONS]


memory = NameMemory(LEARNED_NAMES_PATH)
