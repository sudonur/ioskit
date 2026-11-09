"""Reusable UI blocks: tables, panels, status cells (responsive to width)."""
from __future__ import annotations

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from typing import TYPE_CHECKING, Sequence

from ioskit.core.collector import Entry
from ioskit.core.connection import Probe
from ioskit.core.result import Result, Status
from ioskit.ui.theme import symbol, unicode_ok
from ioskit.utils.errors import ConnState, Diagnosis

if TYPE_CHECKING:  # avoid a ui -> modules import at runtime
    from ioskit.modules.common import Report


def result_cell(res: Result, console: Console, fmt=str) -> Text:
    """Value for OK; otherwise a distinct marker for each non-OK status."""
    sym, style = symbol(res.status, console)
    if res.is_ok:
        t = Text(fmt(res.value), style="info")
        if res.derived:
            t.append(" (derived)", style="muted")
        return t
    word = {Status.UNAVAILABLE: "недоступно", Status.UNSUPPORTED: "не поддерживается",
            Status.ERROR: "ошибка"}[res.status]
    return Text(f"{sym} {word}", style=style)


def state_cell(state: ConnState, console: Console) -> Text:
    ok, _ = symbol(Status.OK, console)
    if state is ConnState.CONNECTED:
        return Text(f"{ok} подключено", style="ok")
    warn_states = {ConnState.NOT_PAIRED, ConnState.PAIRING_PENDING, ConnState.PASSWORD_REQUIRED}
    sym, style = symbol(Status.UNSUPPORTED if state in warn_states else Status.ERROR, console)
    label = {ConnState.NOT_PAIRED: "не сопряжено", ConnState.PAIRING_PENDING: "ждёт «Доверять»",
             ConnState.PAIRING_DENIED: "сопряжение отклонено",
             ConnState.PASSWORD_REQUIRED: "заблокировано"}.get(state, state.value)
    return Text(f"{sym} {label}", style=style)


def devices_table(probes: list[Probe], console: Console) -> Table:
    w = console.width
    table = Table(box=box.SIMPLE_HEAVY, header_style="brand", expand=False, pad_edge=False)
    table.add_column("#", justify="right", style="muted", no_wrap=True)
    table.add_column("Модель", no_wrap=True)
    if w >= 100:
        table.add_column("ProductType", no_wrap=True)
    table.add_column("iOS", no_wrap=True)
    if w >= 80:
        table.add_column("Имя", overflow="ellipsis", max_width=20)
    table.add_column("UDID", overflow="fold", min_width=12)
    table.add_column("Состояние", no_wrap=True)
    for i, p in enumerate(probes, 1):
        row = [str(i), result_cell(p.model, console)]
        if w >= 100:
            row.append(result_cell(p.product_type, console))
        row.append(result_cell(p.os_version, console))
        if w >= 80:
            row.append(result_cell(p.device_name, console))
        row += [Text(p.udid, style="info"), state_cell(p.state, console)]
        table.add_row(*row)
    return table


def diagnosis_panel(diag: Diagnosis, console: Console, detail: str | None = None) -> Panel:
    err_states = {ConnState.NO_USBMUXD, ConnState.DISCONNECTED, ConnState.MISSING_LIB,
                  ConnState.UNKNOWN, ConnState.PAIRING_DENIED}
    style = "err" if diag.state in err_states else "warn"
    body = Text()
    body.append(diag.title + "\n", style=style)
    if diag.hint:
        body.append("→ " + diag.hint, style="info")
    if detail:
        body.append("\n" + detail, style="muted")
    return Panel(body, border_style=style, box=box.ROUNDED, expand=False)


def message_panel(text: str, console: Console, *, style: str = "warn", title: str | None = None) -> Panel:
    return Panel(Text(text, style=style), title=title, border_style=style,
                 box=box.ROUNDED, expand=False)


# ---- generic parameter tables (shared by all modules) -----------------------

def status_cell(res: Result, console: Console, *, compact: bool = False) -> Text:
    sym, style = symbol(res.status, console)
    if compact:
        return Text(sym, style=style)
    word = {Status.OK: "вычислено" if res.derived else "получено",
            Status.UNAVAILABLE: "недоступно",
            Status.UNSUPPORTED: "не поддерж.",
            Status.ERROR: "ошибка"}[res.status]
    return Text(f"{sym} {word}", style=style)


