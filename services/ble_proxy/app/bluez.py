"""Bluetooth backend on BlueZ.

bluetoothd, started in the same container, owns the adapter. bleak drives it
over D-Bus: discovery for scanning, connections and GATT. Raw advertisements
come from the kernel's management socket (mgmt.py), not from D-Bus. bluetoothd
runs without plugins, so nothing else powers the adapter; the backend does
before every scanner start.

BlueZ refuses writes to a Client Characteristic Configuration descriptor
(0x2902) and enables notifications with StartNotify instead. Home Assistant
writes that descriptor after every notify request, because an ESP32 needs
it, so such a write becomes StartNotify or StopNotify, or nothing when the
characteristic is already in that state.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from typing import Any, Awaitable, Callable, TypeVar

from bleak import BleakClient, BleakScanner
from bleak.args.bluez import BlueZScannerArgs, OrPattern
from bleak.assigned_numbers import AdvertisementDataType
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.descriptor import BleakGATTDescriptor
from bleak.exc import (
    BleakDeviceNotFoundError,
    BleakError,
    BleakGATTProtocolError,
)
from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

from .backend import (
    CCCD_UUID,
    CONN_FAIL_ESTABLISH,
    CONN_TERMINATE_PEER_USER,
    CONN_TIMEOUT,
    GATT_BUSY,
    GATT_ERROR,
    GATT_INVALID_HANDLE,
    GATT_NOT_CONNECTED,
    BleError,
    GattCharacteristic,
    GattDescriptor,
    GattService,
    RawAdvertisement,
    ScannerMode,
    ScannerState,
    format_address,
)
from .mgmt import MgmtSocket

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")

CONNECT_TIMEOUT = 20.0
GATT_TIMEOUT = 30.0
SCANNER_RETRY = 10.0
# A running scanner that hears nothing for this long is started again.
SCANNER_SILENCE = 120.0

PROPERTY_BITS = {
    "broadcast": 0x01,
    "read": 0x02,
    "write-without-response": 0x04,
    "write": 0x08,
    "notify": 0x10,
    "indicate": 0x20,
    "authenticated-signed-writes": 0x40,
    "extended-properties": 0x80,
    "reliable-write": 0x80,
    "writable-auxiliaries": 0x80,
}

# Advertisements arrive through mgmt, so D-Bus may drop repeats.
ACTIVE_ARGS: BlueZScannerArgs = {
    "filters": {"Transport": "le", "DuplicateData": False}
}
# Passive scanning runs through an advertisement monitor, which needs
# patterns; these match the flags of practically every advertiser. The same
# patterns as Home Assistant's own passive scanner.
PASSIVE_ARGS: BlueZScannerArgs = {
    "or_patterns": [
        OrPattern(0, AdvertisementDataType.FLAGS, b"\x02"),
        OrPattern(0, AdvertisementDataType.FLAGS, b"\x06"),
        OrPattern(0, AdvertisementDataType.FLAGS, b"\x1a"),
    ]
}


def property_bits(properties: list[str]) -> int:
    bits = 0
    for name in properties:
        bits |= PROPERTY_BITS.get(name, 0)
    return bits


def convert_services(client_services: Any) -> list[GattService]:
    """bleak's service collection as the backend-neutral service list."""

    services = []
    for service in sorted(client_services, key=lambda s: s.handle):
        characteristics = tuple(
            GattCharacteristic(
                uuid=char.uuid,
                handle=char.handle,
                properties=property_bits(char.properties),
                descriptors=tuple(
                    GattDescriptor(uuid=desc.uuid, handle=desc.handle)
                    for desc in sorted(char.descriptors, key=lambda d: d.handle)
                ),
            )
            for char in sorted(service.characteristics, key=lambda c: c.handle)
        )
        services.append(
            GattService(
                uuid=service.uuid,
                handle=service.handle,
                characteristics=characteristics,
            )
        )
    return services


async def gatt_call(operation: Awaitable[_T], denied: int = GATT_ERROR) -> _T:
    """Runs a GATT operation and turns its failures into BleError."""

    try:
        async with asyncio.timeout(GATT_TIMEOUT):
            return await operation
    except TimeoutError as err:
        raise BleError(GATT_ERROR, "GATT operation timed out") from err
    except BleakGATTProtocolError as err:
        # ATT error codes and ESP-IDF GATT status codes are the same numbers.
        raise BleError(int(err.args[0]), str(err)) from err
    except BleakError as err:
        text = str(err)
        if "Not connected" in text or "NotConnected" in text:
            raise BleError(GATT_NOT_CONNECTED, text) from err
        if "InProgress" in text:
            raise BleError(GATT_BUSY, text) from err
        raise BleError(denied, text) from err


