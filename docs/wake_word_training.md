# Wake word training

This runbook trains the custom wake word "Hey AIVI" for the satellites with
[microWakeWord](https://github.com/OHF-Voice/micro-wake-word). It is spoken
like English "Hey Ivy". Everything runs locally, and no recording leaves the
machine. The pipeline was built on a MacBook Air M5 (32 GB). It runs on any
macOS or Linux machine with enough disk space.

The model learns from three sources:

- **Synthetic speech.** A Piper generator mixes two of 600 English speakers
  per sample, and two German Piper voices add a German accent. Each sample
  speaks one of several pronunciations, written as IPA in
  `training/wake_word/config.yaml`. Whisper checks every sample: only
  samples it hears as "Hey Ivy" become wake word examples. Similar-sounding
  phrases such as "Hey, wie geht's?" or "Hi, I've" are generated as
  negatives.
- **Recordings of the household**, through the satellite and by phone. They
  are few but decisive, so every clip enters the training 15 times, each
  time augmented differently.
- **Negative datasets**: precomputed English speech, dinner party and
  non-speech spectrograms, German read speech, and everyday sound recorded
  at home.

## Overview

| Step | Command | Result |
|---|---|---|
| 1 | `make ww-setup` | Pinned tools, generators and datasets |
| 2 | `make ww-preview` | A few checked samples per pronunciation and voice |
| 3 | `make ww-samples` | 12,000 wake word and 6,000 confusable samples |
| 4 | `make ww-record`, phone | Household recordings |
| 5 | `make ww-import` | Recordings cut into clips |
| 6 | `make ww-features` | Augmented spectrograms |
| 7 | `make ww-train` | Quantized streaming model |
| 8 | `make ww-evaluate` | Recall, false accepts, cutoffs, manifest |

All data lives under `dev/` and is gitignored:

| Content | Location |
|---|---|
| Tools, generators, datasets | `dev/cache/wake_word/` |
| Recordings | `dev/datasets/wake_word/recordings/` |
| Previews, clips, features | `dev/datasets/wake_word/` |
| Models and reports | `dev/models/wake_word/hey_aivi/<run>/` |

The repository is public. Recordings of the household must never be
committed.

## Prerequisites

- `uv`. The targets run in their own environment, `training/wake_word/`,
  with Python 3.12, which uv installs on first use.
- About 40 GB of free disk space.
- For satellite recordings: the satellite's API key in
  `esphome/secrets.yaml` (see `docs/satellite_flashing.md`) and its IP
  address.

## 1. Set up

```bash
make ww-setup
```

This checks out microWakeWord and piper-sample-generator at pinned commits.
Neither project publishes a complete package, so both run from these
checkouts. The step also downloads the following, about 7 GB in total:

- the English Piper generator (checked by SHA-256) and two German Piper
  voices (Karlsson, Thorsten medium),
- room impulse responses (MIT),
- background audio (one AudioSet shard, FMA extra small),
- the precomputed negative spectrograms (`kahrendt/microwakeword`),
- German read speech: the dev and test splits of Multilingual LibriSpeech,
  60 speakers in 28.6 h. The other negative sets are English only.

Finished steps are skipped on the next run. The download took about five
minutes; unpacked, the data needs about 33 GB. The Whisper model for the
sample check (about 150 MB) is fetched on first use.

The background and negative datasets come with mixed licenses, some of them
non-commercial (cc-by-nc-4.0). Models trained with them are for personal use.

## 2. Check the pronunciation

```bash
make ww-preview
```

Each file `<phrase number>_<voice>.wav` in
`dev/datasets/wake_word/preview/positive/` has six samples of one
pronunciation by one voice, all of which passed the Whisper check.
`phrases_<voice>.txt` lists the IPA, what Whisper heard in the kept samples
and in the rejected ones. The `negative/` folder holds the confusable
phrases. Listen to them:

```bash
for f in dev/datasets/wake_word/preview/positive/*.wav; do echo "$f"; afplay "$f"; done
```

If a pronunciation is wrong, edit `samples.positive.phrases` in
`training/wake_word/config.yaml`. Print the espeak IPA of any text in German
and English:

```bash
uv run --project training/wake_word python training/wake_word/generate_samples.py --phonemize "Hey Aivi"
```

Which voices are used, and why, is documented in the config. Checked with
Whisper, short phrases are hard for many Piper voices: the German MLS
generator (236 speakers) invents other German words for "Hey Aivi"
("Wir waren traurig"), and the voices Kerstin, Ramona, Eva K and Pavoque
almost never say it. A glottal stop (ʔ) comes out as "t" ("Hey Tyvee"), and
espeak's German "Hey" (hˈaɪ) sounds like "Hi". None of these is used.

## 3. Generate synthetic samples

```bash
make ww-samples
```

The English speakers make up 80 % of the samples, Karlsson 16 % and
Thorsten medium 2 %.
Whisper (MLX on the Apple GPU, about 19 samples per second on the M5)
transcribes every sample. A wake word sample is kept only if the transcript
matches `wake_word_pattern` ("Hey Ivy", "Hey I.V." and similar), a
confusable sample only if it does not. Samples outside 0.35 to 1.45 s are
dropped too.

For the German voices the English model alone is too lenient: a second,
multilingual model transcribing German heard only half of Thorsten's
accepted samples as the wake word ("P.I.V.", "K.I.V."). Their wake word
samples must therefore pass both models (`double_check`). Karlsson keeps
two samples in three, Thorsten medium about one in seven. Thorsten high and
Thorsten emotional keep fewer than one in ten and synthesize slowly, so
they are not used.

After changing the settings of some voices, generate only those again; the
samples of the other voices stay:

```bash
uv run --project training/wake_word python training/wake_word/generate_samples.py --voices de_DE-karlsson-low
```

The file name tells the voice and the phrase:
`<voice>_p<phrase number>_<count>.wav`, the phrase number counting from 1 in
`samples.positive.phrases` (or `negative`). `samples.csv` in the same folder
has one row per generated sample, kept or not: voice, phrase, speakers,
synthesis settings, duration, Whisper transcript and the reason for a
rejection.

## 4. Record the household

Aim for at least 100 "Hey AIVI" per person who will use the assistant, plus
one to three hours of everyday sound without the wake word.

### Through the satellite

The satellite hears the wake word through the XVF3800, and the recordings go
through exactly that signal chain: channel 1 at 16 kHz, the same input the
model gets on the device.

ESPHome serves one voice assistant client at a time. Before recording,
disable the satellite's ESPHome entry in Home Assistant: Settings, Devices &
services, ESPHome, the satellite, the three-dot menu, Disable. Enable it
again afterwards. The recorder refuses to start while Home Assistant still
holds the voice assistant.

```bash
make ww-record HOST=<satellite-ip> SPEAKER=<name> TAKES=5
```

Each take works like this:

1. Say "Okay Nabu". The ring pulses white-blue twice and the recording
   starts.
2. For 60 seconds, say "Hey AIVI" every two to three seconds. Vary the
   distance (next to it, 1 m, 3 m, across the room), the direction, the
   volume (quiet, normal, loud), the speed and the tone (a question, tired,
   in passing). Record some takes with music or the TV playing softly.
3. Say nothing else during a take. Every utterance becomes a wake word clip.

`DURATION=<seconds>` changes the length of a take. `SPEAKER` becomes the
folder name, so use a short name without spaces.

Record everyday sound the same way:

```bash
make ww-record-negative HOST=<satellite-ip> DURATION=1800 TAKES=2
```

Leave the take running during normal life: conversations, TV, music, cooking,
phone calls. The wake word must not be said. This audio is used three ways:
as background noise in the augmentation, as negative training data, and, from
the last quarter of each recording, to measure false accepts per hour.

### By phone

Voice memos cover other rooms and people who are not near the satellite.
Record many "Hey AIVI" with short pauses in one memo, varied as above. Copy
the files to `dev/datasets/wake_word/recordings/positive/<name>/`. Any format
works; m4a is converted with `afconvert` (macOS) or `ffmpeg`. Long recordings
of everyday sound go to `dev/datasets/wake_word/recordings/negative/`.

## 5. Import the recordings

```bash
make ww-import
```

Voice activity detection finds the phrases in every positive recording.
Each phrase is trimmed to its loud part, so reverberation or music after it
does not stretch the clip; clips are 0.4 to 2.2 s long. The import drops
three kinds of segments:

- noise: more than 20 dB below the speaker's phrases in the same
  recording (clicks, distant voices, the TV);
- cut off: at the very start or end of a recording;
- too short or too long, e.g. two phrases without a pause.

Whisper then transcribes each clip in English and in German. It knows no
"Ei-wi" and writes things like "Hey, I.B.", "Hey, I mean" or "Here, Ivy",
so the check only asks for "Hey" followed by an "i", "ai" or "e" sound. A
clip that fails ("Hey." alone, "Okay.", nothing) goes to
`dev/datasets/wake_word/clips/real_positive/review/` instead of the training.
Listen to those:

```bash
for f in dev/datasets/wake_word/clips/real_positive/review/*.wav; do echo "$f"; afplay "$f"; done
```

Put the names of good ones, one per line, into
`dev/datasets/wake_word/recordings/accepted.txt`; names in `excluded.txt` in
the same folder are dropped even if Whisper accepts them. Then import again.
`clips.csv` next to the clips lists every segment with its position, level,
both transcripts and the verdict. The clip names depend on the
segmentation, so check the lists again after changing its settings.

Each accepted clip keeps its split for good: 70 % training, 10 %
validation, 20 % test. The test clips are never trained on; they measure
the recall.

Everyday recordings are cut into 10 s training chunks. The last quarter of
each stays one piece for the false accept measurement.

## 6. Build the features

```bash
make ww-features
```

The clip folders become labelled feature sets. The training draws its
batches from the training part; the validation part picks the best weights,
and the test part is only measured after training.

| Clips | Label | Training | Validation | Test |
|---|---|---|---|---|
| `clips/tts_positive/` | wake word | 80 %, each twice | 10 % | 10 % |
| `clips/tts_negative/` | not the wake word | 80 %, each twice | 10 % | 10 % |
| `clips/real_positive/{train,validation,test}/` | wake word | `train`, each 15 times | `validation` | `test` |
| `clips/real_negative/train/` | not the wake word | all | | |
| German speech, English speech, dinner party, non-speech | not the wake word | all | | |
| Dinner party evaluation set | not the wake word | | CHiME-6 | DiPCo |

The synthetic clips are split by a hash of the file name, so a clip stays in
its part when the set is rebuilt. The held-out everyday recordings in
`clips/real_negative/test/` are used only by the evaluation.

Each clip is placed at the end of a 3.2 s window, mixed with background
audio at -5 to 10 dB SNR, reverberated, equalized and pitch shifted at
random. It is then turned into the 40-channel spectrogram the satellite
computes every 10 ms. The work runs in parallel on all but two cores. The
features are stored once, as uint16; the training shifts them itself. For
the 30,000 synthetic samples this takes under two minutes and 1.3 GB. The
German speech keeps its length and gets the same augmentation (two minutes,
0.9 GB). Household recordings are used as recorded.

Do not rebuild features while a training runs: the step replaces the
feature directories the training reads.

## 7. Train

```bash
make ww-train
```

The run gets the current time as its name; `RUN=<name>` sets one. An
interrupted run resumes when started again with the same name. The training
uses okay_nabu's architecture and two phases (20,000 steps with SpecAugment,
then 10,000 fine-tuning steps). It validates every 500 steps and keeps the
weights with the best recall at a low false accept rate. The quantized
streaming model ends up in `dev/models/wake_word/hey_aivi/<run>/hey_aivi.tflite`,
next to a copy of the configuration it was trained with. microWakeWord
trains on the CPU on Apple Silicon, which is faster there than the GPU;
30,000 steps take about 25 minutes on the M5 and need 11 GB of memory, so
run one training at a time. Its own test results (synthetic test clips and
DiPCo) are in
`model/tflite_stream_state_internal_quant/tflite_streaming_roc.txt`.

