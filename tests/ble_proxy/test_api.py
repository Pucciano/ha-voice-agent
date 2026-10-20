"""The proxy as Home Assistant sees it: aioesphomeapi over Noise."""

import asyncio

import pytest
from aioesphomeapi import APIClient
from aioesphomeapi.core import (
    BluetoothGATTAPIError,
    InvalidEncryptionKeyAPIError,
    RequiresEncryptionAPIError,
)
from aioesphomeapi.model import BluetoothProxyFeature
from helpers import (
    ADAPTER_MAC,
    CCCD_HANDLE,
    MOUNT,
    MOUNT_TYPE,
    NOTIFY_HANDLE,
    OTHER_KEY_TEXT,
    VALUE_HANDLE,
    FakeBackend,
    RawAdvertisement,
    client,
    run,
    running,
    until,
)
from app.api import FEATURES
from app.backend import (
    CONN_FAIL_ESTABLISH,
    CONN_TERMINATE_PEER_USER,
    GATT_INVALID_HANDLE,
    GATT_NO_RESOURCES,
    GATT_NOT_CONNECTED,
)

ADVERTISEMENT = RawAdvertisement(
    address=MOUNT,
    address_type=MOUNT_TYPE,
    rssi=-61,
    data=bytes.fromhex("02010611070ea16cc459393a11ea8e2d0242ac130003"),
)


class States:
    """Collects BluetoothDeviceConnectionResponse callbacks."""

    def __init__(self) -> None:
        self.seen: list[tuple[bool, int, int]] = []

    def __call__(self, connected: bool, mtu: int, error: int) -> None:
        self.seen.append((connected, mtu, error))


async def connect(api: APIClient, address: int = MOUNT) -> States:
    states = States()
    await api.bluetooth_device_connect(
        address,
        states,
        feature_flags=FEATURES,
        has_cache=False,
        address_type=MOUNT_TYPE,
        timeout=5,
    )
    return states


def test_device_info_describes_an_encrypted_bluetooth_proxy() -> None:
    async def body() -> None:
        async with running() as (port, _backend, _proxy):
            async with client(port) as api:
                info, entities, services = (
                    await api.device_info_and_list_entities()
                )
                version = api.api_version
        assert (version.major, version.minor) == (1, 14)
        assert info.name == "aivi-ble-test"
        assert info.friendly_name == "Bluetooth Test"
        assert info.mac_address == ADAPTER_MAC
        assert info.bluetooth_mac_address == ADAPTER_MAC
        assert info.suggested_area == "Wohnzimmer"
        assert info.api_encryption_supported
        assert not info.uses_password
        flags = info.bluetooth_proxy_feature_flags_compat(version)
        assert flags == FEATURES
        assert flags & BluetoothProxyFeature.REMOTE_CACHING
        assert flags & BluetoothProxyFeature.RAW_ADVERTISEMENTS
        assert not flags & BluetoothProxyFeature.PAIRING
        assert entities == [] and services == []

    run(body)


def test_wrong_key_is_rejected() -> None:
    async def body() -> None:
        async with running() as (port, _backend, _proxy):
            with pytest.raises(InvalidEncryptionKeyAPIError):
                async with client(port, OTHER_KEY_TEXT):
                    pass

    run(body)


def test_plaintext_client_is_told_to_use_encryption() -> None:
    async def body() -> None:
        async with running() as (port, _backend, _proxy):
            with pytest.raises(RequiresEncryptionAPIError):
                async with client(port, None):
                    pass

    run(body)


def test_raw_advertisements_and_scanner_state_reach_the_client() -> None:
    async def body() -> None:
        async with running() as (port, backend, _proxy):
            async with client(port) as api:
                batches = []
                states = []
                api.subscribe_bluetooth_scanner_state(states.append)
                api.subscribe_bluetooth_le_raw_advertisements(batches.append)
                await until(lambda: states)
                backend.advertise(ADVERTISEMENT)
                backend.advertise(ADVERTISEMENT)
                await until(lambda: batches)
                api.bluetooth_scanner_set_mode(0)  # passive, as AUTO asks
                await until(lambda: backend.modes[-1] == 0)
        assert states[0].state == 2  # RUNNING
        advs = batches[0].advertisements
        assert len(advs) == 2
        assert advs[0].address == MOUNT
        assert advs[0].address_type == MOUNT_TYPE
        assert advs[0].rssi == -61
        assert advs[0].data == ADVERTISEMENT.data

    run(body)


