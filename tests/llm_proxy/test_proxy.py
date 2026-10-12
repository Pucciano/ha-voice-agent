"""The proxy as Home Assistant sees it: alias, keys, upstream request, limits."""

import json

import httpx
from helpers import (
    CLIENT_KEY,
    UPSTREAM,
    UPSTREAM_KEY,
    FakeUpstream,
    auth,
    chat_request,
    completion,
    proxy,
    tool_call,
)


def test_models_lists_only_the_alias_without_asking_upstream():
    with proxy() as (client, upstream):
        response = client.get("/v1/models", headers=auth())

    assert response.status_code == 200
    assert [model["id"] for model in response.json()["data"]] == ["aivi"]
    assert upstream.requests == []


def test_models_pass_through_without_an_upstream_model():
    listing = {"object": "list", "data": [{"id": "qwen", "object": "model"}]}
    fake = FakeUpstream(httpx.Response(200, json=listing))
    with proxy(upstream=fake, LLM_UPSTREAM_MODEL="") as (client, _):
        response = client.get("/v1/models", headers=auth())

    assert response.json() == listing
    assert str(fake.requests[0].url) == f"{UPSTREAM}/models"


def test_requests_without_the_client_key_are_refused():
    with proxy() as (client, upstream):
        missing = client.post("/v1/chat/completions", json=chat_request())
        wrong = client.post(
            "/v1/chat/completions",
            json=chat_request(),
            headers=auth("guess"),
        )
        models = client.get("/v1/models")

    for response in (missing, wrong, models):
        assert response.status_code == 401
        assert response.headers["x-should-retry"] == "false"
    assert upstream.requests == []


def test_without_a_configured_client_key_every_client_is_served():
    with proxy(LLM_PROXY_API_KEY="") as (client, _):
        response = client.post("/v1/chat/completions", json=chat_request())

    assert response.status_code == 200


def test_a_required_but_missing_client_key_refuses_everyone():
    with proxy(
        LLM_PROXY_API_KEY="", LLM_PROXY_REQUIRE_API_KEY="true"
    ) as (client, upstream):
        response = client.post(
            "/v1/chat/completions", json=chat_request(), headers=auth()
        )

    assert response.status_code == 401
    assert upstream.requests == []


def test_upstream_request_maps_model_drops_params_and_adds_extra_body():
    with proxy() as (client, upstream):
        response = client.post(
            "/v1/chat/completions", json=chat_request(), headers=auth()
        )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == (
        "Es ist 12:00."
    )
    request = upstream.requests[0]
    assert str(request.url) == f"{UPSTREAM}/chat/completions"
    assert request.headers["authorization"] == f"Bearer {UPSTREAM_KEY}"
    assert CLIENT_KEY not in request.headers["authorization"]
    body = json.loads(request.content)
    assert body["model"] == "cloud-small"
    assert "user" not in body
    assert body["reasoning_effort"] == "none"
    assert body["max_tokens"] == 3000
    assert body["messages"] == chat_request()["messages"]


def test_streaming_is_refused_so_home_assistant_turns_it_off():
    with proxy() as (client, upstream):
        response = client.post(
            "/v1/chat/completions",
            json=chat_request(stream=True),
            headers=auth(),
        )

    assert response.status_code == 400
    assert upstream.requests == []


def test_invalid_json_body_is_a_client_error():
    with proxy() as (client, _):
        response = client.post(
            "/v1/chat/completions",
            content=b"{not json",
            headers={**auth(), "Content-Type": "application/json"},
        )

    assert response.status_code == 400


def test_daily_limit_refuses_further_requests_without_retry():
    with proxy(LLM_PROXY_DAILY_REQUEST_LIMIT="2") as (client, upstream):
        statuses = [
            client.post(
                "/v1/chat/completions", json=chat_request(), headers=auth()
            )
            for _ in range(3)
        ]
        health = client.get("/health").json()

    assert [response.status_code for response in statuses] == [200, 200, 429]
    assert statuses[2].headers["x-should-retry"] == "false"
    assert len(upstream.requests) == 2
    assert health["requests_today"] == 2


def test_invalid_tool_arguments_get_one_repair_request():
    fake = FakeUpstream(
        httpx.Response(
            200, json=completion(None, [tool_call('{"name": "Licht",}')])
        ),
        httpx.Response(
            200, json=completion(None, [tool_call('{"name": "Licht"}')])
        ),
    )
    with proxy(upstream=fake) as (client, _):
        response = client.post(
            "/v1/chat/completions", json=chat_request(), headers=auth()
        )

    first, second = fake.bodies()
    assert len(second["messages"]) == len(first["messages"]) + 3
    assert second["model"] == "cloud-small"
    assert "user" not in second
    arguments = response.json()["choices"][0]["message"]["tool_calls"][0]
    assert json.loads(arguments["function"]["arguments"]) == {"name": "Licht"}


def test_upstream_errors_keep_their_status():
    error = {"message": "Extra inputs are not permitted", "type": "invalid"}
    fake = FakeUpstream(httpx.Response(422, json={"error": error}))
    with proxy(upstream=fake) as (client, _):
        response = client.post(
            "/v1/chat/completions", json=chat_request(), headers=auth()
        )

    assert response.status_code == 422
    assert response.json()["error"]["message"] == error["message"]


def test_unreachable_upstream_is_a_bad_gateway():
    def refuse():
        raise httpx.ConnectError("connection refused")

    with proxy(upstream=FakeUpstream(refuse)) as (client, _):
        response = client.post(
            "/v1/chat/completions", json=chat_request(), headers=auth()
        )

    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream_http_error"


def test_health_asks_the_upstream_only_on_request():
    with proxy() as (client, upstream):
        local = client.get("/health").json()
        assert upstream.requests == []
        remote = client.get("/health?upstream=true").json()

    assert local["status"] == "ok"
    assert local["upstream_model"] == "cloud-small"
    assert local["capture"] == "disabled"
    assert "upstream_ok" not in local
    assert remote["upstream_ok"] is True
    assert str(upstream.requests[0].url) == f"{UPSTREAM}/models"
    assert (
        upstream.requests[0].headers["authorization"]
        == f"Bearer {UPSTREAM_KEY}"
    )
    assert UPSTREAM_KEY not in json.dumps(local)
