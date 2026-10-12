# Wall displays

Two Home Assistant dashboards share one design:

| Dashboard | Device | Room file |
|---|---|---|
| "Bedienfeld" (`dashboard-living-room-wall-display`) | Shelly Wall Display X2i in the living room, portrait | `living_room_wall_display.yaml` |
| "Schlafzimmer" (`dashboard-bedroom-wall-display`) | Tablet or phone in the bedroom, portrait or landscape | `bedroom_wall_display.yaml` |

## Files

The files live in `config/homeassistant/dashboards/`:

- `wall_display_templates.yaml` holds every button-card template: palette,
  fonts, sections and tiles. It is the only place for colours and design.
- A room file holds the title, kiosk-mode settings, the views with their
  entity ids, and the template `wd_room` with the room settings.
  `home_area` is the area listed first in the climate section.

A room file may add templates, but it must not replace a shared one. Home
Assistant cannot include files in a storage dashboard, so
`scripts/wall_display_deploy.py` merges the shared file into each room file
and saves the result (see Deploy).

## Layout

The root card holds two stacks of sections, `main` and `side`. They sit one
below the other, and side by side on a landscape screen at least 900 CSS px
wide. The Shelly display (464 px) therefore shows one column.

The living room, from top to bottom:

| Section | Content |
|---|---|
| Header | Clock, date, the initial of every person entity with a home or away badge, one summary line (lights on, open windows and doors, running appliances) |
| Safety | Red banner per wet leak sensor; amber pill when a leak sensor is offline; green "Alles trocken" otherwise |
| Weather | Current condition, today's high and low, today and the next two days with chance of precipitation (from 20 %), temperature bars and a dot for the current temperature; below them the active and announced DWD weather warnings |
| Licht · Wohnzimmer | Vitrine, Pendel, Stehlampe; "Alle N aus" turns off every counted light in the flat after a confirmation |
| Musik | Active Bluesound player, play controls, volume, room chips to join or leave the group |
| Raumklima | Temperature, humidity and window state per room, with the time a window was opened |
| Geräte | Washing machine and dryer, while one of them runs or draws more than 5 W |

The bedroom has the same header, weather, music, climate and appliance
sections. In between:

| Section | Content |
|---|---|
| Licht · Schlafzimmer | Ceiling light and bed (the WLED master, which switches every segment); "Alle N aus" as in the living room. The wardrobe lights have no tile: they stay on and follow their own sensors |
| Lichtszenen | Love, Day, Night, Ocean, Sunset and Reading in two rows of three; a tap activates the scene, a long press opens its details |

The climate section lists the bedroom first. `main` holds the header,
weather, lights and scenes; `side` holds music, climate and appliances.

The palette follows `sun.sun`: light (`scandi_beige` colours) by day, dark
while the sun is below the horizon. The view theme `scandi_beige` styles the
Home Assistant dialogs, for example the details of a light.

## Living room display settings

The usable area is 464 × 904 CSS px: the panel has 720 × 1440 px at 240 dpi,
and the Shelly firmware keeps a small frame around the web view.

On the display, under **Settings → Network → Home Assistant**:

- **Untere Leiste ausblenden** (hide bottom bar) is on. A swipe up from the
  bottom edge brings the Shelly bar back.
- The display logs in as the HA user "Shelly Wall Display" (username
  `shelly`, no admin rights, local only).

In the HA profile of that user, **Dashboard** is set to "Bedienfeld". The
frontend picks the start dashboard from the user setting first, then from
the system-wide default (Settings → Dashboards), then from the default stored
in the browser. A per-device choice therefore loses against the system-wide
default as soon as one is set, and the display starts with "Übersicht" after
a reboot.

Tested with firmware 2.7.0. Firmware 2.6.0 marked the built-in Home Assistant
page as deprecated in favour of an app from the Shelly app store, and 2.7.0
reverted that. Keep using the built-in page: the bottom bar setting only
applies there.

## Bedroom device

The tablet or phone opens the dashboard with kiosk-mode's query string,
which hides the header and the sidebar:

```text
http://<home-assistant>:8123/dashboard-bedroom-wall-display/home?kiosk
```

`?kiosk&cache` stores this on the device, so it also applies after
navigating, but then on every dashboard. The bedroom room file sets no
kiosk-mode user, so the dashboard looks normal on other devices. If the
device logs in with a user of its own, set **Dashboard** in that user's
profile to "Schlafzimmer".

## Home Assistant prerequisites

