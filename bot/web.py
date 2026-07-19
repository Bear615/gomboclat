"""The web control hub: the browser twin of the TUI, served for nginx.

Same job as ``bot/tui.py`` — run and manage the whole bot — but over HTTP so it
can live at https://dcgsl.duckdns.org (or any domain): dashboard with start /
stop / restart and a live audit feed, a Configure tab that edits .env, and a
Maintenance tab for dependency installs and git updates.

Security model
--------------
  * Binds to 127.0.0.1 by default. The internet only reaches it through nginx
    over TLS — ``deploy/install-web.sh`` sets that up end to end.
  * One admin password, stored as an scrypt hash in .env (never plaintext).
    The hub refuses to start until a password has been set.
  * Sessions are HMAC-signed expiring cookies (HttpOnly, SameSite=Strict,
    Secure behind TLS). Every state-changing request must also carry the
    per-session CSRF token in an ``X-CSRF-Token`` header.
  * Failed logins are throttled per client address (nginx adds a second
    rate-limit layer in front of /login).
  * Strict security headers on every response (self-only CSP, no framing,
    no sniffing); secrets are never echoed back to the browser.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import discord
from aiohttp import web

from . import control, maintenance, websecurity
from .ai import Agent
from .audit import AuditLogger, AuditRecord
from .config import Config, GuildSettingsStore, update_env_file
from .main import BotHooks, create_bot
from .ratelimit import RateLimiter

WEBUI_DIR = Path(__file__).resolve().parent / "webui"
SESSION_COOKIE = "gomboclat_session"

_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

# Paths reachable without a session (login itself, health probe, assets).
_PUBLIC_PATHS = {"/login", "/healthz"}


class EventBuffer:
    """Bounded in-memory event log with monotonically increasing ids, so the
    browser can poll incrementally with ``?after=<last seen id>``."""

    def __init__(self, maxlen: int = 500):
        self._events: deque[dict] = deque(maxlen=maxlen)
        self._next_id = 1

    def append(self, kind: str, text: str) -> None:
        self._events.append(
            {
                "id": self._next_id,
                "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "kind": kind,
                "text": text,
            }
        )
        self._next_id += 1

    def since(self, after_id: int) -> list[dict]:
        return [e for e in self._events if e["id"] > after_id]


class WebHub:
    """Owns the bot lifecycle plus the aiohttp app. Mirrors ``ModeratorHub``:
    persistent components survive bot restarts; a fresh bot is built on every
    start so config edits apply on restart without leaving the process."""

    def __init__(self) -> None:
        self.config = Config.load(require_secrets=False)
        self.audit = AuditLogger(self.config.db_path)
        self.settings_store = GuildSettingsStore(self.config.db_path)
        self.ratelimiter = RateLimiter(self.config.rate_limit_max, self.config.rate_limit_window)
        self.controller = control.BotController(self._build_bot, on_state=self._on_bot_state)

        self.feed = EventBuffer(500)
        self.maintlog = EventBuffer(1000)
        self._throttle = websecurity.LoginThrottle()
        self._secret = ""
        self._bot_lock = asyncio.Lock()
        self._maint_task: asyncio.Task | None = None
        self._connected = False
        self._user = "—"
        self._guilds: list[str] = []
        self._git = ""

    # ------------------------------------------------------------------ #
    # Bot builder + hooks (same shape as the TUI's)
    # ------------------------------------------------------------------ #

    def _build_bot(self):
        config = Config.load(require_secrets=True)  # re-reads .env
        self.config = config
        self.ratelimiter.max_actions = config.rate_limit_max
        self.ratelimiter.window = config.rate_limit_window
        agent = Agent(config)
        hooks = BotHooks(
            on_ready=self._on_ready,
            on_status=lambda s: self.feed.append("status", s),
            on_message_seen=lambda s: self.feed.append("seen", f"📨 {s}"),
        )
        bot = create_bot(config, self.audit, self.settings_store, self.ratelimiter, agent, hooks)
        return bot, config.discord_token

    def _on_ready(self, user, guilds) -> None:
        self._user = str(user)
        self._guilds = [g.name for g in guilds]
        self._connected = True
        self.controller.mark_ready()
        self.feed.append("state", f"✔ Connected as {self._user} — {len(self._guilds)} guild(s).")

    def _on_bot_state(self, state: str, error: str) -> None:
        if state in (control.STOPPED, control.ERROR):
            self._connected = False
        self.feed.append("state", f"bot {state}" + (f" — {error}" if error else ""))

    def _on_audit(self, rec: AuditRecord) -> None:
        tag = "✅ EXECUTED" if rec.allowed else "⛔ REFUSED"
        self.feed.append(
            "audit-ok" if rec.allowed else "audit-refused",
            f"{tag} {rec.action} · {rec.requester_name} ({rec.requester_id}) "
            f"· {rec.guild_name} — {rec.outcome}",
        )

    # ------------------------------------------------------------------ #
    # Auth plumbing
    # ------------------------------------------------------------------ #

    def _client_key(self, request: web.Request) -> str:
        """Throttle key: the real client address. X-Real-IP is only trusted
        when the TCP peer is localhost (i.e. it was set by our nginx)."""
        peer = request.remote or "?"
        if peer in ("127.0.0.1", "::1"):
            return request.headers.get("X-Real-IP", peer)
        return peer

    @staticmethod
    def _is_https(request: web.Request) -> bool:
        return request.headers.get("X-Forwarded-Proto") == "https" or request.secure

    def _session_ok(self, request: web.Request) -> bool:
        token = request.cookies.get(SESSION_COOKIE, "")
        return bool(token) and websecurity.check_token(self._secret, token)

    @staticmethod
    def _secure_headers(resp: web.StreamResponse) -> web.StreamResponse:
        for name, value in _SECURITY_HEADERS.items():
            resp.headers[name] = value
        return resp

    async def _middleware_impl(self, request: web.Request, handler):
        path = request.path
        public = path in _PUBLIC_PATHS or path.startswith("/static/")
        if not public:
            if not self._session_ok(request):
                if path.startswith("/api/") or path == "/logout":
                    return self._secure_headers(
                        web.json_response({"error": "unauthenticated"}, status=401)
                    )
                return self._secure_headers(web.HTTPFound("/login"))
            if request.method not in ("GET", "HEAD"):
                token = request.cookies.get(SESSION_COOKIE, "")
                csrf = request.headers.get("X-CSRF-Token", "")
                if not websecurity.check_csrf(self._secret, token, csrf):
                    return self._secure_headers(
                        web.json_response({"error": "bad CSRF token"}, status=403)
                    )
        resp = await handler(request)
        return self._secure_headers(resp)

    # ------------------------------------------------------------------ #
    # Page + auth handlers
    # ------------------------------------------------------------------ #

    async def handle_index(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(WEBUI_DIR / "index.html")

    async def handle_login_page(self, request: web.Request) -> web.StreamResponse:
        if self._session_ok(request):
            return web.HTTPFound("/")
        return web.FileResponse(WEBUI_DIR / "login.html")

    async def handle_login_post(self, request: web.Request) -> web.StreamResponse:
        key = self._client_key(request)
        wait = self._throttle.retry_after(key)
        if wait > 0:
            return web.HTTPFound(f"/login?error=throttled&wait={int(wait) + 1}")
        form = await request.post()
        password = str(form.get("password", ""))
        if not (
            password
            and self.config.web_password_hash
            and websecurity.verify_password(password, self.config.web_password_hash)
        ):
            self._throttle.record_failure(key)
            self.feed.append("state", f"⚠ Failed web login from {key}")
            return web.HTTPFound("/login?error=bad")
        self._throttle.record_success(key)
        lifetime = max(1, self.config.web_session_hours) * 3600
        token = websecurity.issue_token(self._secret, lifetime)
        resp = web.HTTPFound("/")
        resp.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=lifetime,
            path="/",
            httponly=True,
            samesite="Strict",
            secure=self._is_https(request),
        )
        return resp

    async def handle_logout(self, request: web.Request) -> web.StreamResponse:
        resp = web.json_response({"ok": True})
        resp.del_cookie(SESSION_COOKIE, path="/")
        return resp

    async def handle_healthz(self, request: web.Request) -> web.StreamResponse:
        return web.json_response({"ok": True})

    # ------------------------------------------------------------------ #
    # API handlers
    # ------------------------------------------------------------------ #

    async def handle_status(self, request: web.Request) -> web.StreamResponse:
        c = self.config
        token = request.cookies.get(SESSION_COOKIE, "")
        return web.json_response(
            {
                "state": self.controller.state,
                "error": self.controller.last_error,
                "connected": self._connected,
                "user": self._user,
                "guilds": self._guilds,
                "missing_secrets": c.missing_secrets(),
                "git": self._git,
                "csrf": websecurity.csrf_for(self._secret, token),
                "config": {
                    "model": c.anthropic_model,
                    "rate_limit_max": c.rate_limit_max,
                    "rate_limit_window": c.rate_limit_window,
                    "enable_punitive": c.enable_punitive,
                    "auto_update": c.auto_update,
                    "auto_update_interval": c.auto_update_interval,
                    "auto_restart": c.auto_restart,
                },
            }
        )

    @staticmethod
    def _after_param(request: web.Request) -> int:
        try:
            return int(request.query.get("after", "0"))
        except ValueError:
            return 0

    async def handle_feed(self, request: web.Request) -> web.StreamResponse:
        return web.json_response({"events": self.feed.since(self._after_param(request))})

    async def handle_maintlog(self, request: web.Request) -> web.StreamResponse:
        return web.json_response({"events": self.maintlog.since(self._after_param(request))})

    async def handle_bot(self, request: web.Request) -> web.StreamResponse:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        action = data.get("action")
        if action not in ("start", "stop", "restart"):
            return web.json_response({"error": "unknown action"}, status=400)
        if action in ("start", "restart") and self.config.missing_secrets():
            return web.json_response(
                {"error": "Set your tokens in the Configure tab first."}, status=400
            )
        async with self._bot_lock:
            await getattr(self.controller, action)()
        return web.json_response({"ok": True, "state": self.controller.state})

    # Same field <-> env-var mapping as the TUI's Configure tab.
    _ENV_MAP = {
        "discord_token": "DISCORD_TOKEN",
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "anthropic_model": "ANTHROPIC_MODEL",
        "max_tokens": "MAX_TOKENS",
        "max_agent_iterations": "MAX_AGENT_ITERATIONS",
        "rate_limit_max": "RATE_LIMIT_MAX",
        "rate_limit_window": "RATE_LIMIT_WINDOW",
        "bulk_confirm_threshold": "BULK_CONFIRM_THRESHOLD",
        "auto_update_interval": "AUTO_UPDATE_INTERVAL",
    }
    _BOOL_MAP = {
        "enable_punitive": "ENABLE_PUNITIVE",
        "auto_update": "AUTO_UPDATE",
        "auto_restart": "AUTO_RESTART",
    }
    _INT_FIELDS = {
        "max_tokens",
        "max_agent_iterations",
        "rate_limit_max",
        "rate_limit_window",
        "bulk_confirm_threshold",
        "auto_update_interval",
    }
    _SECRET_FIELDS = ("discord_token", "anthropic_api_key")

    async def handle_config_get(self, request: web.Request) -> web.StreamResponse:
        c = self.config
        missing = c.missing_secrets()
        return web.json_response(
            {
                # Secrets are never echoed back — only whether they're set.
                "discord_token_set": "DISCORD_TOKEN" not in missing,
                "anthropic_key_set": "ANTHROPIC_API_KEY" not in missing,
                "values": {
                    "discord_token": "",
                    "anthropic_api_key": "",
                    "anthropic_model": c.anthropic_model,
                    "max_tokens": str(c.max_tokens),
                    "max_agent_iterations": str(c.max_agent_iterations),
                    "rate_limit_max": str(c.rate_limit_max),
                    "rate_limit_window": str(c.rate_limit_window),
                    "bulk_confirm_threshold": str(c.bulk_confirm_threshold),
                    "auto_update_interval": str(c.auto_update_interval),
                    "enable_punitive": c.enable_punitive,
                    "auto_update": c.auto_update,
                    "auto_restart": c.auto_restart,
                },
            }
        )

    async def handle_config_post(self, request: web.Request) -> web.StreamResponse:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        values = data.get("values") or {}
        restart = bool(data.get("restart"))

        updates: dict[str, str] = {}
        bad: list[str] = []
        for field, key in self._ENV_MAP.items():
            if field not in values:
                continue
            val = str(values[field]).strip()
            if field in self._SECRET_FIELDS and not val:
                continue  # blank secret box = keep the existing value
            if field in self._INT_FIELDS:
                try:
                    int(val)
                except ValueError:
                    bad.append(field)
                    continue
            updates[key] = val
        for field, key in self._BOOL_MAP.items():
            if field in values:
                updates[key] = "true" if values[field] else "false"
        if bad:
            return web.json_response(
                {"error": f"not a number: {', '.join(bad)}"}, status=400
            )

        try:
            update_env_file(updates)
        except Exception as e:
            return web.json_response({"error": f"could not write .env: {e}"}, status=500)

        self.config = Config.load(require_secrets=False)
        self.ratelimiter.max_actions = self.config.rate_limit_max
        self.ratelimiter.window = self.config.rate_limit_window
        self.feed.append("state", "Configuration saved to .env.")
        if restart:
            async with self._bot_lock:
                await self.controller.restart()
        return web.json_response({"ok": True, "restarted": restart})

    async def handle_maintenance(self, request: web.Request) -> web.StreamResponse:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        action = data.get("action")
        if action not in ("install", "reinstall", "check", "update"):
            return web.json_response({"error": "unknown action"}, status=400)
        if self._maint_task is not None and not self._maint_task.done():
            return web.json_response(
                {"error": "another maintenance task is already running"}, status=409
            )
        self._maint_task = asyncio.create_task(self._run_maintenance(action))
        return web.json_response({"ok": True})

    # ------------------------------------------------------------------ #
    # Maintenance workers (mirroring the TUI's)
    # ------------------------------------------------------------------ #

    def _maint_line(self, line: str) -> None:
        self.maintlog.append("maint", line)

    async def _run_maintenance(self, action: str) -> None:
        sink = self._maint_line
        try:
            if action == "install":
                sink("Installing dependencies…")
                res = await maintenance.install_dependencies(sink, upgrade=True)
                sink("✔ Dependencies installed." if res.ok else "✖ Install failed.")
            elif action == "reinstall":
                sink("Force-reinstalling dependencies…")
                res = await maintenance.reinstall_dependencies(sink)
                sink("✔ Reinstall complete." if res.ok else "✖ Reinstall failed.")
            elif action == "check":
                sink("Checking GitHub for updates…")
                status = await maintenance.check_for_updates(sink)
                await self._refresh_git(status)
                if status.error:
                    sink(status.error)
                elif status.update_available:
                    sink(f"Update available: {status.behind} commit(s) behind {status.branch}.")
                else:
                    sink("Already up to date.")
            elif action == "update":
                sink("Pulling latest and reinstalling…")
                report = await maintenance.pull_and_install(sink)
                if not report.pulled:
                    sink("✖ Pull failed (not fast-forward, or no upstream).")
                    return
                await self._report_update(report)
                if self.controller.active:
                    sink("Restarting bot to apply update…")
                    async with self._bot_lock:
                        await self.controller.restart()
        except Exception as e:
            sink(f"✖ {action} failed: {e}")

    async def _report_update(self, report: "maintenance.UpdateReport") -> None:
        if report.commits:
            self._maint_line(f"✔ Updated — {len(report.commits)} new commit(s):")
            for subject in report.commits[:15]:
                self._maint_line(f"  • {subject}")
            if len(report.commits) > 15:
                self._maint_line(f"  …and {len(report.commits) - 15} more")
        else:
            self._maint_line("✔ Updated (no new commits).")
        if not report.deps_ok:
            self._maint_line("(dependency install had issues)")
        await self._refresh_git()
        await self._announce_update(report)

    async def _announce_update(self, report: "maintenance.UpdateReport") -> None:
        """Post an update summary to every guild's configured log channel.
        Best-effort and silent per-guild, exactly like the TUI's."""
        if not report.changed:
            return
        bot = self.controller.bot
        if bot is None:
            self._maint_line("Bot not running — skipped Discord announcement.")
            return

        embed = discord.Embed(
            title="🔄 Bot updated",
            description=(
                f"Pulled **{len(report.commits)}** new commit(s) "
                f"(`{report.old_rev[:7]}` → `{report.new_rev[:7]}`)."
            ),
            colour=discord.Colour.blurple(),
        )
        commit_lines = "\n".join(f"• {c}" for c in report.commits[:10])
        if len(report.commits) > 10:
            commit_lines += f"\n…and {len(report.commits) - 10} more"
        embed.add_field(name="Changes", value=commit_lines[:1024] or "—", inline=False)

        changelog = maintenance.read_changelog_section()
        if changelog:
            snippet = changelog if len(changelog) <= 1000 else changelog[:1000].rstrip() + "\n…"
            embed.add_field(name="Changelog", value=snippet, inline=False)

        sent = 0
        for guild in list(bot.guilds):
            s = self.settings_store.get(guild.id)
            if not s.log_channel_id:
                continue
            channel = guild.get_channel(s.log_channel_id)
            if channel is None:
                continue
            try:
                await channel.send(embed=embed)
                sent += 1
            except discord.HTTPException:
                pass
        self._maint_line(f"Announced update to {sent} log channel(s).")

    async def _auto_update_loop(self) -> None:
        await asyncio.sleep(60)
        while True:
            interval = max(5, self.config.auto_update_interval)
            if self.config.auto_update:
                status = await maintenance.check_for_updates(self._maint_line)
                if status.update_available:
                    self._maint_line(f"Auto-update: {status.behind} commit(s) behind — pulling…")
                    report = await maintenance.pull_and_install(self._maint_line)
                    if report.pulled:
                        await self._report_update(report)
                        if self.config.auto_restart and self.controller.active:
                            self._maint_line("Auto-restarting bot…")
                            async with self._bot_lock:
                                await self.controller.restart()
            await asyncio.sleep(interval * 60)

    async def _refresh_git(self, status: "maintenance.UpdateStatus | None" = None) -> None:
        try:
            if status is None:
                branch = await maintenance.git_current_branch()
                local = (await maintenance.run(["git", "rev-parse", "--short", "HEAD"])).output.strip()
                self._git = f"Branch: {branch or '—'} · Commit: {local or '—'}"
            elif status.error:
                self._git = f"Branch: {status.branch or '—'} · {status.error}"
            else:
                behind = f"{status.behind} behind" if status.update_available else "up to date"
                self._git = (
                    f"Branch: {status.branch} · Local: {status.local_rev} · "
                    f"Remote: {status.remote_rev} · {behind}"
                )
        except Exception:
            self._git = ""

    # ------------------------------------------------------------------ #
    # App assembly + entrypoint
    # ------------------------------------------------------------------ #

    def make_app(self) -> web.Application:
        @web.middleware
        async def middleware(request, handler):
            return await self._middleware_impl(request, handler)

        app = web.Application(middlewares=[middleware])
        app.router.add_get("/", self.handle_index)
        app.router.add_get("/login", self.handle_login_page)
        app.router.add_post("/login", self.handle_login_post)
        app.router.add_post("/logout", self.handle_logout)
        app.router.add_get("/healthz", self.handle_healthz)
        app.router.add_get("/api/status", self.handle_status)
        app.router.add_get("/api/feed", self.handle_feed)
        app.router.add_get("/api/maintlog", self.handle_maintlog)
        app.router.add_get("/api/config", self.handle_config_get)
        app.router.add_post("/api/config", self.handle_config_post)
        app.router.add_post("/api/bot", self.handle_bot)
        app.router.add_post("/api/maintenance", self.handle_maintenance)
        app.router.add_static("/static/", WEBUI_DIR, show_index=False)
        return app

    async def serve(self, host: str, port: int) -> None:
        if not self.config.web_password_hash:
            raise SystemExit(
                "The web hub has no admin password yet.\n"
                "Set one first:  .venv/bin/python run.py --set-web-password"
            )
        if not self.config.web_session_secret:
            # Generate + persist once so restarts don't log everyone out.
            secret = websecurity.generate_secret()
            update_env_file({"WEB_SESSION_SECRET": secret})
            self.config.web_session_secret = secret
        self._secret = self.config.web_session_secret

        self.audit.subscribe(self._on_audit)
        self.feed.append("state", "Web hub ready. Recent audit history:")
        for rec in reversed(self.audit.recent(15)):
            self._on_audit(rec)
        if self.config.missing_secrets():
            self.feed.append(
                "state", "No tokens set yet — open the Configure tab, fill them in, then press Start."
            )

        runner = web.AppRunner(self.make_app(), access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        print(f"Web control hub listening on http://{host}:{port}")
        if host not in ("127.0.0.1", "::1", "localhost"):
            print(
                "WARNING: bound to a non-loopback address — this hub is meant to "
                "sit behind nginx+TLS (see deploy/install-web.sh)."
            )

        tasks = [
            asyncio.create_task(self._auto_update_loop()),
            asyncio.create_task(self._refresh_git()),
        ]
        if not self.config.missing_secrets():
            await self.controller.start()
        try:
            await asyncio.Event().wait()
        finally:
            for t in tasks:
                t.cancel()
            try:
                await self.controller.stop()
            except Exception:
                pass
            await runner.cleanup()


def run_web(host: str | None = None, port: int | None = None) -> None:
    hub = WebHub()
    try:
        asyncio.run(hub.serve(host or hub.config.web_host, port or hub.config.web_port))
    except KeyboardInterrupt:
        pass
