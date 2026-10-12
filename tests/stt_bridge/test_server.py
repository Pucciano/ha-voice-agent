"""The bridge as Home Assistant's Wyoming client sees it."""

import asyncio
import io
import wave

import httpx
from helpers import (
    API_KEY,
    FakeTranscriptionApi,
    bridge_server,
    client,
    silence,
)
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.error import Error
from wyoming.info import Describe, Info

RATE, WIDTH, CHANNELS = 16000, 2, 1


async def _request(port, audio_chunks, language="de"):
    async with client(port) as wyoming:
        await wyoming.write_event(Transcribe(language=language).event())
        await wyoming.write_event(
            AudioStart(rate=RATE, width=WIDTH, channels=CHANNELS).event()
        )
        for audio in audio_chunks:
            await wyoming.write_event(
                AudioChunk(
                    rate=RATE, width=WIDTH, channels=CHANNELS, audio=audio
                ).event()
            )
        await wyoming.write_event(AudioStop().event())
        return await asyncio.wait_for(wyoming.read_event(), 5)


def _wav_of(request: httpx.Request) -> wave.Wave_read:
    body = request.content
    start = body.index(b"RIFF")
    return wave.open(io.BytesIO(body[start:]))


def test_describe_announces_the_model_and_language():
    async def scenario():
        async with bridge_server(FakeTranscriptionApi()) as port:
            async with client(port) as wyoming:
                await wyoming.write_event(Describe().event())
                return Info.from_event(await wyoming.read_event())

    info = asyncio.run(scenario())

    program = info.asr[0]
    assert program.name == "stt-bridge"
    assert program.models[0].name == "voxtral-test"
    assert program.models[0].languages == ["de"]


def test_request_becomes_one_transcription_with_wav_language_and_terms():
    fake = FakeTranscriptionApi()

    async def scenario():
        async with bridge_server(
            fake, terms=("Stehlampe", "Großer Kellerraum")
        ) as port:
            return await _request(port, [silence(0.5), silence(0.5)])

    event = asyncio.run(scenario())

    assert Transcript.from_event(event).text == "Wie spät ist es?"
    request = fake.requests[0]
    assert str(request.url) == "https://stt.example/v1/audio/transcriptions"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    body = request.content
    assert b'name="model"\r\n\r\nvoxtral-test' in body
    assert b'name="language"\r\n\r\nde' in body
    assert body.count(b'name="context_bias"') == 3
    with _wav_of(request) as wav:
        assert wav.getframerate() == RATE
        assert wav.getnframes() == RATE


def test_upstream_failure_is_a_wyoming_error_without_the_key():
    fake = FakeTranscriptionApi(
        lambda _: httpx.Response(401, json={"message": "Unauthorized"})
    )

    async def scenario():
        async with bridge_server(fake) as port:
            return await _request(port, [silence(0.5)])

    event = asyncio.run(scenario())

    assert Error.is_type(event.type)
    error = Error.from_event(event)
    assert "401" in error.text
    assert API_KEY not in error.text


def test_request_without_audio_gives_an_empty_transcript():
    fake = FakeTranscriptionApi()

    async def scenario():
        async with bridge_server(fake) as port:
            return await _request(port, [])

    event = asyncio.run(scenario())

    assert Transcript.from_event(event).text == ""
    assert fake.requests == []


def test_long_request_is_cut_to_its_beginning():
    fake = FakeTranscriptionApi()

    async def scenario():
        async with bridge_server(fake, max_seconds=1.0) as port:
            return await _request(port, [silence(0.8), silence(0.8)])

    event = asyncio.run(scenario())

    assert Transcript.is_type(event.type)
    with _wav_of(fake.requests[0]) as wav:
        assert wav.getnframes() == RATE


def test_changed_audio_format_is_refused():
    fake = FakeTranscriptionApi()

    async def scenario():
        async with bridge_server(fake) as port:
            async with client(port) as wyoming:
                await wyoming.write_event(
                    AudioStart(rate=RATE, width=WIDTH, channels=1).event()
                )
                await wyoming.write_event(
                    AudioChunk(
                        rate=8000, width=WIDTH, channels=1, audio=silence(0.1)
                    ).event()
                )
                return await asyncio.wait_for(wyoming.read_event(), 5)

    event = asyncio.run(scenario())

    assert Error.from_event(event).code == "audio-format"
    assert fake.requests == []
