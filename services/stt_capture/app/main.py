"""Entry point: python -m app.main [options]

Home Assistant access comes from the environment, the same values that
speech-to-phrase uses:
  HASS_WEBSOCKET_URI  ws://<home-assistant>:8123/api/websocket
  HASS_TOKEN          long-lived access token (read-only use)
Without them, or without --satellite, the relay only forwards.
"""

import argparse
import asyncio
import logging
import os
import pathlib
import signal
import urllib.parse

from . import __version__
from . import wyoming_frames as frames
from .home_assistant import (
    HomeAssistant,
    parse_satellite,
    rest_url_from_websocket,
)
from .relay import Config, Relay
from .store import Store

_LOGGER = logging.getLogger(__name__)


def _tcp_uri(value: str) -> tuple[str, int]:
    parts = urllib.parse.urlsplit(value)
    if parts.scheme != "tcp" or not parts.hostname or parts.port is None:
        raise argparse.ArgumentTypeError(f"expected tcp://host:port: {value}")
    return parts.hostname, parts.port


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--uri", type=_tcp_uri, default="tcp://0.0.0.0:10300")
    parser.add_argument(
        "--upstream",
        type=_tcp_uri,
        default="tcp://speech-to-phrase:10300",
        help="Wyoming speech-to-text service",
    )
    parser.add_argument(
        "--capture-dir",
        type=pathlib.Path,
        default=pathlib.Path("/capture/drive"),
        help="Root of the capture drive (holds the marker file)",
    )
    parser.add_argument(
        "--satellite",
        action="append",
        type=parse_satellite,
        default=[],
        metavar="ID=ASSIST_SATELLITE,SWITCH",
        help="Satellite that may be captured; repeat for more",
    )
    parser.add_argument("--max-seconds", type=float, default=60.0)
    parser.add_argument(
        "--max-capture-bytes", type=int, default=32 * 1024 * 1024
    )
    parser.add_argument("--min-free-gb", type=float, default=1.0)
    parser.add_argument(
        "--decision-budget",
        type=float,
        default=2.0,
        help="Seconds for all Home Assistant queries of one decision",
    )
    parser.add_argument(
        "--hass-url",
        help="Home Assistant REST URL; default from HASS_WEBSOCKET_URI",
    )
    return parser.parse_args(argv)


def _home_assistant(args: argparse.Namespace) -> HomeAssistant | None:
    token = os.environ.get("HASS_TOKEN", "")
    url = args.hass_url
    if url is None and os.environ.get("HASS_WEBSOCKET_URI"):
        url = rest_url_from_websocket(os.environ["HASS_WEBSOCKET_URI"])
    if not token or not url:
        return None
    return HomeAssistant(url, token)


async def run(args: argparse.Namespace) -> None:
    home_assistant = _home_assistant(args)
    store = Store(args.capture_dir, int(args.min_free_gb * 1024**3))
    config = Config(
        upstream_host=args.upstream[0],
        upstream_port=args.upstream[1],
        home_assistant=home_assistant,
        satellites=tuple(args.satellite),
        store=store,
        max_seconds=args.max_seconds,
        max_capture_bytes=args.max_capture_bytes,
        decision_budget=args.decision_budget,
    )
    if config.capture_enabled:
        _LOGGER.info(
            "Capture possible for: %s",
            ", ".join(s.satellite_id for s in config.satellites),
        )
    else:
        _LOGGER.warning(
            "Capture disabled: Home Assistant access or --satellite missing"
        )
    reason = await asyncio.to_thread(store.unavailable_reason)
    if reason is None:
        removed = await asyncio.to_thread(store.cleanup_once)
        _LOGGER.info(
            "Capture drive ready (%d unfinished samples removed)", removed
        )
    else:
        _LOGGER.info("Capture drive not ready (%s)", reason)

    relay = Relay(config)
    server = await asyncio.start_server(
        relay.handle, args.uri[0], args.uri[1], limit=frames.MAX_HEADER_BYTES
    )
    info_task = asyncio.create_task(relay.fetch_stt_info())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    _LOGGER.info("stt-capture %s listening on %s:%d", __version__, *args.uri)
    async with server:
        await stop.wait()
    info_task.cancel()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
