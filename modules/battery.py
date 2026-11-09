"""Battery analytics.

Sources
  * lockdownd com.apple.mobile.battery: level, charging flags. Works on every iOS.
  * Diagnostics Relay -> IORegistry IOPMPowerSource: cycles, capacities, voltage,
    temperature, current. UNDOCUMENTED by Apple: key names/units differ between
    models and iOS versions, so every number is range-checked and anything
    implausible is reported UNAVAILABLE instead of being shown.

Derived values (health %, temperature, signed current) carry derived=True and
the exact formula with the input numbers. Health is computed only when BOTH
inputs exist and their ratio is plausible.
"""
from __future__ import annotations

import dataclasses
from typing import Any

from ioskit.core.collector import Collector, Field, ProgressCb
from ioskit.core.device import Device
from ioskit.core.result import Result, derive
from ioskit.modules.common import Report, as_number, lockdown_field as L
from ioskit.utils.formatting import (format_celsius, format_ma, format_mah, format_mv,
                                     format_percent, format_watts)
from ioskit.utils.trace import trace

BATTERY_DOMAIN = "com.apple.mobile.battery"
PM_CLASS = "IOPMPowerSource"
PM_SRC = f"diagnostics_relay:IORegistry/{PM_CLASS}"

S_CHARGE = "Заряд (lockdown)"
S_CAP = "Ёмкость и износ (IORegistry)"
S_ELEC = "Электрические параметры (IORegistry)"
SECTIONS = (S_CHARGE, S_CAP, S_ELEC)
TITLE = "Батарея"
HEADER = (("Заряд", "level"), ("Зарядка", "charging"), ("Циклы", "cycles"),
          ("Здоровье", ("health_nominal", "health_raw")))

# plausibility windows: values outside are not trusted
CAPACITY_MAH = (100, 10000)
VOLTAGE_MV = (2500, 5000)
TEMP_C = (-20.0, 80.0)
CURRENT_MA = (-20000, 20000)
CYCLES = (0, 20000)
HEALTH_RATIO = (0.2, 1.3)

NOTES = (
    "Уровень и признаки зарядки читаются через lockdown (com.apple.mobile.battery) и доступны на всех версиях iOS.",
    "Остальное — из IORegistry (IOPMPowerSource): интерфейс не документирован Apple, "
    "ключи и единицы зависят от модели и версии iOS; значения вне правдоподобного диапазона отбрасываются.",
    "«Здоровье» — расчёт iOSKit (ёмкость / проектная ёмкость). Оно может не совпадать с "
    "«Максимальной ёмкостью» в Настройках iOS.",
)


def _pm(ctx: Collector) -> Result[Any]:
    return ctx.service("ioreg:" + PM_CLASS, lambda: ctx.device.ioregistry(PM_CLASS),
                       source=f"diagnostics_relay:IORegistry({PM_CLASS})")


def _pm_value(ctx: Collector, key: str) -> Result[Any]:
    """Raw key from IOPMPowerSource; falls back to the nested BatteryData dict."""
    src = f"{PM_SRC}/{key}"
    pm = _pm(ctx)
    if not pm.is_ok:
        return dataclasses.replace(pm, source=src)
    data = pm.value
    if data.get(key) not in (None, ""):
        return Result.ok(data[key], src)
    nested = data.get("BatteryData")
    if isinstance(nested, dict) and nested.get(key) not in (None, ""):
        return Result.ok(nested[key], f"{PM_SRC}/BatteryData/{key}")
    return Result.unavailable("ключ отсутствует в IOPMPowerSource (зависит от модели и iOS)", source=src)


def _pm_number(key: str, lo: float, hi: float):
    return lambda ctx: as_number(_pm_value(ctx, key), lo=lo, hi=hi, integer=True)


_design = _pm_number("DesignCapacity", *CAPACITY_MAH)
_nominal = _pm_number("NominalChargeCapacity", *CAPACITY_MAH)
_raw_max = _pm_number("AppleRawMaxCapacity", *CAPACITY_MAH)


def _health(cap_fn, cap_key: str):
    def reader(ctx: Collector) -> Result[Any]:
        design, cap = _design(ctx), cap_fn(ctx)
        src = f"расчёт iOSKit ← {PM_SRC}/DesignCapacity, {cap_key}"
        if design.is_ok and cap.is_ok:
            ratio = cap.value / design.value
            if not (HEALTH_RATIO[0] <= ratio <= HEALTH_RATIO[1]):
                return Result.unavailable(
                    f"неправдоподобное отношение {cap_key}/DesignCapacity = "
                    f"{cap.value:g}/{design.value:g}; здоровье не рассчитывается", source=src)
        return derive(lambda d, c: round(c / d * 100, 1), design, cap, source=src,
                      formula=lambda d, c: f"{cap_key} / DesignCapacity × 100 = {c:g} / {d:g} × 100")
    return reader


