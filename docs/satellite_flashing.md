# Satellite flashing

This runbook brings up one AIVI satellite board: a Seeed XIAO ESP32-S3 Plus
soldered onto a reSpeaker XVF3800. Repeat it for every room. The steps were
verified on the living-room board (`aivi-sat-living-room`).

The board runs the phase 3 firmware from `esphome/`: the wake word "Okay Nabu"
on the device, with LED feedback and no voice pipeline yet. The flashing steps
stay the same when later phases add packages.

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

Before the first power-up, measure `5V`↔`GND` and `3V3`↔`GND` for shorts.
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
- Say "Okay Nabu". The ring pulses white-blue twice and then points at you for
  three seconds. `Wake-Word-Erkennungen` counts up, and Home Assistant receives
  the event `esphome.aivi_wake_word` with the `satellite_id`.
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
| Two white-blue pulses, then one blue LED | Wake word detected; the LED points at the speaker |
| Off | Ready, listening for the wake word |

### 6. Later updates

Updates go over the network with encrypted OTA:

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
