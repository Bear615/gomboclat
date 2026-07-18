"""Terminal UI dashboard.

Textual runs on asyncio and so does discord.py, so we run the bot as a worker on
the TUI's own event loop and stream status + audit events into the dashboard.

Panels:
  * Status: connection state, logged-in bot user, guild list.
  * Live audit feed: every executed/refused action, with the REAL requester.

Run headless (no TUI) with the --headless flag on run.py.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Static

try:  # RichLog was renamed from TextLog in newer Textual.
    from textual.widgets import RichLog
except ImportError:  # pragma: no cover
    from textual.widgets import TextLog as RichLog  # type: ignore

from .ai import Agent
from .audit import AuditLogger, AuditRecord
from .config import Config, GuildSettingsStore
from .main import BotHooks, create_bot
from .ratelimit import RateLimiter


class StatusPanel(Static):
    """Left-hand panel: connection + guild list."""

    def update_status(self, *, connected: bool, user: str, model: str, guilds: list[str]) -> None:
        dot = "[green]● online[/]" if connected else "[red]● offline[/]"
        lines = [
            "[b]AI Moderator[/b]",
            "",
            f"Status : {dot}",
            f"Bot    : {user}",
            f"Model  : {model}",
            f"Guilds : {len(guilds)}",
            "",
            "[b]Servers[/b]",
        ]
        lines += [f"  • {g}" for g in guilds] or ["  (none yet)"]
        self.update("\n".join(lines))


class ModeratorTUI(App):
    CSS = """
    Screen { layout: vertical; }
    #body { height: 1fr; }
    StatusPanel { width: 34; border: round $accent; padding: 1 2; }
    #feed { border: round $accent; padding: 0 1; }
    """
    BINDINGS = [("q", "quit", "Quit"), ("c", "clear_feed", "Clear feed")]

    def __init__(
        self,
        config: Config,
        audit: AuditLogger,
        settings_store: GuildSettingsStore,
        ratelimiter: RateLimiter,
        agent: Agent,
    ):
        super().__init__()
        self.config = config
        self.audit = audit
        self.settings_store = settings_store
        self.ratelimiter = ratelimiter
        self.agent = agent
        self._queue: asyncio.Queue = asyncio.Queue()
        self._connected = False
        self._user = "connecting…"
        self._guilds: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            yield StatusPanel(id="status")
            with Vertical():
                yield RichLog(id="feed", highlight=True, markup=True, wrap=True)
        yield Footer()

    # -- lifecycle --------------------------------------------------------- #

    def on_mount(self) -> None:
        self.title = "AI Moderator — Discord"
        self.sub_title = "mention the bot in Discord to make requests"
        self._refresh_status()

        feed = self.query_one("#feed", RichLog)
        feed.write("[dim]Starting up… loading recent audit history.[/]")
        for rec in reversed(self.audit.recent(20)):
            feed.write(self._format_record(rec))

        # Wire hooks + audit subscription into the queue (loop-safe).
        loop = asyncio.get_running_loop()

        def enqueue(kind: str, payload) -> None:
            loop.call_soon_threadsafe(self._queue.put_nowait, (kind, payload))

        self.audit.subscribe(lambda rec: enqueue("audit", rec))
        hooks = BotHooks(
            on_ready=lambda user, guilds: enqueue("ready", (str(user), [g.name for g in guilds])),
            on_status=lambda s: enqueue("status", s),
            on_message_seen=lambda s: enqueue("seen", s),
        )
        self._bot = create_bot(
            self.config, self.audit, self.settings_store, self.ratelimiter, self.agent, hooks
        )

        self.run_worker(self._consume(), name="consume", exclusive=False)
        self.run_worker(self._run_bot(), name="bot", exclusive=False)

    async def _run_bot(self) -> None:
        feed = self.query_one("#feed", RichLog)
        try:
            await self._bot.start(self.config.discord_token)
        except Exception as e:  # login failure, bad token, etc.
            feed.write(f"[red]Bot stopped: {e}[/]")
            self._connected = False
            self._refresh_status()

    async def _consume(self) -> None:
        feed = self.query_one("#feed", RichLog)
        while True:
            kind, payload = await self._queue.get()
            if kind == "ready":
                self._user, self._guilds = payload
                self._connected = True
                self._refresh_status()
                feed.write(f"[green]Connected as {self._user}.[/]")
            elif kind == "status":
                feed.write(f"[dim]{self._ts()} · {payload}[/]")
            elif kind == "seen":
                feed.write(f"[cyan]{self._ts()} · 📨 {payload}[/]")
            elif kind == "audit":
                feed.write(self._format_record(payload))

    async def on_unmount(self) -> None:
        try:
            await self._bot.close()
        except Exception:
            pass

    # -- helpers ----------------------------------------------------------- #

    def action_clear_feed(self) -> None:
        self.query_one("#feed", RichLog).clear()

    def _refresh_status(self) -> None:
        self.query_one(StatusPanel).update_status(
            connected=self._connected, user=self._user, model=self.config.anthropic_model, guilds=self._guilds
        )

    @staticmethod
    def _ts() -> str:
        return datetime.now().strftime("%H:%M:%S")

    def _format_record(self, rec: AuditRecord) -> str:
        if rec.allowed:
            tag = "[green]✅ EXECUTED[/]"
        else:
            tag = "[red]⛔ REFUSED [/]"
        return (
            f"{tag} [b]{rec.action}[/b] · {rec.requester_name} "
            f"[dim]({rec.requester_id})[/] · {rec.guild_name}\n"
            f"    {rec.outcome}"
        )


def run_tui(
    config: Config,
    audit: AuditLogger,
    settings_store: GuildSettingsStore,
    ratelimiter: RateLimiter,
    agent: Agent,
) -> None:
    ModeratorTUI(config, audit, settings_store, ratelimiter, agent).run()
