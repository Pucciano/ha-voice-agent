"""Download the pinned tools and datasets for wake word training.

Every step is idempotent: finished steps leave a stamp with the pinned
revision and are skipped on the next run.
"""

import argparse
import hashlib
import io
import logging
import shutil
import subprocess
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import soundfile
from huggingface_hub import hf_hub_download, snapshot_download
from tqdm import tqdm

from common import (
    SAMPLE_RATE,
    Paths,
    load_config,
    read_audio,
    resample,
    setup_logging,
    write_wav,
)

STEPS = (
    "vendor",
    "generators",
    "voices",
    "impulse_responses",
    "background",
    "negative_features",
    "german_speech",
)

_LOGGER = logging.getLogger("download")


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--only",
        nargs="+",
        choices=STEPS,
        default=list(STEPS),
        help="Run only these steps",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def is_done(paths: Paths, step: str, revision: str) -> bool:
    """Whether a step already finished for the pinned revision."""

    stamp = paths.cache / "stamps" / step
    return stamp.is_file() and stamp.read_text(encoding="utf-8") == revision


def mark_done(paths: Paths, step: str, revision: str) -> None:
    """Record that a step finished for the pinned revision."""

    stamp = paths.cache / "stamps" / step
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(revision, encoding="utf-8")


def git(*args: str) -> str:
    """Run git and return its output."""

    result = subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def checkout(url: str, commit: str, target: Path) -> None:
    """Check out a repository at a pinned commit."""

    if not (target / ".git").is_dir():
        target.parent.mkdir(parents=True, exist_ok=True)
        git(
            "clone",
            "--quiet",
            "--filter=blob:none",
            "--no-checkout",
            url,
            str(target),
        )
    try:
        git("-C", str(target), "cat-file", "-e", f"{commit}^{{commit}}")
    except subprocess.CalledProcessError:
        git("-C", str(target), "fetch", "--quiet", "origin", commit)
    git("-C", str(target), "checkout", "--quiet", "--detach", commit)
    head = git("-C", str(target), "rev-parse", "HEAD")
    if head != commit:
        raise RuntimeError(f"{target} is at {head}, expected {commit}")
    _LOGGER.info("%s at %s", target.name, commit[:12])


