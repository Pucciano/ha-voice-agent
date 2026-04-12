"""Evaluate discover_entities -> perform_action two-step compliance."""

import argparse
import json
import pathlib


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""

    parser = argparse.ArgumentParser(
        description=(
            "Evaluate two-step tool-call compliance from JSONL captures."
        )
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
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    records.append(payload)
    return records


def _tool_names(record: dict) -> list[str]:
    response = record.get("response", {})
    if not isinstance(response, dict):
        return []
    choices = response.get("choices", [])
    if not isinstance(choices, list):
        return []

    names: list[str] = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message", {})
        if not isinstance(message, dict):
            continue
        tool_calls = message.get("tool_calls", [])
        if not isinstance(tool_calls, list):
            continue
        for item in tool_calls:
            if not isinstance(item, dict):
                continue
            function_data = item.get("function", {})
            if not isinstance(function_data, dict):
                continue
            name = function_data.get("name")
            if isinstance(name, str):
                names.append(name)
    return names


def evaluate(dataset_dir: pathlib.Path) -> dict:
    """Compute two-step compliance metrics."""

    records = _iter_records(dataset_dir)
    records_with_tool_calls = 0
    compliant_records = 0
    direct_perform_action = 0

    for record in records:
        names = _tool_names(record)
        if not names:
            continue

        records_with_tool_calls += 1
        has_discover = "discover_entities" in names
        has_perform = "perform_action" in names

        if has_perform and not has_discover:
            direct_perform_action += 1

        if has_discover and has_perform:
            discover_index = names.index("discover_entities")
            perform_index = names.index("perform_action")
            if discover_index < perform_index:
                compliant_records += 1

    denominator = records_with_tool_calls if records_with_tool_calls else 1
    return {
        "records_total": len(records),
        "records_with_tool_calls": records_with_tool_calls,
        "compliant_records": compliant_records,
        "direct_perform_action_records": direct_perform_action,
        "two_step_compliance_rate": round(compliant_records / denominator, 6),
    }


def main() -> None:
    """Run the evaluator and print metrics JSON."""

    args = parse_args()
    dataset_dir = pathlib.Path(args.dataset)
    if not dataset_dir.exists():
        raise FileNotFoundError(f"Dataset path does not exist: {dataset_dir}")

    metrics = evaluate(dataset_dir)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
