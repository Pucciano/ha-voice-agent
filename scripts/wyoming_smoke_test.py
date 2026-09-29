"""Smoke test for Wyoming speech services (plan phase 4 acceptance).

  info  list the models or voices a service offers
  tts   send text to a text-to-speech service and save the reply as WAV
  stt   send a WAV file to a speech-to-text service and print the text

The stt timing is measured from the end of the audio, which is the delay a
user notices after speaking. Run it with the wyoming package, for example:

  uvx --from wyoming==1.10.2 python scripts/wyoming_smoke_test.py \\
      tts --host <jetson-ip> --port 10200 --text "Hallo" --out hallo.wav
"""

import argparse
import asyncio
import time
import wave

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.event import Event
from wyoming.info import Describe, Info
from wyoming.tts import Synthesize

CHUNK_SAMPLES = 1024


async def read_event(client: AsyncTcpClient) -> Event:
    event = await client.read_event()
    if event is None:
        raise SystemExit("The service closed the connection.")
    return event


async def info(args: argparse.Namespace) -> None:
    async with AsyncTcpClient(args.host, args.port) as client:
        await client.write_event(Describe().event())
        while not Info.is_type((event := await read_event(client)).type):
            pass
    described = Info.from_event(event)
    for program in described.asr:
        for model in program.models:
            print(f"stt {program.name}: {model.name} {model.languages}")
    for program in described.tts:
        for voice in program.voices:
            print(f"tts {program.name}: {voice.name} {voice.languages}")


async def tts(args: argparse.Namespace) -> None:
    start = time.monotonic()
    first_audio = None
    async with AsyncTcpClient(args.host, args.port) as client:
        await client.write_event(Synthesize(text=args.text).event())
        with wave.open(args.out, "wb") as wav:
            while True:
                event = await read_event(client)
                if AudioStart.is_type(event.type):
                    audio_start = AudioStart.from_event(event)
                    wav.setframerate(audio_start.rate)
                    wav.setsampwidth(audio_start.width)
                    wav.setnchannels(audio_start.channels)
                elif AudioChunk.is_type(event.type):
                    if first_audio is None:
                        first_audio = time.monotonic() - start
                    wav.writeframes(AudioChunk.from_event(event).audio)
                elif AudioStop.is_type(event.type):
                    break
    total = time.monotonic() - start
    print(f"{args.out}: first audio after {first_audio:.2f} s, done after {total:.2f} s")


async def stt(args: argparse.Namespace) -> None:
    with wave.open(args.wav, "rb") as wav:
        rate, width, channels = wav.getframerate(), wav.getsampwidth(), wav.getnchannels()
        audio = wav.readframes(wav.getnframes())
    duration = len(audio) / (rate * width * channels)
    step = CHUNK_SAMPLES * width * channels
    async with AsyncTcpClient(args.host, args.port) as client:
        await client.write_event(Transcribe(language=args.language).event())
        await client.write_event(AudioStart(rate, width, channels).event())
        for offset in range(0, len(audio), step):
            chunk = AudioChunk(rate, width, channels, audio[offset : offset + step])
            await client.write_event(chunk.event())
        await client.write_event(AudioStop().event())
        audio_end = time.monotonic()
        while not Transcript.is_type((event := await read_event(client)).type):
            pass
    text = Transcript.from_event(event).text
    delay = time.monotonic() - audio_end
    print(f"{args.wav} ({duration:.1f} s): {text!r} after {delay:.2f} s")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["info", "tts", "stt"])
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--text", help="tts: text to speak")
    parser.add_argument("--out", default="tts.wav", help="tts: output WAV file")
    parser.add_argument("--wav", help="stt: 16-bit WAV file to transcribe")
    parser.add_argument("--language", default="de", help="stt: language code")
    args = parser.parse_args()
    if args.command == "tts" and not args.text:
        parser.error("tts needs --text")
    if args.command == "stt" and not args.wav:
        parser.error("stt needs --wav")
    asyncio.run({"info": info, "tts": tts, "stt": stt}[args.command](args))


if __name__ == "__main__":
    main()
