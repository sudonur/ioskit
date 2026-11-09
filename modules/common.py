"""Helpers shared by all feature modules: field factories, validation, Report."""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from ioskit.core.collector import Collector, Entry, Field
from ioskit.core.result import Result, Status, derive
from ioskit.utils.formatting import format_value

NO_CELLULAR = "нет сотового модуля (TelephonyCapability = нет)"
TABLE_SOURCE = "таблица iOSKit ← ProductType"


def lockdown_field(key: str, label: str, section: str, lk_key: str, *,
                   domain: str | None = None,
                   fmt: Callable[[Any], str] = format_value,
                   sensitive: bool = False, telephony: bool = False,
                   check: Callable[[Result], Result] | None = None) -> Field:
    """Field read from lockdownd. `check` can validate/convert the raw Result."""
    def reader(ctx: Collector) -> Result[Any]:
        res = ctx.lookup(lk_key, domain)
        if res.status is Status.UNAVAILABLE and telephony:
            tel = ctx.lookup("TelephonyCapability")
            if tel.is_ok and tel.value is False:
                return Result.unavailable(NO_CELLULAR, source=res.source)
        return check(res) if check else res

    return Field(key, label, section, reader, fmt=fmt, sensitive=sensitive)


def table_reader(fn: Callable[[str | None], Result[Any]], what: str):
    """Reader for values derived from ProductType through a static table."""
    def reader(ctx: Collector) -> Result[Any]:
        pt = ctx.lookup("ProductType")
        if pt.status is Status.ERROR:  # a real failure must not look like "no data"
            return Result.fail(pt.reason or "ошибка чтения ProductType", source=TABLE_SOURCE,
                               hint=pt.hint, code=pt.code)
        if not pt.is_ok:
            return Result.unavailable(f"ProductType не получен — {what} не определить",
                                      source=TABLE_SOURCE)
        return fn(pt.value)
    return reader


def as_number(res: Result, *, lo: float | None = None, hi: float | None = None,
              integer: bool = False, accept_str: bool = False) -> Result[Any]:
    """Validate a raw value.

    not a number          -> ERROR (parse failure)
    outside [lo, hi]      -> UNAVAILABLE (implausible value is not trusted)
    """
    if not res.is_ok:
        return res
    v = res.value
    if isinstance(v, bool):
        return Result.fail(f"ожидалось число, получено bool ({v})", source=res.source)
    if isinstance(v, str) and accept_str and v.strip().isdigit():
        v = int(v)
    if not isinstance(v, (int, float)):
        return Result.fail(f"не удалось разобрать число: {v!r}", source=res.source)
    if integer and isinstance(v, float) and v.is_integer():
        v = int(v)
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        return Result.unavailable(f"значение вне правдоподобного диапазона: {v:g}", source=res.source)
    return Result.ok(v, res.source)


@dataclass(slots=True)
class Report:
    entries: list[Entry]
    aborted: Result[Any] | None = None
    section_order: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    extra: Any = None  # records for module-specific tables (logs, crashes, apps)

    def get(self, key: str) -> Entry | None:
        return next((e for e in self.entries if e.field.key == key), None)

    def sections(self) -> list[tuple[str, list[Entry]]]:
        names = list(self.section_order)
        for e in self.entries:  # sections created at runtime go last
            if e.field.section not in names:
                names.append(e.field.section)
        out = []
        for name in names:
            items = [e for e in self.entries if e.field.section == name]
            if items:
                out.append((name, items))
        return out

    def counts(self) -> dict[Status, int]:
        c = {s: 0 for s in Status}
        for e in self.entries:
            c[e.result.status] += 1
        return c


# ---- reuse of existing fields ---------------------------------------------

def pick(fields: Sequence[Field], key: str, **changes: Any) -> Field:
    """An existing Field (same reader, same cache) optionally moved/renamed."""
    f = next(f for f in fields if f.key == key)
    return dataclasses.replace(f, **changes) if changes else f


def lk(key: str, domain: str | None = None) -> Callable[[Collector], Result[Any]]:
    """Reader of one lockdown value."""
    return lambda ctx: ctx.lookup(key, domain)


def ios_major(ctx: Collector) -> int | None:
    res = ctx.lookup("ProductVersion")
    m = re.match(r"\s*(\d+)", str(res.value)) if res.is_ok else None
    return int(m.group(1)) if m else None


# ---- findings: consistency checks and warning rules -----------------------
# A finding value is (level, message), level in {"ok", "warn"}. It exists ONLY
# when every input is OK: missing data is UNAVAILABLE, never a passed check.

Finding = tuple[str, str]


def finding(ok: bool, good: str, bad: str) -> Finding:
    return ("ok", good) if ok else ("warn", bad)


def format_finding(v: Finding) -> str:
    return ("норма — " if v[0] == "ok" else "ВНИМАНИЕ — ") + v[1]


def check_field(key: str, label: str, section: str, inputs: Sequence[Callable[[Collector], Result[Any]]],
                fn: Callable[..., Finding], *, source: str,
                formula: "str | Callable[..., str]") -> Field:
    def reader(ctx: Collector) -> Result[Any]:
        return derive(fn, *[r(ctx) for r in inputs], source=source, formula=formula)
    return Field(key, label, section, reader, fmt=format_finding)


def collect_warnings(entries: Sequence[Entry]) -> tuple[str, ...]:
    out = []
    for e in entries:
        v = e.result.value if e.result.is_ok else None
        if isinstance(v, tuple) and len(v) == 2 and v[0] == "warn":
            out.append(f"{e.field.label}: {v[1]}")
    return tuple(out)


def static_field(key: str, label: str, section: str, res: Result[Any],
                 fmt: Callable[[Any], str] = format_value) -> Field:
    """A field whose Result is already known (computed from captured data)."""
    return Field(key, label, section, lambda ctx, r=res: r, fmt=fmt)
