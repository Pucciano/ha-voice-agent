"""Entry point: python -m app.main [options]

The shared token comes from the environment:
  WAKE_WORD_RECORDER_TOKEN  the same value as wake_word_recorder_token in the
                            satellites' esphome/secrets.yaml
Without it the recorder starts but refuses every take, so a missing token
never stops the other services of the compose project.
"""

import argparse
import asyncio
import logging
import os
import pathlib
import re
import signal
import urllib.parse

from . import __version__, protocol
from .server import Config, Recorder
from .store import Store

_LOGGER = logging.getLogger(__name__)


def _tcp_uri(value: str) -> tuple[str, int]:
    parts = urllib.parse.urlsplit(value)
    if parts.scheme != "tcp" or not parts.hostname or parts.port is None:
        raise argparse.ArgumentTypeError(f"expected tcp://host:port: {value}")
    return parts.hostname, parts.port


def _satellite_id(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9_]+", value):
        raise argparse.ArgumentTypeError(f"invalid satellite id: {value}")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--uri", type=_tcp_uri, default="tcp://0.0.0.0:10800")
    parser.add_argument(
        "--capture-dir",
        type=pathlib.Path,
        default=pathlib.Path("/capture/drive"),
        help="Root of the capture drive (holds the marker file)",
    )
    parser.add_argument(
        "--satellite",
        action="append",
        type=_satellite_id,
        default=[],
        metavar="ID",
        help="Satellite that may record; repeat for more",
    )
    parser.add_argument("--max-seconds", type=float, default=7200.0)
    parser.add_argument("--min-free-gb", type=float, default=1.0)
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> None:
    token = os.environ.get("WAKE_WORD_RECORDER_TOKEN", "")
    store = Store(args.capture_dir, int(args.min_free_gb * 1024**3))
    config = Config(
        store=store,
        token=token,
        satellites=frozenset(args.satellite),
        max_seconds=args.max_seconds,
    )
    if not token:
        _LOGGER.warning(
            "Takes refused: WAKE_WORD_RECORDER_TOKEN is missing or empty"
        )
    if not config.satellites:
        _LOGGER.warning("Takes refused: no --satellite given")
    reason = await asyncio.to_thread(store.unavailable_reason)
    _LOGGER.info(
        "Capture drive %s",
        "ready" if reason is None else f"not ready ({reason})",
    )

    recorder = Recorder(config)
    await recorder.recover()
    server = await asyncio.start_server(
        recorder.handle,
        args.uri[0],
        args.uri[1],
        limit=protocol.MAX_HEADER_BYTES,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    _LOGGER.info(
        "wake-word-recorder %s listening on %s:%d for %s",
        __version__,
        *args.uri,
        ", ".join(sorted(config.satellites)) or "no satellite",
    )
    async with server:
        await stop.wait()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
