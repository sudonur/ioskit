"""Advanced diagnostics: what the device itself reports about its components.

This is NOT Apple's hardware test suite and does not imitate one. It shows:
  * availability of every diagnostic data source (OK / UNAVAILABLE / UNSUPPORTED / ERROR),
  * component and sensor data the device really returns,
  * consistency checks between independent sources,
  * warnings from explicit rules (thresholds are iOSKit heuristics, shown with numbers).

A check or rule exists only when ALL its inputs were read: missing data gives
UNAVAILABLE ("cannot check"), never a passed check.
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable

from ioskit.core.collector import Collector, Entry, Field, ProgressCb
from ioskit.core.device import Device
from ioskit.core.result import Result, Status
from ioskit.modules import battery as bat
from ioskit.modules import hardware as hw
from ioskit.modules import storage as st
from ioskit.modules.common import (Report, check_field, collect_warnings, finding, format_finding,
                                   ios_major, lk, lockdown_field as L, pick)
from ioskit.utils.formatting import format_bytes, format_value
from ioskit.utils.trace import trace

S_AVAIL = "Доступность диагностических данных"
S_COMP = "Компоненты (то, что сообщает устройство)"
S_SENS = "Датчики и телеметрия"
S_CONS = "Проверки согласованности"
S_WARN = "Предупреждения оборудования"
SECTIONS = (S_AVAIL, S_COMP, S_SENS, S_CONS, S_WARN)
TITLE = "Диагностика"
HEADER = (("iOS", "ios_version"), ("Здоровье батареи", ("health_nominal", "health_raw")),
          ("Температура батареи", "temperature"), ("Видимость данных", "src_lockdown"))

# iOSKit heuristics (NOT Apple criteria). Shown with the numbers in every result.
HEALTH_WARN_PCT = 80.0
TEMP_WARN_C = 45.0
STORAGE_WARN_PCT = 90.0
CLOCK_SKEW_S = 600
AFC_TOLERANCE = 0.10
BATTERY_CAP_TOLERANCE = 1.05
HEALTH_METHOD_GAP = 10.0
MAX_GROUP_KEYS = 40
MAX_GROUPS = 6

NOTES = (
    "Это не аппаратный тест Apple: показано только то, что устройство само сообщает через lockdown, "
    "Diagnostics Relay, MobileGestalt и AFC.",
    "Нет данных ≠ норма: если исходных данных нет, проверка получает статус «недоступно», а не «норма».",
    "Пороги предупреждений (здоровье 80 %, 45 °C, заполнение 90 %, расхождение часов 10 мин) — "
    "эвристики iOSKit, а не критерии Apple.",
    "Акселерометр, гироскоп, датчик приближения, камеры и Touch/Face ID через публичные USB-сервисы недоступны.",
)
NEW_IOS_NOTE = ("На iOS 17 и новее Diagnostics Relay и MobileGestalt могут отдавать заметно меньше ключей; "
                "строки «недоступно» и «не поддерживается» для них ожидаемы.")

_BAT = {f.key: f for f in bat.FIELDS}
_HW = {f.key: f for f in hw.FIELDS}
_ST = {f.key: f for f in st.FIXED_FIELDS}


def _r(table: dict, key: str) -> Callable[[Collector], Result[Any]]:
    return table[key].reader


# ---- shared Diagnostics Relay info(All) -----------------------------------
DIAG_INFO_SRC = "diagnostics_relay:Diagnostics(All)"


def diag_info(ctx: Collector) -> Result[Any]:
    return ctx.service("diag_info", lambda: ctx.device.diagnostics_info("All"), source=DIAG_INFO_SRC)


_SENSITIVE_WORDS = ("serial", "uuid", "udid", "mac", "imei", "iccid", "address")


def _leaves(d: dict, prefix: str = "", depth: int = 0):
    for k, v in d.items():
        path = f"{prefix}{k}"
        if isinstance(v, dict) and depth < 2:
            yield from _leaves(v, path + "/", depth + 1)
        elif isinstance(v, (bool, int, float, str, bytes)):
            yield path, v


def group_fields(group: str, data: dict, section: str) -> tuple[list[Field], bool]:
    """Fields for raw scalar values of one Diagnostics Relay group. Returns (fields, truncated)."""
    items = list(_leaves(data))
    fields = []
    for path, value in items[:MAX_GROUP_KEYS]:
        src = f"{DIAG_INFO_SRC}/{group}/{path}"
        res = Result.ok(value, src)
        fields.append(Field(f"diag:{group}/{path}", path, section, lambda ctx, r=res: r,
                            fmt=format_value, sensitive=any(w in path.lower() for w in _SENSITIVE_WORDS)))
    return fields, len(items) > MAX_GROUP_KEYS


def dynamic_diag_fields(info: Result, *, only: tuple[str, ...] | None = None) -> tuple[list[Field], list[str]]:
    """Dynamic fields from info(All); `only` restricts to given group names."""
    if not info.is_ok:
        return [], []
    fields, notes = [], []
    groups = {k: v for k, v in info.value.items() if isinstance(v, dict)}
    scalars = {k: v for k, v in info.value.items() if not isinstance(v, dict)}
    if scalars:
        groups = {"Общие": scalars, **groups}
    for name in list(groups)[:MAX_GROUPS]:
        if only is not None and name not in only:
            continue
        f, truncated = group_fields(name, groups[name], f"{name} (Diagnostics Relay)")
        fields += f
        if truncated:
            notes.append(f"{name}: показаны первые {MAX_GROUP_KEYS} ключей.")
    return fields, notes


# ---- availability of data sources -----------------------------------------

def _avail_dump(domain: str | None) -> Callable[[Collector], Result[Any]]:
    def reader(ctx: Collector) -> Result[Any]:
        d = ctx.dump(domain)
        return d if not d.is_ok else Result.ok(f"отвечает, ключей: {len(d.value)}", d.source)
    return reader


def _avail_service(get: Callable[[Collector], Result[Any]]) -> Callable[[Collector], Result[Any]]:
    def reader(ctx: Collector) -> Result[Any]:
        res = get(ctx)
        if not res.is_ok:
            return res
        n = sum(1 for v in res.value.values() if v is not None)
        return Result.ok(f"отвечает, значений: {n}", res.source)
    return reader


def _avail_gestalt(ctx: Collector) -> Result[Any]:
    g = hw.gestalt(ctx)
    if not g.is_ok:
        return g
    got = sum(1 for k in hw.GESTALT_KEYS if g.value.get(k) is not None)
    return Result.ok(f"отвечает, ключей: {got} из {len(hw.GESTALT_KEYS)} запрошенных", g.source)


# ---- fields ----------------------------------------------------------------

def _clock_check(now: Callable[[], float]) -> Field:
    def fn(device_ts: Any, ) -> tuple[str, str]:
        diff = abs(float(device_ts) - now())
        return finding(diff <= CLOCK_SKEW_S, f"часы устройства и хоста расходятся на {diff:.0f} с",
                       f"часы устройства и хоста расходятся на {diff:.0f} с (> {CLOCK_SKEW_S} с; "
                       "возможно, время изменено вручную)")
    return check_field("chk_clock", "Часы устройства ↔ хост", S_CONS, [lk("TimeIntervalSince1970")], fn,
                       source="расчёт iOSKit ← lockdownd:TimeIntervalSince1970, часы хоста",
                       formula=lambda t: f"|время устройства − время хоста| = {abs(float(t) - now()):.0f} с, порог {CLOCK_SKEW_S} с")


def _telephony_check(ctx: Collector) -> Result[Any]:
    src = "расчёт iOSKit ← lockdownd:TelephonyCapability, BasebandVersion"
    tel, bb = ctx.lookup("TelephonyCapability"), ctx.lookup("BasebandVersion")
    for r in (tel, bb):
        if r.status is Status.ERROR:
            return Result.fail(f"ошибка во входных данных: {r.reason}", source=src, hint=r.hint, code=r.code)
    if not tel.is_ok:
        return Result.unavailable(f"нет исходных данных: {tel.reason}", source=src)
    has = bb.is_ok
    if tel.value:
        f = finding(has, "сотовая связь заявлена, baseband сообщён",
                    "сотовая связь заявлена, но baseband не сообщён (возможна особенность iOS или режим устройства)")
    else:
        f = finding(not has, "сотовой связи нет, baseband не сообщён",
                    "сотовой связи нет, но baseband сообщён")
    return Result.ok(f, src, formula=f"TelephonyCapability={tel.value}, baseband {'есть' if has else 'нет'}")


_MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$", re.I)
_UDID = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{8}-[0-9a-f]{16})$", re.I)


def _consistency_fields(now: Callable[[], float]) -> list[Field]:
    S = "расчёт iOSKit ← "
    return [
        check_field("chk_class", "DeviceClass ↔ ProductType", S_CONS, [lk("DeviceClass"), lk("ProductType")],
                    lambda c, p: finding(str(p).startswith(str(c)), f"{c} согласуется с {p}",
                                         f"DeviceClass={c} не соответствует ProductType={p}"),
                    source=S + "lockdownd:DeviceClass, ProductType",
                    formula=lambda c, p: f"ProductType «{p}» начинается с DeviceClass «{c}»"),
        Field("chk_telephony", "Сотовая связь ↔ baseband", S_CONS, _telephony_check, fmt=format_finding),
        check_field("chk_data_disk", "Раздел данных ≤ накопитель", S_CONS,
                    [_r(_ST, "data_total"), _r(_ST, "disk_capacity")],
                    lambda d, t: finding(d <= t, "TotalDataCapacity не превышает TotalDiskCapacity",
                                         "TotalDataCapacity больше TotalDiskCapacity"),
                    source=S + "lockdownd:com.apple.disk_usage/TotalDataCapacity, TotalDiskCapacity",
                    formula=lambda d, t: f"TotalDataCapacity ≤ TotalDiskCapacity: {d} ≤ {t}"),
        check_field("chk_afc_free", "Свободно: lockdown ↔ AFC", S_CONS,
                    [_r(_ST, "data_free"), _r(_ST, "afc_free"), _r(_ST, "data_total")],
                    lambda a, b, t: finding(abs(a - b) <= AFC_TOLERANCE * t,
                                            f"значения совпадают в пределах {AFC_TOLERANCE:.0%} ёмкости",
                                            f"значения расходятся на {format_bytes(abs(a - b))} "
                                            f"(> {AFC_TOLERANCE:.0%} ёмкости; возможна разница в учёте освобождаемого места)"),
                    source=S + "lockdownd:TotalDataAvailable, afc:FSFreeBytes",
                    formula=lambda a, b, t: f"|{a} − {b}| ≤ {AFC_TOLERANCE:.0%} × {t}"),
        check_field("chk_mac", "Формат Wi-Fi/Bluetooth MAC", S_CONS, [lk("WiFiAddress"), lk("BluetoothAddress")],
                    lambda w, b: finding(bool(_MAC.match(str(w))) and bool(_MAC.match(str(b))),
                                         "оба адреса в корректном формате", "адрес в некорректном формате"),
                    source=S + "lockdownd:WiFiAddress, BluetoothAddress",
                    formula="формат xx:xx:xx:xx:xx:xx для обоих адресов"),
        check_field("chk_udid", "Формат UDID", S_CONS, [lk("UniqueDeviceID")],
                    lambda u: finding(bool(_UDID.match(str(u))), "UDID в корректном формате",
                                      "UDID в нестандартном формате"),
                    source=S + "lockdownd:UniqueDeviceID",
                    formula="40 hex-символов (старый формат) или 8-16 hex через дефис"),
        check_field("chk_batt_cap", "Батарея: текущая ≤ максимальной ёмкости", S_CONS,
                    [_r(_BAT, "raw_current"), _r(_BAT, "raw_max")],
                    lambda c, m: finding(c <= m * BATTERY_CAP_TOLERANCE, "AppleRawCurrentCapacity не превышает AppleRawMaxCapacity",
                                         "AppleRawCurrentCapacity больше AppleRawMaxCapacity"),
                    source=S + bat.PM_SRC + "/AppleRawCurrentCapacity, AppleRawMaxCapacity",
                    formula=lambda c, m: f"{c:g} ≤ {m:g} × {BATTERY_CAP_TOLERANCE}"),
        check_field("chk_health_methods", "Батарея: два метода оценки здоровья", S_CONS,
                    [_r(_BAT, "health_nominal"), _r(_BAT, "health_raw")],
                    lambda a, b: finding(abs(a - b) <= HEALTH_METHOD_GAP, f"методы расходятся на {abs(a - b):.1f} п.п.",
                                         f"методы расходятся на {abs(a - b):.1f} п.п. (> {HEALTH_METHOD_GAP:g}); значениям не стоит доверять"),
                    source=S + "расчётные значения здоровья батареи",
                    formula=lambda a, b: f"|{a:g} − {b:g}| ≤ {HEALTH_METHOD_GAP:g} п.п."),
        _clock_check(now),
    ]


def _health_any(ctx: Collector) -> Result[Any]:
    n, r = _r(_BAT, "health_nominal")(ctx), _r(_BAT, "health_raw")(ctx)
    if n.is_ok:
        return n
    if r.is_ok:
        return r
    return n if n.status is Status.ERROR or r.status is not Status.ERROR else r


def _warning_fields() -> list[Field]:
    S = "расчёт iOSKit ← "
    pm = lambda key: (lambda ctx: bat.pm_value(ctx, key))  # noqa: E731
    return [
        check_field("w_health", "Износ батареи", S_WARN, [_health_any],
                    lambda h: finding(h >= HEALTH_WARN_PCT, f"расчётное здоровье {h:g} %",
                                      f"расчётное здоровье {h:g} % ниже порога {HEALTH_WARN_PCT:g} %"),
                    source=S + "здоровье батареи (оценка)",
                    formula=lambda h: f"здоровье {h:g} % ≥ порога {HEALTH_WARN_PCT:g} % (эвристика iOSKit)"),
        check_field("w_temp", "Температура батареи", S_WARN, [_r(_BAT, "temperature")],
                    lambda t: finding(t < TEMP_WARN_C, f"{t:g} °C", f"{t:g} °C — не ниже порога {TEMP_WARN_C:g} °C"),
                    source=S + bat.PM_SRC + "/Temperature",
                    formula=lambda t: f"{t:g} °C < порога {TEMP_WARN_C:g} °C (эвристика iOSKit)"),
        check_field("w_critical", "Критический уровень заряда", S_WARN, [pm("AtCriticalLevel")],
                    lambda v: finding(not v, "флаг AtCriticalLevel не установлен", "устройство сообщает AtCriticalLevel = да"),
                    source=S + bat.PM_SRC + "/AtCriticalLevel",
                    formula=lambda v: f"AtCriticalLevel = {v!r}"),
        check_field("w_failure", "Флаг неисправности батареи", S_WARN, [pm("PermanentFailureStatus")],
                    lambda v: finding(v == 0, "PermanentFailureStatus = 0",
                                      f"PermanentFailureStatus = {v!r}: устройство сообщает о неисправности батареи"),
                    source=S + bat.PM_SRC + "/PermanentFailureStatus",
                    formula=lambda v: f"PermanentFailureStatus = {v!r}"),
        check_field("w_storage", "Заполнение хранилища", S_WARN, [_r(_ST, "used_pct")],
                    lambda p: finding(p < STORAGE_WARN_PCT, f"занято {p:g} %", f"занято {p:g} % — не ниже порога {STORAGE_WARN_PCT:g} %"),
                    source=S + "lockdownd:com.apple.disk_usage",
                    formula=lambda p: f"занято {p:g} % < порога {STORAGE_WARN_PCT:g} % (эвристика iOSKit)"),
    ]


def _build_fields(now: Callable[[], float]) -> list[Field]:
    return [
        Field("src_lockdown", "lockdownd (глобальный домен)", S_AVAIL, _avail_dump(None)),
        Field("src_battery", "lockdownd: com.apple.mobile.battery", S_AVAIL, _avail_dump(bat.BATTERY_DOMAIN)),
        Field("src_disk", "lockdownd: com.apple.disk_usage", S_AVAIL, _avail_dump(st.DOMAIN)),
        Field("src_ioreg", "Diagnostics Relay: IORegistry (IOPMPowerSource)", S_AVAIL,
              _avail_service(bat.pm_entry)),
        Field("src_info", "Diagnostics Relay: info(All)", S_AVAIL, _avail_service(diag_info)),
        Field("src_gestalt", "Diagnostics Relay: MobileGestalt", S_AVAIL, _avail_gestalt),
        Field("src_afc", "AFC (GetDeviceInfo)", S_AVAIL, _avail_service(st.afc_info)),

        L("ios_version", "Версия iOS", S_COMP, "ProductVersion"),
        L("has_battery", "Батарея установлена (lockdown)", S_COMP, "HasBattery", domain=bat.BATTERY_DOMAIN),
        Field("battery_installed", "Батарея: BatteryInstalled (IORegistry)", S_COMP,
              lambda ctx: bat.pm_value(ctx, "BatteryInstalled")),
        pick(hw.FIELDS, "disk_capacity", section=S_COMP, label="Накопитель: ёмкость"),
        pick(hw.FIELDS, "resolution", section=S_COMP, label="Дисплей: разрешение"),
        L("wifi_mac", "Wi-Fi модуль: адрес", S_COMP, "WiFiAddress", sensitive=True),
        L("bt_mac", "Bluetooth модуль: адрес", S_COMP, "BluetoothAddress", sensitive=True),
        L("telephony", "Сотовый модуль: TelephonyCapability", S_COMP, "TelephonyCapability"),
        L("baseband", "Сотовый модуль: baseband", S_COMP, "BasebandVersion", telephony=True),
        L("sim_status", "SIM: статус", S_COMP, "SIMStatus", telephony=True),

        pick(bat.FIELDS, "temperature", section=S_SENS, label="Температура батареи"),
        pick(bat.FIELDS, "voltage", section=S_SENS, label="Напряжение батареи"),
        pick(bat.FIELDS, "amperage", section=S_SENS, label="Ток батареи"),
        pick(bat.FIELDS, "health_nominal", section=S_SENS, label="Здоровье батареи (Nominal/Design)"),
        pick(bat.FIELDS, "health_raw", section=S_SENS, label="Здоровье батареи (RawMax/Design)"),
        *_consistency_fields(now),
        *_warning_fields(),
    ]


FIELDS: list[Field] = _build_fields(time.time)


def collect(device: Device, *, timeout: float = 15.0, now: Callable[[], float] | None = None,
            on_progress: ProgressCb | None = None) -> Report:
    trace("расширенная диагностика", source="diagnostics")
    collector = Collector(device, timeout=timeout, on_progress=on_progress)
    entries: list[Entry] = collector.run(FIELDS if now is None else _build_fields(now))
    notes = list(NOTES)

    extra, extra_notes = dynamic_diag_fields(diag_info(collector))
    if extra:
        entries += collector.run(extra)
        notes += extra_notes
    major = ios_major(collector)
    if major is not None and major >= 17:
        notes.append(NEW_IOS_NOTE)
    return Report(entries, collector.aborted, SECTIONS, tuple(notes), collect_warnings(entries))
