"""Takes on the drive: guards, names, finishing and recovery."""

import datetime
import json
import wave

from helpers import make_store, tone
from app.store import HEADER_BYTES, MARKER, Store, TakeInfo, wav_header

STARTED = datetime.datetime(
    2026, 10, 4, 12, 30, 5, tzinfo=datetime.timezone.utc
)


def info(kind: str = "positive", speaker: str | None = "alex") -> TakeInfo:
    return TakeInfo("living_room", kind, speaker, 60.0, STARTED)


def test_guards_keep_writes_off_a_missing_drive(tmp_path) -> None:
    store = Store(tmp_path, 0)
    assert store.unavailable_reason() == "no_marker"
    (tmp_path / MARKER).write_text("x")
    assert store.unavailable_reason() == "no_takes_dir"
    store.takes.mkdir()
    assert store.unavailable_reason() is None
    store.min_free_bytes = 1 << 62
    assert store.unavailable_reason() == "low_space"


def test_header_is_a_standard_wav(tmp_path) -> None:
    pcm = tone(0.5)
    path = tmp_path / "x.wav"
    path.write_bytes(wav_header(len(pcm)) + pcm)
    assert len(wav_header(0)) == HEADER_BYTES
    with wave.open(str(path)) as wav:
        assert (wav.getframerate(), wav.getnchannels()) == (16000, 1)
        assert wav.getsampwidth() == 2
        assert wav.readframes(wav.getnframes()) == pcm


def test_finished_take_has_wav_and_sidecar_without_parts(tmp_path) -> None:
    store = make_store(tmp_path)
    take = store.open_take(info())
    pcm = tone(3.0)
    take.write(pcm)
    take.add_detection("Hey AIVI", 1.2345)
    take.finalize("completed", STARTED + datetime.timedelta(seconds=3))
    directory = store.recordings / "positive" / "alex"
    stem = "sat_living_room_20261004-123005"
    assert sorted(p.name for p in directory.iterdir()) == [
        f"{stem}.json",
        f"{stem}.wav",
    ]
    meta = json.loads((directory / f"{stem}.json").read_text())
    assert meta["end_reason"] == "completed"
    assert meta["detections"] == [{"name": "Hey AIVI", "offset_s": 1.234}]
    assert meta["audio"]["duration_s"] == 3.0
    assert -12 < meta["audio"]["peak_dbfs"] < -10
    assert -15 < meta["audio"]["rms_dbfs"] < -13
    with wave.open(str(directory / f"{stem}.wav")) as wav:
        assert wav.readframes(wav.getnframes()) == pcm


def test_same_second_gets_a_numbered_name(tmp_path) -> None:
    store = make_store(tmp_path)
    first = store.open_take(info(kind="negative", speaker=None))
    second = store.open_take(info(kind="negative", speaker=None))
    assert first.stem == "sat_living_room_20261004-123005"
    assert second.stem == "sat_living_room_20261004-123005-2"
    assert first.directory == store.recordings / "negative"


def test_discard_leaves_nothing(tmp_path) -> None:
    store = make_store(tmp_path)
    take = store.open_take(info())
    take.write(tone(1.0))
    take.discard()
    assert not list(store.recordings.rglob("*.*"))


def test_recovery_finishes_an_interrupted_take(tmp_path) -> None:
    store = make_store(tmp_path)
    take = store.open_take(info())
    pcm = tone(3.0)
    take.write(pcm)
    take.add_detection("Hey AIVI", 2.0)
    # The process dies: the header still says 0 bytes.
    take._file.close()  # pylint: disable=protected-access
    assert store.recover() == (1, 0)
    directory = store.recordings / "positive" / "alex"
    stem = take.stem
    assert sorted(p.name for p in directory.iterdir()) == [
        f"{stem}.json",
        f"{stem}.wav",
    ]
    with wave.open(str(directory / f"{stem}.wav")) as wav:
        assert wav.readframes(wav.getnframes()) == pcm
    meta = json.loads((directory / f"{stem}.json").read_text())
    assert meta["end_reason"] == "recovered"
    assert meta["speaker"] == "alex"
    assert meta["detections"] == [{"name": "Hey AIVI", "offset_s": 2.0}]
    assert meta["audio"]["duration_s"] == 3.0


def test_recovery_removes_short_leftovers(tmp_path) -> None:
    store = make_store(tmp_path)
    take = store.open_take(info())
    take.write(tone(0.5))
    take._file.close()  # pylint: disable=protected-access
    assert store.recover() == (0, 1)
    assert not list(store.recordings.rglob("*.*"))


def test_recovery_keeps_a_finished_sidecar(tmp_path) -> None:
    store = make_store(tmp_path)
    take = store.open_take(info())
    take.write(tone(3.0))
    take.finalize("completed", STARTED)
    directory = take.directory
    # As if the process died between renaming the sidecar and the WAV.
    (directory / f"{take.stem}.wav").rename(directory / f"{take.stem}.wav.part")
    assert store.recover() == (1, 0)
    meta = json.loads((directory / f"{take.stem}.json").read_text())
    assert meta["end_reason"] == "completed"
    assert (directory / f"{take.stem}.wav").is_file()
