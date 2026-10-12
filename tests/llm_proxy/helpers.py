"""Fake upstream and app factory for the llm_proxy tests."""

import contextlib
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
