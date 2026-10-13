# Bedroom sunrise alarm

The "Lichtwecker" wakes up with the bed light: a WLED strip under the bed
rises like a sunrise, from dark through deep red, orange and yellow to
bright daylight. It is set in the section "Lichtwecker" at the top of the
bedroom wall display (see `docs/wall_display.md`).

## Behaviour

The alarm time is the time the light is fully bright. The sunrise starts
its duration earlier, 10 to 60 minutes (60 by default), so a sunrise for
00:20 starts at 23:20 the evening before.

The alarm starts when

- it is on,
- the weekday of the alarm time is selected,
- with "Nur an Arbeitstagen", the day of the alarm time is a workday of the
  Workday integration (no weekends and no public holidays),
- someone is home,
- the bed light is off. A light that is on means someone is awake.

The sunrise and what follows:

1. **Sunrise.** The WLED effect speed becomes the duration in minutes, and
   the scene "Schlafzimmer Sunrise" starts WLED's Sunrise effect at full
   brightness. The effect fills the strip from its middle with the Fire
   palette, which begins black. The scene also sets a primary colour
   without white, see "White channel" below, and the script activates it
   without a transition: WLED's default of 0.7 seconds blended from the
   last look, the full white channel, and flashed white before the dark
   start.
2. **Daylight.** At the alarm time the strip fades in 10 seconds to the bed
   light of the scene "Schlafzimmer Day": solid, white channel only, full
   brightness. The ceiling light stays as it is. The effect alone would end
   with dark edges.
3. **Off.** After 60 minutes of daylight the light fades out.

Someone who is awake takes over:

| During | Action | Result |
|---|---|---|
| Sunrise | Light off (remote, voice, app, "Stopp") | The light stays off; a WLED preset sets the effect back to solid |
| Sunrise | Any other button of the remote | Daylight at once; the light stays on until someone turns it off |
| Daylight | Any button of the remote, or light off | The light stays as it is; no automatic off |

The next "on" shows the last look, as after any scene: daylight, or, after
a stop during the sunrise, the white channel at full brightness.

## White channel

The bed strip is RGBW, and WLED's palettes have no white. WLED takes the
white of every pixel from the segment's primary colour instead
(`Segment::color_from_palette`: `palcol.w = W(color)`). The bed light's
usual white, `[0, 0, 0, 255]`, therefore lit the whole strip at full white
from the first second, with the red sun barely visible in the middle. The
scene sets the primary colour to `[255, 0, 0, 0]`: its red is not shown,
because the effect draws from the palette, and its white is 0. WLED's auto
white mode "Dual" then adds white only where the palette turns pale yellow
and white at the end.

Measured with WLED's live view on a one-minute preview (pixel colours with
white added, middle of the strip / a quarter in / the edge):

| Time | Middle | Quarter | Edge |
|---|---|---|---|
| 2 s | `260000` dark red | `130000` | black |
| 11 s | `A70000` red | `4D0000` | black |
| 20 s | `F78A24` orange | `9F0000` | black |
| 26 s | `FFE229` yellow | `B50902` | black |
| 41 s | `FFFF8F` pale yellow | `FFA529` | black |
| 57 s | `FFFF8F` | `FFFF8F` | black |
| 72 s | daylight, white channel | | |

The outermost pixels stay dark during the sunrise: the effect grows from
the middle and reaches the ends only in the daylight step.

## Entities

| Entity | Name | Purpose |
|---|---|---|
| `input_boolean.bedroom_alarm_enabled` | Lichtwecker | Alarm on or off |
| `input_datetime.bedroom_alarm_time` | Lichtwecker Weckzeit | Alarm time (time only) |
| `input_number.bedroom_alarm_sunrise_minutes` | Lichtwecker Dauer | Sunrise duration, 10 to 60 minutes in steps of 5 |
| `input_boolean.bedroom_alarm_workdays_only` | Lichtwecker nur an Arbeitstagen | Only on workdays |
| `input_boolean.bedroom_alarm_mon` … `_sun` | Lichtwecker Montag … Sonntag | Weekdays |
| `scene.bedroom_sunrise` | Schlafzimmer Sunrise | Bed light on, effect Sunrise, brightness 255 |
| `script.bedroom_sunrise` | Lichtwecker Sonnenaufgang | Sunrise, daylight, off and the hand-over to the remote |
| `automation.bedroom_sunrise_alarm` | Lichtwecker Schlafzimmer | Starts the script at the right minute |

The definitions are in `config/homeassistant/bedroom_alarm/`:
`helpers.yaml`, `scene.yaml`, `script.yaml`, `automation.yaml` and
`wled_reset_preset.json`. None of the entities is exposed to Assist, so
their names stay out of the Speech-to-Phrase vocabulary.

The alarm uses these existing entities:

- `light.bedroom_bed_wled` "Bettlicht", the WLED strip with one segment, and
  its effect speed `number.wled_geschwindigkeit`.
- `select.bedroom_bed_wled_preset`, the WLED presets.
- The IKEA RODRET remote, whose button presses arrive on the MQTT topic
  `zigbee2mqtt/bed_room_dimmer_1/action`. The automation "Bedroom RODRET
  controls WLED" switches and dims the bed light with it.
