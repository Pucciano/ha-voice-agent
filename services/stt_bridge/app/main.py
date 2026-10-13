"""Entry point: python -m app.main --api-url URL --model MODEL [options]

Keys come from the environment:
  STT_BRIDGE_API_KEY  key of the transcription API
  HASS_WEBSOCKET_URI  ws://<home-assistant>:8123/api/websocket, and
  HASS_TOKEN          a long-lived token: optional, for the Assist vocabulary
"""

import argparse
import asyncio
import functools
import logging
import os

import httpx
from wyoming.server import AsyncServer

from . import __version__
from .server import Bridge, BridgeHandler, build_info
from .transcription import BIAS_FIELDS, TranscriptionApi
from .vocabulary import HomeAssistantVocabulary

_LOGGER = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--uri", default="tcp://0.0.0.0:10300")
    parser.add_argument(
        "--api-url",
        required=True,
        help="OpenAI-compatible base URL, e.g. https://api.mistral.ai/v1",
    )
    parser.add_argument("--model", required=True, help="Transcription model")
    parser.add_argument(
        "--language",
        action="append",
        default=[],
        help="Language announced to Home Assistant and used when a request "
        "names none; repeat for more (default: de)",
    )
    parser.add_argument(
        "--bias-field",
        choices=BIAS_FIELDS,
        default="context_bias",
        help="How the API takes the Assist vocabulary: context_bias "
        "(Mistral), prompt (OpenAI, Whisper servers) or none",
    )
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=30.0,
        help="Longer requests are cut to their beginning",
    )
    parser.add_argument(
        "--vocabulary-refresh",
        type=float,
        default=600.0,
        help="Seconds between two reads of the Assist names",
    )
    args = parser.parse_args(argv)
    args.language = args.language or ["de"]
    return args


async def run(args: argparse.Namespace) -> None:
    api = TranscriptionApi(
        base_url=args.api_url,
        api_key=os.environ.get("STT_BRIDGE_API_KEY", ""),
        model=args.model,
        bias_field=args.bias_field,
        timeout=args.timeout,
    )
    if not api.api_key:
        _LOGGER.warning("STT_BRIDGE_API_KEY is not set")

    vocabulary: HomeAssistantVocabulary | None = None
    websocket_uri = os.environ.get("HASS_WEBSOCKET_URI", "")
    token = os.environ.get("HASS_TOKEN", "")
    if args.bias_field != "none" and websocket_uri and token:
        vocabulary = HomeAssistantVocabulary(
            websocket_uri, token, args.vocabulary_refresh
        )
        refresh = asyncio.create_task(vocabulary.run())
    else:
        refresh = None
        _LOGGER.info("No Assist vocabulary")

    async with httpx.AsyncClient() as client:
        bridge = Bridge(
            api=api,
            info=build_info(api, args.language),
            client=client,
            default_language=args.language[0],
            max_seconds=args.max_seconds,
            terms=(lambda: vocabulary.terms) if vocabulary else (lambda: ()),
        )
        server = AsyncServer.from_uri(args.uri)
        _LOGGER.info(
            "stt-bridge %s on %s: %s at %s",
            __version__,
            args.uri,
            api.model,
            api.url,
        )
        try:
            await server.run(functools.partial(BridgeHandler, bridge))
        finally:
            if refresh is not None:
                refresh.cancel()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
