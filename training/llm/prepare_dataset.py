"""Prepare captured llm_proxy JSONL records for supervised fine-tuning."""

import argparse
import json
import pathlib


SYSTEM_FALLBACK = (
    "You are a local Home Assistant tool-calling assistant. "
    "Always produce valid JSON arguments for tool calls."
)


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""

    parser = argparse.ArgumentParser(
        description="Convert capture JSONL into training messages JSONL."
    )
    parser.add_argument("--input-dir", required=True, help="Capture directory")
    parser.add_argument(
        "--output",
        required=True,
        help="Prepared dataset JSONL",
    )
    return parser.parse_args()


def _iter_capture_records(input_dir: pathlib.Path) -> list[dict]:
    records: list[dict] = []
    for path in sorted(input_dir.rglob("*.jsonl")):
        with path.open("r", encoding="utf-8") as handle:
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


def _to_training_record(capture_record: dict) -> dict | None:
    request = capture_record.get("request", {})
    response = capture_record.get("response", {})
    if not isinstance(request, dict) or not isinstance(response, dict):
        return None

    messages = request.get("messages", [])
    if not isinstance(messages, list) or not messages:
        return None

    choices = response.get("choices", [])
    if not isinstance(choices, list) or not choices:
        return None
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return None
    assistant_message = first_choice.get("message", {})
    if not isinstance(assistant_message, dict):
        return None

    has_system = any(
        isinstance(item, dict) and item.get("role") == "system"
        for item in messages
    )
    final_messages: list[dict] = []
    if not has_system:
        final_messages.append({"role": "system", "content": SYSTEM_FALLBACK})

    for item in messages:
        if isinstance(item, dict):
            final_messages.append(item)

    assistant_content = assistant_message.get("content")
    if not isinstance(assistant_content, str):
        assistant_content = ""

    final_messages.append(
        {
            "role": "assistant",
            "content": assistant_content,
            "tool_calls": assistant_message.get("tool_calls"),
        }
    )

    return {
        "messages": final_messages,
        "metadata": {
            "request_id": capture_record.get("request_id"),
            "latency_ms": capture_record.get("latency_ms"),
            "repair_retry_used": capture_record.get("repair_retry_used", 0),
        },
    }


def main() -> None:
    """Convert capture data into supervised fine-tuning examples."""

    args = parse_args()
    input_dir = pathlib.Path(args.input_dir)
    output = pathlib.Path(args.output)

    if not input_dir.exists():
        raise FileNotFoundError(f"Input directory not found: {input_dir}")

    records = _iter_capture_records(input_dir)
    converted = []
    for record in records:
        candidate = _to_training_record(record)
        if candidate is not None:
            converted.append(candidate)

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for item in converted:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    print(f"Wrote {len(converted)} examples to {output}")


if __name__ == "__main__":
    main()
