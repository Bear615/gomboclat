"""Persistent moderation cases used by the human-facing slash commands."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class Warning:
    id: int
    guild_id: int
    member_id: int
    moderator_id: int
    moderator_name: str
    reason: str
    created_at: str


class WarningStore:
    """Thread-safe warning/case storage in the bot's existing SQLite database."""

    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS warnings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    member_id INTEGER NOT NULL,
                    moderator_id INTEGER NOT NULL,
                    moderator_name TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_warnings_member "
                "ON warnings (guild_id, member_id, id DESC)"
            )
            self._conn.commit()

    def add(self, guild_id: int, member_id: int, moderator_id: int, moderator_name: str, reason: str) -> Warning:
        created_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO warnings (guild_id, member_id, moderator_id, moderator_name, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (guild_id, member_id, moderator_id, moderator_name, reason, created_at),
            )
            self._conn.commit()
            warning_id = int(cur.lastrowid)
        return Warning(warning_id, guild_id, member_id, moderator_id, moderator_name, reason, created_at)

    def list_for(self, guild_id: int, member_id: int, limit: int = 10) -> list[Warning]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM warnings WHERE guild_id = ? AND member_id = ? ORDER BY id DESC LIMIT ?",
                (guild_id, member_id, max(1, min(limit, 50))),
            ).fetchall()
        return [Warning(**dict(row)) for row in rows]

    def clear(self, guild_id: int, member_id: int) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM warnings WHERE guild_id = ? AND member_id = ?", (guild_id, member_id)
            )
            self._conn.commit()
            return cur.rowcount
