#!/usr/bin/env python3
"""Entrypoint for the AI-Moderator Discord bot.

Default: launches the TUI dashboard (which runs the bot inside it).
Use --headless to run the bot with plain console logging and no TUI.
Use --web to run the browser control hub (see deploy/install-web.sh).
"""

from __future__ import annotations

import argparse
import logging

from bot.ai import Agent
from bot.audit import AuditLogger
from bot.config import Config, GuildSettingsStore
from bot.ratelimit import RateLimiter


def build_components(config: Config):
    audit = AuditLogger(config.db_path)
    settings_store = GuildSettingsStore(config.db_path)
    ratelimiter = RateLimiter(config.rate_limit_max, config.rate_limit_window)
    agent = Agent(config)
    return audit, settings_store, ratelimiter, agent


def run_headless(config: Config) -> None:
    from bot.audit import AuditRecord
    from bot.main import BotHooks, create_bot

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("moderator")

    audit, settings_store, ratelimiter, agent = build_components(config)
    audit.subscribe(lambda rec: log.info(rec.summary_line()))
    hooks = BotHooks(
        on_ready=lambda user, guilds: log.info("Connected as %s (%d guilds)", user, len(guilds)),
        on_status=lambda s: log.info(s),
        on_message_seen=lambda s: log.info("MESSAGE %s", s),
    )
    bot = create_bot(config, audit, settings_store, ratelimiter, agent, hooks)
    bot.run(config.discord_token)


def set_web_password() -> None:
    """Interactive password setup for the web hub. Stores only an scrypt hash
    (plus a session-signing secret if one doesn't exist yet)."""
    import getpass

    from bot import websecurity
    from bot.config import update_env_file

    password = getpass.getpass("New web UI admin password: ")
    if len(password) < websecurity.MIN_PASSWORD_LENGTH:
        raise SystemExit(f"Password must be at least {websecurity.MIN_PASSWORD_LENGTH} characters.")
    if getpass.getpass("Repeat password: ") != password:
        raise SystemExit("Passwords did not match — nothing changed.")

    updates = {"WEB_PASSWORD_HASH": websecurity.hash_password(password)}
    if not Config.load(require_secrets=False).web_session_secret:
        updates["WEB_SESSION_SECRET"] = websecurity.generate_secret()
    update_env_file(updates)
    print("Web admin password saved to .env (as an scrypt hash).")


def main() -> None:
    parser = argparse.ArgumentParser(description="AI-Moderator Discord bot")
    parser.add_argument("--headless", action="store_true", help="Run without the TUI dashboard.")
    parser.add_argument("--web", action="store_true", help="Run the web control hub (for nginx; see deploy/install-web.sh).")
    parser.add_argument("--set-web-password", action="store_true", help="Set the web UI admin password and exit.")
    args = parser.parse_args()

    if args.set_web_password:
        set_web_password()
    elif args.web:
        # The web hub self-manages the bot lifecycle, like the TUI, and binds
        # to localhost — expose it via nginx+TLS (deploy/install-web.sh).
        from bot.web import run_web

        run_web()
    elif args.headless:
        run_headless(Config.load())
    else:
        # The TUI hub self-manages config, components, and the bot lifecycle, and
        # opens even before secrets are set (so you can configure from the UI).
        from bot.tui import run_tui

        run_tui()


if __name__ == "__main__":
    main()
