# Living room TV power

The living room TV (Sony XR-65X90K) has power only while someone watches.
Its plug also feeds the soundbar and the power supply of the motorized
Vogel's TVM 7675 wall mount. "Fernsehen" switches all of it on and off: the
switch on a dashboard, the voice ("schalte den Fernseher an"), or the button
on the plug.

## Why

Since April 2026 the TV never reaches deep standby when it is off. It stays
awake with a dark screen at about 26 W (the soundbar adds about 2-6 W), a
daily mean of 22 W or about 190 kWh a year. Until February 2026 it drew
less than 1 W in standby. Switching off Music Assistant's Chromecast
provider and the Android TV Remote integration for 25 minutes each did not
let it sleep, so the cause stays open; cutting the power avoids it.

## Behaviour

**Fernsehen an** (script "Fernsehen einschalten"):

1. The plug turns on. The TV boots and is on the network after 32-36 s.
2. After 25 s the script reloads the Android TV Remote integration every few
   seconds until it reaches the TV. On its own the integration retries only
   every 30 s.
3. After a cold start the TV reports "on" without a picture for about 110 s
   and ignores "turn on". The script sends the POWER key once, which drops
   the TV into standby at once, and then turns it on. The picture appears
   45-54 s after the plug turned on.
4. If the TV is not reachable or shows no picture after 3 minutes, a
   notification "Fernseher" appears in Home Assistant.

When the plug is on already, step 1 and the POWER key are skipped.

**Fernsehen aus** (script "Fernsehen ausschalten"):

1. The TV turns off through Android TV and reaches standby within 5-14 s.
2. After 60 s the plug turns off. The wait leaves the mount time to drive
   back to the wall, because it loses its power with the TV. If the TV was
   switched on again meanwhile, the plug stays on.

**Automatic:**

| When | Then |
|---|---|
| The TV is off for 5 minutes with power (its own remote, Android TV) | "Fernsehen aus" |
| Someone presses the button on the plug and it turns on | "Fernsehen an" |
| The update window is on and its schedule starts | The plug turns on; the TV can update in standby |
| The update window ends and nobody watches | "Fernsehen aus" |

The TV's own remote cannot switch on a TV without power. Use the voice, the
switch "Fernsehen", or the button on the plug.

The update window is off by default. Without it the TV installs software
and app updates only while someone watches, or when they are started by
hand in the TV's settings.

## Voice

Sentence triggers of the automation "Fernsehen per Sprache" catch these
sentences before the built-in intents:

- "[schalte|schalt|mach] [den] Fernseher (an|ein)"
- "[schalte|schalt|mach] [das] Fernsehen (an|ein)"
- the same with "aus"

The answer is "Der Fernseher startet, das dauert knapp eine Minute.", "Der
Fernseher ist schon an." or "Der Fernseher wird ausgeschaltet." The media
player keeps the name "Fernseher" and stays exposed, so pause, volume and
the like still go to it. The switch "Fernsehen" is exposed for the LLM
assistant and other wording; the plug "Steckdose Fernseher" is not exposed,
so nobody cuts the power of a running TV by voice.

## Entities

| Entity | Name | Purpose |
|---|---|---|
| `switch.tv_power` | Fernsehen | On/off for dashboards and Assist (template helper) |
| `script.tv_power_on` | Fernsehen einschalten | Power on, boot, picture |
| `script.tv_power_off` | Fernsehen ausschalten | TV off, wait, power off |
| `automation.tv_power_voice` | Fernsehen per Sprache | Voice sentences |
| `automation.tv_power_auto_off` | Fernseher nach dem Ausschalten stromlos schalten | Power off 5 minutes after the TV went off |
| `automation.tv_power_plug_button` | Fernseher über die Steckdosentaste einschalten | Button on the plug |
| `automation.tv_power_update_window` | Fernseher Updatefenster | Weekly update window |
| `input_boolean.tv_update_window_enabled` | Fernseher Updatefenster | Update window on or off (off by default) |
| `schedule.tv_update_window` | Fernseher Updatefenster Zeit | Time of the window, Sunday 03:00-05:00 |

The definitions are in `config/homeassistant/tv_power/`: `helpers.yaml`,
`script.yaml` and `automation.yaml`.

They use these existing entities:

