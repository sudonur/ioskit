"""System log (syslog) capture.

Sources
  * pymobiledevice3 SyslogService  (com.apple.syslog_relay) - primary
  * libimobiledevice `idevicesyslog`                        - fallback (--backend auto)

A live stream is captured for a bounded time / number of lines, parsed and filtered.
Nothing is invented: a line that does not match the syslog format is kept raw and
counted as "unparsed" (no process/level is guessed for it). The log has no year, so
none is shown. Only the live stream is read: iOS does not hand out its history here.
"""
from __future__ import annotations

import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from ioskit.core.collector import FATAL_CODES, Collector, Entry, Field, ProgressCb
from ioskit.core.device import Device
from ioskit.core.result import Result
from ioskit.modules.common import Report, static_field
from ioskit.utils import libimobile
from ioskit.utils.trace import trace

S_CAP = "Захват журнала"
S_STAT = "Статистика захвата (расчёт iOSKit)"
SECTIONS = (S_CAP, S_STAT)
TITLE = "Системный журнал"
HEADER = (("Источник", "stream_source"), ("Получено строк", "received"),
          ("Прошло фильтры", "matched"), ("Остановка", "ended"))

# most severe first; unknown levels have no rank and never pass a level filter
LEVELS = ("Emergency", "Alert", "Critical", "Fault", "Error", "Warning", "Notice", "Info", "Debug")
BACKENDS = ("auto", "pymobiledevice3", "libimobiledevice")
MAX_SCAN = 20_000

NOTES = (
    "Читается только живой поток syslog с момента подключения; архив журнала iOS этой командой не отдаёт.",
    "iOS часто скрывает динамические данные в сообщениях («<private>»): это ограничение системы, не ошибка iOSKit.",
    "Журнал может содержать персональные данные: просмотрите вывод и файл --output перед передачей другим.",
    "iOSKit ничего не отправляет по сети.",
)

_LINE = re.compile(
    r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+(?P<host>\S+)\s+"
    r"(?P<proc>.+?)\[(?P<pid>\d+)\]\s+<(?P<level>[A-Za-z]+)>:\s?(?P<msg>.*)$")


@dataclass(frozen=True, slots=True)
class LogRecord:
    raw: str
    source: str
    parsed: bool = False
    timestamp: str | None = None
    host: str | None = None
    process: str | None = None
    pid: int | None = None
    level: str | None = None
    message: str | None = None


def level_rank(level: str | None) -> int | None:
    if not level:
        return None
    for i, name in enumerate(LEVELS):
        if name.lower() == level.lower():
            return i
    return None


def normalize_level(level: str) -> str:
    rank = level_rank(level)
    if rank is None:
        raise ValueError(f"неизвестный уровень «{level}»; допустимо: {', '.join(LEVELS)}")
    return LEVELS[rank]


def parse_line(raw: str, source: str) -> LogRecord:
    m = _LINE.match(raw)
    if not m:
        return LogRecord(raw, source)
    rank = level_rank(m["level"])
    return LogRecord(raw, source, True, m["ts"], m["host"], m["proc"], int(m["pid"]),
                     LEVELS[rank] if rank is not None else m["level"], m["msg"])


@dataclass(frozen=True, slots=True)
class LogFilter:
    min_level: str | None = None
    process: str | None = None
    pid: int | None = None
    grep: "re.Pattern[str] | None" = None

    @property
    def active(self) -> bool:
        return any((self.min_level, self.process, self.pid is not None, self.grep))

    def matches(self, rec: LogRecord) -> bool:
        if self.grep and not self.grep.search(rec.raw):
            return False
        if self.min_level or self.process or self.pid is not None:
            if not rec.parsed:  # process/level of a raw line is unknown: cannot satisfy the filter
                return False
            if self.min_level:
                rank = level_rank(rec.level)
                if rank is None or rank > level_rank(self.min_level):
                    return False
            if self.process and self.process.lower() not in (rec.process or "").lower():
                return False
            if self.pid is not None and rec.pid != self.pid:
                return False
        return True

    def describe(self) -> str:
        parts = []
        if self.min_level:
            parts.append(f"уровень ≥ {self.min_level}")
        if self.process:
            parts.append(f"процесс содержит «{self.process}»")
        if self.pid is not None:
            parts.append(f"pid = {self.pid}")
        if self.grep:
            parts.append(f"текст: {self.grep.pattern}")
        return "; ".join(parts) or "нет"


