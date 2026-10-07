# Living room wall display

The living room has a Shelly Wall Display X2i, mounted in portrait. It shows
the Home Assistant dashboard "Bedienfeld" (`dashboard-living-room-wall-display`).
The dashboard is versioned in
`config/homeassistant/dashboards/living_room_wall_display.yaml`.

## Layout

From top to bottom:

| Section | Content |
|---|---|
| Header | Clock, date, the initial of every person entity with a home or away badge, one summary line (lights on, open windows and doors, running appliances) |
| Safety | Red banner per wet leak sensor; amber pill when a leak sensor is offline; green "Alles trocken" otherwise |
| Weather | Current condition, today's high and low, four days with temperature bars and a dot for the current temperature |
| Licht · Wohnzimmer | Vitrine, Pendel, Stehlampe; "Alle N aus" turns off every counted light in the flat after a confirmation |
| Musik | Active Bluesound player, play controls, volume, room chips to join or leave the group |
| Raumklima | Temperature, humidity and window state per room, with the time a window was opened |
| Geräte | Washing machine and dryer, while one of them runs or draws more than 5 W |

The palette follows `sun.sun`: light (`scandi_beige` colours) by day, dark
while the sun is below the horizon.

## Display settings

The usable area is 464 × 904 CSS px: the panel has 720 × 1440 px at 240 dpi,
and the Shelly firmware keeps a small frame around the web view.

On the display, under **Settings → Network → Home Assistant**:

- **Untere Leiste ausblenden** (hide bottom bar) is on. A swipe up from the
  bottom edge brings the Shelly bar back.
- The display logs in as the HA user "Shelly Wall Display" (username
  `shelly`, no admin rights, local only).

Tested with firmware 2.7.0. Firmware 2.6.0 marked the built-in Home Assistant
page as deprecated in favour of an app from the Shelly app store, and 2.7.0
reverted that. Keep using the built-in page: the bottom bar setting only
applies there.

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
- `sensor.time` from the Time & Date integration drives the clock.
- The two font files in `/config/www/fonts/` (see Fonts).

## Deploy

Open the dashboard, then **Edit → three-dot menu → Raw configuration editor**,
replace everything with the YAML file and save. Home Assistant stores the
dashboard without comments, so the repository file is the source. The display
picks up the change within a few seconds without a reload.

All JavaScript lives in the `button_card_templates`. The sections in `views`
contain only entity ids and labels.

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
| "Vorhersage wird geladen …" | The daily forecast arrives through a websocket subscription; it shows within a minute |
| Summary counts too many lights | A group member lacks the label `light_count_exclude` |
| Music card controls the wrong speaker | Check `sensor.active_media_player` in the developer tools |
| A room is missing in the climate section | The area lacks the label `wall_display_climate` or a temperature sensor in its settings |
| Geräte section disappears while the machine runs | The running flag is off; check the two laundry automations and `input_boolean.washer_running` |
| Light tiles stay pale while the light is on | A template variable named `on`, `off`, `yes` or `no` turns into a boolean key when the YAML is converted; use names like `is_on` |
| System fonts instead of Figtree and Newsreader | The font files are missing in `/config/www/fonts/`; `/local/fonts/figtree-latin.woff2` must load in a browser |
