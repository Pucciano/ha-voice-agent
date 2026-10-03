"""Relay end to end: transparency, capture decisions, outcomes, drive guards."""

import asyncio
import json
import logging
import socket
import wave

import pytest
from helpers import (
    LIVING_ROOM,
    FakeUpstream,
    audio_chunk,
    audio_start,
    frame,
    frames,
    harness,
    samples,
    tone,
    until,
)

SAT = LIVING_ROOM.satellite_entity
SWITCH = LIVING_ROOM.switch_entity


async def connect(port: int):
    return await asyncio.open_connection(
        "127.0.0.1", port, limit=frames.MAX_HEADER_BYTES
    )


async def send(writer: asyncio.StreamWriter, *raws: bytes) -> None:
    for raw in raws:
        writer.write(raw)
    await writer.drain()


async def read_until_end(reader: asyncio.StreamReader) -> list[frames.Frame]:
    """Frames until transcript or error, or until the relay hangs up."""

    result = []
    while True:
        item = await asyncio.wait_for(frames.read_frame(reader), 5)
        if item is None:
            return result
        result.append(item)
        if item.type in ("transcript", "error"):
            return result


async def request(
    h, pcm: bytes, *, start: bytes | None = None, chunks=None, wait=True
) -> list[frames.Frame]:
    """One request like Home Assistant sends it, then hang up.

    With wait=True the client waits for the audio-start decision, as there
    is speech between audio-start and audio-stop in real use.
    """

    reader, writer = await connect(h.port)
    await send(
        writer,
        frame("transcribe", {"language": "de"}),
        start or audio_start(),
        *(chunks if chunks is not None else [audio_chunk(pcm)]),
    )
    if wait:
        await until(lambda: h.ha.requests >= 1 and h.relay.pending_tasks == 0)
    await send(writer, frame("audio-stop"))
    received = await read_until_end(reader)
    writer.close()
    await h.relay.drain()
    return received


def metadata(path):
    return json.loads((path / "metadata.json").read_text())


def test_frames_pass_byte_for_byte(tmp_path) -> None:
    # Unknown events, inline header data plus data block, streaming
    # transcripts: nothing may be changed, dropped or reordered.
    block = b'{"b": 22}'
    client_bytes = (
        b'{"type": "describe"}\n'
        + b'{"type": "x-unknown", "data": {"a": 1}, "data_length": %d, '
        b'"payload_length": 3, "extra": true}\n' % len(block)
        + block
        + b"xyz"
        + frame("transcribe", {"language": "de"})
        + audio_start()
        + audio_chunk(tone())
        + frame("audio-stop")
    )
    upstream_bytes = (
        frame("info", {"asr": []})
        + b'{"type": "x-future", "version": "9.9.9"}\n'
        + frame("transcript-start", {"language": "de"})
        + frame("transcript-chunk", {"text": "wie spät"})
        + frame("transcript-chunk", {"text": " ist es"})
        + frame("transcript-stop")
        + frame("transcript", {"text": "wie spät ist es"})
    )

    async def run() -> None:
        upstream = FakeUpstream(reply="raw", raw_reply=upstream_bytes)
        async with harness(tmp_path, upstream=upstream) as h:
            reader, writer = await connect(h.port)
            await send(writer, client_bytes)
            received = await asyncio.wait_for(
                reader.readexactly(len(upstream_bytes)), 5
            )
            writer.close()
            await upstream.closed.wait()
            assert received == upstream_bytes
            assert bytes(upstream.received[0]) == client_bytes

    asyncio.run(run())


def test_capture_writes_a_complete_sample(tmp_path) -> None:
    pcm = tone(0.5)

    async def run() -> None:
        async with harness(tmp_path) as h:
            await h.relay.fetch_stt_info()
            received = await request(h, pcm)
            assert received[-1].type == "transcript"
            [sample] = samples(h.store)
            meta = metadata(sample)
            assert f"-{LIVING_ROOM.satellite_id}-" in sample.name
            assert meta["verified"] is False
            assert meta["label_source"] == "speech-to-phrase"
            assert meta["pseudo_transcript"] == "wie spät ist es"
            assert meta["capture_outcome"] == "transcript"
            assert meta["satellite_id"] == "living_room"
            assert meta["satellite_entity"] == SAT
            assert meta["language"] == "de"
            assert meta["speaker"] is None
            assert meta["stt"] == {
                "program": "speech-to-phrase",
                "version": "1.4.3",
                "models": ["de_DE-zamia"],
            }
            assert meta["audio"]["bytes"] == len(pcm)
            assert meta["audio"]["duration_s"] == 0.5
            assert -11 < meta["audio"]["peak_dbfs"] < -10
            assert meta["stt_latency_ms"] is not None
            text = (sample / "transcript.txt").read_text(encoding="utf-8")
            assert text == "wie spät ist es\n"
            with wave.open(str(sample / "audio.wav")) as wav:
                assert (wav.getframerate(), wav.getsampwidth()) == (16000, 2)
                assert wav.getnchannels() == 1
                assert wav.readframes(wav.getnframes()) == pcm

    asyncio.run(run())


@pytest.mark.parametrize(
    ("upstream", "outcome", "transcript"),
    [
        (FakeUpstream(text=""), "empty", ""),
        (FakeUpstream(reply="error"), "error", None),
        (FakeUpstream(reply="close", delay=0.2), "upstream_disconnect", None),
    ],
)
def test_failed_recognitions_are_captured_too(
    tmp_path, upstream, outcome, transcript
) -> None:
    async def run() -> None:
        async with harness(tmp_path, upstream=upstream) as h:
            await request(h, tone())
            [sample] = samples(h.store)
            meta = metadata(sample)
            assert meta["capture_outcome"] == outcome
            assert meta["pseudo_transcript"] == transcript
            if outcome == "error":
                assert meta["error"] == {"text": "failed", "code": "test"}

    asyncio.run(run())


