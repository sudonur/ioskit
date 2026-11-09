"""Pure value formatting helpers (no I/O, no UI dependencies)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def format_value(value: Any) -> str:
    """Human-readable text for a raw lockdown value. bool is checked before int."""
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, (list, tuple)):
        return ", ".join(format_value(v) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}={format_value(v)}" for k, v in value.items())
    return str(value)


def format_timestamp(value: Any) -> str:
    """Unix timestamp -> UTC string. Raises on non-numeric input (caller falls back)."""
    dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def mask_text(text: str, keep: int = 4) -> str:
    """Hide all but the last `keep` characters (used by --mask)."""
    if len(text) <= keep:
        return "*" * len(text)
    return "*" * (len(text) - keep) + text[-keep:]


# ---- unit formatting (display only; raw values stay in Result.value) -------

def format_bytes(n: Any) -> str:
    """Decimal units (1 ГБ = 10^9 Б), as iOS Settings shows them, plus raw bytes."""
    n = int(n)
    if n < 1000:
        return f"{n} Б"
    v = float(n)
    units = ("КБ", "МБ", "ГБ", "ТБ", "ПБ")
    for u in units:
        v /= 1000
        if v < 1000 or u == units[-1]:
            return f"{v:.2f} {u} ({n:,} байт)".replace(",", " ")
    return str(n)  # pragma: no cover


def format_percent(v: Any) -> str:
    return f"{float(v):.1f} %"


def format_mah(v: Any) -> str:
    return f"{v:g} мАч"


def format_ma(v: Any) -> str:
    return f"{v:g} мА"


def format_mv(v: Any) -> str:
    return f"{v / 1000:.3f} В ({v:g} мВ)"


def format_celsius(v: Any) -> str:
    return f"{float(v):.1f} °C"


def format_watts(v: Any) -> str:
    return f"{v:g} Вт"


def format_hex(v: Any, *, width: int = 0, show_dec: bool = True) -> str:
    n = int(v)
    h = f"0x{n:0{width}X}"
    return f"{h} ({n})" if show_dec else h


def format_ram_mb(mb: Any) -> str:
    return f"{mb / 1024:g} ГБ" if mb >= 1024 else f"{mb:g} МБ"


def format_resolution(v: Any) -> str:
    w, h = v
    return f"{w} × {h} px"


def format_scale(v: Any) -> str:
    return f"{v:g}×"


def format_latency(v: dict) -> str:
    return f"{v['median']:.1f} мс (min {v['min']:.1f} / max {v['max']:.1f}, n={v['n']})"
