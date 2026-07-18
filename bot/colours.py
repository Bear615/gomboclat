"""Colour handling: a small name->hex map plus validation.

Pure module. The AI may supply a colour as a named colour (looked up here) or as
an explicit hex string. Either way we *validate* it parses to a real
``discord.Colour`` before anything downstream uses it; anything that doesn't
parse is rejected rather than silently defaulted.
"""

from __future__ import annotations

import re

import discord

# name -> hex. Seeded with a standard, readable palette. Extend freely; keys are
# matched case-insensitively and whitespace/hyphens are ignored.
NAMED_COLOURS: dict[str, str] = {
    "red": "#E74C3C",
    "crimson": "#DC143C",
    "orange": "#E67E22",
    "amber": "#FFBF00",
    "yellow": "#F1C40F",
    "gold": "#FFD700",
    "lime": "#A2E82C",
    "green": "#2ECC71",
    "forest": "#228B22",
    "teal": "#1ABC9C",
    "cyan": "#00BCD4",
    "blue": "#3498DB",
    "navy": "#34495E",
    "indigo": "#4B0082",
    "purple": "#A020F0",
    "violet": "#8E44AD",
    "magenta": "#FF00FF",
    "pink": "#FF69B4",
    "rose": "#F16A9A",
    "brown": "#8B5A2B",
    "black": "#010101",  # 0x000000 renders as "no colour" in Discord; nudge it.
    "white": "#FFFFFF",
    "grey": "#95A5A6",
    "gray": "#95A5A6",
    "silver": "#BDC3C7",
    "blurple": "#5865F2",
}

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")


def _normalise(name: str) -> str:
    return re.sub(r"[\s_-]+", "", name).strip().lower()


class ColourError(ValueError):
    """Raised when a colour string cannot be resolved to a valid colour."""


def resolve_colour(value: str) -> discord.Colour:
    """Resolve a named colour or a hex string to a ``discord.Colour``.

    Raises ``ColourError`` if it cannot be parsed. Never returns a fallback --
    the caller must handle rejection explicitly.
    """
    if value is None:
        raise ColourError("No colour was given.")

    raw = str(value).strip()
    if not raw:
        raise ColourError("Empty colour string.")

    # Named colour?
    key = _normalise(raw)
    if key in NAMED_COLOURS:
        raw = NAMED_COLOURS[key]

    # Hex path.
    if _HEX_RE.match(raw):
        hex_digits = raw.lstrip("#")
        try:
            return discord.Colour(int(hex_digits, 16))
        except ValueError as exc:  # pragma: no cover - regex already guards this
            raise ColourError(f"Could not parse hex colour {value!r}.") from exc

    raise ColourError(
        f"{value!r} isn't a colour I recognise. Use a name like 'purple' or a "
        f"hex code like '#A020F0'."
    )


def is_valid_colour(value: str) -> bool:
    """Non-raising check, handy for validation branches."""
    try:
        resolve_colour(value)
        return True
    except ColourError:
        return False
