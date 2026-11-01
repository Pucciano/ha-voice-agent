# Microphone levels of a satellite

How to read the level history of a satellite, what the numbers mean, and the
baseline measured in the living room. The levels answer three questions: is
the microphone still healthy, how loud is the room over the day, and was
there anything to hear when the wake word fired.

The first analysis of a full week, on 2026-11-01, is in
[Baseline, 2026-10-26 to 2026-11-01](#baseline-2026-10-26-to-2026-11-01).
Its open question — what actually triggers a false accept — is issue #30,
which the wake word clips
([wake word training, section 4](wake_word_training.md#4-record-the-household))
collect the evidence for.

## What is measured

The satellite runs two `sound_level` sensors, one per I2S channel of the
XVF3800 (`esphome/packages/aivi-diagnostics.yaml`):

| Entity | Meaning |
|---|---|
| `Pegel Kanal 0` / `Pegel Kanal 1` | RMS, the average level of the window |
| `Spitzenpegel Kanal 0` / `Spitzenpegel Kanal 1` | the loudest single sample of the window |

Both are dBFS of the processed XVF3800 output: 0 dBFS is digital full scale,
everything below it is negative. Keep in mind:

- The window is 2 s and the sensors publish continuously, so an hourly
  maximum really is the loudest moment of that hour, not a sample.
- This is not room sound pressure. The XVF3800 controls its own gain and
  lifts a quiet room, so the floor says more about the processing than about
  the room.
- Channel 1 is the one that matters: microWakeWord and the voice pipeline
  both read it (`esphome/packages/aivi-voice-input.yaml`). Channel 0 is the
  hotter signal and clips far more often, which is why the pipeline was
  moved off it.
- Home Assistant keeps hourly statistics long term and five minute
  statistics for about ten days. States older than the recorder's retention
  are gone, so a single detection can only be examined for about ten days.

## Read the history

```bash
make sat-levels DAYS=7 HA_ENV=~/.config/aivi/ha.env
```

`HA_ENV` is a file with `HA_URL` and `HA_TOKEN`; without it, set both in the
environment. `DAYS` is the window, `AROUND=<timestamp>` adds the five minute
levels around one moment, and the script prints four blocks:

1. **One line per day**: the noise floor (lowest hourly minimum), the average
   level, the loudest peak and how many hours each channel clipped.
2. **The level per hour of the day**, averaged over the window: the rhythm of
   the household.
3. **A summary**: how many buckets, gaps in the history, the noise floor and
   the clipped hours per channel.
4. **The five minute levels** before and after each `AROUND` moment.

## Check a single detection

Every voice request that the satellite recorded lies on the Jetson's capture
drive, named after the moment it started in UTC
([Jetson runbook, section 9](jetson_setup.md#9-request-capture)). List the
ones that carry no command — an empty transcript or a quiet recording — and
feed their timestamps back into the script:

```bash
ssh aivi@<jetson-ip> python3 - <<'PY'
import glob, json, os

for path in sorted(glob.glob("/mnt/aivi-capture/drive/aivi-stt/*/*.json")):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    audio = data["audio"]
    if data["capture_outcome"] == "transcript" and audio["rms_dbfs"] > -45:
        continue
    print(
        os.path.basename(os.path.dirname(path)),
        data["capture_outcome"],
        f'{audio["duration_s"]} s',
        f'{audio["rms_dbfs"]} dBFS',
        (data["pseudo_transcript"] or "")[:40],
    )
PY
```

Then look at the level around one of those moments:

```bash
make sat-levels DAYS=7 AROUND=20261101T172158Z HA_ENV=~/.config/aivi/ha.env
```

A capture whose RMS is below about -45 dBFS holds no speech worth
recognising; such a request is almost always a false accept of the wake word.
What the room level says about it:

- **RMS around -60 dBFS and no peak above -20 dBFS**: nothing audible
  happened. The wake word fired on the noise floor, on something very quiet,
  or on an artefact of the gain control.
- **RMS above -50 dBFS**: somebody was talking, or the TV was on. The model
  heard speech and mistook it; such a clip is the most valuable negative
  training data there is.
- **A peak at 0 dBFS**: the channel clipped. Check whether the satellite's
  own answer was playing at that moment (`media_player.…_audioausgang`, the
  jack route in [audio output](audio_output.md)) before blaming the room.

The clips in `recordings/wake/` hold the two seconds before each detection,
so they answer directly what the levels can only narrow down.

## Baseline, 2026-10-26 to 2026-11-01

167 hourly buckets of the living room satellite, no gaps, firmware with the
pipeline on channel 1.

```text
day        ch1 floor   ch1 mean   ch1 peak   ch0 mean   clip h
2026-10-26      -76.4      -67.0        0.0      -54.3  ch1  3, ch0 12
2026-10-27      -76.7      -71.2        0.0      -58.8  ch1  2, ch0  6
2026-10-28      -76.6      -68.1        0.0      -55.5  ch1  4, ch0  9
2026-10-29      -76.5      -68.1        0.0      -52.1  ch1  2, ch0 15
2026-10-30      -76.5      -68.0        0.0      -52.3  ch1  3, ch0 16
2026-10-31      -76.7      -56.3        0.0      -46.7  ch1 12, ch0 15
2026-11-01      -76.7      -68.0        0.0      -58.0  ch1  4, ch0 12
```

What to take from it:

- **The noise floor is constant**: the lowest minute of every single night
  lands between -76.0 and -76.7 dBFS. A floor that climbs over days would
  point at the microphone, its mounting or the XVF3800; nothing of the kind
  is happening here. Use -76.5 dBFS as the reference.
- **The rhythm is plausible**: quietest between 01:00 and 03:00 (-76 dBFS),
  rising from 06:00, loudest at 21:00 (-53 dBFS). A night that is as loud as
  an evening means something is running that should not be.
- **Channel 1 clips in 30 of 167 hours**, channel 0 in 85. Clipping on
  channel 1 costs wake word detections and garbles the transcript, and it
  happens in the loud evening hours. Keeping the TV quieter is the only
  remedy at hand; the XVF3800 gain is not under our control.
- **2026-10-31 is an outlier, not a defect**: -56.3 dBFS daily average and
  -25 dBFS from 21:00 to 00:00, about 40 dB above the usual level of those
  hours. The TV plug was on from 20:41 to 00:12. Do not use such a day as a
  reference for measurements.

### False accepts are not loud events

The obvious guess — a bang, a door or clattering dishes sets the wake word
off — does not survive the data. Of the 13 quiet hours (channel 1 average at
or below -55 dBFS) that contained a full scale peak, exactly one held a
non-command activation; of the 135 quiet hours without such a peak, eleven
did.

In the five minute window around the 14 non-command activations of that week,
ten sat at -60 to -71 dBFS RMS with the loudest peak of the whole window
between -20 and -46 dBFS. Examples:

| Moment (UTC) | ch1 RMS | loudest peak in the window |
|---|---:|---:|
| 2026-10-28 17:02 | -70.5 | -30.2 |
| 2026-10-29 07:40 | -65.0 | -31.5 |
| 2026-10-31 07:32 | -68.4 | -41.3 |
| 2026-11-01 17:21 | -62.6 | -25.2 |

Nothing in those rooms was loud enough to hear. The remaining four
activations did fall into loud windows, so there is more than one cause. The
working hypothesis is that the model fires on very low level input, plausibly
on noise that the XVF3800 gain control lifts in a silent room — but the
levels cannot prove it, because they only say how loud it was, never what it
was. That is what the wake word clips are for: after a week of them, compare
the detections with the recordings before touching a probability cutoff.
