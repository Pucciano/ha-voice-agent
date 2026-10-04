"""Decide from Home Assistant states whether a request may be captured.

A Wyoming speech-to-text request carries no satellite id. The relay therefore
asks Home Assistant which configured satellite is `listening` right now. Only
when exactly one is, and that satellite's capture switch is `on`, may the
request be stored. Every other answer, error or timeout means no capture.
"""

import asyncio
import http.client
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass

_ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")

# Reasons after which a second attempt at audio-stop can change the result.
RETRYABLE = frozenset(
    {"no_listening_satellite", "several_listening", "ha_error", "ha_timeout"}
)


@dataclass(frozen=True)
class Satellite:
    satellite_id: str
    satellite_entity: str
    switch_entity: str


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    satellite: Satellite | None = None

    @property
    def retryable(self) -> bool:
        return self.reason in RETRYABLE


def parse_satellite(spec: str) -> Satellite:
    """Parse `<satellite_id>=<assist_satellite entity>,<switch entity>`."""

    satellite_id, sep, entities = spec.partition("=")
    satellite_entity, sep2, switch_entity = entities.partition(",")
    if not (sep and sep2 and re.fullmatch(r"[a-z0-9_]+", satellite_id)):
        raise ValueError(f"invalid satellite: {spec!r}")
    if not (
        _ENTITY_ID.match(satellite_entity)
        and satellite_entity.startswith("assist_satellite.")
    ):
        raise ValueError(f"invalid assist_satellite entity: {spec!r}")
    if not (
        _ENTITY_ID.match(switch_entity) and switch_entity.startswith("switch.")
    ):
        raise ValueError(f"invalid switch entity: {spec!r}")
    return Satellite(satellite_id, satellite_entity, switch_entity)


def rest_url_from_websocket(uri: str) -> str:
    """ws://host:8123/api/websocket -> http://host:8123"""

    parts = urllib.parse.urlsplit(uri)
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme)
    if scheme is None or not parts.netloc:
        raise ValueError("HASS_WEBSOCKET_URI must be a ws:// or wss:// URL")
    return f"{scheme}://{parts.netloc}"


class HomeAssistant:
    """Minimal read-only REST client; any user's token is enough."""

    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token

    def state(self, entity_id: str, timeout: float) -> str | None:
        """Return the entity's state, or None on any error.

        Blocking; run it in a thread.
        """

        request = urllib.request.Request(
            f"{self._base_url}/api/states/{entity_id}",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read(1024 * 1024))
        except (OSError, ValueError, http.client.HTTPException):
            return None
        if not isinstance(body, dict) or body.get("entity_id") != entity_id:
            return None
        state = body.get("state")
        return state if isinstance(state, str) else None


async def _decide(
    home_assistant: HomeAssistant,
    satellites: list[Satellite],
    budget: float,
) -> Decision:
    states = await asyncio.gather(
        *(
            asyncio.to_thread(
                home_assistant.state, satellite.satellite_entity, budget
            )
            for satellite in satellites
        )
    )
    if any(state is None for state in states):
        # An unknown state could hide a second listening satellite.
        return Decision(False, "ha_error")
    listening = [
        satellite
        for satellite, state in zip(satellites, states)
        if state == "listening"
    ]
    if not listening:
        return Decision(False, "no_listening_satellite")
    if len(listening) > 1:
        return Decision(False, "several_listening")

    satellite = listening[0]
    switch = await asyncio.to_thread(
        home_assistant.state, satellite.switch_entity, budget
    )
    if switch is None:
        return Decision(False, "ha_error", satellite)
    if switch != "on":
        return Decision(False, "not_enabled", satellite)
    return Decision(True, "enabled", satellite)


async def decide(
    home_assistant: HomeAssistant | None,
    satellites: list[Satellite],
    budget: float = 2.0,
) -> Decision:
    """Decide within one shared time budget for all queries."""

    if home_assistant is None or not satellites:
        return Decision(False, "capture_disabled")
    try:
        return await asyncio.wait_for(
            _decide(home_assistant, satellites, budget), budget
        )
    except asyncio.TimeoutError:
        return Decision(False, "ha_timeout")