For an experiment, copy `config.yaml`, change it and pass the copy:

```bash
uv run --project training/wake_word python training/wake_word/train.py --run <name> --config <copy>
```

## 8. Evaluate

```bash
make ww-evaluate
```

It evaluates the latest run, or `RUN=<name>`. The model runs as on the
satellite: a new probability every 30 ms, and the mean of the last five
probabilities must exceed the cutoff. The evaluation measures:

- recall on the held-out household clips per person, each heard after one
  second of held-out everyday sound, and on held-out synthetic clips;
- false accepts per hour on the held-out everyday recordings and on the DiPCo
  dinner party test set.

The report `evaluation_<time>.md` suggests one cutoff for each level of the
satellite's `Wake-Word-Empfindlichkeit` select, following okay_nabu: the
lowest cutoff with 0, at most 0.375 and at most 0.75 false accepts per hour
on DiPCo. Each must also keep household false accepts at or below 0.1 per
hour. The manifest `hey_aivi.json` next to the model takes the cutoff for
"Wenig empfindlich".

## 9. Put the model on the satellite

The training data includes material licensed for non-commercial use only, so
the model stays out of the public repository. Copy it into the gitignored
`esphome/models/` folder before building the satellite firmware:

```bash
mkdir -p esphome/models
cp dev/models/wake_word/hey_aivi/<run>/hey_aivi.{json,tflite} esphome/models/
make sat-flash SAT=<room> DEVICE=<ip-address>
```

The build fails without these files. The firmware runs "Hey AIVI" next to
"Okay Nabu". Set the new cutoffs for "Hey AIVI" in the
`Wake-Word-Empfindlichkeit` select in `esphome/packages/aivi-voice-input.yaml`.
The satellite's `Wake Word` select in Home Assistant chooses `Okay Nabu`,
`Hey AIVI` or `Beide`. Home Assistant's own `Aktivierungswort` select stays
unavailable: Home Assistant fills it only for satellites that play
announcements. Disable it on the device page to avoid confusion.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Recorder: Home Assistant still holds the voice assistant | Only one voice assistant client is allowed | Disable the ESPHome entry in Home Assistant |
| A take never starts | "Okay Nabu" not detected, or microphone muted | Check the ring; switch `Mikrofon stumm` off |
| Few or no clips from a recording | Too quiet, or no pauses between the phrases | Speak up, pause two seconds, check `clips.csv` |
| Many clips to review | Whisper hears no "Hey" at the start | Listen; add the good ones to `accepted.txt` |
| German preview samples are seconds long | Duration noise too high | Lower `noise_scale_ws` for `de_DE-mls-medium` |
| `Missing checkout` | Setup not run | `make ww-setup` |
