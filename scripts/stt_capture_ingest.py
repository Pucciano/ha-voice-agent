"""Ingest STT audio/transcript pairs into the development dataset tree."""

import argparse
import datetime
import pathlib
import shutil
import uuid


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(
        description="Copy STT audio and transcript into dev/datasets/stt."
    )
    parser.add_argument("--audio", required=True, help="Path to input WAV/OGG")
    parser.add_argument(
        "--transcript",
        required=True,
        help="Transcript text for the audio sample",
    )
    parser.add_argument(
        "--speaker",
        default="unknown",
        help="Optional speaker ID",
    )
    parser.add_argument(
        "--dataset-root",
        default="dev/datasets/stt",
        help="Dataset root directory",
    )
    return parser.parse_args()


def main() -> None:
    """Ingest one STT sample into a reproducible folder structure."""

    args = parse_args()
    audio_path = pathlib.Path(args.audio)
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file does not exist: {audio_path}")

    dataset_root = pathlib.Path(args.dataset_root)
    dataset_root.mkdir(parents=True, exist_ok=True)

    sample_id = uuid.uuid4().hex
    sample_dir = dataset_root / sample_id
    sample_dir.mkdir(parents=True, exist_ok=False)

    extension = audio_path.suffix or ".wav"
    target_audio = sample_dir / f"audio{extension}"
    shutil.copy2(audio_path, target_audio)

    transcript_path = sample_dir / "transcript.txt"
    transcript_path.write_text(args.transcript + "\n", encoding="utf-8")

    metadata_path = sample_dir / "metadata.json"
    metadata = {
        "sample_id": sample_id,
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "speaker": args.speaker,
        "audio_file": target_audio.name,
        "transcript_file": transcript_path.name,
        # The transcript is given by a person, so it counts as reviewed.
        "label_source": "manual",
        "verified": True,
    }
    import json

    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"Ingested STT sample into {sample_dir}")


if __name__ == "__main__":
    main()
