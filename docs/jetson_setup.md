# Jetson Nano setup

This runbook turns a classic Jetson Nano Developer Kit (4 GB, microSD) into
the speech server of plan phase 4. It runs two Wyoming services for Home
Assistant, plus a relay that can store requests for training and a recorder
for wake word training takes. It is also the gateway of the LLM assistant
([LLM assistant](llm_assistant.md)): a proxy to the language model and a
bridge to a cloud speech recogniser:

| Service | Purpose | Port |
|---|---|---:|
| stt-capture | relays speech to text to Speech-to-Phrase; stores requests on a USB drive while a satellite allows it ([section 9](#9-request-capture)) | 10300 |
| stt-capture-cloud | the same relay in front of stt-bridge, for the LLM pipeline ([section 12](#12-cloud-speech-to-text)) | 10310 |
| stt-bridge | speech to text through a cloud transcription API ([section 12](#12-cloud-speech-to-text)) | internal |
| llm-proxy | OpenAI-compatible endpoint for Home Assistant, forwards to the cloud model ([section 11](#11-llm-proxy)) | 18001 |
| wake-word-recorder | stores the wake word training takes that satellites stream when started in Home Assistant ([section 10](#10-wake-word-recording)) | 10800 |
| Speech-to-Phrase | speech to text | internal |
| Piper (`de_DE-thorsten-medium`) | text to speech | 10200 |
| Whisper (`base-int8`, on demand) | speech to text, comparison only | 10301 |
| ble-proxy (own compose project) | Bluetooth for Home Assistant through a USB dongle ([section 13](#13-bluetooth-proxy)) | 6053 |

The steps were verified on `aivi-jetson` on 2026-09-27. In the commands,
replace `<jetson-ip>` with the Nano's `eth0` address (`ip -4 addr show eth0`)
and `<home-assistant-ip>` with the address of Home Assistant. 192.168.55.1 is
the Nano's fixed address on the USB link.

## Why this stack

The classic Nano ends at JetPack 4.6.6 (L4T R32.7.6, Ubuntu 18.04, CUDA 10.2).
faster-whisper runs on CTranslate2, which needs CUDA 11 or newer, so its GPU
path is not available on this board. On the CPU, which is comparable to a
Raspberry Pi 4, Whisper needs several seconds per command.

Speech-to-Phrase is Home Assistant's closed-vocabulary recogniser. It trains on
the names of the entities, areas and floors exposed to Assist. It supports
German and answered test commands in about 1.3 s. It only recognises commands
that Assist knows: no shopping lists, timer names or free text.

Free speech for the LLM assistant needs an open recogniser. Whisper on the
Nano's CPU was measured on 2026-10-11 with 17 captured real commands
(wyoming-whisper 3.8.1, time from the end of speech to the transcript):

| Model | Typical | Outliers | Text |
|---|---:|---:|---|
| `base-int8` | 5.1–6.4 s | 22–57 s | frequent nonsense ("Blischpethystas") |
| `small-int8` | 13–14 s | 41–50 s | mostly right |

That is too slow for a voice assistant, so the LLM pipeline transcribes in the
cloud ([section 12](#12-cloud-speech-to-text)) and Speech-to-Phrase stays the
offline path.

## 1. Flash the microSD card (macOS)

Download the official image (6.56 GB) and write it to the card:

```bash
curl -fLO https://developer.download.nvidia.com/embedded/L4T/r32_Release_v7.1/JP_4.6.1_b110_SD_Card/Jeston_Nano/jetson-nano-jp461-sd-card-image.zip
```

```bash
sudo scripts/flash_jetson_sd.sh jetson-nano-jp461-sd-card-image.zip disk6
```

Find the disk name with `diskutil list`. The script accepts only an external,
removable disk and asks you to type its name. It checks the image, writes it,
reads the card back and compares the SHA-256, then ejects the card. A 32 GB
card is enough; use a high-endurance card.

## 2. First boot without a display

1. Insert the card, connect Ethernet to the IoT VLAN, set jumper J48 and
   power the Nano from a 5 V / 4 A barrel jack supply.
2. Connect the micro-USB port to the Mac. The Nano appears as
   `/dev/cu.usbmodem…` and as a network link with the Nano at 192.168.55.1.
3. Open the setup console and complete the wizard:

   ```bash
   screen /dev/cu.usbmodem* 115200
   ```

   Use English, time zone Europe/Berlin, user `aivi`, hostname
   `aivi-jetson`, `eth0` with DHCP and power mode MAXN. Press Ctrl-A then L
   if the screen looks garbled.
4. Install your SSH key (you type the Nano password once):

   ```bash
   ssh-copy-id -o StrictHostKeyChecking=accept-new aivi@192.168.55.1
   ```

## 3. Provision

`scripts/jetson_provision.sh` does the following:

- turns off automatic updates
- updates to L4T R32.7.6 and installs curl
- removes the large desktop applications (about 845 MB)
- boots to the console instead of the desktop
- installs Docker Compose v2.29.7 and adds `aivi` to the `docker` group
- creates `/srv/wyoming` and `/opt/aivi/compose`, and copies the compose file
  and its `custom_sentences/` there
- builds the `stt-capture` and `wake-word-recorder` images, installs
  `aivi-capture` and creates the empty mount point of the capture drive
  ([sections 9](#9-request-capture) and [10](#10-wake-word-recording))
- installs `aivi-logmode` and sets it to dev

NVIDIA's list of desktop-only packages is not used on purpose: on this image it
would also remove network-manager, the CUDA compiler and TensorRT. The script
simulates the removal first and stops if a protected package would go.

Run it detached, so that a dropped connection cannot interrupt the update. It
took about 15 minutes. Do not cut the power while it updates the bootloader.

```bash
ssh aivi@192.168.55.1 mkdir -p /tmp/aivi-provision
```

```bash
scp -r scripts/jetson_provision.sh scripts/jetson_logmode.sh scripts/jetson_capture.sh compose/jetson/docker-compose.yml compose/jetson/custom_sentences services/stt_capture services/wake_word_recorder aivi@192.168.55.1:/tmp/aivi-provision/
```

```bash
ssh -t aivi@192.168.55.1 "sudo -v && sudo sh -c 'setsid nohup bash /tmp/aivi-provision/jetson_provision.sh > /run/aivi-provision.log 2>&1 < /dev/null &' && sleep 2 && tail -f /run/aivi-provision.log"
```

When the log ends with `Done`, reboot and check the release:

```bash
ssh -t aivi@192.168.55.1 sudo reboot
```

```bash
ssh aivi@192.168.55.1 head -1 /etc/nv_tegra_release
```

Expected: `R32 (release), REVISION: 7.6`, kernel `4.9.337-tegra`, about
225 MB of memory in use and no failed systemd units.

## 4. Home Assistant token

Speech-to-Phrase reads the exposed entities and areas over the Home Assistant
websocket API. Create a long-lived access token in Home Assistant (profile →
Security) and store it on the Jetson. Replace `<home-assistant-ip>` in the
command. It asks for the token without echoing it, then for the sudo password:

```bash
ssh -t aivi@192.168.55.1 'read -rsp "Home Assistant token: " t && echo && printf "HASS_WEBSOCKET_URI=ws://<home-assistant-ip>:8123/api/websocket\nHASS_TOKEN=%s\n" "$t" | sudo sh -c "umask 027 && cat > /opt/aivi/compose/.env && chgrp docker /opt/aivi/compose/.env" && unset t'
```

The file is `root:docker` with mode 0640 and is never committed.

## 5. Start the services

```bash
ssh aivi@192.168.55.1 'cd /opt/aivi/compose && docker compose up -d'
```

The first start pulls about 1.5 GB of images, downloads the German model
(`de_DE-zamia`) and the Piper voice, and trains Speech-to-Phrase. It retrains
on every start and not otherwise, so restart it after renaming entities,
areas or floors (see [section 8](#8-names-and-sentences)):

```bash
ssh aivi@192.168.55.1 'cd /opt/aivi/compose && docker compose restart speech-to-phrase'
```

To compare with Whisper on port 10301:

```bash
ssh aivi@192.168.55.1 'cd /opt/aivi/compose && docker compose --profile compare up -d whisper'
```

## 6. Verify

`scripts/wyoming_smoke_test.py` talks to the services directly, without Home
Assistant. Test recordings can come from a German macOS voice:

```bash
say -v Anna -o test.aiff "Wie spät ist es?" && afconvert -f WAVE -d LEI16@16000 -c 1 test.aiff test.wav
```

```bash
uvx --from wyoming==1.10.2 python scripts/wyoming_smoke_test.py stt --host <jetson-ip> --port 10300 --wav test.wav
```

```bash
uvx --from wyoming==1.10.2 python scripts/wyoming_smoke_test.py tts --host <jetson-ip> --port 10200 --text "Das Licht im Wohnzimmer ist jetzt eingeschaltet." --out answer.wav
```

Reference results from the living room setup:

| Input | Result | Time |
|---|---|---:|
| "Wie spät ist es?" | `wie spät ist es` | 1.3 s |
| "Schalte das Licht im Wohnzimmer ein." | `schalte das licht im Wohnzimmer ein` | 1.3 s |
| "Schalte die Lichter im Wohnzimmer an." | `schalte die lichter im Wohnzimmer an` | 0.8 s |
| "Schalte alle Lichter in der Wohnung aus." | `schalte alle lichter in der Wohnung aus` | 1.2 s |
| "Schalte die Stehlampe aus." | `schalte die Stehlampe aus` | 1.1 s |
| "Stelle einen Timer auf fünf Minuten." | `stelle einen Timer auf 5 Minuten` | 0.8 s |
| Piper's own answer sentence | empty, because it is not a command | 2.3 s |
| Piper, 2.75 s answer | audio ready | 1.0 s |

The first Piper request after a start takes about 6 s while the voice loads.

## 7. Connect Home Assistant

Add the **Wyoming Protocol** integration twice, with host `<jetson-ip>` and
ports `10300` and `10200`. Give the Jetson a DHCP reservation.

## 8. Names and sentences

Speech-to-Phrase only recognises its sentence templates, filled with the names
of the entities, areas and floors exposed to Assist. What worked in the living
room setup:

- **German display names.** Speech-to-Phrase always trains an entity's display
  name and adds its aliases to it, so an alias does not remove an English or
  technical name. Rename the device ("name by user") or the entity instead; the
  entity ID stays. When Home Assistant offers to rename the entity IDs too,
  decline.
- **No entity named like an area or floor.** Every entity name also becomes
  "schalte <name> ein". A window contact named "Wohnzimmer" turned "Schalte das
  Licht im Wohnzimmer ein" into `schalte Wohnzimmer ein`, and Home Assistant
  answered that it found several.
- **Light groups instead of their bulbs.** Expose the group and not its
  members, so an area command sends one command per lamp.
- **Aliases spelled as spoken** for loanwords and foreign names, for example
  `Haibord` for `Highboard`.
- **Nothing exposed that must never switch by mistake**, such as the plug of
  the server rack.

`train/de_DE-zamia/missing_words_dictionary.txt` under
`/srv/wyoming/speech-to-phrase` lists every word whose pronunciation was
guessed. English words there point to names that still need a German one.

### Custom sentences

The built-in German sentences of speech-to-phrase 1.4.3 only know "schalte die
Lichter in [dem] <area> ein". "Schalte das Licht im Wohnzimmer ein" is
missing, and the rule `[schalte ][ das]licht[er]` produces the joined word
`daslicht`, which Home Assistant does not understand.
`compose/jetson/custom_sentences/de/lights.yaml` adds the everyday forms for
areas and floors. It is mounted read-only and read at every training.

To update the sentences on a running Jetson, copy them, install them with sudo
and recreate the container:

```bash
ssh aivi@<jetson-ip> 'rm -rf /tmp/aivi-update && mkdir /tmp/aivi-update'
```

```bash
scp -r compose/jetson/custom_sentences compose/jetson/docker-compose.yml aivi@<jetson-ip>:/tmp/aivi-update/
```

```bash
ssh -t aivi@<jetson-ip> 'sudo sh -c "rm -rf /opt/aivi/compose/custom_sentences && cp -R /tmp/aivi-update/custom_sentences /opt/aivi/compose/ && chown -R root:root /opt/aivi/compose/custom_sentences && chmod -R u=rwX,go=rX /opt/aivi/compose/custom_sentences && install -m 0644 /tmp/aivi-update/docker-compose.yml /opt/aivi/compose/docker-compose.yml"'
```

```bash
ssh aivi@<jetson-ip> 'cd /opt/aivi/compose && docker compose up -d --force-recreate speech-to-phrase'
```

Training takes about 11 s. Requests in that time return an empty text.

## 9. Request capture

The `stt-capture` relay can store real voice requests as raw data for
improving speech recognition: the audio that Speech-to-Phrase received and
the text it recognised. Every satellite has a switch `Anfragen aufzeichnen` in
Home Assistant. It is off after every restart of the satellite.

### How it works

Home Assistant connects to port 10300 as before. The relay passes every
Wyoming frame byte for byte to Speech-to-Phrase and back, and only watches a
copy. A request carries no satellite id, so the relay asks Home Assistant
which satellite is listening. It stores a request only if all of these hold:

- exactly one of the satellites in the compose file (`--satellite`) is
  `listening` at `audio-start`, or else at `audio-stop` (one more attempt)
- that satellite's switch is `on`
- both answers arrived before the request ended, within 2 s in total
- the audio kept one format and stayed under 60 s
- the capture drive is mounted, carries its marker file and has at least
  1 GiB free

Anything else discards the audio, which never touches the SD card. Speech
recognition does not wait for any of this. Empty results, errors and broken
connections are stored too, with their `capture_outcome`; they are the most
useful samples for review.

Each request becomes one directory on the drive, in the layout of
`training/stt/prepare_dataset.py`:

```text
/mnt/aivi-capture/drive/aivi-stt/20261003T194312Z-living_room-1a2b3c/
  audio.wav        16 kHz, 16 bit, mono, as Home Assistant sent it
  transcript.txt   the recogniser's text (a pseudo label)
  metadata.json    satellite, outcome, format, levels, SHA-256, model, latency
```

`label_source` names the recogniser behind the relay: `speech-to-phrase` on
port 10300, `stt-bridge` on port 10310. Speech-to-Phrase picks from known
sentences, and the cloud model can mishear too, so neither text is ground
truth. Samples start as `verified: false`, and `prepare_dataset.py` leaves
them out until they are reviewed.

While the switch is on, everything said after the wake word is stored
unencrypted on the drive. Tell the household, and switch it off when the
recording period is over.

### Prepare the drive

The drive is formatted as ext4 on the Jetson and mounted by its UUID, so the
Mac cannot read it directly; fetch the data over the network instead. Find
the drive with `lsblk -o NAME,SIZE,TRAN,MODEL`, then format it. This erases
it; you type the device name to confirm:

```bash
ssh -t aivi@<jetson-ip> sudo aivi-capture format /dev/sda
```

```bash
ssh -t aivi@<jetson-ip> sudo aivi-capture setup
```

`setup` refuses to run unless the root mount propagates mounts (`shared`).
It adds an fstab entry, a udev rule that mounts the drive when it is plugged
in, the marker file `.aivi-capture-volume` and the directories `aivi-stt/`
and `aivi-wake-word/`.
Without the drive, `/mnt/aivi-capture/drive` is an empty, immutable
directory on the SD card. Check the state at any time:

```bash
ssh aivi@<jetson-ip> aivi-capture status
```

Unmount before unplugging the drive, and mount it again after plugging it
back in if udev has not done so already. `eject` refuses while a wake word
take runs; `mount` also creates a missing data directory:

```bash
ssh -t aivi@<jetson-ip> sudo aivi-capture eject
```

```bash
ssh -t aivi@<jetson-ip> sudo aivi-capture mount
```

### Install or update the relay

Provisioning builds the image. To add the relay to a running Jetson, or to
update it, copy the files and build the image:

```bash
ssh aivi@<jetson-ip> 'rm -rf /tmp/aivi-update && mkdir /tmp/aivi-update'
```

```bash
scp -r compose/jetson/docker-compose.yml scripts/jetson_capture.sh services/stt_capture aivi@<jetson-ip>:/tmp/aivi-update/
```

```bash
ssh aivi@<jetson-ip> 'docker build -t aivi/stt-capture:0.1.1 /tmp/aivi-update/stt_capture'
```

The first time, try the relay on a spare port while Speech-to-Phrase still
serves port 10300 (`aivi-capture prepare` creates the mount point if
provisioning has not):

```bash
ssh -t aivi@<jetson-ip> 'sudo install -m 0755 /tmp/aivi-update/jetson_capture.sh /usr/local/sbin/aivi-capture && sudo aivi-capture prepare'
```

```bash
ssh aivi@<jetson-ip> 'docker run --rm -d --name stt-capture-trial --network aivi-jetson_default -p 10390:10300 -v /mnt/aivi-capture:/capture:rslave aivi/stt-capture:0.1.1 --upstream tcp://speech-to-phrase:10300'
```

```bash
uvx --from wyoming==1.10.2 python scripts/wyoming_smoke_test.py stt --host <jetson-ip> --port 10390 --wav test.wav
```

```bash
ssh aivi@<jetson-ip> docker stop stt-capture-trial
```

Then install the compose file, keeping the old one, and start the stack:

```bash
ssh -t aivi@<jetson-ip> 'sudo sh -c "install -m 0755 /tmp/aivi-update/jetson_capture.sh /usr/local/sbin/aivi-capture && cp -p /opt/aivi/compose/docker-compose.yml /opt/aivi/compose/docker-compose.yml.bak && install -m 0644 /tmp/aivi-update/docker-compose.yml /opt/aivi/compose/docker-compose.yml"'
```

```bash
ssh aivi@<jetson-ip> 'cd /opt/aivi/compose && docker compose up -d'
```

Speech-to-Phrase is recreated and trains again. Its health check only shows
that it answers Wyoming requests: during training it already answers, with
empty transcripts. Readiness is a real request through the relay, which must
return the recognised text ([section 6](#6-verify)):

```bash
uvx --from wyoming==1.10.2 python scripts/wyoming_smoke_test.py stt --host <jetson-ip> --port 10300 --wav test.wav
```

To roll back, restore the old compose file:

```bash
ssh -t aivi@<jetson-ip> 'sudo cp -p /opt/aivi/compose/docker-compose.yml.bak /opt/aivi/compose/docker-compose.yml'
```

```bash
ssh aivi@<jetson-ip> 'cd /opt/aivi/compose && docker compose up -d --remove-orphans'
```

### Satellites

Each satellite that may be captured has a `--satellite` line in the compose
file: its `satellite_id`, its `assist_satellite` entity and its
`Anfragen aufzeichnen` switch, as Home Assistant names them. Take both
entity IDs from the satellite's device page, install the compose file and run
`docker compose up -d`. A satellite without such a line is never captured.

### Fetch and review the data

Fetch new samples to the Mac. `--ignore-existing` keeps samples you have
already reviewed there:

```bash
rsync -av --ignore-existing --exclude '.tmp-*' aivi@<jetson-ip>:/mnt/aivi-capture/drive/aivi-stt/ dev/datasets/stt/
```

To review a sample, listen to `audio.wav` and write what was actually said
into `transcript.txt`. Then set `"verified": true`, `"label_source":
"manual"` and `"verified_at"` (ISO time) in `metadata.json`. Keep
`pseudo_transcript`: the difference between it and the reviewed text is what
the recogniser got wrong. Leave samples without intelligible speech
unverified. Only verified samples reach the manifest:

```bash
python training/stt/prepare_dataset.py --input-dir dev/datasets/stt --output dev/datasets/meta/stt_manifest.jsonl
```

`--include-unverified` adds the rest, for review or evaluation only.

## 10. Wake word recording

The `wake-word-recorder` stores training takes for the custom wake word
([wake word training, section 4](wake_word_training.md#4-record-the-household)).
A take is started on the satellite's device page in Home Assistant. The
satellite then streams the signal that microWakeWord hears straight to the
recorder on port 10800. Home Assistant stays connected, and speech
recognition on port 10300 is not involved.

### How it works

The satellite opens a TCP connection and sends Wyoming events: `take-start`
with its id, the kind of take, the speaker, the length and a shared token,
then `audio-chunk`, `detection` for wake word detections during the take,
and `audio-stop`. The recorder answers `take-accepted` or an error code, and
at the end `take-saved`; the satellite shows the result in its entity
`Wake-Word-Aufnahme Status`. A take is accepted only if all of these hold:

- the token matches `WAKE_WORD_RECORDER_TOKEN`
- the satellite id has a `--satellite` line in the compose file
- a "Hey AIVI" take names its speaker with lower case letters and digits,
  single `_` or `-` in between
- the take is at most two hours long
- the capture drive is mounted, carries its marker file and the
  `aivi-wake-word/` directory, and has at least 1 GiB free; the space is
  checked again every minute of a take

The audio is written while the take runs, in the layout of
`dev/datasets/wake_word/recordings/` on the Mac:

```text
/mnt/aivi-capture/drive/aivi-wake-word/recordings/
  positive/<speaker>/sat_living_room_20261004-143012.wav   16 kHz, 16 bit, mono
  positive/<speaker>/sat_living_room_20261004-143012.json  speaker, length, end, detections
  negative/sat_living_room_20261004-150000.wav
  negative/sat_living_room_20261004-150000.json
```

Both files end in `.part` while the take runs. A take that ends without
`audio-stop` keeps its audio, e.g. after a lost connection, a satellite
restart or 10 s without data. Takes under 2 s are dropped. Leftovers of an
interrupted recorder are finished when it starts again. The log names takes
and their outcome, never a speaker.

Takes are stored unencrypted. An everyday take records whole conversations;
tell the household.

### Token and address

The satellites and the recorder share a token. Generate one:

```bash
openssl rand -hex 24
```

Put it into `esphome/secrets.yaml` as `wake_word_recorder_token`, with the
Jetson's address as `wake_word_recorder_host`, and add it to the compose
`.env` on the Jetson. The command asks for the token without echoing it,
then for the sudo password:

```bash
ssh -t aivi@<jetson-ip> 'read -rsp "Wake word recorder token: " t && echo && printf "WAKE_WORD_RECORDER_TOKEN=%s\n" "$t" | sudo sh -c "cat >> /opt/aivi/compose/.env" && unset t'
```

Without the token the recorder runs but refuses every take; the other
services do not depend on it. The satellites connect to the Jetson's
address directly, so give the Jetson a DHCP reservation.

### Install or update the recorder

Provisioning builds the image. On a running Jetson, copy the files and build
it; do not run the provisioning script again for this, because it switches
the log mode to dev:

```bash
ssh aivi@<jetson-ip> 'rm -rf /tmp/aivi-update && mkdir /tmp/aivi-update'
```

```bash
scp -r compose/jetson/docker-compose.yml scripts/jetson_capture.sh services/wake_word_recorder aivi@<jetson-ip>:/tmp/aivi-update/
```

```bash
ssh aivi@<jetson-ip> 'docker build -t aivi/wake-word-recorder:0.1.0 /tmp/aivi-update/wake_word_recorder'
```

Update `aivi-capture` and create the `aivi-wake-word/` directory on the
drive:

```bash
ssh -t aivi@<jetson-ip> 'sudo install -m 0755 /tmp/aivi-update/jetson_capture.sh /usr/local/sbin/aivi-capture && sudo aivi-capture mount'
```

The first time, try the recorder on a spare port. The token comes from the
`.env` file without showing up in a command line:

```bash
ssh aivi@<jetson-ip> 'export WAKE_WORD_RECORDER_TOKEN="$(sed -n "s/^WAKE_WORD_RECORDER_TOKEN=//p" /opt/aivi/compose/.env)" && docker run --rm -d --name wake-word-recorder-trial -p 10810:10800 -e WAKE_WORD_RECORDER_TOKEN -e TZ=Europe/Berlin -v /mnt/aivi-capture:/capture:rslave aivi/wake-word-recorder:0.1.0 --satellite living_room'
```

`scripts/wyoming_smoke_test.py take` sends a WAV file (16 kHz, 16 bit, mono,
at least 2 s) as a satellite would. It takes the token from
`WAKE_WORD_RECORDER_TOKEN`, here read from `esphome/secrets.yaml`, or asks
for it:

```bash
say -v Anna -o take.aiff "Hey Ei-wi. Hey Ei-wi. Hey Ei-wi." && afconvert -f WAVE -d LEI16@16000 -c 1 take.aiff take.wav
```

```bash
WAKE_WORD_RECORDER_TOKEN="$(sed -n 's/^wake_word_recorder_token: "\(.*\)"$/\1/p' esphome/secrets.yaml)" uvx --from wyoming==1.10.2 python scripts/wyoming_smoke_test.py take --host <jetson-ip> --port 10810 --wav take.wav --speaker trial --detection 1.0
```

It prints `take-saved` with the take's name.

```bash
ssh aivi@<jetson-ip> docker stop wake-word-recorder-trial
```

The trial take lands in `aivi-wake-word/recordings/positive/trial/`; delete
it afterwards. Then install the compose file, keeping the old one, and start
the recorder:

```bash
ssh -t aivi@<jetson-ip> 'sudo sh -c "cp -p /opt/aivi/compose/docker-compose.yml /opt/aivi/compose/docker-compose.yml.bak && install -m 0644 /tmp/aivi-update/docker-compose.yml /opt/aivi/compose/docker-compose.yml"'
```

```bash
ssh aivi@<jetson-ip> 'cd /opt/aivi/compose && docker compose up -d wake-word-recorder'
```

Naming the service leaves the running speech services alone. To roll back,
restore the old compose file and run `docker compose up -d --remove-orphans`
as in [section 9](#install-or-update-the-relay).

### Satellites

Each satellite that may record has a `--satellite <satellite_id>` line in
the compose file, its `satellite_id` substitution. Its firmware needs
`wake_word_recorder_host` and `wake_word_recorder_token` in
`esphome/secrets.yaml` ([satellite flashing](satellite_flashing.md)).

### Fetch the takes

On the Mac, `make ww-pull` copies new takes into
`dev/datasets/wake_word/recordings/` and skips takes that are still running;
`MOVE=1` deletes the copied ones from the drive:

```bash
make ww-pull JETSON=<jetson-ip>
```

## 11. LLM proxy

`llm-proxy` (`services/llm_proxy`) is the OpenAI-compatible endpoint that Home
Assistant's **llama.cpp** integration talks to, on port 18001. It forwards
every request to the cloud model and keeps the provider's key on the Jetson
([LLM assistant](llm_assistant.md)).

### How it works

- Home Assistant sees one model, `aivi`. The proxy replaces it with
  `LLM_UPSTREAM_MODEL`, so another model or provider, or later a local
  server, needs no change in Home Assistant.
- Requests need the proxy's own key (`LLM_PROXY_API_KEY`) as a bearer token.
  Without that entry in `.env` the proxy refuses everything.
- `user` is dropped, because the Mistral API refuses it, and
  `LLM_UPSTREAM_EXTRA_BODY` is merged in (e.g. provider options).
- At most `LLM_PROXY_DAILY_REQUEST_LIMIT` requests per UTC day (default 500);
  then it answers 429 until midnight UTC. A command with tools takes two to
  three requests.
- Streaming is refused. Home Assistant notices that when the integration is
  added and then asks without streaming.
- Malformed tool arguments get one repair request.
- A tool call under a raw intent name such as `HassStartTimer` gets its
  prefix back (`intent__HassStartTimer`) when exactly one of the request's
  tools carries that name, in the history before the request goes out and in
  the model's answer. Home Assistant logs a command that its built-in agent
  handled locally under the raw name; the model copied it in the next turn
  of the conversation, and Home Assistant answered "Tool not found". The
  capture keeps the request as Home Assistant sent it and lists each rename
  in `fixed_tool_names`.
- An unreachable provider fails at once: at most 10 s to connect, and the
  answer tells the client not to retry. Its message,
  `LLM_PROXY_UNAVAILABLE_MESSAGE` ("Das Sprachmodell ist gerade nicht
  erreichbar."), is what Home Assistant speaks; the cause is in the log.
- Every exchange is appended to
  `/mnt/aivi-capture/drive/aivi-llm/llm_capture_<UTC day>.jsonl`, but only
  while the drive is mounted with its marker and the directory exists. A
  record holds Home Assistant's whole prompt with the exposed devices and
  their states, the tool calls and the answer, the latency and the token
  counts; never a key. `LLM_PROXY_ENABLE_CAPTURE=false` in `.env` turns it
  off.

### Keys

The cloud key (`MISTRAL_API_KEY`) serves the proxy and the speech-to-text
bridge. Create it in the provider's console with a monthly spending limit.
The command asks for it without echoing it, creates the proxy key, stores
both in `.env` and shows the proxy key once, for Home Assistant:

```bash
ssh -t aivi@<jetson-ip> 'read -rsp "Mistral API key: " k && echo && p=$(openssl rand -hex 24) && printf "MISTRAL_API_KEY=%s\nLLM_PROXY_API_KEY=%s\n" "$k" "$p" | sudo sh -c "cat >> /opt/aivi/compose/.env" && echo "Proxy key for Home Assistant (llama.cpp): $p" && unset k p'
```

To replace a key, edit `.env` with `sudo nano /opt/aivi/compose/.env` and run
`docker compose up -d` in `/opt/aivi/compose`.

### Install or update

Copy the files and build the images. The proxy and the bridge install their
Python packages while building, so the Jetson needs internet access then:

```bash
ssh aivi@<jetson-ip> 'rm -rf /tmp/aivi-update && mkdir /tmp/aivi-update'
```

```bash
scp -r compose/jetson/docker-compose.yml scripts/jetson_capture.sh services/stt_capture services/stt_bridge services/llm_proxy aivi@<jetson-ip>:/tmp/aivi-update/
```

```bash
ssh aivi@<jetson-ip> 'cd /tmp/aivi-update && docker build -t aivi/stt-capture:0.1.1 stt_capture && docker build -t aivi/stt-bridge:0.1.0 stt_bridge && docker build -t aivi/llm-proxy:0.2.1 llm_proxy'
```

Update `aivi-capture`, which creates `aivi-llm/` on the drive, and install
the compose file, keeping the old one:

```bash
ssh -t aivi@<jetson-ip> 'sudo sh -c "install -m 0755 /tmp/aivi-update/jetson_capture.sh /usr/local/sbin/aivi-capture && aivi-capture mount && cp -p /opt/aivi/compose/docker-compose.yml /opt/aivi/compose/docker-compose.yml.bak && install -m 0644 /tmp/aivi-update/docker-compose.yml /opt/aivi/compose/docker-compose.yml"'
```

```bash
ssh aivi@<jetson-ip> 'cd /opt/aivi/compose && docker compose up -d'
```

Docker recreates `stt-capture` for its new image and starts the new services;
Speech-to-Phrase keeps running. To roll back, restore the old compose file
and run `docker compose up -d --remove-orphans` as in
[section 9](#install-or-update-the-relay).

### Verify

`?upstream=true` also asks the provider for its model list:

```bash
ssh aivi@<jetson-ip> 'curl -s "localhost:18001/health?upstream=true"'
```

Expected: `"upstream_ok": true` and `"capture": "ready"`. A chat request with
the proxy key from `.env`, which never appears on the command line:

```bash
ssh aivi@<jetson-ip> 'k=$(sed -n "s/^LLM_PROXY_API_KEY=//p" /opt/aivi/compose/.env) && curl -s localhost:18001/v1/chat/completions -H "Authorization: Bearer $k" -H "Content-Type: application/json" -d "{\"model\": \"aivi\", \"messages\": [{\"role\": \"user\", \"content\": \"Sag Hallo.\"}]}"; unset k'
```

### Change model or provider

These `.env` entries override the defaults of the compose file:

| Entry | Default | Purpose |
|---|---|---|
| `LLM_UPSTREAM_MODEL` | `mistral-small-latest` | model behind the alias `aivi` |
| `LLM_UPSTREAM_BASE_URL` | `https://api.mistral.ai/v1` | any OpenAI-compatible server, e.g. a local llama.cpp or vLLM |
| `LLM_UPSTREAM_EXTRA_BODY` | empty | JSON object merged into every request |
| `LLM_PROXY_DAILY_REQUEST_LIMIT` | `500` | requests per UTC day, `0` for no limit |
| `LLM_PROXY_ENABLE_CAPTURE` | `true` | capture on the drive |

To try a failure without touching `.env`, start the services with an
unresolvable host; a shell variable overrides `.env`:

```bash
ssh aivi@<jetson-ip> 'cd /opt/aivi/compose && LLM_UPSTREAM_BASE_URL=https://api.mistral.invalid/v1 STT_API_URL=https://api.mistral.invalid/v1 docker compose up -d llm-proxy stt-bridge'
```

`docker compose up -d llm-proxy stt-bridge` without the variables restores
them. Otherwise, recreate the proxy after changing `.env`:

```bash
ssh aivi@<jetson-ip> 'cd /opt/aivi/compose && docker compose up -d llm-proxy'
```

### Fetch the captured exchanges

```bash
rsync -av aivi@<jetson-ip>:/mnt/aivi-capture/drive/aivi-llm/ dev/datasets/llm/
```

`training/llm/prepare_dataset.py` and the evaluators in `training/eval/` read
this layout.

## 12. Cloud speech to text

`stt-bridge` (`services/stt_bridge`) is a Wyoming speech-to-text server that
sends each request, as one WAV file, to an OpenAI-compatible
`/audio/transcriptions` endpoint: Mistral's Voxtral Mini Transcribe by
default. A second relay, `stt-capture-cloud`, serves it on port 10310 and
captures requests exactly like [section 9](#9-request-capture), now with the
cloud transcript as pseudo label. Port 10300 with Speech-to-Phrase stays as it
is.

- The audio stays in memory. A request ends at most 30 s after its start;
  longer audio is cut to its beginning.
- The bridge reads the names of the areas, floors and entities exposed to
  Assist, plus their aliases, over the Home Assistant websocket with the token
  from [section 4](#4-home-assistant-token), and sends up to 100 of them as
  `context_bias`. It reads them again every 10 minutes, so renamed devices
  arrive without a restart.
- If the provider fails, Home Assistant gets a Wyoming error and logs it
  with the provider's status; the pipeline then ends with
  `stt-stream-failed`.
- The log shows the length of the audio, the time and the number of
  characters, never the text.

It uses `MISTRAL_API_KEY` from [section 11](#keys), is installed with the
proxy and needs nothing else. Test it from the Mac with a recording as in
[section 6](#6-verify):

```bash
uvx --from wyoming==1.10.2 python scripts/wyoming_smoke_test.py stt --host <jetson-ip> --port 10310 --wav test.wav
```

In Home Assistant, add the **Wyoming Protocol** integration a third time,
with host `<jetson-ip>` and port `10310`.

Another transcription API takes `.env` entries too: `STT_API_URL` (default
`https://api.mistral.ai/v1`) and `STT_MODEL` (default
`voxtral-mini-latest`). For OpenAI or a Whisper server, add `--bias-field
prompt` to the bridge's command in the compose file.

## 13. Bluetooth proxy

A Bluetooth dongle at the Jetson serves Home Assistant as an ESPHome-compatible
Bluetooth proxy for the living room: the Vogel's TV mount and presence. It is
a compose project of its own, `/opt/aivi/ble-proxy`, so it runs on a
Raspberry Pi unchanged. The L4T kernel needs a patched Realtek driver for the
dongle; `scripts/ble_proxy_host.sh` builds and installs it with a boot service
and a crash guard. Everything else is in [Bluetooth proxy](ble_proxy.md).

## Logging

`aivi-logmode` switches between two modes:

- **prod:** journald in RAM only and no container logs, so nothing is
  written to the card.
- **dev:** journald on the card (at most 256 MB) and container logs of 3 × 10 MB.

rsyslog is off in both modes. In dev mode, `docker compose logs stt-capture`
shows one line per request: the outcome and whether it was stored, or why
not (`not_enabled`, `no_listening_satellite`, `no_marker`, …). Transcripts
never appear in a log. `docker compose logs wake-word-recorder` shows one
line per take start, refusal and end, without speaker names.
`docker compose logs llm-proxy` shows one line per chat request (status,
time, number of tool calls, repair) and the provider's error messages, and
`docker compose logs stt-bridge` one line per request (length, time,
characters); neither logs what was said.

```bash
ssh -t aivi@192.168.55.1 sudo aivi-logmode prod
```

The switch restarts Docker and recreates the services, so Speech-to-Phrase
trains again. To debug, switch to dev, reproduce the problem and switch back
to prod.

The journal from dev mode stays on the card after the switch to prod. Delete
it; dev mode creates the directory again:

```bash
ssh -t aivi@192.168.55.1 sudo rm -rf /var/log/journal
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Many `Guessing pronunciation for …` warnings | English entity or area names in a German model | Give the exposed entities German display names, then restart speech-to-phrase ([section 8](#8-names-and-sentences)) |
| `schalte Wohnzimmer ein`, answer "Es wurden mehrere … gefunden" | An entity is named like an area | Rename the entity ([section 8](#8-names-and-sentences)) |
| `daslicht` in the transcript | Built-in German rule of speech-to-phrase 1.4.3 | Install the custom sentences ([section 8](#8-names-and-sentences)) |
| A renamed light keeps its old name in speech-to-phrase | The entity is unavailable, so its restored state still carries the old name | Set the entity name as well, not only the device name |
| `docker logs` fails with `configured logging driver does not support reading` | prod log mode keeps no container logs | Switch to dev, reproduce, switch back ([Logging](#logging)) |
| New containers fail with `Operation not permitted` on thread start | Docker 20.10.7 from the SD image blocks `clone3` | Provision; the update brings Docker 20.10.21 |
| `curl: command not found` | The SD card image has no curl | Provision installs it |
| No `/dev/cu.usbmodem…` after boot | Charge-only cable, or setup already done | Use a data cable; after setup use SSH instead |
| Switch `Anfragen aufzeichnen` on, but no samples | Drive not mounted, or the request was refused | `aivi-capture status`; in dev mode the relay log names the reason ([section 9](#9-request-capture)) |
| Relay log `reason=ha_error` | Token in `.env` invalid, or an entity ID in `--satellite` does not exist | Check the entity IDs on the satellite's device page; renew the token ([section 4](#4-home-assistant-token)) |
| Assist answers that the daily limit is reached | `LLM_PROXY_DAILY_REQUEST_LIMIT` used up | Wait for midnight UTC, or raise the entry and recreate `llm-proxy` ([section 11](#change-model-or-provider)) |
| llama.cpp integration: invalid authentication | Proxy key in Home Assistant differs from `LLM_PROXY_API_KEY`, or the entry is missing | Compare the keys; the proxy refuses everything without one ([section 11](#keys)) |
| `stt-stream-failed` with the LLM pipeline, Home Assistant log names `stt-bridge-upstream` | Provider unreachable or the key is invalid | `curl -s "localhost:18001/health?upstream=true"` on the Jetson; switch the satellite to the Speech-to-Phrase pipeline meanwhile |
| No speech recognition after installing the relay | `stt-capture` not running, e.g. image not built | `docker compose ps`; build the image or roll back ([section 9](#9-request-capture)) |
| Drive plugged in, `aivi-capture status` says not mounted | Plugged in after an eject, or udev missed it | `sudo aivi-capture mount` |
| Satellite status `Fehler: Jetson nicht erreichbar` | `wake-word-recorder` not running, or `wake_word_recorder_host` wrong | `docker compose ps`; check the secret and flash again ([section 10](#10-wake-word-recording)) |
| Satellite status `Fehler: Token passt nicht` or `Recorder ohne Token` | The token differs between `esphome/secrets.yaml` and `.env`, or is missing in `.env` | Set the same token in both; `docker compose up -d wake-word-recorder` ([section 10](#token-and-address)) |
| Satellite status `Fehler: Laufwerk fehlt` | Drive not mounted, or no `aivi-wake-word/` directory | `sudo aivi-capture mount` |
| `aivi-capture eject` refuses: a wake word take is running | A take holds its `.part` file open | Switch `Wake-Word-Aufnahme` off, then eject |
