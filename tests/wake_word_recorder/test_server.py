"""Takes over TCP: the happy paths, every refusal and the broken endings."""

import asyncio
import re
import wave

import pytest
from helpers import (
    Satellite,
    audio_stop,
    chunks,
    detection,
    files,
    make_store,
    protocol,
    running,
    sidecar,
    take_start,
    tone,
    until,
)
from app.store import Store

NAME = re.compile(r"sat_living_room_\d{8}-\d{6}")


def test_positive_take_lands_in_the_import_layout(tmp_path) -> None:
    store = make_store(tmp_path)
    pcm = tone(3.0)

    async def run() -> None:
        async with running(store) as port, Satellite(port) as satellite:
            await satellite.send(take_start(seconds=3))
            accepted = await satellite.answer()
            assert accepted["type"] == "take-accepted"
            name = accepted["data"]["name"]
            assert NAME.fullmatch(name)
            await satellite.send(
                chunks(pcm[:32000]),
                detection("Hey AIVI", 1500),
                chunks(pcm[32000:]),
                audio_stop(),
            )
            saved = await satellite.answer()
            assert saved == {
                "type": "take-saved",
                "data": {"name": name, "seconds": 3.0, "end": "completed"},
            }
            assert await satellite.answer() is None

        assert files(store) == [
            f"positive/alex/{name}.json",
            f"positive/alex/{name}.wav",
        ]
        with wave.open(
            str(store.recordings / f"positive/alex/{name}.wav")
        ) as w:
            assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (
                16000,
                1,
                2,
            )
            assert w.readframes(w.getnframes()) == pcm
        meta = sidecar(store, f"positive/alex/{name}.json")
        assert meta["kind"] == "positive"
        assert meta["speaker"] == "alex"
        assert meta["satellite_id"] == "living_room"
        assert meta["requested_s"] == 3.0
        assert meta["end_reason"] == "completed"
        assert meta["detections"] == [{"name": "Hey AIVI", "offset_s": 1.5}]
        assert meta["dropped_bytes"] == 0

    asyncio.run(run())


def test_negative_take_ignores_the_speaker(tmp_path) -> None:
    store = make_store(tmp_path)

    async def run() -> None:
        async with running(store) as port, Satellite(port) as satellite:
            await satellite.send(
                take_start(kind="negative", speaker="Not Checked")
            )
            name = (await satellite.answer())["data"]["name"]
            await satellite.send(chunks(tone(2.5)), audio_stop("stopped", 640))
            saved = await satellite.answer()
            assert saved["data"]["end"] == "stopped"
        meta = sidecar(store, f"negative/{name}.json")
        assert meta["speaker"] is None
        assert meta["dropped_bytes"] == 640
        assert files(store) == [f"negative/{name}.json", f"negative/{name}.wav"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "changes, code",
    [
        ({"token": "wrong"}, "unauthorized"),
        ({"token": None}, "unauthorized"),
        ({"satellite": "garage"}, "unknown_satellite"),
        ({"satellite": ["living_room"]}, "unknown_satellite"),
        ({"version": 2}, "bad_version"),
        ({"rate": 48000}, "bad_format"),
        ({"channels": 2}, "bad_format"),
        ({"kind": "maybe"}, "bad_request"),
        ({"speaker": "Alex"}, "bad_speaker"),
        ({"speaker": "jürgen"}, "bad_speaker"),
        ({"speaker": "a__b"}, "bad_speaker"),
        ({"speaker": "-a"}, "bad_speaker"),
        ({"speaker": "a" * 25}, "bad_speaker"),
        ({"speaker": None}, "bad_speaker"),
        ({"seconds": 0}, "bad_duration"),
        ({"seconds": 7201}, "bad_duration"),
        ({"seconds": True}, "bad_duration"),
        ({"seconds": "60"}, "bad_duration"),
    ],
)
def test_bad_requests_are_refused_before_anything_is_written(
    tmp_path, changes, code
) -> None:
    store = make_store(tmp_path)

    async def run() -> None:
        async with running(store) as port, Satellite(port) as satellite:
            await satellite.send(take_start(**changes))
            assert await satellite.answer() == {
                "type": "error",
                "data": {"code": code},
            }
            assert await satellite.answer() is None
        assert not files(store)

    asyncio.run(run())


@pytest.mark.parametrize(
    "prepare, settings, code",
    [
        ("none", {}, "drive"),
        ("marker", {}, "drive"),
        ("full", {}, "low_space"),
        ("ready", {"token": ""}, "disabled"),
    ],
)
def test_missing_drive_or_token_refuses_takes(
    tmp_path, prepare, settings, code
) -> None:
    if prepare == "none":
        store = Store(tmp_path, 0)
    elif prepare == "marker":
        store = Store(tmp_path, 0)
        (tmp_path / ".aivi-capture-volume").write_text("x")
    else:
        store = make_store(tmp_path)
        if prepare == "full":
            store.min_free_bytes = 1 << 62

    async def run() -> None:
        async with (
            running(store, **settings) as port,
            Satellite(port) as satellite,
        ):
            await satellite.send(take_start())
            answer = await satellite.answer()
            assert answer == {"type": "error", "data": {"code": code}}
        assert not files(store)

    asyncio.run(run())