- `binary_sensor.arbeitstag` and `calendar.arbeitstag_kalender` from the
  Workday integration (workdays Monday to Friday, excluding public
  holidays).
- `zone.home` for "someone is home".

## WLED preset

The preset "Bedroom Bed Alarm Reset" (`wled_reset_preset.json`) sets the
effect back to solid and leaves the light off:

```json
{"on": false, "seg": [{"id": 0, "fx": 0, "col": [[0, 0, 0, 255]]}]}
```

Without it, the next "on" after a stop during the sunrise would show what
is left of the Sunrise effect, or the scene's red. Home Assistant can only change the effect by
turning the light on, which would flash in a dark bedroom.

WLED applies a preset that is saved through its JSON API right away, so
save it while the bed light is off. Back up the presets first:

```bash
curl -s http://<wled-ip>/presets.json -o presets_backup.json
```

```bash
curl -s -X POST -H 'Content-Type: application/json' -d '{"on":false,"seg":[{"id":0,"fx":0,"col":[[0,0,0,255]]}],"o":true,"psave":2,"n":"Bedroom Bed Alarm Reset"}' http://<wled-ip>/json/state
```

`psave` is the preset id; use a free one. In the WLED web interface the
same preset is a preset with "Use current state" off and the JSON as API
command.

The WLED integration in Home Assistant reads the presets when it connects.
WLED has no clock here, so Home Assistant does not notice a new preset:
reload the integration (Settings → Devices & services → WLED → Reload)
until `select.bedroom_bed_wled_preset` lists it.

## Deploy

Home Assistant keeps helpers, scenes, scripts and automations in its own
storage; the files in the repository are copies.

- **Helpers:** websocket `<domain>/create` with the key as the name, so the
  entity id becomes `input_boolean.bedroom_alarm_enabled` and so on, then
  `<domain>/update` with the German name. Set the first values once
  (06:30, 60 minutes, Monday to Friday, only on workdays); an `initial`
  value would replace the restored state after every restart.
- **Scene:** `POST /api/config/scene/config/bedroom_sunrise` with
  `scene.yaml` as JSON. A new scene takes its entity id from its name;
  rename `scene.schlafzimmer_sunrise` to `scene.bedroom_sunrise` once.
- **Script:** `POST /api/config/script/config/bedroom_sunrise` with
  `script.yaml`.
- **Automation:** `POST /api/config/automation/config/bedroom_sunrise_alarm`
  with `automation.yaml`. Rename the entity to
  `automation.bedroom_sunrise_alarm` once.
- **Assist:** new entities are exposed by default. Turn that off with
  `homeassistant/expose_entity` and `should_expose: false` for all of them.
- **Dashboard:** `make wall-display-deploy HA_ENV=<env-file>`.

## Testing

Without the light:

- `workday.check_date` on `binary_sensor.arbeitstag` with a date returns
  `{"binary_sensor.arbeitstag": {"workday": …}}`.
- Set the duration to 10 minutes and the alarm time to now plus 11 minutes;
  the automation triggers at the start of the next minute. With the alarm
  off, its trace stops at the first condition; with the weekday of today
  unselected, at the second. Set the helpers back afterwards.

With the light, only when nobody sleeps in the bedroom: "Vorschau" on the
display runs the script with one minute of sunrise and one minute of
daylight. Check the effect, the daylight and the fade-out, then run it again
and turn the light off during the sunrise: the light stays off, and WLED's
`/json/state` shows `"on": false`, `"fx": 0` and the white channel as
primary colour.

The pixel colours can be read without looking at the strip: WLED's
websocket (`ws://<wled-ip>/ws`) sends the live view after the message
`{"lv": true}`, as binary frames of `L`, a version byte and three bytes per
LED (RGB with the white channel added). `/json/live` is not built into this
WLED.

## Troubleshooting

| Symptom | Cause |
|---|---|
| No sunrise on a selected weekday | Check the trace of "Lichtwecker Schlafzimmer": the alarm is off, nobody is home, the bed light was on, or the day is a holiday with "Nur an Arbeitstagen" |
| The strip flashes white for about a second when the sunrise starts | The scene was activated with WLED's default transition, which blends from the last look; the script passes `transition: 0`. Activating the scene by hand still blends |
| The whole strip is bright from the start of the sunrise | The primary colour has white, so every pixel gets it (see White channel); the scene must set `rgbw_color` with white 0 |
| The sunrise shows other colours | The bed light's palette is not "Default"; with "Default" the Sunrise effect uses the Fire palette |
| The light jumps to daylight before the end | A button of the remote was pressed |
| After a stop the next "on" shows the sunrise | The preset "Bedroom Bed Alarm Reset" is missing or not listed in `select.bedroom_bed_wled_preset`; reload the WLED integration |
| "Arbeitstage werden geladen …" stays | The display could not read `calendar.arbeitstag_kalender` |
| A control of the alarm card reacts to the mouse but not to touch | It is drawn as HTML instead of a nested button-card; button-card calls `preventDefault` on `touchend`, so the phone sends no click (see `docs/wall_display.md`) |
