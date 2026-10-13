"""Fakes for the stt-bridge tests: transcription API, Wyoming client."""

import asyncio
import contextlib
import functools
import pathlib
import sys
from collections.abc import AsyncIterator, Callable

import httpx

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "services" / "stt_bridge"))

# pylint: disable=wrong-import-position
from wyoming.client import AsyncTcpClient  # noqa: E402
from wyoming.server import AsyncServer  # noqa: E402

from app.server import Bridge, BridgeHandler, build_info  # noqa: E402
from app.transcription import TranscriptionApi  # noqa: E402

API_KEY = "stt-secret"
API = TranscriptionApi(
    base_url="https://stt.example/v1",
    api_key=API_KEY,
    model="voxtral-test",
)


class FakeTranscriptionApi:
    """Records requests; answers with a fixed text or a given response."""

    def __init__(
        self, answer: Callable[[httpx.Request], httpx.Response] | None = None
    ) -> None:
        self.requests: list[httpx.Request] = []
        self._answer = answer or (
            lambda _: httpx.Response(200, json={"text": " Wie spät ist es? "})
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._answer(request)


def free_port() -> int:
    import socket  # pylint: disable=import-outside-toplevel

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.asynccontextmanager
async def bridge_server(
    fake: FakeTranscriptionApi,
    terms: tuple[str, ...] = (),
    max_seconds: float = 30.0,
) -> AsyncIterator[int]:
    """Run the bridge on a free port; yields the port."""

    port = free_port()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(fake.handler)
    ) as client:
        bridge = Bridge(
            api=API,
            info=build_info(API, ["de"]),
            client=client,
            default_language="de",
            max_seconds=max_seconds,
            terms=lambda: terms,
        )
        server = AsyncServer.from_uri(f"tcp://127.0.0.1:{port}")
        task = asyncio.create_task(
            server.run(functools.partial(BridgeHandler, bridge))
        )
        await asyncio.sleep(0.05)
        try:
            yield port
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def client(port: int) -> AsyncTcpClient:
    return AsyncTcpClient("127.0.0.1", port)


def silence(seconds: float, rate: int = 16000) -> bytes:
    return bytes(int(seconds * rate) * 2)
