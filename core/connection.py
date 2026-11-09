"""USB connection layer: usbmuxd -> pairing/trust -> lockdown session.

This is the ONLY module that touches the pymobiledevice3 connection API
(sync API of 4.x), so a future library change stays isolated here.
No pairing bypass: pairing needs the user to tap "Trust" on the device.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

from ioskit.core.result import Result
from ioskit.utils.errors import ConnState, classify, diagnosis_for, Diagnosis
from ioskit.utils.models import commercial_name
from ioskit.utils.trace import trace, trace_error

T = TypeVar("T")
USBMUXD_SOCKET = Path("/var/run/usbmuxd")


@dataclass(frozen=True, slots=True)
class MuxEntry:
    udid: str
    connection_type: str


def usbmuxd_socket_present() -> bool:
    return USBMUXD_SOCKET.exists()


def crosscheck_idevice_id() -> Result[list[str]]:
    """Independent cross-check through libimobiledevice (hint only)."""
    exe = shutil.which("idevice_id")
    if not exe:
        return Result.unavailable("idevice_id не установлен (libimobiledevice-utils)",
                                  source="idevice_id")
    try:
        out = subprocess.run([exe, "-l"], capture_output=True, text=True, timeout=5)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return Result.fail(exc, source="idevice_id")
    udids = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    trace(f"idevice_id -l -> {udids or 'пусто'}", source="idevice_id")
    return Result.ok(udids, "idevice_id -l")


def list_devices() -> Result[list[MuxEntry]]:
    """USB devices known to usbmuxd. Empty list is OK (no device), not an error."""
    if not usbmuxd_socket_present():
        trace(f"{USBMUXD_SOCKET} не найден", source="usbmuxd")
        diag = diagnosis_for(ConnState.NO_USBMUXD)
        return Result.fail(diag.title, source="usbmuxd", hint=diag.hint,
                           code=ConnState.NO_USBMUXD.value)
    try:
        from pymobiledevice3.usbmux import list_devices as mux_list
    except ImportError as exc:
        diag = diagnosis_for(ConnState.MISSING_LIB)
        return Result.fail(exc, source="pymobiledevice3", hint=diag.hint,
                           code=ConnState.MISSING_LIB.value)
    try:
        raw = mux_list()
    except Exception as exc:  # noqa: BLE001
        trace_error(exc, source="usbmuxd")
        diag = classify(exc)
        return Result.fail(exc, source="usbmuxd", hint=diag.hint, code=diag.state.value)

    seen: dict[str, MuxEntry] = {}
    for dev in raw:
        ctype = str(getattr(dev, "connection_type", "USB"))
        if ctype.upper() != "USB":  # iOSKit works over USB only
            trace(f"пропущено устройство {dev.serial} ({ctype})", source="usbmuxd")
            continue
        seen.setdefault(dev.serial, MuxEntry(dev.serial, ctype))
    trace(f"найдено USB-устройств: {len(seen)}", source="usbmuxd")
    return Result.ok(list(seen.values()), "usbmuxd")


def read_value(lockdown: Any, key: str, domain: str | None = None) -> Result[Any]:
    """Read one lockdown value. Missing key -> UNAVAILABLE, failure -> ERROR."""
    src = f"lockdownd:{domain + '/' if domain else ''}{key}"
    try:
        value = lockdown.get_value(domain=domain, key=key)
    except Exception as exc:  # noqa: BLE001
        trace_error(exc, source=src)
        diag = classify(exc)
        if diag.state is ConnState.SERVICE_UNAVAILABLE:
            return Result.unavailable(diag.title, source=src, hint=diag.hint, code=diag.state.value)
        return Result.fail(exc, source=src, hint=diag.hint, code=diag.state.value)
    if value is None or value == "":
        trace("значение отсутствует", source=src)
        return Result.unavailable("устройство не отдаёт это значение", source=src)
    trace(f"ok -> {value!r}", source=src)
    return Result.ok(value, src)


class ConnectionManager:
    """Owns one lockdown session; reconnects once after a dropped cable."""

    def __init__(self, udid: str | None = None, *, pair: bool = False,
                 pair_timeout: float = 60.0) -> None:
        self.udid, self.pair, self.pair_timeout = udid, pair, pair_timeout
        self._lockdown: Any = None

    @property
    def lockdown(self) -> Any:
        return self._lockdown

    def _create(self, autopair: bool) -> Result[Any]:
        try:
            from pymobiledevice3.lockdown import create_using_usbmux
        except ImportError as exc:
            diag = diagnosis_for(ConnState.MISSING_LIB)
            return Result.fail(exc, source="pymobiledevice3", hint=diag.hint,
                               code=ConnState.MISSING_LIB.value)
        kwargs: dict[str, Any] = {"serial": self.udid, "autopair": autopair,
                                  "connection_type": "USB"}
        if autopair:
            kwargs["pair_timeout"] = self.pair_timeout
        trace(f"create_using_usbmux({kwargs})", source="lockdownd")
        try:
            try:
                lk = create_using_usbmux(**kwargs)
            except TypeError as exc:  # older signature without some kwargs
                trace(f"сигнатура не подошла ({exc}); упрощённый вызов", source="lockdownd")
                lk = create_using_usbmux(serial=self.udid, autopair=autopair)
        except Exception as exc:  # noqa: BLE001
            trace_error(exc, source="lockdownd")
            diag = classify(exc)
            return Result.fail(exc, source="lockdownd", hint=diag.hint, code=diag.state.value)
        if getattr(lk, "paired", True) is False:
            diag = diagnosis_for(ConnState.NOT_PAIRED)
            return Result.fail(diag.title, source="lockdownd", hint=diag.hint,
                               code=ConnState.NOT_PAIRED.value)
        return Result.ok(lk, "lockdownd")

    def open(self) -> Result[Any]:
        res = self._create(autopair=False)
        if not res.is_ok and res.code == ConnState.NOT_PAIRED.value and self.pair:
            trace("не сопряжено -> запуск сопряжения (нужно «Доверять» на iPhone)",
                  source="pairing")
            res = self._create(autopair=True)
        if res.is_ok:
            self._lockdown = res.value
        return res

    def close(self) -> None:
        lk, self._lockdown = self._lockdown, None
        try:
            if lk is not None and hasattr(lk, "close"):
                lk.close()
        except Exception as exc:  # noqa: BLE001
            trace_error(exc, source="lockdownd")

    def is_alive(self) -> bool:
        if self._lockdown is None:
            return False
        return read_value(self._lockdown, "ProductType").is_ok

    def call(self, fn: Callable[[Any], T], *, source: str, retries: int = 1) -> Result[T]:
        """Run fn(lockdown). Maps errors to typed Results; reconnects on disconnect."""
        for attempt in range(retries + 1):
            if self._lockdown is None:
                opened = self.open()
                if not opened.is_ok:
                    return opened  # type: ignore[return-value]
            try:
                return Result.ok(fn(self._lockdown), source)
            except Exception as exc:  # noqa: BLE001
                trace_error(exc, source=source)
                diag = classify(exc)
                if diag.state is ConnState.SERVICE_UNAVAILABLE:
                    return Result.unavailable(diag.title, source=source, hint=diag.hint,
                                              code=diag.state.value)
                if diag.state is ConnState.DISCONNECTED and attempt < retries:
                    trace("соединение потеряно -> переподключение", source=source)
                    self.close()
                    continue
                return Result.fail(exc, source=source, hint=diag.hint, code=diag.state.value)
        return Result.fail("недостижимо", source=source)  # pragma: no cover


@dataclass(slots=True)
class Probe:
    """Snapshot of one device used by `ioskit devices`."""
    udid: str
    state: ConnState
    diagnosis: Diagnosis | None
    product_type: Result[Any]
    os_version: Result[Any]
    device_name: Result[Any]

    @property
    def model(self) -> Result[str]:
        return commercial_name(self.product_type.unwrap_or(None))


def probe(udid: str, *, pair: bool = False, pair_timeout: float = 60.0) -> Probe:
    mgr = ConnectionManager(udid, pair=pair, pair_timeout=pair_timeout)
    res = mgr.open()
    if not res.is_ok:
        try:
            state = ConnState(res.code or "unknown")
        except ValueError:
            state = ConnState.UNKNOWN
        skip = Result.unavailable("нет соединения", code=state.value)
        return Probe(udid, state, diagnosis_for(state), skip, skip, skip)
    try:
        lk = mgr.lockdown
        return Probe(udid, ConnState.CONNECTED, None,
                     read_value(lk, "ProductType"),
                     read_value(lk, "ProductVersion"),
                     read_value(lk, "DeviceName"))
    finally:
        mgr.close()
