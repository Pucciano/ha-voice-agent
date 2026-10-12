"""Names Home Assistant's Assist knows, as hints for the transcription.

Device and room names are the words a general model gets wrong most often.
The bridge reads the areas, floors and the entities exposed to Assist over
the websocket API, with the same token as speech-to-phrase, and refreshes
them every few minutes. A failed refresh keeps the last list.
"""

import asyncio
import json
import logging
import typing

import websockets.asyncio.client
import websockets.exceptions

_LOGGER = logging.getLogger(__name__)

MAX_TERMS = 100
MAX_TERM_LENGTH = 60


def _names(entry: dict[str, typing.Any], *keys: str) -> list[str]:
    names: list[str] = []
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str):
            names.append(value)
        elif isinstance(value, list):
            # None in an alias list stands for the computed name.
            names.extend(alias for alias in value if isinstance(alias, str))
    return names


def collect_terms(
    areas: list[dict],
    floors: list[dict],
    exposed: dict[str, dict],
    registry: dict[str, dict | None],
    states: list[dict],
) -> tuple[str, ...]:
    """Areas and floors first, then entity names, then aliases; no repeats."""

    friendly = {
        state.get("entity_id"): state.get("attributes", {}).get(
            "friendly_name"
        )
        for state in states
        if isinstance(state, dict)
    }
    exposed_ids = sorted(
        entity_id
        for entity_id, options in exposed.items()
        if isinstance(options, dict) and options.get("conversation")
    )
    candidates: list[str] = []
    for area in areas:
        candidates.extend(_names(area, "name", "aliases"))
    for floor in floors:
        candidates.extend(_names(floor, "name", "aliases"))
    for entity_id in exposed_ids:
        name = friendly.get(entity_id)
        if isinstance(name, str):
            candidates.append(name)
    for entity_id in exposed_ids:
        entry = registry.get(entity_id) or {}
        candidates.extend(_names(entry, "aliases"))

    terms: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        term = " ".join(candidate.split())
        key = term.casefold()
        if not term or len(term) > MAX_TERM_LENGTH or key in seen:
            continue
        seen.add(key)
        terms.append(term)
        if len(terms) == MAX_TERMS:
            break
    return tuple(terms)


class HomeAssistantVocabulary:
    """Keeps `terms` up to date from Home Assistant."""

    def __init__(
        self, websocket_uri: str, token: str, refresh_seconds: float = 600.0
    ) -> None:
        self._uri = websocket_uri
        self._token = token
        self._refresh_seconds = refresh_seconds
        self.terms: tuple[str, ...] = ()

    async def fetch(self) -> tuple[str, ...]:
        async with websockets.asyncio.client.connect(
            self._uri, max_size=None, open_timeout=10
        ) as socket:
            await socket.recv()
            await socket.send(
                json.dumps({"type": "auth", "access_token": self._token})
            )
            answer = json.loads(await socket.recv())
            if answer.get("type") != "auth_ok":
                raise RuntimeError("Home Assistant refused the token")

            next_id = 0

            async def call(message: dict) -> typing.Any:
                nonlocal next_id
                next_id += 1
                await socket.send(json.dumps({**message, "id": next_id}))
                while True:
                    reply = json.loads(await socket.recv())
                    if reply.get("id") == next_id:
                        break
                if not reply.get("success"):
                    raise RuntimeError(
                        f"{message['type']} failed: {reply.get('error')}"
                    )
                return reply.get("result")

            exposed = (await call({"type": "homeassistant/expose_entity/list"}))[
                "exposed_entities"
            ]
            exposed_ids = [
                entity_id
                for entity_id, options in exposed.items()
                if isinstance(options, dict) and options.get("conversation")
            ]
            registry = await call(
                {
                    "type": "config/entity_registry/get_entries",
                    "entity_ids": exposed_ids,
                }
            )
            return collect_terms(
                areas=await call({"type": "config/area_registry/list"}),
                floors=await call({"type": "config/floor_registry/list"}),
                exposed=exposed,
                registry=registry or {},
                states=await call({"type": "get_states"}),
            )

    async def run(self) -> None:
        """Refresh forever; meant to run as a background task."""

        while True:
            try:
                terms = await self.fetch()
            except (
                OSError,
                RuntimeError,
                KeyError,
                TypeError,
                ValueError,
                websockets.exceptions.WebSocketException,
            ) as err:
                _LOGGER.warning("Vocabulary refresh failed: %s", err)
            else:
                if terms != self.terms:
                    _LOGGER.info("Vocabulary: %d terms", len(terms))
                self.terms = terms
            await asyncio.sleep(self._refresh_seconds)
