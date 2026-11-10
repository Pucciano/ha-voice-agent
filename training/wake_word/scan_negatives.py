"""Search everyday recordings for wake words the satellite did not detect.

Everyday takes become negative training data, so a "Hey AIVI" in them that
the satellite missed would teach the model to ignore its own wake word. The
sidecar only lists the detections; this scan finds the rest in two ways:

- Whisper transcribes every take with word timestamps, and every word that
  sounds like "Hey AIVI" ("Hey Ivy", "Hey, I.B.", "AIVI", "Hey Baby") is a
  candidate;
- the deployed model runs over the take, and every peak of its sliding mean
  above --cutoff is a candidate, even far below the satellite's own cutoff.

Candidates near a detection of the sidecar are skipped: the review page
lists those already. The result, review/candidates.json, feeds the review
page (`make ww-review`). A candidate rated as a real wake word is cut from
the recording on the next import, like a detection.
"""

import argparse
import datetime
import json
import logging
import os
import re
from pathlib import Path

import numpy as np

from common import (
    REPO_ROOT,
    SAMPLE_RATE,
    Paths,
    load_config,
    read_audio,
    setup_logging,
    use_vendor,
)
from evaluate import OUTPUT_STEP_S, Detector

# Words Whisper writes for "Hey AIVI", spoken "Ei-wi"; lower case, letters
# only. The first group needs a "Hey" in front, the second stands alone.
AFTER_HEY = re.compile(r"^(iv|aiv|eiv|ib|abi|baby|ivy|ivi|ai)")
ALONE = re.compile(r"^(ivy|ivie|aivi|eivi|ivi|aiwi|eiwi)$")
HEY = re.compile(r"^(hey|hei|hay|ey|he)$")
# Candidates closer than this to each other or to a detection are merged.
MERGE_S = 2.0

