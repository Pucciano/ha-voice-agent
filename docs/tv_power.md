# Living room TV power

The living room TV (Sony XR-65X90K) has power only while someone watches.
Its plug also feeds the soundbar and the power supply of the motorized
Vogel's TVM 7675 wall mount. "Fernsehen" switches all of it on and off: the
tile "Fernseher" in the section "Fernsehen" of the living room wall display
(see `docs/wall_display.md`), the switch on any other dashboard, the voice
("schalte den Fernseher an"), or the button on the plug.

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

When the plug is on already, step 1 is skipped. The POWER key is sent only
after a cold start: the plug was off, or it turned on less than 150 s ago.
The button on the plug switches the plug on before the script starts, so a
check for "off" alone missed that case (2026-10-19).

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

The switch "Fernsehen" does not run the scripts itself: it fires the event
`tv_power_request` (`action: on` or `off`), and the automation "Fernsehen an
oder aus" runs them. A script started directly by a user runs with that
user's rights, and reloading the Android TV Remote integration needs an
admin; the wall display logs in as a user without admin rights. On
2026-10-19 a tap on the display therefore stopped "Fernsehen einschalten"
with "Unauthorized" after the plug had turned on, and the TV stayed in its
"on" without a picture until it settled to standby. An automation runs
without a user, so this path works for every user.

The switch is on while "Fernsehen einschalten" runs, and while the TV is on
with power, unless "Fernsehen ausschalten" runs. Within 150 s after the plug
turned on, "on" counts only with a picture (45 W or more), so the "on"
without a picture after a cold start never shows as on.

The update window is off by default. Without it the TV installs software
and app updates only while someone watches, or when they are started by
hand in the TV's settings.

## Voice

Sentence triggers of the automation "Fernsehen an oder aus" catch these
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

## Apps

"Starte Netflix" (also "öffne …", "… starten", "mach … an", optionally "ich"
in front or "auf dem Fernseher" at the end) starts Netflix or YouTube. The
automation "Fernseher App per Sprache" answers "Netflix startet." or, with
the TV off, "Der Fernseher startet mit Netflix, das dauert etwa eine
Minute.", and runs the script "Fernseher App starten". The script switches
the TV on through the event `tv_power_request` when needed, waits for
"Fernsehen einschalten" to finish, and opens the app with
`media_player.play_media` (`media_content_type: app`) and a deep link.
Disney+ and Prime Video have sentences too, but only answer "… kann ich auf
dem Fernseher noch nicht starten." until their links are checked (below).
Only these four names have sentences, so "starte einen Timer" still reaches
the built-in intents. Voxtral wrote "Startet Netflix." and "Ich starte
YouTube.", hence "startet" and the optional "ich".

The script is exposed to Assist, so the LLM assistant can call it for other
wording; its field `app` takes "Netflix" or "YouTube".

### Links and safety

A link is resolved by Android on the TV, not by Home Assistant. If more than
one app claims it, Android asks "open with" (web or app), and with an https
link a browser can open a website instead of the app. So:

- **Allowlist:** the script sends only the links in its `apps` table. A
  field selector is no check at run time; any other value of `app`, for
  example an address from the LLM, stops the run before anything reaches the
  TV (tested with "Disney+" and `https://example.com`).
- **Check after the launch:** the foreground app (`app_id` of the media
  player) must be the expected package within 8 s. Otherwise the script
  sends BACK, which closes a chooser or a browser, and a notification
  "Fernseher" names what opened instead. (This path has not run yet.)
- **Only checked links:** package ids (`market://launch?id=…`) do nothing on
  this TV; the androidtv_remote docs blame a Google Play Store change. What
  was tried on 2026-10-19:

| App | Link | Result |
|---|---|---|
| Netflix | `netflix://` | Opens Netflix within 2 s; used |
| Netflix | `com.netflix.ninja` (package id) | Nothing, home screen stays |
| YouTube | `https://www.youtube.com` | Opens YouTube directly, no chooser; used |
| YouTube | `vnd.youtube://` | System dialog (`app_id` "android"), closed with BACK |
| Disney+ | `https://www.disneyplus.com` | "Open with" web or app; closed with BACK; not used |
| Prime Video | `https://app.primevideo.com` | Not tried; not used |

Before Disney+ and Prime Video get a link, check which app resolves it on
the TV instead of trying links: with ADB debugging switched on in the TV's
developer options, `adb shell cmd package query-activities -a
android.intent.action.VIEW -d <link>` lists every app that would open the
link, and `adb shell dumpsys package <package>` shows the app's intent
filters (schemes and hosts). A link that only the app itself resolves opens
it without a chooser. Switch ADB off again afterwards.

The Android TV Remote integration has the four apps with their package ids
in its options (Settings → Devices & services → Android TV Remote →
Configure), so the media player and the wall display show their names:

| Name | App id |
|---|---|
| Netflix | `com.netflix.ninja` |
| YouTube | `com.google.android.youtube.tv` |
| Disney+ | `com.disney.disneyplus` |
| Prime Video | `com.amazon.amazonvideo.livingroom` |

For the local pipeline, `compose/jetson/custom_sentences/de/tv_apps.yaml`
teaches Speech-to-Phrase the same sentences; deploy it as described in
`docs/jetson_setup.md` (custom sentences), which needs sudo on the Jetson.

## Entities

| Entity | Name | Purpose |
|---|---|---|
| `switch.tv_power` | Fernsehen | On/off for dashboards and Assist (template helper) |
| `script.tv_power_on` | Fernsehen einschalten | Power on, boot, picture |
| `script.tv_power_off` | Fernsehen ausschalten | TV off, wait, power off |
| `script.tv_launch_app` | Fernseher App starten | TV on when needed, then start Netflix or YouTube by a checked link (exposed to Assist) |
| `automation.tv_power_app_voice` | Fernseher App per Sprache | Voice sentences for the four apps |
| `automation.tv_power_command` | Fernsehen an oder aus | Runs the scripts for the voice sentences and the event `tv_power_request` from the switch |
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
| "Starte Netflix" switches the TV on but the app does not open | The link no longer opens the app; the notification names what opened instead. Check the link on the TV (see Links and safety) before changing it |
| "Fernsehen einschalten" stops with "Unauthorized" | It was started directly by a user without admin rights; start it through the switch, the event `tv_power_request` or the voice |
| The TV shuts down while someone watches | Android TV lost the TV and the plug reads less than 40 W, or the update window ended; check the trace of the automation that ran |
| The plug stays on after the TV went off | A script was running when the TV went off, or the update window was on; the next "aus" turns it off |
| The button on the plug switches only the plug | The automation ignores changes from the app and dashboards (user context), while "Fernsehen einschalten" runs, and for a minute after the update window switched the plug |
| The TV switches on when the plug is switched from somewhere else | Another automation or script switched the plug; their runs carry no parent context, so only the TV power scripts and the update window are recognised |
| The mount stops halfway when the power goes | Raise `settle_s` in "Fernsehen ausschalten" |