def test_connect_services_read_write_notify_disconnect() -> None:
    async def body() -> None:
        async with running() as (port, backend, _proxy):
            async with client(port) as api:
                slots = []
                api.subscribe_bluetooth_connections_free(
                    lambda free, limit, allocated: slots.append(
                        (free, limit, list(allocated))
                    )
                )
                await until(lambda: slots)
                states = await connect(api)
                assert states.seen == [(True, 247, 0)]
                await until(lambda: slots[-1] == (2, 3, [MOUNT]))

                services = await api.bluetooth_gatt_get_services(MOUNT)
                service = services.services[0]
                assert service.uuid == "030013ac-4202-2d8e-ea11-3959c46ca10e"
                chars = {c.handle: c for c in service.characteristics}
                assert chars[VALUE_HANDLE].properties == 0x0A
                assert chars[NOTIFY_HANDLE].descriptors[0].handle == CCCD_HANDLE

                assert (
                    await api.bluetooth_gatt_read(MOUNT, VALUE_HANDLE)
                    == b"\x01"
                )
                await api.bluetooth_gatt_write(
                    MOUNT, VALUE_HANDLE, b"\x02", True
                )
                await api.bluetooth_gatt_write(
                    MOUNT, VALUE_HANDLE, b"\x03", False
                )
                await until(lambda: len(backend.writes) == 2)
                assert backend.writes == [
                    ("char", MOUNT, VALUE_HANDLE, b"\x02", True),
                    ("char", MOUNT, VALUE_HANDLE, b"\x03", False),
                ]
                assert (
                    await api.bluetooth_gatt_read_descriptor(MOUNT, CCCD_HANDLE)
                    == b"\x00\x00"
                )

                received = []
                stop, _remove = await api.bluetooth_gatt_start_notify(
                    MOUNT,
                    NOTIFY_HANDLE,
                    lambda handle, data: received.append(data),
                )
                assert (MOUNT, NOTIFY_HANDLE) in backend.notifying
                # What Home Assistant does next on a REMOTE_CACHING proxy.
                await api.bluetooth_gatt_write_descriptor(
                    MOUNT, CCCD_HANDLE, b"\x01\x00"
                )
                backend.notify(MOUNT, NOTIFY_HANDLE, b"\x2a")
                await until(lambda: received)
                assert received == [b"\x2a"]
                await stop()
                await until(lambda: not backend.notifying)

                await api.bluetooth_device_disconnect(MOUNT)
                assert MOUNT not in backend.connected
                await until(lambda: slots[-1] == (3, 3, []))

    run(body)


def test_failed_connect_reports_the_error_and_frees_the_slot() -> None:
    async def body() -> None:
        async with running() as (port, _backend, proxy):
            async with client(port) as api:
                states = await connect(api, address=0x112233445566)
                assert states.seen == [(False, 0, CONN_FAIL_ESTABLISH)]
                await until(lambda: not proxy._connections)

    run(body)


def test_all_slots_taken_refuses_another_connection() -> None:
    async def body() -> None:
        backend = FakeBackend()
        backend.peripherals[0x112233445566] = []
        async with running(backend, max_connections=1) as (port, _b, _p):
            async with client(port) as api:
                await connect(api)
                states = await connect(api, address=0x112233445566)
        assert states.seen == [(False, 0, GATT_NO_RESOURCES)]

    run(body)


def test_gatt_without_connection_answers_not_connected() -> None:
    async def body() -> None:
        async with running() as (port, _backend, _proxy):
            async with client(port) as api:
                with pytest.raises(BluetoothGATTAPIError) as err:
                    await api.bluetooth_gatt_read(MOUNT, VALUE_HANDLE)
        assert err.value.error.error == GATT_NOT_CONNECTED

    run(body)


def test_gatt_error_carries_the_code() -> None:
    async def body() -> None:
        async with running() as (port, _backend, _proxy):
            async with client(port) as api:
                await connect(api)
                with pytest.raises(BluetoothGATTAPIError) as err:
                    await api.bluetooth_gatt_read(MOUNT, 0x0099)
        assert err.value.error.error == GATT_INVALID_HANDLE

    run(body)


def test_peripheral_disconnect_reaches_the_client() -> None:
    async def body() -> None:
        async with running() as (port, backend, proxy):
            async with client(port) as api:
                states = await connect(api)
                backend.drop(MOUNT)
                await until(lambda: len(states.seen) == 2)
                assert not proxy._connections
        assert states.seen[1] == (False, 0, CONN_TERMINATE_PEER_USER)

    run(body)


def test_ended_session_disconnects_its_devices() -> None:
    async def body() -> None:
        async with running() as (port, backend, proxy):
            async with client(port) as api:
                await connect(api)
                assert MOUNT in backend.connected
            await until(lambda: MOUNT not in backend.connected)
            assert not proxy._connections
            # A new session finds every slot free.
            async with client(port) as api:
                slots = []
                api.subscribe_bluetooth_connections_free(
                    lambda free, limit, allocated: slots.append(free)
                )
                await until(lambda: slots)
                assert slots == [3]

    run(body)


def test_disconnect_while_connecting_frees_the_slot() -> None:
    async def body() -> None:
        backend = FakeBackend()
        backend.connect_gate = asyncio.Event()
        async with running(backend) as (port, _backend, proxy):
            async with client(port) as api:
                attempt = asyncio.ensure_future(connect(api))
                await until(lambda: proxy._connections)
                await api.bluetooth_device_disconnect(MOUNT)
                assert not proxy._connections
                attempt.cancel()
                backend.connect_gate.set()
                await asyncio.sleep(0.05)
        assert MOUNT not in backend.connected

    run(body)


def test_server_pings_a_quiet_client_instead_of_dropping_it(
    monkeypatch,
) -> None:
    import app.api

    monkeypatch.setattr(app.api, "PING_AFTER", 0.1)
    monkeypatch.setattr(app.api, "CLOSE_AFTER", 0.6)
    monkeypatch.setattr(app.api, "KEEPALIVE_CHECK", 0.05)

    async def body() -> None:
        async with running() as (port, backend, _proxy):
            async with client(port) as api:
                batches = []
                api.subscribe_bluetooth_le_raw_advertisements(batches.append)
                # Advertisements keep the client from pinging on its own.
                for _ in range(15):
                    backend.advertise(ADVERTISEMENT)
                    await asyncio.sleep(0.1)
                info = await api.device_info()
        assert info.name == "aivi-ble-test"
        assert len(batches) >= 10

    run(body)
