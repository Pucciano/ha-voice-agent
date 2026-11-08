"""The kernel's Bluetooth management socket: raw advertisements and the
adapter address.

BlueZ's D-Bus interface only hands out parsed advertisement fields. The
kernel sends every advertisement report as a Device Found event on the
management control channel, with the raw AD structures, the address type
and the RSSI, to every socket opened with CAP_NET_ADMIN. Home Assistant's
own Bluetooth integration reads them the same way. Scanning itself stays
with bluetoothd; this socket only listens and asks for controller info.

Python's socket module lacks AF_BLUETOOTH in many builds (the official
Docker images among them), so the socket is opened through libc.
"""

from __future__ import annotations

import asyncio
import ctypes
import dataclasses
import errno
import os
import struct
from typing import Callable

from .backend import ADDRESS_PUBLIC, ADDRESS_RANDOM, RawAdvertisement

AF_BLUETOOTH = 31
BTPROTO_HCI = 1
SOCK_RAW = 3
SOCK_NONBLOCK = 0o4000
SOCK_CLOEXEC = 0o2000000
HCI_DEV_NONE = 0xFFFF
HCI_CHANNEL_CONTROL = 3

MGMT_OP_READ_INFO = 0x0004
MGMT_EV_CMD_COMPLETE = 0x0001
MGMT_EV_CMD_STATUS = 0x0002
MGMT_EV_INDEX_ADDED = 0x0004
MGMT_EV_INDEX_REMOVED = 0x0005
MGMT_EV_DEVICE_FOUND = 0x0012
MGMT_EV_ADV_MONITOR_DEVICE_FOUND = 0x002F

_HEADER = struct.Struct("<HHH")
# address (6), address type, RSSI, flags, EIR length
_DEVICE_FOUND = struct.Struct("<6sBbIH")
_MGMT_ADDRESS_TYPES = {0x01: ADDRESS_PUBLIC, 0x02: ADDRESS_RANDOM}
RSSI_UNKNOWN = 127


class MgmtError(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class ControllerInfo:
    address: str
    powered: bool


def _address_text(raw: bytes) -> str:
    return ":".join(f"{octet:02X}" for octet in reversed(raw))


def parse_device_found(params: bytes) -> RawAdvertisement | None:
    """A Device Found event as an advertisement; None for BR/EDR or junk."""

    if len(params) < _DEVICE_FOUND.size:
        return None
    raw_address, address_type, rssi, _flags, eir_len = (
        _DEVICE_FOUND.unpack_from(params)
    )
    if address_type not in _MGMT_ADDRESS_TYPES or rssi == RSSI_UNKNOWN:
        return None
    eir = params[_DEVICE_FOUND.size : _DEVICE_FOUND.size + eir_len]
    if len(eir) != eir_len:
        return None
    return RawAdvertisement(
        address=int.from_bytes(raw_address, "little"),
        address_type=_MGMT_ADDRESS_TYPES[address_type],
        rssi=rssi,
        data=bytes(eir),
    )


def parse_controller_info(params: bytes) -> ControllerInfo:
    """Read Controller Information reply, after opcode and status."""

    if len(params) < 6 + 1 + 2 + 4 + 4:
        raise MgmtError("short controller info")
    current_settings = struct.unpack_from("<I", params, 6 + 1 + 2 + 4)[0]
    return ControllerInfo(
        address=_address_text(params[:6]),
        powered=bool(current_settings & 0x1),
    )


class MgmtSocket:
    """Control channel listener with one command: Read Controller Info."""

    def __init__(
        self,
        index: int,
        on_advertisement: Callable[[RawAdvertisement], None],
        on_index_removed: Callable[[], None],
    ) -> None:
        self._index = index
        self._on_advertisement = on_advertisement
        self._on_index_removed = on_index_removed
        self._fd = -1
        self._info: asyncio.Future[ControllerInfo] | None = None

    def open(self) -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        fd = libc.socket(
            AF_BLUETOOTH, SOCK_RAW | SOCK_CLOEXEC | SOCK_NONBLOCK, BTPROTO_HCI
        )
        if fd < 0:
            code = ctypes.get_errno()
            raise OSError(code, f"Bluetooth socket: {os.strerror(code)}")
        address = struct.pack(
            "<HHH", AF_BLUETOOTH, HCI_DEV_NONE, HCI_CHANNEL_CONTROL
        )
        if libc.bind(fd, address, len(address)) < 0:
            code = ctypes.get_errno()
            os.close(fd)
            raise OSError(
                code, f"Bluetooth control channel: {os.strerror(code)}"
            )
        self._fd = fd
        asyncio.get_running_loop().add_reader(fd, self._readable)

    def close(self) -> None:
        if self._fd >= 0:
            asyncio.get_running_loop().remove_reader(self._fd)
            os.close(self._fd)
            self._fd = -1

    async def read_controller_info(
        self, timeout: float = 5.0
    ) -> ControllerInfo:
        self._info = asyncio.get_running_loop().create_future()
        os.write(self._fd, _HEADER.pack(MGMT_OP_READ_INFO, self._index, 0))
        try:
            return await asyncio.wait_for(self._info, timeout)
        finally:
            self._info = None

    def _readable(self) -> None:
        while True:
            try:
                packet = os.read(self._fd, 1024)
            except BlockingIOError:
                return
            except OSError as err:
                if err.errno == errno.EINTR:
                    continue
                raise
            if len(packet) < _HEADER.size:
                continue
            self._handle(packet)

    def _handle(self, packet: bytes) -> None:
        event, index, length = _HEADER.unpack_from(packet)
        params = packet[_HEADER.size : _HEADER.size + length]
        if index != self._index:
            return
        if event == MGMT_EV_DEVICE_FOUND:
            adv = parse_device_found(params)
        elif event == MGMT_EV_ADV_MONITOR_DEVICE_FOUND:
            adv = parse_device_found(params[2:])  # after the monitor handle
        elif event == MGMT_EV_INDEX_REMOVED:
            self._on_index_removed()
            return
        elif event in (MGMT_EV_CMD_COMPLETE, MGMT_EV_CMD_STATUS):
            self._command_reply(event, params)
            return
        else:
            return
        if adv is not None:
            self._on_advertisement(adv)

    def _command_reply(self, event: int, params: bytes) -> None:
        if len(params) < 3 or self._info is None or self._info.done():
            return
        opcode, status = struct.unpack_from("<HB", params)
        if opcode != MGMT_OP_READ_INFO:
            return
        if event == MGMT_EV_CMD_STATUS or status != 0:
            self._info.set_exception(
                MgmtError(f"controller info failed, status {status:#x}")
            )
            return
        try:
            self._info.set_result(parse_controller_info(params[3:]))
        except MgmtError as err:
            self._info.set_exception(err)
