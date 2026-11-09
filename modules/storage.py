"""Storage analysis.

Sources
  * lockdownd com.apple.disk_usage   - capacities of the data/system partitions and
                                       per-category *Usage keys (whatever iOS returns)
  * AFC GetDeviceInfo                - media partition totals (cross-check)
  * installation_proxy               - app sizes (StaticDiskUsage/DynamicDiskUsage)

"Used" and "% used" are derived from two lockdown values, with the formula shown,
and only when the pair is consistent. The file-system TYPE is not exposed by
lockdown or AFC, so it is reported as unavailable instead of being inferred.
"""
from __future__ import annotations

from typing import Any

from ioskit.core.collector import Collector, Entry, Field, ProgressCb
from ioskit.core.device import Device
from ioskit.core.result import Result, derive
from ioskit.modules.common import Report, as_number, lockdown_field as L
from ioskit.utils.formatting import format_bytes, format_percent, format_value
from ioskit.utils.trace import trace

DOMAIN = "com.apple.disk_usage"
S_DATA = "Раздел данных (lockdown)"
S_SYSTEM = "Системный раздел (lockdown)"
S_AFC = "Файловая система (AFC)"
S_USAGE = "Использование по категориям (lockdown)"
S_APPS = "Данные приложений (installation_proxy)"
S_TOP = "Крупнейшие приложения"
SECTIONS = (S_DATA, S_SYSTEM, S_AFC, S_USAGE, S_APPS, S_TOP)
TITLE = "Хранилище"
HEADER = (("Всего (данные)", "data_total"), ("Свободно", "data_free"),
          ("Занято", "used"), ("Использовано", "used_pct"))

FS_TYPE_REASON = "тип ФС не раскрывается ни lockdown, ни AFC; вывод по версии iOS ненадёжен, поэтому не делается"

BASE_NOTES = (
    "Размеры десятичные (1 ГБ = 10^9 байт), как в iOS; цифры могут отличаться от «Настроек» — "
    "iOS по-своему учитывает освобождаемое пространство.",
    "«Занято» и «%» — расчёт iOSKit по двум значениям lockdown; формула и числа показаны в строке.",
)


def _bytes(res: Result, *, accept_str: bool = False) -> Result[Any]:
    return as_number(res, lo=0, integer=True, accept_str=accept_str)


def _pair(ctx: Collector) -> tuple[Result, Result]:
    return (_bytes(ctx.lookup("TotalDataCapacity", DOMAIN)),
            _bytes(ctx.lookup("TotalDataAvailable", DOMAIN)))


def _consistent(total: Result, free: Result, src: str) -> Result | None:
    if total.is_ok and free.is_ok and (total.value <= 0 or free.value > total.value):
        return Result.unavailable(
            f"противоречивые данные (всего {total.value}, свободно {free.value}); не рассчитывается",
            source=src)
    return None


_DERIVED_SRC = (f"расчёт iOSKit ← lockdownd:{DOMAIN}/TotalDataCapacity, TotalDataAvailable")


def _used(ctx: Collector) -> Result[Any]:
    total, free = _pair(ctx)
    bad = _consistent(total, free, _DERIVED_SRC)
    if bad:
        return bad
    return derive(lambda t, f: t - f, total, free, source=_DERIVED_SRC,
                  formula=lambda t, f: f"TotalDataCapacity − TotalDataAvailable = {t} − {f}")


def _used_pct(ctx: Collector) -> Result[Any]:
    total, free = _pair(ctx)
    bad = _consistent(total, free, _DERIVED_SRC)
    if bad:
        return bad
    return derive(lambda t, f: round((t - f) / t * 100, 1), total, free, source=_DERIVED_SRC,
                  formula=lambda t, f: f"(TotalDataCapacity − TotalDataAvailable) / TotalDataCapacity × 100"
                                       f" = ({t} − {f}) / {t} × 100")


def _afc(ctx: Collector) -> Result[Any]:
    return ctx.service("afc", ctx.device.afc_info, source="afc:GetDeviceInfo")


def _afc_field(key: str, label: str, afc_key: str, fmt=format_bytes) -> Field:
    def reader(ctx: Collector) -> Result[Any]:
        src = f"afc:GetDeviceInfo/{afc_key}"
        info = _afc(ctx)
        if not info.is_ok:
            import dataclasses
            return dataclasses.replace(info, source=src)
        v = info.value.get(afc_key)
        if v is None:
            return Result.unavailable("ключ не возвращён AFC", source=src)
        return _bytes(Result.ok(v, src), accept_str=True)  # AFC returns numbers as strings
    return Field(key, label, S_AFC, reader, fmt=fmt)


def _fs_type(ctx: Collector) -> Result[Any]:
    return Result.unavailable(FS_TYPE_REASON, source="lockdownd/afc")


def _lk_bytes(key: str, label: str, section: str, lk_key: str) -> Field:
    return L(key, label, section, lk_key, domain=DOMAIN, fmt=format_bytes, check=_bytes)


