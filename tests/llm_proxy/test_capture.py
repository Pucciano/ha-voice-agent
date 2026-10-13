"""Capture on the Jetson's drive: only with marker and directory, no keys."""

import json

from helpers import (
    CLIENT_KEY,
    LOCAL_CALL_ID,
    UPSTREAM_KEY,
    auth,
    chat_request,
    local_intent_follow_up,
    proxy,
)


def _capture_env(tmp_path):
    drive = tmp_path / "drive"
    drive.mkdir()
    return drive, {
        "LLM_PROXY_ENABLE_CAPTURE": "true",
        "LLM_PROXY_CAPTURE_DIR": str(drive / "aivi-llm"),
        "LLM_PROXY_CAPTURE_MARKER": str(drive / ".aivi-capture-volume"),
    }


def _ask(client):
    response = client.post(
        "/v1/chat/completions", json=chat_request(), headers=auth()
    )
    assert response.status_code == 200


def test_capture_writes_one_jsonl_record_without_keys(tmp_path):
    drive, env = _capture_env(tmp_path)
    (drive / ".aivi-capture-volume").touch()
    (drive / "aivi-llm").mkdir()
    with proxy(**env) as (client, _):
        _ask(client)
        assert client.get("/health").json()["capture"] == "ready"

    files = list((drive / "aivi-llm").glob("llm_capture_*.jsonl"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    record = json.loads(text)
    assert record["request"]["messages"][1]["content"] == (
        "Wie viel Uhr ist es?"
    )
    assert record["client_model"] == "aivi"
    assert record["upstream_model"] == "cloud-small"
    assert record["response_status"] == 200
    assert UPSTREAM_KEY not in text
    assert CLIENT_KEY not in text


def test_capture_without_marker_writes_nothing(tmp_path):
    drive, env = _capture_env(tmp_path)
    (drive / "aivi-llm").mkdir()
    with proxy(**env) as (client, _):
        _ask(client)
        assert client.get("/health").json()["capture"] == "no_marker"

    assert list((drive / "aivi-llm").iterdir()) == []


def test_capture_never_creates_the_directory_on_a_marked_drive(tmp_path):
    drive, env = _capture_env(tmp_path)
    (drive / ".aivi-capture-volume").touch()
    with proxy(**env) as (client, _):
        _ask(client)
        assert client.get("/health").json()["capture"] == "no_directory"

    assert not (drive / "aivi-llm").exists()


def test_capture_without_marker_setting_creates_its_directory(tmp_path):
    target = tmp_path / "datasets" / "llm"
    env = {
        "LLM_PROXY_ENABLE_CAPTURE": "true",
        "LLM_PROXY_CAPTURE_DIR": str(target),
        "LLM_PROXY_LOG_DIR": str(tmp_path / "logs"),
    }
    with proxy(**env) as (client, _):
        _ask(client)

    assert len(list(target.glob("llm_capture_*.jsonl"))) == 1


def test_capture_keeps_the_raw_request_and_lists_the_fixed_names(tmp_path):
    drive, env = _capture_env(tmp_path)
    (drive / ".aivi-capture-volume").touch()
    (drive / "aivi-llm").mkdir()
    with proxy(**env) as (client, _):
        response = client.post(
            "/v1/chat/completions",
            json=local_intent_follow_up(),
            headers=auth(),
        )
    assert response.status_code == 200

    (capture_file,) = (drive / "aivi-llm").glob("llm_capture_*.jsonl")
    record = json.loads(capture_file.read_text(encoding="utf-8"))
    local_call = record["request"]["messages"][6]["tool_calls"][0]
    assert local_call["function"]["name"] == "HassStartTimer"
    assert record["fixed_tool_names"] == [
        {
            "source": "history",
            "tool_call_id": LOCAL_CALL_ID,
            "from": "HassStartTimer",
            "to": "intent__HassStartTimer",
        }
    ]


def test_capture_lists_no_fixed_names_for_a_clean_request(tmp_path):
    drive, env = _capture_env(tmp_path)
    (drive / ".aivi-capture-volume").touch()
    (drive / "aivi-llm").mkdir()
    with proxy(**env) as (client, _):
        _ask(client)

    (capture_file,) = (drive / "aivi-llm").glob("llm_capture_*.jsonl")
    record = json.loads(capture_file.read_text(encoding="utf-8"))
    assert record["fixed_tool_names"] == []
