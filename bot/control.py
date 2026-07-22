"""Bot lifecycle controller.

Runs the Discord bot as a cancellable task on the caller's event loop (the TUI's
loop) so it can be started, stopped, and restarted from the dashboard. A fresh
bot is built on every start via the injected ``builder``, so configuration edits
(model, rate limits, punitive toggle, token) take effect on the next start
without leaving the process.

Unexpected runtime failures are supervised here too. The controller rebuilds
the bot and retries with bounded backoff, while deliberate stops, clean exits,
and invalid credentials remain stopped.
"""

from __future__ import annotations

import asyncio
from typing import Callable

import discord
from discord.ext import commands

# builder() -> (bot, token). Raises on misconfiguration (e.g. missing secrets).
Builder = Callable[[], "tuple[commands.Bot, str]"]
StateCallback = Callable[[str, str], None] | None

STOPPED = "stopped"
STARTING = "starting"
RUNNING = "running"
STOPPING = "stopping"
ERROR = "error"
RESTARTING = "restarting"

DEFAULT_RESTART_DELAYS = (5.0, 10.0, 30.0, 60.0)


class BotController:
    def __init__(
        self,
        builder: Builder,
        on_state: StateCallback = None,
        restart_delays: tuple[float, ...] = DEFAULT_RESTART_DELAYS,
    ):
        self._builder = builder
        self._on_state = on_state
        self._restart_delays = tuple(max(0.0, delay) for delay in restart_delays)
        self._restart_attempt = 0
        self._stop_requested = False
        self._bot: commands.Bot | None = None
        self._task: asyncio.Task | None = None
        self.state = STOPPED
        self.last_error = ""

    # -- state ------------------------------------------------------------- #

    def _set(self, state: str, error: str = "") -> None:
        self.state = state
        self.last_error = error
        if self._on_state:
            self._on_state(state, error)

    @property
    def active(self) -> bool:
        return self.state in (STARTING, RUNNING, RESTARTING, STOPPING)

    def mark_ready(self) -> None:
        """Called when the bot fires on_ready -- transition starting -> running."""
        if self.state in (STARTING, RUNNING):
            # A successful connection ends the previous crash streak. A later
            # independent crash should start again at the shortest delay.
            self._restart_attempt = 0
            self._set(RUNNING)

    async def _close_bot(self) -> None:
        """Best-effort cleanup for both crashed and deliberately stopped clients."""
        if self._bot is None:
            return
        try:
            await self._bot.close()
        except Exception:
            pass

    # -- controls ---------------------------------------------------------- #

    async def start(self) -> None:
        if self.active:
            return
        self._stop_requested = False
        self._restart_attempt = 0
        try:
            self._bot, token = self._builder()
        except SystemExit as e:  # missing secrets, etc.
            self._set(ERROR, str(e) or "configuration incomplete")
            return
        except Exception as e:
            self._set(ERROR, f"build failed: {e}")
            return
        self._set(STARTING)
        self._task = asyncio.create_task(self._runner(token))

    async def _runner(self, token: str) -> None:
        try:
            while True:
                assert self._bot is not None
                try:
                    await self._bot.start(token)
                except asyncio.CancelledError:
                    raise
                except discord.LoginFailure as e:
                    # Credentials need a human fix. Retrying forever would only
                    # hammer Discord with the same invalid token.
                    await self._close_bot()
                    self._set(ERROR, f"login failed: {e}")
                    return
                except Exception as e:
                    await self._close_bot()
                    if self._stop_requested or not self._restart_delays:
                        self._set(ERROR, f"bot crashed: {e}")
                        return

                    index = min(self._restart_attempt, len(self._restart_delays) - 1)
                    delay = self._restart_delays[index]
                    self._restart_attempt += 1
                    self._set(
                        RESTARTING,
                        f"bot crashed: {e}; restarting in {delay:g}s",
                    )
                    await asyncio.sleep(delay)
                    if self._stop_requested:
                        return

                    try:
                        self._bot, token = self._builder()
                    except SystemExit as build_error:
                        self._set(ERROR, str(build_error) or "configuration incomplete")
                        return
                    except Exception as build_error:
                        self._set(ERROR, f"restart build failed: {build_error}")
                        return
                    self._set(STARTING)
                    continue

                # A normal return (for example after bot.close()) is deliberate.
                return
        finally:
            if self.state != ERROR:
                self._set(STOPPED)

    async def stop(self) -> None:
        self._stop_requested = True
        if self._bot is None and self._task is None:
            self._set(STOPPED)
            return
        if self.active:
            self._set(STOPPING)
        await self._close_bot()
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=15)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                self._task.cancel()
        self._task = None
        self._bot = None
        self._set(STOPPED)

    async def restart(self) -> None:
        await self.stop()
        await self.start()
