"""Cut own recordings into wake word clips and background chunks.

Positive recordings live in recordings/positive/<speaker>/, one or more files
per person with many "Hey AIVI" and short pauses in between. Negative
recordings in recordings/negative/ hold everyday sound without the wake word.
Any audio format works. The clips are regenerated on every run; list clip
names in recordings/excluded.txt to drop badly cut ones for good.
"""

import argparse
import csv
import logging
from pathlib import Path

import numpy as np
import webrtcvad

from common import (
    REPO_ROOT,
    SAMPLE_RATE,
    Paths,
    dbfs,
    load_config,
    read_audio,
    reset_directory,
    setup_logging,
    stable_fraction,
    write_wav,
)

AUDIO_SUFFIXES = {
    ".aac",
    ".aif",
    ".aiff",
    ".caf",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
}

_LOGGER = logging.getLogger("import")


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def audio_files(directory: Path) -> list[Path]:
    """Audio files below a directory, sorted."""

    return sorted(
        path
        for path in directory.rglob("*")
        if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES
    )


def find_segments(audio: np.ndarray, settings: dict) -> list[tuple[int, int]]:
    """Voice activity segments as (start, end) sample indices."""

    frame = SAMPLE_RATE * settings["frame_ms"] // 1000
    pcm = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
    vad = webrtcvad.Vad(settings["vad_mode"])
    frames = len(pcm) // frame
    speech = [
        vad.is_speech(pcm[i * frame : (i + 1) * frame].tobytes(), SAMPLE_RATE)
        for i in range(frames)
    ]

    # Runs of speech frames; short gaps (the pause in "Hey, AIVI") are closed.
    max_gap = int(settings["merge_gap_s"] * SAMPLE_RATE / frame)
    runs: list[list[int]] = []
    for index, active in enumerate(speech):
        if not active:
            continue
        if runs and index - runs[-1][1] <= max_gap:
            runs[-1][1] = index + 1
        else:
            runs.append([index, index + 1])

    padding = int(settings["padding_s"] * SAMPLE_RATE)
    segments = []
    for number, (first, last) in enumerate(runs):
        start = first * frame - padding
        end = last * frame + padding
        # Never reach into the neighbouring segment.
        if number > 0:
            start = max(
                start, (runs[number - 1][1] * frame + first * frame) // 2
            )
        if number + 1 < len(runs):
            end = min(end, (last * frame + runs[number + 1][0] * frame) // 2)
        start, end = max(start, 0), min(end, len(audio))
        duration = (end - start) / SAMPLE_RATE
        if settings["min_duration_s"] <= duration <= settings["max_duration_s"]:
            segments.append((start, end))
    return segments


def split_name(key: str, split: dict) -> str:
    """Stable train, validation or test assignment of a clip."""

    fraction = stable_fraction(key)
    if fraction < split["train"]:
        return "train"
    if fraction < split["train"] + split["validation"]:
        return "validation"
    return "test"


def import_positive(config: dict, paths: Paths, excluded: set[str]) -> None:
    """Cut the wake word recordings of every speaker into clips."""

    source = paths.recordings / "positive"
    target = paths.clips / "real_positive"
    reset_directory(target)
    rows = []
    counts: dict[str, dict[str, int]] = {}
    speakers = (
        sorted(p for p in source.iterdir() if p.is_dir())
        if source.is_dir()
        else []
    )
    for speaker_dir in speakers:
        speaker = speaker_dir.name
        for recording in audio_files(speaker_dir):
            audio = read_audio(recording)
            segments = find_segments(audio, config["recordings"]["segment"])
            _LOGGER.info(
                "%s/%s: %d clips from %.0f s",
                speaker,
                recording.name,
                len(segments),
                len(audio) / SAMPLE_RATE,
            )
            for number, (start, end) in enumerate(segments):
                name = f"{speaker}__{recording.stem}__{number:03d}.wav"
                if name in excluded:
                    continue
                split = split_name(name, config["recordings"]["split"])
                clip = audio[start:end]
                write_wav(target / split / name, clip)
                counts.setdefault(
                    speaker, {"train": 0, "validation": 0, "test": 0}
                )
                counts[speaker][split] += 1
                rows.append(
                    {
                        "clip": name,
                        "split": split,
                        "speaker": speaker,
                        "recording": str(recording.relative_to(REPO_ROOT)),
                        "start_s": f"{start / SAMPLE_RATE:.2f}",
                        "duration_s": f"{(end - start) / SAMPLE_RATE:.2f}",
                        "peak_dbfs": f"{dbfs(clip):.1f}",
                    }
                )
    if rows:
        with (target / "clips.csv").open(
            "w", newline="", encoding="utf-8"
        ) as out:
            writer = csv.DictWriter(out, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    for speaker, split_counts in counts.items():
        _LOGGER.info("%s: %s", speaker, split_counts)
    if not rows:
        _LOGGER.warning("No wake word recordings in %s", source)


def import_negative(config: dict, paths: Paths) -> None:
    """Cut everyday recordings into training chunks and held-out tracks."""

    source = paths.recordings / "negative"
    target = paths.clips / "real_negative"
    reset_directory(target)
    chunk = int(config["recordings"]["negative_chunk_s"] * SAMPLE_RATE)
    test_fraction = config["recordings"]["negative_test_fraction"]
    totals = {"train": 0.0, "test": 0.0}
    recordings = audio_files(source) if source.is_dir() else []
    for recording in recordings:
        audio = read_audio(recording)
        cut = int(len(audio) * (1.0 - test_fraction))
        name = recording.relative_to(source).with_suffix("").as_posix()
        name = name.replace("/", "__")
        for number, start in enumerate(range(0, cut - chunk + 1, chunk)):
            write_wav(
                target / "train" / f"{name}__{number:04d}.wav",
                audio[start : start + chunk],
            )
            totals["train"] += chunk / SAMPLE_RATE
        # The held-out part stays one track for streaming evaluation.
        write_wav(target / "test" / f"{name}.wav", audio[cut:])
        totals["test"] += (len(audio) - cut) / SAMPLE_RATE
    _LOGGER.info(
        "Negative recordings: %.1f min for training, %.1f min held out",
        totals["train"] / 60,
        totals["test"] / 60,
    )


def main() -> None:
    """Import positive and negative recordings."""

    args = parse_args()
    setup_logging(args.verbose)
    config = load_config()
    paths = Paths.from_config(config)
    excluded_file = paths.recordings / "excluded.txt"
    excluded = set()
    if excluded_file.is_file():
        excluded = {
            line.strip()
            for line in excluded_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        }
    import_positive(config, paths, excluded)
    import_negative(config, paths)


if __name__ == "__main__":
    main()