class Device:
    """One connected peripheral: its client, attribute handles and the
    characteristics that notify."""

    def __init__(self, address: int, client: Any) -> None:
        self.address = address
        self.client = client
        self.closing = False
        self.services: list[GattService] = []
        self._chars: dict[int, BleakGATTCharacteristic] = {}
        self._descs: dict[int, BleakGATTDescriptor] = {}
        self.notifying: set[int] = set()

    def load_services(self) -> int:
        """Indexes the resolved services; returns the ATT MTU."""

        self.services = convert_services(self.client.services)
        mtu = 23
        for service in self.client.services:
            for char in service.characteristics:
                self._chars[char.handle] = char
                mtu = max(mtu, char.max_write_without_response_size + 3)
                for desc in char.descriptors:
                    self._descs[desc.handle] = desc
        return mtu

    def characteristic(self, handle: int) -> BleakGATTCharacteristic:
        try:
            return self._chars[handle]
        except KeyError:
            raise BleError(
                GATT_INVALID_HANDLE, f"no characteristic {handle}"
            ) from None

    def descriptor(self, handle: int) -> BleakGATTDescriptor:
        try:
            return self._descs[handle]
        except KeyError:
            raise BleError(
                GATT_INVALID_HANDLE, f"no descriptor {handle}"
            ) from None


