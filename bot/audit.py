"""Audit logging.

MANDATORY and requester-truthful. Discord's own audit log shows the *bot* as the
actor, hiding who actually asked -- so we log it ourselves: timestamp, the real
requester (id + name), the raw message, the AI's proposed action(s), each
validation result, and the final outcome.

Writes go to three places:
  * a local SQLite table (queryable, persistent),
  * a configured Discord log channel (if set for the guild),
  * live subscribers (the TUI dashboard), via registered callbacks.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import discord


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AuditRecord:
    timestamp: str
    guild_id: int | None
    guild_name: str
    requester_id: int
    requester_name: str
    raw_message: str
    action: str  # tool name
    arguments: dict[str, Any]
    validation: str  # which check + verdict, human-readable
    allowed: bool
    outcome: str  # what actually happened (or the error)

    def summary_line(self) -> str:
        verdict = "✅ EXECUTED" if self.allowed else "⛔ REFUSED"
        return (
            f"{verdict}  {self.action}  by {self.requester_name} "
            f"({self.requester_id})  in {self.guild_name} — {self.outcome}"
        )


class AuditLogger:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._subscribers: list[Callable[[AuditRecord], None]] = []
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_log (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp      TEXT    NOT NULL,
                    guild_id       INTEGER,
                    guild_name     TEXT,
                    requester_id   INTEGER NOT NULL,
                    requester_name TEXT    NOT NULL,
                    raw_message    TEXT,
                    action         TEXT    NOT NULL,
                    arguments      TEXT,
                    validation     TEXT,
                    allowed        INTEGER NOT NULL,
                    outcome        TEXT
                )
                """
            )
            self._conn.commit()

    # --- live subscription (used by the TUI) ------------------------------- #

    def subscribe(self, callback: Callable[[AuditRecord], None]) -> None:
        self._subscribers.append(callback)

    def _notify(self, record: AuditRecord) -> None:
        for cb in list(self._subscribers):
            try:
                cb(record)
            except Exception:  # never let a bad subscriber break logging
                pass

    # --- persistence ------------------------------------------------------- #

    def _persist(self, record: AuditRecord) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO audit_log
                    (timestamp, guild_id, guild_name, requester_id, requester_name,
                     raw_message, action, arguments, validation, allowed, outcome)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.timestamp,
                    record.guild_id,
                    record.guild_name,
                    record.requester_id,
                    record.requester_name,
                    record.raw_message,
                    record.action,
                    json.dumps(record.arguments, default=str),
                    record.validation,
                    int(record.allowed),
                    record.outcome,
                ),
            )
            self._conn.commit()

    def recent(self, limit: int = 50) -> list[AuditRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out: list[AuditRecord] = []
        for r in rows:
            out.append(
                AuditRecord(
                    timestamp=r["timestamp"],
                    guild_id=r["guild_id"],
                    guild_name=r["guild_name"],
                    requester_id=r["requester_id"],
                    requester_name=r["requester_name"],
                    raw_message=r["raw_message"],
                    action=r["action"],
                    arguments=json.loads(r["arguments"] or "{}"),
                    validation=r["validation"],
                    allowed=bool(r["allowed"]),
                    outcome=r["outcome"],
                )
            )
        return out

    # --- the main entry point --------------------------------------------- #

    async def log(
        self,
        *,
        requester: "discord.abc.User | discord.Member",
        guild: "discord.Guild | None",
        raw_message: str,
        action: str,
        arguments: dict[str, Any],
        validation: str,
        allowed: bool,
        outcome: str,
        log_channel: "discord.abc.Messageable | None" = None,
    ) -> AuditRecord:
        record = AuditRecord(
            timestamp=_now_iso(),
            guild_id=getattr(guild, "id", None),
            guild_name=getattr(guild, "name", "DM"),
            requester_id=requester.id,
            requester_name=str(requester),
            raw_message=raw_message,
            action=action,
            arguments=arguments,
            validation=validation,
            allowed=allowed,
            outcome=outcome,
        )
        # 1) SQLite (synchronous, fast, guarded).
        self._persist(record)
        # 2) live subscribers (TUI).
        self._notify(record)
        # 3) Discord log channel (best-effort).
        if log_channel is not None:
            try:
                await log_channel.send(embed=self._embed(record))
            except (discord.Forbidden, discord.HTTPException):
                pass
        return record

    @staticmethod
    def _embed(record: AuditRecord) -> "discord.Embed":
        colour = discord.Colour.green() if record.allowed else discord.Colour.red()
        title = "✅ Action executed" if record.allowed else "⛔ Action refused"
        embed = discord.Embed(title=title, colour=colour, timestamp=datetime.now(timezone.utc))
        embed.add_field(
            name="Requester",
            value=f"{record.requester_name} (`{record.requester_id}`)",
            inline=False,
        )
        embed.add_field(name="Action", value=f"`{record.action}`", inline=True)
        args_str = json.dumps(record.arguments, default=str)
        if len(args_str) > 1000:
            args_str = args_str[:1000] + "…"
        embed.add_field(name="Arguments", value=f"```{args_str}```", inline=False)
        embed.add_field(name="Validation", value=record.validation[:1024] or "—", inline=False)
        embed.add_field(name="Outcome", value=record.outcome[:1024] or "—", inline=False)
        msg = record.raw_message or ""
        if msg:
            embed.add_field(name="Original request", value=msg[:1024], inline=False)
        return embed
