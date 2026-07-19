"""Tests for the update-changelog announcer.

Covers the pure decision logic, the persistent state store, embed formatting, and
an end-to-end announce (with git shelled-out calls monkeypatched) that verifies the
version auto-increments and the guild owner gets pinged in the log channel.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from bot import updatenotice as un
from bot.config import BotStateStore


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_format_version():
    assert un.format_version(1) == "v0.0.1"
    assert un.format_version(12) == "v0.0.12"


def test_plan_update_noop_when_no_git():
    assert un._plan_update("abc", 3, "") == ("noop", 3)


def test_plan_update_init_on_first_boot():
    assert un._plan_update(None, 0, "abc") == ("init", 1)


def test_plan_update_announce_when_head_changed():
    assert un._plan_update("abc", 4, "def") == ("announce", 5)


def test_plan_update_noop_when_unchanged():
    assert un._plan_update("abc", 4, "abc") == ("noop", 4)


# --------------------------------------------------------------------------- #
# State store
# --------------------------------------------------------------------------- #


def test_state_store_roundtrip_and_persistence(tmp_path):
    db = str(tmp_path / "state.db")
    s = BotStateStore(db)
    assert s.get("missing") is None
    assert s.get("missing", "d") == "d"
    s.set("k", "v1")
    s.set("k", "v2")  # upsert
    assert s.get("k") == "v2"
    # A fresh store on the same file sees the persisted value.
    assert BotStateStore(db).get("k") == "v2"


def test_current_version_defaults_to_v001(tmp_path):
    s = BotStateStore(str(tmp_path / "state.db"))
    assert un.current_version(s) == "v0.0.1"
    s.set("version_patch", "3")
    assert un.current_version(s) == "v0.0.3"


# --------------------------------------------------------------------------- #
# Embed formatting
# --------------------------------------------------------------------------- #


def test_build_embed_lists_changelog_with_version_title():
    info = un.UpdateInfo(version="v0.0.2", old_short="aaaaaaa", new_short="bbbbbbb",
                         changelog=["Add tools", "Fix bug"])
    embed = un._build_embed(info)
    assert "v0.0.2" in embed.title
    assert "• Add tools" in embed.description
    assert "• Fix bug" in embed.description
    assert embed.footer.text == "aaaaaaa → bbbbbbb"


def test_build_embed_truncates_long_changelog():
    info = un.UpdateInfo(version="v0.0.9", old_short="a", new_short="b",
                         changelog=[f"c{i}" for i in range(20)])
    embed = un._build_embed(info)
    assert "…and 5 more change(s)." in embed.description  # 20 - 15 shown


def test_build_embed_fallback_when_no_changelog():
    info = un.UpdateInfo(version="v0.0.2", old_short="a", new_short="b", changelog=[])
    assert "Updated to the latest version." in un._build_embed(info).description


# --------------------------------------------------------------------------- #
# End-to-end announce (git calls monkeypatched)
# --------------------------------------------------------------------------- #


class _FakeChannel:
    def __init__(self):
        self.sends = []

    async def send(self, content=None, embed=None, allowed_mentions=None):
        self.sends.append(types.SimpleNamespace(content=content, embed=embed, allowed_mentions=allowed_mentions))


class _FakeState:
    def __init__(self):
        self.d = {}

    def get(self, key, default=None):
        return self.d.get(key, default)

    def set(self, key, value):
        self.d[key] = str(value)


def _make_bot(channel):
    def audit_logs(*a, **k):  # no inviter found -> only the owner is pinged
        async def _gen():
            if False:
                yield None
        return _gen()

    guild = types.SimpleNamespace(id=99, owner_id=42, get_channel=lambda cid: channel, audit_logs=audit_logs)
    return types.SimpleNamespace(user=types.SimpleNamespace(id=1000), guilds=[guild])


def _settings_store(channel_id):
    return types.SimpleNamespace(get=lambda gid: types.SimpleNamespace(log_channel_id=channel_id))


def test_announce_flow_increments_and_pings(monkeypatch):
    channel = _FakeChannel()
    bot = _make_bot(channel)
    settings = _settings_store(555)
    state = _FakeState()

    head = {"sha": "a" * 40}
    log = {"subjects": ["c1", "c2"]}

    async def fake_head():
        return head["sha"]

    async def fake_log(old, new="HEAD", limit=20):
        return list(log["subjects"])

    monkeypatch.setattr(un.maintenance, "git_head_sha", fake_head)
    monkeypatch.setattr(un.maintenance, "git_log_subjects", fake_log)

    # 1) First boot: baseline recorded silently, no announcement.
    r1 = asyncio.run(un.announce_if_updated(bot, state, settings))
    assert r1 is None
    assert channel.sends == []
    assert un.current_version(state) == "v0.0.1"

    # 2) HEAD changed -> announce v0.0.2 with changelog, pinging the owner.
    head["sha"] = "b" * 40
    log["subjects"] = ["Add mini-admin tools", "Fix voice mute"]
    r2 = asyncio.run(un.announce_if_updated(bot, state, settings))
    assert r2 is not None and r2.version == "v0.0.2"
    assert len(channel.sends) == 1
    sent = channel.sends[0]
    assert "<@42>" in sent.content  # guild owner pinged
    assert "v0.0.2" in sent.content
    assert "Add mini-admin tools" in sent.embed.description
    assert un.current_version(state) == "v0.0.2"

    # 3) Same HEAD on a reconnect -> no re-announce.
    r3 = asyncio.run(un.announce_if_updated(bot, state, settings))
    assert r3 is None
    assert len(channel.sends) == 1


def test_announce_skips_guilds_without_log_channel(monkeypatch):
    channel = _FakeChannel()
    bot = _make_bot(channel)
    settings = _settings_store(None)  # no log channel set
    state = _FakeState()
    state.set("last_sha", "a" * 40)
    state.set("version_patch", "1")

    async def fake_head():
        return "b" * 40

    async def fake_log(old, new="HEAD", limit=20):
        return ["something"]

    monkeypatch.setattr(un.maintenance, "git_head_sha", fake_head)
    monkeypatch.setattr(un.maintenance, "git_log_subjects", fake_log)

    r = asyncio.run(un.announce_if_updated(bot, state, settings))
    assert r is not None  # an update happened...
    assert channel.sends == []  # ...but nothing to post to


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
