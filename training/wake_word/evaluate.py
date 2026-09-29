"""Measure a trained model the way the satellite runs it; write the manifest.

Detection works as on the device: the mean of the last sliding_window_size
probabilities must exceed the cutoff, then a pause (cooldown_s) follows.

- Recall: held-out household clips (never trained on), each played after
  lead_in_s of held-out everyday audio, and held-out synthetic clips.
- False accepts per hour: held-out household recordings and the DiPCo dinner
  party test set that okay_nabu's cutoffs were chosen with.

The report suggests one cutoff per sensitivity level in the satellite's
Wake-Word-Empfindlichkeit select, following okay_nabu: the lowest cutoff
with 0, at most 0.375 and at most 0.75 false accepts per hour on DiPCo. Each
must also keep household false accepts at or below the configured limit.
"""

import argparse
import datetime
import json
import logging
import random
from pathlib import Path

import numpy as np
import soundfile
from mmap_ninja.ragged import RaggedMmap

from common import (
    SAMPLE_RATE,
    Paths,
    load_config,
    setup_logging,
    stable_fraction,
    use_vendor,
)

CUTOFFS = np.round(np.arange(0.50, 1.00, 0.01), 2)
# Seconds per model output: stride 3 x 10 ms feature step.
OUTPUT_STEP_S = 0.03
TRAILING_SILENCE_S = 0.5
MAX_SYNTHETIC_CLIPS = 500
LEVELS = (
    ("Wenig empfindlich", 0.0),
    ("Mittel empfindlich", 0.375),
    ("Sehr empfindlich", 0.75),
)

