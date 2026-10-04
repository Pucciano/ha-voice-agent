"""Wyoming framing between a satellite and the recorder.

An event is one JSON header line, optionally followed by `data_length` bytes
of JSON data and `payload_length` bytes of payload, as in the Wyoming
protocol. The satellite writes its data inline in the header; a data block is
accepted as well.

The limits are much tighter than Wyoming's because a satellite sends small
frames and nothing is read before the take is authorised. Answers to the
satellite are compact ASCII lines with inline data and short values, so the
firmware can read them without a JSON library.

A take, satellite to recorder unless marked otherwise:

    take-start   {satellite, kind, speaker, seconds, token,
                  rate, width, channels, version}
    <-           take-accepted {name} | error {code}
    audio-chunk  {rate, width, channels} + 16-bit PCM payload
    detection    {name, timestamp}  ms since the take started
    audio-stop   {reason, dropped_bytes}
    <-           take-saved {name, seconds} | error {code}
"""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

MAX_HEADER_BYTES = 4096
MAX_DATA_BYTES = 4096
MAX_PAYLOAD_BYTES = 64 * 1024
# The firmware reads answers into a fixed buffer of this size.
MAX_ANSWER_BYTES = 256


class ProtocolError(Exception):
    """The stream holds no valid event; the connection must end."""


@dataclass(frozen=True)
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    payload: bytes = b""


def _length(header: dict[str, Any], key: str, limit: int) -> int:
    value = header.get(key)
    if value is None:
        return 0
    # bool is an int subclass; a JSON true is not a length.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"{key} is not an integer")
    if value < 0 or value > limit:
        raise ProtocolError(f"{key} {value} out of range")
    return value


async def read_event(reader: asyncio.StreamReader) -> Event | None:
    """Read one event; None on a clean end of stream between events.

    The reader's limit must be MAX_HEADER_BYTES, so a longer header line
    fails before it is buffered.
    """

    try:
        line = await reader.readuntil(b"\n")
    except asyncio.IncompleteReadError as err:
        if not err.partial:
            return None
        raise ProtocolError("stream ended inside a header") from err
    except asyncio.LimitOverrunError as err:
        raise ProtocolError("header line too long") from err

    try:
        header = json.loads(line)
    except ValueError as err:
        raise ProtocolError("header is not JSON") from err
    if not isinstance(header, dict) or not isinstance(header.get("type"), str):
        raise ProtocolError("header has no event type")
    data = header.get("data") or {}
    if not isinstance(data, dict):
        raise ProtocolError("data is not an object")

    data_length = _length(header, "data_length", MAX_DATA_BYTES)
    payload_length = _length(header, "payload_length", MAX_PAYLOAD_BYTES)
    try:
        block = await reader.readexactly(data_length)
        payload = await reader.readexactly(payload_length)
    except asyncio.IncompleteReadError as err:
        raise ProtocolError("stream ended inside an event") from err

    if block:
        try:
            extra = json.loads(block)
        except ValueError as err:
            raise ProtocolError("data block is not JSON") from err
        if not isinstance(extra, dict):
            raise ProtocolError("data block is not an object")
        # As in wyoming.event.async_read_event: the block updates the header.
        data = {**data, **extra}
    return Event(type=header["type"], data=data, payload=payload)


def encode(event_type: str, data: dict[str, Any] | None = None) -> bytes:
    """One answer line with inline data, as the firmware expects it."""

    event: dict[str, Any] = {"type": event_type}
    if data:
        event["data"] = data
    line = json.dumps(event, separators=(",", ":"), ensure_ascii=True)
    raw = line.encode("ascii") + b"\n"
    if len(raw) > MAX_ANSWER_BYTES:
        raise ValueError(f"answer longer than {MAX_ANSWER_BYTES} bytes")
    return raw


def encode_event(
    event_type: str, data: dict[str, Any] | None = None, payload: bytes = b""
) -> bytes:
    """A frame as the satellite writes it; used by tests and trial runs."""

    event: dict[str, Any] = {"type": event_type}
    if data:
        event["data"] = data
    if payload:
        event["payload_length"] = len(payload)
    line = json.dumps(event, separators=(",", ":"), ensure_ascii=True)
    return line.encode("ascii") + b"\n" + payload
