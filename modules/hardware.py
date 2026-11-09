"""Hardware: SoC, model, memory, storage capacity, display, hardware identifiers.

Sources
  * lockdownd global domain               - CPU architecture, ChipID, board, colours, ECID
  * lockdownd com.apple.disk_usage        - TotalDiskCapacity
  * Diagnostics Relay / MobileGestalt     - display size and scale (closed on new iOS)
  * static reference tables (derived)     - SoC name and RAM by ProductType

Not exposed by iOS (reported honestly, never faked): RAM measured on the device,
NAND type/vendor, CPU load or temperature.
"""
from __future__ import annotations

import dataclasses
from typing import Any

from ioskit.core.collector import Collector, Field, ProgressCb
from ioskit.core.device import Device
from ioskit.core.result import Result
from ioskit.modules.common import Report, as_number, lockdown_field as L, table_reader
from ioskit.utils.formatting import (format_bytes, format_hex, format_ram_mb,
                                     format_resolution, format_scale)
from ioskit.utils.models import chip_name, commercial_name, ram_reference_mb
from ioskit.utils.trace import trace

S_CPU = "Платформа и процессор"
S_MODEL = "Модель и корпус"
S_MEM = "Память и накопитель"
S_DISPLAY = "Дисплей"
S_IDS = "Аппаратные идентификаторы"
SECTIONS = (S_CPU, S_MODEL, S_MEM, S_DISPLAY, S_IDS)
TITLE = "Оборудование"
HEADER = (("Модель", "model"), ("Чип", "chip"), ("RAM (справочно)", "ram"), ("Архитектура", "cpu_arch"))

DISK_DOMAIN = "com.apple.disk_usage"
GESTALT_KEYS = ["main-screen-width", "main-screen-height", "main-screen-scale"]
GESTALT_SRC = "diagnostics_relay:MobileGestalt"

NOTES = (
    "Чип и RAM — справочные значения из таблицы iOSKit по ProductType, а не измерение: "
    "iOS не сообщает объём ОЗУ через публичные сервисы.",
    "Тип и производитель NAND, загрузка и температура CPU через lockdown/Diagnostics недоступны.",
    "Дисплей читается из MobileGestalt; на новых версиях iOS Apple закрыла этот сервис.",
)


def _gestalt(ctx: Collector) -> Result[Any]:
    return ctx.service("mobilegestalt", lambda: ctx.device.mobilegestalt(GESTALT_KEYS),
                       source=GESTALT_SRC)


def _gestalt_number(ctx: Collector, key: str, lo: float, hi: float) -> Result[Any]:
    src = f"{GESTALT_SRC}/{key}"
    g = _gestalt(ctx)
    if not g.is_ok:
        return dataclasses.replace(g, source=src)
    v = g.value.get(key)
    if v is None:
        return Result.unavailable("ключ не возвращён MobileGestalt (зависит от модели и iOS)", source=src)
    return as_number(Result.ok(v, src), lo=lo, hi=hi)


def _resolution(ctx: Collector) -> Result[Any]:
    w = _gestalt_number(ctx, "main-screen-width", 100, 10000)
    h = _gestalt_number(ctx, "main-screen-height", 100, 10000)
    for r in (w, h):
        if not r.is_ok:
            return r
    return Result.ok((int(w.value), int(h.value)),
                     f"{GESTALT_SRC}/main-screen-width,main-screen-height")


def _scale(ctx: Collector) -> Result[Any]:
    return _gestalt_number(ctx, "main-screen-scale", 1, 5)


def _build_fields() -> list[Field]:
    disk = lambda res: as_number(res, lo=1, integer=True)  # noqa: E731
    return [
        L("cpu_arch", "Архитектура CPU", S_CPU, "CPUArchitecture"),
        Field("chip", "Чип (SoC, справочно)", S_CPU, table_reader(chip_name, "чип")),
        L("chip_id", "ChipID", S_CPU, "ChipID", fmt=lambda v: format_hex(v, width=4),
          check=lambda r: as_number(r, lo=0, integer=True)),
        L("platform", "Hardware platform", S_CPU, "HardwarePlatform"),
        L("board_id", "BoardId", S_CPU, "BoardId"),
        L("hardware_model", "Hardware model", S_CPU, "HardwareModel"),
        L("production_soc", "Серийный (production) SoC", S_CPU, "ProductionSOC"),

        Field("model", "Модель", S_MODEL, table_reader(commercial_name, "название модели")),
        L("product_type", "ProductType", S_MODEL, "ProductType"),
        L("model_number", "Номер модели", S_MODEL, "ModelNumber"),
        L("color", "Цвет (DeviceColor)", S_MODEL, "DeviceColor"),
        L("enclosure", "Цвет корпуса", S_MODEL, "DeviceEnclosureColor"),

        Field("ram", "RAM (справочно)", S_MEM, table_reader(ram_reference_mb, "объём RAM"),
              fmt=format_ram_mb),
        L("disk_capacity", "Ёмкость накопителя", S_MEM, "TotalDiskCapacity",
          domain=DISK_DOMAIN, fmt=format_bytes, check=disk),

        Field("resolution", "Разрешение экрана", S_DISPLAY, _resolution, fmt=format_resolution),
        Field("scale", "Масштаб (scale)", S_DISPLAY, _scale, fmt=format_scale),

        L("ecid", "ECID (UniqueChipID)", S_IDS, "UniqueChipID", sensitive=True,
          fmt=lambda v: format_hex(v, show_dec=False), check=lambda r: as_number(r, lo=0, integer=True)),
        L("serial", "Серийный номер", S_IDS, "SerialNumber", sensitive=True),
        L("wifi_mac", "Wi-Fi MAC", S_IDS, "WiFiAddress", sensitive=True),
        L("bt_mac", "Bluetooth MAC", S_IDS, "BluetoothAddress", sensitive=True),
    ]


FIELDS: list[Field] = _build_fields()


def collect(device: Device, *, timeout: float = 15.0, on_progress: ProgressCb | None = None) -> Report:
    trace(f"сбор оборудования: {len(FIELDS)} параметров", source="hardware")
    collector = Collector(device, timeout=timeout, on_progress=on_progress)
    entries = collector.run(FIELDS)
    return Report(entries, collector.aborted, SECTIONS, NOTES)


# public alias for other modules (diagnostics)
gestalt = _gestalt
