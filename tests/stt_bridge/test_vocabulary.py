"""Assist vocabulary: which names, in which order, and the websocket calls."""

import asyncio
import json

import websockets.asyncio.server
from helpers import free_port

from app.vocabulary import (  # pylint: disable=wrong-import-order
    MAX_TERMS,
    HomeAssistantVocabulary,
    collect_terms,
)

AREAS = [
    {"area_id": "living_room", "name": "Wohnzimmer", "aliases": []},
    {"area_id": "bathroom", "name": "Badezimmer", "aliases": ["Bad"]},
]
FLOORS = [{"floor_id": "ground", "name": "Erdgeschoss", "aliases": []}]
EXPOSED = {
    "light.floor_lamp": {"conversation": True},
    "switch.rack_ups": {"conversation": False},
    "media_player.living_room": {"conversation": True, "cloud.alexa": True},
}
REGISTRY = {
    "light.floor_lamp": {"aliases": [None, "Standleuchte"]},
    "media_player.living_room": {"aliases": ["Lautsprecher", "stehlampe"]},
}
STATES = [
    {"entity_id": "light.floor_lamp", "attributes": {"friendly_name": "Stehlampe"}},
    {"entity_id": "switch.rack_ups", "attributes": {"friendly_name": "USV"}},
    {
        "entity_id": "media_player.living_room",
        "attributes": {"friendly_name": "Wohnzimmer  Lautsprecher"},
    },
]


def test_areas_floors_names_then_aliases_without_repeats():
    terms = collect_terms(AREAS, FLOORS, EXPOSED, REGISTRY, STATES)

    assert terms == (
        "Wohnzimmer",
        "Badezimmer",
        "Bad",
        "Erdgeschoss",
        "Stehlampe",
        "Wohnzimmer Lautsprecher",
        "Standleuchte",
        "Lautsprecher",
    )


def test_terms_stop_at_the_api_limit_and_skip_long_names():
    states = [
        {"entity_id": f"light.l{i}", "attributes": {"friendly_name": f"L {i}"}}
        for i in range(150)
    ] + [{"entity_id": "light.long", "attributes": {"friendly_name": "x" * 61}}]
    exposed = {state["entity_id"]: {"conversation": True} for state in states}

    terms = collect_terms([], [], exposed, {}, states)

    assert len(terms) == MAX_TERMS
    assert "x" * 61 not in terms


def test_fetch_asks_home_assistant_over_the_websocket():
    answers = {
        "homeassistant/expose_entity/list": {"exposed_entities": EXPOSED},
        "config/entity_registry/get_entries": REGISTRY,
        "config/area_registry/list": AREAS,
        "config/floor_registry/list": FLOORS,
        "get_states": STATES,
    }
    seen = []

    async def handler(socket):
        await socket.send(json.dumps({"type": "auth_required"}))
        auth = json.loads(await socket.recv())
        if auth.get("access_token") != "token":
            await socket.send(json.dumps({"type": "auth_invalid"}))
            return
        await socket.send(json.dumps({"type": "auth_ok"}))
        async for raw in socket:
            message = json.loads(raw)
            seen.append(message)
            await socket.send(
                json.dumps(
                    {
                        "id": message["id"],
                        "type": "result",
                        "success": True,
                        "result": answers[message["type"]],
                    }
                )
            )

    async def scenario(token):
        port = free_port()
        async with websockets.asyncio.server.serve(handler, "127.0.0.1", port):
            vocabulary = HomeAssistantVocabulary(
                f"ws://127.0.0.1:{port}/api/websocket", token
            )
            return await vocabulary.fetch()

    terms = asyncio.run(scenario("token"))

    assert terms[:2] == ("Wohnzimmer", "Badezimmer")
    registry_call = next(
        m for m in seen if m["type"] == "config/entity_registry/get_entries"
    )
    assert sorted(registry_call["entity_ids"]) == [
        "light.floor_lamp",
        "media_player.living_room",
    ]

    try:
        asyncio.run(scenario("wrong"))
    except RuntimeError as err:
        assert "refused" in str(err)
    else:
        raise AssertionError("a wrong token must fail")
