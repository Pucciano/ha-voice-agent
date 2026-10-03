# Satellite flashing

This runbook brings up one AIVI satellite board: a Seeed XIAO ESP32-S3 Plus
mounted on a reSpeaker XVF3800. Repeat it for every room. The steps were
verified on the living-room board (`aivi-sat-living-room`), first on hardware
revision 1.0 and again on 1.1.

| Revision | XIAO mounting | Status |
|---|---|---|
| 1.0 | Soldered flat onto the XVF3800 board (surface mount, board to board) | Retired: the XVF3800 failed twice, the second time for good |
| 1.1 | Soldered onto standoff pins (pin headers) | Current |

The board runs the phase 5 firmware from `esphome/`. "Okay Nabu" is detected on
the device and starts the Home Assistant pipeline `AIVI Lokal Deutsch`. The
satellite has no speaker: it reports the answer URL to Home Assistant, which
plays it on the room's speaker. The flashing steps stay the same when later
phases add packages.

## Prerequisites

- `uv` on the Mac. ESPHome runs through `uvx` with the version pinned in the
  `Makefile`, so there is nothing to install globally.
- `dfu-util` for the XVF3800 (`brew install dfu-util`).
- `esphome/secrets.yaml`, created from `esphome/secrets.yaml.example`. It holds
  the Wi-Fi credentials and one API key per satellite. The file is gitignored.

The satellites join a dedicated IoT Wi-Fi network on its own VLAN. mDNS does
not cross VLANs, so from another network address a satellite by its IP
address. Give each satellite a DHCP reservation for its MAC address.

## Per board

### 1. Check the hardware

Mount the XIAO on standoff pins, not flat onto the board (revision 1.1).
Before the first power-up, measure `5V`↔`GND` and `3V3`↔`GND` for shorts.
On the living-room board `5V`↔`GND` read about 285 Ω in circuit.
Attach the U.FL antenna to the XIAO. The XIAO has no PCB antenna. Without the
external antenna the first board saw its access point at -96 dBm and could not
connect.

### 2. Flash the XVF3800 firmware (once per board)

A new XVF3800 runs the factory USB firmware. In that mode it does not answer
on I2C and sends no I2S audio. Switch it to the formatBCE I2S firmware v1.0.7:

1. Connect only the XMOS USB-C port (next to the 3.5 mm jack). Do not connect
   the XIAO at the same time: both ports feed the same 5 V rail.
2. Run `make sat-xvf3800`.

The script takes the image from the formatBCE commit pinned in
`esphome/packages/aivi-base.yaml` and checks its MD5. It writes the DFU upgrade
slot (`alt=1`) only. The factory slot stays as a fallback. After a successful
flash the board disappears from USB, because the I2S firmware has no USB
interface.

A board that already runs the I2S firmware does not show up on USB. The script
then stops without changes.

### 3. Create the device configuration (new room only)

Room identifiers are English. File and node names use hyphens
(`aivi-sat-living-room`), secret names use underscores
(`living_room_api_key`).

1. Copy `esphome/aivi-sat-living-room.yaml` to `esphome/aivi-sat-<room>.yaml`.
2. Change `name`, `friendly_name` and the secret name `<room>_api_key`.
3. Add the key to `esphome/secrets.yaml`. Generate it with
   `openssl rand -base64 32`. Never reuse a key from another satellite.
4. Copy `esphome/aivi-sat-living-room-diagnostics.yaml` to
   `esphome/aivi-sat-<room>-diagnostics.yaml` and point its `device` include at
   the new file. This is the diagnostic build of the same device.

The API key also encrypts OTA updates, so there is no separate OTA password.

### 4. Flash the ESP32 over USB

1. Disconnect the XMOS port and connect the XIAO USB-C port. The board appears
   as `/dev/cu.usbmodemXXXX` (`ls /dev/cu.usbmodem*`). If it does not appear,
   hold the XIAO BOOT button while you plug in the cable.
2. Identify the chip and note the MAC address for the DHCP reservation:

   ```bash
   make sat-chip DEVICE=/dev/cu.usbmodemXXXX
   ```

   Expected: `ESP32-S3`, `Embedded PSRAM 8MB`. The XIAO ESP32-S3 Plus reports
   16 MB flash. The configuration uses 8 MB, which also fits the plain XIAO.
