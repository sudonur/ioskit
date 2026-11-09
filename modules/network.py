"""Network diagnostics available over USB.

What iOS really exposes through lockdown/Diagnostics Relay is limited: SIM and
carrier-bundle identity, baseband, radio MAC addresses, Wi-Fi sync flags. The
device's own IP addresses, SSID, signal strength and network type are NOT exposed
and are never guessed. Saved Wi-Fi passwords are never requested.

The IP block describes the Linux HOST side (ipheth interface of USB tethering).
No internet requests are made here.
"""
from __future__ import annotations

import statistics
import time
from typing import Any, Callable

from ioskit.core import connection
from ioskit.core.collector import Collector, Entry, Field, ProgressCb
from ioskit.core.device import Device
from ioskit.core.result import Result, Status
from ioskit.modules.common import NO_CELLULAR, Report, lockdown_field as L
from ioskit.modules.diagnostics import diag_info, dynamic_diag_fields
from ioskit.utils import hostnet
from ioskit.utils.formatting import format_latency
from ioskit.utils.trace import trace

S_SIM = "Оператор и SIM (lockdown)"
S_CELL = "Сотовая сеть (lockdown)"
S_WIFI = "Wi-Fi и Bluetooth"
S_IP = "IP-информация (сторона хоста)"
S_LINK = "Диагностика USB-соединения"
S_ERR = "Сетевые ошибки"
SECTIONS = (S_SIM, S_CELL, S_WIFI, S_IP, S_LINK, S_ERR)
TITLE = "Сеть"
HEADER = (("SIM", "sim_status"), ("MCC", "mcc"), ("MNC", "mnc"), ("Задержка lockdown", "latency"))

WIRELESS_DOMAIN = "com.apple.mobile.wireless_lockdown"
LATENCY_SAMPLES = 5
LATENCY_SRC = "измерено iOSKit: lockdownd GetValue(ProductType)"

NOTES = (
    "Название оператора, уровень сигнала, тип сети (LTE/5G), роуминг и собственные IP-адреса iPhone "
    "через USB-сервисы не передаются; iOSKit их не показывает и не оценивает.",
    "Сохранённые пароли Wi-Fi и SSID не запрашиваются.",
    "Блок IP описывает хост: интерфейс ipheth появляется только при включённом Personal Hotspot по USB; "
    "адрес шлюза на нём — адрес iPhone в этой сети.",
    "Этот модуль не делает запросов в интернет.",
)


# ---- carrier ---------------------------------------------------------------

def _cellular_guard(ctx: Collector, res: Result[Any]) -> Result[Any]:
    if res.status is Status.UNAVAILABLE:
        tel = ctx.lookup("TelephonyCapability")
        if tel.is_ok and tel.value is False:
            return Result.unavailable(NO_CELLULAR, source=res.source)
    return res


def _carrier_entries(ctx: Collector) -> Result[Any]:
    res = ctx.lookup("CarrierBundleInfoArray")
    if not res.is_ok:
        return res
    entries = [e for e in res.value if isinstance(e, dict)] if isinstance(res.value, list) else None
    if entries is None:
        return Result.fail("неожиданный тип CarrierBundleInfoArray", source=res.source)
    if not entries:
        return Result.unavailable("список пакетов оператора пуст (SIM не вставлена или не активирована)",
                                  source=res.source)
    return Result.ok(entries, res.source)


def _carrier_value(global_key: str | None, *array_keys: str) -> Callable[[Collector], Result[Any]]:
    """Global lockdown key first, then the first CarrierBundleInfoArray entry."""
    def reader(ctx: Collector) -> Result[Any]:
        if global_key:
            g = ctx.lookup(global_key)
            if g.is_ok or g.status is Status.ERROR:
                return g
        arr = _carrier_entries(ctx)
        if not arr.is_ok:
            return _cellular_guard(ctx, arr)
        first = arr.value[0]
        for k in array_keys:
            if first.get(k) not in (None, ""):
                return Result.ok(first[k], f"{arr.source}[0]/{k}")
        return _cellular_guard(ctx, Result.unavailable("ключ отсутствует в пакете оператора",
                                                       source=f"{arr.source}[0]/{'|'.join(array_keys)}"))
    return reader


