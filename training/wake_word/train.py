"""Train the wake word model with microWakeWord.

Writes microWakeWord's training_parameters.yaml from config.yaml, trains
(resuming an interrupted run with the same name), and converts the best
weights to the quantized streaming TFLite model the satellite runs. The
probability cutoff for the manifest comes from evaluate.py.
"""

import argparse
import datetime
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from common import (
    CONFIG_PATH,
    Paths,
    load_config,
    setup_logging,
    vendor_pythonpath,
)

# Training shifts every stored phrase spectrogram by 0-9 feature windows,
# the same as ten sliding copies (see build_features.py).
RIGHT_CUTOFFS = list(range(10))
STREAMING_MODEL = Path(
    "tflite_stream_state_internal_quant", "stream_state_internal_quant.tflite"
)

_LOGGER = logging.getLogger("train")


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--run",
        default=datetime.datetime.now().strftime("%Y%m%d-%H%M"),
        help="Run name; an existing run resumes (default: current time)",
    )
    parser.add_argument(
        "--steps",
        type=int,
        nargs="+",
        help="Override the training steps per phase, e.g. for a smoke test",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=CONFIG_PATH,
        help="Configuration to train with, e.g. a copy for an experiment",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only write and check the training parameters",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def feature_entries(config: dict, paths: Paths) -> list[dict]:
    """microWakeWord feature sets for every set that exists on disk."""

    locations = {
        "tts_positive": (paths.features / "tts_positive", "fixed_right_cutoff"),
        "tts_negative": (paths.features / "tts_negative", "fixed_right_cutoff"),
        "real_positive": (
            paths.features / "real_positive",
            "fixed_right_cutoff",
        ),
        "real_negative": (paths.features / "real_negative", "random"),
        "speech": (paths.negative_features / "speech", "random"),
        "dinner_party": (paths.negative_features / "dinner_party", "random"),
        "no_speech": (paths.negative_features / "no_speech", "random"),
        "german_speech": (paths.features / "german_speech", "random"),
        "dinner_party_eval": (
            paths.negative_features / "dinner_party_eval",
            "split",
        ),
    }
    entries = []
    for name, settings in config["features"]["sets"].items():
        directory, truncation = locations[name]
        if not any(directory.glob("*/*_mmap")):
            _LOGGER.warning("Skipping %s: no features in %s", name, directory)
            continue
        entry = {
            "features_dir": str(directory),
            "sampling_weight": settings["sampling_weight"],
            "penalty_weight": settings["penalty_weight"],
            "truth": settings["truth"],
            "truncation_strategy": truncation,
            "type": "mmap",
        }
        if truncation == "fixed_right_cutoff":
            entry["fixed_right_cutoffs"] = RIGHT_CUTOFFS
        entries.append(entry)
    if not any(entry["truth"] for entry in entries):
        raise SystemExit("No positive features; run `make ww-features` first.")
    return entries


def training_parameters(
    config: dict, paths: Paths, train_dir: Path, steps: list[int] | None
) -> dict:
    """The training configuration microWakeWord reads."""

    training = config["training"]
    phases = steps or training["steps"]

    def per_phase(key: str) -> list:
        values = training[key]
        if len(values) != len(training["steps"]):
            raise SystemExit(f"training.{key} needs one value per phase")
        return values[: len(phases)]

    return {
        "window_step_ms": config["manifest"]["feature_step_size"],
        "train_dir": str(train_dir),
        "features": feature_entries(config, paths),
        "training_steps": phases,
        "learning_rates": per_phase("learning_rates"),
        "positive_class_weight": per_phase("positive_class_weight"),
        "negative_class_weight": per_phase("negative_class_weight"),
        "time_mask_max_size": per_phase("time_mask_max_size"),
        "time_mask_count": per_phase("time_mask_count"),
        "freq_mask_max_size": per_phase("freq_mask_max_size"),
        "freq_mask_count": per_phase("freq_mask_count"),
        "batch_size": training["batch_size"],
        "eval_step_interval": training["eval_step_interval"],
        "clip_duration_ms": training["clip_duration_ms"],
        "target_minimization": training["target_minimization"],
        "minimization_metric": training["minimization_metric"],
        "maximization_metric": training["maximization_metric"],
    }


def main() -> None:
    """Train, convert and copy the streaming model into the run directory."""

    args = parse_args()
    setup_logging(args.verbose)
    config = load_config(args.config)
    paths = Paths.from_config(config)
    run_dir = paths.models / config["wake_word"]["id"] / args.run
    run_dir.mkdir(parents=True, exist_ok=True)
    # The run keeps the configuration it was trained with.
    shutil.copyfile(args.config, run_dir / "config.yaml")
    parameters = training_parameters(
        config, paths, run_dir / "model", args.steps
    )
    parameters_path = run_dir / "training_parameters.yaml"
    with parameters_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(parameters, handle, sort_keys=False)
    for entry in parameters["features"]:
        _LOGGER.info(
            "%-18s truth=%-5s weight=%s",
            Path(entry["features_dir"]).name,
            entry["truth"],
            entry["sampling_weight"],
        )
    _LOGGER.info("Parameters in %s", parameters_path)
    if args.dry_run:
        return

    command = [
        sys.executable,
        "-m",
        "microwakeword.model_train_eval",
        f"--training_config={parameters_path}",
        "--train=1",
        "--restore_checkpoint=1",
        "--test_tf_nonstreaming=0",
        "--test_tflite_nonstreaming=0",
        "--test_tflite_nonstreaming_quantized=0",
        "--test_tflite_streaming=0",
        "--test_tflite_streaming_quantized=1",
        "--use_weights=best_weights",
        *config["training"]["model"],
    ]
    env = dict(os.environ, PYTHONPATH=vendor_pythonpath(paths))
    subprocess.run(command, check=True, env=env, cwd=run_dir)

    model = run_dir / f"{config['wake_word']['id']}.tflite"
    shutil.copyfile(run_dir / "model" / STREAMING_MODEL, model)
    _LOGGER.info("Streaming model: %s (%d bytes)", model, model.stat().st_size)
    _LOGGER.info("Next: make ww-evaluate RUN=%s", args.run)


if __name__ == "__main__":
    main()
