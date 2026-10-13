"""Fake upstream and app factory for the llm_proxy tests."""

import contextlib
import copy
import json
import pathlib
import sys
from collections.abc import Callable, Iterator
from typing import Any

import httpx
from fastapi.testclient import TestClient

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "services" / "llm_proxy"))

# pylint: disable=wrong-import-position
from app import config  # noqa: E402
from app.main import create_app  # noqa: E402

UPSTREAM = "https://llm.example/v1"
UPSTREAM_KEY = "upstream-secret"
CLIENT_KEY = "client-secret"

# The environment of the Jetson: a cloud model behind the alias "aivi".
JETSON_ENV = {
    "LLM_UPSTREAM_BASE_URL": UPSTREAM,
    "LLM_UPSTREAM_API_KEY": UPSTREAM_KEY,
    "LLM_UPSTREAM_MODEL": "cloud-small",
    "LLM_PROXY_MODEL_ALIAS": "aivi",
    "LLM_PROXY_API_KEY": CLIENT_KEY,
    "LLM_UPSTREAM_DROP_PARAMS": "user",
    "LLM_UPSTREAM_EXTRA_BODY": '{"reasoning_effort": "none"}',
    "LLM_PROXY_ENABLE_CAPTURE": "false",
}


def completion(
    content: str | None = "Es ist 12:00.",
    tool_calls: list[dict] | None = None,
) -> dict[str, Any]:
    """An OpenAI-style chat completion answer."""

    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "id": "cmpl-1",
        "object": "chat.completion",
        "model": "cloud-small",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def tool_call(arguments: str, name: str = "HassTurnOn") -> dict[str, Any]:
    return {
        "id": "call00001",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


class FakeUpstream:
    """Records every request and answers from a list, the last one repeating."""

    def __init__(
        self, *answers: httpx.Response | Callable[[httpx.Request], Any]
    ) -> None:
        self.answers = list(answers) or [httpx.Response(200, json=completion())]
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers[min(len(self.requests), len(self.answers)) - 1]
        if callable(answer):
            return answer(request)
        return answer

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests]


@contextlib.contextmanager
def proxy(
    env: dict[str, str] | None = None,
    upstream: FakeUpstream | None = None,
    **overrides: str,
) -> Iterator[tuple[TestClient, FakeUpstream]]:
    """Run the app against a fake upstream; overrides change single variables."""

    fake = upstream or FakeUpstream()
    settings = config.load_settings({**(env or JETSON_ENV), **overrides})
    app = create_app(settings, transport=httpx.MockTransport(fake.handler))
    with TestClient(app) as client:
        yield client, fake


def auth(key: str = CLIENT_KEY) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def chat_request(**extra: Any) -> dict[str, Any]:
    """What Home Assistant's llama.cpp integration sends, shortened."""

    return {
        "model": "aivi",
        "messages": [
            {"role": "system", "content": "Du bist ein Sprachassistent."},
            {"role": "user", "content": "Wie viel Uhr ist es?"},
        ],
        "max_tokens": 3000,
        "temperature": 0.7,
        "top_p": 1.0,
        "user": "01JCONVERSATION",
        **extra,
    }


def assist_tool(tool: str, /, **properties: dict) -> dict[str, Any]:
    """A tool as Home Assistant's Assist API declares it, shortened."""

    return {
        "type": "function",
        "function": {
            "name": tool,
            "description": f"Assist tool {tool}",
            "parameters": {"type": "object", "properties": properties},
        },
    }


# Tools of a satellite that supports timers, with Home Assistant's prefixes.
ASSIST_TOOLS = [
    assist_tool(
        "intent__HassStartTimer",
        hours={"type": "integer"},
        minutes={"type": "integer"},
        seconds={"type": "integer"},
        name={"type": "string"},
    ),
    assist_tool("intent__HassCancelTimer", name={"type": "string"}),
    assist_tool("intent__HassTimerStatus", name={"type": "string"}),
    assist_tool("intent__HassTurnOn", name={"type": "string"}),
    assist_tool("media_player__HassMediaPause", name={"type": "string"}),
    assist_tool("llm__GetDateTime"),
]

# Id of the tool call that Home Assistant logs for a locally handled turn
# (a ULID), and of a call the model made itself (nine characters).
LOCAL_CALL_ID = "01K7FZ3QH8M2V6T9R4N1PXJWDC"
MODEL_CALL_ID = "Xk3mQ9pLa"


def local_intent_follow_up() -> dict[str, Any]:
    """The request after a timer that the built-in agent started locally.

    Modelled on the requests of 2026-10-12 (issue #19): the earlier turns
    sit in the history, the local one under the raw intent name
    `HassStartTimer` while the tools carry the prefixed names.
    """

    return {
        "model": "aivi",
        "messages": [
            {"role": "system", "content": "Du bist ein Sprachassistent."},
            {"role": "user", "content": "Wie spät ist es?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": MODEL_CALL_ID,
                        "type": "function",
                        "function": {
                            "name": "llm__GetDateTime",
                            "arguments": "{}",
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": MODEL_CALL_ID,
                "content": '{"date": "2026-10-12", "time": "22:03:40"}',
            },
            {"role": "assistant", "content": "Es ist 22:03 Uhr."},
            {"role": "user", "content": "Stelle einen Timer auf 10 Sekunden"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": LOCAL_CALL_ID,
                        "type": "function",
                        "function": {
                            "name": "HassStartTimer",
                            "arguments": '{"seconds": 10}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": LOCAL_CALL_ID,
                "content": (
                    '{"speech": {"plain": {"speech": "Timer gestartet"}}, '
                    '"response_type": "action_done", '
                    '"data": {"targets": [], "success": [], "failed": []}}'
                ),
            },
            {"role": "assistant", "content": "Timer gestartet"},
            {"role": "user", "content": "Stelle einen 10-Sekunden-Timer."},
        ],
        "tools": copy.deepcopy(ASSIST_TOOLS),
        "tool_choice": "auto",
        "max_tokens": 3000,
        "temperature": 0.3,
        "top_p": 1.0,
        "user": "01K7FZ0CONVERSATION0000000",
    }
