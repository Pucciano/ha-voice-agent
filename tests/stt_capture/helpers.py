"""Fakes and helpers for the stt-capture tests: Home Assistant, speech-to-text."""

import asyncio
import contextlib
import json
import math
import pathlib
import sys
import threading
import time
from array import array
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "services" / "stt_capture"))

# pylint: disable=wrong-import-position
from app import wyoming_frames as frames  # noqa: E402
from app.home_assistant import HomeAssistant, Satellite  # noqa: E402
from app.relay import Config, Relay  # noqa: E402
from app.store import MARKER, SAMPLES_DIR, Store  # noqa: E402

TOKEN = "test-token"
LIVING_ROOM = Satellite(
    "living_room",
    "assist_satellite.living_room_satellite",
    "switch.living_room_capture",
)
KITCHEN = Satellite(
    "kitchen", "assist_satellite.kitchen_satellite", "switch.kitchen_capture"
)
INFO = {
    "asr": [
        {
            "name": "speech-to-phrase",
            "version": "1.4.3",
            "models": [
                {
                    "name": "en_US-rhasspy",
                    "installed": True,
                    "languages": ["en"],
                },
                {"name": "de_DE-zamia", "installed": True, "languages": ["de"]},
            ],
        }
    ]
}


def frame(
    event_type: str, data: dict[str, Any] | None = None, payload: bytes = b""
) -> bytes:
    """Build a frame the way the Wyoming library writes it."""

    header: dict[str, Any] = {"type": event_type, "version": "1.10.2"}
    block = b""
    if data:
        block = json.dumps(data, ensure_ascii=False).encode()
        header["data_length"] = len(block)
    if payload:
        header["payload_length"] = len(payload)
    return json.dumps(header).encode() + b"\n" + block + payload


def audio_start(rate: int = 16000, width: int = 2, channels: int = 1) -> bytes:
    return frame(
        "audio-start", {"rate": rate, "width": width, "channels": channels}
    )


def audio_chunk(
    pcm: bytes, rate: int = 16000, width: int = 2, channels: int = 1
) -> bytes:
    return frame(
        "audio-chunk",
        {"rate": rate, "width": width, "channels": channels},
        pcm,
    )


def tone(seconds: float = 0.2, rate: int = 16000) -> bytes:
    """16-bit mono sine at about -10 dBFS."""

    count = int(seconds * rate)
    return array(
        "h",
        (
            int(10000 * math.sin(2 * math.pi * 440 * i / rate))
            for i in range(count)
        ),
    ).tobytes()


class FakeHomeAssistant:
    """REST /api/states/<entity_id> with scripted answers.

    A state value is one of:
      "listening"            200 with that state
      ("status", 401)        that HTTP status
      ("raw", b"...")        200 with this body
      ("delay", 0.5, value)  wait, then answer with value
    Unknown entities get 404.
    """

    def __init__(self, states: dict[str, Any]) -> None:
        self.states = dict(states)
        self.requests = 0
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silence
                pass

            def do_GET(self) -> None:  # noqa: N802
                with fake._lock:
                    fake.requests += 1
                    entity_id = self.path.rsplit("/", 1)[-1]
                    value = fake.states.get(entity_id)
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    self._answer(401, b'{"message": "Unauthorized"}')
                    return
                if isinstance(value, tuple) and value[0] == "delay":
                    time.sleep(value[1])
                    value = value[2]
                if value is None:
                    self._answer(404, b'{"message": "Entity not found."}')
                elif isinstance(value, tuple) and value[0] == "status":
                    self._answer(value[1], b"{}")
                elif isinstance(value, tuple) and value[0] == "raw":
                    self._answer(200, value[1])
                else:
                    body = {"entity_id": entity_id, "state": value}
                    self._answer(200, json.dumps(body).encode())

            def _answer(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()

    def set(self, **states: Any) -> None:
        with self._lock:
            for key, value in states.items():
                self.states[key.replace("__", ".")] = value

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class FakeUpstream:
    """Speech-to-text service that answers audio-stop as scripted.

    reply: "transcript", "error", "close" (hang up), "none" (stay silent)
    or "raw" (send raw_reply bytes).
    """

    def __init__(
        self,
        reply: str = "transcript",
        text: str = "wie spät ist es",
        delay: float = 0.0,
        raw_reply: bytes = b"",
    ) -> None:
        self.reply = reply
        self.text = text
        self.delay = delay
        self.raw_reply = raw_reply
        self.received: list[bytearray] = []
        self.closed = asyncio.Event()
        self.port = 0
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0, limit=frames.MAX_HEADER_BYTES
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        received = bytearray()
        self.received.append(received)
        try:
            while True:
                item = await frames.read_frame(reader)
                if item is None:
                    break
                received += item.raw
                if item.type == "describe" and self.reply != "raw":
                    writer.write(frame("info", INFO))
                elif item.type == "audio-stop":
                    await asyncio.sleep(self.delay)
                    if self.reply == "close":
                        break
                    if self.reply == "transcript":
                        writer.write(
                            frame(
                                "transcript",
                                {"text": self.text, "language": "de"},
                            )
                        )
                    elif self.reply == "error":
                        writer.write(
                            frame("error", {"text": "failed", "code": "test"})
                        )
                    elif self.reply == "raw":
                        writer.write(self.raw_reply)
                await writer.drain()
        except (frames.FrameError, OSError):
            pass
        finally:
            writer.close()
            self.closed.set()


async def until(predicate: Any, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


@contextlib.asynccontextmanager
async def harness(
    tmp_path: pathlib.Path,
    states: dict[str, Any] | None = None,
    upstream: FakeUpstream | None = None,
    satellites: tuple[Satellite, ...] = (LIVING_ROOM,),
    marker: bool = True,
    min_free_bytes: int = 0,
    **config: Any,
):
    """Relay between a client and a fake upstream, with a fake HA."""

    home_assistant = FakeHomeAssistant(
        states
        if states is not None
        else {
            LIVING_ROOM.satellite_entity: "listening",
            LIVING_ROOM.switch_entity: "on",
        }
    )
    upstream = upstream or FakeUpstream()
    await upstream.start()
    root = tmp_path / "drive"
    (root / SAMPLES_DIR).mkdir(parents=True)
    if marker:
        (root / MARKER).write_text("test\n")
    store = Store(root, min_free_bytes)
    relay = Relay(
        Config(
            upstream_host="127.0.0.1",
            upstream_port=upstream.port,
            home_assistant=HomeAssistant(home_assistant.url, TOKEN),
            satellites=satellites,
            store=store,
            **config,
        )
    )
    server = await asyncio.start_server(
        relay.handle, "127.0.0.1", 0, limit=frames.MAX_HEADER_BYTES
    )
    try:
        yield SimpleNamespace(
            ha=home_assistant,
            upstream=upstream,
            relay=relay,
            store=store,
            port=server.sockets[0].getsockname()[1],
        )
    finally:
        server.close()
        await relay.drain()
        await upstream.stop()
        home_assistant.close()


def samples(store: Store) -> list[pathlib.Path]:
    return sorted(
        path
        for path in store.samples.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    )