def sha256_of(path: Path) -> str:
    """SHA-256 of a file."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch_url(url: str, target: Path, sha256: str | None) -> None:
    """Download a file once and check its SHA-256 when one is pinned."""

    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        with urllib.request.urlopen(url) as response, partial.open("wb") as out:
            total = int(response.headers.get("Content-Length", 0)) or None
            with tqdm(
                total=total, unit="B", unit_scale=True, desc=target.name
            ) as progress:
                for block in iter(lambda: response.read(1 << 20), b""):
                    out.write(block)
                    progress.update(len(block))
        partial.rename(target)
    actual = sha256_of(target)
    if sha256 is None:
        _LOGGER.warning(
            "%s has no pinned sha256; it is %s", target.name, actual
        )
    elif actual != sha256:
        target.unlink()
        raise RuntimeError(f"{target.name}: sha256 {actual}, expected {sha256}")


def download_vendor(config: dict, paths: Paths) -> None:
    """Check out microWakeWord and piper-sample-generator."""

    vendor = config["vendor"]
    checkout(
        vendor["micro_wake_word"]["url"],
        vendor["micro_wake_word"]["commit"],
        paths.micro_wake_word,
    )
    checkout(
        vendor["piper_sample_generator"]["url"],
        vendor["piper_sample_generator"]["commit"],
        paths.piper_sample_generator,
    )


def download_generators(config: dict, paths: Paths) -> None:
    """Download the Piper generators and place their configs next to them."""

    for name, spec in config["downloads"]["generators"].items():
        target = paths.generators / f"{name}.pt"
        fetch_url(spec["url"], target, spec.get("sha256"))
        model_config = (
            paths.piper_sample_generator / "models" / f"{name}.pt.json"
        )
        shutil.copyfile(model_config, paths.generators / f"{name}.pt.json")
        _LOGGER.info("Generator %s ready", name)


def download_voices(config: dict, paths: Paths) -> None:
    """Download the German Piper voices."""

    spec = config["downloads"]["voices"]
    if is_done(paths, "voices", spec["revision"]):
        return
    for name in spec["files"]:
        for suffix in (".onnx", ".onnx.json"):
            hf_hub_download(
                repo_id=spec["repo"],
                revision=spec["revision"],
                filename=name + suffix,
                local_dir=paths.voices,
            )
        _LOGGER.info("Voice %s ready", Path(name).name)
    mark_done(paths, "voices", spec["revision"])


def download_impulse_responses(config: dict, paths: Paths) -> None:
    """Download the MIT room impulse responses (already 16 kHz)."""

    spec = config["downloads"]["impulse_responses"]
    if is_done(paths, "impulse_responses", spec["revision"]):
        return
    snapshot = Path(
        snapshot_download(
            repo_id=spec["repo"],
            repo_type="dataset",
            revision=spec["revision"],
            allow_patterns=[spec["pattern"]],
            local_dir=paths.cache / "downloads" / "impulse_responses",
        )
    )
    paths.impulse_responses.mkdir(parents=True, exist_ok=True)
    count = 0
    for wav in snapshot.glob(spec["pattern"]):
        info = soundfile.info(str(wav))
        if info.samplerate != SAMPLE_RATE or info.channels != 1:
            raise RuntimeError(
                f"{wav.name}: {info.samplerate} Hz, expected mono"
            )
        shutil.copyfile(wav, paths.impulse_responses / wav.name)
        count += 1
    _LOGGER.info("%d impulse responses", count)
    mark_done(paths, "impulse_responses", spec["revision"])


def convert_audioset(parquet: Path, target: Path) -> int:
    """Decode the AudioSet shard to 16 kHz WAV files."""

    count = 0
    shard = pq.ParquetFile(parquet)
    with tqdm(total=shard.metadata.num_rows, desc="AudioSet") as progress:
        for batch in shard.iter_batches(
            batch_size=64, columns=["video_id", "audio"]
        ):
            for row in batch.to_pylist():
                progress.update(1)
                try:
                    audio, rate = soundfile.read(
                        io.BytesIO(row["audio"]["bytes"]),
                        dtype="float32",
                        always_2d=True,
                    )
                except soundfile.LibsndfileError as error:
                    _LOGGER.debug("Skipping %s: %s", row["video_id"], error)
                    continue
                mono = resample(audio.mean(axis=1), rate, SAMPLE_RATE)
                if np.max(np.abs(mono), initial=0.0) < 1e-4:
                    continue
                write_wav(target / f"{row['video_id']}.wav", mono)
                count += 1
    return count


def convert_fma(archive: Path, target: Path) -> int:
    """Decode the FMA extra-small archive to 16 kHz WAV files."""

    extracted = archive.parent / "fma_xs"
    if not extracted.is_dir():
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(extracted)
    count = 0
    for mp3 in tqdm(sorted(extracted.glob("**/*.mp3")), desc="FMA"):
        try:
            audio = read_audio(mp3)
        except (soundfile.LibsndfileError, RuntimeError) as error:
            _LOGGER.debug("Skipping %s: %s", mp3.name, error)
            continue
        write_wav(target / f"{mp3.stem}.wav", audio)
        count += 1
    return count


def download_background(config: dict, paths: Paths) -> None:
    """Download background audio and convert it to 16 kHz WAV files."""

    for name, spec in config["downloads"]["background"].items():
        if is_done(paths, f"background_{name}", spec["revision"]):
            continue
        downloaded = Path(
            hf_hub_download(
                repo_id=spec["repo"],
                repo_type="dataset",
                revision=spec["revision"],
                filename=spec["file"],
                local_dir=paths.cache / "downloads" / name,
            )
        )
        target = paths.background / name
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        if name == "audioset":
            count = convert_audioset(downloaded, target)
        else:
            count = convert_fma(downloaded, target)
        _LOGGER.info("%d background clips from %s", count, name)
        mark_done(paths, f"background_{name}", spec["revision"])


def download_negative_features(config: dict, paths: Paths) -> None:
    """Download and unpack the precomputed negative spectrograms."""

    spec = config["downloads"]["negative_features"]
    for filename in spec["files"]:
        step = f"negative_features_{Path(filename).stem}"
        if is_done(paths, step, spec["revision"]):
            continue
        archive = Path(
            hf_hub_download(
                repo_id=spec["repo"],
                repo_type="dataset",
                revision=spec["revision"],
                filename=filename,
                local_dir=paths.cache / "downloads" / "negative_features",
            )
        )
        target = paths.negative_features / Path(filename).stem
        if target.exists():
            shutil.rmtree(target)
        _LOGGER.info("Unpacking %s", filename)
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(paths.negative_features)
        if not target.is_dir():
            raise RuntimeError(f"{filename} did not unpack to {target}")
        mark_done(paths, step, spec["revision"])


def download_german_speech(config: dict, paths: Paths) -> None:
    """Download German speech; build_features.py decodes it."""

    spec = config["downloads"]["german_speech"]
    if is_done(paths, "german_speech", spec["revision"]):
        return
    for filename in spec["files"]:
        hf_hub_download(
            repo_id=spec["repo"],
            repo_type="dataset",
            revision=spec["revision"],
            filename=filename,
            local_dir=paths.german_speech,
        )
    mark_done(paths, "german_speech", spec["revision"])


def main() -> None:
    """Run the selected download steps in order."""

    args = parse_args()
    setup_logging(args.verbose)
    config = load_config()
    paths = Paths.from_config(config)
    steps = {
        "vendor": download_vendor,
        "generators": download_generators,
        "voices": download_voices,
        "impulse_responses": download_impulse_responses,
        "background": download_background,
        "negative_features": download_negative_features,
        "german_speech": download_german_speech,
    }
    for step in STEPS:
        if step in args.only:
            _LOGGER.info("Step %s", step)
            steps[step](config, paths)
    _LOGGER.info("Done")


if __name__ == "__main__":
    main()
