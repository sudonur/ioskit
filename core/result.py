"""Typed result of any data collection.

The main rule of iOSKit: "no data" is NOT the same as "failure".
Every collector returns a Result, so the UI and reports can tell apart:

  OK           real data, with the service it came from
  UNAVAILABLE  the device / iOS version / SIM does not expose this value
  UNSUPPORTED  a known iOS restriction (min_ios may be set)
  ERROR        communication or parse failure
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Generic, TypeVar

T = TypeVar("T")
U = TypeVar("U")


class Status(str, Enum):
    OK = "ok"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Result(Generic[T]):
    status: Status
    value: T | None = None
    source: str | None = None      # which service/key produced the value
    reason: str | None = None      # human-readable explanation
    hint: str | None = None        # what the user can do
    code: str | None = None        # machine-readable code (e.g. ConnState)
    min_ios: str | None = None     # for UNSUPPORTED
    error_type: str | None = None  # exception class name for ERROR
    derived: bool = False          # computed from other values, not read directly
    formula: str | None = None     # how a derived value was computed (with numbers)

    @classmethod
    def ok(cls, value: T, source: str, *, derived: bool = False,
           formula: str | None = None) -> "Result[T]":
        return cls(Status.OK, value=value, source=source,
                   derived=derived or formula is not None, formula=formula)

    @classmethod
    def unavailable(cls, reason: str, *, source: str | None = None,
                    hint: str | None = None, code: str | None = None) -> "Result[T]":
        return cls(Status.UNAVAILABLE, reason=reason, source=source, hint=hint, code=code)

    @classmethod
    def unsupported(cls, reason: str, *, min_ios: str | None = None,
                    source: str | None = None) -> "Result[T]":
        return cls(Status.UNSUPPORTED, reason=reason, min_ios=min_ios, source=source)

    @classmethod
    def fail(cls, error: BaseException | str, *, source: str | None = None,
             hint: str | None = None, code: str | None = None) -> "Result[T]":
        if isinstance(error, BaseException):
            reason = str(error) or type(error).__name__
            etype = type(error).__name__
        else:
            reason, etype = error, None
        return cls(Status.ERROR, reason=reason, source=source, hint=hint,
                   code=code, error_type=etype)

    @property
    def is_ok(self) -> bool:
        return self.status is Status.OK

    def unwrap_or(self, default: U) -> T | U:
        return self.value if self.is_ok else default  # type: ignore[return-value]


def derive(fn: Callable[..., T], *inputs: Result, source: str,
           formula: "str | Callable[..., str] | None" = None) -> Result[T]:
    """Compute a value from other Results.

    * any input ERROR       -> ERROR (a failure is never downgraded to "no data");
    * any input not OK      -> UNAVAILABLE: nothing is guessed;
    * otherwise             -> OK with derived=True and the formula (a string, or a
      callable receiving the input values so numbers can be shown).
    """
    errors = [r for r in inputs if r.status is Status.ERROR]
    if errors:
        e = errors[0]
        return Result.fail(f"ошибка во входных данных: {e.reason}", source=source,
                           hint=e.hint, code=e.code)
    bad = [r for r in inputs if not r.is_ok]
    if bad:
        why = "; ".join(r.reason or r.status.value for r in bad)
        return Result.unavailable(f"нет исходных данных: {why}", source=source)
    values = [r.value for r in inputs]
    try:
        text = formula(*values) if callable(formula) else (formula or "derived")
        return Result.ok(fn(*values), source, derived=True, formula=text)
    except Exception as exc:  # noqa: BLE001 - converted to a typed result
        return Result.fail(exc, source=source)
