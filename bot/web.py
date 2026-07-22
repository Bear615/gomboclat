"""Browser control hub with feature parity with the Textual dashboard.

The server deliberately binds to loopback by default.  It owns the same bot
controller, configuration, audit logger, and maintenance helpers as the TUI;
the browser is only a presentation layer over those objects.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import web

from . import control, maintenance
from .ai import Agent
from .audit import AuditLogger, AuditRecord
from .config import Config, GuildSettingsStore, update_env_file
from .main import BotHooks, create_bot
from .ratelimit import RateLimiter

STATIC = Path(__file__).with_name("web_static")
CONFIG_FIELDS = {
    "api_base_url": "OPENAI_BASE_URL",
    "model": "OPENAI_MODEL",
    "max_tokens": "MAX_TOKENS",
    "max_agent_iterations": "MAX_AGENT_ITERATIONS",
    "rate_limit_max": "RATE_LIMIT_MAX",
    "rate_limit_window": "RATE_LIMIT_WINDOW",
    "bulk_confirm_threshold": "BULK_CONFIRM_THRESHOLD",
    "enable_punitive": "ENABLE_PUNITIVE",
    "auto_update": "AUTO_UPDATE",
    "auto_update_interval": "AUTO_UPDATE_INTERVAL",
    "auto_restart": "AUTO_RESTART",
    "discord_token": "DISCORD_TOKEN",
    "api_key": "OPENAI_API_KEY",
}
INTEGER_FIELDS = {
    "max_tokens": 1,
    "max_agent_iterations": 1,
    "rate_limit_max": 1,
    "rate_limit_window": 1,
    "bulk_confirm_threshold": 1,
    "auto_update_interval": 1,
}


class WebHub:
    """Own the bot lifecycle and expose it through a localhost-first API."""

    def __init__(self) -> None:
        self.config = Config.load(require_secrets=False)
        self.audit = AuditLogger(self.config.db_path)
        self.settings = GuildSettingsStore(self.config.db_path)
        self.ratelimiter = RateLimiter(self.config.rate_limit_max, self.config.rate_limit_window)
        self.user = "—"
        self.guilds: list[str] = []
        self.connected = False
        self.messages: list[dict[str, str]] = []
        self.repository: dict[str, Any] = {"branch": "—", "commit": "—", "state": "Not checked"}
        self.controller = control.BotController(self._build_bot, on_state=self._state)
        self._maintenance_lock = asyncio.Lock()
        self._background_tasks: set[asyncio.Task] = set()
        self._log("system", "Web control hub ready.")
        self.audit.subscribe(self._audit_event)

    def _log(self, kind: str, message: str) -> None:
        self.messages.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "message": str(message),
        })
        self.messages = self.messages[-100:]

    def _audit_event(self, record: AuditRecord) -> None:
        verdict = "Executed" if record.allowed else "Refused"
        self._log("audit", f"{verdict}: {record.action} requested by {record.requester_name}")

    def _state(self, state: str, error: str) -> None:
        if state in (control.STOPPED, control.ERROR):
            self.connected = False
            self.user, self.guilds = "—", []
        self._log("error" if error else "lifecycle", f"Bot {state}" + (f": {error}" if error else ""))

    def _ready(self, user: Any, guilds: list[Any]) -> None:
        self.user, self.guilds = str(user), [g.name for g in guilds]
        self.connected = True
        self.controller.mark_ready()

    def _build_bot(self):
        self.config = Config.load(require_secrets=True)
        self.ratelimiter.max_actions = self.config.rate_limit_max
        self.ratelimiter.window = self.config.rate_limit_window
        hooks = BotHooks(
            on_ready=self._ready,
            on_status=lambda message: self._log("discord", message),
            on_message_seen=lambda message: self._log("request", message),
        )
        bot = create_bot(
            self.config, self.audit, self.settings, self.ratelimiter, Agent(self.config), hooks
        )
        return bot, self.config.discord_token

    @staticmethod
    def _json_error(message: str, status: int = 400) -> web.Response:
        return web.json_response({"ok": False, "error": message}, status=status)

    async def status(self, request: web.Request) -> web.Response:
        c = self.config
        return web.json_response({
            "state": self.controller.state,
            "error": self.controller.last_error,
            "connected": self.connected,
            "user": self.user,
            "guilds": self.guilds,
            "missing": c.missing_secrets(),
            "config": {
                "api_base_url": c.api_base_url,
                "model": c.model,
                "max_tokens": c.max_tokens,
                "max_agent_iterations": c.max_agent_iterations,
                "rate_limit_max": c.rate_limit_max,
                "rate_limit_window": c.rate_limit_window,
                "bulk_confirm_threshold": c.bulk_confirm_threshold,
                "enable_punitive": c.enable_punitive,
                "auto_update": c.auto_update,
                "auto_update_interval": c.auto_update_interval,
                "auto_restart": c.auto_restart,
            },
            "activity": self.messages[-30:],
            "audit": [asdict(item) for item in self.audit.recent(30)],
            "repository": self.repository,
            "maintenance_busy": self._maintenance_lock.locked(),
        })

    async def lifecycle(self, request: web.Request) -> web.Response:
        action = request.match_info["action"]
        fn: Callable[[], Awaitable[None]] | None = {
            "start": self.controller.start,
            "stop": self.controller.stop,
            "restart": self.controller.restart,
        }.get(action)
        if fn is None:
            raise web.HTTPNotFound()
        if action in ("start", "restart") and self.config.missing_secrets():
            return self._json_error("Configure the Discord token and API key before starting.")
        await fn()
        return web.json_response({"ok": True, "state": self.controller.state})

    async def clear_activity(self, request: web.Request) -> web.Response:
        self.messages.clear()
        self._log("system", "Runtime feed cleared.")
        return web.json_response({"ok": True})

    async def save_config(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return self._json_error("Expected a JSON request body.")
        if not isinstance(data, dict):
            return self._json_error("Configuration must be a JSON object.")

        updates: dict[str, str] = {}
        for key, env_name in CONFIG_FIELDS.items():
            if key not in data or data[key] in (None, ""):
                continue
            value = data[key]
            if key in INTEGER_FIELDS:
                if isinstance(value, bool):
                    return self._json_error(f"{key} must be a positive integer.")
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    return self._json_error(f"{key} must be a positive integer.")
                if value < INTEGER_FIELDS[key]:
                    return self._json_error(f"{key} must be at least {INTEGER_FIELDS[key]}.")
            if key in {"enable_punitive", "auto_update", "auto_restart"}:
                if not isinstance(value, bool):
                    return self._json_error(f"{key} must be true or false.")
                value = str(value).lower()
            updates[env_name] = str(value).strip()

        try:
            update_env_file(updates)
            self.config = Config.load(require_secrets=False)
        except OSError as exc:
            return self._json_error(f"Could not write .env: {exc}", 500)
        self.ratelimiter.max_actions = self.config.rate_limit_max
        self.ratelimiter.window = self.config.rate_limit_window
        self._log("configuration", "Configuration saved to .env.")
        if data.get("restart"):
            if self.config.missing_secrets():
                return self._json_error("Saved, but required secrets are still missing.")
            await self.controller.restart()
        return web.json_response({"ok": True, "restart_required": self.controller.active and not data.get("restart")})

    async def maintain(self, request: web.Request) -> web.Response:
        action = request.match_info["action"]
        if action not in {"check", "install", "reinstall", "update"}:
            raise web.HTTPNotFound()
        if self._maintenance_lock.locked():
            return self._json_error("Another maintenance operation is already running.", 409)

        lines: list[str] = []
        sink = lambda line: (lines.append(line), self._log("maintenance", line))
        async with self._maintenance_lock:
            if action == "check":
                result = await maintenance.check_for_updates(sink)
                payload = asdict(result)
                self._set_repository(result)
            elif action in ("install", "reinstall"):
                result = await (
                    maintenance.reinstall_dependencies(sink)
                    if action == "reinstall"
                    else maintenance.install_dependencies(sink, upgrade=True)
                )
                payload = asdict(result) | {"ok": result.ok}
            else:
                pulled, deps = await maintenance.pull_and_install(sink)
                payload = {"ok": pulled, "dependencies": deps}
                if pulled:
                    await self._refresh_repository()
                    if self.controller.active:
                        await self.controller.restart()
        return web.json_response({"ok": bool(payload.get("ok", True)), "result": payload, "output": lines})

    def _set_repository(self, status: maintenance.UpdateStatus) -> None:
        if status.error:
            state = status.error
        elif status.update_available:
            state = f"{status.behind} behind"
        else:
            state = "Up to date"
        self.repository = {
            "branch": status.branch or "—",
            "commit": status.local_rev or "—",
            "remote": status.remote_rev or "—",
            "ahead": status.ahead,
            "behind": status.behind,
            "state": state,
        }

    async def _refresh_repository(self) -> None:
        branch, rev = await asyncio.gather(
            maintenance.git_current_branch(),
            maintenance.run(["git", "rev-parse", "--short", "HEAD"]),
        )
        self.repository.update(branch=branch or "—", commit=rev.output.strip() or "—")

    async def _auto_update_loop(self) -> None:
        await asyncio.sleep(60)
        while True:
            interval = max(5, self.config.auto_update_interval)
            if self.config.auto_update and not self._maintenance_lock.locked():
                async with self._maintenance_lock:
                    status = await maintenance.check_for_updates(
                        lambda line: self._log("maintenance", line)
                    )
                    self._set_repository(status)
                    if status.update_available:
                        self._log("maintenance", f"Auto-update: {status.behind} commit(s) behind; pulling.")
                        pulled, _ = await maintenance.pull_and_install(
                            lambda line: self._log("maintenance", line)
                        )
                        if pulled and self.config.auto_restart and self.controller.active:
                            await self.controller.restart()
            await asyncio.sleep(interval * 60)

    async def startup(self, app: web.Application) -> None:
        await self._refresh_repository()
        task = asyncio.create_task(self._auto_update_loop(), name="web-auto-update")
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        if not self.config.missing_secrets():
            await self.controller.start()

    async def shutdown(self, app: web.Application) -> None:
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        await self.controller.stop()

    async def index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC / "index.html")

    def app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024)
        app.add_routes([
            web.get("/", self.index),
            web.get("/api/status", self.status),
            web.post("/api/bot/{action}", self.lifecycle),
            web.post("/api/config", self.save_config),
            web.post("/api/activity/clear", self.clear_activity),
            web.post("/api/maintenance/{action}", self.maintain),
            web.static("/static", STATIC),
        ])
        app.on_startup.append(self.startup)
        app.on_shutdown.append(self.shutdown)
        return app


def run_web(host: str = "127.0.0.1", port: int = 8765) -> None:
    web.run_app(WebHub().app(), host=host, port=port, print=print)
