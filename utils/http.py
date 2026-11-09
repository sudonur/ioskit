"""Tiny HTTP JSON client with a hard timeout and typed failures.

Network failure is an ERROR (communication failure) with a code that is NOT
one of the device-fatal codes, so a dead internet never aborts device reads:
  http_404 -> UNAVAILABLE (the source does not know the resource)
  network_timeout / network_unreachable / http_<n> / parse_error -> ERROR
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any, Callable

from ioskit.core.result import Result
from ioskit.utils.trace import trace

DEFAULT_TIMEOUT = 8.0
MAX_BYTES = 5_000_000
USER_AGENT = "iOSKit/0.1 (diagnostic tool)"
HINT = "Проверьте подключение к интернету или увеличьте --online-timeout; без сети используйте --offline."

Opener = Callable[[str, float], bytes]


def _urlopen(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https URL built by caller
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("ответ слишком большой")
    return data


def fetch_json(url: str, *, timeout: float = DEFAULT_TIMEOUT, opener: Opener | None = None,
               source: str | None = None) -> Result[Any]:
    src = source or url
    trace(f"GET {url} (тайм-аут {timeout:g} с)", source="http")
    try:
        raw = (opener or _urlopen)(url, timeout)
    except urllib.error.HTTPError as exc:  # before URLError: it is a subclass
        if exc.code == 404:
            return Result.unavailable("источник не знает этот ресурс (HTTP 404)", source=src, code="http_404")
        return Result.fail(f"HTTP {exc.code}: {exc.reason}", source=src, hint=HINT, code=f"http_{exc.code}")
    except (socket.timeout, TimeoutError):
        return Result.fail(f"тайм-аут {timeout:g} с", source=src, hint=HINT, code="network_timeout")
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            return Result.fail(f"тайм-аут {timeout:g} с", source=src, hint=HINT, code="network_timeout")
        return Result.fail(f"сеть недоступна: {exc.reason}", source=src, hint=HINT, code="network_unreachable")
    except (OSError, ValueError) as exc:
        return Result.fail(f"ошибка запроса: {exc}", source=src, hint=HINT, code="network_unreachable")
    try:
        return Result.ok(json.loads(raw.decode("utf-8")), src)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return Result.fail(f"ответ не является JSON: {exc}", source=src, code="parse_error")
