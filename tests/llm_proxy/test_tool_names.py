"""Raw intent names from locally handled turns get their tool prefix back."""

import copy
import json

import httpx
from helpers import (
    ASSIST_TOOLS,
    LOCAL_CALL_ID,
    MODEL_CALL_ID,
    FakeUpstream,
    assist_tool,
    auth,
    completion,
    local_intent_follow_up,
    proxy,
    tool_call,
)

from app import tool_names  # pylint: disable=wrong-import-order


def _calls(messages):
    return [
        (call["id"], call["function"]["name"])
        for message in messages
        if message["role"] == "assistant"
        for call in message.get("tool_calls") or []
    ]


def test_each_unprefixed_name_maps_to_its_only_prefixed_tool():
    names = tool_names.prefixed_names(ASSIST_TOOLS)

    assert names["HassStartTimer"] == "intent__HassStartTimer"
    assert names["HassMediaPause"] == "media_player__HassMediaPause"
    assert names["GetDateTime"] == "llm__GetDateTime"


def test_a_name_under_two_prefixes_is_not_guessed():
    tools = ASSIST_TOOLS + [assist_tool("intent__HassMediaPause")]

    names = tool_names.prefixed_names(tools)

    assert "HassMediaPause" not in names
    assert names["HassStartTimer"] == "intent__HassStartTimer"


def test_a_name_that_is_a_tool_of_its_own_stays():
    tools = ASSIST_TOOLS + [assist_tool("HassStartTimer")]

    assert "HassStartTimer" not in tool_names.prefixed_names(tools)


def test_no_tools_no_names():
    assert not tool_names.prefixed_names(None)
    assert not tool_names.prefixed_names([{"type": "function"}, "broken"])


