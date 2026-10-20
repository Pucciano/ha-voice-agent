"""The proxy between API sessions and one Bluetooth backend.

All sessions share the advertisement fan-out (batched every 100 ms, as an
ESP32 proxy does), the scanner state, the connection slots and the record of
which session owns which Bluetooth connection. A connection belongs to the
session that opened it. When that session ends, the proxy disconnects it, so
a Home Assistant restart never leaves a slot taken.
"""

from __future__ import annotations

# api_pb2 builds its message classes at import time, out of pylint's sight.
# pylint: disable=no-member

import asyncio
import dataclasses
import logging
import time
from typing import Any, Awaitable, Callable, Coroutine, Protocol

from aioesphomeapi import api_pb2

from .backend import (
    ADDRESS_PUBLIC,
    GATT_BUSY,
    GATT_NO_RESOURCES,
    GATT_NOT_CONNECTED,
    GATT_REQ_NOT_SUPPORTED,
    Backend,
    BleError,
    GattService,
    RawAdvertisement,
    ScannerMode,
    ScannerState,
    format_address,
)

_LOGGER = logging.getLogger(__name__)

ADVERTISEMENT_INTERVAL = 0.1
ADVERTISEMENTS_PER_MESSAGE = 32
# A session whose socket buffer holds more than this misses advertisements
# until it catches up; GATT answers are never dropped.
MAX_BACKLOG_BYTES = 256 * 1024
STATS_INTERVAL = 300.0

RequestType = api_pb2.BluetoothDeviceRequestType
CONNECT_REQUESTS = (
    RequestType.BLUETOOTH_DEVICE_REQUEST_TYPE_CONNECT_V3_WITH_CACHE,
    RequestType.BLUETOOTH_DEVICE_REQUEST_TYPE_CONNECT_V3_WITHOUT_CACHE,
)


class Session(Protocol):
    """The side of an API session the proxy talks to."""

    def send(self, *messages: Any) -> None: ...

    @property
    def backlog(self) -> int:
        """Bytes waiting in the session's socket buffer."""


@dataclasses.dataclass(eq=False)
class _Connection:
    address: int
    owner: Session
    connected: bool = False
    mtu: int = 0
    task: asyncio.Task[None] | None = None


@dataclasses.dataclass
class _Stats:
    received: int = 0
    sent: int = 0
    dropped: int = 0
    addresses: set[int] = dataclasses.field(default_factory=set)
    since: float = dataclasses.field(default_factory=time.monotonic)


def _uuid_words(uuid: str) -> list[int]:
    value = int(uuid.replace("-", ""), 16)
    return [value >> 64, value & 0xFFFFFFFFFFFFFFFF]


def service_message(address: int, service: GattService) -> Any:
    """One service, as Home Assistant expects it from an ESP32 proxy."""

    return api_pb2.BluetoothGATTGetServicesResponse(
        address=address,
        services=[
            api_pb2.BluetoothGATTService(
                uuid=_uuid_words(service.uuid),
                handle=service.handle,
                characteristics=[
                    api_pb2.BluetoothGATTCharacteristic(
                        uuid=_uuid_words(char.uuid),
                        handle=char.handle,
                        properties=char.properties,
                        descriptors=[
                            api_pb2.BluetoothGATTDescriptor(
                                uuid=_uuid_words(desc.uuid),
                                handle=desc.handle,
                            )
                            for desc in char.descriptors
                        ],
                    )
                    for char in service.characteristics
                ],
            )
        ],
    )