class BlueZBackend:  # pylint: disable=too-many-instance-attributes
    def __init__(
        self,
        adapter: str,
        on_fatal: Callable[[str], None],
        client_factory: Callable[..., Any] = BleakClient,
    ) -> None:
        if not adapter.startswith("hci") or not adapter[3:].isdigit():
            raise ValueError(f"adapter must look like hci0: {adapter}")
        self._adapter = adapter
        self._index = int(adapter[3:])
        self._on_fatal = on_fatal
        self._client_factory = client_factory
        self._mac = ""
        self._mgmt: MgmtSocket | None = None
        self._scanner: BleakScanner | None = None
        self._mode = ScannerMode.ACTIVE
        self._wanted_mode = ScannerMode.ACTIVE
        self._passive_ok = True
        self._scan_lock = asyncio.Lock()
        self._last_advertisement = time.monotonic()
        self._devices: dict[int, Device] = {}
        self._watchdog: asyncio.Task[None] | None = None
        self._bus: MessageBus | None = None
        self._on_advertisement: Callable[[RawAdvertisement], None] = _ignore
        self._on_scanner_state: Callable[[ScannerState, ScannerMode], None] = (
            _ignore
        )
        self._on_disconnected: Callable[[int, int], None] = _ignore
        self._on_notification: Callable[[int, int, bytes], None] = _ignore

    # Lifecycle

    @property
    def mac_address(self) -> str:
        return self._mac

    @property
    def passive_scan_supported(self) -> bool:
        return self._passive_ok

    def set_callbacks(
        self,
        on_advertisement: Callable[[RawAdvertisement], None],
        on_scanner_state: Callable[[ScannerState, ScannerMode], None],
        on_disconnected: Callable[[int, int], None],
        on_notification: Callable[[int, int, bytes], None],
    ) -> None:
        self._on_advertisement = on_advertisement
        self._on_scanner_state = on_scanner_state
        self._on_disconnected = on_disconnected
        self._on_notification = on_notification

    async def open(self) -> str:
        """Opens the management socket; returns the adapter address."""

        if self._mgmt is not None:
            self._mgmt.close()
        self._mgmt = MgmtSocket(
            self._index, self._advertisement, self._index_removed
        )
        self._mgmt.open()
        try:
            info = await self._mgmt.read_controller_info()
        except BaseException:
            self._mgmt.close()
            self._mgmt = None
            raise
        self._mac = info.address
        return self._mac

    async def start(self, mode: ScannerMode) -> None:
        await self.set_scanner_mode(mode)
        self._watchdog = asyncio.get_running_loop().create_task(
            self._watch_scanner()
        )

    async def close(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
        for address in list(self._devices):
            await self.disconnect(address)
        async with self._scan_lock:
            await self._stop_scanner()
        if self._mgmt is not None:
            self._mgmt.close()
        if self._bus is not None:
            self._bus.disconnect()

    def _index_removed(self) -> None:
        self._on_fatal(f"adapter {self._adapter} disappeared")

    def _advertisement(self, adv: RawAdvertisement) -> None:
        self._last_advertisement = time.monotonic()
        self._on_advertisement(adv)

    # Scanning

    async def set_scanner_mode(self, mode: ScannerMode) -> None:
        self._wanted_mode = mode
        async with self._scan_lock:
            await self._apply_mode()

    async def _apply_mode(self) -> None:
        mode = self._wanted_mode
        if mode is ScannerMode.PASSIVE and not self._passive_ok:
            mode = ScannerMode.ACTIVE
        if self._scanner is not None and mode is self._mode:
            self._on_scanner_state(ScannerState.RUNNING, self._mode)
            return
        await self._stop_scanner()
        self._on_scanner_state(ScannerState.STARTING, mode)
        try:
            await self._power_on()
            try:
                await self._start_scanner(mode)
            except BleakError as err:
                if mode is not ScannerMode.PASSIVE:
                    raise
                _LOGGER.warning(
                    "Passive scanning not available (%s); scanning actively",
                    err,
                )
                self._passive_ok = False
                mode = ScannerMode.ACTIVE
                await self._start_scanner(mode)
        except (BleakError, OSError) as err:
            _LOGGER.error("Scanner failed to start: %s", err)
            self._on_scanner_state(ScannerState.FAILED, mode)
            return
        self._mode = mode
        self._last_advertisement = time.monotonic()
        self._on_scanner_state(ScannerState.RUNNING, mode)

    async def _system_bus(self) -> MessageBus:
        if self._bus is None or not self._bus.connected:
            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        return self._bus

    async def _power_on(self) -> None:
        bus = await self._system_bus()
        reply = await bus.call(
            Message(
                destination="org.bluez",
                path=f"/org/bluez/{self._adapter}",
                interface="org.freedesktop.DBus.Properties",
                member="Set",
                signature="ssv",
                body=["org.bluez.Adapter1", "Powered", Variant("b", True)],
            )
        )
        if reply.message_type is MessageType.ERROR:
            raise BleakError(f"power on {self._adapter}: {reply.error_name}")

    async def _start_scanner(self, mode: ScannerMode) -> None:
        passive = mode is ScannerMode.PASSIVE
        args = dict(PASSIVE_ARGS if passive else ACTIVE_ARGS)
        args["adapter"] = self._adapter
        scanner = BleakScanner(
            None, scanning_mode="passive" if passive else "active", bluez=args
        )
        await scanner.start()
        self._scanner = scanner

    async def _stop_scanner(self) -> None:
        scanner, self._scanner = self._scanner, None
        if scanner is None:
            return
        try:
            await scanner.stop()
        except (BleakError, OSError) as err:
            _LOGGER.debug("Stopping the scanner: %s", err)

    async def _watch_scanner(self) -> None:
        while True:
            await asyncio.sleep(SCANNER_RETRY)
            async with self._scan_lock:
                silent = time.monotonic() - self._last_advertisement
                if self._scanner is None:
                    await self._apply_mode()
                elif silent > SCANNER_SILENCE:
                    _LOGGER.warning(
                        "No advertisement for %.0f s; restarting the scanner",
                        silent,
                    )
                    await self._stop_scanner()
                    await self._apply_mode()

    # Connections

    async def connect(self, address: int, address_type: int) -> int:
        if address in self._devices:
            raise BleError(GATT_BUSY, "already connected")
        text = format_address(address)
        client = self._client_factory(
            text,
            disconnected_callback=functools.partial(self._lost, address),
            timeout=CONNECT_TIMEOUT,
            bluez={"adapter": self._adapter},
        )
        device = Device(address, client)
        self._devices[address] = device
        _LOGGER.debug("Connecting to %s (type %d)", text, address_type)
        try:
            # The scanner pauses while connecting, as on an ESP32 proxy: the
            # 4.9 kernel of L4T refuses an LE connection during discovery
            # ("Operation already in progress").
            async with self._scan_lock:
                await self._stop_scanner()
                try:
                    async with asyncio.timeout(CONNECT_TIMEOUT + 5):
                        await client.connect()
                finally:
                    await self._apply_mode()
            return device.load_services()
        except BaseException as err:
            self._devices.pop(address, None)
            device.closing = True
            await _quiet(client.disconnect())
            if isinstance(err, BleakDeviceNotFoundError):
                raise BleError(CONN_FAIL_ESTABLISH, "device not found") from err
            if isinstance(err, TimeoutError):
                raise BleError(CONN_TIMEOUT, "connect timed out") from err
            if isinstance(err, BleakError):
                raise BleError(CONN_FAIL_ESTABLISH, str(err)) from err
            raise

    async def disconnect(self, address: int) -> None:
        device = self._devices.pop(address, None)
        if device is None:
            return
        device.closing = True
        await _quiet(device.client.disconnect())

    def _lost(self, address: int, client: Any) -> None:
        device = self._devices.get(address)
        if device is None or device.client is not client or device.closing:
            return
        del self._devices[address]
        self._on_disconnected(address, CONN_TERMINATE_PEER_USER)

    def _device(self, address: int) -> Device:
        device = self._devices.get(address)
        if device is None:
            raise BleError(GATT_NOT_CONNECTED, "not connected")
        return device

    def services(self, address: int) -> list[GattService]:
        return self._device(address).services

    async def clear_cache(self, address: int) -> None:
        if address in self._devices:
            raise BleError(GATT_BUSY, "still connected")
        bus = await self._system_bus()
        path = f"/org/bluez/{self._adapter}/dev_" + format_address(
            address
        ).replace(":", "_")
        reply = await bus.call(
            Message(
                destination="org.bluez",
                path=f"/org/bluez/{self._adapter}",
                interface="org.bluez.Adapter1",
                member="RemoveDevice",
                signature="o",
                body=[path],
            )
        )
        # An unknown device has no cache to clear.
        if reply.message_type is MessageType.ERROR and reply.error_name != (
            "org.bluez.Error.DoesNotExist"
        ):
            raise BleError(GATT_ERROR, f"RemoveDevice: {reply.error_name}")

    # GATT

    async def read(self, address: int, handle: int) -> bytes:
        device = self._device(address)
        char = device.characteristic(handle)
        return bytes(await gatt_call(device.client.read_gatt_char(char)))

    async def write(
        self, address: int, handle: int, data: bytes, response: bool
    ) -> None:
        device = self._device(address)
        char = device.characteristic(handle)
        await gatt_call(
            device.client.write_gatt_char(char, data, response=response)
        )

    async def read_descriptor(self, address: int, handle: int) -> bytes:
        device = self._device(address)
        desc = device.descriptor(handle)
        return bytes(await gatt_call(device.client.read_gatt_descriptor(desc)))

    async def write_descriptor(
        self, address: int, handle: int, data: bytes
    ) -> None:
        device = self._device(address)
        desc = device.descriptor(handle)
        if desc.uuid == CCCD_UUID:
            char = device.characteristic(desc.characteristic_handle)
            if data[:1] and data[0] & 0x03:
                await self._start_notify(device, char)
            else:
                await self._stop_notify(device, char)
            return
        await gatt_call(device.client.write_gatt_descriptor(desc, data))

    async def start_notify(self, address: int, handle: int) -> None:
        device = self._device(address)
        await self._start_notify(device, device.characteristic(handle))

    async def stop_notify(self, address: int, handle: int) -> None:
        device = self._device(address)
        await self._stop_notify(device, device.characteristic(handle))

    async def _start_notify(
        self, device: Device, char: BleakGATTCharacteristic
    ) -> None:
        if char.handle in device.notifying:
            return
        await gatt_call(
            device.client.start_notify(
                char, functools.partial(self._notified, device.address)
            )
        )
        device.notifying.add(char.handle)

    async def _stop_notify(
        self, device: Device, char: BleakGATTCharacteristic
    ) -> None:
        if char.handle not in device.notifying:
            return
        device.notifying.discard(char.handle)
        await gatt_call(device.client.stop_notify(char))

    def _notified(
        self, address: int, sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        self._on_notification(address, sender.handle, bytes(data))


def _ignore(*_args: Any) -> None:
    pass


async def _quiet(operation: Awaitable[Any]) -> None:
    try:
        async with asyncio.timeout(10):
            await operation
    except (BleakError, OSError, TimeoutError, EOFError) as err:
        _LOGGER.debug("Ignoring: %r", err)
