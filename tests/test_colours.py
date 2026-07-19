"""Tests for colour resolution + validation."""

from __future__ import annotations

import discord
import pytest

from bot.colours import ColourError, is_valid_colour, resolve_colour


def test_named_colour():
    assert resolve_colour("purple") == discord.Colour(0xA020F0)


def test_named_colour_case_and_space_insensitive():
    assert resolve_colour("  Purple ") == discord.Colour(0xA020F0)


def test_hex_with_hash():
    assert resolve_colour("#A020F0") == discord.Colour(0xA020F0)


def test_hex_without_hash():
    assert resolve_colour("a020f0") == discord.Colour(0xA020F0)


def test_hex_with_0x_prefix():
    assert resolve_colour("0xA020F0") == discord.Colour(0xA020F0)


def test_three_digit_hex_shorthand():
    assert resolve_colour("#f0f") == discord.Colour(0xFF00FF)
    assert resolve_colour("abc") == discord.Colour(0xAABBCC)


def test_rgb_form():
    assert resolve_colour("rgb(160, 32, 240)") == discord.Colour(0xA020F0)
    assert resolve_colour("RGB(0,0,0)") == discord.Colour.from_rgb(0, 0, 0)


def test_rgb_out_of_range_rejected():
    with pytest.raises(ColourError):
        resolve_colour("rgb(256, 0, 0)")


def test_random_colour_resolves():
    # Just needs to produce a valid Colour without raising.
    assert isinstance(resolve_colour("random"), discord.Colour)


def test_newly_added_named_colours():
    for name in ("coral", "turquoise", "lavender", "charcoal", "mint"):
        assert is_valid_colour(name), name


def test_invalid_colour_rejected():
    with pytest.raises(ColourError):
        resolve_colour("not-a-colour")


def test_invalid_hex_length_rejected():
    with pytest.raises(ColourError):
        resolve_colour("#12345")  # 5 digits


def test_invalid_rgb_rejected():
    with pytest.raises(ColourError):
        resolve_colour("rgb(1, 2)")  # too few components


def test_empty_rejected():
    with pytest.raises(ColourError):
        resolve_colour("")


def test_is_valid_colour():
    assert is_valid_colour("teal")
    assert is_valid_colour("#000000")
    assert not is_valid_colour("chartreuse-ish")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
