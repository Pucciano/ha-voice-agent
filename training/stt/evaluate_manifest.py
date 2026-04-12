"""Evaluate STT manifest coverage and basic transcript statistics."""

import argparse
import json
import pathlib
import statistics


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(
        description="Compute simple STT dataset quality metrics."
    )
    parser.add_argument("--manifest", required=True, help="Manifest JSONL file")
    return parser.parse_args()


def main() -> None:
    """Load manifest and print summary statistics."""

    args = parse_args()
    manifest_path = pathlib.Path(args.manifest)
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"Manifest file does not exist: {manifest_path}"
        )

    rows = []
    with manifest_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)

    text_lengths = []
    missing_audio = 0
    for row in rows:
        transcript = row.get("text", "")
        if isinstance(transcript, str):
            text_lengths.append(len(transcript.split()))
        audio_path = row.get("audio")
        if (
            not isinstance(audio_path, str)
            or not pathlib.Path(audio_path).exists()
        ):
            missing_audio += 1

    metrics = {
        "rows": len(rows),
        "missing_audio": missing_audio,
        "avg_words": round(statistics.mean(text_lengths), 3)
        if text_lengths
        else 0.0,
        "median_words": round(statistics.median(text_lengths), 3)
        if text_lengths
        else 0.0,
    }

    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
