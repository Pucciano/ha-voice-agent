"""Listen to every wake word window and rate it on a local page.

Three kinds of windows decide what the training sees, and each one is rated
by ear:

- the clips of single detections in recordings/wake/;
- the detections the sidecar of an everyday take lists;
- the candidates of `make ww-scan`, places in everyday takes that sound
  like the wake word although the satellite did not wake up.

The page runs on a small server on this machine (127.0.0.1 only) that cuts
the audio from the recordings on request, so nothing is copied. Every
rating is saved at once in recordings/review.json, and the server rewrites
its own block in three lists the import reads:

- negative_keep.txt: false accepts, trained on as negatives;
- excluded.txt: windows holding both a false accept and a real wake word;
- negative_cut.txt: scan candidates that hold a wake word, or might; the
  import cuts them from the everyday take like a detection.

Lines outside the block are left alone, so names listed by hand still
count. A real "Hey AIVI" in a detection clip is not listed anywhere: the
clip stays in a review folder and never becomes a negative.
"""

import argparse
import csv
import datetime
import http.server
import io
import json
import logging
import threading
import urllib.parse
import webbrowser
import wave
from pathlib import Path

import numpy as np

from common import REPO_ROOT, SAMPLE_RATE, Paths, load_config, setup_logging

PAGE = Path(__file__).with_name("review_page.html")
VERDICTS = ("f", "e", "u", "x")
BLOCK_START = "# >>> written by review.py, rate on the review page instead"
BLOCK_END = "# <<< review.py"
# Context around a window: the whole clip of a single detection, and this
# much around a window in an everyday take.
CONTEXT_S = (6.0, 4.0)

_LOGGER = logging.getLogger("review")


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--no-browser", action="store_true", help="Do not open the page"
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def window_name(stem: str, offset_s: float, kind: str = "wake") -> str:
    """The clip name the import gives a window."""

    return f"{stem}__{kind}_{round(offset_s * 1000):08d}.wav"


def kind_of(name: str) -> str:
    """The kind of a window, from its name."""

    if "__scan_" in name:
        return "scan"
    return "clip" if name.startswith("clip_") else "take"


def read_json(path: Path, default):
    """JSON content of a file, or the default if it is missing or broken."""

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def listed(path: Path) -> set[str]:
    """Names in a list file outside the block of this tool."""

    if not path.is_file():
        return set()
    names, inside = set(), False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() == BLOCK_START:
            inside = True
        elif line.strip() == BLOCK_END:
            inside = False
        elif not inside and line.strip() and not line.startswith("#"):
            names.add(line.split()[0])
    return names


def stt_index(directory: Path) -> list[tuple[datetime.datetime, dict]]:
    """STT captures by time, from metadata files fetched by `ww-pull`."""

    captures = []
    for path in sorted(directory.glob("*/metadata.json")):
        metadata = read_json(path, {})
        try:
            created = datetime.datetime.fromisoformat(metadata["created_at"])
        except (KeyError, TypeError, ValueError):
            continue
        captures.append((created, metadata))
    return captures


def following_request(
    captures: list[tuple[datetime.datetime, dict]], when: datetime.datetime
) -> str:
    """What the speech recognition understood right after a detection."""

    best = None
    for created, metadata in captures:
        delta = (created - when).total_seconds()
        if -3 < delta < 15 and (best is None or abs(delta) < abs(best[0])):
            best = (delta, metadata)
    if best is None:
        return ""
    metadata = best[1]
    return metadata.get("pseudo_transcript") or (
        f"({metadata.get('capture_outcome', 'no transcript')})"
    )


def whisper_transcripts(paths: Paths) -> dict[str, str]:
    """Whisper's transcripts of the detection clip windows, from the import."""

    listing = paths.clips / "real_negative" / "wake_clips.csv"
    if not listing.is_file():
        return {}
    with listing.open(encoding="utf-8") as source:
        return {
            row["clip"]: " / ".join(
                text for text in (row["transcript_en"], row["transcript_de"])
                if text
            )
            for row in csv.DictReader(source)
        }


