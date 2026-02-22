"""Tool-call validation and repair request helpers."""

import copy
import json
import typing


def validate_tool_call_arguments(
    response_body: dict,
) -> list[dict[str, typing.Any]]:
    """Return details about invalid tool-call argument payloads.

    Args:
      response_body: OpenAI-compatible chat completion response JSON.

    Returns:
      A list with one record per invalid tool call.
    """

    problems: list[dict[str, typing.Any]] = []
    choices = response_body.get("choices", [])
    if not isinstance(choices, list):
        return problems

    for choice_index, choice in enumerate(choices):
        if not isinstance(choice, dict):
            continue

        message = choice.get("message", {})
        if not isinstance(message, dict):
            continue

        tool_calls = message.get("tool_calls", [])
        if not isinstance(tool_calls, list):
            continue

        for tool_call_index, tool_call in enumerate(tool_calls):
            if not isinstance(tool_call, dict):
                continue

            function_data = tool_call.get("function", {})
            if not isinstance(function_data, dict):
                continue

            arguments = function_data.get("arguments")
            if not isinstance(arguments, str):
                continue

            try:
                parsed_json = json.loads(arguments)
                if not isinstance(parsed_json, (dict, list)):
                    problems.append(
                        {
                            "choice_index": choice_index,
                            "tool_call_index": tool_call_index,
                            "error": "arguments must decode to object or array",
                            "arguments": arguments,
                        }
                    )
            except json.JSONDecodeError as exc:
                problems.append(
                    {
                        "choice_index": choice_index,
                        "tool_call_index": tool_call_index,
                        "error": f"json decode error: {exc}",
                        "arguments": arguments,
                    }
                )

    return problems


def build_repair_request(
    original_request: dict,
    invalid_response: dict,
    problems: list[dict[str, typing.Any]],
) -> dict:
    """Create a single-retry payload that asks the model to repair tool JSON."""

    repaired_request = copy.deepcopy(original_request)
    messages = repaired_request.get("messages", [])
    if not isinstance(messages, list):
        messages = []

    assistant_message = {
        "role": "assistant",
        "content": (
            "Previous response included invalid tool-call JSON. "
            "I will retry with valid JSON arguments."
        ),
        "tool_calls": _extract_tool_calls(invalid_response),
    }
    repair_message = {
        "role": "user",
        "content": (
            "Regenerate the previous answer with the same intent. "
            "Each tool_call.function.arguments value must be strict JSON, "
            "and it must parse "
            "with RFC8259. Do not include trailing commas, comments, or plain "
            "text inside arguments."
        ),
    }
    problem_message = {
        "role": "user",
        "content": (
            "Invalid argument details: "
            f"{json.dumps(problems, ensure_ascii=False)}"
        ),
    }

    messages.append(assistant_message)
    messages.append(repair_message)
    messages.append(problem_message)
    repaired_request["messages"] = messages
    repaired_request["stream"] = False
    return repaired_request


def _extract_tool_calls(response_body: dict) -> list[dict]:
    choices = response_body.get("choices", [])
    if not isinstance(choices, list) or not choices:
        return []

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return []

    message = first_choice.get("message", {})
    if not isinstance(message, dict):
        return []

    tool_calls = message.get("tool_calls", [])
    if isinstance(tool_calls, list):
        return tool_calls
    return []
