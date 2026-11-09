"""Verbose tracing (--verbose): data sources and errors go to stderr."""
from __future__ import annotations

from rich.console import Console
from rich.text import Text

_console = Console(stderr=True, highlight=False)
_enabled = False


def set_verbose(value: bool) -> None:
    global _enabled
    _enabled = _enabled or value  # once enabled, stays enabled for this run


def is_verbose() -> bool:
    return _enabled


def trace(message: str, *, source: str | None = None) -> None:
    if not _enabled:
        return
    line = Text()
    line.append("  · ", style="dim")
    if source:
        line.append(f"[{source}] ", style="cyan")
    line.append(message, style="dim")
    _console.print(line)


def trace_error(exc: BaseException, *, source: str | None = None) -> None:
    if not _enabled:
        return
    line = Text()
    line.append("  ✖ ", style="bold red")
    if source:
        line.append(f"[{source}] ", style="cyan")
    line.append(f"{type(exc).__name__}: {exc}", style="red")
    _console.print(line)