def collect(config: dict, paths: Paths) -> list[dict]:
    """Every window to rate, oldest first."""

    before, after = config["recordings"]["negative_cut_window_s"]
    recordings = paths.recordings
    captures = stt_index(paths.data / "stt")
    whisper = whisper_transcripts(paths)
    items = []
    for sidecar in sorted((recordings / "wake").glob("*.json")):
        content = read_json(sidecar, {})
        found = content.get("detections") or []
        wav = sidecar.with_suffix(".wav")
        if not found or not wav.is_file():
            continue
        offset = float(found[0]["offset_s"])
        started = datetime.datetime.fromisoformat(content["started_at"])
        name = window_name(f"clip_{sidecar.stem}", offset)
        items.append(
            {
                "id": name,
                "kind": "clip",
                "file": wav.relative_to(recordings).as_posix(),
                "start_s": max(offset - before, 0.0),
                "end_s": offset + after,
                "context": [0.0, None],
                "when": started.isoformat(),
                "detected_s": offset,
                "after": following_request(captures, started),
                "whisper": whisper.get(name, ""),
            }
        )
    for sidecar in sorted((recordings / "negative").glob("*.json")):
        content = read_json(sidecar, {})
        wav = sidecar.with_suffix(".wav")
        if not wav.is_file():
            continue
        started = datetime.datetime.fromisoformat(content["started_at"])
        for found in content.get("detections") or []:
            offset = float(found["offset_s"])
            items.append(
                take_item(
                    recordings,
                    wav,
                    started,
                    offset,
                    window_name(sidecar.stem, offset),
                    "take",
                    (before, after),
                )
            )
    scan = read_json(paths.data / "review" / "candidates.json", {})
    for candidate in scan.get("candidates", []):
        wav = recordings / "negative" / candidate["recording"]
        if not wav.is_file():
            continue
        offset = float(candidate["offset_s"])
        stem = Path(candidate["recording"]).with_suffix("").as_posix()
        stem = stem.replace("/", "__")
        started = datetime.datetime.fromisoformat(candidate["started_at"])
        item = take_item(
            recordings,
            wav,
            started,
            offset,
            window_name(stem, offset, "scan"),
            "scan",
            (before, after),
        )
        hints = []
        if candidate.get("text"):
            hints.append(candidate["text"])
        if candidate.get("score") is not None:
            hints.append(f"model {candidate['score']:.2f}")
        item["whisper"] = " · ".join(hints)
        item["sources"] = candidate.get("sources", [])
        items.append(item)
    items.sort(key=lambda item: item["when"])
    return items


def take_item(
    recordings: Path,
    wav: Path,
    started: datetime.datetime,
    offset: float,
    name: str,
    kind: str,
    window: tuple[float, float],
) -> dict:
    """A window in an everyday take."""

    when = started + datetime.timedelta(seconds=offset)
    return {
        "id": name,
        "kind": kind,
        "file": wav.relative_to(recordings).as_posix(),
        "start_s": max(offset - window[0], 0.0),
        "end_s": offset + window[1],
        "context": [max(offset - CONTEXT_S[0], 0.0), offset + CONTEXT_S[1]],
        "when": when.isoformat(),
        "detected_s": offset,
        "after": "",
        "whisper": "",
    }


