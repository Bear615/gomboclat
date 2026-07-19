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
    "maroon": "#800000",
    "scarlet": "#FF2400",
    "orange": "#E67E22",
    "tangerine": "#F28500",
    "coral": "#FF7F50",
    "salmon": "#FA8072",
    "peach": "#FFCBA4",
    "amber": "#FFBF00",
    "yellow": "#F1C40F",
    "gold": "#FFD700",
    "mustard": "#FFDB58",
    "lime": "#A2E82C",
    "chartreuse": "#7FFF00",
    "olive": "#808000",
    "green": "#2ECC71",
    "forest": "#228B22",
    "emerald": "#2ECC71",
    "mint": "#98FF98",
    "sage": "#9CAF88",
    "teal": "#1ABC9C",
    "turquoise": "#40E0D0",
    "cyan": "#00BCD4",
    "aqua": "#00FFFF",
    "sky": "#87CEEB",
    "azure": "#007FFF",
    "blue": "#3498DB",
    "cobalt": "#0047AB",
    "navy": "#34495E",
    "indigo": "#4B0082",
    "periwinkle": "#CCCCFF",
    "lavender": "#B57EDC",
    "purple": "#A020F0",
    "violet": "#8E44AD",
    "plum": "#8E4585",
    "orchid": "#DA70D6",
    "magenta": "#FF00FF",
    "fuchsia": "#FF00FF",
    "pink": "#FF69B4",
    "hotpink": "#FF1493",
    "rose": "#F16A9A",
    "blush": "#DE5D83",
    "brown": "#8B5A2B",
    "chocolate": "#7B3F00",
    "tan": "#D2B48C",
    "beige": "#D8C3A5",
    "cream": "#FFFDD0",
    "black": "#010101",  # 0x000000 renders as "no colour" in Discord; nudge it.
    "white": "#FFFFFF",
    "grey": "#95A5A6",
    "gray": "#95A5A6",
    "slate": "#708090",
    "charcoal": "#36454F",
    "silver": "#BDC3C7",
    "blurple": "#5865F2",
}

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")
# 3-digit shorthand like #f0f -> #ff00ff.
_HEX3_RE = re.compile(r"^#?[0-9a-fA-F]{3}$")
# rgb(r, g, b) with 0-255 components.
_RGB_RE = re.compile(r"^rgb\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)$")


def _normalise(name: str) -> str:
    return re.sub(r"[\s_-]+", "", name).strip().lower()


class ColourError(ValueError):
    """Raised when a colour string cannot be resolved to a valid colour."""


def resolve_colour(value: str) -> discord.Colour:
    """Resolve a colour string to a ``discord.Colour``.

    Accepts, in order: a named colour (``purple``); the special value
    ``random``; a six-digit hex (``#A020F0``, ``0xA020F0``, ``a020f0``); a
    three-digit shorthand hex (``#f0f``); and CSS-style ``rgb(r, g, b)``.

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
    if key == "random":
        return discord.Colour.random()
    if key in NAMED_COLOURS:
        raw = NAMED_COLOURS[key]

    # Normalise a leading 0x to a hash so the hex paths below accept it.
    if raw[:2].lower() == "0x":
        raw = "#" + raw[2:]

    # Six-digit hex.
    if _HEX_RE.match(raw):
        hex_digits = raw.lstrip("#")
        try:
            return discord.Colour(int(hex_digits, 16))
        except ValueError as exc:  # pragma: no cover - regex already guards this
            raise ColourError(f"Could not parse hex colour {value!r}.") from exc

    # Three-digit shorthand hex: #abc -> #aabbcc.
    if _HEX3_RE.match(raw):
        short = raw.lstrip("#")
        expanded = "".join(ch * 2 for ch in short)
        return discord.Colour(int(expanded, 16))

    # rgb(r, g, b).
    rgb = _RGB_RE.match(raw.lower())
    if rgb:
        r, g, b = (int(rgb.group(i)) for i in (1, 2, 3))
        if max(r, g, b) > 255:
            raise ColourError(
                f"RGB components must be 0-255; got {value!r}."
            )
        return discord.Colour.from_rgb(r, g, b)

    raise ColourError(
        f"{value!r} isn't a colour I recognise. Use a name like 'purple', a hex "
        f"code like '#A020F0' or '#f0f', 'rgb(160, 32, 240)', or 'random'."
    )


def is_valid_colour(value: str) -> bool:
    """Non-raising check, handy for validation branches."""
    try:
        resolve_colour(value)
        return True
    except ColourError:
        return False