def _temperature(ctx: Collector) -> Result[Any]:
    raw = as_number(_pm_value(ctx, "Temperature"))
    if not raw.is_ok:
        return raw
    celsius = raw.value / 100
    if not (TEMP_C[0] <= celsius <= TEMP_C[1]):
        return Result.unavailable(
            f"неправдоподобное значение Temperature={raw.value:g} (÷100 = {celsius:.1f} °C)",
            source=raw.source)
    return Result.ok(round(celsius, 2), raw.source,
                     formula=f"Temperature / 100 = {raw.value:g} / 100 "
                             "(единицы 0.01 °C — допущение, Apple не документирует)")


def _amperage(ctx: Collector) -> Result[Any]:
    raw = as_number(_pm_value(ctx, "InstantAmperage"), integer=True)
    if not raw.is_ok:
        return raw
    v, formula = raw.value, None
    if v >= 2 ** 63:  # known quirk: negative current reported as unsigned 64-bit
        v, formula = v - 2 ** 64, f"InstantAmperage − 2^64 = {raw.value} − 2^64 (знаковое 64-бит)"
    if not (CURRENT_MA[0] <= v <= CURRENT_MA[1]):
        return Result.unavailable(f"неправдоподобное значение тока: {v}", source=raw.source)
    return Result.ok(v, raw.source, formula=formula) if formula else Result.ok(v, raw.source)


def _adapter_watts(ctx: Collector) -> Result[Any]:
    ad = _pm_value(ctx, "AdapterDetails")
    if not ad.is_ok:
        return ad
    watts = ad.value.get("Watts") if isinstance(ad.value, dict) else None
    src = f"{PM_SRC}/AdapterDetails/Watts"
    if watts is None:
        return Result.unavailable("адаптер не подключён или не сообщает мощность", source=src)
    return as_number(Result.ok(watts, src), lo=0, hi=500)


def _build_fields() -> list[Field]:
    pct = lambda r: as_number(r, lo=0, hi=100)  # noqa: E731
    return [
        L("level", "Уровень заряда", S_CHARGE, "BatteryCurrentCapacity", domain=BATTERY_DOMAIN,
          fmt=format_percent, check=pct),
        L("charging", "Идёт зарядка", S_CHARGE, "BatteryIsCharging", domain=BATTERY_DOMAIN),
        L("external", "Внешнее питание подключено", S_CHARGE, "ExternalConnected", domain=BATTERY_DOMAIN),
        L("full", "Полностью заряжена", S_CHARGE, "FullyCharged", domain=BATTERY_DOMAIN),
        L("has_battery", "Батарея установлена", S_CHARGE, "HasBattery", domain=BATTERY_DOMAIN),

        Field("cycles", "Циклы зарядки (CycleCount)", S_CAP, _pm_number("CycleCount", *CYCLES)),
        Field("design", "Проектная ёмкость (DesignCapacity)", S_CAP, _design, fmt=format_mah),
        Field("nominal", "Номинальная ёмкость (NominalChargeCapacity)", S_CAP, _nominal, fmt=format_mah),
        Field("raw_max", "Макс. ёмкость (AppleRawMaxCapacity)", S_CAP, _raw_max, fmt=format_mah),
        Field("raw_current", "Текущая ёмкость (AppleRawCurrentCapacity)", S_CAP,
              _pm_number("AppleRawCurrentCapacity", *CAPACITY_MAH), fmt=format_mah),
        Field("health_nominal", "Здоровье (Nominal / Design)", S_CAP,
              _health(_nominal, "NominalChargeCapacity"), fmt=format_percent),
        Field("health_raw", "Здоровье (RawMax / Design)", S_CAP,
              _health(_raw_max, "AppleRawMaxCapacity"), fmt=format_percent),

        Field("voltage", "Напряжение", S_ELEC, _pm_number("Voltage", *VOLTAGE_MV), fmt=format_mv),
        Field("temperature", "Температура батареи", S_ELEC, _temperature, fmt=format_celsius),
        Field("amperage", "Ток (InstantAmperage)", S_ELEC, _amperage, fmt=format_ma),
        Field("adapter", "Мощность адаптера", S_ELEC, _adapter_watts, fmt=format_watts),
    ]


FIELDS: list[Field] = _build_fields()


def collect(device: Device, *, timeout: float = 15.0, on_progress: ProgressCb | None = None) -> Report:
    trace(f"сбор данных батареи: {len(FIELDS)} параметров", source="battery")
    collector = Collector(device, timeout=timeout, on_progress=on_progress)
    entries = collector.run(FIELDS)
    return Report(entries, collector.aborted, SECTIONS, NOTES)


# public aliases for other modules (diagnostics)
pm_entry = _pm
pm_value = _pm_value