3. Build, flash and follow the log:

   ```bash
   make sat-flash SAT=<room> DEVICE=/dev/cu.usbmodemXXXX
   ```

   The first build downloads ESP-IDF and takes several minutes.

### 5. Verify

- At boot the LED ring runs one lap each in red, green and blue. This only
  happens when the ESP32 reaches the XVF3800 over I2C.
- The log shows `Found device at address 0x18` (audio codec) and `0x2C`
  (XVF3800), and `XMOS firmware version: 1.0.7`.
- Wi-Fi connects and the log shows the IP address. The ring blinks orange until
  Home Assistant has connected, then it goes dark.
- `Pegel Kanal 0` and `Pegel Kanal 1` change with sound. Quiet rooms read around
  -75 dBFS and speech between -40 and -15 dBFS.
- In Home Assistant, reload the satellite's ESPHome integration once after the
  first flash with the voice assistant. Only then does it create the
  `Assistent` (pipeline) and `Sprechpausen-Erkennung` selects. Set `Assistent`
  to `AIVI Lokal Deutsch`.
- Say "Okay Nabu", then "Wie spät ist es?". The ring pulses white-blue twice,
  points at you while you speak, and shows a circling comet while Home
  Assistant works. Home Assistant receives `esphome.aivi_wake_word`,
  `esphome.aivi_stt_text` ("wie spät ist es") and `esphome.aivi_tts_uri` (an
  absolute `http://…/api/tts_proxy/….mp3` URL), each with the `satellite_id`.
- Switch on `Mikrofon stumm`. The ring turns red and "Okay Nabu" does nothing.

Each detection logs `Detected 'Okay Nabu' with sliding average probability is
… and max probability is …`. The average is taken at the moment it crosses the
threshold, so it always sits just above the cutoff (0.85) and says nothing
about confidence. Judge detections by the max probability instead. On the
living-room board it averaged 0.98 in a quiet room and 0.94 with music.

ESPHome 2026.9 logs sensor states at VERBOSE level only, so the levels do not
appear in the default log. Read them in Home Assistant or with any API client.

### LED ring states

| Ring | Meaning |
|---|---|
| Red, green, blue chase | Boot, or `LED-Ring-Test` pressed |
| Blinking orange | Wi-Fi or Home Assistant not connected |
| Steady red | Microphone muted |
| Three short red flashes | Error, e.g. no command recognised |
| Two white-blue pulses | Wake word detected |
| One blue LED | Listening; the LED points at the speaker |
| Circling white-blue comet | Home Assistant is working on the command |
| Off | Ready, listening for the wake word |

The satellite refuses follow-up questions (`continue_conversation`): every
command needs the wake word. Without an echo reference it would otherwise hear
its own answer from the room speaker.

### 6. Route answers to the room speaker

The Home Assistant automation `config/homeassistant/aivi_tts_router.yaml`
(`AIVI Antwort-Routing`) plays each answer on exactly one speaker. For a new
room, add an entry under `rooms`:

```yaml
    kitchen:
      device_id: <Home Assistant device id of aivi-sat-kitchen>
      speaker: media_player.pulse_flex_kitchen_1
      volume: 0.3
```

The device id is the last part of the satellite's device page URL in Home
Assistant. Update the automation there with the file content (edit in YAML).

The automation fails closed. It plays nothing for an unknown satellite, for an
event from a different device, for a URL outside the Home Assistant TTS proxy,
or while the speaker is grouped with other rooms. Only the path of the URL
goes to the speaker; Home Assistant adds its own address, so the automation
holds no IP address. The answer plays at the room's `volume`, and the previous
volume comes back afterwards. It replaces running music, which does not resume
(plan section 20, version 1).

Test it:

- "Okay Nabu", then "Wie spät ist es?": the answer comes from the room speaker.
- Negative test: in Home Assistant, fire the event `esphome.aivi_tts_uri` with
  `satellite_id: unknown` from the developer tools. Nothing plays, and the
  automation trace ends at the first condition.

### 7. Later updates

Updates go over the network with encrypted OTA:

```bash
make sat-flash SAT=<room> DEVICE=<ip-address>
```

### 8. Replace a board

A replacement board for a room keeps the room's device file and API key, so
Home Assistant keeps the device, its entities and the router entry:

1. Unplug the old board for good. It runs under the same name and key.
2. Run steps 1, 2 and 4 on the new board, with the same `SAT=<room>`.
3. Move the room's DHCP reservation to the new MAC address and restart the
   satellite.
