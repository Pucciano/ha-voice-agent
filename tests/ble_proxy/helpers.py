"""A proxy on a free port with a fake Bluetooth adapter, and the real
aioesphomeapi client that Home Assistant uses."""

import asyncio
import base64
import contextlib
import pathlib
import sys
from typing import AsyncIterator, Awaitable, Callable

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "services" / "ble_proxy"))

# pylint: disable=wrong-import-position
from aioesphomeapi import APIClient  # noqa: E402
from app.api import ApiServer, DeviceIdentity  # noqa: E402
from app.backend import (  # noqa: E402
    CONN_FAIL_ESTABLISH,
    CONN_TERMINATE_PEER_USER,
    GATT_INVALID_HANDLE,
    GATT_REQ_NOT_SUPPORTED,
    BleError,
    GattCharacteristic,
    GattDescriptor,
    GattService,
    RawAdvertisement,
    ScannerMode,
    ScannerState,
)
from app.proxy import BluetoothProxy  # noqa: E402

KEY = bytes(range(32))
KEY_TEXT = base64.b64encode(KEY).decode()
OTHER_KEY_TEXT = base64.b64encode(bytes(32)).decode()
ADAPTER_MAC = "8C:88:4B:45:5F:60"
MOUNT = 0xD322E6103DA6
MOUNT_TYPE = 1
VALUE_HANDLE = 0x0010
NOTIFY_HANDLE = 0x0012
CCCD_HANDLE = 0x0014
MOUNT_SERVICES = [
    GattService(
        uuid="030013ac-4202-2d8e-ea11-3959c46ca10e",
        handle=0x000E,
        characteristics=(
            GattCharacteristic(
                uuid="c005fa2a-0651-4800-b000-000000000000",
                handle=VALUE_HANDLE,
                properties=0x02 | 0x08,
            ),
            GattCharacteristic(
                uuid="c005fa00-0651-4800-b000-000000000000",
                handle=NOTIFY_HANDLE,
                properties=0x02 | 0x04 | 0x10,
                descriptors=(
                    GattDescriptor(
                        uuid="00002902-0000-1000-8000-00805f9b34fb",
                        handle=CCCD_HANDLE,
                    ),
                ),
            ),
        ),
    )
]


class FakeBackend:
    """An adapter that reaches the devices in `peripherals`."""

    def __init__(self) -> None:
        self.mac_address = ADAPTER_MAC
        self.passive_scan_supported = False
        self.peripherals: dict[int, list[GattService]] = {MOUNT: MOUNT_SERVICES}
        self.connected: set[int] = set()
        self.values: dict[tuple[int, int], bytes] = {
            (MOUNT, VALUE_HANDLE): b"\x01",
            (MOUNT, CCCD_HANDLE): b"\x00\x00",
        }
        self.writes: list[tuple[str, int, int, bytes, bool]] = []
        self.notifying: set[tuple[int, int]] = set()
        self.modes: list[ScannerMode] = []
        self.connect_gate: asyncio.Event | None = None
        self.on_advertisement: Callable[[RawAdvertisement], None]
        self.on_scanner_state: Callable[[ScannerState, ScannerMode], None]
        self.on_disconnected: Callable[[int, int], None]
        self.on_notification: Callable[[int, int, bytes], None]

    def set_callbacks(
        self,
        on_advertisement,
        on_scanner_state,
        on_disconnected,
        on_notification,
    ) -> None:
        self.on_advertisement = on_advertisement
        self.on_scanner_state = on_scanner_state
        self.on_disconnected = on_disconnected
        self.on_notification = on_notification

    async def set_scanner_mode(self, mode: ScannerMode) -> None:
        self.modes.append(mode)
        actual = mode if self.passive_scan_supported else ScannerMode.ACTIVE
        self.on_scanner_state(ScannerState.RUNNING, actual)

    async def connect(self, address: int, address_type: int) -> int:
        if self.connect_gate is not None:
            await self.connect_gate.wait()
        if address not in self.peripherals:
            raise BleError(CONN_FAIL_ESTABLISH, "device not found")
        self.connected.add(address)
        return 247

    async def disconnect(self, address: int) -> None:
        self.connected.discard(address)
        self.notifying = {n for n in self.notifying if n[0] != address}

    def services(self, address: int) -> list[GattService]:
        return self.peripherals[address]

    def _value(self, address: int, handle: int) -> bytes:
        try:
            return self.values[(address, handle)]
        except KeyError:
            raise BleError(GATT_INVALID_HANDLE, "no such handle") from None

    async def read(self, address: int, handle: int) -> bytes:
        return self._value(address, handle)

    async def write(self, address, handle, data, response) -> None:
        self._value(address, handle)
        self.writes.append(("char", address, handle, data, response))
        self.values[(address, handle)] = data

    async def read_descriptor(self, address: int, handle: int) -> bytes:
        return self._value(address, handle)

    async def write_descriptor(self, address, handle, data) -> None:
        self._value(address, handle)
        self.writes.append(("descriptor", address, handle, data, True))

    async def start_notify(self, address: int, handle: int) -> None:
        if handle != NOTIFY_HANDLE:
            raise BleError(GATT_REQ_NOT_SUPPORTED, "does not notify")
        self.notifying.add((address, handle))

    async def stop_notify(self, address: int, handle: int) -> None:
        self.notifying.discard((address, handle))

    async def clear_cache(self, address: int) -> None:
        pass

    # Test controls

    def advertise(self, adv: RawAdvertisement) -> None:
        self.on_advertisement(adv)

    def drop(self, address: int) -> None:
        self.connected.discard(address)
        self.on_disconnected(address, CONN_TERMINATE_PEER_USER)

    def notify(self, address: int, handle: int, data: bytes) -> None:
        self.on_notification(address, handle, data)


@contextlib.asynccontextmanager
async def running(
    backend: FakeBackend | None = None, max_connections: int = 3
) -> AsyncIterator[tuple[int, FakeBackend, BluetoothProxy]]:
    backend = backend or FakeBackend()
    proxy = BluetoothProxy(backend, max_connections)  # type: ignore[arg-type]
    identity = DeviceIdentity(
        name="aivi-ble-test",
        friendly_name="Bluetooth Test",
        mac_address=ADAPTER_MAC,
        suggested_area="Wohnzimmer",
    )
    server = ApiServer(identity, KEY, proxy)
    port = await server.start("127.0.0.1", 0)
    await backend.set_scanner_mode(ScannerMode.ACTIVE)
    try:
        yield port, backend, proxy
    finally:
        await server.close()
        await proxy.close()


@contextlib.asynccontextmanager
async def client(
    port: int, key: str | None = KEY_TEXT
) -> AsyncIterator[APIClient]:
    api = APIClient("127.0.0.1", port, None, noise_psk=key, client_info="test")
    await api.connect(login=True)
    try:
        yield api
    finally:
        await api.disconnect()


async def until(check: Callable[[], bool], timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)


def run(coro: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(coro())
