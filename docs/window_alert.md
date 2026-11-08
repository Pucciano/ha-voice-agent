# Window alert

The heating often stays on while a window is open. The radiator thermostats
are not in Home Assistant yet, so for now the voice assistant reminds us
(issue #34). The configuration lives in `config/homeassistant/window_alert/`.

## Behaviour

**Announcement (`window_alert_announcement`).** A window contact or the
balcony door has stayed open for "Fensterhinweis Wartezeit" (default
5 minutes). If at that moment

- somebody is at home (`zone.home` above 0),
- the outdoor temperature is at least "Fensterhinweis Temperaturdifferenz"
  (default 2 °C) below the room's temperature, and
- at least one AIVI satellite is available,

every available satellite says once:

> Im Wohnzimmer ist das linke Fenster seit 5 Minuten offen. Draußen sind es
> 8 Grad, drinnen 18. Denk an die Heizung.

The trigger fires once per opening, when the delay has passed. If the
condition does not hold at that moment (warm outside, nobody at home),
nothing is said for this opening, even if it gets colder later. Closing and
opening the window starts a new delay.

- **Outdoor temperature:** the `temperature` attribute of `weather.dwd`
  (measured DWD values), with `weather.forecast_zuhause` (met.no) as fallback.
  If neither is a number, or the room sensor is unknown, nothing is said.
- **Satellites:** every ESPHome media player whose entity id ends in
  `_aivi_satellit_audioausgang` and that has a state. A new satellite with
  that naming takes part without a change. Through the jack, "AIVI
  Klinkenausgang" switches the room speaker to its analog input, as for the
  timer announcement. The voice is Piper `de_DE-thorsten-medium`, as in the
  satellites' pipelines.

**When leaving (`window_alert_leaving`).** When `zone.home` drops to 0 and a
window or the balcony door is open, the person whose state changed last gets
a push notification on their iPhone, whatever the temperature:

> **Fenster offen** — Noch offen: Wohnzimmerfenster links, Badfenster.

The apartment door is not part of either automation.

## Rooms

The room table `rooms` in `automation.yaml` maps each area to its spoken
place, its temperature sensor and its contacts with their spoken names. The
trigger lists the same contacts; `tests/homeassistant/test_window_alert.py`
checks that both agree.

| Area | Contacts | Temperature |
|---|---|---|
| living_room | window left, center, right, balcony door | `sensor.living_room_climate_temperatur` |
| child_room | window left, right | `sensor.kinderzimmer_child_room_climate_temperatur` |
| bed_room | window left, right | `sensor.bed_room_climate_temperature` |
| office | window | `sensor.timmerflotte_temp_hmd_sensor_temperatur` |
| bathroom | window | `sensor.bathroom_climate_temperature` |

To add a contact, add it to the trigger and to its room. To add a person,
add a placeholder to `phones` in `window_alert_leaving` and the real ids to the local phones file.

## Entities

| Entity | Name | Purpose |
|---|---|---|
| `input_number.window_alert_delta` | Fensterhinweis Temperaturdifferenz | Outdoor at least this far below the room (°C, 0.5–10) |
| `input_number.window_alert_delay` | Fensterhinweis Wartezeit | Minutes a window stays open before the announcement (1–30) |
| `automation.window_alert_announcement` | Fensterhinweis Ansage | Announcement on the satellites |
| `automation.window_alert_leaving` | Fensterhinweis beim Verlassen | Push notification when the last person leaves |

None of them is exposed to Assist. The delay is read when a window opens,
so a change applies to the next opening.

## Deploy

Home Assistant keeps helpers and automations in its own storage; the files
in the repository are copies.

- **Helpers:** websocket `input_number/create` with the key as the name and
  the settings from `helpers.yaml`, then `input_number/update` with the
  German name. Set the first values once: 2 and 5.
- **Automations:** `POST /api/config/automation/config/<id>` for each entry
  of `automation.yaml` (the YAML anchor of `rooms` is resolved when it is
  converted to JSON). Rename each entity to `automation.<id>` once.
- **Phones:** person and notify ids carry our names, so `phones` in
  `window_alert_leaving` holds placeholders (`person.<person-1>`,
  `notify.mobile_app_<person-1-phone>`). The deploy replaces them with the
  real ids from a local file outside the repository,
  `~/.config/aivi/window_alert_phones.json`, which maps each placeholder
  person to its real person id and notify action
  (`{"person.<person-1>": ["person.…", "notify.…"]}`).
  Compare a read-back config with the repository after the same
  replacement.
- **Assist:** new entities are exposed by default; unexpose the helpers and
  the automations with `homeassistant/expose_entity`.

## Testing

- `uvx --with pyyaml --with jinja2 pytest tests/homeassistant` renders the
  templates with controlled states: delta, fallbacks, nobody at home,
  satellite discovery, message text, open contacts and the last person.
- Live: set the delay to 1 minute, open a window while it is colder
  outside, and check the announcement and the trace. Set it back to 5.
- Leaving: open a window and run `automation.trigger` on
  `automation.window_alert_leaving`. The run does not read trigger data, so
  it sends the notification to whoever changed state last.

## Later: radiator thermostats

When the thermostats are in Home Assistant, each room in `rooms` gets its
`climate` entity. A new automation then turns a room's heating off when one
of its contacts has been open for a short time, and restores the previous
mode when all of the room's contacts are closed. The announcement can stay
as a reminder for rooms without a thermostat.

## Live verification on 2026-11-07

- Bathroom window opened at 15:20:12. The automation triggered at
  15:25:12; all three conditions held (19.8 °C inside, 7.6 °C outside per
  DWD, two people at home). The living room satellite said "Im Bad ist das
  Fenster seit 5 Minuten offen. Draußen sind es 8 Grad, drinnen 20. Denk an
  die Heizung." and it was heard.
- The window was closed before the leaving test, so the leaving automation
  itself was not run. Its notify action was checked by sending the same
  title and message format to one iPhone by hand; who counts as the last
  person and the list of open contacts are covered by the template tests.
