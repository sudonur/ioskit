"""Classification of connection errors.

pymobiledevice3 exception classes are matched by NAME along the MRO, so
iOSKit does not break when a library version moves or renames a class.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ConnState(str, Enum):
    CONNECTED = "connected"
    NO_USBMUXD = "no_usbmuxd"
    NO_DEVICE = "no_device"
    MULTIPLE_DEVICES = "multiple_devices"
    NOT_PAIRED = "not_paired"
    PAIRING_PENDING = "pairing_pending"
    PAIRING_DENIED = "pairing_denied"
    PASSWORD_REQUIRED = "password_required"
    SERVICE_UNAVAILABLE = "service_unavailable"
    DISCONNECTED = "disconnected"
    TIMEOUT = "timeout"
    MISSING_LIB = "missing_lib"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Diagnosis:
    state: ConnState
    title: str
    hint: str


_TEXT: dict[ConnState, tuple[str, str]] = {
    ConnState.CONNECTED: ("Подключено", ""),
    ConnState.NO_USBMUXD: (
        "Служба usbmuxd недоступна",
        "Установите и запустите: sudo apt install usbmuxd && sudo systemctl start usbmuxd"),
    ConnState.NO_DEVICE: (
        "Устройство не найдено",
        "Подключите iPhone кабелем, разблокируйте экран, проверьте кабель и порт."),
    ConnState.MULTIPLE_DEVICES: (
        "Подключено несколько устройств",
        "Укажите нужное через --udid (список: ioskit devices)."),
    ConnState.NOT_PAIRED: (
        "Устройство не сопряжено с этим компьютером",
        "Разблокируйте iPhone и выполните: ioskit devices --pair, затем нажмите «Доверять»."),
    ConnState.PAIRING_PENDING: (
        "Ожидается подтверждение на устройстве",
        "Нажмите «Доверять» на экране iPhone и введите код-пароль, затем повторите."),
    ConnState.PAIRING_DENIED: (
        "Сопряжение отклонено на устройстве",
        "Переподключите кабель и нажмите «Доверять» в диалоге."),
    ConnState.PASSWORD_REQUIRED: (
        "Устройство заблокировано код-паролем",
        "Разблокируйте iPhone и повторите. iOSKit не обходит код-пароль."),
    ConnState.SERVICE_UNAVAILABLE: (
        "Сервис не предоставляется этим устройством / версией iOS",
        "Это ограничение устройства или iOS, а не ошибка связи."),
    ConnState.DISCONNECTED: (
        "Соединение с устройством потеряно",
        "Проверьте кабель; устройство могло перезагрузиться или заблокироваться."),
    ConnState.TIMEOUT: (
        "Тайм-аут ответа устройства",
        "Разблокируйте экран и повторите."),
    ConnState.MISSING_LIB: (
        "Не установлена библиотека pymobiledevice3",
        "Выполните: pip install -e ."),
    ConnState.UNKNOWN: (
        "Непредвиденная ошибка связи",
        "Запустите с --verbose, чтобы увидеть подробности."),
}

# (state, class-name fragments, message fragments); first match wins.
_RULES: list[tuple[ConnState, tuple[str, ...], tuple[str, ...]]] = [
    (ConnState.PAIRING_DENIED, ("userdeniedpairing",), ("user denied", "usercancel")),
    (ConnState.PAIRING_PENDING, ("pairingdialogresponsepending",), ("dialog",)),
    (ConnState.PASSWORD_REQUIRED, ("passwordrequired",), ("passcode", "password protected")),
    (ConnState.NOT_PAIRED, ("notpaired", "pairingerror"), ("not paired", "invalidhostid")),
    (ConnState.NO_DEVICE, ("nodeviceconnected", "devicenotfound"), ("no device",)),
    (ConnState.SERVICE_UNAVAILABLE, ("invalidservice", "startservice"),
     ("invalidservice", "service not")),
    (ConnState.NO_USBMUXD, ("filenotfound", "connectionrefused"), ("usbmuxd",)),
    (ConnState.DISCONNECTED,
     ("brokenpipe", "connectionreset", "connectionabort", "connectionterminated",
      "connectionfailed", "incompleteread"),
     ("connection closed", "broken pipe")),
    (ConnState.TIMEOUT, ("timeout",), ("timed out",)),
]


def diagnosis_for(state: ConnState) -> Diagnosis:
    title, hint = _TEXT[state]
    return Diagnosis(state, title, hint)


def classify(exc: BaseException) -> Diagnosis:
    names = [c.__name__.lower() for c in type(exc).__mro__]
    msg = str(exc).lower()
    for state, name_frags, msg_frags in _RULES:
        if any(f in n for f in name_frags for n in names) or any(f in msg for f in msg_frags):
            return diagnosis_for(state)
    return diagnosis_for(ConnState.UNKNOWN)
