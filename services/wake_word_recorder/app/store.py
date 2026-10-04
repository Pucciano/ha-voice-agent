"""Write wake word takes to the USB capture drive.

Layout, the same as dev/datasets/wake_word/recordings/ on the Mac, so
`make ww-pull` copies it as it is and `make ww-import` reads it unchanged:

    <capture dir>/.aivi-capture-volume       marker, by aivi-capture setup
    <capture dir>/aivi-wake-word/recordings/positive/<speaker>/<stem>.wav
                                                              /<stem>.json
    <capture dir>/aivi-wake-word/recordings/negative/<stem>.wav
                                                    /<stem>.json

<stem> is sat_<satellite>_<YYYYmmdd-HHMMSS> in local time, the scheme of the
Mac recorder, so entries in accepted.txt and excluded.txt stay valid. While a
take runs both files end in .part. At the end the sidecar is renamed first
and the WAV last, so a finished WAV always has its finished sidecar.

Nothing is written unless the marker and aivi-wake-word/ exist, both only on
the drive, and enough space is free.
"""

import datetime
import json
import math
import os
import pathlib
import shutil
import struct
import sys
from array import array
from dataclasses import dataclass
from typing import Any, BinaryIO

from . import __version__

SCHEMA_VERSION = 1
MARKER = ".aivi-capture-volume"
TAKES_DIR = "aivi-wake-word"
PART = ".part"
RATE = 16000
WIDTH = 2
CHANNELS = 1
HEADER_BYTES = 44
# Shorter takes are switched on by mistake; they are not kept.
MIN_SECONDS = 2.0


def wav_header(data_bytes: int) -> bytes:
    """Canonical 44-byte header for 16 kHz mono 16-bit PCM."""

    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_bytes,
        b"WAVE",
        b"fmt ",
        16,
        1,
        CHANNELS,
        RATE,
        RATE * WIDTH * CHANNELS,
        WIDTH * CHANNELS,
        WIDTH * 8,
        b"data",
        data_bytes,
    )


def _dbfs(value: float) -> float | None:
    if value <= 0:
        return None
    return round(20 * math.log10(value / 32768), 1)


def _fsync_dir(directory: pathlib.Path) -> None:
    handle = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def _write_json(path: pathlib.Path, content: dict[str, Any]) -> None:
    """Replace path with content; the temporary name also ends in .part."""

    temp = path.with_name(path.name.removesuffix(PART) + ".tmp" + PART)
    with temp.open("wb") as handle:
        handle.write(
            (json.dumps(content, ensure_ascii=False, indent=2) + "\n").encode()
        )
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


@dataclass(frozen=True)
class TakeInfo:
    satellite_id: str
    kind: str
    speaker: str | None
    requested_s: float
    started_at: datetime.datetime


class Take:  # pylint: disable=too-many-instance-attributes
    """One take being written. Blocking; call the methods in a thread."""

    def __init__(
        self, directory: pathlib.Path, stem: str, info: TakeInfo
    ) -> None:
        self.directory = directory
        self.stem = stem
        self.info = info
        self.wav_part = directory / f"{stem}.wav{PART}"
        self.json_part = directory / f"{stem}.json{PART}"
        self.data_bytes = 0
        self.dropped_bytes = 0
        self.detections: list[dict[str, Any]] = []
        self._peak = 0
        self._sum_squares = 0
        self._file: BinaryIO = self.wav_part.open("xb")
        try:
            self._file.write(wav_header(0))
            # Written now so that a recovered take keeps its metadata.
            _write_json(self.json_part, self.metadata("running"))
        except BaseException:
            self.discard()
            raise

    @property
    def duration_s(self) -> float:
        return self.data_bytes / (RATE * WIDTH * CHANNELS)

    def write(self, pcm: bytes) -> None:
        self._file.write(pcm)
        self.data_bytes += len(pcm)
        samples = array("h")
        samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
        if not samples:
            return
        if sys.byteorder != "little":
            samples.byteswap()
        self._peak = max(self._peak, abs(min(samples)), abs(max(samples)))
        self._sum_squares += math.sumprod(samples, samples)

    def add_detection(self, name: str, offset_s: float) -> None:
        self.detections.append({"name": name, "offset_s": round(offset_s, 3)})
        # Rewritten on every detection, so a recovered take keeps them too.
        _write_json(self.json_part, self.metadata("running"))

    def metadata(
        self, end_reason: str, ended_at: datetime.datetime | None = None
    ) -> dict[str, Any]:
        samples = self.data_bytes // WIDTH
        rms = math.sqrt(self._sum_squares / samples) if samples else 0.0
        return {
            "schema_version": SCHEMA_VERSION,
            "recorder_version": __version__,
            "name": self.stem,
            "satellite_id": self.info.satellite_id,
            "kind": self.info.kind,
            "speaker": self.info.speaker,
            "requested_s": self.info.requested_s,
            "started_at": self.info.started_at.isoformat(timespec="seconds"),
            "ended_at": (
                ended_at.isoformat(timespec="seconds") if ended_at else None
            ),
            "end_reason": end_reason,
            "audio": {
                "file": f"{self.stem}.wav",
                "rate": RATE,
                "width": WIDTH,
                "channels": CHANNELS,
                "samples": samples,
                "duration_s": round(self.duration_s, 3),
                "peak_dbfs": _dbfs(self._peak),
                "rms_dbfs": _dbfs(rms),
            },
            "detections": self.detections,
            "dropped_bytes": self.dropped_bytes,
        }

    def finalize(self, end_reason: str, ended_at: datetime.datetime) -> None:
        """Complete the header and both files, then give them their names."""

        self._file.seek(0)
        self._file.write(wav_header(self.data_bytes))
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()
        _write_json(self.json_part, self.metadata(end_reason, ended_at))
        os.replace(self.json_part, self.directory / f"{self.stem}.json")
        os.replace(self.wav_part, self.directory / f"{self.stem}.wav")
        _fsync_dir(self.directory)

    def discard(self) -> None:
        if not self._file.closed:
            self._file.close()
        self.wav_part.unlink(missing_ok=True)
        self.json_part.unlink(missing_ok=True)


