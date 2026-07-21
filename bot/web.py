"""Local web control hub with feature parity with the Textual dashboard."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from aiohttp import web

from . import control, maintenance
from .ai import Agent
from .audit import AuditLogger
from .config import Config, GuildSettingsStore, update_env_file
from .main import BotHooks, create_bot
from .ratelimit import RateLimiter

STATIC = Path(__file__).with_name("web_static")


class WebHub:
    """Owns the bot lifecycle and exposes a deliberately localhost-first API."""

    def __init__(self):
        self.config = Config.load(require_secrets=False)
        self.audit = AuditLogger(self.config.db_path)
        self.settings = GuildSettingsStore(self.config.db_path)
        self.ratelimiter = RateLimiter(self.config.rate_limit_max, self.config.rate_limit_window)
        self.user = "—"
        self.guilds: list[str] = []
        self.messages: list[str] = ["Web control hub ready."]
        self.controller = control.BotController(self._build_bot, on_state=self._state)
        self.audit.subscribe(lambda _: None)

    def _log(self, message: str) -> None:
        self.messages.append(message)
        self.messages = self.messages[-100:]

    def _state(self, state: str, error: str) -> None:
        self._log(f"Bot {state}" + (f": {error}" if error else ""))

    def _ready(self, user, guilds) -> None:
        self.user, self.guilds = str(user), [g.name for g in guilds]
        self.controller.mark_ready()

    def _build_bot(self):
        self.config = Config.load(require_secrets=True)
        self.ratelimiter.max_actions = self.config.rate_limit_max
        self.ratelimiter.window = self.config.rate_limit_window
        hooks = BotHooks(on_ready=self._ready, on_status=self._log, on_message_seen=self._log)
        return create_bot(self.config, self.audit, self.settings, self.ratelimiter, Agent(self.config), hooks), self.config.discord_token

    async def status(self, request):
        c = self.config
        return web.json_response({
            "state": self.controller.state, "error": self.controller.last_error,
            "user": self.user, "guilds": self.guilds, "missing": c.missing_secrets(),
            "config": {"api_base_url": c.api_base_url, "model": c.model, "max_tokens": c.max_tokens,
                       "max_agent_iterations": c.max_agent_iterations, "rate_limit_max": c.rate_limit_max,
                       "rate_limit_window": c.rate_limit_window, "bulk_confirm_threshold": c.bulk_confirm_threshold,
                       "enable_punitive": c.enable_punitive, "auto_update": c.auto_update,
                       "auto_update_interval": c.auto_update_interval, "auto_restart": c.auto_restart},
            "activity": self.messages[-20:], "audit": [asdict(x) for x in self.audit.recent(30)],
        })

    async def lifecycle(self, request):
        action = request.match_info["action"]
        fn = {"start": self.controller.start, "stop": self.controller.stop, "restart": self.controller.restart}.get(action)
        if fn is None:
            raise web.HTTPNotFound()
        await fn()
        return web.json_response({"ok": True, "state": self.controller.state})

    async def save_config(self, request):
        data = await request.json()
        names = {"api_base_url": "OPENAI_BASE_URL", "model": "OPENAI_MODEL", "max_tokens": "MAX_TOKENS",
                 "max_agent_iterations": "MAX_AGENT_ITERATIONS", "rate_limit_max": "RATE_LIMIT_MAX",
                 "rate_limit_window": "RATE_LIMIT_WINDOW", "bulk_confirm_threshold": "BULK_CONFIRM_THRESHOLD",
                 "enable_punitive": "ENABLE_PUNITIVE", "auto_update": "AUTO_UPDATE",
                 "auto_update_interval": "AUTO_UPDATE_INTERVAL", "auto_restart": "AUTO_RESTART",
                 "discord_token": "DISCORD_TOKEN", "api_key": "OPENAI_API_KEY"}
        updates = {env: str(data[key]).lower() if isinstance(data[key], bool) else str(data[key])
                   for key, env in names.items() if key in data and data[key] not in (None, "")}
        update_env_file(updates)
        self.config = Config.load(require_secrets=False)
        if data.get("restart"):
            await self.controller.restart()
        return web.json_response({"ok": True})

    async def maintain(self, request):
        action = request.match_info["action"]
        lines = []
        if action == "check":
            result = await maintenance.check_for_updates(lines.append)
            payload = asdict(result)
        elif action in ("install", "reinstall"):
            result = await (maintenance.reinstall_dependencies(lines.append) if action == "reinstall" else maintenance.install_dependencies(lines.append, upgrade=True))
            payload = asdict(result)
        elif action == "update":
            pulled, deps = await maintenance.pull_and_install(lines.append)
            payload = {"ok": pulled, "dependencies": deps}
            if pulled and self.controller.active:
                await self.controller.restart()
        else:
            raise web.HTTPNotFound()
        self.messages.extend(lines[-30:])
        return web.json_response({"result": payload, "output": lines})

    async def shutdown(self, app):
        await self.controller.stop()

    def app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024)
        app.add_routes([
            web.get("/", lambda _: web.FileResponse(STATIC / "index.html")),
            web.get("/api/status", self.status), web.post("/api/bot/{action}", self.lifecycle),
            web.post("/api/config", self.save_config), web.post("/api/maintenance/{action}", self.maintain),
            web.static("/static", STATIC),
        ])
        app.on_shutdown.append(self.shutdown)
        return app


def run_web(host: str = "127.0.0.1", port: int = 8765) -> None:
    web.run_app(WebHub().app(), host=host, port=port, print=lambda s: print(s))
