"""BlueZ backend on a stand-in for bleak's client: handles, properties,
the CCCD rule and the error codes."""

import pytest
from bleak.exc import (
    BleakDeviceNotFoundError,
    BleakError,
    BleakGATTProtocolError,
)
from helpers import MOUNT, run
from app.backend import (
    CCCD_UUID,
    CONN_FAIL_ESTABLISH,
    CONN_TERMINATE_PEER_USER,
    GATT_BUSY,
    GATT_INVALID_HANDLE,
    GATT_NOT_CONNECTED,
    GATT_WRITE_NOT_PERMIT,
    BleError,
)
from app.bluez import BlueZBackend, gatt_call, property_bits


class Desc:
    def __init__(self, handle: int, uuid: str, char: "Char") -> None:
        self.handle = handle
        self.uuid = uuid
        self.characteristic_handle = char.handle


class Char:
    def __init__(self, handle, uuid, properties, mtu=247) -> None:
        self.handle = handle
        self.uuid = uuid
        self.properties = properties
        self.max_write_without_response_size = mtu - 3
        self.descriptors: list[Desc] = []


class Service:
    def __init__(self, handle: int, uuid: str, chars: list[Char]) -> None:
        self.handle = handle
        self.uuid = uuid
        self.characteristics = chars


def mount_services() -> list[Service]:
    notify = Char(
        0x12, "c005fa00-0651-4800-b000-000000000000", ["read", "notify"]
    )
    notify.descriptors = [Desc(0x14, CCCD_UUID, notify)]
    preset = Char(
        0x10, "c005fa2a-0651-4800-b000-000000000000", ["read", "write"]
    )
    return [
        Service(0x0E, "030013ac-4202-2d8e-ea11-3959c46ca10e", [notify, preset]),
        Service(0x01, "00001801-0000-1000-8000-00805f9b34fb", []),
    ]


class FakeClient:
    """The parts of bleak.BleakClient the backend uses."""

    instances: list["FakeClient"] = []
    fail: BaseException | None = None

    def __init__(self, address, disconnected_callback, timeout, bluez) -> None:
        self.address = address
        self.disconnected_callback = disconnected_callback
        self.bluez = bluez
        self.services = mount_services()
        self.calls: list[tuple] = []
        FakeClient.instances.append(self)

    async def connect(self) -> None:
        if FakeClient.fail is not None:
            raise FakeClient.fail

    async def disconnect(self) -> None:
        self.calls.append(("disconnect",))

    async def read_gatt_char(self, char):
        self.calls.append(("read", char.handle))
        return bytearray(b"\x05")

    async def write_gatt_char(self, char, data, response):
        self.calls.append(("write", char.handle, data, response))

    async def read_gatt_descriptor(self, desc):
        return bytearray(b"\x01\x00")

    async def write_gatt_descriptor(self, desc, data):
        raise AssertionError("BlueZ refuses CCCD writes; never reached")

    async def start_notify(self, char, callback):
        self.calls.append(("start_notify", char.handle))
        self.callback = callback

    async def stop_notify(self, char):
        self.calls.append(("stop_notify", char.handle))


@pytest.fixture(autouse=True)
def reset_fake_client():
    FakeClient.instances = []
    FakeClient.fail = None


def make_backend(events: list) -> BlueZBackend:
    backend = BlueZBackend("hci0", events.append, client_factory=FakeClient)

    # The scanner talks to bluetoothd; here it only records the order.
    async def stop_scanner() -> None:
        events.append("scanner stopped")

    async def apply_mode() -> None:
        events.append("scanner started")

    backend._stop_scanner = stop_scanner  # type: ignore[method-assign]
    backend._apply_mode = apply_mode  # type: ignore[method-assign]
    backend.set_callbacks(
        on_advertisement=events.append,
        on_scanner_state=lambda state, mode: events.append((state, mode)),
        on_disconnected=lambda address, reason: events.append(
            ("lost", address, reason)
        ),
        on_notification=lambda address, handle, data: events.append(
            ("notified", address, handle, data)
        ),
    )
    return backend


def test_property_names_become_bits() -> None:
    assert property_bits(["read", "write-without-response", "notify"]) == 0x16
    assert property_bits(["indicate", "reliable-write", "encrypt-read"]) == 0xA0