def _slots(ctx: Collector) -> Result[Any]:
    arr = _carrier_entries(ctx)
    return _cellular_guard(ctx, arr) if not arr.is_ok else Result.ok(len(arr.value), arr.source)


# ---- host IP block ---------------------------------------------------------

def _host(ctx: Collector, probe: Callable[[], Result[Any]]) -> Result[Any]:
    return ctx.external("hostnet", probe, source=hostnet.SOURCE, timeout=10.0)


_NO_IFACE = ("ipheth-интерфейс не найден: Personal Hotspot по USB не активен "
             "или драйвер ipheth не загружен")


def _iface_field(key: str, label: str, getter: Callable[[Any], Any], empty: str,
                 probe: Callable[[], Result[Any]], formula: str | None = None) -> Field:
    def reader(ctx: Collector) -> Result[Any]:
        res = _host(ctx, probe)
        if not res.is_ok:
            return res
        if not res.value:
            return Result.unavailable(_NO_IFACE, source=res.source)
        value = getter(res.value)
        if not value:
            return Result.unavailable(empty, source=res.source)
        return Result.ok(value, res.source, formula=formula)
    return Field(key, label, S_IP, reader)


# ---- connection diagnostics ------------------------------------------------

def _usbmuxd(ctx: Collector) -> Result[Any]:
    if connection.usbmuxd_socket_present():
        return Result.ok(f"сокет {connection.USBMUXD_SOCKET} существует", "хост: usbmuxd")
    return Result.unavailable(f"сокет {connection.USBMUXD_SOCKET} не найден", source="хост: usbmuxd")


def _mux_entry(ctx: Collector) -> Result[Any]:
    listed = connection.list_devices()
    if not listed.is_ok:
        return listed
    for e in listed.value or []:
        if e.udid == ctx.device.udid:
            return Result.ok(f"{e.connection_type} через usbmuxd", "usbmuxd")
    return Result.unavailable("устройство не найдено в списке usbmuxd (при активной сессии lockdown)",
                              source="usbmuxd")


def _crosscheck(ctx: Collector) -> Result[Any]:
    res = connection.crosscheck_idevice_id()
    if not res.is_ok:
        return res
    seen = ctx.device.udid in (res.value or [])
    return Result.ok("устройство видно" if seen else
                     f"устройство НЕ видно (libimobiledevice видит {len(res.value or [])} шт.)", res.source)


def _latency(ctx: Collector) -> Result[Any]:
    def measure() -> Result[Any]:
        times = []
        for _ in range(LATENCY_SAMPLES):
            t0 = time.perf_counter()
            r = ctx.device.read("ProductType")
            times.append((time.perf_counter() - t0) * 1000)
            if not r.is_ok:
                return r
        stats = {"median": statistics.median(times), "min": min(times), "max": max(times), "n": len(times)}
        return Result.ok(stats, LATENCY_SRC, formula=f"медиана {LATENCY_SAMPLES} последовательных замеров")
    return ctx.service("latency", measure, source=LATENCY_SRC)