4. In Home Assistant, open the satellite's ESPHome entry, choose
   `Reconfigure` and enter the new IP address. Home Assistant reports the same
   name with a new MAC address. Choose `Migrate configuration to new device`.
   The device ID stays the same, so `AIVI Antwort-Routing` needs no change.
5. Run the checks from step 5.

## Diagnostic firmware

Use it when a satellite hears nothing: `Pegel Kanal 0` and `Pegel Kanal 1` stay
at `-inf` (digital silence) and "Okay Nabu" does nothing. It is the regular
firmware plus `esphome/packages/aivi-xvf3800-diagnostics.yaml`, which looks
inside the XVF3800 and can repair its flash. Flash it over the network; the
log follows right after the upload:

```bash
make sat-diag SAT=<room> DEVICE=<ip-address>
```

The package adds diagnostic entities named `Diagnose …`. Read and press them
on the device page in Home Assistant. Every probe also appears in the log, one
XMOS servicer every 400 ms:

```text
probe dfu_version   240/88  status=0x00 tries=1  data=01 00 07
probe aec_spenergy   33/80  status=0x40 tries=30 data=00 00 …
```

How to read it:

- `Diagnose Dienste` lists the status of each servicer. `00` means it answered.
  `dfu_version`, `gpo_values` and `app_version` run on the control tile of the
  XVF3800. The `aec_*`, `am_*` and `pp_*` servicers run on the audio tile. If
  they stay at `40` (retry) or `44` (queue full) after 30 tries, the audio
  pipeline is not running, and the XVF3800 sends only zeros.
- `Diagnose Pins` shows the pulls and the share of high samples on each line.
  BCLK and LRCLK read about half high while the clock runs. GPIO43, the data
  from the XVF3800, has a pull-up, so `high=0` means the XVF3800 drives zeros.
  A broken solder joint on D6 would read high.
- `Diagnose Sprachenergie`, `Diagnose Mikrofon-Gain`, `Diagnose AGC-Gain` and
  `Diagnose DSP-Leerlauf` show values only while the audio servicers answer.
  Speech energy rises above zero while someone speaks.
- `Diagnose Ausgang links Quelle` routes the left output (ESP32 channel 0) to a
  raw microphone. If `Pegel Kanal 0` then moves with sound, the microphone and
  the data line work.

Repairs, in this order:

1. `Diagnose XVF3800 Neustart` reboots only the XVF3800.
2. `Diagnose XVF3800 Konfiguration löschen` removes parameters saved in the
   XVF3800 flash and reboots it. The satellite saves none, so nothing is lost.
3. `Diagnose XVF3800 Firmware flashen` writes the pinned image 1.0.7 into the
   DFU upgrade slot over I2C. It takes about 4.5 minutes; keep the power on.
   The log shows the progress and ends with `Update complete`. If it breaks
   off, the XVF3800 starts the factory USB firmware at the next power-up;
   repeat step 2 of the per-board steps to get it back.
4. `Diagnose GPIO9 und GPIO44 hochohmig`, then a reboot of the XVF3800, rules
   out the ESP32. GPIO9 is the MCLK line and GPIO44 carries data to the
   XVF3800.

If the audio servicers still do not answer after these steps, suspect a
hardware defect of the XVF3800 board.

The package embeds the XVF3800 image (889 KB). The component flashes it at boot
only when the board reports a different version. Go back to the regular
firmware afterwards:

```bash
make sat-flash SAT=<room> DEVICE=<ip-address>
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| I2C scan finds `0x18` but not `0x2C`, no microphone levels | XVF3800 still on factory USB firmware | Step 2 |
| Wi-Fi `Auth Expired` or `Probe Request Unsuccessful`, RSSI near -95 dBm | Antenna missing | Attach the U.FL antenna |
| One `Authentication Failed` right after a USB reset, then connected | The access point still held the old association | None; clean reboots connect at the first attempt |
| `aivi-sat-<room>.local` does not resolve | mDNS does not cross VLANs | Use the IP address |
| No `/dev/cu.usbmodem*` | Charge-only cable or no bootloader | Use a data cable; hold BOOT while plugging in |
| `Pegel Kanal 0` and `1` at `-inf`, no wake word | XVF3800 audio pipeline not running | [Diagnostic firmware](#diagnostic-firmware) |
