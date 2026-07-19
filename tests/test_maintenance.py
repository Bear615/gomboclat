"""Tests for the pure, I/O-free maintenance helpers.

The git/pip helpers shell out and aren't unit-tested here, but the changelog
extractor is pure string handling and is exercised directly.
"""

from __future__ import annotations

from bot.maintenance import top_changelog_section


def test_extracts_first_section_only():
    text = (
        "# Changelog\n\n"
        "## Unreleased\n\n"
        "### Added\n- feature A\n- feature B\n\n"
        "## 1.0.0\n\n- old thing\n"
    )
    out = top_changelog_section(text)
    assert out.startswith("## Unreleased")
    assert "feature A" in out
    assert "feature B" in out
    assert "old thing" not in out  # stopped before the next section
    assert "1.0.0" not in out


def test_skips_preamble_before_first_heading():
    text = "Some intro line\nanother\n\n## v2\n- change\n"
    out = top_changelog_section(text)
    assert out.startswith("## v2")
    assert "intro" not in out


def test_single_section_returns_whole_body():
    text = "## Only\n- a\n- b\n"
    assert top_changelog_section(text) == "## Only\n- a\n- b"


def test_empty_or_headingless_returns_empty():
    assert top_changelog_section("") == ""
    assert top_changelog_section("no headings here\njust text") == ""


def test_result_is_stripped():
    text = "## Head\n\n- x\n\n\n"
    assert top_changelog_section(text) == "## Head\n\n- x"
