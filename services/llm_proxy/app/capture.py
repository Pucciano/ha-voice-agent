"""JSONL capture utilities for development dataset generation."""

import datetime
import pathlib

import orjson


def ensure_directories(paths: list[pathlib.Path]) -> None:
    """Ensure all directories in the list exist."""

    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def write_jsonl_record(directory: pathlib.Path, record: dict) -> pathlib.Path:
    """Append a record to a daily JSONL file and return the file path."""

    day = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    target = directory / f"llm_capture_{day}.jsonl"
    with target.open("ab") as handle:
        handle.write(orjson.dumps(record))
        handle.write(b"\n")
    return target
