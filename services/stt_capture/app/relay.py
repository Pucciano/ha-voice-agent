"""Relay Wyoming connections and capture speech-to-text requests.

Every connection from Home Assistant gets its own connection to the
speech-to-text service. Two independent pumps copy frames in both directions
byte for byte. Observers look at a decoded copy of each frame after it has
been forwarded; they never delay, change or drop a frame.

Per request (audio-start to transcript or error):
- the audio is buffered in memory, never on disk;
- the capture decision runs in the background (see home_assistant.py) at
  audio-start and, if needed, once more at audio-stop;
- when the request ends, the sample is written only if a decision has
  allowed it by then and the drive is ready. Everything else discards it.
"""

import asyncio
import contextlib
import datetime
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Coroutine

from . import wyoming_frames as frames
from .home_assistant import Decision, HomeAssistant, Satellite, decide
from .store import Sample, Store

_LOGGER = logging.getLogger(__name__)

_CLIENT_EVENTS = frozenset(
    {"transcribe", "audio-start", "audio-chunk", "audio-stop"}
)
_UPSTREAM_EVENTS = frozenset({"transcript", "error", "info"})


@dataclass(frozen=True)
class Config:  # pylint: disable=too-many-instance-attributes
    upstream_host: str
    upstream_port: int
    home_assistant: HomeAssistant | None
    satellites: tuple[Satellite, ...]
    store: Store
    max_seconds: float = 60.0
    max_capture_bytes: int = 32 * 1024 * 1024
    decision_budget: float = 2.0
    connect_timeout: float = 5.0

    @property
    def capture_enabled(self) -> bool:
        return self.home_assistant is not None and bool(self.satellites)


def _stt_info(data: dict[str, Any]) -> dict[str, Any] | None:
    """Program, version and installed models from an `info` event."""

    asr = data.get("asr")
    if not isinstance(asr, list) or not asr or not isinstance(asr[0], dict):
        return None
    program = asr[0]
    models = [
        {"name": model.get("name"), "languages": model.get("languages") or []}
        for model in program.get("models") or []
        if isinstance(model, dict) and model.get("installed", True)
    ]
    return {
        "program": program.get("name"),
        "version": program.get("version"),
        "models": models,
    }


def _stt_for(
    info: dict[str, Any] | None, language: str | None
) -> dict[str, Any] | None:
    """The program plus the models that serve the request's language.

    speech-to-phrase lists every model it knows; one language picks one.
    """

    if info is None:
        return None
    return {
        "program": info["program"],
        "version": info["version"],
        "models": [
            model["name"]
            for model in info["models"]
            if language and language in model["languages"]
        ],
    }


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _final_decision(decisions: list["asyncio.Task[Decision]"]) -> Decision:
    """The first finished, conclusive decision; otherwise a refusal."""

    reason = "decision_pending"
    pending = False
    for task in decisions:
        if not task.done():
            pending = True
            continue
        decision = task.result()
        if not decision.retryable:
            return decision
        reason = decision.reason
    return Decision(False, "decision_pending" if pending else reason)


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()


class _Request:  # pylint: disable=too-many-instance-attributes
    """Audio and decisions of one request on a connection."""

    def __init__(
        self, rate: int, width: int, channels: int, language: str | None
    ) -> None:
        self.started_at = datetime.datetime.now(datetime.UTC)
        self.rate = rate
        self.width = width
        self.channels = channels
        self.language = language
        self.chunks: list[bytes] = []
        self.size = 0
        self.limit = 0
        self.discard_reason: str | None = None
        self.decisions: list[asyncio.Task[Decision]] = []
        self.audio_stopped_at: float | None = None

    @property
    def bytes_per_second(self) -> int:
        return self.rate * self.width * self.channels

    def discard(self, reason: str) -> None:
        if self.discard_reason is None:
            self.discard_reason = reason
        self.chunks.clear()


