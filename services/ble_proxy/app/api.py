"""ESPHome native API server: sessions, device info and message dispatch.

The server answers like an ESPHome Bluetooth proxy without entities. The
protobuf messages and their type numbers come from aioesphomeapi, pinned to
the version Home Assistant uses, so both sides share one definition.
"""

from __future__ import annotations

# api_pb2 builds its message classes at import time, out of pylint's sight.
# pylint: disable=no-member

import asyncio
import dataclasses
import logging
import time
from typing import Any, Callable

from aioesphomeapi import api_pb2
from aioesphomeapi.core import MESSAGE_TYPE_TO_PROTO
from aioesphomeapi.model import BluetoothProxyFeature

from . import __version__
from .noise_transport import HandshakeError, NoiseTransport, ProtocolError
from .proxy import BluetoothProxy

_LOGGER = logging.getLogger(__name__)

# API 1.15 and later make the client ask for DeviceCapabilities; 1.14 keeps
# everything in DeviceInfo.
API_VERSION = (1, 14)
# Home Assistant raises a repair issue for proxies older than its stable BLE
# version (2026.5.1 in Home Assistant 2026.10); report the ESPHome release the
# rest of this repository builds with.
ESPHOME_VERSION = "2026.9.0"
PROJECT_NAME = "aivi.ble-proxy"
# aioesphomeapi only pings while the device is silent, and a proxy is rarely
# silent. So the server pings a client it has not heard from, as ESPHome does,
# and drops it when that stays unanswered.
HANDSHAKE_TIMEOUT = 30.0
PING_AFTER = 60.0
CLOSE_AFTER = 90.0
KEEPALIVE_CHECK = 10.0

FEATURES = (
    BluetoothProxyFeature.PASSIVE_SCAN
    | BluetoothProxyFeature.ACTIVE_CONNECTIONS
    | BluetoothProxyFeature.REMOTE_CACHING
    | BluetoothProxyFeature.CACHE_CLEARING
    | BluetoothProxyFeature.RAW_ADVERTISEMENTS
    | BluetoothProxyFeature.FEATURE_STATE_AND_MODE
)

MESSAGE_TYPES: dict[type, int] = {
    cls: number for number, cls in MESSAGE_TYPE_TO_PROTO.items()
}


@dataclasses.dataclass(frozen=True)
class DeviceIdentity:
    name: str
    friendly_name: str
    mac_address: str
    suggested_area: str = ""

    @property
    def mac_hex(self) -> str:
        """The MAC as ESPHome puts it into the Noise hello and mDNS."""

        return self.mac_address.replace(":", "").lower()


