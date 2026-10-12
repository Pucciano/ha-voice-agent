"""JSONL capture utilities for development dataset generation.

Two modes:
  without a marker  the capture directory is created when missing, as in the
                    dev stack with its local dev/datasets/llm
  with a marker     (LLM_PROXY_CAPTURE_MARKER, on the Jetson's capture drive)
                    nothing is written unless the marker file and the capture
                    directory exist, so a missing drive never fills the SD card
"""

import datetime
import pathlib

import orjson


def ensure_directories(paths: list[pathlib.Path]) -> None:
    """Ensure all directories in the list exist."""

    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def unavailable_reason(
    directory: pathlib.Path, marker: pathlib.Path | None
) -> str | None:
    """Return why nothing may be written now, or None when capture can run."""

    if marker is None:
        return None
    if not marker.is_file():
        return "no_marker"
    if not directory.is_dir():
        return "no_directory"
    return None


def write_jsonl_record(directory: pathlib.Path, record: dict) -> pathlib.Path:
    """Append a record to a daily JSONL file and return the file path."""

    day = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    target = directory / f"llm_capture_{day}.jsonl"
    with target.open("ab") as handle:
        handle.write(orjson.dumps(record))
        handle.write(b"\n")
    return target