class Relay:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.stt_info: dict[str, Any] | None = None
        self._background: set[asyncio.Task[Any]] = set()

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        """Run a task that must not be garbage-collected while it runs."""

        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    @property
    def pending_tasks(self) -> int:
        return len(self._background)

    async def drain(self) -> None:
        """Wait for decisions and writes still running (tests, shutdown)."""

        while self._background:
            await asyncio.gather(*self._background, return_exceptions=True)

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await _Connection(self, reader, writer).run()

    def update_stt_info(self, data: dict[str, Any]) -> None:
        info = _stt_info(data)
        if info is not None:
            self.stt_info = info

    async def fetch_stt_info(self, retry_interval: float = 30.0) -> None:
        """Ask speech-to-text for its description until it answers."""

        while self.stt_info is None:
            with contextlib.suppress(
                OSError, asyncio.TimeoutError, frames.FrameError
            ):
                data = await self._describe()
                if data is not None:
                    self.update_stt_info(data)
            if self.stt_info is None:
                await asyncio.sleep(retry_interval)
        _LOGGER.info(
            "Speech-to-text: %s %s, %d models",
            self.stt_info["program"],
            self.stt_info["version"],
            len(self.stt_info["models"]),
        )

    async def _describe(self) -> dict[str, Any] | None:
        timeout = self.config.connect_timeout
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                self.config.upstream_host,
                self.config.upstream_port,
                limit=frames.MAX_HEADER_BYTES,
            ),
            timeout,
        )
        try:
            writer.write(frames.encode("describe"))
            await writer.drain()
            while True:
                frame = await asyncio.wait_for(
                    frames.read_frame(reader), timeout
                )
                if frame is None:
                    return None
                event = frames.decode(frame)
                if event is not None and event.type == "info":
                    return event.data
        finally:
            await _close(writer)

    async def write(self, sample: Sample, duration: float) -> None:
        store = self.config.store
        reason = await asyncio.to_thread(store.unavailable_reason)
        if reason is not None:
            _log_skip(sample.outcome, reason, duration)
            return
        try:
            sample_id = await asyncio.to_thread(store.write, sample)
        except OSError as err:
            _LOGGER.warning(
                "request outcome=%s capture=failed error=%s duration=%.2fs",
                sample.outcome,
                err.__class__.__name__,
                duration,
            )
            return
        _LOGGER.info(
            "request outcome=%s capture=saved sample=%s duration=%.2fs "
            "stt_ms=%s",
            sample.outcome,
            sample_id,
            duration,
            sample.stt_latency_ms,
        )


def _log_skip(outcome: str, reason: str, duration: float) -> None:
    # Never log transcript text: these logs may be kept in dev mode.
    _LOGGER.info(
        "request outcome=%s capture=skipped reason=%s duration=%.2fs",
        outcome,
        reason,
        duration,
    )


