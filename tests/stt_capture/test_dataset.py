"""Captured samples work with the dataset tools; only reviewed ones train."""

import json
import subprocess
import sys

from helpers import REPO

PREPARE = REPO / "training" / "stt" / "prepare_dataset.py"
INGEST = REPO / "scripts" / "stt_capture_ingest.py"


def make_sample(root, name, metadata):
    sample = root / name
    sample.mkdir(parents=True)
    (sample / "audio.wav").write_bytes(b"RIFF")
    (sample / "transcript.txt").write_text("schalte das licht ein\n")
    (sample / "metadata.json").write_text(json.dumps(metadata))
    return sample


def manifest(root, output, *extra):
    subprocess.run(
        [
            sys.executable,
            str(PREPARE),
            "--input-dir",
            str(root),
            "--output",
            str(output),
            *extra,
        ],
        check=True,
        capture_output=True,
    )
    return [json.loads(line) for line in output.read_text().splitlines()]


def test_only_verified_samples_by_default(tmp_path) -> None:
    root = tmp_path / "stt"
    make_sample(root, "a-reviewed", {"verified": True})
    make_sample(root, "b-captured", {"verified": False})
    make_sample(root, "c-old-manual", {})
    make_sample(root, ".tmp-d-unfinished", {"verified": True})
    output = tmp_path / "manifest.jsonl"

    rows = manifest(root, output)
    assert [row["audio"].split("/")[-2] for row in rows] == ["a-reviewed"]

    rows = manifest(root, output, "--include-unverified")
    names = [row["audio"].split("/")[-2] for row in rows]
    assert names == ["a-reviewed", "b-captured", "c-old-manual"]


def test_manual_ingest_counts_as_reviewed(tmp_path) -> None:
    audio = tmp_path / "input.wav"
    audio.write_bytes(b"RIFF")
    root = tmp_path / "stt"
    subprocess.run(
        [
            sys.executable,
            str(INGEST),
            "--audio",
            str(audio),
            "--transcript",
            "wie spät ist es",
            "--dataset-root",
            str(root),
        ],
        check=True,
        capture_output=True,
    )
    [sample] = list(root.iterdir())
    metadata = json.loads((sample / "metadata.json").read_text())
    assert metadata["verified"] is True
    assert metadata["label_source"] == "manual"
    assert len(manifest(root, tmp_path / "manifest.jsonl")) == 1
