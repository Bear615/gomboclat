"""The control hub: a Textual TUI that runs and manages the whole bot.

Everything happens here — you never need to touch the shell after launch:

  • Dashboard  — start / stop / restart the bot, live status, live audit feed.
  • Configure  — edit every setting (tokens, model, rate limits, punitive,
                 auto-update) and save it to .env.
  • Maintenance — install / reinstall dependencies, check GitHub for updates,
                 pull + reinstall + auto-restart, with a streamed output log.

Textual and discord.py share one asyncio loop, so the bot runs as a managed task
right inside this app (see control.BotController).

Navigation: switch tabs with 1 / 2 / 3, move between fields with Tab /
Shift+Tab, save the config from anywhere with Ctrl+S. All key bindings are
listed in the footer.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Static,
    Switch,
    TabbedContent,
    TabPane,
)

from . import control, maintenance
from .ai import Agent
from .audit import AuditLogger, AuditRecord
from .config import Config, GuildSettingsStore, update_env_file
from .main import BotHooks, create_bot
from .ratelimit import RateLimiter

STATE_STYLE = {
    control.RUNNING: "[b green]● running[/]",
    control.STARTING: "[b yellow]● starting…[/]",
    control.STOPPING: "[b yellow]● stopping…[/]",
    control.RESTARTING: "[b yellow]● restarting…[/]",
    control.STOPPED: "[dim]○ stopped[/]",
    control.ERROR: "[b red]● error[/]",
}

TAB_TITLES = {
    "dashboard": "Dashboard",
    "configure": "Configure",
    "maintenance": "Maintenance",
}


class ModeratorHub(App):
    CSS = """
    Screen { background: $surface; }

    TabbedContent { height: 1fr; }
    TabPane { padding: 1 2 0 2; }

    /* ---- Buttons: flat coloured text ---- */
    Button {
        border: none;
        height: 1;
        min-width: 0;
        padding: 0 1;
        text-style: bold;
        background: transparent;
    }
    Button.-default { background: transparent; color: $text; }
    Button.-primary { background: transparent; color: $primary; }
    Button.-success { background: transparent; color: $success; }
    Button.-warning { background: transparent; color: $warning; }
    Button.-error   { background: transparent; color: $error; }
    Button:hover, Button.-default:hover, Button.-primary:hover, Button.-success:hover,
    Button.-warning:hover, Button.-error:hover { border: none; background: $boost; }
    Button.-active, Button.-default.-active, Button.-primary.-active, Button.-success.-active,
    Button.-warning.-active, Button.-error.-active { border: none; background: $boost; }
    Button:focus { text-style: bold reverse; }

    /* ---- Dashboard ---- */
    #status {
        width: 42; margin: 0 1 0 0; padding: 1 2;
        border: round $primary-darken-2;
        border-title-color: $text-muted; border-title-style: bold;
    }
    #dash-right { width: 1fr; }
    #controls { height: auto; margin: 0 0 1 0; }
    #controls Button { margin: 0 2 0 0; }
    #feed, #maint-log {
        border: round $primary-darken-2; padding: 0 1; height: 1fr;
        background: $surface-darken-1;
        border-title-color: $text-muted; border-title-style: bold;
    }

    /* ---- Configure ---- */
    #config-scroll { height: 1fr; }
    .section {
        border: round $primary-darken-2; padding: 1 2; margin: 0 0 1 0;
        height: auto;
        border-title-color: $text-muted; border-title-style: bold;
    }
    .section:focus-within {
        border: round $accent;
        border-title-color: $accent;
    }
    .row { height: auto; margin: 0 0 1 0; }
    .field { width: 32; padding: 1 0 0 0; color: $text-muted; }
    .row Input { width: 48; }
    #save-bar {
        dock: bottom; height: auto; padding: 1 2;
        background: $panel; border-top: solid $primary-darken-2;
    }
    #save-bar Button { margin: 0 2 0 0; }
    #save-hint { padding: 0 0 0 1; color: $text-muted; }

    /* ---- Maintenance ---- */
    #maint-buttons { height: auto; margin: 0 0 1 0; }
    #maint-buttons Button { margin: 0 2 0 0; }
    #git-status {
        border: round $primary-darken-2; padding: 0 2; margin: 0 0 1 0;
        height: auto;
        border-title-color: $text-muted; border-title-style: bold;
    }
    """

    BINDINGS = [
        Binding("1", "show_tab('dashboard')", "Dashboard"),
        Binding("2", "show_tab('configure')", "Configure"),
        Binding("3", "show_tab('maintenance')", "Maintenance"),
        Binding("s", "start", "Start bot", show=False),
        Binding("x", "stop", "Stop bot", show=False),
        Binding("r", "restart", "Restart bot", show=False),
        Binding("ctrl+s", "save", "Save config", priority=True),
        Binding("c", "clear_feed", "Clear feed", show=False),
        Binding("escape", "unfocus", "Unfocus field", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        # Persistent components (survive bot restarts). Loaded non-strict so the
        # hub opens even before tokens are configured.
        self.config = Config.load(require_secrets=False)
        self.audit = AuditLogger(self.config.db_path)
        self.settings_store = GuildSettingsStore(self.config.db_path)
        self.ratelimiter = RateLimiter(self.config.rate_limit_max, self.config.rate_limit_window)

        # Bound the producer side of the live dashboard as well as its widgets.
        # Otherwise the default launch mode retains activity for process life.
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=1_000)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connected = False
        self._user = "—"
        self._guilds: list[str] = []

        self.controller = control.BotController(self._build_bot, on_state=self._on_bot_state)

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="dashboard"):
            with TabPane("Dashboard", id="dashboard"):
                with Horizontal():
                    yield StatusPanel(id="status")
                    with Vertical(id="dash-right"):
                        with Horizontal(id="controls"):
                            yield Button("Start", id="btn-start", variant="success")
                            yield Button("Stop", id="btn-stop", variant="error")
                            yield Button("Restart", id="btn-restart", variant="warning")
                        yield RichLog(
                            id="feed", markup=True, wrap=True, highlight=False, max_lines=500
                        )
            with TabPane("Configure", id="configure"):
                yield from self._compose_configure()
            with TabPane("Maintenance", id="maintenance"):
                yield from self._compose_maintenance()
        yield Footer()

    def _compose_configure(self) -> ComposeResult:
        with VerticalScroll(id="config-scroll"):
            with Vertical(classes="section", id="sec-secrets"):
                yield from self._field("Discord bot token", Input(password=True, id="cfg-discord_token"))
            with Vertical(classes="section", id="sec-llm"):
                yield from self._field(
                    "API endpoint (base URL)",
                    Input(placeholder="https://api.openai.com/v1", id="cfg-api_base_url"),
                )
                yield from self._field("API key", Input(password=True, id="cfg-api_key"))
                yield from self._field(
                    "Model",
                    Input(placeholder="gpt-4o-mini", id="cfg-model"),
                )
                yield from self._field("Max tokens", Input(id="cfg-max_tokens"))
                yield from self._field("Max agent iterations", Input(id="cfg-max_agent_iterations"))
            with Vertical(classes="section", id="sec-limits"):
                yield from self._field("Rate limit: max writes", Input(id="cfg-rate_limit_max"))
                yield from self._field("Rate limit: window (s)", Input(id="cfg-rate_limit_window"))
                yield from self._field("Bulk-confirm threshold", Input(id="cfg-bulk_confirm_threshold"))
                yield from self._field("Punitive tools (ban/kick/timeout)", Switch(id="cfg-enable_punitive"))
                yield from self._field("Cache all Discord members", Switch(id="cfg-cache_members"))
            with Vertical(classes="section", id="sec-update"):
                yield from self._field("Auto-update from GitHub", Switch(id="cfg-auto_update"))
                yield from self._field("Update check interval (min)", Input(id="cfg-auto_update_interval"))
                yield from self._field("Auto-restart after update", Switch(id="cfg-auto_restart"))
        with Horizontal(id="save-bar"):
            yield Button("Save to .env", id="btn-save", variant="primary")
            yield Button("Save & restart bot", id="btn-save-restart", variant="warning")
            yield Static("Ctrl+S saves from anywhere · blank secret fields keep their stored value", id="save-hint")

    def _compose_maintenance(self) -> ComposeResult:
        with Vertical():
            with Horizontal(id="maint-buttons"):
                yield Button("Install deps", id="btn-install", variant="primary")
                yield Button("Reinstall deps", id="btn-reinstall", variant="warning")
                yield Button("Check for updates", id="btn-check", variant="default")
                yield Button("Update & restart", id="btn-update", variant="success")
            yield Static("", id="git-status")
            yield RichLog(
                id="maint-log", markup=True, wrap=True, highlight=False, max_lines=2_000
            )

    def _field(self, label: str, widget) -> ComposeResult:
        yield Horizontal(Label(label, classes="field"), widget, classes="row")

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def on_mount(self) -> None:
        self.title = "AI Moderator — Control Hub"
        self.sub_title = TAB_TITLES["dashboard"]
        self._loop = asyncio.get_running_loop()

        # Panel titles.
        titles = {
            "status": "Overview",
            "feed": "Activity",
            "maint-log": "Output",
            "git-status": "Repository",
            "sec-secrets": "Secrets",
            "sec-llm": "LLM (OpenAI-compatible)",
            "sec-limits": "Limits & safety",
            "sec-update": "Auto-update",
        }
        for wid, title in titles.items():
            try:
                self.query_one(f"#{wid}").border_title = title
            except Exception:
                pass

        self._populate_config_form()
        self._refresh_status()

        feed = self.query_one("#feed", RichLog)
        feed.write("[dim]Hub ready. Recent audit history:[/]")
        for rec in reversed(self.audit.recent(15)):
            feed.write(self._format_record(rec))
        if self.config.missing_secrets():
            feed.write(
                "[yellow]No tokens set yet — press 2 to open Configure, fill them in, "
                "then Ctrl+S to save and press Start.[/]"
            )

        # Wire the audit log into the live feed (loop-safe).
        self.audit.subscribe(lambda rec: self._enqueue("audit", rec))

        self.run_worker(self._consume(), name="consume", exclusive=False)
        self.run_worker(self._auto_update_loop(), name="autoupdate", exclusive=False)
        self.run_worker(self._refresh_git_status(), name="gitstatus", exclusive=False)

        # Auto-start the bot if it's configured.
        if not self.config.missing_secrets():
            self.run_worker(self.controller.start(), group="bot", exclusive=True)

    async def on_unmount(self) -> None:
        try:
            await self.controller.stop()
        except Exception:
            pass

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        pane_id = getattr(event.pane, "id", None)
        if pane_id in TAB_TITLES:
            self.sub_title = TAB_TITLES[pane_id]

    # ------------------------------------------------------------------ #
    # Bot builder + hooks
    # ------------------------------------------------------------------ #

    def _build_bot(self):
        """Build a fresh, fully-configured bot. Raises SystemExit if unconfigured."""
        config = Config.load(require_secrets=True)  # re-reads .env
        self.config = config
        self.ratelimiter.max_actions = config.rate_limit_max
        self.ratelimiter.window = config.rate_limit_window
        agent = Agent(config)
        hooks = BotHooks(
            on_ready=lambda user, guilds: self._enqueue("ready", (str(user), [g.name for g in guilds])),
            on_status=lambda s: self._enqueue("status", s),
            on_message_seen=lambda s: self._enqueue("seen", s),
        )
        bot = create_bot(config, self.audit, self.settings_store, self.ratelimiter, agent, hooks)
        return bot, config.discord_token

    def _enqueue(self, kind: str, payload) -> None:
        if self._loop is not None:
            def put_bounded() -> None:
                if self._queue.full():
                    try:
                        self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                self._queue.put_nowait((kind, payload))

            self._loop.call_soon_threadsafe(put_bounded)

    def _on_bot_state(self, state: str, error: str) -> None:
        self._enqueue("botstate", (state, error))

    async def _consume(self) -> None:
        feed = self.query_one("#feed", RichLog)
        while True:
            kind, payload = await self._queue.get()
            if kind == "ready":
                self._user, self._guilds = payload
                self._connected = True
                self.controller.mark_ready()
                feed.write(f"[green]✔ Connected as {self._user} — {len(self._guilds)} guild(s).[/]")
                self._refresh_status()
            elif kind == "botstate":
                state, error = payload
                if state in (control.STOPPED, control.ERROR, control.RESTARTING):
                    self._connected = False
                feed.write(f"[dim]{self._ts()} · bot {state}[/]" + (f" [red]{error}[/]" if error else ""))
                self._refresh_status()
            elif kind == "status":
                feed.write(f"[dim]{self._ts()} · {payload}[/]")
            elif kind == "seen":
                feed.write(f"[cyan]{self._ts()} · request · {payload}[/]")
            elif kind == "audit":
                feed.write(self._format_record(payload))

    # ------------------------------------------------------------------ #
    # Button / action handlers
    # ------------------------------------------------------------------ #

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-start":
            self.action_start()
        elif bid == "btn-stop":
            self.action_stop()
        elif bid == "btn-restart":
            self.action_restart()
        elif bid == "btn-save":
            self._save_config(restart=False)
        elif bid == "btn-save-restart":
            self._save_config(restart=True)
        elif bid == "btn-install":
            self.run_worker(self._install(upgrade=True), group="maint", exclusive=True)
        elif bid == "btn-reinstall":
            self.run_worker(self._reinstall(), group="maint", exclusive=True)
        elif bid == "btn-check":
            self.run_worker(self._check_updates(), group="maint", exclusive=True)
        elif bid == "btn-update":
            self.run_worker(self._update_now(), group="maint", exclusive=True)

    def action_show_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab

    def action_start(self) -> None:
        if self.config.missing_secrets():
            self.notify("Set your tokens in the Configure tab first (press 2).", severity="warning")
            return
        self.run_worker(self.controller.start(), group="bot", exclusive=True)

    def action_stop(self) -> None:
        self.run_worker(self.controller.stop(), group="bot", exclusive=True)

    def action_restart(self) -> None:
        self.run_worker(self.controller.restart(), group="bot", exclusive=True)

    def action_save(self) -> None:
        self._save_config(restart=False)

    def action_unfocus(self) -> None:
        """Escape drops focus out of a field so 1/2/3 tab switching works again."""
        self.set_focus(None)

    def action_clear_feed(self) -> None:
        self.query_one("#feed", RichLog).clear()

    # ------------------------------------------------------------------ #
    # Config form
    # ------------------------------------------------------------------ #

    _INPUT_FIELDS = [
        "discord_token", "api_base_url", "api_key", "model", "max_tokens",
        "max_agent_iterations", "rate_limit_max", "rate_limit_window",
        "bulk_confirm_threshold", "auto_update_interval",
    ]
    _SWITCH_FIELDS = ["enable_punitive", "cache_members", "auto_update", "auto_restart"]
    # Masked secrets: never pre-fill the box from a live value, and never
    # overwrite the stored value when the box is left blank.
    _SECRET_FIELDS = ["discord_token", "api_key"]

    def _populate_config_form(self) -> None:
        c = self.config
        missing = c.missing_secrets()
        values = {
            "discord_token": "" if "DISCORD_TOKEN" in missing else c.discord_token,
            "api_base_url": c.api_base_url,
            "api_key": "" if "OPENAI_API_KEY" in missing else c.api_key,
            "model": c.model,
            "max_tokens": str(c.max_tokens),
            "max_agent_iterations": str(c.max_agent_iterations),
            "rate_limit_max": str(c.rate_limit_max),
            "rate_limit_window": str(c.rate_limit_window),
            "bulk_confirm_threshold": str(c.bulk_confirm_threshold),
            "auto_update_interval": str(c.auto_update_interval),
        }
        for field in self._INPUT_FIELDS:
            self.query_one(f"#cfg-{field}", Input).value = values[field]
        self.query_one("#cfg-enable_punitive", Switch).value = c.enable_punitive
        self.query_one("#cfg-cache_members", Switch).value = c.cache_members
        self.query_one("#cfg-auto_update", Switch).value = c.auto_update
        self.query_one("#cfg-auto_restart", Switch).value = c.auto_restart

    def _save_config(self, restart: bool) -> None:
        env_map = {
            "discord_token": "DISCORD_TOKEN",
            "api_base_url": "OPENAI_BASE_URL",
            "api_key": "OPENAI_API_KEY",
            "model": "OPENAI_MODEL",
            "max_tokens": "MAX_TOKENS",
            "max_agent_iterations": "MAX_AGENT_ITERATIONS",
            "rate_limit_max": "RATE_LIMIT_MAX",
            "rate_limit_window": "RATE_LIMIT_WINDOW",
            "bulk_confirm_threshold": "BULK_CONFIRM_THRESHOLD",
            "auto_update_interval": "AUTO_UPDATE_INTERVAL",
        }
        updates: dict[str, str] = {}
        for field, key in env_map.items():
            val = self.query_one(f"#cfg-{field}", Input).value.strip()
            if field in self._SECRET_FIELDS and not val:
                continue  # don't wipe a secret with an empty box
            updates[key] = val
        updates["ENABLE_PUNITIVE"] = str(self.query_one("#cfg-enable_punitive", Switch).value).lower()
        updates["CACHE_MEMBERS"] = str(self.query_one("#cfg-cache_members", Switch).value).lower()
        updates["AUTO_UPDATE"] = str(self.query_one("#cfg-auto_update", Switch).value).lower()
        updates["AUTO_RESTART"] = str(self.query_one("#cfg-auto_restart", Switch).value).lower()

        try:
            update_env_file(updates)
        except Exception as e:
            self.notify(f"Could not write .env: {e}", severity="error")
            return

        self.config = Config.load(require_secrets=False)
        self.ratelimiter.max_actions = self.config.rate_limit_max
        self.ratelimiter.window = self.config.rate_limit_window
        self._refresh_status()
        self.notify("Saved to .env.", severity="information")
        if restart:
            self.run_worker(self.controller.restart(), group="bot", exclusive=True)
        elif self.controller.active:
            self.notify("Restart the bot to apply the new settings.", severity="warning")

    # ------------------------------------------------------------------ #
    # Maintenance workers
    # ------------------------------------------------------------------ #

    def _maint_log(self, line: str) -> None:
        try:
            self.query_one("#maint-log", RichLog).write(line)
        except Exception:
            pass

    async def _install(self, upgrade: bool) -> None:
        self._maint_log("[b]Installing dependencies…[/]")
        res = await maintenance.install_dependencies(self._maint_log, upgrade=upgrade)
        self._maint_log("[green]✔ Dependencies installed.[/]" if res.ok else "[red]✖ Install failed.[/]")

    async def _reinstall(self) -> None:
        self._maint_log("[b]Force-reinstalling dependencies…[/]")
        res = await maintenance.reinstall_dependencies(self._maint_log)
        self._maint_log("[green]✔ Reinstall complete.[/]" if res.ok else "[red]✖ Reinstall failed.[/]")

    async def _check_updates(self) -> None:
        self._maint_log("[b]Checking GitHub for updates…[/]")
        status = await maintenance.check_for_updates(self._maint_log)
        await self._refresh_git_status(status)
        if status.error:
            self._maint_log(f"[yellow]{status.error}[/]")
        elif status.update_available:
            self._maint_log(f"[green]Update available: {status.behind} commit(s) behind {status.branch}.[/]")
        else:
            self._maint_log("[dim]Already up to date.[/]")

    async def _update_now(self) -> None:
        self._maint_log("[b]Pulling latest and reinstalling…[/]")
        pulled, deps = await maintenance.pull_and_install(self._maint_log)
        if not pulled:
            self._maint_log("[red]✖ Pull failed (not fast-forward, or no upstream).[/]")
            return
        self._maint_log("[green]✔ Updated.[/]" + ("" if deps else " [yellow](dependency install had issues)[/]"))
        await self._refresh_git_status()
        if self.controller.active:
            self._maint_log("[b]Restarting bot to apply update…[/]")
            await self.controller.restart()

    async def _auto_update_loop(self) -> None:
        # Wait a bit before the first check so startup logs stay readable.
        await asyncio.sleep(60)
        while True:
            interval = max(5, self.config.auto_update_interval)
            if self.config.auto_update:
                status = await maintenance.check_for_updates(self._maint_log)
                if status.update_available:
                    self._maint_log(
                        f"[b]Auto-update:[/] {status.behind} commit(s) behind — pulling…"
                    )
                    pulled, _ = await maintenance.pull_and_install(self._maint_log)
                    if pulled and self.config.auto_restart and self.controller.active:
                        self._maint_log("[b]Auto-restarting bot…[/]")
                        await self.controller.restart()
            await asyncio.sleep(interval * 60)

    async def _refresh_git_status(self, status: "maintenance.UpdateStatus | None" = None) -> None:
        try:
            widget = self.query_one("#git-status", Static)
        except Exception:
            return
        if status is None:
            branch = await maintenance.git_current_branch()
            local = (await maintenance.run(["git", "rev-parse", "--short", "HEAD"])).output.strip()
            widget.update(f"Branch [b]{branch or '—'}[/]   Commit [b]{local or '—'}[/]")
            return
        if status.error:
            widget.update(f"Branch [b]{status.branch or '—'}[/]   [yellow]{status.error}[/]")
        else:
            state = (
                f"[yellow]{status.behind} behind[/]" if status.update_available else "[green]up to date[/]"
            )
            widget.update(
                f"Branch [b]{status.branch}[/]   Local [b]{status.local_rev}[/]   "
                f"Remote [b]{status.remote_rev}[/]   {state}"
            )

    # ------------------------------------------------------------------ #
    # Status panel
    # ------------------------------------------------------------------ #

    def _refresh_status(self) -> None:
        self.query_one(StatusPanel).render_state(
            state=self.controller.state,
            error=self.controller.last_error,
            connected=self._connected,
            user=self._user,
            config=self.config,
            guilds=self._guilds,
        )

    @staticmethod
    def _ts() -> str:
        return datetime.now().strftime("%H:%M:%S")

    def _format_record(self, rec: AuditRecord) -> str:
        tag = "[green]✔ executed[/]" if rec.allowed else "[red]✖ refused [/]"
        return (
            f"{tag} [b]{rec.action}[/b] · {rec.requester_name} "
            f"[dim]({rec.requester_id})[/] · {rec.guild_name}\n    [dim]{rec.outcome}[/]"
        )


class StatusPanel(Static):
    def render_state(self, *, state, error, connected, user, config: Config, guilds) -> None:
        dot = STATE_STYLE.get(state, state)
        secrets = config.missing_secrets()
        if config.auto_update:
            auto = f"on · every {config.auto_update_interval}m"
            if config.auto_restart:
                auto += " · auto-restart"
        else:
            auto = "[dim]off[/]"
        lines = [
            "[b]Bot[/b]",
            f"  State       {dot}",
            f"  Connection  {'[green]connected[/]' if connected else '[dim]offline[/]'}",
            f"  Account     {user}",
            f"  Guilds      {len(guilds)}",
            "",
            "[b]Configuration[/b]",
            f"  Model       {config.model or '[dim]not set[/]'}",
            f"  Rate limit  {config.rate_limit_max} writes / {config.rate_limit_window}s",
            f"  Punitive    {'[yellow]on[/] (typed CONFIRM)' if config.enable_punitive else '[dim]off[/]'}",
            f"  Members     {'[yellow]full cache[/]' if config.cache_members else '[dim]low-memory[/]'}",
            f"  Updates     {auto}",
        ]
        if secrets:
            lines += ["", f"[yellow]⚠ Missing: {', '.join(secrets)}[/]"]
        if error:
            lines += ["", f"[red]{error[:120]}[/]"]
        if guilds:
            lines += ["", "[b]Servers[/b]"] + [f"  • {g}" for g in guilds[:8]]
        self.update("\n".join(lines))


def run_tui() -> None:
    ModeratorHub().run()
