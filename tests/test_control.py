"""Tests for bot lifecycle supervision and crash recovery."""

from __future__ import annotations

import asyncio

import discord

from bot import control


class _FakeBot:
    def __init__(self, failure: Exception | None = None):
        self.failure = failure
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def start(self, token: str) -> None:
        self.started.set()
        if self.failure is not None:
            raise self.failure
        await self.closed.wait()

    async def close(self) -> None:
        self.closed.set()


async def _eventually(predicate, timeout: float = 1.0) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout)


def test_runtime_crash_rebuilds_and_restarts_bot() -> None:
    async def scenario() -> None:
        bots = [_FakeBot(RuntimeError("socket exploded")), _FakeBot()]
        states: list[tuple[str, str]] = []
        builds = 0

        def builder():
            nonlocal builds
            bot = bots[builds]
            builds += 1
            return bot, "token"

        controller = control.BotController(
            builder,
            on_state=lambda state, error: states.append((state, error)),
            restart_delays=(0,),
        )
        await controller.start()
        await asyncio.wait_for(bots[1].started.wait(), 1)
        controller.mark_ready()

        assert builds == 2
        assert bots[0].closed.is_set()
        assert controller.state == control.RUNNING
        assert any(
            state == control.RESTARTING and "socket exploded" in error
            for state, error in states
        )

        await controller.stop()
        assert controller.state == control.STOPPED

    asyncio.run(scenario())


def test_clean_exit_is_not_restarted() -> None:
    async def scenario() -> None:
        builds = 0

        class CleanExitBot(_FakeBot):
            async def start(self, token: str) -> None:
                self.started.set()
                return

        def builder():
            nonlocal builds
            builds += 1
            return CleanExitBot(), "token"

        controller = control.BotController(builder, restart_delays=(0,))
        await controller.start()
        await _eventually(lambda: controller.state == control.STOPPED)

        assert builds == 1

    asyncio.run(scenario())


def test_manual_stop_does_not_restart_bot() -> None:
    async def scenario() -> None:
        bot = _FakeBot()
        builds = 0

        def builder():
            nonlocal builds
            builds += 1
            return bot, "token"

        controller = control.BotController(builder, restart_delays=(0,))
        await controller.start()
        await asyncio.wait_for(bot.started.wait(), 1)
        controller.mark_ready()
        await controller.stop()
        await asyncio.sleep(0)

        assert builds == 1
        assert controller.state == control.STOPPED

    asyncio.run(scenario())


def test_login_failure_is_not_retried() -> None:
    async def scenario() -> None:
        builds = 0

        def builder():
            nonlocal builds
            builds += 1
            return _FakeBot(discord.LoginFailure("bad token")), "token"

        controller = control.BotController(builder, restart_delays=(0,))
        await controller.start()
        await _eventually(lambda: controller.state == control.ERROR)

        assert builds == 1
        assert "login failed" in controller.last_error

        await controller.stop()

    asyncio.run(scenario())
