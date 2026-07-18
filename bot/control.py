"""Bot lifecycle controller.

Runs the Discord bot as a cancellable task on the caller's event loop (the TUI's
loop) so it can be started, stopped, and restarted from the dashboard. A fresh
bot is built on every start via the injected ``builder``, so configuration edits
(model, rate limits, punitive toggle, token) take effect on the next start
without leaving the process.
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


class BotController:
    def __init__(self, builder: Builder, on_state: StateCallback = None):
        self._builder = builder
        self._on_state = on_state
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
        return self.state in (STARTING, RUNNING, STOPPING)

    def mark_ready(self) -> None:
        """Called when the bot fires on_ready -- transition starting -> running."""
        if self.state in (STARTING, RUNNING):
            self._set(RUNNING)

    # -- controls ---------------------------------------------------------- #

    async def start(self) -> None:
        if self.active:
            return
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
        assert self._bot is not None
        try:
            await self._bot.start(token)
        except asyncio.CancelledError:
            raise
        except discord.LoginFailure as e:
            self._set(ERROR, f"login failed: {e}")
        except Exception as e:
            self._set(ERROR, f"bot stopped: {e}")
        finally:
            if self.state != ERROR:
                self._set(STOPPED)

    async def stop(self) -> None:
        if self._bot is None and self._task is None:
            self._set(STOPPED)
            return
        prev_error = self.state == ERROR
        if not prev_error:
            self._set(STOPPING)
        if self._bot is not None:
            try:
                await self._bot.close()
            except Exception:
                pass
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=15)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                self._task.cancel()
        self._task = None
        self._bot = None
        if self.state != ERROR:
            self._set(STOPPED)

    async def restart(self) -> None:
        await self.stop()
        await self.start()
