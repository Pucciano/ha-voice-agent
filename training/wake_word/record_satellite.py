"""Record wake word samples through an AIVI satellite.

The satellite streams its voice assistant audio (XVF3800 channel 1, 16 kHz,
exactly what the wake word model hears) to this script instead of Home
Assistant. ESPHome serves one voice assistant client at a time: disable the
satellite's ESPHome entry in Home Assistant before recording. Every take
starts when the satellite detects "Okay Nabu" and lasts --seconds.
"""

import argparse
import asyncio
import datetime
import logging
import math
import sys
from pathlib import Path

import numpy as np
import yaml
from aioesphomeapi import APIClient, LogLevel
from aioesphomeapi.model import VoiceAssistantEventType

from common import (
    REPO_ROOT,
    SAMPLE_RATE,
    Paths,
    load_config,
    setup_logging,
    write_wav,
)

BUSY_MESSAGE = "Multiple API Clients attempting to connect to Voice Assistant"

_LOGGER = logging.getLogger("record")


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", required=True, help="Satellite IP address")
    parser.add_argument(
        "--satellite",
        default="living_room",
        help="Satellite id; the API key is <id>_api_key in the secrets file",
    )
    kind = parser.add_mutually_exclusive_group(required=True)
    kind.add_argument("--speaker", help="Record wake words of this person")
    kind.add_argument(
        "--negative",
        action="store_true",
        help="Record everyday sound without the wake word",
    )
    parser.add_argument(
        "--seconds",
        type=float,
        help="Length of a take (default: 60, or 1800 with --negative)",
    )
    parser.add_argument(
        "--takes", type=int, default=1, help="Number of takes, 0 for no limit"
    )
    parser.add_argument(
        "--secrets", type=Path, default=REPO_ROOT / "esphome" / "secrets.yaml"
    )
    parser.add_argument("--port", type=int, default=6053)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def load_api_key(secrets: Path, satellite: str) -> str:
    """Read the satellite's API encryption key without printing it."""

    with secrets.open(encoding="utf-8") as handle:
        values = yaml.safe_load(handle) or {}
    key = values.get(f"{satellite}_api_key")
    if not key:
        raise SystemExit(f"{satellite}_api_key is missing in {secrets}")
    return key


class Take:
    """Audio of one recording take."""

    def __init__(self, seconds: float):
        self.wanted_samples = int(seconds * SAMPLE_RATE)
        self.chunks: list[bytes] = []
        self.samples = 0
        self.started = asyncio.Event()
        self.finished = asyncio.Event()

    def add(self, data: bytes) -> None:
        if self.finished.is_set():
            return
        self.chunks.append(data)
        self.samples += len(data) // 2
        if self.samples >= self.wanted_samples:
            self.finished.set()

    def audio(self) -> np.ndarray:
        data = np.frombuffer(b"".join(self.chunks), dtype=np.int16)
        return data[: self.wanted_samples]


class Recorder:
    """Voice assistant client that stores the satellite's audio stream."""

    def __init__(self, client: APIClient):
        self.client = client
        self.take: Take | None = None
        self.busy = asyncio.Event()

    def on_log(self, message) -> None:
        if BUSY_MESSAGE in message.message.decode("utf-8", "replace"):
            self.busy.set()

    async def handle_start(
        self, conversation_id: str, flags: int, audio_settings, wake_word: str
    ) -> int | None:
        del conversation_id, flags, audio_settings
        take = self.take
        if take is None or take.started.is_set():
            return None
        _LOGGER.debug("Stream started by %s", wake_word)
        take.started.set()
        # Answer like a pipeline, after the start response has gone out:
        # on_listening in the firmware consumes the wake word flag on STT
        # start.
        loop = asyncio.get_running_loop()
        for event in (
            VoiceAssistantEventType.VOICE_ASSISTANT_RUN_START,
            VoiceAssistantEventType.VOICE_ASSISTANT_STT_START,
        ):
            loop.call_later(0.2, self._send, event)
        # Port 0: the audio arrives over the API connection.
        return 0

    async def handle_stop(self, abort: bool) -> None:
        del abort
        if self.take is not None and self.take.started.is_set():
            self.take.finished.set()

    async def handle_audio(self, data: bytes, data2: bytes | None) -> None:
        del data2
        if self.take is not None and self.take.started.is_set():
            self.take.add(data)

    def _send(self, event: VoiceAssistantEventType) -> None:
        self.client.send_voice_assistant_event(event, None)

    def stop_stream(self) -> None:
        self._send(VoiceAssistantEventType.VOICE_ASSISTANT_RUN_END)


