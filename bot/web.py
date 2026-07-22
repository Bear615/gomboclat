"""Browser control hub with the same control surface as the Textual TUI.

The web process and the TUI deliberately share ``bot.config`` and the same
repository-root ``.env`` file. Secrets are write-only in API responses: the
browser receives only a saved/missing flag and a blank value preserves the
stored secret. Runtime, audit, repository, and maintenance events are delivered
over server-sent events with polling kept as a fallback.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from aiohttp import web

from . import control, maintenance
from .ai import Agent
from .audit import AuditLogger, AuditRecord
from .config import (
    Config,
    GuildSettingsStore,
    env_revision,
    update_env_file,
)
from .main import BotHooks, create_bot
from .ratelimit import RateLimiter

STATIC = Path(__file__).with_name("web_static")

CONFIG_FIELDS = {
    "discord_token": "DISCORD_TOKEN",
    "api_base_url": "OPENAI_BASE_URL",
    "api_key": "OPENAI_API_KEY",
    "model": "OPENAI_MODEL",
    "max_tokens": "MAX_TOKENS",
    "max_agent_iterations": "MAX_AGENT_ITERATIONS",
    "rate_limit_max": "RATE_LIMIT_MAX",
    "rate_limit_window": "RATE_LIMIT_WINDOW",
    "bulk_confirm_threshold": "BULK_CONFIRM_THRESHOLD",
    "enable_punitive": "ENABLE_PUNITIVE",
    "cache_members": "CACHE_MEMBERS",
    "auto_update": "AUTO_UPDATE",
    "auto_update_interval": "AUTO_UPDATE_INTERVAL",
    "auto_restart": "AUTO_RESTART",
}
INTEGER_FIELDS = {
    "max_tokens": 1,
    "max_agent_iterations": 1,
    "rate_limit_max": 1,
    "rate_limit_window": 1,
    "bulk_confirm_threshold": 1,
    "auto_update_interval": 1,
}
BOOLEAN_FIELDS = {"enable_punitive", "cache_members", "auto_update", "auto_restart"}
SECRET_FIELDS = {"discord_token", "api_key"}

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    # Prevent an old provider-specific frontend surviving a deployment in cache.
    "Cache-Control": "no-store, max-age=0",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def config_snapshot(config: Config) -> dict[str, Any]:
    """Return every non-secret setting editable from the control hubs."""
    return {
        "api_base_url": config.api_base_url,
        "model": config.model,
        "max_tokens": config.max_tokens,
        "max_agent_iterations": config.max_agent_iterations,
        "rate_limit_max": config.rate_limit_max,
        "rate_limit_window": config.rate_limit_window,
        "bulk_confirm_threshold": config.bulk_confirm_threshold,
        "enable_punitive": config.enable_punitive,
        "cache_members": config.cache_members,
        "auto_update": config.auto_update,
        "auto_update_interval": config.auto_update_interval,
        "auto_restart": config.auto_restart,
    }


def secret_snapshot(config: Config) -> dict[str, bool]:
    missing = set(config.missing_secrets())
    return {
        "discord_token": "DISCORD_TOKEN" not in missing,
        "api_key": "OPENAI_API_KEY" not in missing,
    }


def validate_config_payload(data: object) -> dict[str, str]:
    """Validate a browser config payload and translate it to environment keys."""
    if not isinstance(data, dict):
        raise ValueError("Configuration must be a JSON object.")

    unknown = set(data) - set(CONFIG_FIELDS) - {"restart"}
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"Unknown configuration field(s): {names}.")
    if "restart" in data and not isinstance(data["restart"], bool):
        raise ValueError("restart must be true or false.")

    updates: dict[str, str] = {}
    for key, env_name in CONFIG_FIELDS.items():
        if key not in data:
            continue
        value = data[key]

        # Secret controls are intentionally blank after every load. Blank means
        # keep the existing value; it never erases a working credential.
        if key in SECRET_FIELDS and value in (None, ""):
            continue

        if key in INTEGER_FIELDS:
            if isinstance(value, bool):
                raise ValueError(f"{key} must be a positive integer.")
            try:
                number = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} must be a positive integer.") from exc
            minimum = INTEGER_FIELDS[key]
            if number < minimum:
                raise ValueError(f"{key} must be at least {minimum}.")
            value = str(number)
        elif key in BOOLEAN_FIELDS:
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true or false.")
            value = str(value).lower()
        else:
            if not isinstance(value, str):
                raise ValueError(f"{key} must be text.")
            value = value.strip()
            if not value:
                raise ValueError(f"{key} cannot be empty.")
            if "\n" in value or "\r" in value:
                raise ValueError(f"{key} must be a single line.")

        if key == "api_base_url":
            parsed = urlparse(str(value))
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("api_base_url must be a complete http:// or https:// URL.")

        updates[env_name] = str(value)
    return updates


class EventBroker:
    """Fan out bounded live events without allowing slow browsers to grow RAM."""

    def __init__(self, queue_size: int = 256) -> None:
        self._queues: set[asyncio.Queue[tuple[str, Any]]] = set()
        self._queue_size = queue_size

    def subscribe(self) -> asyncio.Queue[tuple[str, Any]]:
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[tuple[str, Any]]) -> None:
        self._queues.discard(queue)

    def publish(self, event: str, payload: Any) -> None:
        for queue in tuple(self._queues):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait((event, payload))


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
        self.activity: deque[dict[str, Any]] = deque(maxlen=500)
        self.maintenance_output: deque[dict[str, Any]] = deque(maxlen=2_000)
        self.repository: dict[str, Any] = {
            "branch": "—",
            "commit": "—",
            "remote": "—",
            "ahead": 0,
            "behind": 0,
            "state": "Not checked",
        }
        self.maintenance_state: dict[str, Any] = {
            "busy": False,
            "action": None,
            "ok": None,
            "started_at": None,
            "finished_at": None,
        }

        self.controller = control.BotController(self._build_bot, on_state=self._state)
        self.events = EventBroker()
        self._event_id = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._maintenance_lock = asyncio.Lock()
        self._config_lock = asyncio.Lock()
        self._background_tasks: set[asyncio.Task[Any]] = set()

        self._log("system", "Web control hub ready.")
        self.audit.subscribe(self._audit_event)

    # ------------------------------------------------------------------ #
    # Event and state plumbing
    # ------------------------------------------------------------------ #

    def _publish(self, event: str, payload: Any) -> None:
        if self._loop is None or not self._loop.is_running():
            return
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is self._loop:
            self.events.publish(event, payload)
        else:
            self._loop.call_soon_threadsafe(self.events.publish, event, payload)

    def _append(self, target: deque[dict[str, Any]], kind: str, message: str) -> dict[str, Any]:
        self._event_id += 1
        item = {
            "id": self._event_id,
            "time": _now_iso(),
            "kind": kind,
            "message": str(message),
        }
        target.append(item)
        return item

    def _log(self, kind: str, message: str) -> None:
        item = self._append(self.activity, kind, message)
        self._publish("activity", item)

    def _maintenance_log(self, kind: str, message: str) -> None:
        item = self._append(self.maintenance_output, kind, message)
        self._publish("maintenance", item)

    def _audit_event(self, record: AuditRecord) -> None:
        verdict = "Executed" if record.allowed else "Refused"
        self._log("audit", f"{verdict}: {record.action} requested by {record.requester_name}")
        self._publish("audit", asdict(record))

    def _state(self, state: str, error: str) -> None:
        if state in (control.STOPPED, control.ERROR, control.RESTARTING):
            self.connected = False
            if state in (control.STOPPED, control.ERROR):
                self.user, self.guilds = "—", []
        self._log("error" if error else "lifecycle", f"Bot {state}" + (f": {error}" if error else ""))
        self._publish("status", {"state": state, "error": error})

    def _ready(self, user: Any, guilds: list[Any]) -> None:
        self.user, self.guilds = str(user), [g.name for g in guilds]
        self.connected = True
        self.controller.mark_ready()
        self._log("discord", f"Connected as {self.user} — {len(self.guilds)} server(s).")
        self._publish("status", {"state": self.controller.state, "connected": True})

    def _reload_saved_config(self) -> None:
        """Pick up changes saved by the TUI or a manual .env edit."""
        self.config = Config.load(require_secrets=False)
        self.ratelimiter.max_actions = self.config.rate_limit_max
        self.ratelimiter.window = self.config.rate_limit_window

    # ------------------------------------------------------------------ #
    # Bot builder
    # ------------------------------------------------------------------ #

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

    # ------------------------------------------------------------------ #
    # API helpers and handlers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _json_error(message: str, status: int = 400, **extra: Any) -> web.Response:
        return web.json_response({"ok": False, "error": message, **extra}, status=status)

    def _status_payload(self) -> dict[str, Any]:
        self._reload_saved_config()
        return {
            "ok": True,
            "generated_at": _now_iso(),
            "state": self.controller.state,
            "error": self.controller.last_error,
            "connected": self.connected,
            "user": self.user,
            "guilds": self.guilds,
            "missing": self.config.missing_secrets(),
            "secrets": secret_snapshot(self.config),
            "config": config_snapshot(self.config),
            "config_revision": env_revision(),
            "activity": list(self.activity)[-100:],
            "audit": [asdict(item) for item in self.audit.recent(50)],
            "repository": self.repository,
            "maintenance": {
                **self.maintenance_state,
                "busy": self._maintenance_lock.locked(),
                "output": list(self.maintenance_output)[-300:],
            },
        }

    async def status(self, request: web.Request) -> web.Response:
        return web.json_response(self._status_payload())

    async def lifecycle(self, request: web.Request) -> web.Response:
        action = request.match_info["action"]
        fn: Callable[[], Awaitable[None]] | None = {
            "start": self.controller.start,
            "stop": self.controller.stop,
            "restart": self.controller.restart,
        }.get(action)
        if fn is None:
            raise web.HTTPNotFound()

        async with self._lifecycle_lock:
            self._reload_saved_config()
            if action in ("start", "restart") and self.config.missing_secrets():
                return self._json_error(
                    "Configure the Discord token and OpenAI-compatible API key before starting."
                )
            await fn()
        return web.json_response({"ok": True, "state": self.controller.state})

    async def clear_activity(self, request: web.Request) -> web.Response:
        self.activity.clear()
        self._log("system", "Runtime feed cleared.")
        return web.json_response({"ok": True})

    async def save_config(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return self._json_error("Expected a JSON request body.")

        try:
            updates = validate_config_payload(data)
        except ValueError as exc:
            return self._json_error(str(exc))

        async with self._config_lock:
            try:
                update_env_file(updates)
                self._reload_saved_config()
            except OSError as exc:
                return self._json_error(f"Could not write .env: {exc}", 500)

        self._log("configuration", "Configuration saved to the shared .env file.")
        self._publish("config", {"revision": env_revision()})

        restart_requested = bool(isinstance(data, dict) and data.get("restart"))
        restarted = False
        warning = ""
        if restart_requested:
            if self.config.missing_secrets():
                warning = "Saved, but required secrets are still missing, so the bot was not restarted."
            else:
                async with self._lifecycle_lock:
                    await self.controller.restart()
                restarted = True

        return web.json_response({
            "ok": True,
            "config": config_snapshot(self.config),
            "secrets": secret_snapshot(self.config),
            "config_revision": env_revision(),
            "restart_required": self.controller.active and not restarted,
            "restarted": restarted,
            "warning": warning,
        })

    async def events_stream(self, request: web.Request) -> web.StreamResponse:
        queue = self.events.subscribe()
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache, no-store",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)
        await response.write(b"retry: 2500\n\n")
        try:
            while True:
                try:
                    event, payload = await asyncio.wait_for(queue.get(), timeout=20)
                    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                    await response.write(f"event: {event}\ndata: {encoded}\n\n".encode())
                except asyncio.TimeoutError:
                    await response.write(b": heartbeat\n\n")
        except (ConnectionResetError, RuntimeError):
            pass
        except asyncio.CancelledError:
            raise
        finally:
            self.events.unsubscribe(queue)
        return response

    # ------------------------------------------------------------------ #
    # Maintenance
    # ------------------------------------------------------------------ #

    async def maintain(self, request: web.Request) -> web.Response:
        action = request.match_info["action"]
        if action not in {"check", "install", "reinstall", "update"}:
            raise web.HTTPNotFound()
        if self._maintenance_lock.locked():
            return self._json_error("Another maintenance operation is already running.", 409)

        start_id = self._event_id
        payload: dict[str, Any] = {}
        ok = False
        async with self._maintenance_lock:
            self.maintenance_state = {
                "busy": True,
                "action": action,
                "ok": None,
                "started_at": _now_iso(),
                "finished_at": None,
            }
            self._maintenance_log("operation", f"Starting {action} operation.")
            self._publish("maintenance_state", self.maintenance_state)

            def sink(line: str) -> None:
                self._maintenance_log("line", line)

            try:
                if action == "check":
                    result = await maintenance.check_for_updates(sink)
                    self._set_repository(result)
                    payload = asdict(result)
                    ok = not bool(result.error)
                elif action == "install":
                    result = await maintenance.install_dependencies(sink, upgrade=True)
                    payload = asdict(result)
                    ok = result.ok
                elif action == "reinstall":
                    result = await maintenance.reinstall_dependencies(sink)
                    payload = asdict(result)
                    ok = result.ok
                else:
                    pulled, dependencies = await maintenance.pull_and_install(sink)
                    payload = {"pulled": pulled, "dependencies": dependencies}
                    ok = pulled and dependencies
                    if pulled:
                        await self._refresh_repository()
                        if self.controller.active:
                            self._maintenance_log("operation", "Restarting the bot to apply the update.")
                            async with self._lifecycle_lock:
                                await self.controller.restart()
            except Exception as exc:
                payload = {"error": str(exc)}
                self._maintenance_log("error", f"Operation failed: {exc}")
                ok = False
            finally:
                self.maintenance_state = {
                    **self.maintenance_state,
                    "busy": False,
                    "ok": ok,
                    "finished_at": _now_iso(),
                }
                self._maintenance_log(
                    "success" if ok else "error",
                    "Operation complete." if ok else "Operation finished with errors.",
                )
                self._publish("maintenance_state", self.maintenance_state)

        output = [item for item in self.maintenance_output if item["id"] > start_id]
        return web.json_response({"ok": ok, "result": payload, "output": output})

    def _set_repository(self, status: maintenance.UpdateStatus) -> None:
        if status.error:
            state = status.error
        elif status.ahead and status.behind:
            state = f"Diverged · {status.ahead} ahead / {status.behind} behind"
        elif status.update_available:
            state = f"Update available · {status.behind} behind"
        elif status.ahead:
            state = f"Local branch · {status.ahead} ahead"
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
        self._publish("repository", self.repository)

    async def _refresh_repository(self) -> None:
        branch, revision = await asyncio.gather(
            maintenance.git_current_branch(),
            maintenance.run(["git", "rev-parse", "--short", "HEAD"]),
        )
        self.repository.update(
            branch=branch or "—",
            commit=revision.output.strip() if revision.ok else "—",
        )
        self._publish("repository", self.repository)

    async def _auto_update_loop(self) -> None:
        await asyncio.sleep(60)
        while True:
            self._reload_saved_config()
            interval = max(5, self.config.auto_update_interval)
            if self.config.auto_update and not self._maintenance_lock.locked():
                async with self._maintenance_lock:
                    self._maintenance_log("operation", "Automatic update check started.")
                    status = await maintenance.check_for_updates(
                        lambda line: self._maintenance_log("line", line)
                    )
                    self._set_repository(status)
                    if status.update_available:
                        self._maintenance_log(
                            "operation",
                            f"Pulling {status.behind} new commit(s) from the tracked branch.",
                        )
                        pulled, dependencies = await maintenance.pull_and_install(
                            lambda line: self._maintenance_log("line", line)
                        )
                        if pulled:
                            await self._refresh_repository()
                        if (
                            pulled
                            and dependencies
                            and self.config.auto_restart
                            and self.controller.active
                        ):
                            async with self._lifecycle_lock:
                                await self.controller.restart()
            await asyncio.sleep(interval * 60)

    # ------------------------------------------------------------------ #
    # App lifecycle and security
    # ------------------------------------------------------------------ #

    async def startup(self, app: web.Application) -> None:
        self._loop = asyncio.get_running_loop()
        await self._refresh_repository()
        task = asyncio.create_task(self._auto_update_loop(), name="web-auto-update")
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        if not self.config.missing_secrets():
            await self.controller.start()

    async def shutdown(self, app: web.Application) -> None:
        for task in tuple(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        await self.controller.stop()

    async def index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC / "index.html")

    @staticmethod
    @web.middleware
    async def security_middleware(request: web.Request, handler):
        if request.path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("Origin")
            if origin:
                scheme = request.headers.get("X-Forwarded-Proto", request.scheme).split(",")[0].strip()
                host = request.headers.get("X-Forwarded-Host", request.host).split(",")[0].strip()
                if origin.rstrip("/") != f"{scheme}://{host}".rstrip("/"):
                    response = WebHub._json_error(
                        "Cross-origin control requests are not allowed.", 403
                    )
                    for name, value in SECURITY_HEADERS.items():
                        response.headers[name] = value
                    return response
        try:
            response = await handler(request)
        except web.HTTPException as response:
            pass
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        return response

    def app(self) -> web.Application:
        app = web.Application(
            client_max_size=64 * 1024,
            middlewares=[self.security_middleware],
        )
        app.add_routes([
            web.get("/", self.index),
            web.get("/api/status", self.status),
            web.get("/api/events", self.events_stream),
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
