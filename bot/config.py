"""Configuration: global secrets/settings from the environment, plus per-guild
settings persisted in SQLite (this is a multi-guild bot).

Global config (tokens, model, defaults) lives in ``.env``. Anything that can
differ per server -- the log channel, the per-user rate limit, whether the bot
is enabled there -- lives in the ``guild_settings`` SQLite table so admins can
change it at runtime without touching the environment.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ENV_PATH = Path(".env")


def reload_env() -> None:
    """(Re)load .env into the process environment, overriding stale values."""
    load_dotenv(ENV_PATH, override=True)


reload_env()


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def update_env_file(updates: dict[str, str], path: Path = ENV_PATH) -> None:
    """Write key=value pairs into .env, updating existing keys in place and
    appending new ones. Preserves comments and ordering. Creates the file (from
    .env.example if present) if it doesn't exist yet.
    """
    if not path.exists():
        example = Path(".env.example")
        path.write_text(example.read_text() if example.exists() else "")

    lines = path.read_text().splitlines()
    remaining = dict(updates)

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"

    for key, value in remaining.items():
        lines.append(f"{key}={value}")

    path.write_text("\n".join(lines) + "\n")
    reload_env()


@dataclass
class Config:
    """Global, process-wide configuration loaded once at startup."""

    discord_token: str
    # The LLM is spoken to over the OpenAI-compatible chat-completions API, so you
    # can point it at OpenAI, or ANY compatible server -- OpenRouter, Together,
    # Groq, LM Studio, Ollama, vLLM, LiteLLM, ... -- by setting your own endpoint.
    api_key: str
    api_base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    max_tokens: int = 2048
    max_agent_iterations: int = 8
    # Per-user write-action rate limit (default; overridable per guild).
    rate_limit_max: int = 5
    rate_limit_window: int = 60
    # Above this many write actions in a single AI turn, ask for confirmation.
    bulk_confirm_threshold: int = 3
    # Enable punitive tools (ban/kick/timeout) -- gated behind typed CONFIRM.
    enable_punitive: bool = True
    db_path: str = "moderator.db"
    # Auto-update from GitHub.
    auto_update: bool = False
    auto_update_interval: int = 30  # minutes between update checks
    auto_restart: bool = False      # restart the bot automatically after an update

    def missing_secrets(self) -> list[str]:
        out = []
        if not self.discord_token or self.discord_token.startswith("your-"):
            out.append("DISCORD_TOKEN")
        if not self.api_key or self.api_key.startswith("your-"):
            out.append("OPENAI_API_KEY")
        return out

    @classmethod
    def load(cls, require_secrets: bool = True) -> "Config":
        reload_env()
        token = _env("DISCORD_TOKEN")
        key = _env("OPENAI_API_KEY")
        config = cls(
            discord_token=token or "",
            api_key=key or "",
            api_base_url=_env("OPENAI_BASE_URL", "https://api.openai.com/v1"),  # type: ignore[arg-type]
            model=_env("OPENAI_MODEL", "gpt-4o-mini"),  # type: ignore[arg-type]
            max_tokens=_env_int("MAX_TOKENS", 2048),
            max_agent_iterations=_env_int("MAX_AGENT_ITERATIONS", 8),
            rate_limit_max=_env_int("RATE_LIMIT_MAX", 5),
            rate_limit_window=_env_int("RATE_LIMIT_WINDOW", 60),
            bulk_confirm_threshold=_env_int("BULK_CONFIRM_THRESHOLD", 3),
            enable_punitive=_env_bool("ENABLE_PUNITIVE", True),
            db_path=_env("DB_PATH", "moderator.db"),  # type: ignore[arg-type]
            auto_update=_env_bool("AUTO_UPDATE", False),
            auto_update_interval=_env_int("AUTO_UPDATE_INTERVAL", 30),
            auto_restart=_env_bool("AUTO_RESTART", False),
        )
        if require_secrets and config.missing_secrets():
            raise SystemExit(
                "Missing required environment variables: "
                + ", ".join(config.missing_secrets())
                + ".\nCopy .env.example to .env and fill it in (or use the TUI's Configure tab)."
            )
        return config


@dataclass
class GuildSettings:
    guild_id: int
    log_channel_id: int | None = None
    rate_limit_max: int | None = None  # None -> use global default
    enabled: bool = True


class GuildSettingsStore:
    """Thread-safe SQLite-backed store for per-guild settings.

    The bot runs on a single asyncio loop, but the TUI reads on another thread,
    so we guard access with a lock and open the connection with
    ``check_same_thread=False``.
    """

    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id       INTEGER PRIMARY KEY,
                    log_channel_id INTEGER,
                    rate_limit_max INTEGER,
                    enabled        INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            self._conn.commit()

    def get(self, guild_id: int) -> GuildSettings:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)
            ).fetchone()
        if row is None:
            return GuildSettings(guild_id=guild_id)
        return GuildSettings(
            guild_id=row["guild_id"],
            log_channel_id=row["log_channel_id"],
            rate_limit_max=row["rate_limit_max"],
            enabled=bool(row["enabled"]),
        )

    def upsert(self, settings: GuildSettings) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO guild_settings (guild_id, log_channel_id, rate_limit_max, enabled)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    log_channel_id = excluded.log_channel_id,
                    rate_limit_max = excluded.rate_limit_max,
                    enabled        = excluded.enabled
                """,
                (
                    settings.guild_id,
                    settings.log_channel_id,
                    settings.rate_limit_max,
                    int(settings.enabled),
                ),
            )
            self._conn.commit()

    def set_log_channel(self, guild_id: int, channel_id: int | None) -> None:
        current = self.get(guild_id)
        current.log_channel_id = channel_id
        self.upsert(current)
