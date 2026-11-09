"""Crash reports kept by the device (crashreportcopymobile, read through AFC).

Read-only: iOSKit never deletes or moves anything on the device (idevicecrashreport,
which deletes reports unless run with -k, is deliberately not used).

What is shown is only what the files really contain:
  * file name  -> process, timestamp (device local time), coarse kind
                  (JetsamEvent*, panic*, stacks*);
  * file body  -> exception / termination / OS version / bundle id, when readable.
bug_type numbers are shown raw (their meaning is not documented, so it is not guessed).
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ioskit.core.collector import Collector, Entry, ProgressCb
from ioskit.core.device import Device
from ioskit.core.result import Result
from ioskit.modules.common import Report, static_field
from ioskit.utils.formatting import format_bytes
from ioskit.utils.trace import trace

S_SRC = "Доступ к отчётам (crashreportcopymobile / AFC)"
SECTIONS = (S_SRC,)
TITLE = "Отчёты о сбоях"
HEADER = (("Файлов на устройстве", "total_files"), ("Показано", "shown"),
          ("Типы (по имени)", "kinds_all"), ("Новейший", "newest"))

SRC_LIST = "crashreportcopymobile:ls"
SRC_READ = "crashreportcopymobile:read"
DEFAULT_MAX_BYTES = 2_000_000
REPEAT_WARN = 3   # iOSKit heuristic: the same process appears this many times

NOTES = (
    "Доступны только отчёты, которые устройство сохранило; их число и срок хранения ограничены iOS и "
    "зависят от настроек аналитики на устройстве.",
    "Тип определяется по имени файла (JetsamEvent, panic, stacks) и по содержимому (исключение/завершение); "
    "числа bug_type показываются как есть, без расшифровки.",
    "iOSKit только читает отчёты и ничего не удаляет и не переносит на устройстве.",
    "Отчёты содержат имена приложений и системные данные; просмотрите их перед передачей другим.",
)

_TS_IN_NAME = re.compile(r"(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})(\d{2})")


@dataclass(slots=True)
class CrashRecord:
    path: str
    name: str
    source: str
    size: int | None = None
    mtime: datetime | None = None
    timestamp: str | None = None          # from the file name, device local time
    process: str | None = None            # from the file name
    kind: str = "unknown"                 # crash | jetsam | panic | stackshot | unknown
    kind_basis: str = "не определён"
    archived: bool = False
    details: dict[str, Any] = field(default_factory=dict)
    detail_note: str | None = None        # why details are missing (skipped / error / not read)

    @property
    def mtime_text(self) -> str | None:
        return self.mtime.strftime("%Y-%m-%d %H:%M:%S UTC") if self.mtime else None


# ---- pure parsers (unit-tested) -------------------------------------------------

def parse_name(name: str) -> tuple[str | None, str | None]:
    """(process, 'YYYY-MM-DD HH:MM:SS') from names like 'SpringBoard-2024-01-02-101112.ips'."""
    m = _TS_IN_NAME.search(name)
    if not m:
        return None, None
    process = name[:m.start()].rstrip("-_ .") or None
    y, mo, d, h, mi, s = m.groups()
    return process, f"{y}-{mo}-{d} {h}:{mi}:{s}"


def name_kind(name: str) -> tuple[str, str] | None:
    low = name.lower()
    if low.startswith("jetsamevent"):
        return "jetsam", "имя файла"
    if "panic" in low:
        return "panic", "имя файла"
    if low.startswith("stacks"):
        return "stackshot", "имя файла"
    return None


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


_TEXT_KEYS = {
    "incident_id": r"Incident Identifier:\s*(\S+)",
    "process": r"^Process:\s*(.+?)\s*\[\d+\]",
    "bundle_id": r"^Identifier:\s*(\S+)",
    "app_version": r"^Version:\s*(.+)$",
    "os_version": r"^OS Version:\s*(.+)$",
    "model": r"^Hardware Model:\s*(\S+)",
    "exception_type": r"^Exception Type:\s*(.+)$",
    "termination": r"^Termination Reason:\s*(.+)$",
    "timestamp": r"^Date/Time:\s*(.+)$",
}


def parse_report(text: str) -> dict[str, Any]:
    """Extract known fields from a .ips (JSON header + body) or classic text report.
    Returns only keys that are really present."""
    out: dict[str, Any] = {}
    first, _, rest = text.partition("\n")
    header = _json(first) if first.lstrip().startswith("{") else None
    if isinstance(header, dict):
        for src, dst in (("app_name", "process"), ("name", "process"), ("bundleID", "bundle_id"),
                         ("app_version", "app_version"), ("os_version", "os_version"),
                         ("bug_type", "bug_type"), ("incident_id", "incident_id"), ("timestamp", "timestamp")):
            if header.get(src) not in (None, "") and dst not in out:
                out[dst] = header[src]
        body = _json(rest) if rest.strip() else None
        if isinstance(body, dict):
            exc = body.get("exception")
            if isinstance(exc, dict):
                parts = [str(exc[k]) for k in ("type", "signal") if exc.get(k)]
                if parts:
                    out["exception_type"] = " / ".join(parts)
            term = body.get("termination")
            if isinstance(term, dict):
                parts = [str(term[k]) for k in ("namespace", "indicator") if term.get(k)]
                if parts:
                    out["termination"] = " ".join(parts)
            if body.get("modelCode"):
                out["model"] = body["modelCode"]
            if body.get("procName") and "process" not in out:
                out["process"] = body["procName"]
            if body.get("largestProcess"):
                out["largest_process"] = body["largestProcess"]
        return out
    for key, pattern in _TEXT_KEYS.items():
        m = re.search(pattern, text, re.MULTILINE)
        if m:
            out[key] = m.group(1).strip()
    return out


def _to_int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_dt(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    n = _to_int(v) if not isinstance(v, float) else v
    if n is None:
        return None
    n = n / 1e9 if n > 1e17 else n / 1e3 if n > 1e11 else n   # AFC: ns; tolerate ms / s
    try:
        dt = datetime.fromtimestamp(n, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return dt if 2000 <= dt.year <= 2100 else None


def build_records(items: list[dict]) -> tuple[list[CrashRecord], int]:
    """Files only (directories are counted and skipped). Returns (records, dir_count)."""
    records, dirs = [], 0
    for it in items:
        path, st = str(it.get("path", "")), it.get("stat") or {}
        if str(st.get("st_ifmt", "")) == "S_IFDIR":
            dirs += 1
            continue
        name = path.rstrip("/").rsplit("/", 1)[-1]
        process, ts = parse_name(name)
        rec = CrashRecord(path, name, f"{SRC_LIST} {path}", _to_int(st.get("st_size")), _to_dt(st.get("st_mtime")),
                          ts, process, archived="/retired/" in path.lower() or path.lower().startswith("retired/"))
        kind = name_kind(name)
        if kind:
            rec.kind, rec.kind_basis = kind
        records.append(rec)
    return records, dirs


def sort_key(rec: CrashRecord) -> str:
    return rec.timestamp or (rec.mtime.strftime("%Y-%m-%d %H:%M:%S") if rec.mtime else "")


def apply_details(rec: CrashRecord, payload: dict | None) -> None:
    if not payload:
        rec.detail_note = "содержимое не читалось"
        return
    if "data" in payload:
        text = payload["data"].decode("utf-8", "replace")
        rec.details = parse_report(text)
        if rec.process is None:
            rec.process = rec.details.get("process")
        if rec.kind == "unknown":
            if rec.details.get("exception_type") or rec.details.get("termination"):
                rec.kind, rec.kind_basis = "crash", "содержимое"
        if not rec.details:
            rec.detail_note = "формат содержимого не распознан"
    else:
        rec.detail_note = payload.get("skipped") or payload.get("error") or "нет данных"


# ---- collect ---------------------------------------------------------------------

def collect(device: Device, *, timeout: float = 15.0, limit: int = 20, details: bool = True,
            process: str | None = None, max_bytes: int = DEFAULT_MAX_BYTES,
            read_timeout: float = 60.0, on_progress: ProgressCb | None = None) -> Report:
    trace(f"отчёты о сбоях: лимит {limit}, содержимое: {'да' if details else 'нет'}", source="crashes")
    collector = Collector(device, timeout=timeout, on_progress=on_progress)
    listed = collector.service("crash_list", device.crash_list, source=SRC_LIST, timeout=read_timeout)
    if not listed.is_ok:
        return Report([Entry(static_field("total_files", "Файлов на устройстве", S_SRC, listed), listed)],
                      collector.aborted, SECTIONS, NOTES)

    records, dirs = build_records(listed.value)
    calc = "расчёт iOSKit ← список файлов"
    records.sort(key=sort_key, reverse=True)
    pool = [r for r in records if not process or process.lower() in (r.process or r.name).lower()]
    shown = pool[:max(limit, 0)]

    read_result: Result[Any]
    if not details:
        read_result = Result.unavailable("чтение содержимого отключено (--no-details)", source=SRC_READ)
    elif not shown:
        read_result = Result.unavailable("нет файлов для чтения", source=SRC_READ)
    else:
        items = [(r.path, r.size) for r in shown]
        read = collector.service("crash_read", lambda: device.crash_read_many(items, max_bytes),
                                 source=SRC_READ, timeout=read_timeout)
        if read.is_ok:
            ok = skipped = errors = unknown = 0
            for r in shown:
                payload = read.value.get(r.path)
                apply_details(r, payload)
                if payload and "data" in payload:
                    ok, unknown = (ok + 1, unknown) if r.details else (ok, unknown + 1)
                elif payload and "skipped" in payload:
                    skipped += 1
                else:
                    errors += 1
            read_result = Result.ok(f"разобрано {ok} из {len(shown)}; пропущено по размеру {skipped}; "
                                    f"ошибок чтения {errors}; формат не распознан {unknown}", SRC_READ)
        else:
            read_result = read
    if not details or not read_result.is_ok:
        for r in shown:
            r.detail_note = r.detail_note or ("содержимое не читалось" if not details else "чтение не удалось")

    all_kinds = Counter(name_kind(r.name)[0] if name_kind(r.name) else "прочее" for r in records)
    kinds_text = ", ".join(f"{k}: {v}" for k, v in all_kinds.most_common()) or "нет"
    stamps = sorted(r.timestamp for r in records if r.timestamp)
    newest = Result.ok(stamps[-1], calc, formula="максимум по меткам времени в именах файлов") if stamps else \
        Result.unavailable("в именах файлов нет меток времени", source=calc)
    oldest = Result.ok(stamps[0], calc, formula="минимум по меткам времени в именах файлов") if stamps else \
        Result.unavailable("в именах файлов нет меток времени", source=calc)

    warnings: list[str] = []
    panics = [r for r in records if r.kind == "panic"]
    if panics:
        last = max((r.timestamp for r in panics if r.timestamp), default="время неизвестно")
        warnings.append(f"Отчёты kernel panic на устройстве: {len(panics)} (новейший: {last}).")
    repeats = Counter(r.process for r in records if r.process and r.kind not in ("jetsam", "panic", "stackshot"))
    for proc, n in repeats.most_common():
        if n >= REPEAT_WARN:
            warnings.append(f"Процесс «{proc}»: {n} отчётов на устройстве (эвристика iOSKit: ≥ {REPEAT_WARN}).")

    fields = [
        static_field("total_files", "Файлов на устройстве", S_SRC,
                     Result.ok(len(records), SRC_LIST, formula=f"файлов (каталогов пропущено: {dirs})")),
        static_field("shown", "Показано отчётов", S_SRC,
                     Result.ok(len(shown), calc, formula=f"новейшие {limit} из {len(pool)}"
                               + (f" (фильтр процесса «{process}»)" if process else ""))),
        static_field("kinds_all", "Типы всех файлов (по имени)", S_SRC,
                     Result.ok(kinds_text, calc, formula="число файлов по признакам имени: jetsam / panic / stackshot / прочее")),
        static_field("newest", "Новейший отчёт", S_SRC, newest),
        static_field("oldest", "Старейший отчёт", S_SRC, oldest),
        static_field("details_status", "Чтение содержимого", S_SRC, read_result),
    ]
    entries = collector.run(fields)
    return Report(entries, collector.aborted, SECTIONS, NOTES, tuple(warnings), extra=(shown, SRC_LIST))
