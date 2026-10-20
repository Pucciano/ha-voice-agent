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
5. The mount drives to "Couch" ([Mount](#mount)).

When the plug is on already, step 1 is skipped. The POWER key is sent only
after a cold start: the plug was off, or it turned on less than 150 s ago.
The button on the plug switches the plug on before the script starts, so a
check for "off" alone missed that case (2026-10-19).

**Fernsehen aus** (script "Fernsehen ausschalten"):

1. The mount drives to the wall; the script waits until it is there, at
   most 60 s, because the mount loses its power with the TV
   ([Mount](#mount)).
2. The TV turns off through Android TV and reaches standby within 5-14 s.
3. After 60 s the plug turns off. If the TV was switched on again
   meanwhile, the plug stays on.

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

## Positions

The mount turns the TV towards three places, or folds it to the wall:

| Say, for example | Position |
|---|---|
| "Dreh den Fernseher zur Couch", "Fernseher Richtung Sofa" | Couch |
| "Dreh den Fernseher zum Esstisch", "… zum Tisch", "… zum Esszimmer" | Esstisch |
| "Dreh den Fernseher zur Ottomane" | Ottomane |
| "Fahr den Fernseher an die Wand", "Klapp den Fernseher zurück" | Wand |
| "Wohin zeigt der Fernseher?", "Wie steht der Fernseher?" | answer only |

The verbs "dreh", "richte … aus", "schwenk", "stell" and "fahr" work, also
at the end ("den Fernseher zur Couch drehen"), with "zur", "zum", "Richtung",
"in Richtung", "auf die" and the like. Every sentence names "Fernseher" and a
place, so none catches "schalte den Fernseher aus", a light at the dining
table or a timer; checked with hassil 3.12.1, the version of Home Assistant
2026.10.

The automation "Fernseher ausrichten per Sprache" answers "Der Fernseher
dreht sich zur Couch.", "… zeigt schon zur Couch." or, with the TV off, "Der
Fernseher startet und dreht sich dann zur Couch, das dauert gut eine
Minute." It runs the script "Fernseher ausrichten", which is also exposed
to Assist for the LLM assistant: with the TV off, it starts "Fernsehen an"
through the event `tv_power_request` with the position, so the mount goes
there instead of to "Couch". A TV without power stands at the wall already,
so "Wand" then does nothing. Only the script is exposed, not the mount's own
entities, so the LLM can choose one of the four positions but not set a
distance or rotation.

The script "Fernsehhalterung fahren" (`script.tv_mount_move`, not exposed)
drives the mount for all of them. It presses the integration's preset
buttons rather than the preset select, because the mount calls one preset
"Esstisch " with a trailing space, which templates drop. Before a press it
checks that the preset name at that place still matches; a preset moved or
renamed in the Vogel's app stops it with a notification "TV-Halterung".
Speech-to-Phrase learns all of these sentences from Home Assistant (see
Deploy). Tested on 2026-10-20 with Piper recordings: all five came back word
for word in 1.2-1.4 s, "Ottomane" as "Ottoman" and "wohin" as "wo hin",
which the triggers accept as well.

## Apps

"Starte Netflix" (also "öffne …", "… starten", "mach … an", optionally "ich"
in front or "auf dem Fernseher" at the end) starts Netflix. The
automation "Fernseher App per Sprache" answers "Netflix startet." or, with
the TV off, "Der Fernseher startet mit Netflix, das dauert etwa eine
Minute.", and runs the script "Fernseher App starten". The script switches
the TV on through the event `tv_power_request` when needed, waits for
"Fernsehen einschalten" to finish, and opens the app with
`media_player.play_media` (`media_content_type: app`) and a deep link.
YouTube, Disney+ and Prime Video have sentences too, but only answer "… kann
ich auf dem Fernseher noch nicht starten." until their links are checked
(below).
Only these four names have sentences, so "starte einen Timer" still reaches
the built-in intents. Voxtral wrote "Startet Netflix." and "Ich starte
YouTube.", hence "startet" and the optional "ich".

The script is exposed to Assist, so the LLM assistant can call it for other
wording; its field `app` takes only "Netflix".

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
| Netflix | `intent:#Intent;action=android.intent.action.MAIN;category=android.intent.category.LEANBACK_LAUNCHER;package=com.netflix.ninja;end` | Nothing, home screen stays |
| Netflix | `intent://#Intent;scheme=netflix;package=com.netflix.ninja;end` | Nothing, home screen stays |
| YouTube | `https://www.youtube.com` | Opens YouTube directly, no chooser; not used, as an https link can be claimed by a browser |
| YouTube | `vnd.youtube://` | System dialog (`app_id` "android"), closed with BACK |
| Disney+ | `https://www.disneyplus.com` | "Open with" web or app; closed with BACK; not used |
| Prime Video | `https://app.primevideo.com` | Not tried; not used |

An app cannot be named the way a phone does it (an explicit intent with the
package): the TV's remote service only opens links, an implicit VIEW intent
that Android resolves by the apps' intent filters, and the `intent:` URIs
above did nothing. So the rule is: no https links; only a scheme of the app
itself, and the foreground check after the launch. Netflix's `netflix://` is
the only such link checked so far. Launching by package would need ADB
(`am start` with the package) and ADB debugging left on, which is postponed.

Before YouTube, Disney+ and Prime Video get a link, check which app resolves
it on the TV instead of trying links: with ADB debugging switched on in the TV's
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

Speech-to-Phrase learns these sentences for the local pipeline from Home
Assistant (see Deploy).

## Entities

| Entity | Name | Purpose |
|---|---|---|
| `switch.tv_power` | Fernsehen | On/off for dashboards and Assist (template helper) |
| `script.tv_power_on` | Fernsehen einschalten | Power on, boot, picture |
| `script.tv_power_off` | Fernsehen ausschalten | TV off, wait, power off |
| `script.tv_launch_app` | Fernseher App starten | TV on when needed, then start Netflix by its checked link (exposed to Assist) |
| `script.tv_mount_position` | Fernseher ausrichten | TV on when needed, then turn it to Couch, Esstisch or Ottomane or to the wall (exposed to Assist) |
| `script.tv_mount_move` | Fernsehhalterung fahren | Drives the mount by its preset buttons; used by the other scripts |
| `automation.tv_mount_voice` | Fernseher ausrichten per Sprache | Voice sentences for the positions and "Wohin zeigt der Fernseher?" |
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
supports the TVM 7675; Home Assistant reaches it through the Bluetooth proxy
on the Jetson ([Bluetooth proxy](ble_proxy.md)). The device is
"TV-Halterung" in the living room, with Home Assistant on the mount's user
PIN (control rights only).

- "Fernsehen an" moves the mount to "Couch" (`mount_view_preset`), or to the
  position "Fernseher ausrichten" asked for, once the TV shows a picture.
  After a cold start the mount boots with the TV; "Fernsehhalterung fahren"
  waits up to 90 s for it to be available. The move is started, not awaited,
  so a mount that cannot be reached never delays or fails the TV.
- "Fernsehen aus" first moves the mount to its default position "0" (flat
  at the wall) and waits until its distance is 0, at most 60 s, because the
  mount loses its power with the TV; it waits only 15 s for a mount that
  cannot be reached. Then the TV goes off, and the plug after `settle_s`.
- Measured on 2026-10-20: a preset command needs 4-6 s for the connection
  and the PIN; wall to "Esstisch" (94/-94, the farthest preset) took 32 s,
  back 25 s. The positions arrive while the mount drives, about once a
  second. Full cycle: "Fernsehen aus" from "Couch" had the mount at the wall
  after 16 s, the TV off after 18 s and the plug off after 89 s; the cold
  "Fernsehen an" showed the picture after 49 s and had the mount at "Couch"
  after 67 s. Right after it boots, the mount reports a distance of 65535
  for a moment.
- An empty `mount_view_preset` keeps the mount where it is when the TV
  starts.

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
- **Speech-to-Phrase:** restart it on the Jetson
  (`docker compose restart speech-to-phrase` in `/opt/aivi/compose`, no
  sudo). At every training it reads the names exposed to Assist and the
  sentence triggers of all automations (`conversation/sentences/list`), so
  it learns "Fernsehen" and the sentences for apps and positions without
  files of its own.
- **Assist for the mount:** expose `script.tv_mount_position` only; the
  mount's own entities and `script.tv_mount_move` stay unexposed.

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
| The plug stays on after the TV went off | A script or the update window delays auto-off; after it ends, five eligible standby minutes start again. Missing or stale power measurements also keep the plug on |
| The button on the plug switches only the plug | The automation ignores changes from the app and dashboards (user context), while "Fernsehen einschalten" runs, and for a minute after the update window switched the plug |
| The TV switches on when the plug is switched from somewhere else | Another automation or script switched the plug; their runs carry no parent context, so only the TV power scripts and the update window are recognised |
| The mount stops halfway when the power goes | Raise `settle_s` in "Fernsehen ausschalten" |

### Shutdown guards

Auto-off counts five minutes with no power script or update window active.
If a blocker ends while the TV remains off, the countdown starts again.
Both automatic shutdown paths require a numeric, nonnegative power reading
reported less than 30 seconds ago. The shutdown script checks again after
its final MQTT request; invalid or stale data leaves the plug powered and
creates `tv_power_off_failed`. A successful shutdown clears that notification.

Offline regression tests (Jinja templates with controlled state snapshots):
`uvx --python 3.12 --with pyyaml==6.0.3 --with jinja2==3.1.6 --from pytest==8.3.5 pytest tests/homeassistant`
These tests do not replace a Home Assistant trace or hardware test.

After a script config API write, wait for `script.reload` to complete before
starting a test: saving the file can return before the scheduled reload has
finished. Android TV reloads use `entry_id: "{{ config_entry_id(tv) }}"`;
reloading by entity target is deprecated and will be removed in HA 2027.4.

Live verification on 2026-10-20: backed up and deployed the shutdown script,
auto-off and update-window automations. A real shutdown reached standby
then cut the plug after about 77 seconds. After over 60 seconds without
power, the normal `tv_power_request` event started the TV in about 50 seconds.
Netflix was reopened; the user confirmed normal picture and soundbar audio.
Reload by `entry_id` was tested against the real Android TV integration.
Isolated copies in the HA engine confirmed that unknown, unavailable,
negative and stale readings prevent the final power cut, while a fresh
standby reading allows it. With a shortened countdown, both script blockers
and the update-window blocker prevented shutdown and rearmed it on release.
The copies replaced hardware actions with marker events. Config API readback
matched the deployed source and the HA configuration check passed.

The unchanged five-minute countdown was also verified in the HA engine:
after releasing a simulated update window, no early trigger occurred and
the marker fired at five minutes (observed after 300.84 seconds). All
temporary automations, scripts, states and test notifications were removed.
