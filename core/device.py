"""Device facade: one connected iPhone, typed lockdown reads.

Device does NOT cache and does NOT run modules (that is the Collector's job).
It only turns raw lockdown calls into typed Results and delegates connection
handling (pairing, reconnect after a dropped cable) to ConnectionManager.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Callable, Iterator

from ioskit.core import connection
from ioskit.core.connection import ConnectionManager
from ioskit.core.result import Result, Status
from ioskit.utils.errors import ConnState, classify, diagnosis_for

DEPRECATED_REASON = ("MobileGestalt закрыт на этой версии iOS "
                     "(Apple ограничила его в новых версиях, ориентировочно с iOS 17.4)")
_DEPRECATED = object()


def _looks_deprecated(exc: BaseException) -> bool:
    return "deprecat" in type(exc).__name__.lower() or "deprecat" in str(exc).lower()


def _deprecated_payload(out: Any) -> bool:
    if not isinstance(out, dict):
        return False
    probes = [out.get("Status")]
    inner = out.get("MobileGestalt")
    if isinstance(inner, dict):
        probes.append(inner.get("Status"))
    return any(isinstance(p, str) and "deprecat" in p.lower() for p in probes)


def _as_dict(res: Result[Any], source: str) -> Result[dict]:
    """Normalise a service reply to a dict (a list -> its first dict)."""
    if not res.is_ok:
        return res  # type: ignore[return-value]
    v = res.value
    if isinstance(v, list):
        v = next((x for x in v if isinstance(x, dict)), None)
    if v is None or v == {}:
        return Result.unavailable("сервис вернул пустой ответ", source=source)
    if not isinstance(v, dict):
        return Result.fail(f"неожиданный тип ответа: {type(v).__name__}", source=source)
    return Result.ok(v, source)


_FATAL_STATES = {ConnState.DISCONNECTED, ConnState.NO_DEVICE, ConnState.TIMEOUT, ConnState.NO_USBMUXD}


def _reraise_if_fatal(exc: BaseException) -> None:
    """A per-item failure is recorded; a lost device must abort the whole operation."""
    if classify(exc).state in _FATAL_STATES:
        raise exc


class SyslogSession:
    """Open syslog_relay stream. lines() blocks; close() (from another thread) unblocks it."""

    def __init__(self, service: Any) -> None:
        self._svc = service

    def lines(self) -> Iterator[str]:
        for line in self._svc.watch():
            yield line if isinstance(line, str) else bytes(line).decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self._svc.close()
        except Exception:  # noqa: BLE001 - closing is best effort
            pass


EMPTY_REASON = "устройство не отдаёт это значение (зависит от модели, iOS, SIM)"


def lockdown_source(key: str | None = None, domain: str | None = None) -> str:
    if key is None:
        return f"lockdownd:GetValue({domain or 'global'})"
    return f"lockdownd:{domain + '/' if domain else ''}{key}"


def select_udid(udid: str | None = None) -> Result[str]:
    """Pick the target device among USB devices known to usbmuxd."""
    listed = connection.list_devices()
    if not listed.is_ok:
        return listed  # type: ignore[return-value]
    udids = [e.udid for e in listed.value or []]

    def no_device(reason: str) -> Result[str]:
        d = diagnosis_for(ConnState.NO_DEVICE)
        return Result.fail(reason, source="usbmuxd", hint=d.hint, code=ConnState.NO_DEVICE.value)

    if udid:
        if udid in udids:
            return Result.ok(udid, "usbmuxd")
        return no_device(f"устройство {udid} не найдено среди USB-устройств")
    if not udids:
        return no_device("USB-устройства не найдены")
    if len(udids) > 1:
        d = diagnosis_for(ConnState.MULTIPLE_DEVICES)
        return Result.fail("найдено: " + ", ".join(udids), source="usbmuxd",
                           hint=d.hint, code=ConnState.MULTIPLE_DEVICES.value)
    return Result.ok(udids[0], "usbmuxd")


class Device:
    def __init__(self, udid: str | None = None, *, pair: bool = False,
                 pair_timeout: float = 60.0, manager: ConnectionManager | None = None) -> None:
        self.udid = udid
        self._mgr = manager or ConnectionManager(udid, pair=pair, pair_timeout=pair_timeout)

    # -- lifecycle ------------------------------------------------------
    def connect(self) -> Result[Any]:
        return self._mgr.open()

    def close(self) -> None:
        self._mgr.close()

    def __enter__(self) -> "Device":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- reads ----------------------------------------------------------
    def dump(self, domain: str | None = None) -> Result[dict]:
        """All values of a lockdown domain in ONE round trip (fast on old devices)."""
        src = lockdown_source(None, domain)
        res = self._mgr.call(lambda lk: lk.get_value(domain=domain), source=src)
        if not res.is_ok:
            return res  # type: ignore[return-value]
        if res.value is None:
            return Result.unavailable("домен не отдаёт значения", source=src)
        if not isinstance(res.value, dict):
            return Result.fail(f"неожиданный тип ответа: {type(res.value).__name__}", source=src)
        return res

    def read(self, key: str, domain: str | None = None) -> Result[Any]:
        """One lockdown value. Missing/empty -> UNAVAILABLE (never a guess)."""
        src = lockdown_source(key, domain)
        res = self._mgr.call(lambda lk: lk.get_value(domain=domain, key=key), source=src)
        if res.is_ok and (res.value is None or (isinstance(res.value, str) and res.value == "")):
            return Result.unavailable(EMPTY_REASON, source=src)
        return res

    # -- device services (diagnostics relay, AFC, installation proxy) ----
    def _service(self, name: str, lockdown: Any) -> Any:
        """Create a pymobiledevice3 service client. Isolated so tests can replace it."""
        if name == "diagnostics":
            from pymobiledevice3.services.diagnostics import DiagnosticsService
            return DiagnosticsService(lockdown)
        if name == "afc":
            from pymobiledevice3.services.afc import AfcService
            return AfcService(lockdown)
        if name == "syslog":
            from pymobiledevice3.services.syslog import SyslogService
            return SyslogService(lockdown)
        if name == "crash_reports":
            from pymobiledevice3.services.crash_reports import CrashReportsManager
            return CrashReportsManager(lockdown)
        if name == "installation_proxy":
            from pymobiledevice3.services.installation_proxy import InstallationProxyService
            return InstallationProxyService(lockdown)
        raise ValueError(f"неизвестный сервис: {name}")

    def _service_call(self, name: str, op: Callable[[Any], Any], *, source: str,
                      retries: int = 1) -> Result[Any]:
        def run(lockdown: Any) -> Any:
            svc = self._service(name, lockdown)
            try:
                return op(svc)
            finally:
                try:
                    svc.close()
                except Exception:  # noqa: BLE001 - closing is best effort
                    pass

        res = self._mgr.call(run, source=source, retries=retries)
        if res.status is Status.ERROR and res.error_type in ("ImportError", "ModuleNotFoundError"):
            d = diagnosis_for(ConnState.MISSING_LIB)
            return dataclasses.replace(res, hint=d.hint, code=ConnState.MISSING_LIB.value)
        return res

    def ioregistry(self, ioclass: str) -> Result[dict]:
        """IORegistry entry by class via Diagnostics Relay (undocumented keys!)."""
        src = f"diagnostics_relay:IORegistry({ioclass})"
        res = self._service_call("diagnostics", lambda s: s.ioregistry(ioclass=ioclass), source=src)
        return _as_dict(res, src)

    def mobilegestalt(self, keys: list[str]) -> Result[dict]:
        """MobileGestalt values. Closed by Apple on newer iOS -> UNSUPPORTED."""
        src = "diagnostics_relay:MobileGestalt"

        def op(s: Any) -> Any:
            try:
                out = s.mobilegestalt(keys=list(keys))
            except Exception as exc:  # noqa: BLE001
                if _looks_deprecated(exc):
                    return _DEPRECATED
                raise
            return _DEPRECATED if _deprecated_payload(out) else out

        res = self._service_call("diagnostics", op, source=src)
        if res.is_ok and res.value is _DEPRECATED:
            return Result.unsupported(DEPRECATED_REASON, source=src)
        return _as_dict(res, src)

    def diagnostics_info(self, diag_type: str = "All") -> Result[dict]:
        """Diagnostics Relay info (GasGauge/NAND/WiFi...): whatever the device chooses to return."""
        src = f"diagnostics_relay:Diagnostics({diag_type})"
        res = self._service_call("diagnostics", lambda s: s.info(diag_type), source=src)
        return _as_dict(res, src)

    def afc_info(self) -> Result[dict]:
        """Media-partition totals from AFC (FSTotalBytes, FSFreeBytes, FSBlockSize)."""
        src = "afc:GetDeviceInfo"
        return _as_dict(self._service_call("afc", lambda s: s.get_device_info(), source=src), src)

    def installed_apps(self, calculate_sizes: bool = True, application_type: str = "User") -> Result[dict]:
        """Apps via installation_proxy (bundle id -> attributes). Type: User | System | Any."""
        src = f"installation_proxy:Browse({application_type})"

        def op(s: Any) -> Any:
            try:
                return s.get_apps(application_type=application_type, calculate_sizes=calculate_sizes)
            except TypeError:  # older pymobiledevice3 signature: no sizes
                return s.get_apps(application_type=application_type)

        return _as_dict(self._service_call("installation_proxy", op, source=src), src)

    # -- logs and crash reports ---------------------------------------------
    def open_syslog(self) -> Result[SyslogSession]:
        """Start the syslog_relay stream (no automatic retry: a stream cannot be resumed)."""
        return self._mgr.call(lambda lk: SyslogSession(self._service("syslog", lk)),
                              source="syslog_relay", retries=0)

    def crash_list(self, depth: int = 2) -> Result[list]:
        """Files exposed by crashreportcopymobile (via AFC) with stat info where available."""
        src = "crashreportcopymobile:ls"

        def op(m: Any) -> Any:
            items = []
            for path in m.ls("/", depth):
                try:
                    st, err = m.afc.stat(path), None
                except Exception as exc:  # noqa: BLE001
                    _reraise_if_fatal(exc)
                    st, err = None, f"{type(exc).__name__}: {exc}"
                items.append({"path": path, "stat": st, "stat_error": err})
            return items

        res = self._service_call("crash_reports", op, source=src)
        if res.is_ok and not isinstance(res.value, list):
            return Result.fail(f"неожиданный тип ответа: {type(res.value).__name__}", source=src)
        return res

    def crash_read_many(self, items: list[tuple[str, int | None]], max_bytes: int) -> Result[dict]:
        """Read report files. Per-file problems are recorded per file; a lost device aborts."""
        src = "crashreportcopymobile:read"

        def op(m: Any) -> Any:
            out: dict[str, dict] = {}
            for path, size in items:
                if size is not None and size > max_bytes:
                    out[path] = {"skipped": f"размер {size} Б больше лимита {max_bytes} Б"}
                    continue
                try:
                    data = m.afc.get_file_contents(path)
                except Exception as exc:  # noqa: BLE001
                    _reraise_if_fatal(exc)
                    out[path] = {"error": f"{type(exc).__name__}: {exc}"}
                    continue
                out[path] = ({"skipped": f"размер {len(data)} Б больше лимита {max_bytes} Б"}
                             if len(data) > max_bytes else {"data": bytes(data)})
            return out

        return self._service_call("crash_reports", op, source=src)
