"""Capture decision: exactly one listening satellite whose switch is on."""

import asyncio
import time

import pytest
from helpers import KITCHEN, LIVING_ROOM, TOKEN, FakeHomeAssistant
from app.home_assistant import (
    HomeAssistant,
    decide,
    parse_satellite,
    rest_url_from_websocket,
)

SAT = LIVING_ROOM.satellite_entity
SWITCH = LIVING_ROOM.switch_entity


def run_decide(states, satellites=(LIVING_ROOM,), budget=2.0, token=TOKEN):
    fake = FakeHomeAssistant(states)

    async def timed():
        # Timed inside the loop: asyncio.run also waits for query threads
        # that outlive a timeout.
        start = time.monotonic()
        decision = await decide(
            HomeAssistant(fake.url, token), list(satellites), budget
        )
        return decision, time.monotonic() - start

    try:
        return asyncio.run(timed())
    finally:
        fake.close()


def test_listening_satellite_with_switch_on_is_allowed() -> None:
    decision, _ = run_decide({SAT: "listening", SWITCH: "on"})
    assert decision.allowed
    assert decision.satellite == LIVING_ROOM


@pytest.mark.parametrize(
    ("states", "reason"),
    [
        ({SAT: "processing", SWITCH: "on"}, "no_listening_satellite"),
        ({SAT: "idle", SWITCH: "on"}, "no_listening_satellite"),
        ({SAT: "listening", SWITCH: "off"}, "not_enabled"),
        ({SAT: "listening", SWITCH: "unknown"}, "not_enabled"),
        ({SAT: "listening", SWITCH: "unavailable"}, "not_enabled"),
        ({SAT: "listening"}, "ha_error"),
        ({SAT: "listening", SWITCH: ("raw", b"{broken")}, "ha_error"),
        ({SAT: "listening", SWITCH: ("raw", b"[1, 2]")}, "ha_error"),
        (
            {
                SAT: "listening",
                SWITCH: ("raw", b'{"entity_id": "x", "state": "on"}'),
            },
            "ha_error",
        ),
        ({SAT: ("status", 500), SWITCH: "on"}, "ha_error"),
        ({}, "ha_error"),
    ],
)
def test_everything_else_is_refused(states, reason) -> None:
    decision, _ = run_decide(states)
    assert not decision.allowed
    assert decision.reason == reason


def test_wrong_token_is_refused() -> None:
    decision, _ = run_decide({SAT: "listening", SWITCH: "on"}, token="wrong")
    assert (decision.allowed, decision.reason) == (False, "ha_error")


def test_processing_satellite_does_not_count() -> None:
    decision, _ = run_decide(
        {
            SAT: "processing",
            SWITCH: "on",
            KITCHEN.satellite_entity: "listening",
            KITCHEN.switch_entity: "on",
        },
        satellites=(LIVING_ROOM, KITCHEN),
    )
    assert decision.allowed
    assert decision.satellite == KITCHEN


def test_two_listening_satellites_are_refused() -> None:
    decision, _ = run_decide(
        {
            SAT: "listening",
            SWITCH: "on",
            KITCHEN.satellite_entity: "listening",
            KITCHEN.switch_entity: "on",
        },
        satellites=(LIVING_ROOM, KITCHEN),
    )
    assert (decision.allowed, decision.reason) == (False, "several_listening")
    assert decision.retryable


def test_satellites_are_queried_in_parallel() -> None:
    # 0.6 s each: sequential queries would need 1.2 s plus the switch.
    decision, elapsed = run_decide(
        {
            SAT: ("delay", 0.6, "listening"),
            SWITCH: "on",
            KITCHEN.satellite_entity: ("delay", 0.6, "idle"),
        },
        satellites=(LIVING_ROOM, KITCHEN),
        budget=1.0,
    )
    assert decision.allowed
    assert elapsed < 1.0


def test_budget_is_shared_by_all_queries() -> None:
    # Each query fits into the budget alone, together they do not.
    decision, elapsed = run_decide(
        {SAT: ("delay", 0.6, "listening"), SWITCH: ("delay", 0.6, "on")},
        budget=1.0,
    )
    assert (decision.allowed, decision.reason) == (False, "ha_timeout")
    assert elapsed < 1.1


def test_without_home_assistant_capture_is_disabled() -> None:
    decision = asyncio.run(decide(None, [LIVING_ROOM]))
    assert (decision.allowed, decision.reason) == (False, "capture_disabled")
    assert not decision.retryable


def test_parse_satellite() -> None:
    satellite = parse_satellite(
        "living_room=assist_satellite.living_room_satellite,"
        "switch.living_room_capture"
    )
    assert satellite == LIVING_ROOM
    for spec in (
        "living_room",
        "living_room=switch.a,switch.b",
        "living_room=assist_satellite.a,light.b",
        "Living Room=assist_satellite.a,switch.b",
        "living_room=assist_satellite.a/../x,switch.b",
    ):
        with pytest.raises(ValueError):
            parse_satellite(spec)


def test_rest_url_from_websocket() -> None:
    assert (
        rest_url_from_websocket("ws://homeassistant:8123/api/websocket")
        == "http://homeassistant:8123"
    )
    assert (
        rest_url_from_websocket("wss://ha.example:443/api/websocket")
        == "https://ha.example:443"
    )
    with pytest.raises(ValueError):
        rest_url_from_websocket("http://homeassistant:8123/api/websocket")
