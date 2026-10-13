"""Client for an OpenAI-compatible `/audio/transcriptions` endpoint.

The same multipart request works for Mistral (Voxtral), OpenAI and local
Whisper servers. They differ in how they take hints about rare words:
Mistral has `context_bias`, repeated, up to 100 single words without spaces
or commas; OpenAI and Whisper servers a free-text `prompt`.
"""

import dataclasses
import io
import re
import wave

import httpx

BIAS_FIELDS = ("context_bias", "prompt", "none")
MAX_BIAS_WORDS = 100
# A word worth a hint: capitalised, as German nouns and names are, letters
# with inner hyphens, at least four long. Numbers, codes such as 0x… and
# position words like "links" in "Wohnzimmerfenster links" are left out.
_BIAS_WORD = re.compile(r"[^\W\d_](?:[^\W\d_]|-(?=[^\W\d_])){3,}")


class TranscriptionError(Exception):
    """The upstream gave no transcript; the message is safe to log."""


@dataclasses.dataclass(frozen=True)
class TranscriptionApi:
    base_url: str
    api_key: str = dataclasses.field(repr=False)
    model: str
    bias_field: str = "context_bias"
    timeout: float = 15.0

    @property
    def url(self) -> str:
        return f"{self.base_url.rstrip('/')}/audio/transcriptions"


def wav_bytes(pcm: bytes, rate: int, width: int, channels: int) -> bytes:
    """Wrap raw PCM in a WAV container."""

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


def bias_words(terms: tuple[str, ...]) -> list[str]:
    """Single words of the names, in order, without repeats."""

    words: list[str] = []
    seen: set[str] = set()
    for term in terms:
        for word in term.split():
            if not (word[0].isupper() and _BIAS_WORD.fullmatch(word)):
                continue
            if word.casefold() in seen:
                continue
            seen.add(word.casefold())
            words.append(word)
            if len(words) == MAX_BIAS_WORDS:
                return words
    return words


def form_fields(
    api: TranscriptionApi, language: str | None, terms: tuple[str, ...]
) -> dict[str, str | list[str]]:
    """The multipart form fields besides the file."""

    fields: dict[str, str | list[str]] = {"model": api.model}
    if language:
        fields["language"] = language
    if terms and api.bias_field == "context_bias":
        fields["context_bias"] = bias_words(terms)
    elif terms and api.bias_field == "prompt":
        fields["prompt"] = ", ".join(terms)
    return fields


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    error = body.get("error", body) if isinstance(body, dict) else body
    if isinstance(error, dict):
        error = error.get("message") or error.get("detail") or error
    return str(error)[:200]


async def transcribe(
    client: httpx.AsyncClient,
    api: TranscriptionApi,
    wav: bytes,
    language: str | None,
    terms: tuple[str, ...] = (),
) -> str:
    """Return the transcript of one WAV file, or raise TranscriptionError."""

    headers = {"Authorization": f"Bearer {api.api_key}"} if api.api_key else {}
    try:
        response = await client.post(
            api.url,
            data=form_fields(api, language, terms),
            files={"file": ("audio.wav", wav, "audio/wav")},
            headers=headers,
            timeout=api.timeout,
        )
    except httpx.TimeoutException as err:
        raise TranscriptionError("upstream timeout") from err
    except httpx.HTTPError as err:
        raise TranscriptionError(f"upstream unreachable: {err}") from err
    if response.status_code != 200:
        raise TranscriptionError(
            f"upstream answered {response.status_code}: "
            f"{_error_message(response)}"
        )
    try:
        text = response.json().get("text")
    except (ValueError, AttributeError) as err:
        raise TranscriptionError("upstream answer is not JSON") from err
    if not isinstance(text, str):
        raise TranscriptionError("upstream answer has no text")
    return text.strip()
