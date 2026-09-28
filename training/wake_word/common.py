"""Shared configuration, paths and audio helpers for the wake word pipeline."""

import hashlib
import logging
import math
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile
import yaml
from scipy.signal import resample_poly

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(__file__).with_name("config.yaml")
SAMPLE_RATE = 16000


def setup_logging(verbose: bool = False) -> None:
    """Log to stderr with timestamps."""

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )


def load_config(path: Path = CONFIG_PATH) -> dict:
    """Load the training configuration."""

    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


@dataclass(frozen=True)
class Paths:
    """Locations of downloads, data and models, derived from the config."""

    cache: Path
    data: Path
    models: Path

    @classmethod
    def from_config(cls, config: dict) -> "Paths":
        return cls(
            cache=REPO_ROOT / config["paths"]["cache"],
            data=REPO_ROOT / config["paths"]["data"],
            models=REPO_ROOT / config["paths"]["models"],
        )

    @property
    def vendor(self) -> Path:
        return self.cache / "vendor"

    @property
    def micro_wake_word(self) -> Path:
        return self.vendor / "micro-wake-word"

    @property
    def piper_sample_generator(self) -> Path:
        return self.vendor / "piper-sample-generator"

    @property
    def generators(self) -> Path:
        return self.cache / "generators"

    @property
    def voices(self) -> Path:
        return self.cache / "voices"

    @property
    def impulse_responses(self) -> Path:
        return self.cache / "impulse_responses"

    @property
    def background(self) -> Path:
        return self.cache / "background"

    @property
    def negative_features(self) -> Path:
        return self.cache / "negative_features"

    @property
    def german_speech(self) -> Path:
        return self.cache / "german_speech"

    @property
    def recordings(self) -> Path:
        return self.data / "recordings"

    @property
    def clips(self) -> Path:
        return self.data / "clips"

    @property
    def features(self) -> Path:
        return self.data / "features"

    @property
    def preview(self) -> Path:
        return self.data / "preview"


def use_vendor(paths: Paths) -> None:
    """Make the pinned microWakeWord and piper-sample-generator importable."""

    missing = [
        checkout
        for checkout in (paths.micro_wake_word, paths.piper_sample_generator)
        if not checkout.is_dir()
    ]
    if missing:
        names = ", ".join(str(path) for path in missing)
        raise SystemExit(f"Missing checkout {names}; run `make ww-setup`.")
    for checkout in (paths.micro_wake_word, paths.piper_sample_generator):
        if str(checkout) not in sys.path:
            sys.path.insert(0, str(checkout))


def vendor_pythonpath(paths: Paths) -> str:
    """PYTHONPATH value for subprocesses that import the checkouts."""

    return ":".join(
        str(path)
        for path in (paths.micro_wake_word, paths.piper_sample_generator)
    )


def resample(
    audio: np.ndarray, source_rate: int, target_rate: int
) -> np.ndarray:
    """Resample float audio with a polyphase filter."""

    if source_rate == target_rate:
        return audio
    divisor = math.gcd(source_rate, target_rate)
    return resample_poly(
        audio, target_rate // divisor, source_rate // divisor
    ).astype(np.float32)


def _convert_with_system_tool(path: Path, target: Path) -> None:
    """Convert formats libsndfile cannot read (m4a, aac, caf) to WAV."""

    if shutil.which("afconvert"):
        command = [
            "afconvert",
            "-f",
            "WAVE",
            "-d",
            f"LEI16@{SAMPLE_RATE}",
            "-c",
            "1",
            str(path),
            str(target),
        ]
    elif shutil.which("ffmpeg"):
        command = [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(path),
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            str(target),
        ]
    else:
        raise RuntimeError(f"Cannot decode {path}: needs afconvert or ffmpeg")
    subprocess.run(command, check=True)


def read_audio(path: Path) -> np.ndarray:
    """Read any audio file as float32 mono at 16 kHz."""

    try:
        audio, rate = soundfile.read(str(path), dtype="float32", always_2d=True)
    except soundfile.LibsndfileError:
        with tempfile.TemporaryDirectory() as tmp:
            converted = Path(tmp) / "converted.wav"
            _convert_with_system_tool(path, converted)
            audio, rate = soundfile.read(
                str(converted), dtype="float32", always_2d=True
            )
    return resample(audio.mean(axis=1), rate, SAMPLE_RATE)


def write_wav(path: Path, audio: np.ndarray, rate: int = SAMPLE_RATE) -> None:
    """Write float or int16 audio as 16-bit PCM WAV."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if audio.dtype != np.int16:
        audio = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
    soundfile.write(str(path), audio, rate, subtype="PCM_16")


def dbfs(audio: np.ndarray) -> float:
    """Peak level of float audio in dBFS."""

    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    return 20.0 * math.log10(max(peak, 1e-6))


def stable_fraction(key: str) -> float:
    """Deterministic number in [0, 1) for a key, used for stable splits."""

    digest = hashlib.sha1(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def reset_directory(path: Path) -> None:
    """Delete and recreate a generated directory."""

    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
