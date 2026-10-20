"""Management socket events: Device Found, controller info, index removal."""

import asyncio
import struct

from helpers import ADAPTER_MAC, MOUNT, run
from app.mgmt import (
    MGMT_EV_ADV_MONITOR_DEVICE_FOUND,
    MGMT_EV_CMD_COMPLETE,
    MGMT_EV_DEVICE_FOUND,
    MGMT_EV_INDEX_REMOVED,
    MGMT_OP_READ_INFO,
    MgmtSocket,
    parse_controller_info,
    parse_device_found,
)

EIR = bytes.fromhex("020106") + bytes.fromhex("0bff3f0d000000000000")


def device_found(address_type: int = 0x02, rssi: int = -70, eir=EIR) -> bytes:
    raw_address = MOUNT.to_bytes(6, "little")
    return (
        struct.pack("<6sBbIH", raw_address, address_type, rssi, 0, len(eir))
        + eir
    )


def packet(event: int, index: int, params: bytes) -> bytes:
    return struct.pack("<HHH", event, index, len(params)) + params


def test_device_found_becomes_a_raw_advertisement() -> None:
    adv = parse_device_found(device_found())
    assert adv is not None
    assert adv.address == MOUNT
    assert adv.address_type == 1  # random
    assert adv.rssi == -70
    assert adv.data == EIR
    public = parse_device_found(device_found(address_type=0x01))
    assert public is not None and public.address_type == 0


def test_classic_unknown_rssi_and_short_events_are_skipped() -> None:
    assert parse_device_found(device_found(address_type=0x00)) is None
    assert parse_device_found(device_found(rssi=127)) is None
    assert parse_device_found(device_found()[:-1]) is None
    assert parse_device_found(b"\x00" * 5) is None


def test_controller_info_address_and_power() -> None:
    raw = bytes.fromhex("605f454b888c")  # little endian on the wire
    params = raw + b"\x0a" + b"\x5d\x00" + struct.pack("<II", 0xFFFF, 0x0001)
    info = parse_controller_info(params + bytes(3 + 249 + 11))
    assert info.address == ADAPTER_MAC
    assert info.powered


def test_socket_dispatches_events_for_its_adapter_only() -> None:
    async def body() -> None:
        seen: list = []
        removed: list = []
        sock = MgmtSocket(0, seen.append, lambda: removed.append(True))
        sock._handle(packet(MGMT_EV_DEVICE_FOUND, 0, device_found()))
        sock._handle(packet(MGMT_EV_DEVICE_FOUND, 1, device_found()))
        sock._handle(
            packet(
                MGMT_EV_ADV_MONITOR_DEVICE_FOUND,
                0,
                b"\x01\x00" + device_found(),
            )
        )
        assert [adv.address for adv in seen] == [MOUNT, MOUNT]
        sock._handle(packet(MGMT_EV_INDEX_REMOVED, 1, b""))
        assert not removed
        sock._handle(packet(MGMT_EV_INDEX_REMOVED, 0, b""))
        assert removed

    run(body)


def test_controller_info_reply_resolves_the_request() -> None:
    async def body() -> None:
        sock = MgmtSocket(0, print, print)
        sock._info = asyncio.get_running_loop().create_future()
        raw = bytes.fromhex("605f454b888c")
        reply = (
            struct.pack("<HB", MGMT_OP_READ_INFO, 0)
            + raw
            + b"\x0a\x5d\x00"
            + struct.pack("<II", 0xFFFF, 0)
            + bytes(263)
        )
        sock._handle(packet(MGMT_EV_CMD_COMPLETE, 0, reply))
        info = await sock._info
        assert info.address == ADAPTER_MAC
        assert not info.powered

    run(body)
