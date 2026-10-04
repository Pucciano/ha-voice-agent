"""Accept takes from the satellites and write them with the Store.

One connection carries one take (see protocol.py). The first event must be
`take-start` within a few seconds, and nothing is written before the token,
the satellite and the request have been checked. A satellite has at most one
take: a new `take-start` ends a stale one, for example after the satellite
lost its connection without a clean end.

Logs never contain speaker names; they may be kept in dev log mode.
"""

import asyncio
import contextlib
import datetime
import hmac
import logging
import re
from dataclasses import dataclass
from typing import Any

from . import protocol
from .store import CHANNELS, MIN_SECONDS, RATE, WIDTH, Store, Take, TakeInfo

_LOGGER = logging.getLogger(__name__)

BYTES_PER_SECOND = RATE * WIDTH * CHANNELS
KINDS = ("positive", "negative")
# Lower case, no separator at either end and none twice in a row, so a name
# never contains the "__" that separates the parts of an imported clip name.
SPEAKER = re.compile(r"[a-z0-9]+(?:[_-][a-z0-9]+)*")
MAX_SPEAKER = 24
DETECTION_NAME = re.compile(r"[A-Za-z0-9 _.-]{1,32}")
# Reasons a satellite may give in audio-stop.
STOP_REASONS = frozenset(
    {"completed", "stopped", "muted", "overflow", "ota", "error"}
)


class TakeRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Config:  # pylint: disable=too-many-instance-attributes
    store: Store
    token: str
    satellites: frozenset[str]
    max_seconds: float = 7200.0
    start_timeout: float = 5.0
    idle_timeout: float = 10.0
    answer_timeout: float = 5.0
    max_connections: int = 4
    # Audio is written in blocks of about this size, in a thread.
    flush_bytes: int = BYTES_PER_SECOND
    # A take may run this much longer than requested before it is cut.
    overrun_s: float = 5.0
    # Free space is checked again after this much audio.
    space_check_s: float = 60.0


def _now() -> datetime.datetime:
    return datetime.datetime.now().astimezone()


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(OSError, asyncio.TimeoutError):
        await asyncio.wait_for(writer.wait_closed(), 1.0)


class Recorder:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.active: dict[str, "_Session"] = {}
        self._connections = 0

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self._connections >= self.config.max_connections:
            await _close(writer)
            return
        self._connections += 1
        try:
            await _Session(self, reader, writer).run()
        finally:
            self._connections -= 1

    async def end_stale_take(self, satellite_id: str) -> None:
        """End the running take of a satellite that starts a new one."""

        old = self.active.get(satellite_id)
        if old is None:
            return
        old.superseded = True
        old.writer.close()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(old.done.wait(), 5.0)

    async def recover(self) -> None:
        """Finish leftovers of an interrupted process, if the drive is ready."""

        store = self.config.store
        if await asyncio.to_thread(store.unavailable_reason) is not None:
            return
        recovered, removed = await asyncio.to_thread(store.recover)
        if recovered or removed:
            _LOGGER.info(
                "Unfinished takes: %d recovered, %d too short and removed",
                recovered,
                removed,
            )