def build_filter(*, level: str | None = None, process: str | None = None, pid: int | None = None,
                 grep: str | None = None, regex: bool = False) -> LogFilter:
    """Raises ValueError with a user-readable message on a bad level / regex."""
    pattern = None
    if grep:
        try:
            pattern = re.compile(grep if regex else re.escape(grep), re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"некорректное регулярное выражение: {exc}") from exc
    return LogFilter(normalize_level(level) if level else None, process or None, pid, pattern)


# ---- stream ------------------------------------------------------------------

@dataclass(slots=True)
class Stream:
    lines: Iterator[str]
    close: Callable[[], None]
    source: str
    note: str | None = None


def open_stream(device: Device, *, backend: str = "auto") -> Result[Stream]:
    """Open the log stream. auto = pymobiledevice3 first, libimobiledevice if it is unavailable."""
    if backend not in BACKENDS:
        raise ValueError(f"неизвестный бэкенд «{backend}»; допустимо: {', '.join(BACKENDS)}")
    primary: Result[Any] | None = None
    if backend in ("auto", "pymobiledevice3"):
        res = device.open_syslog()
        if res.is_ok:
            return Result.ok(Stream(res.value.lines(), res.value.close, "pymobiledevice3: syslog_relay"),
                             "syslog_relay")
        if backend == "pymobiledevice3" or res.code in FATAL_CODES:
            return res  # type: ignore[return-value]
        primary = res
        trace(f"syslog_relay недоступен ({res.reason}) -> libimobiledevice", source="logs")
    tool = libimobile.syslog_stream(device.udid)
    if tool.is_ok:
        note = f"pymobiledevice3 недоступен ({primary.reason}); использован idevicesyslog" if primary else None
        return Result.ok(Stream(tool.value.lines, tool.value.close, "libimobiledevice: idevicesyslog", note),
                         "idevicesyslog")
    if primary is None:
        return tool  # type: ignore[return-value]
    import dataclasses
    return dataclasses.replace(primary, reason=f"{primary.reason}; запасной вариант libimobiledevice: {tool.reason}")


@dataclass(slots=True)
class Capture:
    records: list[LogRecord] = field(default_factory=list)
    received: int = 0
    unparsed: int = 0
    ended: str = ""
    error: str | None = None
    elapsed: float = 0.0


def capture(stream: Stream, flt: LogFilter, *, duration: float, max_lines: int,
            max_scan: int = MAX_SCAN, clock: Callable[[], float] = time.monotonic) -> Capture:
    """Read the stream in a worker thread for `duration` s or until `max_lines` matched lines.

    ended: limit | scan_limit | duration | stream_end | error
    """
    cap, done, closing = Capture(), threading.Event(), threading.Event()

    def pump() -> None:
        try:
            for raw in stream.lines:
                rec = parse_line(raw, stream.source)
                cap.received += 1
                cap.unparsed += 0 if rec.parsed else 1
                if flt.matches(rec):
                    cap.records.append(rec)
                if len(cap.records) >= max_lines:
                    cap.ended = "limit"
                    break
                if cap.received >= max_scan:
                    cap.ended = "scan_limit"
                    break
            else:
                if not closing.is_set():
                    cap.ended = "stream_end"
        except Exception as exc:  # noqa: BLE001
            if not closing.is_set():
                cap.ended, cap.error = "error", f"{type(exc).__name__}: {exc}"
        finally:
            done.set()

    t0 = clock()
    worker = threading.Thread(target=pump, daemon=True)
    worker.start()
    done.wait(duration)
    if not done.is_set():
        cap.ended = "duration"
    closing.set()
    stream.close()          # unblocks a pending read; errors after this point are expected
    worker.join(2.0)
    cap.records = list(cap.records)
    cap.elapsed = clock() - t0
    return cap


# ---- report ------------------------------------------------------------------

_ENDED = {"duration": "истекло время захвата", "limit": "достигнут лимит строк",
          "scan_limit": f"просмотрено {MAX_SCAN} строк (защитный лимит)",
          "stream_end": "устройство закрыло поток", "error": "ошибка потока"}


