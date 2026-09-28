# Jetson Nano setup

This runbook turns a classic Jetson Nano Developer Kit (4 GB, microSD) into
the speech server of plan phase 4. It runs two Wyoming services for Home
Assistant:

| Service | Purpose | Port |
|---|---|---:|
| Speech-to-Phrase | speech to text | 10300 |
| Piper (`de_DE-thorsten-medium`) | text to speech | 10200 |
| Whisper (`base-int8`, on demand) | speech to text, comparison only | 10301 |

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
scp -r scripts/jetson_provision.sh scripts/jetson_logmode.sh compose/jetson/docker-compose.yml compose/jetson/custom_sentences aivi@192.168.55.1:/tmp/aivi-provision/
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

## Logging

`aivi-logmode` switches between two modes:

- **prod:** journald in RAM only and no container logs, so nothing is
  written to the card.
- **dev:** journald on the card (at most 256 MB) and container logs of 3 × 10 MB.

rsyslog is off in both modes.

```bash
ssh -t aivi@192.168.55.1 sudo aivi-logmode prod
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Many `Guessing pronunciation for …` warnings | English entity or area names in a German model | Give the exposed entities German display names, then restart speech-to-phrase ([section 8](#8-names-and-sentences)) |
| `schalte Wohnzimmer ein`, answer "Es wurden mehrere … gefunden" | An entity is named like an area | Rename the entity ([section 8](#8-names-and-sentences)) |
| `daslicht` in the transcript | Built-in German rule of speech-to-phrase 1.4.3 | Install the custom sentences ([section 8](#8-names-and-sentences)) |
| A renamed light keeps its old name in speech-to-phrase | The entity is unavailable, so its restored state still carries the old name | Set the entity name as well, not only the device name |
| New containers fail with `Operation not permitted` on thread start | Docker 20.10.7 from the SD image blocks `clone3` | Provision; the update brings Docker 20.10.21 |
| `curl: command not found` | The SD card image has no curl | Provision installs it |
| No `/dev/cu.usbmodem…` after boot | Charge-only cable, or setup already done | Use a data cable; after setup use SSH instead |
