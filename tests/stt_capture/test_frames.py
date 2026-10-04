"""Frame reading for the byte-transparent relay."""

import asyncio
import json

import pytest
from helpers import frame, frames


def read_all(data: bytes) -> list[frames.Frame | None]:
    """Read frames from data until the clean end of stream (None)."""

    async def run() -> list[frames.Frame | None]:
        reader = asyncio.StreamReader(limit=frames.MAX_HEADER_BYTES)
        reader.feed_data(data)
        reader.feed_eof()
        result: list[frames.Frame | None] = []
        while True:
            item = await frames.read_frame(reader)
            result.append(item)
            if item is None:
                return result

    return asyncio.run(run())


def test_frames_keep_their_original_bytes() -> None:
    first = b'{"type": "describe"}\n'
    second = frame("audio-chunk", {"rate": 16000}, b"\x01\x02\x03\x04")
    result = read_all(first + second)
    assert [item.raw for item in result[:-1]] == [first, second]
    assert result[-1] is None
    assert result[1].payload == b"\x01\x02\x03\x04"


def test_decode_merges_header_data_with_data_block() -> None:
    block = json.dumps({"rate": 16000, "channels": 1}).encode()
    raw = (
        json.dumps(
            {
                "type": "audio-start",
                "data": {"rate": 8000, "width": 2},
                "data_length": len(block),
            }
        ).encode()
        + b"\n"
        + block
    )
    item = read_all(raw)[0]
    assert item.raw == raw
    event = frames.decode(item)
    assert event.data == {"rate": 16000, "width": 2, "channels": 1}


def test_decode_returns_none_for_unreadable_data() -> None:
    raw = b'{"type": "x", "data_length": 3}\nabc'
    item = read_all(raw)[0]
    assert item.raw == raw
    assert frames.decode(item) is None


@pytest.mark.parametrize(
    "raw",
    [
        b"not json\n",
        b'["type"]\n',
        b'{"data": {}}\n',
        b'{"type": "x", "payload_length": -1}\n',
        b'{"type": "x", "payload_length": true}\n',
        b'{"type": "x", "data_length": "3"}\n',
        b'{"type": "x", "payload_length": 99999999999}\n',
        b'{"type": "audio-chunk"',
        b'{"type": "audio-chunk", "payload_length": 10}\nshort',
    ],
)
def test_invalid_frames_end_the_stream(raw: bytes) -> None:
    with pytest.raises(frames.FrameError):
        read_all(raw)


def test_header_longer_than_the_limit_is_refused() -> None:
    with pytest.raises(frames.FrameError):
        read_all(b'{"type": "' + b"x" * frames.MAX_HEADER_BYTES + b'"}\n')


def test_encode_builds_a_readable_frame() -> None:
    item = read_all(frames.encode("describe"))[0]
    assert frames.decode(item).type == "describe"
