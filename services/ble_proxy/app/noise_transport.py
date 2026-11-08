"""Noise transport of the ESPHome native API, server side.

ESPHome encrypts the API with Noise_NNpsk0_25519_ChaChaPoly_SHA256 and a
pre-shared key, the `api: encryption: key` of a device. Every frame is the
byte 0x01, a 16-bit big-endian length and the payload:

1. The client sends a hello frame, empty so far, and its handshake frame
   (0x00 + Noise message). The Noise prologue is "NoiseAPIInit" followed
   by the hello frame's length and content.
2. The server answers with its hello (0x01 for the chosen protocol, then
   name and MAC, each ended by a zero byte) and its handshake frame
   (0x00 + Noise message), or with a rejection (0x01 + reason) that ends the
   connection.
3. Afterwards a frame carries the encrypted message type (16 bit), message
   length (16 bit) and protobuf payload.

A plaintext client starts with 0x00 instead of 0x01. It gets the rejection,
which tells aioesphomeapi that the device requires encryption.
"""

from __future__ import annotations

import asyncio

from noise.connection import NoiseConnection

PROTOCOL_NAME = b"Noise_NNpsk0_25519_ChaChaPoly_SHA256"
PROLOGUE_START = b"NoiseAPIInit"
INDICATOR = 0x01
# ChaChaPoly adds a 16-byte tag; the frame length field has 16 bits.
MAX_PAYLOAD = 0xFFFF - 16 - 4


class HandshakeError(Exception):
    """The client did not complete the handshake; the session ends."""


class ProtocolError(Exception):
    """A broken frame after the handshake; the session ends."""


class NoiseTransport:
    """One encrypted API connection on an asyncio stream pair."""

    def __init__(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        psk: bytes,
        name: str,
        mac: str,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._psk = psk
        self._name = name
        self._mac = mac
        self._noise = NoiseConnection.from_name(PROTOCOL_NAME)

    async def handshake(self) -> None:
        indicator = await self._reader.readexactly(1)
        if indicator[0] != INDICATOR:
            self._reject("Bad indicator byte")
            raise HandshakeError("plaintext or unknown client")
        client_hello = await self._read_frame(indicator_read=True)
        self._noise.set_as_responder()
        self._noise.set_psks(self._psk)
        self._noise.set_prologue(
            PROLOGUE_START + len(client_hello).to_bytes(2, "big") + client_hello
        )
        self._noise.start_handshake()
        hello = (
            bytes((INDICATOR,))
            + self._name.encode()
            + b"\x00"
            + self._mac.encode()
            + b"\x00"
        )
        self._write_frame(hello)

        message = await self._read_frame()
        if not message:
            self._reject("Empty handshake message")
            raise HandshakeError("empty handshake message")
        if message[0] != 0x00:
            self._reject("Bad handshake error byte")
            raise HandshakeError("bad handshake error byte")
        try:
            self._noise.read_message(message[1:])
        except Exception as err:
            # aioesphomeapi reports exactly this reason as a wrong key.
            self._reject("Handshake MAC failure")
            raise HandshakeError("wrong key") from err
        self._write_frame(b"\x00" + self._noise.write_message())
        await self._writer.drain()

    async def read_message(self) -> tuple[int, bytes]:
        frame = await self._read_frame()
        try:
            plain = self._noise.decrypt(frame)
        except Exception as err:
            raise ProtocolError("frame does not decrypt") from err
        if len(plain) < 4:
            raise ProtocolError("message too short")
        return (plain[0] << 8) | plain[1], bytes(plain[4:])

    def write_message(self, msg_type: int, payload: bytes) -> None:
        if len(payload) > MAX_PAYLOAD:
            raise ValueError(f"message {msg_type} too long: {len(payload)}")
        header = bytes(
            (
                (msg_type >> 8) & 0xFF,
                msg_type & 0xFF,
                (len(payload) >> 8) & 0xFF,
                len(payload) & 0xFF,
            )
        )
        self._write_frame(self._noise.encrypt(header + payload))

    @property
    def closing(self) -> bool:
        return self._writer.transport.is_closing()

    @property
    def write_buffer_size(self) -> int:
        return self._writer.transport.get_write_buffer_size()

    async def drain(self) -> None:
        await self._writer.drain()

    def close(self) -> None:
        self._writer.close()

    async def wait_closed(self) -> None:
        try:
            await self._writer.wait_closed()
        except OSError:
            pass

    async def _read_frame(self, indicator_read: bool = False) -> bytes:
        if not indicator_read:
            indicator = await self._reader.readexactly(1)
            if indicator[0] != INDICATOR:
                raise ProtocolError(f"bad indicator byte {indicator[0]}")
        size = await self._reader.readexactly(2)
        return await self._reader.readexactly((size[0] << 8) | size[1])

    def _write_frame(self, payload: bytes) -> None:
        size = len(payload)
        self._writer.write(bytes((INDICATOR, size >> 8, size & 0xFF)))
        self._writer.write(payload)

    def _reject(self, reason: str) -> None:
        self._write_frame(bytes((INDICATOR,)) + reason.encode())