class BluetoothProxy:  # pylint: disable=too-many-instance-attributes
    def __init__(
        self,
        backend: Backend,
        max_connections: int,
        configured_mode: ScannerMode = ScannerMode.ACTIVE,
    ) -> None:
        self._backend = backend
        self._limit = max_connections
        self._configured_mode = configured_mode
        self._state = ScannerState.IDLE
        self._mode = configured_mode
        self._connections: dict[int, _Connection] = {}
        self._advertisement_sessions: set[Session] = set()
        self._slot_sessions: set[Session] = set()
        self._pending: list[Any] = []
        self._flush_handle: asyncio.TimerHandle | None = None
        self._tasks: dict[asyncio.Task[None], Session | None] = {}
        self._stats = _Stats()
        backend.set_callbacks(
            on_advertisement=self._on_advertisement,
            on_scanner_state=self._on_scanner_state,
            on_disconnected=self._on_disconnected,
            on_notification=self._on_notification,
        )

    @property
    def limit(self) -> int:
        return self._limit

    # Sessions

    def release_session(self, session: Session) -> None:
        """Forgets an ended session and disconnects its connections."""

        self._advertisement_sessions.discard(session)
        self._slot_sessions.discard(session)
        for task, owner in list(self._tasks.items()):
            if owner is session:
                task.cancel()
        owned = [c for c in self._connections.values() if c.owner is session]
        for conn in owned:
            del self._connections[conn.address]
            if conn.task is not None:
                conn.task.cancel()
            self._spawn(None, self._quiet_disconnect(conn.address))
        if owned:
            self._broadcast_slots()

    async def close(self) -> None:
        if self._flush_handle is not None:
            self._flush_handle.cancel()
        for task in list(self._tasks):
            task.cancel()
        for conn in list(self._connections.values()):
            del self._connections[conn.address]
            if conn.task is not None:
                conn.task.cancel()
            await self._quiet_disconnect(conn.address)

    # Advertisements and scanner

    def subscribe_advertisements(self, session: Session) -> None:
        self._advertisement_sessions.add(session)
        # Home Assistant takes this answer as the sign that the subscription
        # landed; without it, it subscribes again and again.
        session.send(self._scanner_message())

    def unsubscribe_advertisements(self, session: Session) -> None:
        self._advertisement_sessions.discard(session)

    def set_scanner_mode(self, mode: int) -> None:
        self._spawn(None, self._backend.set_scanner_mode(ScannerMode(mode)))

    def log_stats(self) -> None:
        stats = self._stats
        seconds = max(time.monotonic() - stats.since, 1e-3)
        _LOGGER.info(
            "Advertisements in the last %.0f s: %.1f/s received, %.1f/s "
            "sent, %d dropped, %d addresses; scanner %s %s; %d of %d "
            "connections in use",
            seconds,
            stats.received / seconds,
            stats.sent / seconds,
            stats.dropped,
            len(stats.addresses),
            self._state.name,
            self._mode.name,
            len(self._connections),
            self._limit,
        )
        self._stats = _Stats()

    def _scanner_message(self) -> Any:
        return api_pb2.BluetoothScannerStateResponse(
            state=self._state,
            mode=self._mode,
            configured_mode=self._configured_mode,
        )

    def _on_scanner_state(self, state: ScannerState, mode: ScannerMode) -> None:
        if (state, mode) == (self._state, self._mode):
            return
        # The scanner pauses for every connection; only failures and mode
        # changes are news.
        quiet = state is not ScannerState.FAILED and mode is self._mode
        _LOGGER.log(
            logging.DEBUG if quiet else logging.INFO,
            "Scanner %s, %s",
            state.name,
            mode.name,
        )
        self._state, self._mode = state, mode
        message = self._scanner_message()
        for session in self._advertisement_sessions:
            session.send(message)

    def _on_advertisement(self, adv: RawAdvertisement) -> None:
        self._stats.received += 1
        self._stats.addresses.add(adv.address)
        if not self._advertisement_sessions:
            return
        self._pending.append(
            api_pb2.BluetoothLERawAdvertisement(
                address=adv.address,
                rssi=adv.rssi,
                address_type=adv.address_type,
                data=adv.data,
            )
        )
        if self._flush_handle is None:
            self._flush_handle = asyncio.get_running_loop().call_later(
                ADVERTISEMENT_INTERVAL, self._flush_advertisements
            )

    def _flush_advertisements(self) -> None:
        self._flush_handle = None
        pending, self._pending = self._pending, []
        messages = [
            api_pb2.BluetoothLERawAdvertisementsResponse(
                advertisements=pending[i : i + ADVERTISEMENTS_PER_MESSAGE]
            )
            for i in range(0, len(pending), ADVERTISEMENTS_PER_MESSAGE)
        ]
        for session in self._advertisement_sessions:
            if session.backlog > MAX_BACKLOG_BYTES:
                self._stats.dropped += len(pending)
                continue
            session.send(*messages)
            self._stats.sent += len(pending)

    # Connection slots

    def subscribe_slots(self, session: Session) -> None:
        self._slot_sessions.add(session)
        session.send(self._slots_message())

    def _slots_message(self) -> Any:
        return api_pb2.BluetoothConnectionsFreeResponse(
            free=self._limit - len(self._connections),
            limit=self._limit,
            allocated=list(self._connections),
        )

    def _broadcast_slots(self) -> None:
        message = self._slots_message()
        for session in self._slot_sessions:
            session.send(message)

    def _release(self, conn: _Connection) -> bool:
        """Frees the slot; False if it was already freed."""

        if self._connections.get(conn.address) is not conn:
            return False
        del self._connections[conn.address]
        self._broadcast_slots()
        return True

    # Device requests

    def device_request(self, session: Session, msg: Any) -> None:
        address = msg.address
        kind = msg.request_type
        if kind in CONNECT_REQUESTS:
            address_type = (
                msg.address_type if msg.has_address_type else ADDRESS_PUBLIC
            )
            self._start_connect(session, address, address_type)
        elif kind == RequestType.BLUETOOTH_DEVICE_REQUEST_TYPE_DISCONNECT:
            self._spawn(session, self._disconnect(session, address))
        elif kind == RequestType.BLUETOOTH_DEVICE_REQUEST_TYPE_CLEAR_CACHE:
            self._spawn(session, self._clear_cache(session, address))
        elif kind == RequestType.BLUETOOTH_DEVICE_REQUEST_TYPE_PAIR:
            session.send(
                api_pb2.BluetoothDevicePairingResponse(
                    address=address, paired=False, error=GATT_REQ_NOT_SUPPORTED
                )
            )
        elif kind == RequestType.BLUETOOTH_DEVICE_REQUEST_TYPE_UNPAIR:
            session.send(
                api_pb2.BluetoothDeviceUnpairingResponse(
                    address=address, success=False, error=GATT_REQ_NOT_SUPPORTED
                )
            )
        else:
            # The connection request of proxies before REMOTE_CACHING.
            session.send(
                api_pb2.BluetoothDeviceConnectionResponse(
                    address=address,
                    connected=False,
                    error=GATT_REQ_NOT_SUPPORTED,
                )
            )

    def _start_connect(
        self, session: Session, address: int, address_type: int
    ) -> None:
        conn = self._connections.get(address)
        if conn is not None:
            if conn.owner is not session:
                session.send(_connection_message(address, error=GATT_BUSY))
            elif conn.connected:
                session.send(_connection_message(address, mtu=conn.mtu))
            # Otherwise the running attempt answers.
            return
        if len(self._connections) >= self._limit:
            session.send(_connection_message(address, error=GATT_NO_RESOURCES))
            return
        conn = _Connection(address, session)
        self._connections[address] = conn
        self._broadcast_slots()
        conn.task = asyncio.get_running_loop().create_task(
            self._connect(conn, address_type)
        )

    async def _connect(self, conn: _Connection, address_type: int) -> None:
        address = conn.address
        try:
            mtu = await self._backend.connect(address, address_type)
        except BleError as err:
            _LOGGER.info(
                "Connect to %s failed: %s", format_address(address), err
            )
            if self._release(conn):
                conn.owner.send(_connection_message(address, error=err.code))
            return
        if self._connections.get(address) is not conn:
            await self._quiet_disconnect(address)
            return
        conn.connected = True
        conn.mtu = mtu
        _LOGGER.info("Connected to %s, MTU %d", format_address(address), mtu)
        conn.owner.send(_connection_message(address, mtu=mtu))

    async def _disconnect(self, session: Session, address: int) -> None:
        conn = self._connections.get(address)
        if conn is None or conn.owner is not session:
            session.send(_connection_message(address, connected=False))
            return
        self._release(conn)
        if conn.task is not None and not conn.task.done():
            conn.task.cancel()
        await self._quiet_disconnect(address)
        session.send(_connection_message(address, connected=False))

    async def _quiet_disconnect(self, address: int) -> None:
        try:
            await self._backend.disconnect(address)
        except BleError as err:
            _LOGGER.info("Disconnect %s: %s", format_address(address), err)

    def _on_disconnected(self, address: int, reason: int) -> None:
        conn = self._connections.get(address)
        if conn is None or not conn.connected:
            return
        _LOGGER.info("%s disconnected (%#x)", format_address(address), reason)
        if self._release(conn):
            conn.owner.send(_connection_message(address, error=reason))

    async def _clear_cache(self, session: Session, address: int) -> None:
        try:
            await self._backend.clear_cache(address)
        except BleError as err:
            session.send(
                api_pb2.BluetoothDeviceClearCacheResponse(
                    address=address, success=False, error=err.code
                )
            )
            return
        session.send(
            api_pb2.BluetoothDeviceClearCacheResponse(
                address=address, success=True
            )
        )

    # GATT

    def get_services(self, session: Session, address: int) -> None:
        if not self._owns(session, address):
            session.send(_gatt_error(address, 0, GATT_NOT_CONNECTED))
            return
        try:
            services = self._backend.services(address)
        except BleError as err:
            session.send(_gatt_error(address, 0, err.code))
            return
        session.send(
            *(service_message(address, service) for service in services),
            api_pb2.BluetoothGATTGetServicesDoneResponse(address=address),
        )

    def read(self, session: Session, msg: Any) -> None:
        self._gatt(
            session,
            msg.address,
            msg.handle,
            self._backend.read(msg.address, msg.handle),
            lambda data: api_pb2.BluetoothGATTReadResponse(
                address=msg.address, handle=msg.handle, data=data
            ),
        )

    def read_descriptor(self, session: Session, msg: Any) -> None:
        self._gatt(
            session,
            msg.address,
            msg.handle,
            self._backend.read_descriptor(msg.address, msg.handle),
            lambda data: api_pb2.BluetoothGATTReadResponse(
                address=msg.address, handle=msg.handle, data=data
            ),
        )

    def write(self, session: Session, msg: Any) -> None:
        # Home Assistant does not wait for a write without response; an
        # answer could be taken for the next write to the same handle.
        self._gatt(
            session,
            msg.address,
            msg.handle,
            self._backend.write(
                msg.address, msg.handle, msg.data, msg.response
            ),
            (
                (
                    lambda _: api_pb2.BluetoothGATTWriteResponse(
                        address=msg.address, handle=msg.handle
                    )
                )
                if msg.response
                else None
            ),
        )

    def write_descriptor(self, session: Session, msg: Any) -> None:
        self._gatt(
            session,
            msg.address,
            msg.handle,
            self._backend.write_descriptor(msg.address, msg.handle, msg.data),
            lambda _: api_pb2.BluetoothGATTWriteResponse(
                address=msg.address, handle=msg.handle
            ),
        )

    def notify(self, session: Session, msg: Any) -> None:
        operation = (
            self._backend.start_notify(msg.address, msg.handle)
            if msg.enable
            else self._backend.stop_notify(msg.address, msg.handle)
        )
        self._gatt(
            session,
            msg.address,
            msg.handle,
            operation,
            lambda _: api_pb2.BluetoothGATTNotifyResponse(
                address=msg.address, handle=msg.handle
            ),
        )

    def _on_notification(self, address: int, handle: int, data: bytes) -> None:
        conn = self._connections.get(address)
        if conn is not None and conn.connected:
            conn.owner.send(
                api_pb2.BluetoothGATTNotifyDataResponse(
                    address=address, handle=handle, data=data
                )
            )

    def _owns(self, session: Session, address: int) -> bool:
        conn = self._connections.get(address)
        return conn is not None and conn.owner is session and conn.connected

    def _gatt(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        session: Session,
        address: int,
        handle: int,
        operation: Awaitable[Any],
        answer: Callable[[Any], Any] | None,
    ) -> None:
        if not self._owns(session, address):
            # Never awaited: close it, or asyncio warns about it.
            operation.close()  # type: ignore[attr-defined]
            if answer is not None:
                session.send(_gatt_error(address, handle, GATT_NOT_CONNECTED))
            return

        async def run() -> None:
            try:
                result = await operation
            except BleError as err:
                if answer is not None:
                    session.send(_gatt_error(address, handle, err.code))
                return
            if answer is not None:
                session.send(answer(result))

        self._spawn(session, run())

    def _spawn(
        self, session: Session | None, coro: Coroutine[Any, Any, None]
    ) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks[task] = session
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.pop(task, None)
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.error("Bluetooth task failed", exc_info=task.exception())


def _connection_message(
    address: int, mtu: int = 0, error: int = 0, connected: bool | None = None
) -> Any:
    if connected is None:
        connected = error == 0
    return api_pb2.BluetoothDeviceConnectionResponse(
        address=address, connected=connected, mtu=mtu, error=error
    )


def _gatt_error(address: int, handle: int, error: int) -> Any:
    return api_pb2.BluetoothGATTErrorResponse(
        address=address, handle=handle, error=error
    )
