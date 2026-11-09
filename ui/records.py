"""Tables of records (log lines, crash reports, apps). Cells are Text objects, never
markup strings, so '[' in process names or messages cannot break rendering."""
from __future__ import annotations

from typing import Sequence

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from ioskit.utils.formatting import format_bytes

DASH = "—"
_LEVEL_STYLE = {"emergency": "err", "alert": "err", "critical": "err", "fault": "err", "error": "err",
                "warning": "warn", "notice": "info", "info": "info", "debug": "muted"}


def level_style(level: str | None) -> str:
    return _LEVEL_STYLE.get((level or "").lower(), "muted")


def _cell(value, style: str = "info") -> Text:
    return Text(DASH, style="muted") if value in (None, "") else Text(str(value), style=style)


def _table(title: str, caption: str) -> Table:
    return Table(title=title, caption=caption, title_justify="left", title_style="brand",
                 caption_justify="left", caption_style="muted", box=box.SIMPLE_HEAVY,
                 header_style="label", expand=True, pad_edge=False)


def log_line(rec) -> Text:
    """One log record as a single colored line (used by --follow)."""
    t = Text()
    if not rec.parsed:
        t.append(rec.raw, style="muted")
        return t
    t.append(f"{rec.timestamp} ", style="muted")
    t.append(f"{rec.process}[{rec.pid}] ", style="label")
    t.append(f"<{rec.level}> ", style=level_style(rec.level))
    t.append(rec.message)
    return t


def logs_table(records: Sequence, console: Console, *, source: str) -> Table:
    wide = console.width >= 100
    table = _table("Записи журнала", f"Источник: {source}; время — как в журнале устройства (без года)")
    table.add_column("Время", no_wrap=True, width=15)
    table.add_column("Процесс", overflow="fold", width=22 if wide else 16)
    table.add_column("Уровень", no_wrap=True, width=9)
    table.add_column("Сообщение", ratio=1, overflow="fold")
    for r in records:
        if r.parsed:
            table.add_row(_cell(r.timestamp, "muted"), _cell(f"{r.process}[{r.pid}]", "label"),
                          _cell(r.level, level_style(r.level)), _cell(r.message, "default"))
        else:
            table.add_row(_cell(None), _cell(None), _cell(None), _cell(r.raw, "muted"))
    return table


def crashes_table(records: Sequence, console: Console, *, source: str) -> Table:
    w = console.width
    table = _table("Отчёты", f"Источник: {source}; тип — по имени файла и содержимому")
    table.add_column("Время", no_wrap=True, width=19)
    table.add_column("Процесс", overflow="fold")
    table.add_column("Тип", no_wrap=True, width=10)
    if w >= 90:
        table.add_column("Исключение / причина", overflow="fold")
    if w >= 120:
        table.add_column("iOS", overflow="fold", width=22)
        table.add_column("Размер", no_wrap=True, width=10, justify="right")
    for r in records:
        reason = r.details.get("exception_type") or r.details.get("termination") or r.details.get("largest_process")
        style = {"panic": "err", "crash": "warn", "jetsam": "warn"}.get(r.kind, "info")
        row = [_cell(r.timestamp or r.mtime_text, "muted"), _cell(r.process or r.name, "label"), _cell(r.kind, style)]
        if w >= 90:
            row.append(_cell(reason or r.detail_note, "default"))
        if w >= 120:
            row += [_cell(r.details.get("os_version"), "muted"),
                    _cell(format_bytes(r.size).split(" (")[0] if r.size is not None else None, "muted")]
        table.add_row(*row)
    return table


def apps_table(records: Sequence, console: Console, *, source: str, show_sizes: bool) -> Table:
    w = console.width
    table = _table("Приложения", f"Источник: {source}; «—» = iOS не вернул значение")
    table.add_column("Название", overflow="fold")
    table.add_column("Bundle ID", overflow="fold")
    if w >= 80:
        table.add_column("Версия", no_wrap=True)
        table.add_column("Тип", no_wrap=True)
    if show_sizes:
        table.add_column("Размер", no_wrap=True, justify="right")
    for r in records:
        row = [_cell(r.name, "default"), _cell(r.bundle_id, "label")]
        if w >= 80:
            row += [_cell(r.version, "info"), _cell(r.app_type, "muted")]
        if show_sizes:
            total = r.total_size
            row.append(_cell(format_bytes(total).split(" (")[0] if total is not None else None, "info"))
        table.add_row(*row)
    return table
