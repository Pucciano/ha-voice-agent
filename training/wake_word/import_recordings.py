"""Cut own recordings into wake word clips and background chunks.

Positive recordings live in recordings/positive/<speaker>/, one or more files
per person with many "Hey AIVI" and short pauses in between. Negative
recordings in recordings/negative/ hold everyday sound without the wake word.
Any audio format works. The clips are regenerated on every run.

Voice activity detection finds the phrases; each is trimmed to its loud
part. Segments much quieter than the speaker are background sounds, and
segments at the edge of a recording may be cut off; neither becomes a clip.
Whisper then checks every clip for "Hey" followed by an "i", "ai" or "e"
sound. Clips that fail go to clips/real_positive/review/ instead of the
training: listen to them and list the good ones in recordings/accepted.txt.
Clip names in recordings/excluded.txt are dropped for good. clips.csv lists
every segment with its level, transcripts and verdict.

Takes recorded through Home Assistant have a JSON sidecar that lists the
satellite's wake word detections. In negative recordings the window around
each "Hey AIVI" detection is cut out, because someone may have said the wake
word, and saved to clips/real_negative/review/. A window that turns out to be
a false accept goes back into the training when its clip name is listed in
recordings/negative_keep.txt.
"""

import argparse
import csv
import json
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
from speech_check import SpeechCheck

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
CSV_FIELDS = (
    "clip",
    "verdict",
    "speaker",
    "recording",
    "start_s",
    "duration_s",
    "peak_dbfs",
    "transcript_en",
    "transcript_de",
)
# Energy frames for trimming a segment to its loud part.
TRIM_FRAME = SAMPLE_RATE // 100

_LOGGER = logging.getLogger("import")


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def audio_files(directory: Path) -> list[Path]:
    """Audio files below a directory, sorted.

    Hidden files and directories are skipped, for example the ._ files that
    macOS writes next to audio files on some drives.
    """

    return sorted(
        path
        for path in directory.rglob("*")
        if path.is_file()
        and path.suffix.lower() in AUDIO_SUFFIXES
        and not any(
            part.startswith(".") for part in path.relative_to(directory).parts
        )
    )