def _build_fields(probe: Callable[[], Result[Any]]) -> list[Field]:
    return [
        L("sim_status", "Статус SIM", S_SIM, "SIMStatus", telephony=True),
        L("sim_tray", "Лоток SIM", S_SIM, "SIMTrayStatus", telephony=True),
        L("phone", "Номер телефона", S_SIM, "PhoneNumber", sensitive=True, telephony=True),
        Field("mcc", "MCC (код страны оператора)", S_SIM,
              _carrier_value("MobileSubscriberCountryCode", "MCC")),
        Field("mnc", "MNC (код сети оператора)", S_SIM,
              _carrier_value("MobileSubscriberNetworkCode", "MNC")),
        Field("carrier_bundle", "Пакет оператора (bundle id)", S_SIM, _carrier_value(None, "CFBundleIdentifier")),
        Field("carrier_version", "Версия пакета оператора", S_SIM,
              _carrier_value(None, "CFBundleVersion", "Version")),
        Field("carrier_slots", "Пакетов оператора (слотов)", S_SIM, _slots),

        L("telephony", "Сотовая связь (TelephonyCapability)", S_CELL, "TelephonyCapability"),
        L("baseband", "Версия baseband", S_CELL, "BasebandVersion", telephony=True),
        L("baseband_status", "Состояние baseband", S_CELL, "BasebandStatus", telephony=True),

        L("wifi_mac", "Wi-Fi MAC", S_WIFI, "WiFiAddress", sensitive=True),
        L("bt_mac", "Bluetooth MAC", S_WIFI, "BluetoothAddress", sensitive=True),
        L("wifi_sync", "Подключение по Wi-Fi разрешено", S_WIFI, "EnableWifiConnections", domain=WIRELESS_DOMAIN),
        L("wifi_sync_support", "Синхронизация по Wi-Fi поддерживается", S_WIFI, "SupportsWifiSyncing",
          domain=WIRELESS_DOMAIN),

        _iface_field("tether_iface", "USB-модем: интерфейс хоста", lambda v: ", ".join(i.name for i in v),
                     "интерфейс найден, но имя не определено", probe),
        _iface_field("host_ip", "USB-модем: IPv4 хоста", lambda v: ", ".join(ip for i in v for ip in i.ipv4),
                     "на интерфейсе ipheth нет IPv4-адреса", probe),
        _iface_field("device_ip", "USB-модем: адрес iPhone (шлюз)", lambda v: ", ".join(i.gateway for i in v if i.gateway),
                     "шлюз в таблице маршрутов не найден", probe,
                     formula="gateway маршрута интерфейса ipheth (интерпретация iOSKit: шлюз = iPhone)"),

        Field("usbmuxd", "Служба usbmuxd", S_LINK, _usbmuxd),
        Field("mux_entry", "Соединение с устройством", S_LINK, _mux_entry),
        Field("crosscheck", "Перекрёстная проверка libimobiledevice", S_LINK, _crosscheck),
        Field("latency", "Задержка ответа lockdown", S_LINK, _latency, fmt=format_latency),
    ]


def collect(device: Device, *, timeout: float = 15.0, host: bool = True,
            host_probe: Callable[[], Result[Any]] | None = None,
            on_progress: ProgressCb | None = None) -> Report:
    trace(f"сетевая диагностика (хост-проба: {'да' if host else 'нет'})", source="network")
    disabled = Result.unavailable("проверка хоста отключена (--no-host)", source=hostnet.SOURCE)
    probe = host_probe or hostnet.list_ipheth
    fields = _build_fields((lambda: disabled) if not host else probe)
    collector = Collector(device, timeout=timeout, on_progress=on_progress)
    entries: list[Entry] = collector.run(fields)

    extra, extra_notes = dynamic_diag_fields(diag_info(collector), only=("WiFi",))
    notes = list(NOTES) + extra_notes
    if extra:
        entries += collector.run(extra)

    errors = [e for e in entries if e.result.status is Status.ERROR]
    summary = (Result.ok("нет", "итог сбора iOSKit", formula="число записей со статусом ERROR = 0") if not errors else
               Result.ok(f"{len(errors)}: " + "; ".join(f"{e.field.label} ({e.result.reason})" for e in errors),
                         "итог сбора iOSKit", formula=f"число записей со статусом ERROR = {len(errors)}"))
    entries.append(Entry(Field("net_errors", "Ошибки при сборе сетевых данных", S_ERR, lambda ctx: summary),
                         summary))
    return Report(entries, collector.aborted, SECTIONS, tuple(notes))