def test_history_gets_the_prefixed_name_and_keeps_the_call_id():
    request = local_intent_follow_up()
    original = copy.deepcopy(request)

    fixed, fixes = tool_names.qualify_history(request)

    assert _calls(fixed["messages"]) == [
        (MODEL_CALL_ID, "llm__GetDateTime"),
        (LOCAL_CALL_ID, "intent__HassStartTimer"),
    ]
    assert fixes == [
        {
            "source": "history",
            "tool_call_id": LOCAL_CALL_ID,
            "from": "HassStartTimer",
            "to": "intent__HassStartTimer",
        }
    ]
    results = [m for m in fixed["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in results] == [
        MODEL_CALL_ID,
        LOCAL_CALL_ID,
    ]
    # Only the tool call changes; the client's request stays as it came.
    assert request == original
    changed = [
        index
        for index, (old, new) in enumerate(
            zip(original["messages"], fixed["messages"])
        )
        if old != new
    ]
    assert changed == [6]


def test_a_named_tool_result_is_renamed_with_its_call():
    request = local_intent_follow_up()
    request["messages"][7]["name"] = "HassStartTimer"
    request["messages"][3]["name"] = "llm__GetDateTime"

    fixed, _ = tool_names.qualify_history(request)

    assert fixed["messages"][7]["name"] == "intent__HassStartTimer"
    assert fixed["messages"][3]["name"] == "llm__GetDateTime"


def test_malformed_tool_messages_do_not_break_the_fix():
    request = local_intent_follow_up()
    request["messages"][6]["tool_calls"][0]["id"] = ["not", "a", "string"]
    request["messages"][7]["tool_call_id"] = {"id": LOCAL_CALL_ID}
    request["messages"].insert(1, "stray text")

    fixed, fixes = tool_names.qualify_history(request)

    assert [fix["to"] for fix in fixes] == ["intent__HassStartTimer"]
    assert fixed["messages"][1] == "stray text"


def test_every_raw_name_of_one_turn_is_fixed():
    request = local_intent_follow_up()
    request["messages"][6]["tool_calls"].append(
        {
            "id": "01K7FZ3QH8M2V6T9R4N1PXJWDD",
            "type": "function",
            "function": {"name": "HassTimerStatus", "arguments": "{}"},
        }
    )

    fixed, fixes = tool_names.qualify_history(request)

    assert [call[1] for call in _calls(fixed["messages"])] == [
        "llm__GetDateTime",
        "intent__HassStartTimer",
        "intent__HassTimerStatus",
    ]
    assert len(fixes) == 2


def test_a_request_without_raw_names_is_passed_on_as_is():
    request = local_intent_follow_up()
    request["messages"][6]["tool_calls"][0]["function"][
        "name"
    ] = "intent__HassStartTimer"

    fixed, fixes = tool_names.qualify_history(request)

    assert fixed is request
    assert not fixes


def test_unknown_and_ambiguous_names_in_the_history_stay():
    request = local_intent_follow_up()
    request["tools"].append(assist_tool("timer__HassStartTimer"))
    request["messages"][2]["tool_calls"][0]["function"]["name"] = "Unknown"

    fixed, fixes = tool_names.qualify_history(request)

    assert fixed is request
    assert not fixes


def test_a_raw_name_in_the_answer_gets_its_prefix():
    answer = completion(
        None, [tool_call('{"seconds": 10}', name="HassStartTimer")]
    )

    fixes = tool_names.qualify_response(answer, ASSIST_TOOLS)

    call = answer["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "intent__HassStartTimer"
    assert call["id"] == "call00001"
    assert fixes == [
        {
            "source": "response",
            "tool_call_id": "call00001",
            "from": "HassStartTimer",
            "to": "intent__HassStartTimer",
        }
    ]


def test_answers_with_valid_or_no_tool_calls_stay():
    valid = completion(
        None, [tool_call('{"seconds": 10}', name="intent__HassStartTimer")]
    )
    text = completion("Es ist 22:03 Uhr.")
    error = {"error": {"message": "overloaded"}}
    before = copy.deepcopy([valid, text, error])

    for body in (valid, text, error):
        assert not tool_names.qualify_response(body, ASSIST_TOOLS)

    assert [valid, text, error] == before


def test_proxy_sends_prefixed_history_upstream():
    answer = completion(
        None, [tool_call('{"seconds": 10}', name="intent__HassStartTimer")]
    )
    fake = FakeUpstream(httpx.Response(200, json=answer))
    with proxy(upstream=fake) as (client, _):
        response = client.post(
            "/v1/chat/completions",
            json=local_intent_follow_up(),
            headers=auth(),
        )

    assert response.status_code == 200
    body = fake.bodies()[0]
    assert (LOCAL_CALL_ID, "intent__HassStartTimer") in _calls(body["messages"])
    assert body["tools"] == ASSIST_TOOLS
    assert body["model"] == "cloud-small"
    assert "user" not in body


def test_proxy_returns_a_prefixed_tool_call_to_home_assistant():
    answer = completion(
        None, [tool_call('{"seconds": 10}', name="HassStartTimer")]
    )
    fake = FakeUpstream(httpx.Response(200, json=answer))
    with proxy(upstream=fake) as (client, _):
        response = client.post(
            "/v1/chat/completions",
            json=local_intent_follow_up(),
            headers=auth(),
        )

    call = response.json()["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "intent__HassStartTimer"
    assert json.loads(call["function"]["arguments"]) == {"seconds": 10}


def test_proxy_fixes_names_in_a_repaired_answer():
    fake = FakeUpstream(
        httpx.Response(
            200,
            json=completion(
                None, [tool_call('{"seconds": 10,}', name="HassStartTimer")]
            ),
        ),
        httpx.Response(
            200,
            json=completion(
                None, [tool_call('{"seconds": 10}', name="HassStartTimer")]
            ),
        ),
    )
    with proxy(upstream=fake) as (client, _):
        response = client.post(
            "/v1/chat/completions",
            json=local_intent_follow_up(),
            headers=auth(),
        )

    repair = fake.bodies()[1]
    assert [name for _, name in _calls(repair["messages"])] == [
        "llm__GetDateTime",
        "intent__HassStartTimer",
        "intent__HassStartTimer",
    ]
    call = response.json()["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "intent__HassStartTimer"
