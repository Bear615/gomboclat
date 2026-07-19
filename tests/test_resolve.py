"""Tests for the pure name-matching helper used to resolve members/roles/channels.

``match_by_name`` is Discord-free by design, so we can exercise its exact-match,
substring-fallback, and ambiguity behaviour with plain string stand-ins.
"""

from __future__ import annotations

import pytest

from bot.tools import match_by_name


def test_exact_match_wins():
    cands = [("A", ["Dave"]), ("B", ["Dave the Great"])]
    # "dave" is an exact match for A, even though it is also a substring of B.
    assert match_by_name("dave", cands, kind="member") == "A"


def test_case_insensitive_exact():
    cands = [("A", ["Moderators"])]
    assert match_by_name("moderators", cands, kind="role") == "A"


def test_substring_fallback_when_unique():
    cands = [("A", ["Dave the Great"]), ("B", ["Bob"])]
    assert match_by_name("dave", cands, kind="member") == "A"


def test_leading_at_and_hash_stripped():
    cands = [("A", ["announcements"])]
    assert match_by_name("#announcements", cands, kind="channel") == "A"
    assert match_by_name("@announcements", cands, kind="channel") == "A"


def test_exact_ambiguity_uses_id():
    cands = [("A", ["Dupe"]), ("B", ["Dupe"])]
    with pytest.raises(ValueError, match="ambiguous"):
        match_by_name("dupe", cands, kind="role")


def test_substring_ambiguity_asks_to_be_specific():
    cands = [("A", ["Team Red"]), ("B", ["Team Blue"])]
    with pytest.raises(ValueError, match="more specific"):
        match_by_name("team", cands, kind="role")


def test_no_match_raises():
    cands = [("A", ["Alice"])]
    with pytest.raises(ValueError, match="couldn't find"):
        match_by_name("zzz", cands, kind="member")


def test_empty_names_are_ignored():
    # A member with no nickname contributes empty strings that must not match.
    cands = [("A", ["Alice", "", "Alice"]), ("B", ["Bob", ""])]
    assert match_by_name("bob", cands, kind="member") == "B"


def test_id_phrase_appears_in_ambiguity_message():
    cands = [("A", ["Dupe"]), ("B", ["Dupe"])]
    with pytest.raises(ValueError, match="their ID"):
        match_by_name("dupe", cands, kind="member", id_phrase="their ID")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
