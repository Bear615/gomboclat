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

load_dotenv()


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


@dataclass
class Config:
    """Global, process-wide configuration loaded once at startup."""

    discord_token: str
    anthropic_api_key: str
    # Default model is Sonnet; swap to claude-haiku-4-5-20251001 via env to cut cost.
    anthropic_model: str = "claude-sonnet-5"
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

    @classmethod
    def load(cls) -> "Config":
        token = _env("DISCORD_TOKEN")
        key = _env("ANTHROPIC_API_KEY")
        missing = [
            n
            for n, v in (("DISCORD_TOKEN", token), ("ANTHROPIC_API_KEY", key))
            if not v
        ]
        if missing:
            raise SystemExit(
                "Missing required environment variables: "
                + ", ".join(missing)
                + ".\nCopy .env.example to .env and fill it in."
            )
        return cls(
            discord_token=token,  # type: ignore[arg-type]
            anthropic_api_key=key,  # type: ignore[arg-type]
            anthropic_model=_env("ANTHROPIC_MODEL", "claude-sonnet-5"),  # type: ignore[arg-type]
            max_tokens=_env_int("MAX_TOKENS", 2048),
            max_agent_iterations=_env_int("MAX_AGENT_ITERATIONS", 8),
            rate_limit_max=_env_int("RATE_LIMIT_MAX", 5),
            rate_limit_window=_env_int("RATE_LIMIT_WINDOW", 60),
            bulk_confirm_threshold=_env_int("BULK_CONFIRM_THRESHOLD", 3),
            enable_punitive=(_env("ENABLE_PUNITIVE", "true") or "true").lower()
            in ("1", "true", "yes", "on"),
            db_path=_env("DB_PATH", "moderator.db"),  # type: ignore[arg-type]
        )


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