class _Connection:
    def __init__(
        self,
        relay: Relay,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.relay = relay
        self.config = relay.config
        self.client_reader = reader
        self.client_writer = writer
        self.language: str | None = None
        self.request: _Request | None = None

    async def run(self) -> None:
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self.config.upstream_host,
                    self.config.upstream_port,
                    limit=frames.MAX_HEADER_BYTES,
                ),
                self.config.connect_timeout,
            )
        except (OSError, asyncio.TimeoutError) as err:
            _LOGGER.warning(
                "Speech-to-text not reachable (%s)", err.__class__.__name__
            )
            await _close(self.client_writer)
            return

        to_upstream = asyncio.create_task(
            self._pump(
                "client",
                self.client_reader,
                upstream_writer,
                self._observe_client,
            )
        )
        to_client = asyncio.create_task(
            self._pump(
                "upstream",
                upstream_reader,
                self.client_writer,
                self._observe_upstream,
            )
        )
        done, pending = await asyncio.wait(
            {to_upstream, to_client}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await _close(upstream_writer)
        await _close(self.client_writer)
        if self.request is not None:
            ended = (
                "downstream_disconnect"
                if to_upstream in done
                else "upstream_disconnect"
            )
            self._finish(ended)

    async def _pump(
        self,
        side: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        observe: Callable[[frames.Frame], None],
    ) -> None:
        while True:
            try:
                frame = await frames.read_frame(reader)
                if frame is None:
                    return
                writer.write(frame.raw)
                await writer.drain()
            except frames.FrameError as err:
                _LOGGER.warning("Invalid frame from %s: %s", side, err)
                return
            except OSError:
                return
            try:
                observe(frame)
            except Exception:  # pylint: disable=broad-exception-caught
                # A capture bug must never break speech recognition.
                _LOGGER.exception("Capture observer failed")

    # Client side: transcribe, audio-start, audio-chunk, audio-stop.

    def _observe_client(self, frame: frames.Frame) -> None:
        if frame.type not in _CLIENT_EVENTS:
            return
        event = frames.decode(frame)
        if event is None:
            if self.request is not None:
                self.request.discard("undecodable_event")
            return
        if event.type == "transcribe":
            language = event.data.get("language")
            self.language = language if isinstance(language, str) else None
        elif event.type == "audio-start":
            self._audio_start(event)
        elif event.type == "audio-chunk":
            self._audio_chunk(event)
        else:
            self._audio_stop()

    def _audio_start(self, event: frames.Event) -> None:
        if self.request is not None:
            _log_skip("restarted", "restarted", self._duration(self.request))
            self.request = None
        rate, width, channels = (
            event.data.get(key) for key in ("rate", "width", "channels")
        )
        if not (
            _positive_int(rate)
            and _positive_int(width)
            and _positive_int(channels)
        ):
            _log_skip("started", "bad_format", 0.0)
            return
        request = _Request(rate, width, channels, self.language)
        request.limit = min(
            self.config.max_capture_bytes,
            int(self.config.max_seconds * request.bytes_per_second),
        )
        self.request = request
        if not self.config.capture_enabled:
            request.discard("capture_disabled")
            return
        request.decisions.append(self.relay.spawn(self._decide()))

    def _audio_chunk(self, event: frames.Event) -> None:
        request = self.request
        if request is None or request.discard_reason is not None:
            return
        chunk_format = tuple(
            event.data.get(key) for key in ("rate", "width", "channels")
        )
        if chunk_format != (request.rate, request.width, request.channels):
            request.discard("format_changed")
            return
        if request.size + len(event.payload) > request.limit:
            request.discard("too_long")
            return
        request.chunks.append(event.payload)
        request.size += len(event.payload)

    def _audio_stop(self) -> None:
        request = self.request
        if request is None:
            return
        request.audio_stopped_at = time.monotonic()
        if request.discard_reason is not None or len(request.decisions) != 1:
            return
        first = request.decisions[0]
        # Exactly one second attempt, while the satellite should still listen.
        if not first.done() or first.result().retryable:
            request.decisions.append(self.relay.spawn(self._decide()))

    def _decide(self) -> Coroutine[Any, Any, Decision]:
        return decide(
            self.config.home_assistant,
            list(self.config.satellites),
            self.config.decision_budget,
        )

    # Upstream side: transcript, error, info.

    def _observe_upstream(self, frame: frames.Frame) -> None:
        if frame.type not in _UPSTREAM_EVENTS:
            return
        event = frames.decode(frame)
        if event is None:
            return
        if event.type == "info":
            self.relay.update_stt_info(event.data)
        elif self.request is None:
            return
        elif event.type == "transcript":
            text = event.data.get("text")
            text = text if isinstance(text, str) else ""
            language = event.data.get("language")
            if isinstance(language, str):
                self.request.language = language
            self._finish("transcript" if text.strip() else "empty", text=text)
        else:
            self._finish(
                "error",
                error={
                    "text": str(event.data.get("text", "")),
                    "code": event.data.get("code"),
                },
            )

    def _duration(self, request: _Request) -> float:
        return request.size / request.bytes_per_second

    def _finish(
        self,
        outcome: str,
        text: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        request, self.request = self.request, None
        if request is None:
            return
        duration = self._duration(request)
        if request.discard_reason is not None:
            _log_skip(outcome, request.discard_reason, duration)
            return
        if not request.size:
            _log_skip(outcome, "no_audio", duration)
            return
        decision = _final_decision(request.decisions)
        if not decision.allowed or decision.satellite is None:
            _log_skip(outcome, decision.reason, duration)
            return

        latency = None
        if request.audio_stopped_at is not None and outcome in (
            "transcript",
            "empty",
            "error",
        ):
            latency = round(
                (time.monotonic() - request.audio_stopped_at) * 1000
            )
        sample = Sample(
            pcm=b"".join(request.chunks),
            rate=request.rate,
            width=request.width,
            channels=request.channels,
            started_at=request.started_at,
            satellite=decision.satellite,
            outcome=outcome,
            transcript=text,
            error=error,
            language=request.language,
            stt_info=_stt_for(self.relay.stt_info, request.language),
            stt_latency_ms=latency,
        )
        request.chunks.clear()
        self.relay.spawn(self.relay.write(sample, duration))