- HACS frontend cards: `button-card` (tested 7.0.1) and `kiosk-mode` (tested
  14.2.1).
- kiosk-mode compares `users` with the **display name** of the HA user, not
  with the username. With the username, the header and sidebar stay visible.
- Label `all_windows` on every window and door contact that counts as an
  opening. The summary and the climate section read it. A window contact
  needs the device class `window`; a name ending in "links", "mitte" or
  "rechts" shows as "Fenster links" and so on. Doors show with their name.
- Label `wall_display_climate` on every area that gets a row in the climate
  section, with its temperature and humidity sensor set in the area settings.
  The rows follow the area order of Home Assistant (Settings → Areas, labels &
  zones), with the living room first. The dashboard therefore contains no room
  names or room entities.
- Label `light_count_exclude` on lights that are part of a lamp or group that
  is already counted: the eight pendant bulbs, the corridor spots 1, 2, 4 and
  5, the two office lamp bulbs and the WLED master of the bed light. Without
  it the summary counts bulbs instead of lamps. Give new group members this
  label too.
- Template helper "Active media player" (Settings → Devices & services →
  Helpers → Template → Sensor). Its state template is
  `config/homeassistant/helpers/active_media_player.jinja`. The entity is
  `sensor.active_media_player` with the display name "Aktiver Lautsprecher",
  and it is not exposed to Assist, so it stays out of the Speech-to-Phrase
  vocabulary.
- `input_boolean.washer_running` and `input_boolean.dryer_running`, set by
  the automations "Waschmaschine: läuft erkannt" and "Waschmaschine: fertig"
  (and the dryer pair): on after 2 minutes above 10 W, off after 5 minutes
  below 4 W. A washing machine drops to about 3 W for seconds to minutes
  during a programme, so the Geräte section and the appliance tiles follow
  these flags instead of the plug power.
- Scenes `scene.bedroom_love`, `scene.bedroom_day`, `scene.bedroom_night`,
  `scene.bedroom_ocean`, `scene.bedroom_sunset` and `scene.bedroom_reading`
  for the bedroom scene tiles. A tile without its entity shows "Nicht angelegt" and does nothing.
  A tile also takes a script; it then runs `script.turn_on`.
- `sensor.time` from the Time & Date integration drives the clock.
- Weather from the Deutscher Wetterdienst, see Weather sources.
- The two font files in `/config/www/fonts/` (see Fonts).

## Weather sources

The weather card reads two integrations:

- **DWD Weather** (HACS, `FL550/dwd_weather`, tested 5.2.0) with the MOSMIX
  forecast of the nearest DWD station that has measurements. Its setup lists
  the stations by distance; a mark in front of the name means measured values
  are available. Pick the data source "mixed": current values are measured,
  the forecast comes from MOSMIX. Name the station `DWD`, so the entity is
  `weather.dwd`. The daily forecast includes the chance of precipitation, and
  the condition of a day summarises all its hours (rain when more than a fifth
  of them have rain), not the weather at noon as with met.no.
- **DWD weather warnings** (core integration `dwd_weather_warnings`) with the
  warncell id of the municipality from the DWD list `cap_warncellids_csv`
  (ids starting with 8). Municipality cells are more precise than district
  cells (starting with 1). Rename the two sensors to
  `sensor.dwd_warning_level` (warnings in effect) and
  `sensor.dwd_advance_warning_level` (announced warnings).

The card shows up to two warnings, highest level first, with the DWD colours:
yellow and orange on an amber background, red (severe) and violet (extreme)
filled. The entities are not exposed to Assist; the voice assistant keeps
using the met.no entity `weather.forecast_zuhause`.

## Deploy

`scripts/wall_display_deploy.py` builds each dashboard from the shared
templates and its room file and saves it over the Home Assistant websocket
API. It needs `HA_URL` and `HA_TOKEN` (a long-lived access token of an admin
user), from the environment or from the file in `HA_ENV` (default `.env`,
see `.env.example`).

```bash
make wall-display-check
```

```bash
make wall-display-diff HA_ENV=<path-to-env-file>
```

```bash
make wall-display-deploy HA_ENV=<path-to-env-file>
```

- `check` works offline. It stops on invalid YAML, a duplicate key, a
  template that is missing or inherits from itself, and on a room file that
  replaces a shared template. The YAML is read with the booleans of YAML
  1.2, as in the Home Assistant frontend: `on`, `off`, `yes` and `no` stay
  strings.
