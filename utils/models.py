"""ProductType -> commercial model name.

A static table: iOS does not report the marketing name through lockdown.
Unknown identifiers are reported honestly as unknown (never guessed).
"""
from __future__ import annotations

from ioskit.core.result import Result

_SOURCE = "таблица iOSKit ← ProductType (справочно, не измерено устройством)"

_N: dict[str, str] = {
    "iPhone1,1": "iPhone 2G", "iPhone1,2": "iPhone 3G", "iPhone2,1": "iPhone 3GS",
    "iPhone3,1": "iPhone 4", "iPhone3,2": "iPhone 4", "iPhone3,3": "iPhone 4",
    "iPhone4,1": "iPhone 4S", "iPhone5,1": "iPhone 5", "iPhone5,2": "iPhone 5",
    "iPhone5,3": "iPhone 5c", "iPhone5,4": "iPhone 5c",
    "iPhone6,1": "iPhone 5s", "iPhone6,2": "iPhone 5s",
    "iPhone7,2": "iPhone 6", "iPhone7,1": "iPhone 6 Plus",
    "iPhone8,1": "iPhone 6s", "iPhone8,2": "iPhone 6s Plus", "iPhone8,4": "iPhone SE (1st gen)",
    "iPhone9,1": "iPhone 7", "iPhone9,3": "iPhone 7",
    "iPhone9,2": "iPhone 7 Plus", "iPhone9,4": "iPhone 7 Plus",
    "iPhone10,1": "iPhone 8", "iPhone10,4": "iPhone 8",
    "iPhone10,2": "iPhone 8 Plus", "iPhone10,5": "iPhone 8 Plus",
    "iPhone10,3": "iPhone X", "iPhone10,6": "iPhone X",
    "iPhone11,8": "iPhone XR", "iPhone11,2": "iPhone XS",
    "iPhone11,4": "iPhone XS Max", "iPhone11,6": "iPhone XS Max",
    "iPhone12,1": "iPhone 11", "iPhone12,3": "iPhone 11 Pro", "iPhone12,5": "iPhone 11 Pro Max",
    "iPhone12,8": "iPhone SE (2nd gen)",
    "iPhone13,1": "iPhone 12 mini", "iPhone13,2": "iPhone 12",
    "iPhone13,3": "iPhone 12 Pro", "iPhone13,4": "iPhone 12 Pro Max",
    "iPhone14,4": "iPhone 13 mini", "iPhone14,5": "iPhone 13",
    "iPhone14,2": "iPhone 13 Pro", "iPhone14,3": "iPhone 13 Pro Max",
    "iPhone14,6": "iPhone SE (3rd gen)",
    "iPhone14,7": "iPhone 14", "iPhone14,8": "iPhone 14 Plus",
    "iPhone15,2": "iPhone 14 Pro", "iPhone15,3": "iPhone 14 Pro Max",
    "iPhone15,4": "iPhone 15", "iPhone15,5": "iPhone 15 Plus",
    "iPhone16,1": "iPhone 15 Pro", "iPhone16,2": "iPhone 15 Pro Max",
    "iPhone17,3": "iPhone 16", "iPhone17,4": "iPhone 16 Plus",
    "iPhone17,1": "iPhone 16 Pro", "iPhone17,2": "iPhone 16 Pro Max",
    "iPhone17,5": "iPhone 16e",
    "iPod5,1": "iPod touch (5th gen)", "iPod7,1": "iPod touch (6th gen)",
    "iPod9,1": "iPod touch (7th gen)",
    "iPad7,5": "iPad (6th gen)", "iPad7,6": "iPad (6th gen)",
    "iPad7,11": "iPad (7th gen)", "iPad7,12": "iPad (7th gen)",
    "iPad11,6": "iPad (8th gen)", "iPad11,7": "iPad (8th gen)",
}


def family(product_type: str) -> str | None:
    for prefix in ("iPhone", "iPad", "iPod", "Watch", "AppleTV"):
        if product_type.startswith(prefix):
            return prefix
    return None


def commercial_name(product_type: str | None) -> Result[str]:
    if not product_type:
        return Result.unavailable("ProductType не получен", source=_SOURCE)
    name = _N.get(product_type)
    if name:
        return Result.ok(name, _SOURCE, formula="таблица ProductType → название модели")
    fam = family(product_type)
    label = f"неизвестная модель {fam}" if fam else "неизвестная модель"
    return Result.unavailable(
        f"{product_type}: {label} (нет в таблице iOSKit)", source=_SOURCE,
        hint="Таблицу можно дополнить в ioskit/utils/models.py")


# ---- reference tables: SoC and RAM -----------------------------------------
# Public Apple specifications keyed by ProductType. NOT read from the device:
# iOS does not expose RAM through lockdown/diagnostics, so these are labelled
# "reference" everywhere they are shown. Unknown models are never guessed.

def _fill(value, *product_types: str) -> dict:
    return {pt: value for pt in product_types}


