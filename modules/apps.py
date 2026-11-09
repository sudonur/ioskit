"""Installed applications (installation_proxy) with an optional libimobiledevice cross-check.

Only package metadata is available: app contents and data are not accessible and are
not touched. Values the device does not return are shown as "—" (never guessed).
Sizes are requested explicitly (--sizes) because computing them can take a long time;
DynamicDiskUsage is not returned by every iOS version.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable

from ioskit.core.collector import Collector, Entry, ProgressCb
from ioskit.core.device import Device
from ioskit.core.result import Result
from ioskit.modules.common import Report, static_field
from ioskit.utils import libimobile
from ioskit.utils.formatting import format_bytes, format_value
from ioskit.utils.trace import trace

S_SRC = "Источник (installation_proxy)"
S_APP = "Приложение (installation_proxy)"
SECTIONS = (S_SRC, S_APP)
TITLE = "Приложения"
HEADER = (("Приложений", "apps_total"), ("По типам", "by_type"), ("Подходят под фильтр", "matched"),
          ("Размеры", "sizes_coverage"))

TYPES = {"user": "User", "system": "System", "any": "Any"}
SORTS = ("name", "bundle", "size")

NOTES = (
    "Доступны только метаданные пакетов; данные и содержимое приложений через USB не читаются.",
    "Размеры запрашиваются отдельно (--sizes) и могут занять десятки секунд; DynamicDiskUsage есть не на всех версиях iOS.",
    "Скрытые и внутренние приложения видны только при --type any.",
    "Перечень приложений личный: просмотрите вывод перед передачей другим.",
)


@dataclass(slots=True)
class AppRecord:
    bundle_id: str
    source: str
    name: str | None = None
    version: str | None = None
    build: str | None = None
    app_type: str | None = None
    signer: str | None = None
    path: str | None = None
    min_os: str | None = None
    static_size: int | None = None
    dynamic_size: int | None = None

    @property
    def total_size(self) -> int | None:
        if self.static_size is None and self.dynamic_size is None:
            return None
        return (self.static_size or 0) + (self.dynamic_size or 0)


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _str(v: Any) -> str | None:
    return str(v) if v not in (None, "") else None


def to_record(bundle: str, info: dict, source: str) -> AppRecord:
    return AppRecord(bundle, f"{source}/{bundle}",
                     _str(info.get("CFBundleDisplayName") or info.get("CFBundleName")),
                     _str(info.get("CFBundleShortVersionString")), _str(info.get("CFBundleVersion")),
                     _str(info.get("ApplicationType")), _str(info.get("SignerIdentity")), _str(info.get("Path")),
                     _str(info.get("MinimumOSVersion")), _int(info.get("StaticDiskUsage")),
                     _int(info.get("DynamicDiskUsage")))


def sort_records(records: list[AppRecord], key: str) -> list[AppRecord]:
    if key == "size":
        return sorted(records, key=lambda r: (r.total_size is None, -(r.total_size or 0), (r.name or r.bundle_id).casefold()))
    if key == "bundle":
        return sorted(records, key=lambda r: r.bundle_id.casefold())
    return sorted(records, key=lambda r: (r.name or r.bundle_id).casefold())


def matches(rec: AppRecord, needle: str | None) -> bool:
    if not needle:
        return True
    n = needle.casefold()
    return n in (rec.name or "").casefold() or n in rec.bundle_id.casefold()


def _detail_fields(rec: AppRecord, raw: dict) -> list:
    S = rec.source

    def val(v: Any, key: str, fmt: Callable[[Any], str] = format_value):
        res = (Result.ok(v, f"{S}/{key}") if v is not None
               else Result.unavailable("iOS не вернул это значение", source=f"{S}/{key}"))
        return res, fmt

    ent = raw.get("Entitlements") if isinstance(raw.get("Entitlements"), dict) else {}
    rows = [
        ("name", "Название", rec.name, "CFBundleDisplayName", format_value),
        ("bundle_id", "Bundle ID", rec.bundle_id, "CFBundleIdentifier", format_value),
        ("version", "Версия", rec.version, "CFBundleShortVersionString", format_value),
        ("build", "Сборка", rec.build, "CFBundleVersion", format_value),
        ("type", "Тип", rec.app_type, "ApplicationType", format_value),
        ("signer", "Подпись (SignerIdentity)", rec.signer, "SignerIdentity", format_value),
        ("min_os", "Минимальная iOS", rec.min_os, "MinimumOSVersion", format_value),
        ("path", "Путь пакета", rec.path, "Path", format_value),
        ("container", "Контейнер данных", _str(raw.get("Container")), "Container", format_value),
        ("static", "Размер пакета (StaticDiskUsage)", rec.static_size, "StaticDiskUsage", format_bytes),
        ("dynamic", "Данные (DynamicDiskUsage)", rec.dynamic_size, "DynamicDiskUsage", format_bytes),
        ("task_allow", "Entitlement get-task-allow", ent.get("get-task-allow"), "Entitlements/get-task-allow",
         format_value),
    ]
    out = []
    for key, label, v, src_key, fmt in rows:
        res, f = val(v, src_key, fmt)
        out.append(static_field(key, label, S_APP, res, fmt=f))
    return out


def collect(device: Device, *, timeout: float = 15.0, app_type: str = "user", sizes: bool = False,
            search: str | None = None, sort: str = "name", bundle: str | None = None,
            sizes_timeout: float = 60.0, crosscheck: bool = True,
            runner: libimobile.Runner | None = None, on_progress: ProgressCb | None = None) -> Report:
    if app_type not in TYPES:
        raise ValueError(f"неизвестный тип «{app_type}»; допустимо: {', '.join(TYPES)}")
    if sort not in SORTS:
        raise ValueError(f"неизвестная сортировка «{sort}»; допустимо: {', '.join(SORTS)}")
    trace(f"приложения: тип {app_type}, размеры: {'да' if sizes else 'нет'}", source="apps")
    pm_type = "Any" if bundle else TYPES[app_type]       # a bundle id is looked up among all apps
    src = f"installation_proxy:Browse({pm_type})"
    collector = Collector(device, timeout=timeout, on_progress=on_progress)
    res = collector.service("apps", lambda: device.installed_apps(sizes, pm_type), source=src,
                            timeout=sizes_timeout if sizes else timeout)
    if not res.is_ok:
        return Report([Entry(static_field("apps_total", "Приложений", S_SRC, res), res)],
                      collector.aborted, SECTIONS, NOTES)

    raw = {b: i for b, i in res.value.items() if isinstance(i, dict)}
    records = [to_record(b, i, src) for b, i in raw.items()]

    if bundle:
        rec = next((r for r in records if r.bundle_id == bundle), None)
        if rec is None:
            miss = Result.unavailable(f"приложение {bundle} не найдено среди {len(records)} установленных",
                                      source=src)
            return Report([Entry(static_field("apps_total", "Приложение", S_APP, miss), miss)],
                          collector.aborted, SECTIONS, NOTES)
        entries = collector.run(_detail_fields(rec, raw[bundle]))
        return Report(entries, collector.aborted, SECTIONS, NOTES)

    calc = "расчёт iOSKit ← installation_proxy"
    by_type = Counter(r.app_type or "—" for r in records)
    signers = Counter(r.signer or "—" for r in records)
    shown = sort_records([r for r in records if matches(r, search)], sort)

    if sizes:
        n_static = sum(r.static_size is not None for r in records)
        n_dyn = sum(r.dynamic_size is not None for r in records)
        coverage = (Result.ok(f"StaticDiskUsage: {n_static} из {len(records)}, DynamicDiskUsage: {n_dyn} из {len(records)}",
                              calc, formula="число приложений с возвращённым размером")
                    if n_static or n_dyn else
                    Result.unavailable("installation_proxy не вернул размеры (зависит от версии iOS и pymobiledevice3)",
                                       source=src))
        tot_s = sum(r.static_size or 0 for r in records)
        tot_d = sum(r.dynamic_size or 0 for r in records)
        total_static = (Result.ok(tot_s, calc, formula=f"Σ StaticDiskUsage по {n_static} приложениям")
                        if n_static else Result.unavailable("размеры не получены", source=src))
        total_dynamic = (Result.ok(tot_d, calc, formula=f"Σ DynamicDiskUsage по {n_dyn} приложениям")
                         if n_dyn else Result.unavailable("размеры данных не получены", source=src))
    else:
        off = Result.unavailable("размеры не запрашивались (--sizes)", source=src)
        coverage = total_static = total_dynamic = off

    if crosscheck and not bundle and app_type in ("user", "system"):
        total = libimobile.ideviceinstaller_total(device.udid, app_type, **({"runner": runner} if runner else {}))
        if total.is_ok:
            same = total.value == len(records)
            check = Result.ok(f"libimobiledevice: {total.value}; iOSKit: {len(records)} — "
                              f"{'совпадает' if same else 'РАСХОДИТСЯ'}", total.source)
        else:
            check = total
    else:
        check = Result.unavailable("кросс-проверка выполняется для --type user/system", source="ideviceinstaller")

    fields = [
        static_field("apps_total", "Приложений (тип запроса: " + app_type + ")", S_SRC, Result.ok(len(records), src)),
        static_field("by_type", "По типам (ApplicationType)", S_SRC,
                     Result.ok(", ".join(f"{k}: {v}" for k, v in by_type.most_common()) or "нет", calc,
                               formula="число приложений по ApplicationType")),
        static_field("signers", "По подписи (SignerIdentity)", S_SRC,
                     Result.ok(", ".join(f"{k}: {v}" for k, v in signers.most_common(4)) or "нет", calc,
                               formula="число приложений по SignerIdentity (значения без интерпретации)")),
        static_field("matched", "Подходят под фильтр", S_SRC,
                     Result.ok(len(shown), calc, formula=f"поиск «{search}»" if search else "фильтр не задан")),
        static_field("sizes_coverage", "Размеры получены", S_SRC, coverage),
        static_field("total_static", "Σ размер пакетов", S_SRC, total_static, fmt=format_bytes),
        static_field("total_dynamic", "Σ данные приложений", S_SRC, total_dynamic, fmt=format_bytes),
        static_field("crosscheck", "Кросс-проверка (libimobiledevice)", S_SRC, check),
    ]
    entries = collector.run(fields)
    return Report(entries, collector.aborted, SECTIONS, NOTES, extra=(shown, src, sizes))