- `switch.living_room_lowboard_tv_plug` "Steckdose Fernseher" with
  `sensor.living_room_lowboard_tv_plug_power`, an IKEA INSPELNING on
  Zigbee2MQTT. The scripts ask Zigbee2MQTT for fresh readings on
  `zigbee2mqtt/living_room_lowboard_tv_plug/get`.
- `media_player.sony_xr_65x90k_2` "Fernseher" and `remote.sony_xr_65x90k`
  from the Android TV Remote integration.

## Measurements

Measured on 2026-10-18 with the plug off for at least 60 s before each
start (TV, soundbar and mount on the plug):

| Step | After the plug turned on |
|---|---|
| TV on the network (port 6466) | 32-36 s |
| Android TV Remote connected, on its own | 44-84 s |
| Android TV Remote connected, with reloads | 35-46 s |
| Picture, waiting for the TV to settle to "off" | 114-116 s |
| Picture, POWER key then turn on | 45-54 s |

- The first power reading after the plug turns on can be the inrush current
  (141.6 W once), so the scripts never take a single early reading as a
  picture.
- The picture draws 68-74 W at the home screen; boot and standby draw
  40 W at most.
- `KEYCODE_WAKEUP` does nothing on this TV.
- After "turn off" the plug reads about 28 W within 5-14 s and stays flat.

## Mount

The TVM 7675 is controlled over Bluetooth only. The official MotionMount
integration supports only the TVM 7675 Pro over Ethernet. The custom
integration [vogels_motion_mount_ble](https://github.com/Nailik/vogels_motion_mount_ble)
supports the TVM 7675, but needs Bluetooth in Home Assistant, which has
none yet: an ESPHome Bluetooth proxy with active connections near the TV
would do.

Both scripts have the steps already. Set `mount_select` to the preset
select entity of the mount, and check `mount_view_preset` ("Fernsehen"),
`mount_wall_preset` ("Wand") and `mount_travel_s` (45 s). "Fernsehen aus"
then moves the mount to the wall before it turns the TV off. Until then the
60 s wait (`settle_s`) leaves the mount time to drive back on its own; raise
it if the mount is not at the wall when the power goes.

## Deploy

Home Assistant keeps helpers, scripts and automations in its own storage;
the files in the repository are copies.

- **Helpers:** websocket `input_boolean/create` and `schedule/create` with
  the key as the name, then `<domain>/update` with the German name.
- **Template switch:** config flow `template`, step `switch`, with the name
  `tv_power`, the state template and the actions from `helpers.yaml`. Then
  set the name "Fernsehen", the icon and the area Wohnzimmer in the entity
  registry.
- **Scripts:** `POST /api/config/script/config/<key>` for `tv_power_on` and
  `tv_power_off` with their part of `script.yaml`.
- **Automations:** `POST /api/config/automation/config/<id>` for each entry
  of `automation.yaml`. Rename each entity to `automation.<id>` once.
- **Assist:** new entities are exposed by default. Expose only
  `switch.tv_power`, and unexpose the plug.
- **Speech-to-Phrase:** restart it on the Jetson to learn "Fernsehen"
  (`docker compose restart speech-to-phrase` in `/opt/aivi/compose`).

## Testing

- `conversation/agent/homeassistant/debug` with the sentences above reports
  `"source": "trigger"`.
- `conversation/process` with "schalte den Fernseher an" and the agent
  `conversation.home_assistant` starts the TV; the picture follows within a
  minute.
- `media_player.turn_off` on the TV: after 5 minutes and the 60 s wait the
  plug turns off.
- Press the button on the plug while it is off: the TV switches on.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Notification "nicht erreichbar" | The TV did not come up on the network; check the plug and the TV's network connection |
| Notification "ließ sich nicht einschalten" | The POWER key and turn on had no effect within 3 minutes; check the trace of "Fernsehen einschalten" |
| The TV shuts down while someone watches | Android TV lost the TV and the plug reads less than 40 W, or the update window ended; check the trace of the automation that ran |
| The plug stays on after the TV went off | A script was running when the TV went off, or the update window was on; the next "aus" turns it off |
| The button on the plug switches only the plug | The automation ignores changes from the app and dashboards (user context), while "Fernsehen einschalten" runs, and for a minute after the update window switched the plug |
| The TV switches on when the plug is switched from somewhere else | Another automation or script switched the plug; their runs carry no parent context, so only the TV power scripts and the update window are recognised |
| The mount stops halfway when the power goes | Raise `settle_s` in "Fernsehen ausschalten" |