def name_list(path: Path) -> set[str]:
    """Clip names listed in a text file, one per line."""

    if not path.is_file():
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def voice_runs(audio: np.ndarray, settings: dict) -> list[tuple[int, int]]:
    """Voice activity runs as (start, end) samples; short gaps are closed."""

    frame = SAMPLE_RATE * settings["frame_ms"] // 1000
    pcm = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
    vad = webrtcvad.Vad(settings["vad_mode"])
    max_gap = int(settings["merge_gap_s"] * SAMPLE_RATE / frame)
    runs: list[list[int]] = []
    for index in range(len(pcm) // frame):
        chunk = pcm[index * frame : (index + 1) * frame].tobytes()
        if not vad.is_speech(chunk, SAMPLE_RATE):
            continue
        if runs and index - runs[-1][1] <= max_gap:
            runs[-1][1] = index + 1
        else:
            runs.append([index, index + 1])
    return [(first * frame, last * frame) for first, last in runs]


def loud_part(
    audio: np.ndarray, start: int, end: int, trim_db: float
) -> tuple[int, int]:
    """The part of a run at most trim_db below its loudest 10 ms."""

    frames = (end - start) // TRIM_FRAME
    if frames == 0:
        return start, end
    chunk = audio[start : start + frames * TRIM_FRAME].reshape(frames, -1)
    level = 20.0 * np.log10(np.sqrt(np.mean(chunk**2, axis=1)) + 1e-9)
    loud = np.flatnonzero(level >= level.max() - trim_db)
    return (
        start + int(loud[0]) * TRIM_FRAME,
        start + int(loud[-1] + 1) * TRIM_FRAME,
    )


def find_segments(audio: np.ndarray, settings: dict) -> list[tuple[int, int]]:
    """Candidate phrase segments: trimmed, padded, not overlapping."""

    trimmed = [
        loud_part(audio, start, end, settings["trim_db"])
        for start, end in voice_runs(audio, settings)
    ]
    padding = int(settings["padding_s"] * SAMPLE_RATE)
    segments = []
    for number, (start, end) in enumerate(trimmed):
        low, high = start - padding, end + padding
        # Never reach into the neighbouring segment.
        if number > 0:
            low = max(low, (trimmed[number - 1][1] + start) // 2)
        if number + 1 < len(trimmed):
            high = min(high, (end + trimmed[number + 1][0]) // 2)
        segments.append((max(low, 0), min(high, len(audio))))
    return segments


def rule_out(
    audio: np.ndarray,
    segment: tuple[int, int],
    loudest: float,
    settings: dict,
) -> str:
    """Why a segment cannot be a wake word clip, or an empty string."""

    start, end = segment
    duration = (end - start) / SAMPLE_RATE
    edge = int(settings["edge_s"] * SAMPLE_RATE)
    if dbfs(audio[start:end]) < loudest - settings["noise_gap_db"]:
        return "noise"
    if start <= edge or end >= len(audio) - edge:
        return "cut off"
    if duration < settings["min_duration_s"]:
        return "too short"
    if duration > settings["max_duration_s"]:
        return "too long"
    return ""


def split_name(key: str, split: dict) -> str:
    """Stable train, validation or test assignment of a clip."""

    fraction = stable_fraction(key)
    if fraction < split["train"]:
        return "train"
    if fraction < split["train"] + split["validation"]:
        return "validation"
    return "test"


def import_recording(
    recording: Path,
    speaker: str,
    config: dict,
    check: SpeechCheck,
    lists: dict[str, set[str]],
    target: Path,
) -> list[dict]:
    """Rows for all segments of one recording; clips are written."""

    settings = config["recordings"]["segment"]
    audio = read_audio(recording)
    segments = find_segments(audio, settings)
    peaks = [dbfs(audio[start:end]) for start, end in segments]
    loudest = float(np.percentile(peaks, 90)) if peaks else 0.0
    rows, clips = [], []
    for number, segment in enumerate(segments):
        start, end = segment
        row = {
            "clip": f"{speaker}__{recording.stem}__{number:03d}.wav",
            "verdict": rule_out(audio, segment, loudest, settings),
            "speaker": speaker,
            "recording": str(recording.relative_to(REPO_ROOT)),
            "start_s": f"{start / SAMPLE_RATE:.2f}",
            "duration_s": f"{(end - start) / SAMPLE_RATE:.2f}",
            "peak_dbfs": f"{peaks[number]:.1f}",
            "transcript_en": "",
            "transcript_de": "",
        }
        rows.append(row)
        if not row["verdict"]:
            clips.append((row, audio[start:end]))
    # One Whisper model after the other: MLX keeps only one loaded.
    for row, clip in clips:
        row["transcript_en"] = check.transcribe(clip)
    for row, clip in clips:
        row["transcript_de"] = check.transcribe_second(clip)
    pattern = config["recordings"]["wake_word_pattern"]
    for row, clip in clips:
        heard = check.matches(pattern, row["transcript_en"]) or check.matches(
            pattern, row["transcript_de"]
        )
        if row["clip"] in lists["excluded"]:
            row["verdict"] = "excluded"
            continue
        if heard or row["clip"] in lists["accepted"]:
            row["verdict"] = split_name(
                row["clip"], config["recordings"]["split"]
            )
        else:
            row["verdict"] = "review"
        write_wav(target / row["verdict"] / row["clip"], clip)
    return rows


def import_positive(config: dict, paths: Paths, check: SpeechCheck) -> None:
    """Cut the wake word recordings of every speaker into clips."""

    source = paths.recordings / "positive"
    target = paths.clips / "real_positive"
    reset_directory(target)
    lists = {
        "accepted": name_list(paths.recordings / "accepted.txt"),
        "excluded": name_list(paths.recordings / "excluded.txt"),
    }
    rows: list[dict] = []
    speakers = (
        sorted(p for p in source.iterdir() if p.is_dir())
        if source.is_dir()
        else []
    )
    for speaker_dir in speakers:
        for recording in audio_files(speaker_dir):
            recording_rows = import_recording(
                recording, speaker_dir.name, config, check, lists, target
            )
            verdicts: dict[str, int] = {}
            for row in recording_rows:
                verdicts[row["verdict"]] = verdicts.get(row["verdict"], 0) + 1
            _LOGGER.info(
                "%s/%s: %s",
                speaker_dir.name,
                recording.name,
                ", ".join(f"{v} {n}" for v, n in sorted(verdicts.items())),
            )
            rows.extend(recording_rows)
    if not rows:
        _LOGGER.warning("No wake word recordings in %s", source)
        return
    with (target / "clips.csv").open("w", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    review = [row for row in rows if row["verdict"] == "review"]
    for row in review:
        _LOGGER.info(
            "review %s: %r / %r",
            row["clip"],
            row["transcript_en"],
            row["transcript_de"],
        )
    used = sum(
        row["verdict"] in ("train", "validation", "test") for row in rows
    )
    _LOGGER.info(
        "%d wake word clips, %d to review in %s",
        used,
        len(review),
        (target / "review").relative_to(REPO_ROOT),
    )


def wake_word_windows(
    recording: Path, name: str, length: int, settings: dict
) -> list[tuple[int, int, str]]:
    """Sample ranges around the wake word detections in a take's sidecar.

    Returns (start, end, review clip name) sorted by start; empty for
    recordings without a sidecar, such as those from the Mac recorder.
    """

    sidecar = recording.with_suffix(".json")
    if not sidecar.is_file():
        return []
    try:
        detections = json.loads(sidecar.read_text(encoding="utf-8"))
        detections = detections.get("detections") or []
    except (ValueError, AttributeError):
        _LOGGER.warning("Unreadable sidecar %s; nothing cut", sidecar.name)
        return []
    names = set(settings["negative_cut_wake_words"])
    before, after = settings["negative_cut_window_s"]
    windows = []
    for item in detections:
        if not isinstance(item, dict) or item.get("name") not in names:
            continue
        offset = item.get("offset_s")
        if isinstance(offset, bool) or not isinstance(offset, float | int):
            continue
        start = max(0, int((offset - before) * SAMPLE_RATE))
        end = min(length, int((offset + after) * SAMPLE_RATE))
        if start < end:
            clip = f"{name}__wake_{round(offset * 1000):08d}.wav"
            windows.append((start, end, clip))
    return sorted(windows)


def outside(
    length: int, windows: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """The parts of [0, length) not covered by the sorted windows."""

    pieces = []
    position = 0
    for start, end in windows:
        if start > position:
            pieces.append((position, start))
        position = max(position, end)
    if position < length:
        pieces.append((position, length))
    return pieces


def within(
    pieces: list[tuple[int, int]], low: int, high: int
) -> list[tuple[int, int]]:
    """The pieces limited to [low, high), empty ones dropped."""

    return [
        (max(start, low), min(end, high))
        for start, end in pieces
        if min(end, high) > max(start, low)
    ]


def write_train(
    target: Path,
    name: str,
    audio: np.ndarray,
    pieces: list[tuple[int, int]],
    chunk: int,
) -> float:
    """Training chunks from the pieces; returns their seconds.

    A chunk never spans two pieces, so none joins the sides of a cut window.
    """

    starts = [
        offset
        for start, end in pieces
        for offset in range(start, end - chunk + 1, chunk)
    ]
    for number, offset in enumerate(starts):
        write_wav(
            target / "train" / f"{name}__{number:04d}.wav",
            audio[offset : offset + chunk],
        )
    return len(starts) * chunk / SAMPLE_RATE


def write_held_out(
    target: Path, name: str, audio: np.ndarray, pieces: list[tuple[int, int]]
) -> float:
    """One track per uninterrupted piece for streaming evaluation."""

    for index, (start, end) in enumerate(pieces):
        part = "" if len(pieces) == 1 else f"__part{index + 1}"
        write_wav(target / "test" / f"{name}{part}.wav", audio[start:end])
    return sum(end - start for start, end in pieces) / SAMPLE_RATE


def split_negative(
    recording: Path, source: Path, target: Path, settings: dict, keep: set
) -> dict[str, float]:
    """Review windows, training chunks and held-out tracks of a recording.

    Returns the number of cut windows and the seconds written per kind.
    """

    audio = read_audio(recording)
    name = recording.relative_to(source).with_suffix("").as_posix()
    name = name.replace("/", "__")
    windows = [
        window
        for window in wake_word_windows(recording, name, len(audio), settings)
        if window[2] not in keep
    ]
    for start, end, clip in windows:
        write_wav(target / "review" / clip, audio[start:end])
    pieces = outside(len(audio), [window[:2] for window in windows])
    cut = int(len(audio) * (1.0 - settings["negative_test_fraction"]))
    chunk = int(settings["negative_chunk_s"] * SAMPLE_RATE)
    return {
        "windows": len(windows),
        "cut": sum(end - start for start, end, _ in windows) / SAMPLE_RATE,
        "train": write_train(
            target, name, audio, within(pieces, 0, cut), chunk
        ),
        "test": write_held_out(
            target, name, audio, within(pieces, cut, len(audio))
        ),
    }


def import_negative(config: dict, paths: Paths) -> None:
    """Cut everyday recordings into training chunks and held-out tracks."""

    source = paths.recordings / "negative"
    target = paths.clips / "real_negative"
    reset_directory(target)
    keep = name_list(paths.recordings / "negative_keep.txt")
    totals = {"windows": 0.0, "cut": 0.0, "train": 0.0, "test": 0.0}
    for recording in audio_files(source) if source.is_dir() else []:
        result = split_negative(
            recording, source, target, config["recordings"], keep
        )
        for key, value in result.items():
            totals[key] += value
    _LOGGER.info(
        "Negative recordings: %.1f min for training, %.1f min held out",
        totals["train"] / 60,
        totals["test"] / 60,
    )
    if totals["windows"]:
        _LOGGER.info(
            "Cut %d wake word windows (%.1f s) from negative recordings; "
            "listen to them in %s and list false accepts in "
            "recordings/negative_keep.txt",
            totals["windows"],
            totals["cut"],
            (target / "review").relative_to(REPO_ROOT),
        )


def main() -> None:
    """Import positive and negative recordings."""

    args = parse_args()
    setup_logging(args.verbose)
    config = load_config()
    paths = Paths.from_config(config)
    import_positive(
        config, paths, SpeechCheck(config["samples"]["check"], paths)
    )
    import_negative(config, paths)


if __name__ == "__main__":
    main()