_LOGGER = logging.getLogger("scan")


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--since",
        type=datetime.date.fromisoformat,
        help="Only takes recorded on or after this day (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=REPO_ROOT / "esphome" / "models" / "hey_aivi.tflite",
        help="Model to run (default: the one on the satellite)",
    )
    parser.add_argument(
        "--cutoff",
        type=float,
        default=0.5,
        help="Report model peaks above this sliding mean",
    )
    parser.add_argument(
        "--whisper",
        default="mlx-community/whisper-small-mlx",
        help="MLX Whisper model for the transcripts",
    )
    parser.add_argument("--language", default="de")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def take_start(recording: Path) -> datetime.datetime | None:
    """Start time from the sidecar; None for takes without one."""

    try:
        sidecar = json.loads(
            recording.with_suffix(".json").read_text(encoding="utf-8")
        )
        return datetime.datetime.fromisoformat(sidecar["started_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def detections(recording: Path) -> list[float]:
    """Offsets of the detections listed in the sidecar."""

    try:
        sidecar = json.loads(
            recording.with_suffix(".json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return []
    return [
        float(item["offset_s"])
        for item in sidecar.get("detections") or []
        if isinstance(item, dict) and "offset_s" in item
    ]


def letters(word: str) -> str:
    """A transcript word in lower case, letters only."""

    return re.sub(r"[^a-zäöüß]", "", word.lower())


def whisper_candidates(
    audio: np.ndarray, model: str, language: str
) -> list[dict]:
    """Words that sound like the wake word, with the time they end."""

    # pylint: disable=import-outside-toplevel,import-error
    import mlx_whisper

    result = mlx_whisper.transcribe(
        audio.astype(np.float32),
        path_or_hf_repo=model,
        language=language,
        temperature=0.0,
        condition_on_previous_text=False,
        word_timestamps=True,
    )
    found = []
    for segment in result["segments"]:
        words = segment.get("words") or []
        for index, word in enumerate(words):
            text = letters(word["word"])
            previous = letters(words[index - 1]["word"]) if index else ""
            after_hey = HEY.match(previous) and AFTER_HEY.match(text)
            if ALONE.match(text) or after_hey:
                found.append(
                    {
                        "offset_s": round(float(word["end"]), 2),
                        "source": "whisper",
                        "text": segment["text"].strip(),
                    }
                )
    return found


def model_candidates(
    audio: np.ndarray, detector: Detector, cutoff: float
) -> list[dict]:
    """Peaks of the model's sliding mean above the cutoff."""

    averages = detector.averages(audio)
    found = []
    position = 0
    above = np.flatnonzero(averages > cutoff)
    while position < len(above):
        # One peak per run of outputs above the cutoff.
        end = position
        while end + 1 < len(above) and above[end + 1] - above[end] <= 1:
            end += 1
        run = above[position : end + 1]
        peak = int(run[np.argmax(averages[run])])
        # Output i ends at sample (i + window) * step.
        offset = (peak + detector.window) * OUTPUT_STEP_S
        found.append(
            {
                "offset_s": round(offset, 2),
                "source": "model",
                "score": round(float(averages[peak]), 3),
            }
        )
        position = end + 1
    return found


def merge(candidates: list[dict], skip: list[float]) -> list[dict]:
    """Candidates sorted by time, near ones joined, detections left out."""

    merged: list[dict] = []
    for candidate in sorted(candidates, key=lambda item: item["offset_s"]):
        offset = candidate["offset_s"]
        if any(abs(offset - other) < MERGE_S for other in skip):
            continue
        if merged and offset - merged[-1]["offset_s"] < MERGE_S:
            last = merged[-1]
            sources = set(last["sources"]) | {candidate["source"]}
            last["sources"] = sorted(sources)
            scores = [last.get("score"), candidate.get("score")]
            scores = [score for score in scores if score is not None]
            last["score"] = max(scores) if scores else None
            last["text"] = last.get("text") or candidate.get("text", "")
            continue
        merged.append(
            {
                "offset_s": offset,
                "sources": [candidate["source"]],
                "score": candidate.get("score"),
                "text": candidate.get("text", ""),
            }
        )
    return merged


def main() -> None:
    """Scan the everyday takes and write review/candidates.json."""

    args = parse_args()
    setup_logging(args.verbose)
    config = load_config()
    paths = Paths.from_config(config)
    use_vendor(paths)
    os.environ.setdefault("HF_HUB_CACHE", str(paths.cache / "huggingface"))
    detector = Detector(
        args.model,
        config["manifest"]["sliding_window_size"],
        config["evaluation"]["cooldown_s"],
    )
    source = paths.recordings / "negative"
    target = paths.data / "review" / "candidates.json"
    results, scanned = [], set()
    for recording in sorted(source.glob("*.wav")):
        started = take_start(recording)
        if args.since and (started is None or started.date() < args.since):
            continue
        scanned.add(recording.relative_to(source).as_posix())
        audio = read_audio(recording)
        found = whisper_candidates(audio, args.whisper, args.language)
        found += model_candidates(audio, detector, args.cutoff)
        candidates = merge(found, detections(recording))
        _LOGGER.info(
            "%s: %.0f min, %d candidates",
            recording.name,
            len(audio) / SAMPLE_RATE / 60,
            len(candidates),
        )
        for candidate in candidates:
            results.append(
                {
                    "recording": recording.relative_to(source).as_posix(),
                    "started_at": started.isoformat() if started else None,
                    **candidate,
                }
            )
    # Candidates of takes outside this run stay, so a scan of the newest
    # takes does not drop places already rated on the review page.
    if target.is_file():
        earlier = json.loads(target.read_text(encoding="utf-8"))
        results += [
            candidate
            for candidate in earlier.get("candidates", [])
            if candidate["recording"] not in scanned
        ]
        results.sort(key=lambda item: (item["recording"], item["offset_s"]))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "model": str(args.model.relative_to(REPO_ROOT))
                if args.model.is_relative_to(REPO_ROOT)
                else str(args.model),
                "cutoff": args.cutoff,
                "since": args.since.isoformat() if args.since else None,
                "candidates": results,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    _LOGGER.info("%d candidates in %s", len(results), target)


if __name__ == "__main__":
    main()