class ApiSession:
    def __init__(
        self,
        server: ApiServer,
        transport: NoiseTransport,
        peer: str,
    ) -> None:
        self._server = server
        self._transport = transport
        self._peer = peer
        self._closed = False
        self._last_received = 0.0
        proxy = server.proxy
        self._handlers: dict[type, Callable[[Any], None]] = {
            api_pb2.HelloRequest: self._hello,
            api_pb2.DisconnectRequest: self._disconnect_request,
            api_pb2.DisconnectResponse: lambda _: self.close(),
            api_pb2.PingRequest: lambda _: self.send(api_pb2.PingResponse()),
            api_pb2.PingResponse: lambda _: None,
            api_pb2.DeviceInfoRequest: lambda _: self.send(
                self._server.device_info()
            ),
            api_pb2.ListEntitiesRequest: lambda _: self.send(
                api_pb2.ListEntitiesDoneResponse()
            ),
            api_pb2.SubscribeBluetoothLEAdvertisementsRequest: (
                self._subscribe_advertisements
            ),
            api_pb2.UnsubscribeBluetoothLEAdvertisementsRequest: lambda _: (
                proxy.unsubscribe_advertisements(self)
            ),
            api_pb2.SubscribeBluetoothConnectionsFreeRequest: lambda _: (
                proxy.subscribe_slots(self)
            ),
            api_pb2.BluetoothScannerSetModeRequest: lambda msg: (
                proxy.set_scanner_mode(msg.mode)
            ),
            api_pb2.BluetoothDeviceRequest: lambda msg: (
                proxy.device_request(self, msg)
            ),
            api_pb2.BluetoothGATTGetServicesRequest: lambda msg: (
                proxy.get_services(self, msg.address)
            ),
            api_pb2.BluetoothGATTReadRequest: lambda msg: proxy.read(self, msg),
            api_pb2.BluetoothGATTReadDescriptorRequest: lambda msg: (
                proxy.read_descriptor(self, msg)
            ),
            api_pb2.BluetoothGATTWriteRequest: lambda msg: proxy.write(
                self, msg
            ),
            api_pb2.BluetoothGATTWriteDescriptorRequest: lambda msg: (
                proxy.write_descriptor(self, msg)
            ),
            api_pb2.BluetoothGATTNotifyRequest: lambda msg: (
                proxy.notify(self, msg)
            ),
        }

    @property
    def backlog(self) -> int:
        return self._transport.write_buffer_size

    def send(self, *messages: Any) -> None:
        if self._closed or self._transport.closing:
            return
        for message in messages:
            self._transport.write_message(
                MESSAGE_TYPES[type(message)], message.SerializeToString()
            )

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._transport.close()

    async def run(self) -> None:
        try:
            await asyncio.wait_for(
                self._transport.handshake(), HANDSHAKE_TIMEOUT
            )
        except HandshakeError as err:
            _LOGGER.warning("Handshake with %s failed: %s", self._peer, err)
            try:
                await self._transport.drain()
            except ConnectionError:
                pass
            return
        except (
            asyncio.IncompleteReadError,
            ConnectionError,
            ProtocolError,
            TimeoutError,
        ) as err:
            _LOGGER.warning("Handshake with %s broke off: %r", self._peer, err)
            return
        _LOGGER.info("API client %s connected", self._peer)
        loop = asyncio.get_running_loop()
        self._last_received = loop.time()
        keepalive = loop.create_task(self._keepalive())
        try:
            while not self._closed:
                msg_type, payload = await self._transport.read_message()
                self._last_received = loop.time()
                self._dispatch(msg_type, payload)
                await self._transport.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except ProtocolError as err:
            _LOGGER.warning("API client %s: %s", self._peer, err)
        finally:
            keepalive.cancel()
        _LOGGER.info("API client %s disconnected", self._peer)

    async def _keepalive(self) -> None:
        # A timer instead of a read timeout: cancelling a read in the middle
        # of a frame would lose the framing.
        loop = asyncio.get_running_loop()
        while not self._closed:
            await asyncio.sleep(KEEPALIVE_CHECK)
            silent = loop.time() - self._last_received
            if silent > CLOSE_AFTER:
                _LOGGER.warning(
                    "API client %s silent for %.0f s; closing",
                    self._peer,
                    silent,
                )
                self.close()
            elif silent > PING_AFTER:
                self.send(api_pb2.PingRequest())

    def _dispatch(self, msg_type: int, payload: bytes) -> None:
        cls = MESSAGE_TYPE_TO_PROTO.get(msg_type)
        handler = self._handlers.get(cls) if cls is not None else None
        if handler is None:
            _LOGGER.debug("Ignoring message type %d", msg_type)
            return
        message = cls()
        message.ParseFromString(payload)
        handler(message)

    def _hello(self, msg: Any) -> None:
        _LOGGER.info(
            "Hello from %s (%s, API %d.%d)",
            self._peer,
            msg.client_info,
            msg.api_version_major,
            msg.api_version_minor,
        )
        self.send(
            api_pb2.HelloResponse(
                api_version_major=API_VERSION[0],
                api_version_minor=API_VERSION[1],
                server_info=f"aivi-ble-proxy {__version__}",
                name=self._server.identity.name,
            )
        )

    def _disconnect_request(self, _msg: Any) -> None:
        self.send(api_pb2.DisconnectResponse())
        self.close()

    def _subscribe_advertisements(self, msg: Any) -> None:
        if not msg.flags & 1:  # BLUETOOTH_PROXY_SUBSCRIPTION_FLAG_RAW
            _LOGGER.warning("%s asked for parsed advertisements", self._peer)
            return
        self._server.proxy.subscribe_advertisements(self)


class ApiServer:
    def __init__(
        self,
        identity: DeviceIdentity,
        psk: bytes,
        proxy: BluetoothProxy,
    ) -> None:
        self.identity = identity
        self.proxy = proxy
        self._psk = psk
        self._server: asyncio.Server | None = None
        self._sessions: set[ApiSession] = set()
        self._compiled = time.strftime("%b %d %Y, %H:%M:%S")

    async def start(self, host: str, port: int) -> int:
        """Listens; returns the port (useful with port 0)."""

        self._server = await asyncio.start_server(self._client, host, port)
        return self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
        for session in list(self._sessions):
            session.close()
        if self._server is not None:
            await self._server.wait_closed()

    def device_info(self) -> Any:
        identity = self.identity
        return api_pb2.DeviceInfoResponse(
            uses_password=False,
            name=identity.name,
            friendly_name=identity.friendly_name,
            mac_address=identity.mac_address,
            bluetooth_mac_address=identity.mac_address,
            esphome_version=ESPHOME_VERSION,
            compilation_time=self._compiled,
            model="AIVI BLE proxy",
            manufacturer="AIVI",
            project_name=PROJECT_NAME,
            project_version=__version__,
            suggested_area=identity.suggested_area,
            bluetooth_proxy_feature_flags=FEATURES,
            api_encryption_supported=True,
        )

    async def _client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        host, port = writer.get_extra_info("peername")[:2]
        peer = f"{host}:{port}"
        transport = NoiseTransport(
            reader,
            writer,
            self._psk,
            self.identity.name,
            self.identity.mac_hex,
        )
        session = ApiSession(self, transport, peer)
        self._sessions.add(session)
        try:
            await session.run()
        finally:
            self._sessions.discard(session)
            self.proxy.release_session(session)
            session.close()
            await transport.wait_closed()
