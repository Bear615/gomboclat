#!/usr/bin/env python3
"""Entrypoint for the AI-Moderator Discord bot.

Default: launches the TUI dashboard (which runs the bot inside it).
Use --headless to run the bot with plain console logging and no TUI.
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


def main() -> None:
    parser = argparse.ArgumentParser(description="AI-Moderator Discord bot")
    parser.add_argument("--headless", action="store_true", help="Run without the TUI dashboard.")
    args = parser.parse_args()

    config = Config.load()

    if args.headless:
        run_headless(config)
    else:
        from bot.tui import run_tui

        audit, settings_store, ratelimiter, agent = build_components(config)
        run_tui(config, audit, settings_store, ratelimiter, agent)


if __name__ == "__main__":
    main()
