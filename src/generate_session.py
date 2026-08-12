#!/usr/bin/env python3
"""
Interactive Telethon Session Generator
--------------------------------------
Run this script once directly in your SSH terminal to generate a fresh,
valid Telethon session file for your Telegram Userbot.
"""

import os
import asyncio
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# Same directory telegram_bot.resolve_session() searches. Anchored to the project root,
# never the working directory: run from src/, a bare "userbot.session" would land
# somewhere the bot never looks, and the service would crash-loop on a login prompt.
SESSION_DIR = BASE_DIR / "session"


def _required(name):
    value = os.getenv(name)
    if value:
        return value
    raise SystemExit(f"Missing {name}. Copy .env.example to .env and set it before generating a session.")


API_ID = int(_required("API_ID"))
API_HASH = _required("API_HASH")

async def main():
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    session_file = str(SESSION_DIR / "userbot.session")
    session_name = session_file.rsplit(".session", 1)[0]

    print("==================================================")
    print("       Telegram Telethon Session Generator        ")
    print("==================================================")
    print(f"Target session file: {session_file}\n")
    print("Follow the prompts below to authenticate with Telegram.")
    print("(You will be asked for your phone number, e.g. +972XXXXXXXXX, and login code)\n")

    client = TelegramClient(session_name, API_ID, API_HASH)
    await client.start()
    
    me = await client.get_me()
    print("\n==================================================")
    print(f"✅ SUCCESS: Session created successfully!")
    print(f"👤 Account: {me.first_name} {me.last_name or ''} (@{me.username or 'N/A'})")
    print(f"🆔 User ID: {me.id}")
    print(f"📁 Session saved to: {session_file}")
    print("==================================================\n")
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