def entries_table(title: str, entries: Sequence[Entry], console: Console, *,
                  mask: bool = False) -> Table:
    """Parameter | Value | Source | Status. On narrow terminals the source moves
    under the value and the status shrinks to a symbol, nothing is dropped.
    Derived values show their formula under the value."""
    w = console.width
    wide, narrow = w >= 110, w < 70
    table = Table(title=title, title_justify="left", title_style="brand",
                  box=box.SIMPLE_HEAVY, header_style="label", expand=True, pad_edge=False)
    table.add_column("Параметр", width=24 if wide else 20 if w >= 80 else 16,
                     no_wrap=False, overflow="fold")
    table.add_column("Значение", ratio=1, overflow="fold")
    if wide:
        table.add_column("Источник", width=34, overflow="fold")
    table.add_column("Статус", width=3 if narrow else 14, no_wrap=True)
    for e in entries:
        res = e.result
        value = Text()
        if res.is_ok:
            value.append(e.display(mask=mask) or "—", style="info")
            if res.formula:
                value.append("\n= " + res.formula, style="muted")
        else:
            _, st = symbol(res.status, console)
            value.append("— " + (res.reason or "нет данных"), style=st)
        if not wide and res.source:
            value.append("\n" + res.source, style="muted")
        row = [Text(e.field.label), value]
        if wide:
            row.append(Text(res.source or "—", style="muted"))
        row.append(status_cell(res, console, compact=narrow))
        table.add_row(*row)
    return table


def header_panel(report: "Report", console: Console, items: Sequence[tuple], title: str = "Устройство") -> Panel:
    """Key facts. An item is (label, key) or (label, (key1, key2...)): first OK key wins."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="label")
    grid.add_column()
    for label, keys in items:
        keys = (keys,) if isinstance(keys, str) else tuple(keys)
        text = None
        for k in keys:
            e = report.get(k)
            if e is not None and e.result.is_ok:
                text = e.display()
                break
        grid.add_row(label, Text(text, style="info") if text else Text("— нет данных", style="warn"))
    return Panel(grid, title=title, title_align="left", border_style="brand",
                 box=box.ROUNDED, expand=False)


def notes_panel(report: "Report", console: Console) -> Panel | None:
    if not report.notes:
        return None
    body = Text()
    for i, note in enumerate(report.notes):
        body.append(("\n" if i else "") + "• " + note, style="muted")
    return Panel(body, title="Примечания", title_align="left", border_style="muted",
                 box=box.ROUNDED, expand=False)


def summary_panel(report: "Report", console: Console) -> Panel:
    c = report.counts()
    body = Text()
    for status, name in ((Status.OK, "получено"), (Status.UNAVAILABLE, "недоступно"),
                         (Status.UNSUPPORTED, "не поддерживается"), (Status.ERROR, "ошибок")):
        sym, style = symbol(status, console)
        body.append(f"{sym} {name}: {c[status]}   ", style=style)
    body.append("\n«Недоступно» значит, что устройство не отдаёт значение; это не ошибка.",
                style="muted")
    errors = [e for e in report.entries if e.result.status is Status.ERROR]
    if report.aborted is not None:
        a = report.aborted
        body.append(f"\nСбор прерван: {a.reason}", style="err")
        if a.hint:
            body.append(f"\n→ {a.hint}", style="info")
    elif errors:
        body.append(f"\nПервая ошибка ({errors[0].field.label}): {errors[0].result.reason}",
                    style="err")
        if errors[0].result.hint:
            body.append(f"\n→ {errors[0].result.hint}", style="info")
    style = "err" if (report.aborted or errors) else "ok"
    return Panel(body, title="Итог", title_align="left", border_style=style,
                 box=box.ROUNDED, expand=False)


def warnings_panel(report: "Report", console: Console) -> Panel | None:
    """Yellow panel with warnings produced by explicit rules (None when there are none)."""
    if not report.warnings:
        return None
    mark = "⚠" if unicode_ok(console) else "!"
    body = Text()
    for i, w in enumerate(report.warnings):
        body.append(("\n" if i else "") + f"{mark} {w}", style="warn")
    return Panel(body, title="Предупреждения", title_align="left", border_style="warn",
                 box=box.ROUNDED, expand=False)
