"""Wrappers over libimobiledevice command-line tools.

Used as a FALLBACK for the log stream and as an independent CROSS-CHECK for the
app count. Nothing here modifies the device: idevicecrashreport is deliberately
NOT used because it deletes reports from the device unless run with -k.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from ioskit.core.result import Result
from ioskit.utils.trace import trace

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
Popen = Callable[..., Any]
Runner = Callable[[list[str], float], str]
MISSING = "не установлен (sudo apt install libimobiledevice-utils)"


@dataclass(frozen=True, slots=True)
class ToolStream:
    lines: Iterator[str]
    close: Callable[[], None]


def syslog_stream(udid: str | None, *, popen: Popen | None = None,
                  which: Callable[[str], str | None] | None = None) -> Result[ToolStream]:
    """Live `idevicesyslog` output. A non-zero exit (not caused by us) raises OSError in the iterator."""
    exe = (which or shutil.which)("idevicesyslog")
    if not exe:
        return Result.unavailable(f"idevicesyslog {MISSING}", source="idevicesyslog")
    cmd = [exe] + (["-u", udid] if udid else [])
    trace(" ".join(cmd), source="idevicesyslog")
    try:
        proc = (popen or subprocess.Popen)(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                     env={**os.environ, "NO_COLOR": "1"})
    except OSError as exc:
        return Result.fail(exc, source="idevicesyslog")
    closed = {"flag": False}
    tail: list[str] = []

    def lines() -> Iterator[str]:
        for raw in proc.stdout:
            line = ANSI.sub("", raw.rstrip("\r\n"))
            tail.append(line)
            del tail[:-3]
            yield line
        rc = proc.wait()
        if rc and not closed["flag"]:
            raise OSError(f"idevicesyslog завершился с кодом {rc}: {' | '.join(t for t in tail if t)}")

    def close() -> None:
        closed["flag"] = True
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001 - best effort, then kill
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    return Result.ok(ToolStream(lines(), close), "idevicesyslog")


def _run(cmd: list[str], timeout: float) -> str:
    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if cp.returncode != 0:
        raise OSError((cp.stderr or cp.stdout).strip() or f"код возврата {cp.returncode}")
    return cp.stdout


def ideviceinstaller_total(udid: str | None, kind: str, *, runner: Runner | None = None,
                           which: Callable[[str], str | None] | None = None,
                           timeout: float = 20.0) -> Result[int]:
    """Number of apps reported by `ideviceinstaller -l -o list_user|list_system`."""
    option = {"user": "list_user", "system": "list_system"}[kind]
    src = f"ideviceinstaller -l -o {option}"
    exe = (which or shutil.which)("ideviceinstaller")
    if not exe:
        return Result.unavailable(f"ideviceinstaller {MISSING.replace('libimobiledevice-utils', 'ideviceinstaller')}",
                                  source=src)
    cmd = [exe] + (["-u", udid] if udid else []) + ["-l", "-o", option]
    try:
        out = (runner or _run)(cmd, timeout)
    except subprocess.TimeoutExpired:
        return Result.fail(f"тайм-аут {timeout:g} с", source=src, code="network_timeout")
    except OSError as exc:
        return Result.fail(exc, source=src)
    m = re.search(r"Total:\s*(\d+)\s+apps", out)
    if not m:
        return Result.fail("формат вывода не распознан (зависит от версии ideviceinstaller)", source=src,
                           code="parse_error", hint="Запустите команду вручную и сравните вывод.")
    return Result.ok(int(m.group(1)), src)
