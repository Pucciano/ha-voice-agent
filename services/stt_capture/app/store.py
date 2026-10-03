"""Write captured requests to the USB drive as dataset samples.

Layout, compatible with training/stt/prepare_dataset.py:

    <capture dir>/.aivi-capture-volume     marker, created by aivi-capture setup
    <capture dir>/aivi-stt/<sample id>/audio.wav
                                      /transcript.txt
                                      /metadata.json

Nothing is written unless the marker and the sample directory exist and
enough space is free. Both exist only on the drive, so a missing drive never
leads to writes on the SD card. A sample is written to `.tmp-<id>` first and
renamed when complete.
"""

import datetime
import hashlib
import io
import json
import math
import os
import pathlib
import secrets
import shutil
import sys
import wave
from array import array
from dataclasses import dataclass
from typing import Any

from . import __version__
from .home_assistant import Satellite

SCHEMA_VERSION = 1
MARKER = ".aivi-capture-volume"
SAMPLES_DIR = "aivi-stt"
TMP_PREFIX = ".tmp-"


@dataclass(frozen=True)
class Sample:  # pylint: disable=too-many-instance-attributes
    pcm: bytes
    rate: int
    width: int
    channels: int
    started_at: datetime.datetime
    satellite: Satellite
    outcome: str
    transcript: str | None
    error: dict[str, Any] | None
    language: str | None
    stt_info: dict[str, Any] | None
    stt_latency_ms: int | None


def _dbfs(value: float, full_scale: float) -> float | None:
    if value <= 0:
        return None
    return round(20 * math.log10(value / full_scale), 1)


def levels(pcm: bytes, width: int) -> tuple[float | None, float | None]:
    """Peak and RMS in dBFS for 16-bit PCM; None for silence or other widths."""

    if width != 2 or len(pcm) < 2:
        return None, None
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if sys.byteorder != "little":
        samples.byteswap()
    peak = max(abs(min(samples)), abs(max(samples)))
    rms = math.sqrt(math.sumprod(samples, samples) / len(samples))
    return _dbfs(peak, 32768), _dbfs(rms, 32768)


def _wav(sample: "Sample") -> bytes:
    buffer = io.BytesIO()
    # pylint cannot tell that mode "wb" returns a Wave_write.
    # pylint: disable=no-member
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(sample.channels)
        wav.setsampwidth(sample.width)
        wav.setframerate(sample.rate)
        wav.writeframes(sample.pcm)
    return buffer.getvalue()


def _write_file(path: pathlib.Path, content: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


class Store:
    def __init__(self, root: pathlib.Path, min_free_bytes: int) -> None:
        self.root = root
        self.samples = root / SAMPLES_DIR
        self.min_free_bytes = min_free_bytes
        self._cleaned = False

    def unavailable_reason(self) -> str | None:
        """Why nothing may be written right now; None when writing is fine."""

        if not (self.root / MARKER).is_file():
            return "no_marker"
        if not self.samples.is_dir():
            return "no_samples_dir"
        try:
            free = shutil.disk_usage(self.samples).free
        except OSError:
            return "no_samples_dir"
        if free < self.min_free_bytes:
            return "low_space"
        return None

    def cleanup_temp(self) -> int:
        """Remove `.tmp-*` leftovers of interrupted writes; returns count."""

        removed = 0
        for path in self.samples.glob(f"{TMP_PREFIX}*"):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        return removed

    def cleanup_once(self) -> int:
        """cleanup_temp() the first time it is called in this process."""

        if self._cleaned:
            return 0
        self._cleaned = True
        return self.cleanup_temp()

    def write(self, sample: Sample) -> str:
        """Write one sample and return its id. Raises OSError on failure.

        Blocking; run it in a thread.
        """

        self.cleanup_once()
        stamp = sample.started_at.strftime("%Y%m%dT%H%M%SZ")
        sample_id = (
            f"{stamp}-{sample.satellite.satellite_id}-{secrets.token_hex(3)}"
        )
        temp_dir = self.samples / f"{TMP_PREFIX}{sample_id}"
        temp_dir.mkdir()
        try:
            self._write_files(temp_dir, sample_id, sample)
            temp_dir.rename(self.samples / sample_id)
        except BaseException:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise
        directory = os.open(self.samples, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return sample_id

    def _write_files(
        self, directory: pathlib.Path, sample_id: str, sample: Sample
    ) -> None:
        _write_file(directory / "audio.wav", _wav(sample))
        peak, rms = levels(sample.pcm, sample.width)
        bytes_per_second = sample.rate * sample.width * sample.channels
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "capture_version": __version__,
            "sample_id": sample_id,
            "created_at": sample.started_at.isoformat(),
            "satellite_id": sample.satellite.satellite_id,
            "satellite_entity": sample.satellite.satellite_entity,
            "language": sample.language,
            "label_source": "speech-to-phrase",
            "verified": False,
            "pseudo_transcript": sample.transcript,
            "capture_outcome": sample.outcome,
            "error": sample.error,
            "audio_file": "audio.wav",
            "transcript_file": "transcript.txt",
            "audio": {
                "rate": sample.rate,
                "width": sample.width,
                "channels": sample.channels,
                "bytes": len(sample.pcm),
                "duration_s": round(len(sample.pcm) / bytes_per_second, 3),
                "sha256": hashlib.sha256(sample.pcm).hexdigest(),
                "peak_dbfs": peak,
                "rms_dbfs": rms,
            },
            "stt": sample.stt_info,
            "stt_latency_ms": sample.stt_latency_ms,
            "speaker": None,
        }
        # The pseudo label until a review replaces it (verified: true).
        _write_file(
            directory / "transcript.txt",
            ((sample.transcript or "") + "\n").encode("utf-8"),
        )
        _write_file(
            directory / "metadata.json",
            (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            ),
        )