def test_connect_reports_mtu_and_sorted_services() -> None:
    async def body() -> None:
        events: list = []
        backend = make_backend(events)
        mtu = await backend.connect(MOUNT, 1)
        assert mtu == 247
        assert events == ["scanner stopped", "scanner started"]
        assert FakeClient.instances[0].address == "D3:22:E6:10:3D:A6"
        assert FakeClient.instances[0].bluez == {"adapter": "hci0"}
        services = backend.services(MOUNT)
        assert [s.handle for s in services] == [0x01, 0x0E]
        chars = services[1].characteristics
        assert [c.handle for c in chars] == [0x10, 0x12]
        assert chars[0].properties == 0x0A
        assert chars[1].descriptors[0].handle == 0x14

    run(body)


def test_cccd_write_becomes_start_and_stop_notify() -> None:
    async def body() -> None:
        events: list = []
        backend = make_backend(events)
        await backend.connect(MOUNT, 1)
        client = FakeClient.instances[0]
        # Home Assistant: notify request, then the CCCD write.
        await backend.start_notify(MOUNT, 0x12)
        await backend.write_descriptor(MOUNT, 0x14, b"\x01\x00")
        assert client.calls.count(("start_notify", 0x12)) == 1
        client.callback(
            client.services[0].characteristics[0], bytearray(b"\x07")
        )
        assert events[-1] == ("notified", MOUNT, 0x12, b"\x07")
        await backend.write_descriptor(MOUNT, 0x14, b"\x00\x00")
        assert ("stop_notify", 0x12) in client.calls
        # A CCCD write alone also enables notifications.
        await backend.write_descriptor(MOUNT, 0x14, b"\x02\x00")
        assert client.calls.count(("start_notify", 0x12)) == 2

    run(body)


def test_unknown_handle_and_missing_connection() -> None:
    async def body() -> None:
        backend = make_backend([])
        with pytest.raises(BleError) as err:
            await backend.read(MOUNT, 0x10)
        assert err.value.code == GATT_NOT_CONNECTED
        await backend.connect(MOUNT, 1)
        with pytest.raises(BleError) as err:
            await backend.read(MOUNT, 0x99)
        assert err.value.code == GATT_INVALID_HANDLE
        with pytest.raises(BleError) as err:
            await backend.connect(MOUNT, 1)
        assert err.value.code == GATT_BUSY

    run(body)


def test_connect_failures_free_the_device() -> None:
    async def body() -> None:
        backend = make_backend([])
        FakeClient.fail = BleakDeviceNotFoundError("D3:22:E6:10:3D:A6")
        with pytest.raises(BleError) as err:
            await backend.connect(MOUNT, 1)
        assert err.value.code == CONN_FAIL_ESTABLISH
        FakeClient.fail = BleakError("[org.bluez.Error.InProgress] Busy")
        with pytest.raises(BleError) as err:
            await backend.connect(MOUNT, 1)
        assert err.value.code == CONN_FAIL_ESTABLISH
        assert ("disconnect",) in FakeClient.instances[0].calls
        FakeClient.fail = None
        assert await backend.connect(MOUNT, 1) == 247

    run(body)


def test_peripheral_loss_is_reported_once_and_own_disconnect_not_at_all() -> (
    None
):
    async def body() -> None:
        events: list = []
        backend = make_backend(events)
        await backend.connect(MOUNT, 1)
        first = FakeClient.instances[0]
        first.disconnected_callback(first)
        first.disconnected_callback(first)
        assert events.count(("lost", MOUNT, CONN_TERMINATE_PEER_USER)) == 1
        await backend.connect(MOUNT, 1)
        second = FakeClient.instances[1]
        await backend.disconnect(MOUNT)
        second.disconnected_callback(second)
        assert events.count(("lost", MOUNT, CONN_TERMINATE_PEER_USER)) == 1

    run(body)


def test_gatt_errors_map_to_esp_codes() -> None:
    async def fails(exc: BaseException):
        raise exc

    async def body() -> None:
        with pytest.raises(BleError) as err:
            await gatt_call(fails(BleakGATTProtocolError(0x03)))
        assert err.value.code == GATT_WRITE_NOT_PERMIT
        with pytest.raises(BleError) as err:
            await gatt_call(fails(BleakError("Not connected")))
        assert err.value.code == GATT_NOT_CONNECTED
        with pytest.raises(BleError) as err:
            await gatt_call(fails(BleakError("org.bluez.Error.InProgress")))
        assert err.value.code == GATT_BUSY

    run(body)


def test_adapter_name_is_checked() -> None:
    with pytest.raises(ValueError):
        BlueZBackend("usb0", print)
