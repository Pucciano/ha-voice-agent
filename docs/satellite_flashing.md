# Satellite flashing

This runbook brings up one AIVI satellite board: a Seeed XIAO ESP32-S3 Plus
mounted on a reSpeaker XVF3800. Repeat it for every room. The steps were
verified on the living-room board (`aivi-sat-living-room`), first on hardware
revision 1.0 and again on 1.1.

| Revision | XIAO mounting | Status |
|---|---|---|
| 1.0 | Soldered flat onto the XVF3800 board (surface mount, board to board) | Retired: the XVF3800 failed twice, the second time for good |
| 1.1 | Soldered onto standoff pins (pin headers) | Current |

The board runs the phase 5 firmware from `esphome/`. "Hey AIVI" is detected on
the device and starts the Home Assistant pipeline `AIVI Lokal Deutsch`. The
wake word model is trained locally and stays out of git: copy it to
`esphome/models/` before the first build
([wake word training, section 9](wake_word_training.md#9-put-the-model-on-the-satellite)). The
answer plays on the room's speaker: Home Assistant streams it there over the
network, or the satellite plays it itself through its 3.5 mm jack into the
speaker's analog input, which gives the XVF3800 an echo reference
([step 6](#6-route-answers-to-the-room-speaker)). Voice timers ring on the
satellite ([step 8](#8-voice-timers)). The flashing steps stay the same when
later phases add packages.

## Prerequisites

- `uv` on the Mac. ESPHome runs through `uvx` with the version pinned in the
  `Makefile`, so there is nothing to install globally.
- `dfu-util` for the XVF3800 (`brew install dfu-util`).
- `esphome/secrets.yaml`, created from `esphome/secrets.yaml.example`. It holds
  the Wi-Fi credentials, one API key per satellite, and the address and token
  of the wake word recorder on the Jetson
  ([Jetson runbook, section 10](jetson_setup.md#10-wake-word-recording)).
  The file is gitignored.

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
2. Change `name`, `friendly_name`, `satellite_id`, the secret name
   `<room>_api_key`, and `room_speaker`: the Home Assistant name of the
   speaker the jack is cabled to. It becomes the network option of
   `Audioausgabe`.
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
- Say "Hey AIVI", then "Wie spät ist es?". The ring pulses white-blue twice,
  points at you while you speak, and shows a circling comet while Home
  Assistant works. Home Assistant receives `esphome.aivi_wake_word`,
  `esphome.aivi_stt_text` ("wie spät ist es") and `esphome.aivi_tts_uri` (an
  absolute `http://…/api/tts_proxy/….mp3` URL), each with the `satellite_id`.
- Switch on `Mikrofon stumm`. The ring turns red and "Hey AIVI" does nothing.
- `Anfragen aufzeichnen` is off after every boot. While it is on, the Jetson
  stores every request of this satellite on its USB drive, if the satellite
  is listed there ([Jetson runbook, section 9](jetson_setup.md#9-request-capture)).
- `Wake-Word-Aufnahme Status` reads `Bereit`. A short take (set
  `Wake-Word-Aufnahme Dauer` to 0.5 and switch `Wake-Word-Aufnahme` on) shows
  a circling light and ends with `Gespeichert: …`, if the satellite is listed
  on the Jetson ([wake word training, section 4](wake_word_training.md#4-record-the-household)).
- `Audioausgabe` reads the room speaker's name (answers over the network), and
  Home Assistant lists the media player `Audioausgang`. `AEC-Verzögerung` and
  `AEC-Referenzverstärkung` show the XVF3800's own values until the self test
  or someone sets them (formatBCE's firmware 1.0.7: -30 and 8.0);
  `Selbsttest Status` reads `Bereit`.

Each detection logs `Detected 'Hey AIVI' with sliding average probability is
… and max probability is …`. The average is taken at the moment it crosses the
threshold, so it always sits just above the cutoff (0.98 at
`Wenig empfindlich`) and says nothing about confidence. Judge detections by
the max probability instead. On the living-room board the first detections
reached 1.00.

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
| Circling green light | Wake word take running: "Hey AIVI" again and again |
| Circling red light | Wake word take running: everyday sound |
| Circling white light | Echo canceller self test running |
| Pulsing amber, once a second | A timer has finished and its alarm rings |
| Pulsing amber, every 4 seconds | Alarm not stopped; its gong ended after 2 minutes |
| Amber, shrinking | A timer runs; the lit LEDs show the time left of the timer that ends next |
| Amber, breathing | The timer that ends next is paused |
| Off | Ready, listening for the wake word |

The satellite refuses follow-up questions (`continue_conversation`): every
command needs the wake word. Over the network it has no echo reference and
would hear its own answer from the room speaker; through the jack the echo
canceller has to prove itself first
([step 7](#7-tune-the-echo-canceller)).

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

- "Hey AIVI", then "Wie spät ist es?": the answer comes from the room speaker.
- Negative test: in Home Assistant, fire the event `esphome.aivi_tts_uri` with
  `satellite_id: unknown` from the developer tools. Nothing plays, and the
  automation trace ends at the condition `Known satellite, sent by its own
  device`.

#### Through the jack

The satellite can play the answer itself: ESP32 → XVF3800 → codec → 3.5 mm
jack, cabled to the speaker's analog input. The XVF3800 takes everything the
satellite plays as the echo canceller's reference, so it can remove the
answer, and music played through the satellite, from what the microphones
hear. Music the speaker plays on its own (Spotify, BluOS) is not in the
reference.

1. Cable the satellite's jack to the speaker's analog input. In the BluOS app,
   for that input: A/V mode on (50 ms delay, the shortest BluOS offers), and
   `Hide source` off. Home Assistant reads the speaker's inputs every
   15 minutes, so it can take that long until `Analog Input` appears in the
   speaker's source list.
   Keep `Amplifier Standby` (BluOS app, Settings > Player) on: the speaker
   puts its amplifier to sleep after 15 minutes without playback. A selected
   analog input counts as playback even when silent, so
   `AIVI Klinkenausgang freigeben` stops the input once the jack has been
   idle for 10 minutes. Measured on the living room Pulse Flex with
   `Lautsprecher-Anlauf messen`: stopped for 2 or 27 minutes, it played the
   input 1.1-1.4 s after the switch, well before an answer starts. If the
   start of answers goes missing, time the speaker as described in
   section 7 and, if it starts later than about 3 seconds, switch
   `Amplifier Standby` off.
2. Add the room to `config/homeassistant/aivi_jack_output.yaml`
   (`AIVI Klinkenausgang`): an entry under `rooms` with the satellite's device
   id, its `Audioausgabe` select and `Audioausgang` media player, the speaker,
   its analog input and a fixed volume; also list the select and the media
   player in the two state triggers. Add the room to
   `config/homeassistant/aivi_jack_release.yaml`
   (`AIVI Klinkenausgang freigeben`) too: the speaker and its analog input,
   and the satellite's `Audioausgang`, assist satellite and
   `Selbsttest Status`. Add it to `config/homeassistant/aivi_jack_playback.yaml`
   (`AIVI Klinkenausgang Wiedergabe`) as well: the speaker in the three
   triggers, and under `rooms` the speaker, its analog input and the
   satellite's `Audioausgang`. Update the three automations in Home Assistant
   with the file content.
3. Set `Audioausgabe` to `Klinke`. The speaker switches to `Analog Input` and
   gets the fixed volume.

From then on, the wake word switches the speaker to its analog input if it
plays something else (music stops before the command, as with network
answers), and the satellite plays the answer through the jack. The event
`esphome.aivi_tts_uri` carries `output: jack`, so the TTS router plays
nothing. The speaker keeps its fixed volume, because the echo canceller needs
a constant echo path; set the loudness on `Audioausgang` instead (100 % is
0 dB, the line level of the analog input; 1 % is -32 dB). Home Assistant can
play anything on `Audioausgang`, for example with `tts.speak` or
`media_player.play_media`; the speaker switches when it starts.
Music Assistant streams to the satellite with its Sendspin protocol. It
finds the satellite by mDNS (Music Assistant and the satellite must share a
network) and connects to TCP port 8928; the satellite then appears as a
player of its own, named after the device. To play Spotify on it, enable
Music Assistant's `Spotify Connect` plugin and select the satellite under
`Connected players`: the Spotify app then lists it as a device. Pick the
satellite, not the speaker: what the speaker plays through its own Spotify
Connect bypasses the satellite and stays outside the echo reference.

`AIVI Klinkenausgang` acts only for listed rooms and only on events from the
satellite's own device. The jack has priority over a group: a speaker that
follows another room leaves the group, and a speaker that leads one releases
its followers, so no other room plays the satellite's audio. It then checks
that the speaker reports `Analog Input`, tries once more, and writes a
warning to the Home Assistant log (logger `aivi_jack_output`) if it still
does not.

`AIVI Klinkenausgang freigeben` checks every minute and stops the speaker's
analog input when, for 10 minutes each, the speaker has played the input,
`Audioausgang` has played nothing, the assist pipeline has not run and the
self test has not run. It stops only the analog input: whatever else the
speaker plays (network answers with `Audioausgabe` on the room speaker,
Spotify, presets, a group led by another room) stays, and BluOS puts the
speaker to standby on its own. With `Audioausgabe` on the room speaker the
analog input only carries what `Audioausgang` plays, so it is stopped
10 minutes after that ends; the same holds for an input left selected after
switching away from `Klinke`.

`AIVI Klinkenausgang Wiedergabe` keeps music on `Audioausgang` in step with
the speaker. When the speaker stops playing its analog input (paused with its
button, the BluOS app, the wall display or a voice command, stopped, or
switched to another source or group) while `Audioausgang` plays,
`Audioausgang` pauses too, so Music Assistant and Spotify pause. Without it the
music plays on unheard, and the next wake word, which restarts the paused
input for the answer, brings it back. When a person starts the paused input
again, `Audioausgang` resumes. Restarts by `AIVI Klinkenausgang` resume
nothing: the speaker's state change carries the automation's context, and
contextless restarts within 15 seconds are skipped only if the paused state
predates that run. A manual pause after the run can be resumed immediately.

How the Pulse Flex treats its analog input (BluOS 4.12, read from the
speaker's settings):

- No Auto Sense: BluOS switches automatically only to digital inputs
  (optical, coaxial, HDMI) when a signal appears, so the automation selects
  the analog input.
- Selecting the input starts a continuous stream of it at once, with or
  without a signal: about 0.2 s after the call Home Assistant reports
  `playing` with source `Analog Input`, about 0.5 s after it BluOS reports
  state `stream` (48 kHz, 24 bit).
- No level control on the input: no gain, no automatic level, no
  compressor. Replay gain applies to files only. The tone controls (bass
  and treble) are a fixed filter; the echo canceller learns them with the
  room. The volume limits (-63 to -3 dB) cap the speaker's volume.

Test it:

- With `Audioausgabe` on `Klinke`: "Hey AIVI", then "Wie spät ist es?". The
  answer comes from the speaker's analog input; the router's trace ends at
  `Answer goes to the room speaker`.
- Start music on the speaker from the BluOS app, then say "Hey AIVI": the
  music stops and the speaker switches to `Analog Input`.
- Back on the room speaker's name, answers play over the network again.

### 7. Tune the echo canceller

The self test measures the echo path through the jack and sets the echo
canceller (AEC). It plays about 45 seconds of noise bursts and tones from
125 Hz to 4 kHz through the jack, and the XVF3800 records them: its output
mux sends the microphone and the reference, later the AEC residual, to the
ESP32 instead of the processed signal. It plays at full satellite volume,
whatever `Audioausgang` is set to, and restores the volume afterwards. Keep
the room quiet and do not speak
while it runs; the wake word is paused, and the ring shows a circling white
light.

Run it after cabling, after moving the satellite or the speaker, and after a
change of the speaker's volume:

1. `Audioausgabe` on `Klinke`, the speaker not grouped with other rooms.
2. Press `Selbsttest starten`. `Selbsttest Status` follows the steps:
   waiting for the echo of a 1 kHz pilot tone (the speaker may still be
   switching to its analog input), delay, tones and reverberation, echo
   attenuation. `Selbsttest abbrechen` stops it and restores the settings.
3. When it reads `Fertig`, read the results:

| Entity | Meaning |
|---|---|
| `Echo-Verzögerung` | Time from the reference to the echo at the microphone. Behind a Bluesound analog input a little more than its 50 ms; it should not change between runs. |
| `AEC-Verzögerung` | Set to the delay minus 24 samples, at most 256 (16 ms). The rest stays inside the 192 ms echo filter. |
| `AEC-Referenzverstärkung` | Set so a full-scale signal reaches -2 dBFS in the XVF3800 (XMOS: as high as possible, 1-2 dB headroom), between 0.25 and 8. The test measures at 1.0, because formatBCE's 8.0 would clip its tones. |
| `Echo-Abstand zur Referenz` | Echo peak at the microphone relative to the reference peak. XMOS wants at least 6 dB below; above that, lower `Audioausgang`. |
| `Nachhallzeit` | RT60, median of the 500 Hz to 2 kHz bands. |
| `Echodämpfung` | Echo attenuation (ERLE), broadband, over the last 5 of 20 seconds of noise. The test moved the AEC settings, so the canceller adapts anew; `Selbsttest Ergebnis` shows the value after 5, 10, 15 and 20 s. |
| `Selbsttest Ergebnis` | Delay, the old and new AEC values, the rest of the delay, echo to reference, RT60, how much of the decay the filter covers, attenuation, convergence. `Lautsprecher spielte erst nach … s` means the speaker started late, for example while its amplifier woke from standby. |
| `Selbsttest Frequenzbänder` | Per band: echo path gain, RT60 and attenuation. |
| `Selbsttest Hinweise` | What to change, or `Keine`. |

`Echodämpfung messen` runs only the attenuation part (about 25 seconds) with
the current settings and changes nothing. Use it to compare settings, for
example `AEC-Verzögerung` at -30 and at 256, or after the speaker sat silent
on its analog input for more than 15 minutes: if its amplifier slept, the
result says `Lautsprecher spielte erst nach … s`.

`Lautsprecher-Anlauf messen` times how the room speaker starts to play the
jack, and changes nothing either. It plays a sweep from 88 Hz to 5.7 kHz,
repeated every 250 ms, and tells Home Assistant to switch the speaker at the
moment the sweep starts, as a wake word does. It ends 8 seconds after the
first echo. `Selbsttest Ergebnis` gives the time from that switch to the
first sound and to full level (every band within 1 dB of its level at the
end); `Selbsttest Frequenzbänder` gives the time and the final level per
band, and `Lautsprecher-Anlaufzeit` keeps the full-level time. The `done`
event and the log also carry the level of every sweep. To time a sleeping
amplifier, stop the speaker and wait more than 15 minutes first; nothing may
select its analog input meanwhile, not even a wake word.

The test changes only the two AEC values; it stores them on the satellite,
which writes them at every boot. Level advice stays advice, because the
volume and the microphone gain also change what the wake word and speech
recognition hear. Home Assistant gets the event `esphome.aivi_self_test` with
`phase: start`, then `done` with every value, or `error` with a code:

| Status | Cause |
|---|---|
| `Die Wiedergabe erreicht den XVF3800 nicht` | The reference stays silent: the ESP32's audio does not reach the XVF3800 (GPIO44, I2S output). |
| `Kein Echo …` | The microphones do not hear the pilot within 20 s: speaker not on its analog input, cable, or volume too low. |
| `Echo zu schwach für die Verzögerung` | No clear correlation peak in at least two of the three bursts. |
| `Messkanäle unplausibel` | The residual is louder than the microphone; the XVF3800 firmware routes its outputs differently. |

The echo canceller works with what is left of its filter: behind the
Bluesound's 50 ms, about 40 ms of the 192 ms filter carry no echo, so it
covers only about 150 ms of the room's decay. `Selbsttest Ergebnis` says how
many dB of the decay that is.

### 8. Voice timers

Home Assistant runs voice timers only for a device that announces timer
support, and the firmware announces it (`esphome/packages/aivi-timers.yaml`).
Home Assistant then offers the timer intents to the satellite: the built-in
agent understands "Stelle einen Timer auf zwei Minuten", "Wie lange läuft der
Timer noch?" or "Brich den Timer ab", and an LLM agent gets the timer tools
(`HassStartTimer`, `HassCancelTimer`, `HassTimerStatus` and others). Without
it Home Assistant answers "Timer werden auf diesem Gerät nicht unterstützt".
Home Assistant keeps the timers; the satellite gets every start, change,
cancellation and end, and counts down in between.

- While a timer runs, the LED ring counts it down in amber: twelve LEDs at
  the start, one fewer every twelfth of the time, and the last LED dims as
  its twelfth runs out. With several timers it shows the one that ends next.
  The ring's other states take precedence.
- When a timer ends, the satellite plays a gong on `Audioausgang`, then Home
  Assistant says which timer has ended ("Der Timer über 2 Minuten ist
  abgelaufen.", with the name if the timer has one), then the gong repeats
  every 3 seconds. The ring pulses amber.
- The wake word stops the alarm and starts no command; say it again for a
  command. If nobody stops it, the gong ends after 2 minutes and the ring
  pulses slowly until the next wake word. With the microphone muted, the
  wake word cannot stop it.
- The alarm waits until a running command, its answer, a self test or a wake
  word take has ended (at most 60 seconds). Music on `Audioausgang` stays
  ducked while the gong repeats.

Gong and announcement always play through the jack, whatever `Audioausgabe`
says: `AIVI Klinkenausgang` switches the room speaker to its analog input as
soon as `Audioausgang` starts. They are in the echo reference, so the wake
word is heard over them. A speaker that `AIVI Klinkenausgang freigeben` has
stopped plays the input about 1.2 seconds late, so the start of the first
gong can be missing. The gong is `esphome/sounds/timer_finished.wav`, three
rising bell notes made by `scripts/generate_timer_sound.py`; run the script
again after changing it.

Home Assistant receives `esphome.aivi_timer` with the `satellite_id` and a
`phase`: `finished` after the first gong (with `name` and `total_seconds`),
`silenced` when the gong ended on its own, `stopped` when a wake word ended
the alarm.

Set it up:

1. Add the room to `config/homeassistant/aivi_timer_announcement.yaml`
   (`AIVI Timer-Ansage`): an entry under `rooms` with the satellite's device
   id and its `Audioausgang` media player. `tts_engine`, `tts_language` and
   `tts_voice` are the text-to-speech settings of the satellite's pipelines.
   Update the automation in Home Assistant with the file content.
2. Flash the firmware ([step 9](#9-later-updates)). Home Assistant takes the
   timer support over when the satellite reconnects (the living room
   satellite needed no reload). If it still answers "Timer werden auf diesem
   Gerät nicht unterstützt", reload the satellite's ESPHome integration.

Test it:

- "Stelle einen Timer auf eine Minute": Home Assistant confirms, and the
  ring shows twelve amber LEDs, one fewer every 5 seconds.
- "Wie lange läuft der Timer noch?": the answer gives the time left; the ring
  goes back to the countdown afterwards.
- "Brich den Timer ab": the countdown disappears.
- "Stelle einen Timer auf zehn Sekunden" and wait: gong, "Der Timer über
  10 Sekunden ist abgelaufen.", then the gong every 3 seconds and the ring
  pulsing. Home Assistant receives `esphome.aivi_timer` with
  `phase: finished`. Say the wake word: the alarm stops (`phase: stopped`),
  and no command starts.
- Let a timer ring out: after 2 minutes the gong stops (`phase: silenced`),
  and the ring pulses slowly until the next wake word.
- With the pipeline `AIVI Cloud`, the same sentences reach the LLM, and its
  request lists the tool `intent__HassStartTimer`.

### 9. Later updates

Updates go over the network with encrypted OTA:

```bash
make sat-flash SAT=<room> DEVICE=<ip-address>
```

### 10. Replace a board

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
at `-inf` (digital silence) and "Hey AIVI" does nothing. It is the regular
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
4. `Diagnose GPIO9 hochohmig`, then a reboot of the XVF3800, rules out the
   ESP32 loading the MCLK line. GPIO44 carries the audio output to the
   XVF3800 and stays with I2S.

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
