"""Generate the satellite's timer gong (esphome/sounds/timer_finished.wav).

Three bell-like notes rise through an E major chord (E5, G#5, B5), each with
a few partials and an exponential decay. The partials stay below 4.2 kHz,
inside the range the echo canceller's self test calibrates (125 Hz to
4 kHz), and the file is 16 kHz, 16 bit mono, so it stays small in the
firmware. The output is deterministic: running the script again gives the
same bytes.

  python scripts/generate_timer_sound.py [--out PATH]
"""

import argparse
import math
import pathlib
import struct
import wave

SAMPLE_RATE = 16000
DURATION_S = 1.6
# Peak level of the mixed gong, -6 dBFS.
PEAK = 0.5
# (frequency in Hz, onset in s) of each note.
NOTES = ((659.26, 0.0), (830.61, 0.16), (987.77, 0.32))
# (frequency ratio, relative amplitude, decay time constant in s) of each
# partial; the slightly stretched upper partial gives the bell colour.
PARTIALS = ((1.0, 1.0, 0.45), (2.0, 0.35, 0.25), (3.01, 0.12, 0.15))
ATTACK_S = 0.004
# The last 50 ms fade to silence, so the file ends without a click.
FADE_S = 0.05


def render() -> list[float]:
    """Return the gong as samples in -1..1, before normalisation."""

    total = int(SAMPLE_RATE * DURATION_S)
    samples = [0.0] * total
    for frequency, onset in NOTES:
        start = int(SAMPLE_RATE * onset)
        for n in range(total - start):
            t = n / SAMPLE_RATE
            attack = min(1.0, t / ATTACK_S)
            value = 0.0
            for ratio, amplitude, decay in PARTIALS:
                value += (
                    amplitude
                    * math.exp(-t / decay)
                    * math.sin(2.0 * math.pi * frequency * ratio * t)
                )
            samples[start + n] += attack * value
    fade = int(SAMPLE_RATE * FADE_S)
    for n in range(fade):
        samples[total - fade + n] *= 1.0 - (n + 1) / fade
    return samples


def write_wav(path: pathlib.Path, samples: list[float]) -> None:
    """Normalise to PEAK and write 16 bit mono PCM."""

    scale = PEAK / max(abs(s) for s in samples)
    frames = b"".join(
        struct.pack("<h", round(s * scale * 32767)) for s in samples
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(frames)


def main() -> None:
    """Parse the arguments and write the gong."""

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        type=pathlib.Path,
        default=pathlib.Path("esphome/sounds/timer_finished.wav"),
        help="output WAV file",
    )
    args = parser.parse_args()
    write_wav(args.out, render())
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
