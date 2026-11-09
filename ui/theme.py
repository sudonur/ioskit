"""Terminal theme: red brand, cyan info, green ok, yellow warn, red error."""
from __future__ import annotations

from rich.console import Console
from rich.theme import Theme

from ioskit.core.result import Status

THEME = Theme({
    "brand": "bold red", "info": "cyan", "label": "bold cyan",
    "ok": "green", "warn": "yellow", "err": "bold red", "muted": "dim",
})


def make_console() -> Console:
    return Console(theme=THEME, highlight=False)


def unicode_ok(console: Console) -> bool:
    return (console.encoding or "").lower().startswith("utf")


def symbol(status: Status, console: Console) -> tuple[str, str]:
    """(symbol, style) for a Result status; ASCII fallback for non-UTF terminals."""
    u = unicode_ok(console)
    table = {
        Status.OK: ("✔" if u else "OK", "ok"),
        Status.UNAVAILABLE: ("○" if u else "--", "warn"),
        Status.UNSUPPORTED: ("⚠" if u else "!!", "warn"),
        Status.ERROR: ("✖" if u else "XX", "err"),
    }
    return table[status]
