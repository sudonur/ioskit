"""Host-side view of the USB link: finds the iPhone USB-tethering interface (ipheth).

This is information about the Linux HOST, not read from the iPhone. The iPhone's
own Wi-Fi/cellular IP addresses are not exposed by any USB service.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ioskit.core.result import Result
from ioskit.utils.trace import trace

SYSFS_NET = Path("/sys/class/net")
SOURCE = "хост: /sys/class/net + ip"
Runner = Callable[[list[str], float], str]


@dataclass(frozen=True, slots=True)
class HostIface:
    name: str
    ipv4: tuple[str, ...]
    gateway: str | None


def _run(cmd: list[str], timeout: float) -> str:
    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if cp.returncode != 0:
        raise OSError(cp.stderr.strip() or f"код возврата {cp.returncode}")
    return cp.stdout


def _driver(sysfs: Path, name: str) -> str | None:
    try:
        return Path(os.readlink(sysfs / name / "device" / "driver")).name
    except OSError:
        return None


def list_ipheth(*, sysfs: Path = SYSFS_NET, runner: Runner = _run, timeout: float = 3.0) -> Result[list[HostIface]]:
    """Interfaces bound to the `ipheth` driver. Empty list is a normal state (OK)."""
    try:
        names = sorted(p.name for p in sysfs.iterdir())
    except OSError as exc:
        return Result.unavailable(f"{sysfs} недоступен: {exc}", source=SOURCE)
    found: list[HostIface] = []
    for name in names:
        if _driver(sysfs, name) != "ipheth":
            continue
        trace(f"интерфейс {name} (ipheth)", source="hostnet")
        try:
            addr = json.loads(runner(["ip", "-j", "-4", "addr", "show", "dev", name], timeout))
            route = json.loads(runner(["ip", "-j", "-4", "route", "show", "dev", name], timeout))
            ips = tuple(ai["local"] for e in addr for ai in e.get("addr_info", []) if ai.get("local"))
            gateway = next((r["gateway"] for r in route if r.get("gateway")), None)
        except FileNotFoundError:
            return Result.unavailable("утилита ip (iproute2) не найдена", source=SOURCE)
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, AttributeError, KeyError) as exc:
            return Result.fail(f"не удалось разобрать вывод ip для {name}: {exc}", source=SOURCE)
        found.append(HostIface(name, ips, gateway))
    return Result.ok(found, SOURCE)
