"""Event framing between satellite and recorder."""

import asyncio
import json

import pytest
from helpers import protocol


def read_all(data: bytes) -> list[protocol.Event | None]:
    async def run() -> list[protocol.Event | None]:
        reader = asyncio.StreamReader(limit=protocol.MAX_HEADER_BYTES)
        reader.feed_data(data)
        reader.feed_eof()
        result: list[protocol.Event | None] = []
        while True:
            item = await protocol.read_event(reader)
            result.append(item)
            if item is None:
                return result

    return asyncio.run(run())


def test_inline_data_and_payload_as_the_satellite_writes_them() -> None:
    raw = protocol.encode_event("audio-chunk", {"rate": 16000}, b"\x01\x02")
    first, end = read_all(raw)
    assert first == protocol.Event("audio-chunk", {"rate": 16000}, b"\x01\x02")
    assert end is None


def test_data_block_updates_the_header_data() -> None:
    block = json.dumps({"rate": 16000, "channels": 1}).encode()
    header = {
        "type": "audio-start",
        "data": {"rate": 8000, "width": 2},
        "data_length": len(block),
    }
    raw = json.dumps(header).encode() + b"\n" + block
    event = read_all(raw)[0]
    assert event.data == {"rate": 16000, "width": 2, "channels": 1}


@pytest.mark.parametrize(
    "raw",
    [
        b'{"type": "x"',
        b"not json\n",
        b'["type"]\n',
        b'{"data": {}}\n',
        b'{"type": "x", "data": [1]}\n',
        b'{"type": "x", "payload_length": true}\n',
        b'{"type": "x", "payload_length": -1}\n',
        b'{"type": "x", "payload_length": 65537}\n',
        b'{"type": "x", "data_length": 4097}\n',
        b'{"type": "x", "payload_length": 4}\nab',
        b'{"type": "x", "data_length": 3}\nabc',
        b'{"type": "x", "' + b"a" * 5000 + b'": 1}\n',
    ],
)
def test_broken_streams_end_the_connection(raw: bytes) -> None:
    with pytest.raises(protocol.ProtocolError):
        read_all(raw)


def test_answers_are_short_ascii_lines_the_firmware_can_read() -> None:
    raw = protocol.encode("take-saved", {"name": "sat_x_1", "seconds": 60.0})
    assert raw == (
        b'{"type":"take-saved","data":{"name":"sat_x_1","seconds":60.0}}\n'
    )
    assert read_all(raw)[0].data == {"name": "sat_x_1", "seconds": 60.0}
    with pytest.raises(ValueError):
        protocol.encode("error", {"code": "x" * 300})