def cut_wav(path: Path, start_s: float, end_s: float | None) -> bytes:
    """A part of a WAV file as a WAV file of its own."""

    with wave.open(str(path), "rb") as source:
        rate = source.getframerate()
        frames = source.getnframes()
        first = min(int(start_s * rate), frames)
        last = frames if end_s is None else min(int(end_s * rate), frames)
        source.setpos(first)
        data = source.readframes(max(last - first, 0))
        channels, width = source.getnchannels(), source.getsampwidth()
    out = io.BytesIO()
    with wave.open(out, "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(width)
        target.setframerate(rate)
        target.writeframes(data)
    return out.getvalue()


def level_dbfs(path: Path, start_s: float, end_s: float) -> float:
    """RMS level of a window."""

    with wave.open(str(path), "rb") as source:
        source.setpos(min(int(start_s * SAMPLE_RATE), source.getnframes()))
        data = source.readframes(int((end_s - start_s) * SAMPLE_RATE))
    audio = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
    if audio.size == 0:
        return -120.0
    return float(20.0 * np.log10(np.sqrt(np.mean(audio**2)) + 1e-9))


def write_block(path: Path, names: list[str], header: str) -> None:
    """Replace this tool's block in a list file, keep everything else."""

    lines = (
        path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    )
    kept, inside = [], False
    for line in lines:
        if line.strip() == BLOCK_START:
            inside = True
        elif line.strip() == BLOCK_END:
            inside = False
        elif not inside:
            kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    if not kept:
        kept = [header]
    block = [BLOCK_START, *sorted(names), BLOCK_END] if names else []
    content = kept + ([""] if block else []) + block
    path.write_text("\n".join(content) + "\n", encoding="utf-8")


class Review:
    """The ratings, the lists they produce, and the windows to rate."""

    def __init__(self, config: dict, paths: Paths):
        self.config = config
        self.paths = paths
        self.store = paths.recordings / "review.json"
        self.lock = threading.Lock()
        self.items = collect(config, paths)
        self.by_id = {item["id"]: item for item in self.items}
        recordings = paths.recordings
        for item in self.items:
            item["level"] = round(
                level_dbfs(
                    recordings / item["file"], item["start_s"], item["end_s"]
                ),
                1,
            )
        self.ratings = read_json(self.store, {}).get("ratings", {})
        self.listed = {
            "keep": listed(recordings / "negative_keep.txt"),
            "excluded": listed(recordings / "excluded.txt"),
            "cut": listed(recordings / "negative_cut.txt"),
        }
        if self.ratings:
            self.write_lists()

    def rate(self, item_id: str, verdict: str | None, note: str) -> None:
        """Save one rating and rewrite the lists."""

        if item_id not in self.by_id:
            raise KeyError(item_id)
        if verdict is not None and verdict not in VERDICTS:
            raise ValueError(verdict)
        with self.lock:
            entry = {
                "verdict": verdict,
                "note": note,
                "rated_at": datetime.datetime.now().isoformat(
                    timespec="seconds"
                ),
            }
            if verdict is None and not note:
                self.ratings.pop(item_id, None)
            else:
                self.ratings[item_id] = entry
            self.store.write_text(
                json.dumps({"ratings": self.ratings}, indent=2) + "\n",
                encoding="utf-8",
            )
            self.write_lists()

    def write_lists(self) -> None:
        """The import's lists, from the ratings."""

        keep, excluded, cut = [], [], []
        for item_id, entry in self.ratings.items():
            verdict = entry.get("verdict")
            if verdict is None:
                continue
            # A rated window that is gone from the page, e.g. after a new
            # scan, still counts: its name tells its kind.
            kind = self.by_id.get(item_id, {}).get("kind") or kind_of(item_id)
            if kind == "scan":
                if verdict in ("e", "u", "x"):
                    cut.append(item_id)
            elif verdict == "f":
                keep.append(item_id)
            elif verdict == "x" and kind == "clip":
                excluded.append(item_id)
        recordings = self.paths.recordings
        write_block(
            recordings / "negative_keep.txt",
            keep,
            "# False accepts, trained on as negatives.",
        )
        write_block(
            recordings / "excluded.txt",
            excluded,
            "# Clips that never enter the training.",
        )
        write_block(
            recordings / "negative_cut.txt",
            cut,
            "# Windows cut from everyday takes without a detection.",
        )

    def state(self) -> dict:
        """Items and ratings for the page."""

        return {
            "items": self.items,
            "ratings": self.ratings,
            "listed": {
                key: sorted(value) for key, value in self.listed.items()
            },
        }


def handler(review: Review):
    """Request handler bound to one review."""

    class Handler(http.server.BaseHTTPRequestHandler):
        """Page, audio and ratings."""

        def log_message(self, format, *args):  # pylint: disable=W0622
            _LOGGER.debug(format, *args)

        def send(self, status: int, body: bytes, kind: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # pylint: disable=C0103
            url = urllib.parse.urlparse(self.path)
            if url.path == "/":
                self.send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/state":
                body = json.dumps(review.state(), ensure_ascii=False)
                self.send(200, body.encode(), "application/json")
            elif url.path == "/audio":
                query = urllib.parse.parse_qs(url.query)
                item = review.by_id.get(query.get("id", [""])[0])
                if item is None:
                    self.send(404, b"unknown window", "text/plain")
                    return
                if query.get("part", ["window"])[0] == "context":
                    start, end = item["context"]
                else:
                    start, end = item["start_s"], item["end_s"]
                path = review.paths.recordings / item["file"]
                self.send(200, cut_wav(path, start, end), "audio/wav")
            else:
                self.send(404, b"not found", "text/plain")

        def do_POST(self):  # pylint: disable=C0103
            if self.path != "/api/rate":
                self.send(404, b"not found", "text/plain")
                return
            length = int(self.headers.get("Content-Length", "0"))
            try:
                body = json.loads(self.rfile.read(length))
                review.rate(
                    body["id"], body.get("verdict"), body.get("note", "")
                )
            except (KeyError, ValueError, TypeError) as error:
                self.send(400, str(error).encode(), "text/plain")
                return
            self.send(200, b"{}", "application/json")

    return Handler


def main() -> None:
    """Serve the review page until Ctrl-C."""

    args = parse_args()
    setup_logging(args.verbose)
    config = load_config()
    paths = Paths.from_config(config)
    review = Review(config, paths)
    kinds: dict[str, int] = {}
    for item in review.items:
        kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
    _LOGGER.info(
        "%d windows (%s), %d rated",
        len(review.items),
        ", ".join(f"{n} {kind}" for kind, n in sorted(kinds.items())),
        len(review.ratings),
    )
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", args.port), handler(review)
    )
    url = f"http://127.0.0.1:{args.port}/"
    _LOGGER.info("Review page at %s; Ctrl-C ends it", url)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    _LOGGER.info("Ratings in %s", review.store.relative_to(REPO_ROOT))


if __name__ == "__main__":
    main()
