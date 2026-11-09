"""ASCII logo "iOSKit". Built from glyphs and padded per column,
so alignment is guaranteed by construction."""
from __future__ import annotations

from rich.console import Console, Group
from rich.padding import Padding
from rich.text import Text

from ioskit import __version__

GLYPHS: dict[str, list[str]] = {
    "i": [" _ ", "(_)", "| |", "| |", "|_|"],
    "O": ["  ___  ", " / _ \\ ", "| | | |", "| |_| |", " \\___/ "],
    "S": [" ____  ", "/ ___| ", "\\___ \\ ", " ___) |", "|____/ "],
    "K": [" _  __", "| |/ /", "| ' / ", "| . \\ ", "|_|\\_\\"],
    "t": [" _   ", "| |_ ", "| __|", "| |_ ", " \\__|"],
}
HEIGHT = 5


def build_logo_lines(text: str = "iOSKit") -> list[str]:
    rows = [""] * HEIGHT
    for n, ch in enumerate(text):
        glyph = GLYPHS[ch]
        width = max(len(r) for r in glyph)
        gap = " " if n else ""  # one-space gap between letters
        for i in range(HEIGHT):
            rows[i] += gap + glyph[i].ljust(width)
    return rows


def logo_width() -> int:
    return max(len(r) for r in build_logo_lines())


def render_logo(console: Console):
    tagline = Text("Advanced iOS Device Diagnostics Toolkit", style="info")
    tagline.append(f"  v{__version__}", style="muted")
    if console.width < logo_width() + 2:  # very narrow terminal -> compact form
        return Group(Text("iOSKit", style="brand"), tagline)
    lines = [Text(r.rstrip(), style="brand") for r in build_logo_lines()]
    return Padding(Group(*lines, tagline), (1, 0, 1, 1))
