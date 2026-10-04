"""Read Wyoming frames for a byte-transparent relay.

A Wyoming event is one JSON header line, optionally followed by
`data_length` bytes of JSON data and `payload_length` bytes of payload. The
relay forwards the original bytes of every frame unchanged and decodes a copy
only to observe the event. Nothing is serialised again, so unknown or newer
events pass through as they are.

A frame that breaks these rules ends the connection: after it there is no
safe frame boundary left to continue from.
"""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

MAX_HEADER_BYTES = 1024 * 1024
MAX_DATA_BYTES = 16 * 1024 * 1024
MAX_PAYLOAD_BYTES = 16 * 1024 * 1024


class FrameError(Exception):
    """The stream holds no valid frame; the connection must end."""


@dataclass(frozen=True)
class Frame:
    """One frame: its original bytes and the parsed header line."""

    raw: bytes
    header: dict[str, Any]
    data_length: int
    payload_length: int

    @property
    def type(self) -> str:
        return self.header["type"]

    @property
    def data_block(self) -> bytes:
        start = len(self.raw) - self.payload_length - self.data_length
        return self.raw[start : start + self.data_length]

    @property
    def payload(self) -> bytes:
        if not self.payload_length:
            return b""
        return self.raw[-self.payload_length :]


@dataclass(frozen=True)
class Event:
    """Decoded copy of a frame, for observation only."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)
    payload: bytes = b""


def _length(header: dict[str, Any], key: str, limit: int) -> int:
    value = header.get(key)
    if value is None:
        return 0
    # bool is an int subclass; a JSON true is not a length.
    if isinstance(value, bool) or not isinstance(value, int):
        raise FrameError(f"{key} is not an integer")
    if value < 0 or value > limit:
        raise FrameError(f"{key} {value} out of range")
    return value


async def read_frame(reader: asyncio.StreamReader) -> Frame | None:
    """Read one frame; None on a clean end of stream between frames.

    The reader's limit must be at least MAX_HEADER_BYTES, otherwise long
    headers are refused earlier.
    """

    try:
        line = await reader.readuntil(b"\n")
    except asyncio.IncompleteReadError as err:
        if not err.partial:
            return None
        raise FrameError("stream ended inside a header") from err
    except asyncio.LimitOverrunError as err:
        raise FrameError("header line too long") from err
    if len(line) > MAX_HEADER_BYTES:
        raise FrameError("header line too long")

    try:
        header = json.loads(line)
    except ValueError as err:
        raise FrameError("header is not JSON") from err
    if not isinstance(header, dict) or not isinstance(header.get("type"), str):
        raise FrameError("header has no event type")

    data_length = _length(header, "data_length", MAX_DATA_BYTES)
    payload_length = _length(header, "payload_length", MAX_PAYLOAD_BYTES)
    try:
        rest = await reader.readexactly(data_length + payload_length)
    except asyncio.IncompleteReadError as err:
        raise FrameError("stream ended inside a frame") from err

    return Frame(
        raw=line + rest,
        header=header,
        data_length=data_length,
        payload_length=payload_length,
    )


def decode(frame: Frame) -> Event | None:
    """Decode a frame like the Wyoming library does; None if unreadable.

    Fields in the data block update the header's `data`, as in
    wyoming.event.async_read_event.
    """

    data = frame.header.get("data") or {}
    if not isinstance(data, dict):
        return None
    data = dict(data)
    if frame.data_length:
        try:
            block = json.loads(frame.data_block)
        except ValueError:
            return None
        if not isinstance(block, dict):
            return None
        data.update(block)
    return Event(type=frame.type, data=data, payload=frame.payload)


def encode(event_type: str, data: dict[str, Any] | None = None) -> bytes:
    """Build a frame; used only for the relay's own describe request."""

    header: dict[str, Any] = {"type": event_type}
    block = b""
    if data:
        block = json.dumps(data, ensure_ascii=False).encode("utf-8")
        header["data_length"] = len(block)
    return json.dumps(header).encode("utf-8") + b"\n" + block
