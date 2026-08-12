"""Test package. Run with `python -m unittest discover -s tests -t .` from Kraken/.

Two things have to be set up here, before unittest imports any test module - and
therefore before any module under test is imported.

1. The bot's modules live in src/ and import each other flat (`import bot_state as
   state`), exactly as they do at runtime when telegram_bot.py is the entry point.
   Discovery puts Kraken/ on sys.path, not Kraken/src, so add it explicitly rather
   than turning src/ into a package - that would mean rewriting every import.

2. bot_config raises at import time on a missing API_ID/API_HASH/BOT_TOKEN, and it
   sits in the import chain of confirmation_flow (-> bot_state -> bot_config). None
   of these tests touch Telegram, so a checkout without a local .env - a fresh clone,
   or CI - would otherwise fail to even collect them. Seed placeholders only when the
   variable is absent, so a real .env still wins.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

for _name, _placeholder in (
    ("API_ID", "0"),
    ("API_HASH", "test"),
    ("BOT_TOKEN", "test"),
    ("ALLOWED_USER_IDS", "12345"),
):
    os.environ.setdefault(_name, _placeholder)