_LOGGER = logging.getLogger("evaluate")


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--run", help="Run name from train.py (default: the latest run)"
    )
    parser.add_argument(
        "--model",
        type=Path,
        help="Evaluate this .tflite instead of the run's model",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


class Detector:
    """Streams audio or features through a fresh copy of the model."""

    def __init__(self, model_path: Path, window: int, cooldown_s: float):
        self.model_path = model_path
        self.window = window
        self.cooldown = int(round(cooldown_s / OUTPUT_STEP_S))

    def _model(self):
        # pylint: disable=import-outside-toplevel,import-error
        from microwakeword.inference import Model

        return Model(str(self.model_path))

    def averages(
        self, audio: np.ndarray | None = None, features=None
    ) -> np.ndarray:
        """Sliding mean probabilities; entry i ends at model output i."""

        model = self._model()
        if features is not None:
            outputs = model.predict_spectrogram(features)
        else:
            pcm = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
            outputs = model.predict_clip(pcm, step_ms=10)
        outputs = np.asarray(outputs, dtype=np.float32)
        if len(outputs) < self.window:
            return np.zeros(0, dtype=np.float32)
        kernel = np.ones(self.window, dtype=np.float32) / self.window
        return np.convolve(outputs, kernel, mode="valid")

    def count_detections(self, averages: np.ndarray) -> np.ndarray:
        """Detections per cutoff, with the cooldown after each one."""

        counts = np.zeros(len(CUTOFFS), dtype=np.int64)
        for index, cutoff in enumerate(CUTOFFS):
            above = np.flatnonzero(averages > cutoff)
            last = -self.cooldown - 1
            for position in above:
                if position - last > self.cooldown:
                    counts[index] += 1
                    last = position
        return counts


def positive_clips(paths: Paths) -> dict[str, list[Path]]:
    """Held-out positive clips by source: household speakers and synthetic."""

    groups: dict[str, list[Path]] = {}
    for clip in sorted((paths.clips / "real_positive" / "test").glob("*.wav")):
        groups.setdefault(clip.name.split("__")[0], []).append(clip)
    synthetic = [
        clip
        for clip in sorted((paths.clips / "tts_positive").glob("*.wav"))
        if stable_fraction(clip.name) >= 0.9
    ]
    if synthetic:
        random.Random(0).shuffle(synthetic)
        groups["synthetic"] = synthetic[:MAX_SYNTHETIC_CLIPS]
    return groups


def recall(
    detector: Detector,
    clips: list[Path],
    lead_in: list[np.ndarray],
    lead_in_s: float,
) -> np.ndarray:
    """Fraction of clips detected at every cutoff."""

    lead_samples = int(lead_in_s * SAMPLE_RATE)
    rng = np.random.default_rng(0)
    peaks = []
    for clip_path in clips:
        clip, _ = soundfile.read(str(clip_path), dtype="float32")
        context = np.zeros(lead_samples, dtype=np.float32)
        if lead_in:
            track = lead_in[int(rng.integers(len(lead_in)))]
            if len(track) > lead_samples:
                start = int(rng.integers(len(track) - lead_samples))
                context = track[start : start + lead_samples]
        audio = np.concatenate(
            [
                context,
                clip,
                np.zeros(int(TRAILING_SILENCE_S * SAMPLE_RATE), np.float32),
            ]
        )
        averages = detector.averages(audio)
        # Only count detections after the clip has started.
        first = max(int(lead_in_s / OUTPUT_STEP_S) - detector.window, 0)
        peaks.append(float(averages[first:].max(initial=0.0)))
    peaks_array = np.asarray(peaks)
    return np.array([(peaks_array > cutoff).mean() for cutoff in CUTOFFS])


def household_false_accepts(
    detector: Detector, tracks: list[np.ndarray]
) -> tuple[np.ndarray, float]:
    """False accepts per hour on held-out everyday recordings."""

    hours = sum(len(track) for track in tracks) / SAMPLE_RATE / 3600
    counts = np.zeros(len(CUTOFFS), dtype=np.int64)
    for track in tracks:
        counts += detector.count_detections(detector.averages(track))
    return (counts / hours if hours else counts.astype(float)), hours


def dipco_false_accepts(
    detector: Detector, paths: Paths
) -> tuple[np.ndarray, float]:
    """False accepts per hour on the DiPCo dinner party test set."""

    counts = np.zeros(len(CUTOFFS), dtype=np.int64)
    frames = 0
    ambient = paths.negative_features / "dinner_party_eval" / "testing_ambient"
    for mmap in sorted(ambient.glob("*_mmap")):
        tracks = RaggedMmap(str(mmap))
        # RaggedMmap has no iterator.
        for index in range(len(tracks)):  # pylint: disable=C0200
            features = tracks[index]
            frames += len(features)
            counts += detector.count_detections(
                detector.averages(features=features)
            )
    hours = frames * 0.01 / 3600
    return (counts / hours if hours else counts.astype(float)), hours


def suggest_cutoffs(
    dipco: np.ndarray,
    household: np.ndarray,
    household_hours: float,
    limit: float,
) -> dict[str, float | None]:
    """Lowest cutoff per sensitivity level within the false accept limits."""

    suggestions: dict[str, float | None] = {}
    for label, dipco_limit in LEVELS:
        allowed = dipco <= dipco_limit
        if household_hours > 0:
            allowed &= household <= limit
        indices = np.flatnonzero(allowed)
        suggestions[label] = (
            float(CUTOFFS[indices[0]]) if indices.size else None
        )
    return suggestions


def write_report(path: Path, results: dict) -> None:
    """Markdown report with the cutoff table and the suggestions."""

    lines = [
        f"# {results['name']} evaluation, run {results['run']}",
        "",
        f"Model: `{results['model']}`",
        "",
        "Held-out data:",
        "",
    ]
    for group, count in results["positives"].items():
        lines.append(f"- {group}: {count} wake word clips")
    lines += [
        f"- household recordings: {results['household_hours']:.2f} h",
        f"- DiPCo dinner party: {results['dipco_hours']:.2f} h",
        "",
        "## Suggested cutoffs",
        "",
    ]
    for label, cutoff in results["suggestions"].items():
        lines.append(
            f"- {label}: "
            + (
                f"{cutoff:.2f} (uint8 {round(cutoff * 255)})"
                if cutoff
                else "none"
            )
        )
    if results["household_hours"] < 1.0:
        lines += [
            "",
            "Less than 1 h of household recordings: the household false "
            "accept rate is a rough estimate. Record more everyday sound.",
        ]
    groups = list(results["positives"])
    lines += [
        "",
        "## Cutoffs",
        "",
        "| cutoff | "
        + " | ".join(f"recall {g}" for g in groups)
        + " | FA/h household | FA/h DiPCo |",
        "|---|" + "---|" * (len(groups) + 2),
    ]
    for index, cutoff in enumerate(CUTOFFS):
        recalls = " | ".join(
            f"{results['recall'][g][index]:.3f}" for g in groups
        )
        household = results["household"][index]
        dipco = results["dipco"][index]
        lines.append(
            f"| {cutoff:.2f} | {recalls} | {household:.2f} | {dipco:.2f} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_manifest(
    path: Path, config: dict, model_name: str, cutoff: float
) -> None:
    """ESPHome model manifest (version 2) next to the model."""

    manifest = config["manifest"]
    content = {
        "type": "micro",
        "wake_word": config["wake_word"]["name"],
        "author": manifest["author"],
        "website": manifest["website"],
        "model": model_name,
        "trained_languages": config["wake_word"]["languages"],
        "version": 2,
        "micro": {
            "probability_cutoff": cutoff,
            "feature_step_size": manifest["feature_step_size"],
            "sliding_window_size": manifest["sliding_window_size"],
            "tensor_arena_size": manifest["tensor_arena_size"],
            "minimum_esphome_version": manifest["minimum_esphome_version"],
        },
    }
    path.write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    """Evaluate, report and write the manifest."""

    args = parse_args()
    setup_logging(args.verbose)
    config = load_config()
    paths = Paths.from_config(config)
    use_vendor(paths)
    settings = config["evaluation"]
    name = config["wake_word"]["id"]
    if not args.run:
        models = sorted(
            (paths.models / name).glob(f"*/{name}.tflite"),
            key=lambda model: model.stat().st_mtime,
        )
        if not models:
            raise SystemExit("No trained model yet; run `make ww-train` first.")
        args.run = models[-1].parent.name
    run_dir = paths.models / name / args.run
    model_path = args.model or run_dir / f"{name}.tflite"
    if not model_path.is_file():
        raise SystemExit(
            f"No model at {model_path}; run `make ww-train` first."
        )
    detector = Detector(
        model_path,
        config["manifest"]["sliding_window_size"],
        settings["cooldown_s"],
    )

    household_tracks = [
        soundfile.read(str(track), dtype="float32")[0]
        for track in sorted(
            (paths.clips / "real_negative" / "test").glob("*.wav")
        )
    ]
    positives = positive_clips(paths)
    recalls = {}
    for group, clips in positives.items():
        recalls[group] = recall(
            detector, clips, household_tracks, settings["lead_in_s"]
        )
        _LOGGER.info(
            "%s: recall at cutoff 0.85 = %.3f",
            group,
            recalls[group][int(np.argmin(np.abs(CUTOFFS - 0.85)))],
        )
    household, household_hours = household_false_accepts(
        detector, household_tracks
    )
    dipco, dipco_hours = dipco_false_accepts(detector, paths)
    _LOGGER.info(
        "False accepts measured on %.2f h household, %.2f h DiPCo",
        household_hours,
        dipco_hours,
    )

    suggestions = suggest_cutoffs(
        dipco,
        household,
        household_hours,
        settings["max_false_accepts_per_hour"],
    )
    results = {
        "name": config["wake_word"]["name"],
        "run": args.run,
        "model": model_path.name,
        "positives": {group: len(clips) for group, clips in positives.items()},
        "recall": recalls,
        "household": household,
        "household_hours": household_hours,
        "dipco": dipco,
        "dipco_hours": dipco_hours,
        "suggestions": suggestions,
    }
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    report = run_dir / f"evaluation_{stamp}.md"
    write_report(report, results)
    for label, cutoff in suggestions.items():
        _LOGGER.info("%s: %s", label, f"{cutoff:.2f}" if cutoff else "none")
    default = suggestions[LEVELS[0][0]] or 0.97
    if args.model is None:
        write_manifest(
            run_dir / f"{name}.json", config, f"{name}.tflite", default
        )
        _LOGGER.info(
            "Manifest %s with cutoff %.2f", run_dir / f"{name}.json", default
        )
    _LOGGER.info("Report %s", report)


if __name__ == "__main__":
    main()
