"""Generate synthetic wake word samples and check each one with Whisper.

The English generator mixes two of 600 speakers per sample; German Piper
voices add a German accent. Every sample speaks one of the IPA phrases from
config.yaml. Whisper transcribes each sample: a positive sample is kept only
if it says the wake word, a negative one only if it does not. samples.csv in
the output folder lists every sample with its source, phrase, settings,
transcript and whether it was kept. `--preview` writes a few kept samples
per phrase and voice to listen to first.
"""

import argparse
import collections
import csv
import itertools
import json
import logging
import os
import random
import re
import unicodedata
from pathlib import Path

import numpy as np
import torch

from common import (
    SAMPLE_RATE,
    Paths,
    load_config,
    reset_directory,
    resample,
    setup_logging,
    use_vendor,
    write_wav,
)

KINDS = ("positive", "negative")
PREVIEW_GAP_S = 0.35
# The phrases are a few words; a longer transcript is a hallucination on
# garbled audio and only costs time.
MAX_TOKENS = 24
# Give up on a voice that keeps less than this share of its samples.
MIN_KEEP_RATE = 0.05
MIN_ATTEMPTS = 300
CSV_FIELDS = (
    "file",
    "kept",
    "reason",
    "transcript",
    "voice",
    "phrase_no",
    "phrase",
    "speakers",
    "length_scale",
    "noise_scale",
    "noise_scale_w",
    "slerp_weight",
    "duration_s",
)

_LOGGER = logging.getLogger("samples")


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Write a few samples per phrase and voice for listening",
    )
    parser.add_argument("--kind", choices=KINDS, nargs="+", default=list(KINDS))
    parser.add_argument(
        "--voices",
        nargs="+",
        help="Generate only these voices; the other voices' samples stay",
    )
    parser.add_argument(
        "--count", type=int, help="Override the configured sample count"
    )
    parser.add_argument(
        "--phonemize",
        metavar="TEXT",
        help="Print the espeak IPA of TEXT for German and English, then exit",
    )
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def pick_device() -> torch.device:
    """Use the GPU when PyTorch has one; piper moves its inputs there."""

    if torch.cuda.is_available():
        return torch.device("cuda")
    if torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def trim_silence(audio: np.ndarray, rate: int) -> np.ndarray:
    """Cut leading and trailing parts more than 30 dB below the loudest."""

    frame = rate // 100
    frames = len(audio) // frame
    if frames == 0:
        return audio
    rms = np.sqrt(
        np.mean(audio[: frames * frame].reshape(frames, frame) ** 2, axis=1)
    )
    loud = np.flatnonzero(rms > rms.max() * 0.03)
    margin = 5
    start = max(int(loud[0]) - margin, 0) * frame
    end = min(int(loud[-1]) + 1 + margin, frames) * frame
    return audio[start:end]


def finish(audio: np.ndarray, rate: int) -> np.ndarray | None:
    """Trim, normalize and resample one sample to 16 kHz."""

    audio = trim_silence(np.asarray(audio, dtype=np.float32), rate)
    peak = float(np.max(np.abs(audio), initial=0.0))
    if peak < 1e-4:
        return None
    return resample(audio / peak * 0.9, rate, SAMPLE_RATE)


def phoneme_ids(id_map: dict, phrase: str) -> list[int]:
    """Piper phoneme ids of an IPA phrase, with the usual padding."""

    ids = list(id_map["^"]) + list(id_map["_"])
    for symbol in unicodedata.normalize("NFD", phrase):
        ids += list(id_map[symbol]) + list(id_map["_"])
    return ids + list(id_map["$"])


