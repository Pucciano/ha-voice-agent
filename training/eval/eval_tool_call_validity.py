"""Evaluate tool-call JSON validity from captured llm_proxy records."""

import argparse
import json
import pathlib
import statistics


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(
        description="Compute tool-call argument JSON validity rate."
    )
    parser.add_argument(
        "--dataset",
        required=True,
        help="Directory that contains llm_capture_*.jsonl files",
    )
    parser.add_argument(
        "--output-json",
        default="",
        help="Optional path to store metrics as JSON",
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


def _collect_tool_calls(record: dict) -> list[dict]:
    response = record.get("response", {})
    if not isinstance(response, dict):
        return []
    choices = response.get("choices", [])
    if not isinstance(choices, list):
        return []

    calls: list[dict] = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message", {})
        if not isinstance(message, dict):
            continue
        tool_calls = message.get("tool_calls", [])
        if isinstance(tool_calls, list):
            valid_items = [
                item for item in tool_calls if isinstance(item, dict)
            ]
            calls.extend(valid_items)
    return calls


def _is_valid_arguments(tool_call: dict) -> bool:
    function_data = tool_call.get("function", {})
    if not isinstance(function_data, dict):
        return False
    arguments = function_data.get("arguments")
    if not isinstance(arguments, str):
        return False
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, (dict, list))


def evaluate(dataset_dir: pathlib.Path) -> dict:
    """Evaluate validity metrics for all tool calls in a dataset."""

    records = _iter_records(dataset_dir)
    total_calls = 0
    valid_calls = 0
    calls_per_record: list[int] = []

    for record in records:
        tool_calls = _collect_tool_calls(record)
        calls_per_record.append(len(tool_calls))
        for tool_call in tool_calls:
            total_calls += 1
            if _is_valid_arguments(tool_call):
                valid_calls += 1

    validity_rate = (valid_calls / total_calls) if total_calls else 1.0
    mean_calls = statistics.mean(calls_per_record) if calls_per_record else 0.0
    return {
        "records": len(records),
        "total_tool_calls": total_calls,
        "valid_tool_calls": valid_calls,
        "invalid_tool_calls": total_calls - valid_calls,
        "tool_call_validity_rate": round(validity_rate, 6),
        "mean_tool_calls_per_record": round(mean_calls, 6),
    }


def main() -> None:
    """Entrypoint for CLI execution."""

    args = parse_args()
    dataset_dir = pathlib.Path(args.dataset)
    if not dataset_dir.exists():
        raise FileNotFoundError(f"Dataset path does not exist: {dataset_dir}")

    metrics = evaluate(dataset_dir)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))

    if args.output_json:
        output_path = pathlib.Path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
