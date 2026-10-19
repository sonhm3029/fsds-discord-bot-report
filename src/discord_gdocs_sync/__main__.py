"""Command line entry point.

    python -m discord_gdocs_sync [--config config.toml] run        # run continuously (default)
    python -m discord_gdocs_sync [--config config.toml] sync-once  # sync everything, then exit
    python -m discord_gdocs_sync [--config config.toml] check      # verify Google + Discord access
    python -m discord_gdocs_sync [--config config.toml] reminder   # preview today's reminder (--send / --send-public to deliver)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

import discord
from dotenv import load_dotenv

from .bot import SyncBot
from .config import Config, ConfigError, load_config
from .gdocs import DocsError, GoogleDocsClient, load_credentials
from .notify import TelegramNotifier
from .state import StateStore
from .sync import SyncEngine


def _check_google(cfg: Config, docs: GoogleDocsClient) -> bool:
    ok = True
    try:
        email = json.loads(cfg.credentials_file.read_text()).get("client_email")
    except Exception:
        email = None
    for s in cfg.students:
        try:
            title = docs.doc_title(s.doc_id)
            tabs = docs.list_tabs(s.doc_id)
            print(f"  ✓ {s.name}: Google Doc {title!r} ({len(tabs)} tab(s))")
        except DocsError as exc:
            ok = False
            hint = ""
            if exc.status in (403, 404):
                who = email or "the account that owns the credentials"
                hint = f" — share the doc with {who} as Editor, and check that the Google Docs API is enabled"
            print(f"  ✗ {s.name}: {exc}{hint}")
    print("Google check:", "OK" if ok else "problems found")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="discord-gdocs-sync", description=__doc__.split("\n")[0])
    parser.add_argument("--config", default=os.environ.get("DGS_CONFIG", "config.toml"))
    parser.add_argument("command", nargs="?", choices=["run", "sync-once", "check", "reminder"], default="run")
    parser.add_argument("--send", action="store_true", help="with 'reminder': send the private summary")
    parser.add_argument("--send-public", action="store_true", help="with 'reminder': post in the topic channels, tagging students")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    load_dotenv()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    try:
        cfg = load_config(args.config)
        token = os.environ.get("DISCORD_TOKEN")
        if not token:
            raise ConfigError("DISCORD_TOKEN is not set (put it in .env or the environment)")
        creds = load_credentials(cfg.credentials_file)
        telegram = None
        wants_reminder = cfg.reminder.enabled or args.command == "reminder"
        if wants_reminder and "telegram" in cfg.reminder.channels:
            tg_token, tg_chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
            if not (tg_token and tg_chat):
                raise ConfigError("reminder uses Telegram: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
            telegram = TelegramNotifier(tg_token, tg_chat)
    except (ConfigError, ValueError, OSError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    docs = GoogleDocsClient(creds)
    mode = {"run": "run", "sync-once": "once", "check": "check", "reminder": "reminder"}[args.command]
    google_ok = True
    if mode == "check":
        google_ok = _check_google(cfg, docs)

    state = StateStore(cfg.state_path)
    bot = SyncBot(cfg, SyncEngine(cfg, docs, state), state, mode=mode, telegram=telegram, send=args.send, send_public=args.send_public)
    try:
        bot.run(token, log_handler=None)
    except discord.LoginFailure:
        print("Discord rejected DISCORD_TOKEN. Reset the bot token in the developer portal.", file=sys.stderr)
        return 2
    except discord.PrivilegedIntentsRequired:
        print(
            "Enable 'Message Content Intent' for the bot: Discord developer portal → Bot → Privileged Gateway Intents.",
            file=sys.stderr,
        )
        return 2
    finally:
        state.close()
    return 1 if (bot.failures or not google_ok) else 0


if __name__ == "__main__":
    sys.exit(main())
