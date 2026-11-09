"""Device information: identity, software, identifiers, radio, state (lockdownd)."""
from __future__ import annotations

from ioskit.core.collector import Collector, Field, ProgressCb
from ioskit.core.device import Device
from ioskit.modules.common import NO_CELLULAR, Report, lockdown_field as L, table_reader  # noqa: F401
from ioskit.utils.formatting import format_timestamp
from ioskit.utils.models import commercial_name
from ioskit.utils.trace import trace

S_DEVICE = "Устройство"
S_SOFT = "Программное обеспечение"
S_IDS = "Идентификаторы"
S_RADIO = "Сеть и радио"
S_STATE = "Состояние"
SECTIONS = (S_DEVICE, S_SOFT, S_IDS, S_RADIO, S_STATE)
TITLE = "Устройство"
HEADER = (("Модель", "model"), ("Имя", "device_name"), ("iOS", "ios_version"), ("Сборка", "build"))


def _build_fields() -> list[Field]:
    return [
        L("device_name", "Имя устройства", S_DEVICE, "DeviceName"),
        Field("model", "Модель", S_DEVICE, table_reader(commercial_name, "название модели")),
        L("product_type", "ProductType", S_DEVICE, "ProductType"),
        L("hardware_model", "Hardware model", S_DEVICE, "HardwareModel"),
        L("model_number", "Номер модели", S_DEVICE, "ModelNumber"),
        L("region", "Регион (RegionInfo)", S_DEVICE, "RegionInfo"),
        L("device_class", "Класс устройства", S_DEVICE, "DeviceClass"),

        L("ios_version", "Версия iOS", S_SOFT, "ProductVersion"),
        L("build", "Сборка", S_SOFT, "BuildVersion"),
        L("os_name", "Название ОС", S_SOFT, "ProductName"),

        L("udid", "UDID", S_IDS, "UniqueDeviceID", sensitive=True),
        L("serial", "Серийный номер", S_IDS, "SerialNumber", sensitive=True),
        L("imei", "IMEI", S_IDS, "InternationalMobileEquipmentIdentity", sensitive=True, telephony=True),
        L("imei2", "IMEI 2", S_IDS, "InternationalMobileEquipmentIdentity2", sensitive=True, telephony=True),
        L("meid", "MEID", S_IDS, "MobileEquipmentIdentifier", sensitive=True, telephony=True),
        L("iccid", "ICCID (SIM)", S_IDS, "IntegratedCircuitCardIdentity", sensitive=True, telephony=True),
        L("phone", "Номер телефона", S_IDS, "PhoneNumber", sensitive=True, telephony=True),

        L("wifi_mac", "Wi-Fi MAC", S_RADIO, "WiFiAddress", sensitive=True),
        L("bt_mac", "Bluetooth MAC", S_RADIO, "BluetoothAddress", sensitive=True),
        L("telephony", "Сотовая связь", S_RADIO, "TelephonyCapability"),
        L("baseband", "Версия baseband", S_RADIO, "BasebandVersion", telephony=True),
        L("sim_status", "Статус SIM", S_RADIO, "SIMStatus", telephony=True),

        L("activation", "Состояние активации", S_STATE, "ActivationState"),
        L("passcode", "Код-пароль установлен", S_STATE, "PasswordProtected"),
        L("trusted_host", "Доверенный хост", S_STATE, "TrustedHostAttached"),
        L("timezone", "Часовой пояс", S_STATE, "TimeZone"),
        L("device_time", "Время на устройстве", S_STATE, "TimeIntervalSince1970", fmt=format_timestamp),
    ]


FIELDS: list[Field] = _build_fields()


def collect(device: Device, *, timeout: float = 15.0, on_progress: ProgressCb | None = None) -> Report:
    trace(f"сбор информации: {len(FIELDS)} параметров, тайм-аут {timeout:g} с", source="info")
    collector = Collector(device, timeout=timeout, on_progress=on_progress)
    entries = collector.run(FIELDS)
    return Report(entries, collector.aborted, SECTIONS)
