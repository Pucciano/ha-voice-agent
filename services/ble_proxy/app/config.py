"""Settings from the environment (the compose file's .env).

BLE_PROXY_API_KEY          base64 key of 32 bytes, as `api: encryption:
                           key` of an ESPHome device; required
BLE_PROXY_NAME             device name and mDNS host name (aivi-ble-proxy)
BLE_PROXY_FRIENDLY_NAME    name Home Assistant shows (Bluetooth Proxy)
BLE_PROXY_AREA             suggested area, optional
BLE_PROXY_ADAPTER          hciN (hci0)
BLE_PROXY_PORT             API port (6053)
BLE_PROXY_MAX_CONNECTIONS  Bluetooth connection slots (3)
BLE_PROXY_SCAN_MODE        active or passive (active); Home Assistant may
                           switch the mode later
BLE_PROXY_ADDRESS          address announced by mDNS (that of the default
                           route)
BLE_PROXY_LOG_LEVEL        DEBUG, INFO ... (INFO)
"""

from __future__ import annotations

import binascii
import dataclasses
import re
from typing import Mapping

from .backend import ScannerMode

NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,29}[a-z0-9])?")
ADAPTER = re.compile(r"hci[0-9]+")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class ConfigError(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class Settings:  # pylint: disable=too-many-instance-attributes
    api_key: bytes = dataclasses.field(repr=False)
    name: str = "aivi-ble-proxy"
    friendly_name: str = "Bluetooth Proxy"
    area: str = ""
    adapter: str = "hci0"
    port: int = 6053
    max_connections: int = 3
    scan_mode: ScannerMode = ScannerMode.ACTIVE
    address: str | None = None
    log_level: str = "INFO"


def decode_key(text: str) -> bytes:
    try:
        key = binascii.a2b_base64(text.strip())
    except binascii.Error as err:
        raise ConfigError("BLE_PROXY_API_KEY is not base64") from err
    if len(key) != 32:
        raise ConfigError("BLE_PROXY_API_KEY must decode to 32 bytes")
    return key


def _int(env: Mapping[str, str], name: str, default: int, low: int, high: int):
    text = env.get(name, "").strip()
    if not text:
        return default
    try:
        value = int(text)
    except ValueError as err:
        raise ConfigError(f"{name} is not a number") from err
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}")
    return value


def from_env(env: Mapping[str, str]) -> Settings:
    key_text = env.get("BLE_PROXY_API_KEY", "")
    if not key_text.strip():
        raise ConfigError("BLE_PROXY_API_KEY is missing")
    name = env.get("BLE_PROXY_NAME", "").strip() or Settings.name
    if not NAME.fullmatch(name):
        raise ConfigError(
            "BLE_PROXY_NAME: 1 to 31 lower case letters, digits and hyphens"
        )
    adapter = env.get("BLE_PROXY_ADAPTER", "").strip() or Settings.adapter
    if not ADAPTER.fullmatch(adapter):
        raise ConfigError("BLE_PROXY_ADAPTER must look like hci0")
    log_level = env.get("BLE_PROXY_LOG_LEVEL", "").strip().upper() or "INFO"
    if log_level not in LOG_LEVELS:
        raise ConfigError("BLE_PROXY_LOG_LEVEL: " + ", ".join(LOG_LEVELS))
    mode = env.get("BLE_PROXY_SCAN_MODE", "").strip().lower() or "active"
    if mode not in ("active", "passive"):
        raise ConfigError("BLE_PROXY_SCAN_MODE must be active or passive")
    return Settings(
        api_key=decode_key(key_text),
        name=name,
        friendly_name=env.get("BLE_PROXY_FRIENDLY_NAME", "").strip()
        or Settings.friendly_name,
        area=env.get("BLE_PROXY_AREA", "").strip(),
        adapter=adapter,
        port=_int(env, "BLE_PROXY_PORT", Settings.port, 1, 65535),
        max_connections=_int(
            env, "BLE_PROXY_MAX_CONNECTIONS", Settings.max_connections, 1, 9
        ),
        scan_mode=ScannerMode[mode.upper()],
        address=env.get("BLE_PROXY_ADDRESS", "").strip() or None,
        log_level=log_level,
    )