def _fixed_fields() -> list[Field]:
    return [
        _lk_bytes("disk_capacity", "Ёмкость накопителя (TotalDiskCapacity)", S_DATA, "TotalDiskCapacity"),
        L("data_total", "Всего (TotalDataCapacity)", S_DATA, "TotalDataCapacity", domain=DOMAIN,
          fmt=format_bytes, check=_bytes),
        L("data_free", "Свободно (TotalDataAvailable)", S_DATA, "TotalDataAvailable", domain=DOMAIN,
          fmt=format_bytes, check=_bytes),
        Field("used", "Занято", S_DATA, _used, fmt=format_bytes),
        Field("used_pct", "Использовано, %", S_DATA, _used_pct, fmt=format_percent),
        _lk_bytes("data_avail2", "AmountDataAvailable", S_DATA, "AmountDataAvailable"),
        _lk_bytes("reserved", "Зарезервировано (AmountDataReserved)", S_DATA, "AmountDataReserved"),

        _lk_bytes("sys_total", "Всего (TotalSystemCapacity)", S_SYSTEM, "TotalSystemCapacity"),
        _lk_bytes("sys_free", "Свободно (TotalSystemAvailable)", S_SYSTEM, "TotalSystemAvailable"),

        Field("fs_type", "Тип файловой системы", S_AFC, _fs_type),
        _afc_field("afc_total", "Всего (FSTotalBytes)", "FSTotalBytes"),
        _afc_field("afc_free", "Свободно (FSFreeBytes)", "FSFreeBytes"),
        _afc_field("afc_block", "Размер блока (FSBlockSize)", "FSBlockSize", fmt=lambda v: f"{v} байт"),
    ]


FIXED_FIELDS: list[Field] = _fixed_fields()


# ---- apps --------------------------------------------------------------

def _apps(ctx: Collector, timeout: float) -> Result[Any]:
    return ctx.service("apps", lambda: ctx.device.installed_apps(True),
                       source="installation_proxy:Browse(User)", timeout=timeout)


def _usage_values(apps: dict, key: str) -> list[int]:
    return [a[key] for a in apps.values()
            if isinstance(a, dict) and isinstance(a.get(key), int) and not isinstance(a.get(key), bool)]


def _app_fields(apps_timeout: float) -> list[Field]:
    def count(ctx: Collector) -> Result[Any]:
        res = _apps(ctx, apps_timeout)
        return res if not res.is_ok else Result.ok(len(res.value), res.source)

    def total(key: str):
        def reader(ctx: Collector) -> Result[Any]:
            res = _apps(ctx, apps_timeout)
            if not res.is_ok:
                return res
            vals = _usage_values(res.value, key)
            src = f"installation_proxy:Browse(User)/{key}"
            if not vals:
                return Result.unavailable(f"installation_proxy не вернул {key} (зависит от iOS)", source=src)
            return Result.ok(sum(vals), src,
                             formula=f"Σ {key} по {len(vals)} из {len(res.value)} приложений")
        return reader

    return [
        Field("apps_count", "Пользовательских приложений", S_APPS, count),
        Field("apps_static", "Размер приложений (StaticDiskUsage)", S_APPS, total("StaticDiskUsage"),
              fmt=format_bytes),
        Field("apps_dynamic", "Данные приложений (DynamicDiskUsage)", S_APPS, total("DynamicDiskUsage"),
              fmt=format_bytes),
    ]


def _top_fields(apps: dict, top: int) -> list[Field]:
    rows = []
    for bundle, info in apps.items():
        if not isinstance(info, dict):
            continue
        s, d = info.get("StaticDiskUsage"), info.get("DynamicDiskUsage")
        s = s if isinstance(s, int) and not isinstance(s, bool) else None
        d = d if isinstance(d, int) and not isinstance(d, bool) else None
        if s is None and d is None:
            continue
        name = info.get("CFBundleDisplayName") or info.get("CFBundleName") or bundle
        rows.append((bundle, str(name), s, d))
    rows.sort(key=lambda r: (r[2] or 0) + (r[3] or 0), reverse=True)
    fields = []
    for i, (bundle, name, s, d) in enumerate(rows[:top], 1):
        src = f"installation_proxy:{bundle}"
        formula = (f"StaticDiskUsage + DynamicDiskUsage = {format_value(s) if s is not None else 'н/д'}"
                   f" + {format_value(d) if d is not None else 'н/д'}")
        res = Result.ok((s or 0) + (d or 0), src, formula=formula)
        fields.append(Field(f"top{i}", name, S_TOP, lambda ctx, r=res: r, fmt=format_bytes))
    return fields


def _usage_fields(dump: dict) -> list[Field]:
    keys = sorted(k for k, v in dump.items()
                  if k.endswith("Usage") and isinstance(v, int) and not isinstance(v, bool))
    return [_lk_bytes(f"usage_{k}", k, S_USAGE, k) for k in keys]


def collect(device: Device, *, timeout: float = 15.0, include_apps: bool = True, top: int = 5,
            apps_timeout: float = 30.0, on_progress: ProgressCb | None = None) -> Report:
    trace(f"анализ хранилища (приложения: {'да' if include_apps else 'нет'})", source="storage")
    collector = Collector(device, timeout=timeout, on_progress=on_progress)
    entries: list[Entry] = collector.run(FIXED_FIELDS)
    notes = list(BASE_NOTES)

    dump = collector.dump(DOMAIN)
    usage = _usage_fields(dump.value) if dump.is_ok else []
    if usage:
        entries += collector.run(usage)
    elif dump.is_ok:
        notes.append("iOS не вернул разбивку по категориям (ключи *Usage) — на этой версии она недоступна.")

    if include_apps:
        entries += collector.run(_app_fields(apps_timeout))
        apps = _apps(collector, apps_timeout)
        if apps.is_ok and top > 0:
            entries += collector.run(_top_fields(apps.value, top))
        notes.append("Размеры приложений — из installation_proxy; доступность DynamicDiskUsage зависит от iOS.")
    else:
        notes.append("Данные приложений пропущены (--no-apps).")
    return Report(entries, collector.aborted, SECTIONS, tuple(notes))


# public alias for other modules (diagnostics)
afc_info = _afc
