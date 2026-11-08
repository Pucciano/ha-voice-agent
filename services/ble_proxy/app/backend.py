"""What the API side needs from Bluetooth, independent of BlueZ.

Addresses are 48-bit integers with the first octet of the printed form most
significant, as in the ESPHome API. Address types follow ESP-IDF: 0 public,
1 random. Error codes are the ESP-IDF ones that aioesphomeapi turns into
messages, so Home Assistant reports them as it does for an ESP32 proxy.
"""

from __future__ import annotations

import dataclasses
import enum
from typing import Callable, Protocol

ADDRESS_PUBLIC = 0
ADDRESS_RANDOM = 1

# esp_gatt_status_t, plus -1, ESPHome's own "not connected".
GATT_NOT_CONNECTED = -1
GATT_INVALID_HANDLE = 0x01
GATT_READ_NOT_PERMIT = 0x02
GATT_WRITE_NOT_PERMIT = 0x03
GATT_INSUF_AUTHENTICATION = 0x05
GATT_REQ_NOT_SUPPORTED = 0x06
GATT_INVALID_OFFSET = 0x07
GATT_INSUF_AUTHORIZATION = 0x08
GATT_INVALID_ATTR_LEN = 0x0D
GATT_INSUF_ENCRYPTION = 0x0F
GATT_NO_RESOURCES = 0x80
GATT_BUSY = 0x84
GATT_ERROR = 0x85

# esp_gatt_conn_reason_t
CONN_TIMEOUT = 0x08
CONN_TERMINATE_PEER_USER = 0x13
CONN_TERMINATE_LOCAL_HOST = 0x16
CONN_FAIL_ESTABLISH = 0x3E

CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"


class ScannerState(enum.IntEnum):
    """BluetoothScannerState of the API."""

    IDLE = 0
    STARTING = 1
    RUNNING = 2
    FAILED = 3
    STOPPING = 4
    STOPPED = 5


class ScannerMode(enum.IntEnum):
    """BluetoothScannerMode of the API."""

    PASSIVE = 0
    ACTIVE = 1


@dataclasses.dataclass(frozen=True, slots=True)
class RawAdvertisement:
    address: int
    address_type: int
    rssi: int
    # AD structures: the advertisement and, when scanned, its scan response.
    data: bytes


@dataclasses.dataclass(frozen=True, slots=True)
class GattDescriptor:
    uuid: str
    handle: int


@dataclasses.dataclass(frozen=True, slots=True)
class GattCharacteristic:
    uuid: str
    handle: int
    # Bit field of the characteristic properties (0x02 read, 0x10 notify ...).
    properties: int
    descriptors: tuple[GattDescriptor, ...] = ()


@dataclasses.dataclass(frozen=True, slots=True)
class GattService:
    uuid: str
    handle: int
    characteristics: tuple[GattCharacteristic, ...] = ()


class BleError(Exception):
    """A failed Bluetooth operation and the code to report for it."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class Backend(Protocol):
    """A Bluetooth adapter: scanning, connections and GATT."""

    @property
    def mac_address(self) -> str:
        """Adapter address, upper case with colons."""

    @property
    def passive_scan_supported(self) -> bool: ...

    def set_callbacks(
        self,
        on_advertisement: Callable[[RawAdvertisement], None],
        on_scanner_state: Callable[[ScannerState, ScannerMode], None],
        on_disconnected: Callable[[int, int], None],
        on_notification: Callable[[int, int, bytes], None],
    ) -> None:
        """Wires the backend to the proxy; called once before start."""

    async def set_scanner_mode(self, mode: ScannerMode) -> None: ...

    async def connect(self, address: int, address_type: int) -> int:
        """Connects and resolves the services; returns the ATT MTU."""

    async def disconnect(self, address: int) -> None: ...

    def services(self, address: int) -> list[GattService]: ...

    async def read(self, address: int, handle: int) -> bytes: ...

    async def write(
        self, address: int, handle: int, data: bytes, response: bool
    ) -> None: ...

    async def read_descriptor(self, address: int, handle: int) -> bytes: ...

    async def write_descriptor(
        self, address: int, handle: int, data: bytes
    ) -> None: ...

    async def start_notify(self, address: int, handle: int) -> None: ...

    async def stop_notify(self, address: int, handle: int) -> None: ...

    async def clear_cache(self, address: int) -> None: ...


def format_address(address: int) -> str:
    """48-bit address as AA:BB:CC:DD:EE:FF."""

    text = f"{address:012X}"
    return ":".join(text[i : i + 2] for i in range(0, 12, 2))


def parse_address(text: str) -> int:
    """AA:BB:CC:DD:EE:FF as a 48-bit address."""

    digits = text.replace(":", "")
    if len(digits) != 12:
        raise ValueError(f"not a Bluetooth address: {text}")
    return int(digits, 16)