class SpeakerMixGenerator:
    """A Piper generator that mixes the embeddings of two speakers."""

    def __init__(self, name: str, paths: Paths, spec: dict):
        # Imported here: the module configures logging and loads espeak.
        # pylint: disable=import-outside-toplevel,import-error
        from piper_sample_generator import __main__ as psg

        self.generate_audio = psg.generate_audio
        model_path = paths.generators / f"{name}.pt"
        with model_path.with_name(model_path.name + ".json").open(
            encoding="utf-8"
        ) as handle:
            config = json.load(handle)
        self.id_map = config["phoneme_id_map"]
        self.sample_rate = config["audio"]["sample_rate"]
        self.num_speakers = min(config["num_speakers"], spec["max_speakers"])
        self.device = pick_device()
        self.model = torch.load(model_path, weights_only=False)
        self.model.eval()
        self.model.to(self.device)

    def synthesize(
        self,
        phrase: str,
        count: int,
        rng: random.Random,
        settings: dict,
        max_phoneme_s: float,
    ) -> list[tuple[np.ndarray | None, str]]:
        """Speak one phrase with random speaker pairs: (audio, speakers).

        Samples with one sound or pause longer than `max_phoneme_s` come back
        without audio.
        """

        pairs = [
            (rng.randrange(self.num_speakers), rng.randrange(self.num_speakers))
            for _ in range(count)
        ]
        with torch.no_grad():
            audio, phoneme_samples = self.generate_audio(
                self.model,
                torch.LongTensor([first for first, _ in pairs]),
                torch.LongTensor([second for _, second in pairs]),
                [phoneme_ids(self.id_map, phrase)] * count,
                settings["slerp_weight"],
                settings["noise_scale"],
                settings["noise_scale_w"],
                settings["length_scale"],
                None,
            )
        audio = audio.cpu().numpy()
        durations = phoneme_samples.cpu().numpy().reshape(count, -1)
        if self.device.type == "mps":
            torch.mps.empty_cache()

        results: list[tuple[np.ndarray | None, str]] = []
        for index, (first, second) in enumerate(pairs):
            speakers = f"{first}+{second}"
            if durations[index].max() / self.sample_rate > max_phoneme_s:
                results.append((None, speakers))
                continue
            sample = audio[index, 0, : int(durations[index].sum())]
            results.append((finish(sample, self.sample_rate), speakers))
        return results


class PiperVoiceGenerator:
    """A regular Piper voice with one or a few speakers."""

    def __init__(self, name: str, paths: Paths, spec: dict):
        del spec
        # pylint: disable=import-outside-toplevel
        from piper import PiperVoice, SynthesisConfig

        self.synthesis_config = SynthesisConfig
        model_path = next(paths.voices.glob(f"**/{name}.onnx"), None)
        if model_path is None:
            raise SystemExit(f"Voice {name} is missing; run `make ww-setup`.")
        self.voice = PiperVoice.load(str(model_path))
        self.id_map = self.voice.config.phoneme_id_map
        self.sample_rate = self.voice.config.sample_rate
        self.num_speakers = self.voice.config.num_speakers

    def synthesize(
        self,
        phrase: str,
        count: int,
        rng: random.Random,
        settings: dict,
        max_phoneme_s: float,
    ) -> list[tuple[np.ndarray | None, str]]:
        """Speak one phrase `count` times: (audio, speaker)."""

        del max_phoneme_s
        ids = phoneme_ids(self.id_map, phrase)
        results: list[tuple[np.ndarray | None, str]] = []
        for _ in range(count):
            speaker = (
                rng.randrange(self.num_speakers)
                if self.num_speakers > 1
                else None
            )
            audio = self.voice.phoneme_ids_to_audio(
                ids,
                self.synthesis_config(
                    speaker_id=speaker,
                    length_scale=settings["length_scale"],
                    noise_scale=settings["noise_scale"],
                    noise_w_scale=settings["noise_scale_w"],
                ),
            )
            results.append((finish(audio, self.sample_rate), str(speaker or 0)))
        return results


def make_generator(name: str, paths: Paths, spec: dict):
    """The generator for a configured voice."""

    if spec["kind"] == "speaker_mix":
        return SpeakerMixGenerator(name, paths, spec)
    if spec["kind"] == "piper":
        return PiperVoiceGenerator(name, paths, spec)
    raise SystemExit(f"{name}: unknown kind {spec['kind']}")


def check_phrases(name: str, id_map: dict, phrases: list[str]) -> None:
    """Fail on IPA symbols a voice does not know."""

    for phrase in phrases:
        unknown = {
            symbol
            for symbol in unicodedata.normalize("NFD", phrase)
            if symbol not in id_map
        }
        if unknown:
            raise SystemExit(
                f"{name} does not know {sorted(unknown)} in '{phrase}'"
            )


