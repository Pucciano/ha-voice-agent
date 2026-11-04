"""Read the microphone level history of a satellite from Home Assistant.

The satellite publishes RMS and peak level per I2S channel every two
seconds (esphome/packages/aivi-diagnostics.yaml). Home Assistant keeps
hourly statistics long term and five minute statistics for about ten days,
which is enough to see the noise floor, the daily rhythm, clipping and the
level around a single wake word detection. See docs/microphone_levels.md.

    python scripts/satellite_levels.py --env-file .env
    python scripts/satellite_levels.py --days 2 --around 2026-11-01T17:21:58Z
"""

import argparse
import asyncio
import collections
import datetime
import json
import os
import pathlib
import statistics
import sys
from typing import Any, Iterable

import websockets

# The channels of the XVF3800: 0 is the hotter one, 1 feeds the wake word
# and the pipeline (esphome/packages/aivi-voice-input.yaml).
CHANNELS = (0, 1)
DEFAULT_PREFIX = "sensor.wohnung_wohnzimmer_aivi_satellit"
# A peak this close to full scale means the channel clipped.
CLIP_DBFS = -0.5
# Five minute statistics are kept for about ten days.
SHORT_TERM_DAYS = 10


class LevelError(Exception):
    """The levels could not be read."""


def read_env_file(path: pathlib.Path) -> dict[str, str]:
    """Read KEY=VALUE lines, as in a shell or compose env file."""

    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def credentials(env_file: pathlib.Path | None) -> tuple[str, str]:
    """Home Assistant URL and token; the environment wins over the file."""

    try:
        values = read_env_file(env_file) if env_file else {}
    except OSError as err:
        raise LevelError(f"{env_file}: {err.strerror}") from err
    url = os.environ.get("HA_URL") or values.get("HA_URL")
    token = os.environ.get("HA_TOKEN") or values.get("HA_TOKEN")
    if not url or not token:
        raise LevelError("set HA_URL and HA_TOKEN or pass --env-file")
    return url, token


