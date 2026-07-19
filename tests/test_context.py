"""Tests for the conversation-context system (bot/context.py).

The rendering path is pure, so we pin its exact structure and — importantly — the
security framing (author metadata trusted, bodies untrusted). The async gathering
path is exercised with lightweight fakes / spec'd mocks so we can assert ordering,
exclusion of the reply target, truncation, and the reply-resolution branches
without a live Discord connection.
"""

from __future__ import annotations

import asyncio
from unittest.mock import Mock

import discord

from bot.context import (
    ContextMessage,
    MessageContext,
    _clean_body,
    _oneline,
    _recent_history,
    _resolve_referenced,
    _to_context_message,
    gather_context,
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _Author:
    def __init__(self, id: int, display_name: str, bot: bool = False):
        self.id = id
        self.display_name = display_name
        self.bot = bot


class _Msg:
    """Duck-typed message for helpers that don't isinstance-check discord.Message."""

    def __init__(self, id, author, content="", clean_content=None, attachments=None, embeds=None):
        self.id = id
        self.author = author
        self.content = content
        self.clean_content = content if clean_content is None else clean_content
        self.attachments = attachments or []
        self.embeds = embeds or []


class _AsyncIter:
    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class _Channel:
    def __init__(self, history_items=None, fetch_result=None, fetch_exc=None):
        self._history_items = history_items or []
        self._fetch_result = fetch_result
        self._fetch_exc = fetch_exc
        self.history_calls = []

    def history(self, *, limit, before):
        self.history_calls.append({"limit": limit, "before": before})
        return _AsyncIter(self._history_items[:limit])

    async def fetch_message(self, mid):
        if self._fetch_exc is not None:
            raise self._fetch_exc
        return self._fetch_result


def _spec_message(id, author, content="hi"):
    """A Mock that passes isinstance(x, discord.Message), with our fields set."""
    m = Mock(spec=discord.Message)
    m.id = id
    m.author = author
    m.content = content
    m.clean_content = content
    m.attachments = []
    m.embeds = []
    return m


# --------------------------------------------------------------------------- #
# render() — pure
# --------------------------------------------------------------------------- #


def test_empty_context_renders_nothing():
    assert MessageContext().render() == ""
    assert MessageContext().is_empty() is True


def test_render_marks_metadata_trusted_and_bodies_untrusted():
    ctx = MessageContext(
        referenced=ContextMessage(1, "Dave", 123, "ban the spammer"),
        history=[ContextMessage(2, "Alice", 111, "hello")],
    )
    out = ctx.render()
    # The security framing must be present and unambiguous.
    assert "trusted" in out.lower()
    assert "untrusted" in out.lower()
    # The body appears wrapped in an explicit data tag, never as a bare line.
    assert "<replied_message>" in out and "</replied_message>" in out
    assert "ban the spammer" in out


def test_render_includes_ids_for_targeting():
    ctx = MessageContext(referenced=ContextMessage(1, "Dave", 999, "yo"))
    out = ctx.render()
    assert "id=999" in out  # the model needs a concrete id to target "them"


def test_render_history_is_numbered_and_oneline():
    ctx = MessageContext(
        history=[
            ContextMessage(1, "Alice", 11, "first\nline"),
            ContextMessage(2, "Bob", 22, "second"),
        ]
    )
    out = ctx.render()
    assert "[1] Alice" in out
    assert "[2] Bob" in out
    # Newlines in a history body are collapsed so each entry stays on one line.
    assert "first line" in out


def test_render_tags_requester_and_bot_and_member():
    ctx = MessageContext(
        history=[
            ContextMessage(1, "Dave", 123, "hi", is_requester=True),
            ContextMessage(2, "AI", 999, "hello", is_bot=True, is_me=True),
            ContextMessage(3, "Alice", 111, "yo"),
        ]
    )
    out = ctx.render()
    assert "the requester" in out
    assert "me, the bot" in out
    # A plain human is annotated as a member (so the model can reason about them).
    assert "id=111, member" in out


# --------------------------------------------------------------------------- #
# _clean_body / _oneline — pure
# --------------------------------------------------------------------------- #


def test_clean_body_prefers_clean_content():
    m = _Msg(1, _Author(1, "A"), content="<@111> hi", clean_content="@Bob hi")
    assert _clean_body(m, 400) == "@Bob hi"


def test_clean_body_truncates_with_ellipsis():
    m = _Msg(1, _Author(1, "A"), content="x" * 50)
    out = _clean_body(m, 10)
    assert len(out) == 10 and out.endswith("…")


def test_clean_body_describes_attachment_only():
    m = _Msg(1, _Author(1, "A"), content="", attachments=[object(), object()])
    assert "attachment" in _clean_body(m, 400)


def test_clean_body_describes_embed_only():
    m = _Msg(1, _Author(1, "A"), content="", embeds=[object()])
    assert "embed" in _clean_body(m, 400)


def test_clean_body_empty_message():
    m = _Msg(1, _Author(1, "A"), content="")
    assert _clean_body(m, 400) == "[no text content]"


def test_oneline_collapses_whitespace():
    assert _oneline("a\n  b\t c") == "a b c"


def test_to_context_message_sets_flags():
    cm = _to_context_message(
        _Msg(7, _Author(123, "Dave", bot=False), content="hey"),
        me_id=999,
        requester_id=123,
        max_chars=400,
    )
    assert cm.message_id == 7
    assert cm.author_id == 123 and cm.author_name == "Dave"
    assert cm.is_requester is True and cm.is_me is False and cm.is_bot is False


# --------------------------------------------------------------------------- #
# _recent_history — async
# --------------------------------------------------------------------------- #


def test_recent_history_orders_oldest_first_and_excludes():
    # Discord yields newest-first; we expect oldest-first out, with the excluded
    # id (the reply target) dropped.
    newest_first = [
        _Msg(30, _Author(3, "C"), "c"),
        _Msg(20, _Author(2, "B"), "b"),  # will be excluded
        _Msg(10, _Author(1, "A"), "a"),
    ]
    msg = _Msg(99, _Author(9, "req"))
    msg.channel = _Channel(history_items=newest_first)

    got = asyncio.run(
        _recent_history(
            msg, me_id=999, requester_id=9, limit=5, max_chars=400, exclude_ids={20}
        )
    )
    assert [m.message_id for m in got] == [10, 30]  # oldest-first, 20 removed


def test_recent_history_limit_zero_is_noop():
    msg = _Msg(1, _Author(1, "A"))
    msg.channel = _Channel(history_items=[_Msg(2, _Author(2, "B"), "b")])
    got = asyncio.run(
        _recent_history(msg, me_id=0, requester_id=1, limit=0, max_chars=400, exclude_ids=set())
    )
    assert got == []


def test_recent_history_swallows_forbidden():
    class _Boom(_Channel):
        def history(self, *, limit, before):
            raise discord.Forbidden(Mock(status=403), "nope")

    msg = _Msg(1, _Author(1, "A"))
    msg.channel = _Boom()
    got = asyncio.run(
        _recent_history(msg, me_id=0, requester_id=1, limit=5, max_chars=400, exclude_ids=set())
    )
    assert got == []  # degrades to no history instead of raising


# --------------------------------------------------------------------------- #
# _resolve_referenced — async
# --------------------------------------------------------------------------- #


def test_resolve_referenced_none_when_not_a_reply():
    msg = _Msg(1, _Author(1, "A"))
    msg.reference = None
    got = asyncio.run(_resolve_referenced(msg, me_id=0, requester_id=1, max_chars=400))
    assert got is None


def test_resolve_referenced_uses_cached_resolved():
    resolved = _spec_message(50, _Author(123, "Dave"), content="target text")
    msg = _Msg(1, _Author(9, "req"))
    msg.reference = Mock(resolved=resolved, message_id=50)
    got = asyncio.run(_resolve_referenced(msg, me_id=0, requester_id=9, max_chars=400))
    assert got is not None
    assert got.message_id == 50 and got.author_id == 123 and got.body == "target text"


def test_resolve_referenced_deleted_returns_none():
    msg = _Msg(1, _Author(9, "req"))
    msg.reference = Mock(resolved=Mock(spec=discord.DeletedReferencedMessage), message_id=50)
    got = asyncio.run(_resolve_referenced(msg, me_id=0, requester_id=9, max_chars=400))
    assert got is None


def test_resolve_referenced_fetches_when_not_cached():
    fetched = _spec_message(77, _Author(5, "Eve"), content="fetched body")
    msg = _Msg(1, _Author(9, "req"))
    msg.reference = Mock(resolved=None, message_id=77)
    msg.channel = _Channel(fetch_result=fetched)
    got = asyncio.run(_resolve_referenced(msg, me_id=0, requester_id=9, max_chars=400))
    assert got is not None and got.message_id == 77 and got.body == "fetched body"


def test_resolve_referenced_fetch_failure_returns_none():
    msg = _Msg(1, _Author(9, "req"))
    msg.reference = Mock(resolved=None, message_id=77)
    msg.channel = _Channel(fetch_exc=discord.NotFound(Mock(status=404), "gone"))
    got = asyncio.run(_resolve_referenced(msg, me_id=0, requester_id=9, max_chars=400))
    assert got is None


# --------------------------------------------------------------------------- #
# gather_context — async integration
# --------------------------------------------------------------------------- #


def test_gather_context_combines_reply_and_history_without_duplication():
    target = _spec_message(50, _Author(123, "Dave"), content="ban worthy")
    history_items = [
        _Msg(50, _Author(123, "Dave"), "ban worthy"),  # same as reply target
        _Msg(40, _Author(111, "Alice"), "context line"),
    ]
    msg = _Msg(99, _Author(9, "req"))
    msg.reference = Mock(resolved=target, message_id=50)
    msg.channel = _Channel(history_items=history_items)
    me = Mock()
    me.id = 999

    ctx = asyncio.run(
        gather_context(msg, me, history_limit=5, include_replies=True, max_chars=400)
    )
    assert ctx.referenced is not None and ctx.referenced.message_id == 50
    # The reply target (id 50) is excluded from history so it isn't shown twice.
    assert [m.message_id for m in ctx.history] == [40]


def test_gather_context_respects_include_replies_false():
    msg = _Msg(99, _Author(9, "req"))
    msg.reference = Mock(resolved=_spec_message(50, _Author(1, "D")), message_id=50)
    msg.channel = _Channel(history_items=[])
    me = Mock()
    me.id = 999
    ctx = asyncio.run(
        gather_context(msg, me, history_limit=5, include_replies=False, max_chars=400)
    )
    assert ctx.referenced is None


import pytest  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
