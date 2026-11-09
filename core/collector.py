"""Collector: runs field readers against a Device with caching, a timeout
and early abort when the device disappears.

Rules:
  * one lockdown dump per domain is cached; keys are looked up in it first,
    and a key missing from the dump is re-read directly once;
  * a fatal communication error (cable pulled, timeout, ...) aborts the
    remaining reads: later fields are reported as ERROR "skipped", not
    silently dropped and not turned into fake values;
  * a reader that raises is converted to an ERROR Result; other fields go on.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from ioskit.core.device import Device, lockdown_source
from ioskit.core.result import Result, Status
from ioskit.utils.errors import ConnState, classify
from ioskit.utils.formatting import format_value, mask_text
from ioskit.utils.trace import trace, trace_error

FATAL_CODES = frozenset({
    ConnState.DISCONNECTED.value, ConnState.NO_DEVICE.value, ConnState.TIMEOUT.value,
    ConnState.NO_USBMUXD.value, ConnState.PASSWORD_REQUIRED.value,
})

Reader = Callable[["Collector"], Result[Any]]
ProgressCb = Callable[[int, int, "Field"], None]


@dataclass(frozen=True, slots=True)
class Field:
    """Declarative description of one displayed parameter."""
    key: str
    label: str
    section: str
    reader: Reader
    fmt: Callable[[Any], str] = format_value
    sensitive: bool = False


@dataclass(frozen=True, slots=True)
class Entry:
    field: Field
    result: Result[Any]

    def display(self, *, mask: bool = False) -> str | None:
        """Formatted value, or None when there is no value."""
        if not self.result.is_ok:
            return None
        try:
            text = self.field.fmt(self.result.value)
        except Exception:  # noqa: BLE001 - odd value type: fall back to generic text
            text = format_value(self.result.value)
        return mask_text(text) if (mask and self.field.sensitive) else text


def _run_with_timeout(fn: Callable[[], Any], timeout: float) -> Any:
    """Run fn in a daemon thread. On timeout the thread is abandoned, which is
    safe only because the Collector aborts all further device access."""
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"нет ответа от устройства за {timeout:g} с")
    if "error" in box:
        raise box["error"]
    return box["value"]


class Collector:
    def __init__(self, device: Device, *, timeout: float = 15.0,
                 on_progress: ProgressCb | None = None) -> None:
        self.device = device
        self.timeout = timeout
        self.on_progress = on_progress
        self.aborted: Result[Any] | None = None
        self._cache: dict[str, Result[Any]] = {}

    # -- infrastructure -------------------------------------------------
    def cached(self, name: str, fn: Callable[[], Result[Any]]) -> Result[Any]:
        if name not in self._cache:
            self._cache[name] = fn()
        return self._cache[name]

    def _note(self, res: Result[Any]) -> None:
        if res.status is Status.ERROR and res.code in FATAL_CODES and self.aborted is None:
            self.aborted = res
            trace("фатальная ошибка связи: дальнейшие чтения пропускаются", source="collector")

    def guard(self, fn: Callable[[], Result[Any]], *, source: str,
              timeout: float | None = None, fatal: bool = True) -> Result[Any]:
        """Run a call with a timeout; always returns a Result.

        fatal=True  (device calls): a lost device / timeout aborts further device reads.
        fatal=False (internet, host tools): failures never abort the device reads.
        """
        limit = timeout or self.timeout
        try:
            res = _run_with_timeout(fn, limit)
        except TimeoutError as exc:
            trace_error(exc, source=source)
            if fatal:
                diag = classify(exc)
                res = Result.fail(exc, source=source, hint=diag.hint, code=diag.state.value)
            else:
                res = Result.fail(f"вызов не завершился за {limit:g} с", source=source,
                                  hint="Повторите позже или увеличьте тайм-аут.", code="network_timeout")
        except Exception as exc:  # noqa: BLE001
            trace_error(exc, source=source)
            diag = classify(exc)
            res = Result.fail(exc, source=source, hint=diag.hint, code=diag.state.value)
        if fatal:
            self._note(res)
        return res

    def _skipped(self, source: str) -> Result[Any]:
        a = self.aborted
        return Result.fail(f"пропущено: {a.reason if a else 'связь потеряна'}", source=source,
                           hint=a.hint if a else None, code=a.code if a else None)

    def dump(self, domain: str | None = None) -> Result[Any]:
        """Cached lockdown dump of one domain."""
        return self.cached(
            f"dump:{domain or ''}",
            lambda: self.guard(lambda: self.device.dump(domain),
                               source=lockdown_source(None, domain)))

    def service(self, name: str, fn: Callable[[], Result[Any]], *, source: str,
                timeout: float | None = None) -> Result[Any]:
        """Cached call of a device service (diagnostics, AFC, installation_proxy...)."""
        key = f"service:{name}"
        if key not in self._cache and self.aborted:
            return self._skipped(source)
        return self.cached(key, lambda: self.guard(fn, source=source, timeout=timeout))

    def external(self, name: str, fn: Callable[[], Result[Any]], *, source: str,
                 timeout: float) -> Result[Any]:
        """Cached call that does not need the device (internet, host tools). Never fatal."""
        return self.cached(f"external:{name}",
                           lambda: self.guard(fn, source=source, timeout=timeout, fatal=False))

    # -- value access ---------------------------------------------------
    def lookup(self, key: str, domain: str | None = None) -> Result[Any]:
        return self.cached(f"read:{domain or ''}/{key}", lambda: self._lookup(key, domain))

    def _lookup(self, key: str, domain: str | None) -> Result[Any]:
        src = lockdown_source(key, domain)
        if self.aborted and f"dump:{domain or ''}" not in self._cache:
            return self._skipped(src)
        dump = self.dump(domain)
        if dump.is_ok:
            data = dump.value
            if key in data:
                value = data[key]
                if value is None or (isinstance(value, str) and value == ""):
                    return Result.unavailable("значение пустое", source=src)
                trace(f"{key} <- дамп", source=src)
                return Result.ok(value, src)
        elif dump.status is Status.UNAVAILABLE:
            return Result.unavailable(dump.reason or "домен недоступен", source=src,
                                      hint=dump.hint, code=dump.code)
        elif dump.code in FATAL_CODES:
            return Result.fail(dump.reason or "ошибка связи", source=src,
                               hint=dump.hint, code=dump.code)
        if self.aborted:
            return self._skipped(src)
        trace(f"{key} нет в дампе -> прямое чтение", source=src)
        return self.guard(lambda: self.device.read(key, domain), source=src)

    # -- execution ------------------------------------------------------
    def run(self, fields: Sequence[Field]) -> list[Entry]:
        entries: list[Entry] = []
        for i, f in enumerate(fields, 1):
            try:
                res = f.reader(self)
                if not isinstance(res, Result):
                    raise TypeError("reader вернул не Result")
            except Exception as exc:  # noqa: BLE001
                trace_error(exc, source=f.key)
                res = Result.fail(exc, source=f"reader:{f.key}")
            self._note(res)
            entries.append(Entry(f, res))
            if self.on_progress:
                self.on_progress(i, len(fields), f)
        return entries