async def statistics_during_period(
    url: str, token: str, requests: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Send recorder/statistics_during_period requests over one connection."""

    websocket_url = url.replace("http", "ws", 1) + "/api/websocket"
    results: list[dict[str, Any]] = []
    async with websockets.connect(websocket_url, max_size=None) as websocket:
        await websocket.recv()
        await websocket.send(
            json.dumps({"type": "auth", "access_token": token})
        )
        answer = json.loads(await websocket.recv())
        if answer.get("type") != "auth_ok":
            raise LevelError("Home Assistant refused the token")
        for number, request in enumerate(requests, start=1):
            message = {
                "id": number,
                "type": "recorder/statistics_during_period",
                **request,
            }
            await websocket.send(json.dumps(message))
            while True:
                answer = json.loads(await websocket.recv())
                if answer.get("id") != number:
                    continue
                if answer.get("type") != "result":
                    continue
                if not answer.get("success"):
                    error = answer.get("error", {}).get("message", "refused")
                    raise LevelError(f"statistics refused: {error}")
                results.append(answer["result"])
                break
    return results


def ids(prefix: str) -> dict[str, str]:
    """Statistic id per series, keyed rms0, peak0, rms1, peak1."""

    names = {"rms": "pegel_kanal", "peak": "spitzenpegel_kanal"}
    return {
        f"{series}{channel}": f"{prefix}_{name}_{channel}"
        for channel in CHANNELS
        for series, name in names.items()
    }


def buckets(
    result: dict[str, Any], statistic_id: str
) -> dict[datetime.datetime, dict[str, Any]]:
    """The buckets of one series, by their start in local time."""

    rows = result.get(statistic_id) or []
    return {
        datetime.datetime.fromtimestamp(row["start"] / 1000).astimezone(): row
        for row in rows
    }


def floor_to(point: datetime.datetime, minutes: int) -> datetime.datetime:
    """The start of the statistics bucket that holds `point`."""

    minute = (point.minute // minutes) * minutes
    return point.replace(minute=minute, second=0, microsecond=0)


def moment(value: str) -> datetime.datetime:
    """An ISO timestamp, as a capture directory name carries it."""

    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as err:
        raise argparse.ArgumentTypeError(f"not a timestamp: {value}") from err
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone()


def print_days(
    hours: dict[str, dict[datetime.datetime, dict[str, Any]]]
) -> None:
    """One line per day: noise floor, average level, peaks and clipping."""

    per_day = collections.defaultdict(list)
    for start in sorted(hours["rms1"]):
        per_day[start.date()].append(start)
    print("day        ch1 floor   ch1 mean   ch1 peak   ch0 mean   clip h")
    for day, starts in sorted(per_day.items()):
        floor = min(hours["rms1"][s]["min"] for s in starts)
        mean = statistics.fmean(hours["rms1"][s]["mean"] for s in starts)
        peak = max(hours["peak1"][s]["max"] for s in starts)
        mean0 = statistics.fmean(hours["rms0"][s]["mean"] for s in starts)
        clip1 = sum(1 for s in starts if hours["peak1"][s]["max"] >= CLIP_DBFS)
        clip0 = sum(1 for s in starts if hours["peak0"][s]["max"] >= CLIP_DBFS)
        print(
            f"{day}  {floor:9.1f}  {mean:9.1f}  {peak:9.1f}  {mean0:9.1f}  "
            f"ch1 {clip1:2d}, ch0 {clip0:2d}"
        )


def print_profile(
    hours: dict[str, dict[datetime.datetime, dict[str, Any]]]
) -> None:
    """The average level per hour of the day, the room's rhythm."""

    per_hour = collections.defaultdict(list)
    for start, bucket in hours["rms1"].items():
        per_hour[start.hour].append(bucket["mean"])
    print("\nch1 RMS per hour of the day, dBFS")
    for row in range(0, 24, 8):
        line = "  ".join(
            f"{hour:02d} {statistics.fmean(per_hour[hour]):6.1f}"
            for hour in range(row, row + 8)
            if hour in per_hour
        )
        print(f"  {line}")


def print_summary(
    hours: dict[str, dict[datetime.datetime, dict[str, Any]]]
) -> None:
    """The numbers that say whether the microphone itself is healthy."""

    starts = sorted(hours["rms1"])
    floors = [hours["rms1"][s]["min"] for s in starts]
    print(f"\n{len(starts)} hourly buckets, {starts[0]} to {starts[-1]}")
    missing = sum(
        1
        for index in range(1, len(starts))
        if starts[index] - starts[index - 1] > datetime.timedelta(hours=1)
    )
    print(f"  gaps in the history: {missing}")
    # The median of the hourly minima, not their maximum: a loud hour has
    # no quiet minute in it and says nothing about the floor.
    print(
        f"  ch1 noise floor: lowest {min(floors):.1f} dBFS, median of the "
        f"hourly minima {statistics.median(floors):.1f} dBFS "
        "(a floor that stays put means the microphone is unchanged)"
    )
    for channel in CHANNELS:
        clipped = sum(
            1 for s in starts if hours[f"peak{channel}"][s]["max"] >= CLIP_DBFS
        )
        print(
            f"  ch{channel} clipped in {clipped} of {len(starts)} hours "
            f"(peak >= {CLIP_DBFS} dBFS)"
        )


def print_around(
    short: dict[str, dict[datetime.datetime, dict[str, Any]]],
    moments: Iterable[datetime.datetime],
) -> None:
    """The five minute levels before and after single detections."""

    for point in moments:
        print(f"\naround {point:%Y-%m-%d %H:%M:%S %Z}")
        for offset in (-10, -5, 0, 5):
            start = floor_to(point + datetime.timedelta(minutes=offset), 5)
            rms = short["rms1"].get(start)
            peak = short["peak1"].get(start)
            if rms is None or peak is None:
                print(f"  {offset:+3d} min  no statistics")
                continue
            print(
                f"  {offset:+3d} min  ch1 RMS {rms['mean']:6.1f}  "
                f"peak {peak['max']:6.1f}"
            )


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(
        description="Read the microphone level history of a satellite from "
        "Home Assistant: noise floor, daily rhythm, clipping, and the level "
        "around single wake word detections."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="how many days of hourly statistics (default: 7)",
    )
    parser.add_argument(
        "--around",
        type=moment,
        action="append",
        metavar="TIMESTAMP",
        help="print five minute levels around this moment (repeatable); "
        "a capture directory name such as 20261101T172158Z works",
    )
    parser.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        help=f"entity id prefix of the satellite (default: {DEFAULT_PREFIX})",
    )
    parser.add_argument(
        "--env-file",
        type=pathlib.Path,
        help="file with HA_URL and HA_TOKEN",
    )
    parser.add_argument(
        "--json", action="store_true", help="print the raw statistics"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Print the level history; 1 on a configuration or request error."""

    args = parse_args(argv)
    try:
        url, token = credentials(args.env_file)
    except LevelError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    series = ids(args.prefix)
    now = datetime.datetime.now().astimezone()
    requests: list[dict[str, Any]] = [
        {
            "start_time": (
                now - datetime.timedelta(days=args.days)
            ).isoformat(),
            "period": "hour",
            "statistic_ids": sorted(series.values()),
            "types": ["mean", "min", "max"],
        }
    ]
    if args.around:
        oldest = min(args.around)
        if now - oldest > datetime.timedelta(days=SHORT_TERM_DAYS):
            print(
                f"error: {oldest:%Y-%m-%d} is older than the "
                f"{SHORT_TERM_DAYS} days of five minute statistics",
                file=sys.stderr,
            )
            return 1
        requests.append(
            {
                "start_time": floor_to(
                    oldest - datetime.timedelta(minutes=15), 5
                ).isoformat(),
                "period": "5minute",
                "statistic_ids": [series["rms1"], series["peak1"]],
                "types": ["mean", "max"],
            }
        )

    try:
        results = asyncio.run(statistics_during_period(url, token, requests))
    except (LevelError, OSError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    hours = {
        key: buckets(results[0], statistic_id)
        for key, statistic_id in series.items()
    }
    if not hours["rms1"]:
        print(f"error: no statistics for {series['rms1']}", file=sys.stderr)
        return 1
    print_days(hours)
    print_profile(hours)
    print_summary(hours)
    if args.around:
        short = {
            key: buckets(results[1], series[key]) for key in ("rms1", "peak1")
        }
        print_around(short, sorted(args.around))
    return 0


if __name__ == "__main__":
    sys.exit(main())