def test_client_hanging_up_after_audio_stop_is_captured(tmp_path) -> None:
    async def run() -> None:
        async with harness(tmp_path, upstream=FakeUpstream(reply="none")) as h:
            _, writer = await connect(h.port)
            await send(writer, audio_start(), audio_chunk(tone()))
            await until(lambda: h.relay.pending_tasks == 0 and h.ha.requests)
            await send(writer, frame("audio-stop"))
            writer.close()
            await h.upstream.closed.wait()
            await h.relay.drain()
            [sample] = samples(h.store)
            assert (
                metadata(sample)["capture_outcome"] == "downstream_disconnect"
            )

    asyncio.run(run())


def test_second_attempt_at_audio_stop(tmp_path) -> None:
    async def run() -> None:
        states = {SAT: "idle", SWITCH: "on"}
        upstream = FakeUpstream(delay=0.3)
        async with harness(tmp_path, states, upstream) as h:
            reader, writer = await connect(h.port)
            await send(writer, audio_start(), audio_chunk(tone()))
            await until(
                lambda: h.ha.requests >= 1 and h.relay.pending_tasks == 0
            )
            h.ha.set(**{SAT.replace(".", "__"): "listening"})
            await send(writer, frame("audio-stop"))
            await read_until_end(reader)
            writer.close()
            await h.relay.drain()
            assert len(samples(h.store)) == 1
            # 1 satellite query at audio-start, satellite + switch at audio-stop
            assert h.ha.requests == 3

    asyncio.run(run())


def test_no_retry_once_the_switch_says_off(tmp_path) -> None:
    async def run() -> None:
        states = {SAT: "listening", SWITCH: "off"}
        async with harness(tmp_path, states, FakeUpstream(delay=0.3)) as h:
            await request(h, tone())
            assert samples(h.store) == []
            assert h.ha.requests == 2

    asyncio.run(run())


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"states": {SAT: "listening", SWITCH: "off"}}, "not_enabled"),
        (
            # The second attempt at audio-stop needs time to finish, as the
            # recogniser takes about a second in real use.
            {
                "states": {SAT: "processing", SWITCH: "on"},
                "upstream": FakeUpstream(delay=0.3),
            },
            "no_listening_satellite",
        ),
        ({"satellites": ()}, "capture_disabled"),
        ({"marker": False}, "no_marker"),
        ({"min_free_bytes": 1 << 60}, "low_space"),
        ({"max_seconds": 0.05}, "too_long"),
        ({"max_capture_bytes": 1000}, "too_long"),
    ],
)
def test_requests_that_must_not_be_stored(
    tmp_path, caplog, kwargs, reason
) -> None:
    caplog.set_level(logging.INFO)

    async def run() -> None:
        async with harness(tmp_path, **kwargs) as h:
            received = await request(
                h, tone(), wait=reason != "capture_disabled"
            )
            assert received[-1].type == "transcript"
            assert samples(h.store) == []

    asyncio.run(run())
    assert f"capture=skipped reason={reason}" in caplog.text
    assert "wie spät" not in caplog.text


def test_format_change_discards_the_capture(tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO)

    async def run() -> None:
        async with harness(tmp_path) as h:
            chunks = [audio_chunk(tone()), audio_chunk(tone(), rate=8000)]
            received = await request(h, b"", chunks=chunks)
            assert received[-1].type == "transcript"
            assert samples(h.store) == []

    asyncio.run(run())
    assert "reason=format_changed" in caplog.text


def test_decision_still_running_at_the_end_discards(tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO)

    async def run() -> None:
        states = {SAT: ("delay", 0.5, "listening"), SWITCH: "on"}
        async with harness(tmp_path, states) as h:
            received = await request(h, tone(), wait=False)
            assert received[-1].type == "transcript"
            assert samples(h.store) == []

    asyncio.run(run())
    assert "reason=decision_pending" in caplog.text


def test_unfinished_samples_are_removed_before_the_first_write(
    tmp_path,
) -> None:
    async def run() -> None:
        async with harness(tmp_path) as h:
            leftover = h.store.samples / ".tmp-20261003T000000Z-living_room-abc"
            leftover.mkdir()
            (leftover / "audio.wav").write_bytes(b"partial")
            await request(h, tone())
            assert not leftover.exists()
            assert len(samples(h.store)) == 1

    asyncio.run(run())


def test_unreachable_upstream_closes_the_client(tmp_path) -> None:
    async def run() -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed_port = probe.getsockname()[1]
        async with harness(tmp_path) as h:
            object.__setattr__(h.relay.config, "upstream_port", closed_port)
            reader, writer = await connect(h.port)
            await send(writer, frame("describe"))
            assert await asyncio.wait_for(reader.read(), 5) == b""
            writer.close()

    asyncio.run(run())


def test_invalid_client_frame_closes_both_sides(tmp_path) -> None:
    async def run() -> None:
        async with harness(tmp_path) as h:
            reader, writer = await connect(h.port)
            await send(writer, frame("describe"), b"not a frame\n")
            await asyncio.wait_for(h.upstream.closed.wait(), 5)
            await asyncio.wait_for(reader.read(), 5)
            assert reader.at_eof()
            assert bytes(h.upstream.received[0]) == frame("describe")
            writer.close()

    asyncio.run(run())