class _Session:  # pylint: disable=too-many-instance-attributes
    def __init__(
        self,
        recorder: Recorder,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.recorder = recorder
        self.config = recorder.config
        self.reader = reader
        self.writer = writer
        self.superseded = False
        self.done = asyncio.Event()
        self.take: Take | None = None
        self._buffer = bytearray()
        self._next_space_check = self.config.space_check_s

    async def run(self) -> None:
        satellite_id = None
        try:
            try:
                event = await asyncio.wait_for(
                    protocol.read_event(self.reader), self.config.start_timeout
                )
            except (asyncio.TimeoutError, protocol.ProtocolError, OSError):
                return
            if event is None:
                return
            if event.type != "take-start":
                await self._answer("error", {"code": "protocol"})
                return
            try:
                info = self._take_info(event.data)
            except TakeRefused as err:
                _LOGGER.info("take refused reason=%s", err.code)
                await self._answer("error", {"code": err.code})
                return
            satellite_id = info.satellite_id
            await self.recorder.end_stale_take(satellite_id)
            self.recorder.active[satellite_id] = self
            await self._record(info)
        finally:
            if (
                satellite_id is not None
                and self.recorder.active.get(satellite_id) is self
            ):
                del self.recorder.active[satellite_id]
            self.done.set()
            await _close(self.writer)

    def _take_info(self, data: dict[str, Any]) -> TakeInfo:
        config = self.config
        if not config.token:
            raise TakeRefused("disabled")
        token = data.get("token")
        if not isinstance(token, str) or not hmac.compare_digest(
            token.encode(), config.token.encode()
        ):
            raise TakeRefused("unauthorized")
        satellite_id = data.get("satellite")
        if (
            not isinstance(satellite_id, str)
            or satellite_id not in config.satellites
        ):
            raise TakeRefused("unknown_satellite")
        if data.get("version") != 1:
            raise TakeRefused("bad_version")
        audio_format = (
            data.get("rate"),
            data.get("width"),
            data.get("channels"),
        )
        if audio_format != (RATE, WIDTH, CHANNELS):
            raise TakeRefused("bad_format")
        kind = data.get("kind")
        if kind not in KINDS:
            raise TakeRefused("bad_request")
        speaker = None
        if kind == "positive":
            speaker = data.get("speaker")
            if (
                not isinstance(speaker, str)
                or len(speaker) > MAX_SPEAKER
                or not SPEAKER.fullmatch(speaker)
            ):
                raise TakeRefused("bad_speaker")
        seconds = data.get("seconds")
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not 0 < seconds <= config.max_seconds
        ):
            raise TakeRefused("bad_duration")
        return TakeInfo(satellite_id, kind, speaker, float(seconds), _now())

    async def _record(self, info: TakeInfo) -> None:
        store = self.config.store
        if len(self.recorder.active) == 1:
            # No other take is open, so leftovers can be finished safely,
            # for example after the drive was pulled during a take.
            await self.recorder.recover()
        reason = await asyncio.to_thread(store.unavailable_reason)
        if reason is not None:
            code = "low_space" if reason == "low_space" else "drive"
            _LOGGER.info("take refused reason=%s", reason)
            await self._answer("error", {"code": code})
            return
        try:
            self.take = await asyncio.to_thread(store.open_take, info)
        except OSError as err:
            _LOGGER.warning(
                "take refused reason=write_failed error=%s",
                err.__class__.__name__,
            )
            await self._answer("error", {"code": "write_failed"})
            return
        _LOGGER.info(
            "take started name=%s kind=%s requested=%.0fs",
            self.take.stem,
            info.kind,
            info.requested_s,
        )
        await self._answer("take-accepted", {"name": self.take.stem})
        end_reason, answer = await self._receive(info)
        await self._finish(end_reason, answer)

    async def _receive(self, info: TakeInfo) -> tuple[str, bool]:
        """Read the take; returns (end_reason, satellite still listens)."""

        limit_seconds = info.requested_s + self.config.overrun_s
        limit_bytes = int(limit_seconds * RATE) * WIDTH * CHANNELS
        while True:
            try:
                event = await asyncio.wait_for(
                    protocol.read_event(self.reader), self.config.idle_timeout
                )
            except asyncio.TimeoutError:
                return "timeout", False
            except (protocol.ProtocolError, OSError):
                event = None
            if self.superseded:
                return "superseded", False
            if event is None:
                return "disconnected", False
            end_reason = await self._handle(event, limit_bytes)
            if end_reason is not None:
                return end_reason, True

    async def _handle(
        self, event: protocol.Event, limit_bytes: int
    ) -> str | None:
        """Process one event; returns the end reason once the take ends."""

        assert self.take is not None
        if event.type == "audio-chunk":
            if not _chunk_ok(event):
                return "protocol"
            written = self.take.data_bytes + len(self._buffer)
            room = max(0, limit_bytes - written)
            self._buffer += event.payload[:room]
            if len(event.payload) > room:
                return "too_long"
            if len(self._buffer) >= self.config.flush_bytes:
                await self._flush()
                if await self._space_low():
                    return "low_space"
        elif event.type == "detection":
            await self._detection(event.data)
        elif event.type == "audio-stop":
            dropped = event.data.get("dropped_bytes")
            if isinstance(dropped, int) and not isinstance(dropped, bool):
                self.take.dropped_bytes = max(0, dropped)
            reason = event.data.get("reason")
            return reason if reason in STOP_REASONS else "error"
        # Unknown events are ignored, so newer satellites keep working.
        return None

    async def _flush(self) -> None:
        if not self._buffer or self.take is None:
            return
        pcm = bytes(self._buffer)
        self._buffer.clear()
        await asyncio.to_thread(self.take.write, pcm)

    async def _space_low(self) -> bool:
        assert self.take is not None
        if self.take.duration_s < self._next_space_check:
            return False
        self._next_space_check += self.config.space_check_s
        reason = await asyncio.to_thread(self.config.store.unavailable_reason)
        return reason is not None

    async def _detection(self, data: dict[str, Any]) -> None:
        assert self.take is not None
        name = data.get("name")
        timestamp = data.get("timestamp")
        if (
            not isinstance(name, str)
            or not DETECTION_NAME.fullmatch(name)
            or isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or timestamp < 0
        ):
            return
        await asyncio.to_thread(self.take.add_detection, name, timestamp / 1000)

    async def _finish(self, end_reason: str, answer: bool) -> None:
        take = self.take
        assert take is not None
        try:
            await self._flush()
            if take.duration_s < MIN_SECONDS:
                await asyncio.to_thread(take.discard)
                _LOGGER.info(
                    "take discarded name=%s seconds=%.1f end=%s",
                    take.stem,
                    take.duration_s,
                    end_reason,
                )
                if answer:
                    await self._answer("error", {"code": "too_short"})
                return
            await asyncio.to_thread(take.finalize, end_reason, _now())
        except OSError as err:
            # The .part files stay for recovery if the drive comes back.
            _LOGGER.warning(
                "take failed name=%s error=%s",
                take.stem,
                err.__class__.__name__,
            )
            if answer:
                await self._answer("error", {"code": "write_failed"})
            return
        _LOGGER.info(
            "take saved name=%s kind=%s seconds=%.1f end=%s detections=%d "
            "dropped=%d",
            take.stem,
            take.info.kind,
            take.duration_s,
            end_reason,
            len(take.detections),
            take.dropped_bytes,
        )
        if answer:
            await self._answer(
                "take-saved",
                {
                    "name": take.stem,
                    "seconds": round(take.duration_s, 1),
                    "end": end_reason,
                },
            )

    async def _answer(self, event_type: str, data: dict[str, Any]) -> None:
        try:
            self.writer.write(protocol.encode(event_type, data))
            await asyncio.wait_for(
                self.writer.drain(), self.config.answer_timeout
            )
        except (OSError, asyncio.TimeoutError):
            pass


def _chunk_ok(event: protocol.Event) -> bool:
    data = event.data
    audio_format = (data.get("rate"), data.get("width"), data.get("channels"))
    return (
        audio_format == (RATE, WIDTH, CHANNELS)
        and len(event.payload) % (WIDTH * CHANNELS) == 0
    )
