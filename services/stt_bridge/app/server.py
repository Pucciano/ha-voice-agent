"""Wyoming speech-to-text handler: collect one request, transcribe at the end.

Home Assistant sends `transcribe`, `audio-start`, the `audio-chunk`s while the
user speaks and `audio-stop` after its voice activity detection. The audio is
kept in memory only and sent to the API as one WAV file; the answer is one
`transcript`, or an `error` that Home Assistant logs. The log never shows the
text.
"""

import dataclasses
import logging
import time
import typing
import urllib.parse

import httpx
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.error import Error
from wyoming.event import Event
from wyoming.info import AsrModel, AsrProgram, Attribution, Describe, Info
from wyoming.server import AsyncEventHandler

from . import __version__
from .transcription import (
    TranscriptionApi,
    TranscriptionError,
    transcribe,
    wav_bytes,
)

_LOGGER = logging.getLogger(__name__)

PROGRAM = "stt-bridge"


@dataclasses.dataclass
class Bridge:
    """What all connections share."""

    api: TranscriptionApi
    info: Info
    client: httpx.AsyncClient
    default_language: str | None
    max_seconds: float
    terms: typing.Callable[[], tuple[str, ...]] = lambda: ()


def build_info(api: TranscriptionApi, languages: list[str]) -> Info:
    host = urllib.parse.urlsplit(api.base_url).hostname or api.base_url
    return Info(
        asr=[
            AsrProgram(
                name=PROGRAM,
                attribution=Attribution(
                    name="ha-voice-agent",
                    url="https://github.com/Pucciano/ha-voice-agent",
                ),
                installed=True,
                description=(
                    "Speech to text through an OpenAI-compatible "
                    "transcription API"
                ),
                version=__version__,
                models=[
                    AsrModel(
                        name=api.model,
                        attribution=Attribution(name=host, url=api.base_url),
                        installed=True,
                        description=f"{api.model} at {host}",
                        version=None,
                        languages=list(languages),
                    )
                ],
            )
        ]
    )


class BridgeHandler(AsyncEventHandler):
    """One Wyoming connection, one request."""

    def __init__(self, bridge: Bridge, *args: typing.Any) -> None:
        super().__init__(*args)
        self.bridge = bridge
        self.language: str | None = None
        self.audio_format: tuple[int, int, int] | None = None
        self.audio = bytearray()
        self.truncated = False

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(self.bridge.info.event())
            return True
        if Transcribe.is_type(event.type):
            self.language = Transcribe.from_event(event).language
            return True
        if AudioStart.is_type(event.type):
            start = AudioStart.from_event(event)
            self.audio_format = (start.rate, start.width, start.channels)
            self.audio.clear()
            self.truncated = False
            return True
        if AudioChunk.is_type(event.type):
            return await self._add_chunk(AudioChunk.from_event(event))
        if AudioStop.is_type(event.type):
            await self._finish()
            return False
        return True

    async def _add_chunk(self, chunk: AudioChunk) -> bool:
        audio_format = (chunk.rate, chunk.width, chunk.channels)
        if self.audio_format is None:
            self.audio_format = audio_format
        elif audio_format != self.audio_format:
            _LOGGER.warning("Audio format changed within a request")
            await self.write_event(
                Error(
                    text="Audio format changed within the request",
                    code="audio-format",
                ).event()
            )
            return False
        rate, width, channels = self.audio_format
        max_bytes = int(self.bridge.max_seconds * rate) * width * channels
        room = max_bytes - len(self.audio)
        if len(chunk.audio) > room:
            # Keep the beginning; a command never lasts this long.
            self.truncated = True
            self.audio.extend(chunk.audio[: max(0, room)])
        else:
            self.audio.extend(chunk.audio)
        return True

    async def _finish(self) -> None:
        if self.audio_format is None or not self.audio:
            await self.write_event(Transcript(text="").event())
            return
        rate, width, channels = self.audio_format
        seconds = len(self.audio) / (rate * width * channels)
        language = self.language or self.bridge.default_language
        started = time.perf_counter()
        try:
            text = await transcribe(
                self.bridge.client,
                self.bridge.api,
                wav_bytes(bytes(self.audio), rate, width, channels),
                language,
                self.bridge.terms(),
            )
        except TranscriptionError as err:
            _LOGGER.warning(
                "Transcription of %.1f s failed after %.0f ms: %s",
                seconds,
                (time.perf_counter() - started) * 1000,
                err,
            )
            await self.write_event(
                Error(
                    text=f"Speech to text failed: {err}",
                    code="stt-bridge-upstream",
                ).event()
            )
            return
        _LOGGER.info(
            "Transcribed %.1f s%s in %.0f ms: %d characters",
            seconds,
            " (truncated)" if self.truncated else "",
            (time.perf_counter() - started) * 1000,
            len(text),
        )
        await self.write_event(Transcript(text=text, language=language).event())
