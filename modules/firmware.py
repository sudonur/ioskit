"""Firmware: versions, metadata, model compatibility, IPSW signing status.

Sources
  * lockdownd                      - iOS version, build, ProductType, baseband, iBoot, board/chip ids
  * static reference table         - first/last iOS major for the model (derived, reference)
  * ipsw.me API v4 (online)        - firmware list with a `signed` flag per build

Signing status honesty: ipsw.me is a third-party aggregator that checks Apple's
signing servers. iOSKit does NOT talk to Apple's TSS itself (that needs a
BuildManifest and the ECID). A build missing from the list is "unknown", never
"unsigned". Every request has a hard timeout; offline mode is available.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote

from ioskit.core.collector import Collector, Entry, Field, ProgressCb
from ioskit.core.device import Device
from ioskit.core.result import Result, Status
from ioskit.modules.common import (Report, check_field, collect_warnings, finding, lk,
                                   lockdown_field as L, table_reader)
from ioskit.utils.formatting import format_bytes, format_hex, format_value
from ioskit.utils.http import DEFAULT_TIMEOUT, Opener, fetch_json
from ioskit.utils.models import ios_support_range
from ioskit.utils.trace import trace

S_VER = "Версия и сборка (lockdown)"
S_META = "Метаданные прошивки (lockdown)"
S_COMPAT = "Совместимость (справочная таблица)"
S_SIGN = "Статус подписи прошивок (ipsw.me)"
S_CONS = "Согласованность метаданных"
SECTIONS = (S_VER, S_META, S_COMPAT, S_SIGN, S_CONS)
TITLE = "Прошивка"
HEADER = (("iOS", "ios_version"), ("Сборка", "build"), ("Установленная прошивка подписана", "signed_installed"),
          ("ProductType", "product_type"))

API_BASE = "https://api.ipsw.me/v4/device/"
API_NAME = "ipsw.me API v4"

NOTES = (
    "Статус подписи — по данным ipsw.me (сторонний агрегатор, проверяющий серверы подписи Apple). "
    "iOSKit не обращается к TSS Apple напрямую: для этого нужны BuildManifest и ECID. Статус может измениться в любой момент.",
    "Сборка, которой нет в списке ipsw.me (бета, снятая прошивка), получает статус «неизвестно», а не «не подписана».",
    "Таблица совместимости справочная и хранит только major-версии iOS; точную финальную версию "
    "проверяйте на apple.com.",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class OnlineConfig:
    online: bool = True
    timeout: float = DEFAULT_TIMEOUT
    opener: Opener | None = None
    now: Callable[[], datetime] = _utcnow


def _ver_key(version: Any) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", str(version)))


def _to_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("bool вместо числа")
    return value if isinstance(value, int) else int(str(value), 0)


# ---- online source -----------------------------------------------------------

def _ipsw(ctx: Collector, cfg: OnlineConfig) -> Result[Any]:
    """Validated ipsw.me device document (cached; never aborts device reads)."""
    if not cfg.online:
        return Result.unavailable("онлайн-проверка отключена (--offline)", source=API_NAME)
    pt = ctx.lookup("ProductType")
    if pt.status is Status.ERROR:
        return Result.fail(pt.reason or "ошибка чтения ProductType", source=API_NAME, hint=pt.hint, code=pt.code)
    if not pt.is_ok:
        return Result.unavailable("ProductType не получен — запрос не выполнен", source=API_NAME)
    src = f"{API_NAME}: /device/{pt.value}"
    url = f"{API_BASE}{quote(str(pt.value), safe=',')}?type=ipsw"
    res = ctx.external("ipsw", lambda: fetch_json(url, timeout=cfg.timeout, opener=cfg.opener, source=src),
                       source=src, timeout=cfg.timeout + 5)
    if not res.is_ok:
        return res
    doc = res.value
    if not isinstance(doc, dict) or not isinstance(doc.get("firmwares"), list):
        return Result.fail("неожиданный формат ответа ipsw.me", source=src, code="parse_error")
    return res


def _doc_field(cfg: OnlineConfig, key: str, doc_key: str, label: str, fmt=format_value) -> Field:
    def reader(ctx: Collector) -> Result[Any]:
        doc = _ipsw(ctx, cfg)
        if not doc.is_ok:
            return doc
        v = doc.value.get(doc_key)
        if v in (None, ""):
            return Result.unavailable("источник не вернул это поле", source=f"{doc.source}/{doc_key}")
        return Result.ok(v, f"{doc.source}/{doc_key}")
    return Field(key, label, S_SIGN, reader, fmt=fmt)


def _installed(ctx: Collector, cfg: OnlineConfig) -> Result[Any]:
    doc, build = _ipsw(ctx, cfg), ctx.lookup("BuildVersion")
    if not doc.is_ok:
        return doc
    if not build.is_ok:
        return build
    for fw in doc.value["firmwares"]:
        if isinstance(fw, dict) and str(fw.get("buildid", "")).lower() == str(build.value).lower():
            return Result.ok(fw, f"{doc.source} (buildid {build.value})")
    return Result.unavailable(f"сборка {build.value} не найдена в списке ipsw.me "
                              "(бета, снятая прошивка или нестандартная сборка); статус подписи неизвестен",
                              source=doc.source, code="not_listed")


def _installed_field(cfg: OnlineConfig, key: str, fw_key: str, label: str, fmt=format_value) -> Field:
    def reader(ctx: Collector) -> Result[Any]:
        inst = _installed(ctx, cfg)
        if not inst.is_ok:
            return inst
        v = inst.value.get(fw_key)
        if v in (None, ""):
            return Result.unavailable("источник не вернул это поле", source=f"{inst.source}/{fw_key}")
        return Result.ok(v, f"{inst.source}/{fw_key}")
    return Field(key, label, S_SIGN, reader, fmt=fmt)


def _listed(cfg: OnlineConfig) -> Callable[[Collector], Result[Any]]:
    def reader(ctx: Collector) -> Result[Any]:
        inst = _installed(ctx, cfg)
        if inst.code == "not_listed":
            return Result.ok(False, inst.source)
        return inst if not inst.is_ok else Result.ok(True, inst.source)
    return reader


def _signed_list(cfg: OnlineConfig) -> Callable[[Collector], Result[Any]]:
    def reader(ctx: Collector) -> Result[Any]:
        doc = _ipsw(ctx, cfg)
        if not doc.is_ok:
            return doc
        signed = sorted((f for f in doc.value["firmwares"] if isinstance(f, dict) and f.get("signed") is True),
                        key=lambda f: _ver_key(f.get("version")), reverse=True)
        text = ", ".join(f"{f.get('version')} ({f.get('buildid')})" for f in signed) or "нет"
        return Result.ok(text, f"{doc.source}/firmwares[signed=true]")
    return reader


def _latest(cfg: OnlineConfig) -> Callable[[Collector], Result[Any]]:
    def reader(ctx: Collector) -> Result[Any]:
        doc = _ipsw(ctx, cfg)
        if not doc.is_ok:
            return doc
        fws = [f for f in doc.value["firmwares"] if isinstance(f, dict) and f.get("version")]
        if not fws:
            return Result.unavailable("в списке ipsw.me нет прошивок для этого устройства", source=doc.source)
        top = max(fws, key=lambda f: _ver_key(f["version"]))
        flag = {True: "подписана", False: "не подписана"}.get(top.get("signed"), "статус неизвестен")
        return Result.ok(f"{top['version']} ({top.get('buildid')}), {flag}", f"{doc.source}/firmwares[max version]")
    return reader


def _fetched_at(cfg: OnlineConfig) -> Callable[[Collector], Result[Any]]:
    def reader(ctx: Collector) -> Result[Any]:
        doc = _ipsw(ctx, cfg)
        if not doc.is_ok:
            return doc
        return Result.ok(cfg.now().strftime("%Y-%m-%d %H:%M:%S UTC"), "время запроса, iOSKit")
    return reader


def _doc_reader(cfg: OnlineConfig, key: str) -> Callable[[Collector], Result[Any]]:
    def reader(ctx: Collector) -> Result[Any]:
        doc = _ipsw(ctx, cfg)
        if not doc.is_ok:
            return doc
        v = doc.value.get(key)
        return Result.ok(v, f"{doc.source}/{key}") if v not in (None, "") else \
            Result.unavailable("источник не вернул это поле", source=f"{doc.source}/{key}")
    return reader


# ---- compatibility ----------------------------------------------------------

def _range_reader(index: int, none_reason: str) -> Callable[[Collector], Result[Any]]:
    inner = table_reader(ios_support_range, "диапазон iOS")

    def reader(ctx: Collector) -> Result[Any]:
        res = inner(ctx)
        if not res.is_ok:
            return res
        v = res.value[index]
        if v is None:
            return Result.unavailable(none_reason, source=res.source)
        return Result.ok(v, res.source, formula=res.formula)
    return reader


def _range_input(ctx: Collector) -> Result[Any]:
    return table_reader(ios_support_range, "диапазон iOS")(ctx)


def _major_check(version: Any, rng: tuple[int, int | None]) -> tuple[str, str]:
    key = _ver_key(version)
    first, last = rng
    if not key:
        raise ValueError(f"не удалось разобрать версию iOS: {version!r}")
    major = key[0]
    span = f"{first}–{last}" if last is not None else f"{first} и новее"
    return finding(major >= first and (last is None or major <= last),
                   f"iOS {major} в известном диапазоне для модели ({span})",
                   f"iOS {major} вне известного диапазона для модели ({span}): проверьте таблицу или устройство")


def _fields(cfg: OnlineConfig) -> list[Field]:
    S = "расчёт iOSKit ← "
    return [
        L("ios_version", "Версия iOS", S_VER, "ProductVersion"),
        L("build", "Номер сборки", S_VER, "BuildVersion"),
        L("product_type", "ProductType", S_VER, "ProductType"),
        L("baseband", "Версия baseband", S_VER, "BasebandVersion", telephony=True),
        L("os_name", "Название ОС", S_VER, "ProductName"),

        L("iboot", "Версия загрузчика (FirmwareVersion)", S_META, "FirmwareVersion"),
        L("hardware_model", "Hardware model (board config)", S_META, "HardwareModel"),
        L("board_id", "BoardId", S_META, "BoardId"),
        L("chip_id", "ChipID", S_META, "ChipID", fmt=lambda v: format_hex(v, width=4)),
        L("ecid", "ECID (UniqueChipID)", S_META, "UniqueChipID", sensitive=True, fmt=lambda v: format_hex(v, show_dec=False)),
        L("cpu_arch", "Архитектура CPU", S_META, "CPUArchitecture"),

        Field("first_ios", "Первая iOS для модели (major)", S_COMPAT, _range_reader(0, "нет данных"),
              fmt=lambda v: f"iOS {v}"),
        Field("last_ios", "Последняя iOS для модели (major)", S_COMPAT,
              _range_reader(1, "модель получает актуальные версии iOS: верхняя граница в таблице не задана"),
              fmt=lambda v: f"iOS {v}"),

        _doc_field(cfg, "online_name", "name", "Название устройства в ipsw.me"),
        _doc_field(cfg, "online_board", "boardconfig", "Board config (ipsw.me)"),
        Field("signed_installed", "Установленная прошивка подписана", S_SIGN,
              lambda ctx: _flag(_installed(ctx, cfg))),
        Field("listed", "Установленная сборка есть в списке ipsw.me", S_SIGN, _listed(cfg)),
        _installed_field(cfg, "fw_release", "releasedate", "Дата выпуска установленной сборки"),
        _installed_field(cfg, "fw_size", "filesize", "Размер IPSW установленной сборки", fmt=format_bytes),
        _installed_field(cfg, "fw_sha1", "sha1sum", "SHA-1 установленной сборки"),
        _installed_field(cfg, "fw_url", "url", "URL IPSW установленной сборки"),
        Field("signed_list", "Подписанные сейчас прошивки", S_SIGN, _signed_list(cfg)),
        Field("latest", "Новейшая прошивка в источнике", S_SIGN, _latest(cfg)),
        Field("fetched_at", "Данные получены", S_SIGN, _fetched_at(cfg)),

        check_field("chk_range", "Установленная iOS в диапазоне модели", S_CONS,
                    [lk("ProductVersion"), _range_input], _major_check,
                    source=S + "lockdownd:ProductVersion, таблица ProductType → iOS",
                    formula=lambda v, r: f"major({v}) в диапазоне {r[0]}–{r[1] if r[1] is not None else '…'}"),
        check_field("chk_board", "HardwareModel ↔ board config ipsw.me", S_CONS,
                    [lk("HardwareModel"), _doc_reader(cfg, "boardconfig")],
                    lambda a, b: finding(str(a).lower() == str(b).lower(), f"{a} совпадает с {b}",
                                         f"устройство сообщает {a}, ipsw.me ожидает {b}"),
                    source=S + "lockdownd:HardwareModel, ipsw.me boardconfig",
                    formula=lambda a, b: f"{str(a).lower()} == {str(b).lower()}"),
        check_field("chk_cpid", "ChipID ↔ CPID ipsw.me", S_CONS,
                    [lk("ChipID"), _doc_reader(cfg, "cpid")],
                    lambda a, b: finding(_to_int(a) == _to_int(b), f"ChipID {_to_int(a)} совпадает с CPID",
                                         f"устройство сообщает ChipID {_to_int(a)}, ipsw.me ожидает {_to_int(b)}"),
                    source=S + "lockdownd:ChipID, ipsw.me cpid",
                    formula=lambda a, b: f"{_to_int(a)} == {_to_int(b)}"),
        check_field("chk_bdid", "BoardId ↔ BDID ipsw.me", S_CONS,
                    [lk("BoardId"), _doc_reader(cfg, "bdid")],
                    lambda a, b: finding(_to_int(a) == _to_int(b), f"BoardId {_to_int(a)} совпадает с BDID",
                                         f"устройство сообщает BoardId {_to_int(a)}, ipsw.me ожидает {_to_int(b)}"),
                    source=S + "lockdownd:BoardId, ipsw.me bdid",
                    formula=lambda a, b: f"{_to_int(a)} == {_to_int(b)}"),
    ]


def _flag(inst: Result[Any]) -> Result[Any]:
    if not inst.is_ok:
        return inst
    signed = inst.value.get("signed")
    if isinstance(signed, bool):
        return Result.ok(signed, f"{inst.source}/signed")
    return Result.unavailable("источник не вернул поле signed", source=f"{inst.source}/signed")


def collect(device: Device, *, timeout: float = 15.0, online: bool = True,
            online_timeout: float = DEFAULT_TIMEOUT, opener: Opener | None = None,
            now: Callable[[], datetime] | None = None, on_progress: ProgressCb | None = None) -> Report:
    trace(f"прошивка (онлайн: {'да' if online else 'нет'}, тайм-аут {online_timeout:g} с)", source="firmware")
    cfg = OnlineConfig(online, online_timeout, opener, now or _utcnow)
    collector = Collector(device, timeout=timeout, on_progress=on_progress)
    entries: list[Entry] = collector.run(_fields(cfg))
    return Report(entries, collector.aborted, SECTIONS, NOTES, collect_warnings(entries))
