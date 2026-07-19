"""Tests for per-guild settings writes and per-guild audit queries.

Both are backed by SQLite and need no Discord client, so they run fast in a
temporary database.
"""

from __future__ import annotations

from bot.audit import AuditLogger, AuditRecord
from bot.config import GuildSettingsStore


def test_set_rate_limit_and_reset(tmp_path):
    store = GuildSettingsStore(str(tmp_path / "s.db"))
    assert store.get(1).rate_limit_max is None  # default -> global

    store.set_rate_limit(1, 12)
    assert store.get(1).rate_limit_max == 12

    store.set_rate_limit(1, None)  # reset to default
    assert store.get(1).rate_limit_max is None


def test_set_enabled_toggles(tmp_path):
    store = GuildSettingsStore(str(tmp_path / "s.db"))
    assert store.get(2).enabled is True  # default enabled

    store.set_enabled(2, False)
    assert store.get(2).enabled is False

    store.set_enabled(2, True)
    assert store.get(2).enabled is True


def test_settings_are_isolated_per_guild(tmp_path):
    store = GuildSettingsStore(str(tmp_path / "s.db"))
    store.set_enabled(10, False)
    store.set_rate_limit(10, 3)
    # A different guild is untouched.
    assert store.get(20).enabled is True
    assert store.get(20).rate_limit_max is None


def _record(guild_id: int, action: str, allowed: bool = True) -> AuditRecord:
    return AuditRecord(
        timestamp="2026-07-19T00:00:00+00:00",
        guild_id=guild_id,
        guild_name=f"Guild {guild_id}",
        requester_id=1,
        requester_name="tester",
        raw_message="hi",
        action=action,
        arguments={},
        validation="ok",
        allowed=allowed,
        outcome="done",
    )


def test_recent_for_guild_filters_and_orders(tmp_path):
    audit = AuditLogger(str(tmp_path / "a.db"))
    audit._persist(_record(100, "create_role"))
    audit._persist(_record(200, "assign_role"))
    audit._persist(_record(100, "create_channel"))

    got = audit.recent_for_guild(100)
    assert [r.action for r in got] == ["create_channel", "create_role"]  # newest first
    assert all(r.guild_id == 100 for r in got)


def test_recent_for_guild_limit(tmp_path):
    audit = AuditLogger(str(tmp_path / "a.db"))
    for i in range(5):
        audit._persist(_record(1, f"action_{i}"))
    assert len(audit.recent_for_guild(1, limit=3)) == 3
