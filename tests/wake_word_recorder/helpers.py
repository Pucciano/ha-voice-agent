"""A recorder on a free port and a fake satellite for the recorder tests."""

import asyncio
import contextlib
import json
import math
import pathlib
import sys
import time
from array import array
from typing import Any, AsyncIterator, Callable

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "services" / "wake_word_recorder"))

# pylint: disable=wrong-import-position
from app import protocol  # noqa: E402
from app.server import Config, Recorder  # noqa: E402
from app.store import MARKER, TAKES_DIR, Store  # noqa: E402

TOKEN = "test-token"
SATELLITES = frozenset({"living_room", "kitchen"})


def make_store(root: pathlib.Path, min_free_bytes: int = 0) -> Store:
    """A store on a prepared drive: marker and takes directory exist."""

    (root / MARKER).write_text("test drive\n")
    (root / TAKES_DIR).mkdir()
    return Store(root, min_free_bytes)


def tone(seconds: float = 1.0, rate: int = 16000) -> bytes:
    """16-bit mono sine at about -10 dBFS."""

    count = int(seconds * rate)
    return array(
        "h",
        (
            int(10000 * math.sin(2 * math.pi * 440 * i / rate))
            for i in range(count)
        ),
    ).tobytes()


def take_start(**changes: Any) -> bytes:
    data: dict[str, Any] = {
        "satellite": "living_room",
        "kind": "positive",
        "speaker": "alex",
        "seconds": 60,
        "token": TOKEN,
        "rate": 16000,
        "width": 2,
        "channels": 1,
        "version": 1,
    }
    data.update(changes)
    return protocol.encode_event("take-start", data)


def chunk(pcm: bytes) -> bytes:
    return protocol.encode_event(
        "audio-chunk", {"rate": 16000, "width": 2, "channels": 1}, pcm
    )


def chunks(pcm: bytes, size: int = 2048) -> bytes:
    """The PCM in frames of `size` bytes, as the firmware sends it."""

    return b"".join(
        chunk(pcm[start : start + size]) for start in range(0, len(pcm), size)
    )


def detection(name: str, timestamp: int) -> bytes:
    return protocol.encode_event(
        "detection", {"name": name, "timestamp": timestamp}
    )


def audio_stop(reason: str = "completed", dropped_bytes: int = 0) -> bytes:
    return protocol.encode_event(
        "audio-stop", {"reason": reason, "dropped_bytes": dropped_bytes}
    )


@contextlib.asynccontextmanager
async def running(store: Store, **changes: Any) -> AsyncIterator[int]:
    """A recorder for `store` on 127.0.0.1; yields its port."""

    settings: dict[str, Any] = {"token": TOKEN, "satellites": SATELLITES}
    settings.update(changes)
    recorder = Recorder(Config(store=store, **settings))
    server = await asyncio.start_server(
        recorder.handle, "127.0.0.1", 0, limit=protocol.MAX_HEADER_BYTES
    )
    port = server.sockets[0].getsockname()[1]
    async with server:
        yield port


class Satellite:
    """One connection of a fake satellite."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None

    async def __aenter__(self) -> "Satellite":
        self.reader, self.writer = await asyncio.open_connection(
            "127.0.0.1", self.port
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def send(self, *frames: bytes) -> None:
        assert self.writer is not None
        self.writer.write(b"".join(frames))
        await self.writer.drain()

    async def answer(self, timeout: float = 2.0) -> dict[str, Any] | None:
        """The next answer line; None once the recorder has closed."""

        assert self.reader is not None
        try:
            line = await asyncio.wait_for(self.reader.readline(), timeout)
        except ConnectionResetError:
            # Closed with unread data on our side, which TCP reports as reset.
            return None
        if not line:
            return None
        assert len(line) <= protocol.MAX_ANSWER_BYTES
        line.decode("ascii")
        return json.loads(line)

    async def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
            with contextlib.suppress(OSError):
                await self.writer.wait_closed()
            self.writer = None


async def until(check: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.02)


def files(store: Store) -> list[str]:
    """All files below recordings/, relative, sorted."""

    if not store.recordings.is_dir():
        return []
    return sorted(
        str(path.relative_to(store.recordings))
        for path in store.recordings.rglob("*")
        if path.is_file()
    )


def sidecar(store: Store, relative: str) -> dict[str, Any]:
    return json.loads((store.recordings / relative).read_text())