def write_output(records: list[LogRecord], path: Path) -> Result[Path]:
    """Raw matched lines to a NEW file (an existing file is never overwritten)."""
    src = "iOSKit: запись файла"
    try:
        with open(path, "x", encoding="utf-8") as fh:
            fh.write("\n".join(r.raw for r in records) + ("\n" if records else ""))
    except FileExistsError:
        return Result.fail(f"файл {path} уже существует", source=src, hint="Укажите другое имя файла.")
    except OSError as exc:
        return Result.fail(exc, source=src)
    return Result.ok(path, src)


def _entries(cap: Capture, stream: Stream, flt: LogFilter, output: Result[Path] | None) -> list[Field]:
    src, calc = stream.source, "расчёт iOSKit ← захваченные строки"
    n = len(cap.records)
    by_level = Counter(r.level or "—" for r in cap.records if r.parsed)
    order = {lv: i for i, lv in enumerate(LEVELS)}
    levels_text = ", ".join(f"{k}: {v}" for k, v in sorted(by_level.items(), key=lambda kv: order.get(kv[0], 99))) or "нет"
    procs = Counter(r.process for r in cap.records if r.parsed)
    procs_text = ", ".join(f"{k}: {v}" for k, v in procs.most_common(5)) or "нет"
    ended = (Result.fail(cap.error or "ошибка потока", source=src) if cap.ended == "error"
             else Result.ok(_ENDED.get(cap.ended, cap.ended), "iOSKit"))
    fields = [
        static_field("stream_source", "Источник потока", S_CAP, Result.ok(stream.source, src)),
        static_field("received", "Получено строк", S_CAP, Result.ok(cap.received, src)),
        static_field("matched", "Прошло фильтры", S_CAP,
                     Result.ok(n, calc, formula=f"строки, удовлетворяющие фильтрам ({flt.describe()})")),
        static_field("filters", "Фильтры", S_CAP, Result.ok(flt.describe(), "параметры команды")),
        static_field("elapsed", "Длительность захвата", S_CAP,
                     Result.ok(round(cap.elapsed, 1), "измерено iOSKit"), fmt=lambda v: f"{v:g} с"),
        static_field("ended", "Причина остановки", S_CAP, ended),
        static_field("unparsed", "Строк не в формате syslog", S_STAT,
                     Result.ok(cap.unparsed, calc, formula="строки, не совпавшие с шаблоном «время хост процесс[pid] <уровень>: текст»")),
        static_field("by_level", "По уровням (прошедшие фильтры)", S_STAT,
                     Result.ok(levels_text, calc, formula="число записей по каждому уровню")),
        static_field("top_processes", "Самые шумные процессы", S_STAT,
                     Result.ok(procs_text, calc, formula="5 процессов с наибольшим числом записей")),
    ]
    if output is not None:
        fields.append(static_field("output_file", "Файл с записями", S_CAP, output,
                                   fmt=lambda p: f"{p} ({n} строк)"))
    return fields


def collect(device: Device, *, timeout: float = 15.0, duration: float = 5.0, max_lines: int = 100,
            flt: LogFilter | None = None, backend: str = "auto", output: Path | None = None,
            on_progress: ProgressCb | None = None) -> Report:
    flt = flt or LogFilter()
    trace(f"захват журнала: {duration:g} с, до {max_lines} строк, бэкенд {backend}", source="logs")
    collector = Collector(device, timeout=timeout, on_progress=on_progress)
    opened = collector.guard(lambda: open_stream(device, backend=backend), source="syslog_relay", timeout=timeout)
    if not opened.is_ok:
        entry = Entry(static_field("stream_source", "Источник потока", S_CAP, opened), opened)
        return Report([entry], collector.aborted, SECTIONS, NOTES)
    stream: Stream = opened.value
    cap = capture(stream, flt, duration=duration, max_lines=max_lines)
    out = write_output(cap.records, output) if output is not None else None
    notes = list(NOTES) + ([stream.note] if stream.note else [])
    if cap.received == 0 and cap.ended == "duration":
        notes.append(f"За {duration:g} с устройство не передало ни одной строки (возможно, нет активности или поток недоступен).")
    fields = _entries(cap, stream, flt, out)
    entries = collector.run(fields)
    return Report(entries, collector.aborted, SECTIONS, tuple(notes), extra=(cap.records, stream.source))
