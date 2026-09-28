"""Augment the clips and compute the spectrogram features for training.

Each phrase clip is placed at the end of a 3.2 s window, mixed with
background audio and reverberation, and turned into the same 40-channel
spectrogram the satellite computes. Training clips are stored once; the
training config shifts them by up to nine feature windows
(fixed_right_cutoff) instead of storing ten shifted copies as the upstream
notebook does. German speech keeps its length and gets the same kind of
augmentation; household recordings stay as they are.
"""

import argparse
import io
import logging
import os
import random
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import soundfile

from common import (
    SAMPLE_RATE,
    Paths,
    load_config,
    reset_directory,
    resample,
    setup_logging,
    stable_fraction,
    use_vendor,
)

# Features are multiples of this step; stored as uint16 they take half the
# space and microWakeWord scales them back when loading.
FEATURE_SCALE = 0.0390625
STEP_MS = 10
# Clips per shard of a training set; shards are built in parallel.
SHARD_SIZE = 2000
# Parquet row groups (about 100 utterances each) per German speech job.
ROW_GROUPS_PER_JOB = 8
PHRASE_SETS = ("tts_positive", "tts_negative", "real_positive")
ALL_SETS = (*PHRASE_SETS, "real_negative", "german_speech")

_LOGGER = logging.getLogger("features")


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--only",
        nargs="+",
        choices=ALL_SETS,
        help="Build only these feature sets",
    )
    parser.add_argument(
        "--workers", type=int, default=max((os.cpu_count() or 2) - 2, 1)
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def wav_files(directory: Path) -> list[str]:
    """WAV files of a directory as sorted strings."""

    return sorted(str(path) for path in directory.glob("*.wav"))


def tts_split(files: list[str]) -> dict[str, list[str]]:
    """Stable 80/10/10 split of synthetic clips."""

    splits: dict[str, list[str]] = {
        "training": [],
        "validation": [],
        "testing": [],
    }
    for path in files:
        fraction = stable_fraction(Path(path).name)
        if fraction < 0.8:
            splits["training"].append(path)
        elif fraction < 0.9:
            splits["validation"].append(path)
        else:
            splits["testing"].append(path)
    return splits


def mmap_dir(paths: Paths, name: str, mode: str, shard: int) -> str:
    """Output directory of one job."""

    return str(paths.features / name / mode / f"{name}_{shard:03d}_mmap")


def phrase_jobs(config: dict, paths: Paths, name: str) -> list[dict]:
    """Jobs for a set of short phrase clips."""

    clips = paths.clips / name
    if name == "real_positive":
        splits = {
            "training": wav_files(clips / "train"),
            "validation": wav_files(clips / "validation"),
            "testing": wav_files(clips / "test"),
        }
    else:
        splits = tts_split(wav_files(clips))
    if not any(splits.values()):
        _LOGGER.warning("No clips for %s in %s", name, clips)
        return []
    jobs = []
    for mode, files in splits.items():
        repeat = (
            config["features"]["repetitions"][name] if mode == "training" else 1
        )
        for shard, start in enumerate(range(0, len(files), SHARD_SIZE)):
            chunk = files[start : start + SHARD_SIZE]
            jobs.append(
                {
                    "files": chunk,
                    "repeat": repeat,
                    "augment": "phrase",
                    "count": len(chunk) * repeat,
                    "out": mmap_dir(paths, name, mode, shard),
                }
            )
    return jobs


def german_speech_jobs(paths: Paths) -> list[dict]:
    """Jobs over the row groups of the German speech parquet files."""

    files = sorted(paths.german_speech.glob("**/*.parquet"))
    if not files:
        _LOGGER.warning("No German speech; run `make ww-setup`")
        return []
    jobs = []
    for parquet in files:
        metadata = pq.ParquetFile(parquet).metadata
        for first in range(0, metadata.num_row_groups, ROW_GROUPS_PER_JOB):
            groups = list(
                range(
                    first,
                    min(first + ROW_GROUPS_PER_JOB, metadata.num_row_groups),
                )
            )
            jobs.append(
                {
                    "parquet": str(parquet),
                    "row_groups": groups,
                    "repeat": 1,
                    "augment": "long",
                    "count": sum(
                        metadata.row_group(g).num_rows for g in groups
                    ),
                    "out": mmap_dir(
                        paths, "german_speech", "training", len(jobs)
                    ),
                }
            )
    return jobs


def plan_jobs(config: dict, paths: Paths, only: set[str]) -> list[dict]:
    """Feature jobs: one per set, mode and shard."""

    jobs = []
    for name in ALL_SETS:
        if name not in only:
            continue
        # Also clears features of clips that are gone.
        reset_directory(paths.features / name)
        if name in PHRASE_SETS:
            jobs.extend(phrase_jobs(config, paths, name))
        elif name == "german_speech":
            jobs.extend(german_speech_jobs(paths))
        else:
            files = wav_files(paths.clips / "real_negative" / "train")
            if not files:
                _LOGGER.warning("No negative recordings imported yet")
                continue
            jobs.append(
                {
                    "files": files,
                    "repeat": 1,
                    "augment": None,
                    "count": len(files),
                    "out": mmap_dir(paths, name, "training", 0),
                }
            )
    return jobs


def make_augmenter(config: dict, paths: Paths, mode: str | None):
    """Augmentation for phrase clips (fixed window) or long speech."""

    if mode is None:
        return None
    # pylint: disable=import-outside-toplevel,import-error
    from microwakeword.audio.augmentation import Augmentation

    settings = config["augmentation"]
    backgrounds = [
        directory
        for directory in (
            paths.background / "audioset",
            paths.background / "fma",
            paths.clips / "real_negative" / "train",
        )
        if any(directory.glob("*.wav"))
    ]
    phrase = mode == "phrase"
    return Augmentation(
        augmentation_duration_s=settings["duration_s"] if phrase else None,
        augmentation_probabilities=settings["probabilities"],
        impulse_paths=[str(paths.impulse_responses)],
        background_paths=[str(directory) for directory in backgrounds],
        background_min_snr_db=settings["background_snr_db"][0],
        background_max_snr_db=settings["background_snr_db"][1],
        min_jitter_s=settings["jitter_s"][0] if phrase else 0.0,
        max_jitter_s=settings["jitter_s"][1] if phrase else 0.0,
    )


def job_audio(job: dict):
    """The job's audio as float32 at 16 kHz, repeated as configured."""

    for _ in range(job["repeat"]):
        if "parquet" in job:
            table = pq.ParquetFile(job["parquet"])
            for group in job["row_groups"]:
                rows = table.read_row_group(group, columns=["audio"])
                for row in rows.column("audio").to_pylist():
                    audio, rate = soundfile.read(
                        io.BytesIO(row["bytes"]),
                        dtype="float32",
                        always_2d=True,
                    )
                    yield resample(audio.mean(axis=1), rate, SAMPLE_RATE)
        else:
            for path in job["files"]:
                yield soundfile.read(path, dtype="float32")[0]


def run_job(job: dict, config: dict, paths: Paths) -> tuple[str, int]:
    """Augment the audio of one job and write its spectrograms."""

    use_vendor(paths)
    # pylint: disable=import-outside-toplevel,import-error
    from microwakeword.audio.audio_utils import generate_features_for_clip
    from mmap_ninja.ragged import RaggedMmap

    seed = int(stable_fraction(job["out"]) * 2**31)
    np.random.seed(seed)
    random.seed(seed)
    augmenter = make_augmenter(config, paths, job["augment"])

    def spectrograms():
        for audio in job_audio(job):
            if augmenter is not None:
                audio = augmenter.augment_clip(audio)
            features = generate_features_for_clip(audio, step_ms=STEP_MS)
            yield np.round(features / FEATURE_SCALE).astype(np.uint16)

    Path(job["out"]).parent.mkdir(parents=True, exist_ok=True)
    RaggedMmap.from_generator(
        out_dir=job["out"],
        sample_generator=spectrograms(),
        batch_size=100,
        verbose=False,
    )
    return job["out"], job["count"]


def main() -> None:
    """Build all feature sets in parallel."""

    args = parse_args()
    setup_logging(args.verbose)
    config = load_config()
    paths = Paths.from_config(config)
    only = set(args.only or ALL_SETS)
    jobs = plan_jobs(config, paths, only)
    total = sum(job["count"] for job in jobs)
    _LOGGER.info(
        "%d jobs, %d spectrograms, %d workers", len(jobs), total, args.workers
    )
    done = 0
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_job, job, config, paths) for job in jobs]
        for future in as_completed(futures):
            out, count = future.result()
            done += count
            _LOGGER.info(
                "%s: %d spectrograms (%d/%d)",
                Path(out).relative_to(paths.features),
                count,
                done,
                total,
            )


if __name__ == "__main__":
    main()
