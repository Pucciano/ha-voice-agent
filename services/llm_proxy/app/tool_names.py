"""Give unprefixed tool names in the chat back their prefix.

Home Assistant offers the model its tools under prefixed names such as
`intent__HassStartTimer` or `llm__GetDateTime`. A command that its built-in
agent handles locally ("prefer handling commands locally") still lands in
the conversation's chat log, as an assistant tool call with the raw intent
name (`HassStartTimer`) and its tool result. In a later turn of the same
conversation the model copies that raw name, and Home Assistant answers
`Tool "HassStartTimer" not found`.

A name that is not among the request's tools but matches exactly one tool
`<prefix>__<name>` is therefore renamed: in the history before the request
goes upstream, and in the model's answer before it goes back to the client.
Tool call ids stay as they are, so every tool result still belongs to its
call.
"""

import copy
import typing

SEPARATOR = "__"


def _function(tool_call: typing.Any) -> dict[str, typing.Any] | None:
    if not isinstance(tool_call, dict):
        return None
    function = tool_call.get("function")
    return function if isinstance(function, dict) else None


def _declared_names(tools: typing.Any) -> set[str]:
    """Names of the function tools in a request's `tools` list."""

    if not isinstance(tools, list):
        return set()
    names = set()
    for tool in tools:
        function = _function(tool)
        if function is not None and isinstance(function.get("name"), str):
            names.add(function["name"])
    return names


def prefixed_names(tools: typing.Any) -> dict[str, str]:
    """Map each unprefixed name to its prefixed tool.

    A name is left out when it is a tool of its own or when more than one
    prefix carries it, e.g. `intent__HassMediaPause` and
    `media_player__HassMediaPause`.
    """

    names = _declared_names(tools)
    matches: dict[str, list[str]] = {}
    for name in names:
        prefix, separator, base = name.partition(SEPARATOR)
        if prefix and separator and base:
            matches.setdefault(base, []).append(name)
    return {
        base: prefixed[0]
        for base, prefixed in matches.items()
        if len(prefixed) == 1 and base not in names
    }


def _qualify_calls(
    tool_calls: typing.Any, names: dict[str, str], source: str
) -> list[dict[str, typing.Any]]:
    fixes: list[dict[str, typing.Any]] = []
    if not isinstance(tool_calls, list):
        return fixes
    for tool_call in tool_calls:
        function = _function(tool_call)
        if function is None:
            continue
        name = function.get("name")
        if isinstance(name, str) and name in names:
            function["name"] = names[name]
            fixes.append(
                {
                    "source": source,
                    "tool_call_id": tool_call.get("id"),
                    "from": name,
                    "to": names[name],
                }
            )
    return fixes


def qualify_history(
    payload: dict,
) -> tuple[dict, list[dict[str, typing.Any]]]:
    """Return the request with prefixed names in its tool calls, and the fixes.

    The client's payload stays unchanged; without a fix it is returned as is.
    A tool result that repeats its call's name in `name` is renamed with it.
    """

    names = prefixed_names(payload.get("tools"))
    if not names or not isinstance(payload.get("messages"), list):
        return payload, []

    fixed = copy.deepcopy(payload)
    fixes: list[dict[str, typing.Any]] = []
    for message in fixed["messages"]:
        if isinstance(message, dict) and message.get("role") == "assistant":
            fixes += _qualify_calls(message.get("tool_calls"), names, "history")
    if not fixes:
        return payload, []

    renamed = {
        fix["tool_call_id"]: fix
        for fix in fixes
        if isinstance(fix["tool_call_id"], str)
    }
    for message in fixed["messages"]:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        call_id = message.get("tool_call_id")
        fix = renamed.get(call_id) if isinstance(call_id, str) else None
        if fix is not None and message.get("name") == fix["from"]:
            message["name"] = fix["to"]
    return fixed, fixes


def qualify_response(
    response_body: dict, tools: typing.Any
) -> list[dict[str, typing.Any]]:
    """Rename unprefixed tool calls in a chat completion in place.

    Returns one record per renamed call.
    """

    names = prefixed_names(tools)
    choices = response_body.get("choices")
    if not names or not isinstance(choices, list):
        return []
    fixes: list[dict[str, typing.Any]] = []
    for choice in choices:
        message = choice.get("message") if isinstance(choice, dict) else None
        if isinstance(message, dict):
            fixes += _qualify_calls(
                message.get("tool_calls"), names, "response"
            )
    return fixes
