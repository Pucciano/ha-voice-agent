"""Evaluate latency and token throughput from llm_proxy capture records."""

import argparse
import json
import pathlib
import statistics


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(
        description="Compute latency and tokens/s metrics from capture JSONL."
    )
    parser.add_argument(
        "--dataset",
        required=True,
        help="Directory containing llm capture JSONL files",
    )
    return parser.parse_args()


def _iter_records(dataset_dir: pathlib.Path) -> list[dict]:
    records: list[dict] = []
    for jsonl_path in sorted(dataset_dir.rglob("*.jsonl")):
        with jsonl_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    records.append(payload)
    return records


def evaluate(dataset_dir: pathlib.Path) -> dict:
    """Calculate latency and tokens-per-second aggregates."""

    records = _iter_records(dataset_dir)
    latencies: list[float] = []
    completion_tokens_per_second: list[float] = []

    for record in records:
        latency = record.get("latency_ms")
        if isinstance(latency, (float, int)):
            latencies.append(float(latency))

        usage = record.get("usage", {})
        if not isinstance(usage, dict):
            continue
        completion_tokens = usage.get("completion_tokens")
        if not isinstance(completion_tokens, int):
            continue
        if not isinstance(latency, (float, int)) or latency <= 0:
            continue
        tokens_per_sec = completion_tokens / (float(latency) / 1000.0)
        completion_tokens_per_second.append(tokens_per_sec)

    metrics: dict[str, float | int] = {
        "records_total": len(records),
        "records_with_latency": len(latencies),
        "records_with_tokens_per_second": len(completion_tokens_per_second),
    }

    if latencies:
        metrics["latency_ms_mean"] = round(statistics.mean(latencies), 3)
        metrics["latency_ms_median"] = round(statistics.median(latencies), 3)
        metrics["latency_ms_min"] = round(min(latencies), 3)
        metrics["latency_ms_max"] = round(max(latencies), 3)
    else:
        metrics["latency_ms_mean"] = 0.0
        metrics["latency_ms_median"] = 0.0
        metrics["latency_ms_min"] = 0.0
        metrics["latency_ms_max"] = 0.0

    if completion_tokens_per_second:
        metrics["completion_tokens_per_second_mean"] = round(
            statistics.mean(completion_tokens_per_second),
            3,
        )
        metrics["completion_tokens_per_second_median"] = round(
            statistics.median(completion_tokens_per_second),
            3,
        )
    else:
        metrics["completion_tokens_per_second_mean"] = 0.0
        metrics["completion_tokens_per_second_median"] = 0.0

    return metrics


def main() -> None:
    """Run evaluator and print JSON metrics."""

    args = parse_args()
    dataset_dir = pathlib.Path(args.dataset)
    if not dataset_dir.exists():
        raise FileNotFoundError(f"Dataset path does not exist: {dataset_dir}")

    print(json.dumps(evaluate(dataset_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