_CHIP: dict[str, str] = {}
for _v, _pts in (
    ("A4", ("iPhone3,1", "iPhone3,2", "iPhone3,3")),
    ("A5", ("iPhone4,1",)),
    ("A6", ("iPhone5,1", "iPhone5,2", "iPhone5,3", "iPhone5,4")),
    ("A7", ("iPhone6,1", "iPhone6,2")),
    ("A8", ("iPhone7,1", "iPhone7,2")),
    ("A9", ("iPhone8,1", "iPhone8,2", "iPhone8,4")),
    ("A10 Fusion", ("iPhone9,1", "iPhone9,2", "iPhone9,3", "iPhone9,4")),
    ("A11 Bionic", ("iPhone10,1", "iPhone10,2", "iPhone10,3", "iPhone10,4", "iPhone10,5", "iPhone10,6")),
    ("A12 Bionic", ("iPhone11,2", "iPhone11,4", "iPhone11,6", "iPhone11,8")),
    ("A13 Bionic", ("iPhone12,1", "iPhone12,3", "iPhone12,5", "iPhone12,8")),
    ("A14 Bionic", ("iPhone13,1", "iPhone13,2", "iPhone13,3", "iPhone13,4")),
    ("A15 Bionic", ("iPhone14,2", "iPhone14,3", "iPhone14,4", "iPhone14,5", "iPhone14,6",
                    "iPhone14,7", "iPhone14,8")),
    ("A16 Bionic", ("iPhone15,2", "iPhone15,3", "iPhone15,4", "iPhone15,5")),
    ("A17 Pro", ("iPhone16,1", "iPhone16,2")),
    ("A18", ("iPhone17,3", "iPhone17,4", "iPhone17,5")),
    ("A18 Pro", ("iPhone17,1", "iPhone17,2")),
):
    _CHIP.update(_fill(_v, *_pts))

_RAM_MB: dict[str, int] = {}
for _v, _pts in (
    (512, ("iPhone3,1", "iPhone3,2", "iPhone3,3", "iPhone4,1")),
    (1024, ("iPhone5,1", "iPhone5,2", "iPhone5,3", "iPhone5,4", "iPhone6,1", "iPhone6,2",
            "iPhone7,1", "iPhone7,2")),
    (2048, ("iPhone8,1", "iPhone8,2", "iPhone8,4", "iPhone9,1", "iPhone9,3",
            "iPhone10,1", "iPhone10,4")),
    (3072, ("iPhone9,2", "iPhone9,4", "iPhone10,2", "iPhone10,5", "iPhone10,3", "iPhone10,6",
            "iPhone11,8", "iPhone12,8")),
    (4096, ("iPhone11,2", "iPhone11,4", "iPhone11,6", "iPhone12,1", "iPhone12,3", "iPhone12,5",
            "iPhone13,1", "iPhone13,2", "iPhone14,4", "iPhone14,5", "iPhone14,6")),
    (6144, ("iPhone13,3", "iPhone13,4", "iPhone14,2", "iPhone14,3", "iPhone14,7", "iPhone14,8",
            "iPhone15,2", "iPhone15,3", "iPhone15,4", "iPhone15,5")),
    (8192, ("iPhone16,1", "iPhone16,2", "iPhone17,1", "iPhone17,2", "iPhone17,3", "iPhone17,4",
            "iPhone17,5")),
):
    _RAM_MB.update(_fill(_v, *_pts))


def _reference(table: dict, product_type: str | None, what: str, formula: str) -> Result:
    if not product_type:
        return Result.unavailable("ProductType не получен", source=_SOURCE)
    value = table.get(product_type)
    if value is None:
        return Result.unavailable(f"{product_type}: {what} нет в таблице iOSKit (не угадываем)",
                                  source=_SOURCE, hint="Таблицы: ioskit/utils/models.py")
    return Result.ok(value, _SOURCE, formula=formula)


def chip_name(product_type: str | None) -> Result[str]:
    return _reference(_CHIP, product_type, "чип", "таблица ProductType → SoC")


def ram_reference_mb(product_type: str | None) -> Result[int]:
    return _reference(_RAM_MB, product_type, "объём RAM", "таблица ProductType → RAM (справочно)")


# ---- iOS support range (major versions), reference table -------------------
# (first iOS major, last iOS major). last=None: the model still gets current
# iOS releases, so no upper bound is claimed. Only majors are stored on purpose:
# patch versions go stale; check apple.com for the exact final release.
_IOS_RANGE: dict[str, tuple[int, int | None]] = {}
for _v, _pts in (
    ((3, 6), ("iPhone2,1",)),
    ((4, 7), ("iPhone3,1", "iPhone3,2", "iPhone3,3")),
    ((5, 9), ("iPhone4,1",)),
    ((6, 10), ("iPhone5,1", "iPhone5,2")),
    ((7, 10), ("iPhone5,3", "iPhone5,4")),
    ((7, 12), ("iPhone6,1", "iPhone6,2")),
    ((8, 12), ("iPhone7,1", "iPhone7,2")),
    ((9, 15), ("iPhone8,1", "iPhone8,2", "iPhone8,4")),
    ((10, 15), ("iPhone9,1", "iPhone9,2", "iPhone9,3", "iPhone9,4")),
    ((11, 16), ("iPhone10,1", "iPhone10,2", "iPhone10,3", "iPhone10,4", "iPhone10,5", "iPhone10,6")),
    ((12, 18), ("iPhone11,2", "iPhone11,4", "iPhone11,6", "iPhone11,8")),
    ((13, None), ("iPhone12,1", "iPhone12,3", "iPhone12,5", "iPhone12,8")),
    ((14, None), ("iPhone13,1", "iPhone13,2", "iPhone13,3", "iPhone13,4")),
    ((15, None), ("iPhone14,2", "iPhone14,3", "iPhone14,4", "iPhone14,5", "iPhone14,6")),
    ((16, None), ("iPhone14,7", "iPhone14,8", "iPhone15,2", "iPhone15,3")),
    ((17, None), ("iPhone15,4", "iPhone15,5", "iPhone16,1", "iPhone16,2")),
    ((18, None), ("iPhone17,1", "iPhone17,2", "iPhone17,3", "iPhone17,4", "iPhone17,5")),
):
    _IOS_RANGE.update(_fill(_v, *_pts))


def ios_support_range(product_type: str | None) -> Result[tuple[int, int | None]]:
    return _reference(_IOS_RANGE, product_type, "диапазон iOS",
                      "таблица ProductType → (первая, последняя) major-версия iOS")