- `diff` shows how Home Assistant differs from the repository.
- `deploy` shows the same diff and asks before saving (`YES=1` skips the
  question). It saves only dashboards that differ, so a second run changes
  nothing. Before saving it backs up every wall display to
  `~/.config/aivi/backups/wall_display_<url_path>_<time>.json`
  (`--backup-dir` to change), then reads each saved dashboard back and
  compares it.
- `restore <backup>` saves a backup again, after backing up the current
  state:

```bash
uv run --no-project --with pyyaml==6.0.3 --with websockets==16.0 python scripts/wall_display_deploy.py restore <backup-file> --env-file <path-to-env-file>
```

Changes made in the raw configuration editor are overwritten by the next
deploy; `diff` shows them first. A new wall display needs a storage
dashboard in Home Assistant (Settings → Dashboards → Add dashboard → "New
dashboard from scratch") and an entry in `DASHBOARDS` in the script.

The display picks up a saved dashboard after the page reloads.

All JavaScript lives in the shared templates. The views contain only entity
ids and labels.

The climate section has eight row slots; an area beyond the eighth needs
another slot in `wd_climate_card`.

## Fonts

The dashboard uses Figtree and Newsreader (SIL Open Font License), as
variable fonts with the latin subset, which covers German. Home Assistant
serves them from `/config/www/fonts/`, so the display needs no internet:

| File | Source |
|---|---|
| `figtree-latin.woff2` | `https://fonts.gstatic.com/s/figtree/v9/_Xms-HUzqDCFdgfMm4S9DaRvzig.woff2` |
| `newsreader-latin.woff2` | `https://fonts.gstatic.com/s/newsreader/v26/cY9AfjOCX1hbuyalUrK4397yjIJFJpc.woff2` |

Copy them there with any file access to the config folder. Without one, add
the Downloader integration with the folder `www`, call
`downloader.download_file` with `subdir: fonts` and the file name, and remove
the integration again; its action stays registered until the next restart.

The root card writes the `@font-face` rules into the page itself, because
they do not work inside the cards' shadow DOM. A dashboard resource would
load them on every dashboard.

## Checking on the device

The display runs Android 11 and can be inspected over ADB when it is enabled
on the device:

```bash
adb connect <display-ip>:5555
```

```bash
adb shell screencap -p /sdcard/s.png
```

```bash
adb pull /sdcard/s.png
```

`adb shell uiautomator dump` lists the texts on screen with their
coordinates, which helps before sending `adb shell input tap`. Taps on the
dashboard switch real devices, so check the coordinates first. Turn ADB off
again afterwards and set a password for the device's RPC interface in the
Shelly app; both are open to the whole network otherwise.

## Troubleshooting

| Symptom | Cause |
|---|---|
| HA header and sidebar visible | kiosk-mode `users` does not match the display name of the logged-in user |
| Display starts with "Übersicht" after a reboot | The profile of the display user has **Dashboard** on "Auto"; set it to "Bedienfeld" |
| "Button-card template 'wd_room' is missing!" | The room file lacks `wd_room`; `make wall-display-check` reports it before a deploy |
| Deploy stops with "must not replace shared ones" | A room file defines a template with the name of a shared one; rename it or change the shared template |
| Header and sidebar visible on the bedroom device | The URL lacks `?kiosk` |
| Scene tile shows "Nicht angelegt" | The scene entity does not exist yet or has another entity id |
| Volume follows a drag on the slider, but the speaker does not change | The Shelly app takes over horizontal swipes and cancels the touch, so no `change` event fires; the slider therefore also sends on `touchend` and `touchcancel` |
| "Vorhersage wird geladen …" | The daily forecast arrives through a websocket subscription; it shows within a minute. If it stays, check that `weather.dwd` exists and has a forecast |
| Summary counts too many lights | A group member lacks the label `light_count_exclude` |
| Music card controls the wrong speaker | Check `sensor.active_media_player` in the developer tools |
| A room is missing in the climate section | The area lacks the label `wall_display_climate` or a temperature sensor in its settings |
| Geräte section disappears while the machine runs | The running flag is off; check the two laundry automations and `input_boolean.washer_running` |
| Light tiles stay pale while the light is on | A template variable named `on`, `off`, `yes` or `no` turns into a boolean key when the YAML is converted; use names like `is_on` |
| System fonts instead of Figtree and Newsreader | The font files are missing in `/config/www/fonts/`; `/local/fonts/figtree-latin.woff2` must load in a browser |
