"""Build a manifest from STT captured samples for adaptation workflows."""

import argparse
import json
import pathlib


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(
        description="Build STT manifest from dev/datasets/stt structure."
    )
    parser.add_argument("--input-dir", required=True, help="STT dataset root")
    parser.add_argument("--output", required=True, help="Output manifest JSONL")
    parser.add_argument(
        "--include-unverified",
        action="store_true",
        help=(
            "Also include samples whose transcript has not been reviewed, "
            "for review or evaluation only, never for training"
        ),
    )
    return parser.parse_args()


def main() -> None:
    """Generate a simple STT adaptation manifest."""

    args = parse_args()
    input_dir = pathlib.Path(args.input_dir)
    output_path = pathlib.Path(args.output)

    if not input_dir.exists():
        raise FileNotFoundError(f"Input directory does not exist: {input_dir}")

    samples = []
    skipped_unverified = 0
    for sample_dir in sorted(input_dir.iterdir()):
        # Dot directories are unfinished writes of the Jetson capture.
        if not sample_dir.is_dir() or sample_dir.name.startswith("."):
            continue
        transcript_path = sample_dir / "transcript.txt"
        metadata_path = sample_dir / "metadata.json"

        audio_candidates = [
            path
            for path in sample_dir.iterdir()
            if path.is_file() and path.name.startswith("audio")
        ]
        if not audio_candidates or not transcript_path.exists():
            continue

        transcript = transcript_path.read_text(encoding="utf-8").strip()
        metadata = {}
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                metadata = {}

        # Captured transcripts are pseudo labels of the recogniser. Only a
        # reviewed transcript (verified: true) may serve as a training label.
        if metadata.get("verified") is not True and not args.include_unverified:
            skipped_unverified += 1
            continue

        samples.append(
            {
                "audio": str(audio_candidates[0]),
                "text": transcript,
                "metadata": metadata,
            }
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for sample in samples:
            handle.write(json.dumps(sample, ensure_ascii=False) + "\n")

    print(f"Wrote {len(samples)} STT manifest rows to {output_path}")
    if skipped_unverified:
        print(
            f"Skipped {skipped_unverified} unverified samples "
            "(see --include-unverified)"
        )


if __name__ == "__main__":
    main()