class SpeechCheck:
    """Transcribes samples with Whisper: MLX on Apple Silicon, else CPU.

    The first model transcribes English; the second, multilingual one gives
    a second opinion in another language.
    """

    def __init__(self, settings: dict, paths: Paths):
        os.environ.setdefault("HF_HUB_CACHE", str(paths.cache / "huggingface"))
        self.pattern = re.compile(settings["wake_word_pattern"])
        self._settings = settings
        self._models: dict[str, object] = {}
        # pylint: disable=import-outside-toplevel,import-error
        try:
            import mlx_whisper

            self._mlx = mlx_whisper
        except ImportError:
            self._mlx = None

    def _transcribe(self, audio: np.ndarray, second: bool) -> str:
        audio = audio.astype(np.float32)
        language = self._settings["second_language"] if second else "en"
        if self._mlx is not None:
            key = "second_mlx_model" if second else "mlx_model"
            result = self._mlx.transcribe(
                audio,
                path_or_hf_repo=self._settings[key],
                language=language,
                temperature=0.0,
                condition_on_previous_text=False,
                without_timestamps=True,
                sample_len=MAX_TOKENS,
            )
            return result["text"].strip()
        key = "second_model" if second else "model"
        if key not in self._models:
            # pylint: disable=import-outside-toplevel,import-error
            from faster_whisper import WhisperModel

            self._models[key] = WhisperModel(
                self._settings[key], compute_type="int8"
            )
        segments, _ = self._models[key].transcribe(
            audio,
            language=language,
            beam_size=5,
            temperature=0.0,
            condition_on_previous_text=False,
            without_timestamps=True,
            max_new_tokens=MAX_TOKENS,
        )
        return " ".join(segment.text.strip() for segment in segments)

    def transcribe(self, audio: np.ndarray) -> str:
        """English transcript of 16 kHz audio."""

        return self._transcribe(audio, second=False)

    def transcribe_second(self, audio: np.ndarray) -> str:
        """Second-opinion transcript of 16 kHz audio."""

        return self._transcribe(audio, second=True)

    def says_wake_word(self, transcript: str) -> bool:
        """Whether a transcript is the wake word."""

        letters = re.sub(r"[^a-z]", "", transcript.lower())
        return bool(self.pattern.match(letters))


def random_settings(config: dict, spec: dict, rng: random.Random) -> dict:
    """Pick one set of synthesis settings for a batch."""

    samples = config["samples"]
    return {
        "length_scale": rng.choice(spec["length_scales"]),
        "noise_scale": rng.choice(samples["noise_scales"]),
        "noise_scale_w": rng.choice(spec["noise_scale_ws"]),
        "slerp_weight": rng.choice(samples["slerp_weights"]),
    }


