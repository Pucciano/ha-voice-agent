"""Whisper transcripts to check what a clip says.

MLX Whisper runs on the Apple GPU; elsewhere faster-whisper runs on the
CPU. The first model transcribes English; the second, multilingual one
gives a second opinion in another language.
"""

import os
import re

import numpy as np

from common import Paths

# The phrases are a few words; a longer transcript is a hallucination on
# garbled audio and only costs time.
MAX_TOKENS = 24


class SpeechCheck:
    """Transcribes clips with the two Whisper models of the config."""

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

    def matches(self, pattern: str, transcript: str) -> bool:
        """Whether a transcript, letters only, matches another pattern."""

        letters = re.sub(r"[^a-z]", "", transcript.lower())
        return bool(re.match(pattern, letters))