class Store:
    def __init__(self, root: pathlib.Path, min_free_bytes: int) -> None:
        self.root = root
        self.takes = root / TAKES_DIR
        self.recordings = self.takes / "recordings"
        self.min_free_bytes = min_free_bytes

    def unavailable_reason(self) -> str | None:
        """Why nothing may be written right now; None when writing is fine."""

        if not (self.root / MARKER).is_file():
            return "no_marker"
        if not self.takes.is_dir():
            return "no_takes_dir"
        try:
            free = shutil.disk_usage(self.takes).free
        except OSError:
            return "no_takes_dir"
        if free < self.min_free_bytes:
            return "low_space"
        return None

    def directory(self, kind: str, speaker: str | None) -> pathlib.Path:
        if kind == "negative":
            return self.recordings / "negative"
        if kind != "positive" or not speaker:
            raise ValueError("a positive take needs a speaker")
        return self.recordings / "positive" / speaker

    def open_take(self, info: TakeInfo) -> Take:
        """Create the .part files under a free name. Raises OSError."""

        directory = self.directory(info.kind, info.speaker)
        directory.mkdir(parents=True, exist_ok=True)
        base = f"sat_{info.satellite_id}_{info.started_at:%Y%m%d-%H%M%S}"
        # A second take in the same second, or the repeated hour when the
        # clocks go back, gets a numbered name instead of overwriting.
        for number in range(1, 100):
            stem = base if number == 1 else f"{base}-{number}"
            names = (".wav", ".json", f".wav{PART}", f".json{PART}")
            if any((directory / f"{stem}{name}").exists() for name in names):
                continue
            try:
                return Take(directory, stem, info)
            except FileExistsError:
                continue
        raise FileExistsError(f"no free name for {base}")

    def recover(self) -> tuple[int, int]:
        """Finish takes an interrupted process left behind.

        Returns (recovered, removed). A recovered take keeps the audio up to
        the last write and the metadata of its sidecar part, with end_reason
        "recovered".
        """

        recovered = removed = 0
        if not self.recordings.is_dir():
            return recovered, removed
        for leftover in self.recordings.rglob(f"*.tmp{PART}"):
            leftover.unlink(missing_ok=True)
        for wav_part in sorted(self.recordings.rglob(f"*.wav{PART}")):
            if _recover(wav_part):
                recovered += 1
            else:
                removed += 1
        for json_part in self.recordings.rglob(f"*.json{PART}"):
            # A sidecar part without its WAV belongs to a discarded take.
            json_part.unlink(missing_ok=True)
        return recovered, removed


def _recover(wav_part: pathlib.Path) -> bool:
    directory = wav_part.parent
    stem = wav_part.name.removesuffix(f".wav{PART}")
    json_part = directory / f"{stem}.json{PART}"
    size = wav_part.stat().st_size
    data_bytes = max(0, size - HEADER_BYTES)
    data_bytes -= data_bytes % (WIDTH * CHANNELS)
    if data_bytes < MIN_SECONDS * RATE * WIDTH * CHANNELS:
        wav_part.unlink()
        json_part.unlink(missing_ok=True)
        return False

    ended_at = datetime.datetime.fromtimestamp(
        wav_part.stat().st_mtime
    ).astimezone()
    with wav_part.open("r+b") as handle:
        handle.write(wav_header(data_bytes))
        handle.truncate(HEADER_BYTES + data_bytes)
        handle.flush()
        os.fsync(handle.fileno())

    final_json = directory / f"{stem}.json"
    if not final_json.exists():
        # The sidecar was renamed first, so it exists only if the take
        # ended normally; otherwise build it from the sidecar part.
        metadata: dict[str, Any] = {}
        if json_part.is_file():
            try:
                metadata = json.loads(json_part.read_text())
            except ValueError:
                metadata = {}
        samples = data_bytes // WIDTH
        metadata.update(
            {
                "schema_version": SCHEMA_VERSION,
                "recorder_version": __version__,
                "name": stem,
                "ended_at": ended_at.isoformat(timespec="seconds"),
                "end_reason": "recovered",
            }
        )
        audio = metadata.setdefault("audio", {})
        audio.update(
            {
                "file": f"{stem}.wav",
                "rate": RATE,
                "width": WIDTH,
                "channels": CHANNELS,
                "samples": samples,
                "duration_s": round(samples / RATE, 3),
                # Not measured again; reading a long take would delay start.
                "peak_dbfs": None,
                "rms_dbfs": None,
            }
        )
        _write_json(json_part, metadata)
        os.replace(json_part, final_json)
    json_part.unlink(missing_ok=True)
    os.replace(wav_part, directory / f"{stem}.wav")
    _fsync_dir(directory)
    return True