def middle_settings(spec: dict) -> dict:
    """Typical synthesis settings of a voice, for the preview."""

    def middle(values: list[float]) -> float:
        return sorted(values)[len(values) // 2]

    return {
        "length_scale": middle(spec["length_scales"]),
        "noise_scale": 0.667,
        "noise_scale_w": middle(spec["noise_scale_ws"]),
        "slerp_weight": 0.5,
    }


def judge_batch(
    clips: list[np.ndarray | None],
    kind: str,
    samples: dict,
    check: SpeechCheck,
    double_check: bool,
) -> list[tuple[bool, str, str]]:
    """Whether to keep each sample of a batch: (keep, reason, transcript).

    Each Whisper model runs over the whole batch in turn; MLX keeps only
    one model loaded.
    """

    verdicts: list[tuple[bool, str, str]] = []
    for clip in clips:
        if clip is None:
            verdicts.append((False, "stretched or silent", ""))
        elif len(clip) / SAMPLE_RATE < samples["min_duration_s"]:
            verdicts.append((False, "too short", ""))
        elif len(clip) / SAMPLE_RATE > samples["max_duration_s"]:
            verdicts.append((False, "too long", ""))
        else:
            transcript = check.transcribe(clip)
            if check.says_wake_word(transcript) != (kind == "positive"):
                verdicts.append((False, "whisper", transcript))
            else:
                verdicts.append((True, "", transcript))
    if double_check and kind == "positive":
        for index, clip in enumerate(clips):
            keep, _, transcript = verdicts[index]
            if not keep:
                continue
            second = check.transcribe_second(clip)
            if not check.says_wake_word(second):
                verdicts[index] = (
                    False,
                    "second whisper",
                    f"{transcript} / {second}",
                )
            else:
                verdicts[index] = (True, "", f"{transcript} / {second}")
    return verdicts


def phonemize(text: str) -> None:
    """Print the espeak IPA of a text in German and English."""

    # pylint: disable=import-outside-toplevel
    from piper.phonemize_espeak import EspeakPhonemizer

    phonemizer = EspeakPhonemizer()
    for voice in ("de", "en-us"):
        sentences = phonemizer.phonemize(voice, text)
        print(f"{voice:6} {' '.join(''.join(s) for s in sentences)}")


def preview_phrase(
    generator,
    phrase: str,
    kind: str,
    spec: dict,
    samples: dict,
    check: SpeechCheck,
    rng: random.Random,
) -> tuple[list[tuple[np.ndarray, str]], list[str]]:
    """Kept (audio, transcript) and rejected transcripts for one phrase."""

    per_phrase = samples["preview_per_phrase"]
    kept: list[tuple[np.ndarray, str]] = []
    rejected: list[str] = []
    while len(kept) < per_phrase and len(rejected) < 4 * per_phrase:
        clips = [
            clip
            for clip, _ in generator.synthesize(
                phrase,
                per_phrase,
                rng,
                middle_settings(spec),
                samples["max_phoneme_s"],
            )
        ]
        verdicts = judge_batch(
            clips, kind, samples, check, spec.get("double_check", False)
        )
        for clip, (keep, reason, text) in zip(clips, verdicts):
            if keep and len(kept) < per_phrase:
                kept.append((clip, text))
            elif not keep:
                rejected.append(text or reason)
    return kept, rejected


def preview(
    config: dict, paths: Paths, kinds: list[str], voices: list[str], seed: int
) -> None:
    """Write kept samples per phrase and voice, joined in one file each."""

    samples = config["samples"]
    rng = random.Random(seed)
    check = SpeechCheck(samples["check"], paths)
    gap = np.zeros(int(PREVIEW_GAP_S * SAMPLE_RATE), dtype=np.float32)
    for kind in kinds:
        (paths.preview / kind).mkdir(parents=True, exist_ok=True)
        for name in voices:
            for old in (paths.preview / kind).glob(f"*{name}*"):
                old.unlink()
    for name in voices:
        spec = samples["voices"][name]
        generator = make_generator(name, paths, spec)
        for kind in kinds:
            phrases = samples[kind]["phrases"]
            check_phrases(name, generator.id_map, phrases)
            lines = []
            for number, phrase in enumerate(phrases, start=1):
                kept, rejected = preview_phrase(
                    generator, phrase, kind, spec, samples, check, rng
                )
                if kept:
                    joined = np.concatenate(
                        list(
                            itertools.chain.from_iterable(
                                (clip, gap) for clip, _ in kept
                            )
                        )
                    )
                    write_wav(
                        paths.preview / kind / f"{number:02d}_{name}.wav",
                        joined,
                    )
                heard = ", ".join(repr(text) for _, text in kept)
                lines.append(
                    f"{number:02d}  {phrase}\n    kept {len(kept)}: {heard}\n"
                    f"    rejected {len(rejected)}: {rejected[:6]}"
                )
            (paths.preview / kind / f"phrases_{name}.txt").write_text(
                "\n".join(lines) + "\n", encoding="utf-8"
            )
            _LOGGER.info("%s %s preview written", name, kind)


def prepare_output(
    target: Path, voices: list[str], configured: list[str]
) -> list[dict]:
    """Clear the samples of the given voices and of voices no longer
    configured; return the log rows of the samples that stay."""

    log_path = target / "samples.csv"
    if set(voices) == set(configured) or not log_path.is_file():
        reset_directory(target)
        return []
    with log_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    stay = [
        row
        for row in rows
        if row["voice"] in configured and row["voice"] not in voices
    ]
    stay_files = {row["file"] for row in stay if row["file"]}
    for wav in target.glob("*.wav"):
        if wav.name not in stay_files:
            wav.unlink()
    return stay


def generate(
    config: dict,
    paths: Paths,
    kind: str,
    voices: list[str],
    count: int | None,
    seed: int,
) -> None:
    """Generate the sample set of one kind, checking every sample.

    Only the given voices are generated anew; the samples of the other
    voices stay.
    """

    samples = config["samples"]
    total = count if count is not None else samples[kind]["count"]
    phrases = samples[kind]["phrases"]
    target = paths.clips / f"tts_{kind}"
    kept_rows = prepare_output(target, voices, list(samples["voices"]))

    check = SpeechCheck(samples["check"], paths)
    with (target / "samples.csv").open(
        "w", newline="", encoding="utf-8"
    ) as log:
        writer = csv.DictWriter(log, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(kept_rows)
        for name in voices:
            spec = samples["voices"][name]
            rng = random.Random(f"{seed}-{kind}-{name}")
            wanted = round(total * spec["share"])
            generator = make_generator(name, paths, spec)
            check_phrases(name, generator.id_map, phrases)
            order = list(enumerate(phrases, start=1))
            rng.shuffle(order)
            reasons: collections.Counter = collections.Counter()
            kept = attempts = 0
            for number, phrase in itertools.cycle(order):
                if kept >= wanted:
                    break
                if attempts >= MIN_ATTEMPTS and kept < attempts * MIN_KEEP_RATE:
                    raise SystemExit(
                        f"{name} kept only {kept} of {attempts} {kind} samples"
                    )
                settings = random_settings(config, spec, rng)
                batch = generator.synthesize(
                    phrase,
                    samples["batch_size"],
                    rng,
                    settings,
                    samples["max_phoneme_s"],
                )
                verdicts = judge_batch(
                    [clip for clip, _ in batch],
                    kind,
                    samples,
                    check,
                    spec.get("double_check", False),
                )
                for (clip, speakers), (keep, reason, transcript) in zip(
                    batch, verdicts
                ):
                    attempts += 1
                    keep = keep and kept < wanted
                    file_name = ""
                    if keep:
                        file_name = f"{name}_p{number:02d}_{kept:06d}.wav"
                        write_wav(target / file_name, clip)
                        kept += 1
                    else:
                        reasons[reason or "enough"] += 1
                    writer.writerow(
                        {
                            "file": file_name,
                            "kept": int(keep),
                            "reason": reason,
                            "transcript": transcript,
                            "voice": name,
                            "phrase_no": number,
                            "phrase": phrase,
                            "speakers": speakers,
                            "duration_s": (
                                f"{len(clip) / SAMPLE_RATE:.2f}"
                                if clip is not None
                                else ""
                            ),
                            **settings,
                        }
                    )
                log.flush()
            _LOGGER.info(
                "%s %s: kept %d of %d (%s)",
                name,
                kind,
                kept,
                attempts,
                ", ".join(f"{r} {n}" for r, n in reasons.most_common()),
            )
    _LOGGER.info("%s samples and samples.csv in %s", kind, target)


def main() -> None:
    """Generate preview or training samples."""

    args = parse_args()
    config = load_config()
    paths = Paths.from_config(config)
    use_vendor(paths)
    if args.phonemize:
        phonemize(args.phonemize)
        return
    voices = args.voices or list(config["samples"]["voices"])
    unknown = set(voices) - set(config["samples"]["voices"])
    if unknown:
        raise SystemExit(f"Unknown voices: {sorted(unknown)}")
    # Importing piper-sample-generator sets up logging; take it back.
    # pylint: disable=import-outside-toplevel,import-error,unused-import
    from piper_sample_generator import __main__  # noqa: F401

    setup_logging(args.verbose)
    if args.preview:
        preview(config, paths, args.kind, voices, args.seed)
        return
    for kind in args.kind:
        generate(config, paths, kind, voices, args.count, args.seed)


if __name__ == "__main__":
    main()
