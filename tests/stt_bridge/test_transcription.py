"""Transcription client: form fields per API style, errors, WAV wrapping."""

import asyncio
import dataclasses
import io
import wave

import httpx
import pytest
from helpers import API

from app.transcription import (  # pylint: disable=wrong-import-order
    MAX_BIAS_WORDS,
    TranscriptionError,
    bias_words,
    form_fields,
    transcribe,
    wav_bytes,
)

TERMS = ("Stehlampe", "Großer Kellerraum")


def test_mistral_takes_single_words_as_context_bias():
    assert form_fields(API, "de", TERMS) == {
        "model": "voxtral-test",
        "language": "de",
        "context_bias": ["Stehlampe", "Großer", "Kellerraum"],
    }


def test_bias_words_skip_short_words_numbers_codes_and_repeats():
    terms = (
        "Wohnzimmerfenster links",
        "Flurspot 3",
        "0xd0cf5efffe159f77",
        "Lautsprecher - Alle",
        "AIVI Satellit Wake-Word-Aufnahme",
        "wohnzimmerfenster",
        "Bad,Küche",
    )

    assert bias_words(terms) == [
        "Wohnzimmerfenster",
        "Flurspot",
        "Lautsprecher",
        "Alle",
        "AIVI",
        "Satellit",
        "Wake-Word-Aufnahme",
    ]


def test_bias_words_stop_at_the_api_limit():
    terms = tuple(
        f"Raum{chr(0x61 + i // 26)}{chr(0x61 + i % 26)}" for i in range(150)
    )

    assert len(bias_words(terms)) == MAX_BIAS_WORDS


def test_openai_style_takes_the_terms_as_prompt():
    api = dataclasses.replace(API, bias_field="prompt")

    assert form_fields(api, None, TERMS) == {
        "model": "voxtral-test",
        "prompt": "Stehlampe, Großer Kellerraum",
    }


def test_no_terms_field_when_disabled_or_empty():
    api = dataclasses.replace(API, bias_field="none")

    assert form_fields(api, "de", TERMS) == {
        "model": "voxtral-test",
        "language": "de",
    }
    assert "context_bias" not in form_fields(API, "de", ())


def test_wav_bytes_keeps_the_format():
    with wave.open(io.BytesIO(wav_bytes(bytes(3200), 16000, 2, 1))) as wav:
        assert (wav.getframerate(), wav.getsampwidth(), wav.getnchannels()) == (
            16000,
            2,
            1,
        )
        assert wav.getnframes() == 1600


@pytest.mark.parametrize(
    "response, message",
    [
        (httpx.Response(500, text="boom"), "upstream answered 500: boom"),
        (
            httpx.Response(422, json={"detail": "bad language"}),
            "upstream answered 422: bad language",
        ),
        (httpx.Response(200, text="not json"), "upstream answer is not JSON"),
        (httpx.Response(200, json={"segments": []}), "upstream answer has no"),
    ],
)
def test_bad_answers_raise_a_loggable_error(response, message):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: response)
        ) as client:
            await transcribe(client, API, b"RIFF", "de")

    with pytest.raises(TranscriptionError, match=message):
        asyncio.run(scenario())


def test_timeout_is_reported_as_such():
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(slow)
        ) as client:
            await transcribe(client, API, b"RIFF", "de")

    with pytest.raises(TranscriptionError, match="upstream timeout"):
        asyncio.run(scenario())