def test_first_event_must_start_a_take(tmp_path) -> None:
    store = make_store(tmp_path)

    async def run() -> None:
        async with running(store) as port, Satellite(port) as satellite:
            await satellite.send(chunks(tone(0.1)))
            assert await satellite.answer() == {
                "type": "error",
                "data": {"code": "protocol"},
            }

    asyncio.run(run())


def test_silent_connections_are_closed(tmp_path) -> None:
    store = make_store(tmp_path)

    async def run() -> None:
        async with (
            running(store, start_timeout=0.2) as port,
            Satellite(port) as satellite,
        ):
            assert await satellite.answer() is None

    asyncio.run(run())


def test_lost_connection_keeps_the_audio(tmp_path) -> None:
    store = make_store(tmp_path)

    async def run() -> None:
        async with running(store) as port:
            async with Satellite(port) as satellite:
                await satellite.send(take_start())
                name = (await satellite.answer())["data"]["name"]
                await satellite.send(chunks(tone(2.5)))
            wav = f"positive/alex/{name}.wav"
            await until(lambda: wav in files(store))
            meta = sidecar(store, f"positive/alex/{name}.json")
            assert meta["end_reason"] == "disconnected"
            assert meta["audio"]["duration_s"] == 2.5

    asyncio.run(run())


def test_idle_satellite_ends_the_take(tmp_path) -> None:
    store = make_store(tmp_path)

    async def run() -> None:
        async with (
            running(store, idle_timeout=0.3) as port,
            Satellite(port) as satellite,
        ):
            await satellite.send(take_start())
            name = (await satellite.answer())["data"]["name"]
            await satellite.send(chunks(tone(2.5)))
            assert await satellite.answer() is None
            meta = sidecar(store, f"positive/alex/{name}.json")
            assert meta["end_reason"] == "timeout"

    asyncio.run(run())


def test_short_take_is_discarded(tmp_path) -> None:
    store = make_store(tmp_path)

    async def run() -> None:
        async with running(store) as port, Satellite(port) as satellite:
            await satellite.send(take_start())
            await satellite.answer()
            await satellite.send(chunks(tone(1.0)), audio_stop("stopped"))
            assert await satellite.answer() == {
                "type": "error",
                "data": {"code": "too_short"},
            }
        assert not files(store)

    asyncio.run(run())


def test_new_take_ends_a_stale_one(tmp_path) -> None:
    store = make_store(tmp_path)

    async def run() -> None:
        async with running(store) as port:
            stale = Satellite(port)
            await stale.__aenter__()
            await stale.send(take_start())
            old = (await stale.answer())["data"]["name"]
            await stale.send(chunks(tone(2.5)))
            await asyncio.sleep(0.1)
            async with Satellite(port) as satellite:
                await satellite.send(take_start(kind="negative"))
                accepted = await satellite.answer()
                assert accepted["type"] == "take-accepted"
                meta = sidecar(store, f"positive/alex/{old}.json")
                assert meta["end_reason"] == "superseded"
                await satellite.send(chunks(tone(2.0)), audio_stop())
                assert (await satellite.answer())["type"] == "take-saved"
            await stale.close()

    asyncio.run(run())


def test_overlong_take_is_cut(tmp_path) -> None:
    store = make_store(tmp_path)

    async def run() -> None:
        async with (
            running(store, overrun_s=0.5) as port,
            Satellite(port) as satellite,
        ):
            await satellite.send(take_start(seconds=2))
            name = (await satellite.answer())["data"]["name"]
            await satellite.send(chunks(tone(4.0)))
            saved = await satellite.answer()
            assert saved["data"] == {
                "name": name,
                "seconds": 2.5,
                "end": "too_long",
            }

    asyncio.run(run())


def test_two_satellites_record_at_the_same_time(tmp_path) -> None:
    store = make_store(tmp_path)

    async def run() -> None:
        async with running(store) as port:
            async with Satellite(port) as first, Satellite(port) as second:
                await first.send(take_start())
                await second.send(take_start(satellite="kitchen"))
                assert (await first.answer())["type"] == "take-accepted"
                assert (await second.answer())["type"] == "take-accepted"
                for satellite in (first, second):
                    await satellite.send(chunks(tone(2.0)), audio_stop())
                    assert (await satellite.answer())["type"] == "take-saved"
        assert len(files(store)) == 4

    asyncio.run(run())


def test_connections_beyond_the_limit_are_closed(tmp_path) -> None:
    store = make_store(tmp_path)

    async def run() -> None:
        async with running(store, max_connections=1) as port:
            async with Satellite(port) as first:
                await first.send(take_start())
                assert (await first.answer())["type"] == "take-accepted"
                async with Satellite(port) as second:
                    await second.send(take_start(satellite="kitchen"))
                    assert await second.answer() is None

    asyncio.run(run())


def test_bad_detections_are_ignored(tmp_path) -> None:
    store = make_store(tmp_path)

    async def run() -> None:
        async with running(store) as port, Satellite(port) as satellite:
            await satellite.send(take_start())
            name = (await satellite.answer())["data"]["name"]
            await satellite.send(
                detection("Hey AIVI", -1),
                detection("<script>", 10),
                protocol.encode_event("detection", {"name": "x"}),
                protocol.encode_event("something-new", {"a": 1}),
                chunks(tone(2.0)),
                audio_stop(),
            )
            assert (await satellite.answer())["type"] == "take-saved"
        assert sidecar(store, f"positive/alex/{name}.json")["detections"] == []

    asyncio.run(run())