def level_dbfs(chunk: np.ndarray) -> float:
    """RMS level of int16 audio in dBFS."""

    if chunk.size == 0:
        return -120.0
    rms = math.sqrt(float(np.mean((chunk.astype(np.float64) / 32768.0) ** 2)))
    return 20.0 * math.log10(max(rms, 1e-6))


async def show_progress(take: Take) -> None:
    """Print elapsed time and level once a second until the take ends."""

    while not take.finished.is_set():
        await asyncio.sleep(1.0)
        recent = np.frombuffer(b"".join(take.chunks[-40:]), dtype=np.int16)
        elapsed = take.samples / SAMPLE_RATE
        total = take.wanted_samples / SAMPLE_RATE
        level = level_dbfs(recent)
        print(
            f"\r  {elapsed:6.1f} / {total:.0f} s   level {level:6.1f} dBFS",
            end="",
            flush=True,
        )
    print()


def output_path(paths: Paths, args: argparse.Namespace) -> Path:
    """Where a take is stored."""

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"sat_{args.satellite}_{stamp}.wav"
    if args.negative:
        return paths.recordings / "negative" / name
    return paths.recordings / "positive" / args.speaker / name


async def record(args: argparse.Namespace, config: dict, paths: Paths) -> None:
    """Connect to the satellite and record the requested takes."""

    seconds = args.seconds or (1800.0 if args.negative else 60.0)
    skip = 0 if args.negative else config["recordings"]["skip_start_s"]
    device_name = "aivi-sat-" + args.satellite.replace("_", "-")
    client = APIClient(
        args.host,
        args.port,
        None,
        noise_psk=load_api_key(args.secrets, args.satellite),
        client_info="aivi-wake-word-recorder",
        expected_name=device_name,
    )
    await client.connect(login=True)
    recorder = Recorder(client)
    try:
        info = await client.device_info()
        _LOGGER.info(
            "Connected to %s (ESPHome %s)", info.name, info.esphome_version
        )
        client.subscribe_logs(
            recorder.on_log, log_level=LogLevel.LOG_LEVEL_ERROR
        )
        # A state subscription keeps the LED ring from blinking "not ready".
        client.subscribe_states(lambda state: None)
        client.subscribe_voice_assistant(
            handle_start=recorder.handle_start,
            handle_stop=recorder.handle_stop,
            handle_audio=recorder.handle_audio,
        )
        try:
            await asyncio.wait_for(recorder.busy.wait(), timeout=2.0)
            raise SystemExit(
                "Home Assistant still holds the voice assistant. Disable the "
                "satellite's ESPHome entry in Home Assistant and try again."
            )
        except TimeoutError:
            pass

        number = 0
        while args.takes == 0 or number < args.takes:
            number += 1
            recorder.take = Take(seconds + skip)
            what = (
                "everyday sound, no wake word"
                if args.negative
                else f'"Hey AIVI" every 2-3 s ({args.speaker})'
            )
            print(f'\nTake {number}: say "Okay Nabu" to start, then {what}.')
            await recorder.take.started.wait()
            print("  Recording.")
            await show_progress(recorder.take)
            recorder.stop_stream()
            audio = recorder.take.audio()[int(skip * SAMPLE_RATE) :]
            path = output_path(paths, args)
            write_wav(path, audio)
            shown = path.relative_to(REPO_ROOT)
            print(f"  Saved {len(audio) / SAMPLE_RATE:.1f} s to {shown}")
            recorder.take = None
            await asyncio.sleep(1.0)
    finally:
        await client.disconnect()


def main() -> None:
    """Record takes until done or interrupted."""

    args = parse_args()
    setup_logging(args.verbose)
    config = load_config()
    paths = Paths.from_config(config)
    try:
        asyncio.run(record(args, config, paths))
    except KeyboardInterrupt:
        print("\nStopped; the current take was not saved.", file=sys.stderr)


if __name__ == "__main__":
    main()
