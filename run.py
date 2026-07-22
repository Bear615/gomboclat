#!/usr/bin/env python3
"""Entrypoint for the AI-Moderator Discord bot.

Default: launches the TUI dashboard (which runs the bot inside it).
Use --headless to run the bot with plain console logging and no TUI.
"""

from __future__ import annotations

import argparse
import logging
import time

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
    import discord

    from bot.audit import AuditRecord
    from bot.main import BotHooks, create_bot

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("moderator")

    restart_delays = (5, 10, 30, 60)
    crash_count = 0

    while True:
        audit, settings_store, ratelimiter, agent = build_components(config)
        audit.subscribe(lambda rec: log.info(rec.summary_line()))

        def ready(user, guilds) -> None:
            nonlocal crash_count
            crash_count = 0
            log.info("Connected as %s (%d guilds)", user, len(guilds))

        hooks = BotHooks(
            on_ready=ready,
            on_status=lambda s: log.info(s),
            on_message_seen=lambda s: log.info("MESSAGE %s", s),
        )
        bot = create_bot(config, audit, settings_store, ratelimiter, agent, hooks)
        try:
            bot.run(config.discord_token)
        except discord.LoginFailure:
            log.exception("Discord login failed; fix DISCORD_TOKEN before restarting.")
            raise
        except Exception:
            delay = restart_delays[min(crash_count, len(restart_delays) - 1)]
            crash_count += 1
            log.exception("Bot crashed; restarting in %ss.", delay)
            time.sleep(delay)
            # Pick up any configuration repair made while the bot was down.
            config = Config.load()
            continue
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="AI-Moderator Discord bot")
    parser.add_argument("--headless", action="store_true", help="Run without the TUI dashboard.")
    parser.add_argument("--web", action="store_true", help="Run the browser control hub on localhost:8765.")
    args = parser.parse_args()

    if args.web:
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
