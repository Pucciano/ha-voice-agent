"""Evaluate the window alert templates (issue #34) with controlled HA states.

Run with pytest, PyYAML and Jinja2. Variables are rendered in order and
parsed into native types like Home Assistant does; this does not simulate
the state trigger's delay.
"""
from ast import literal_eval
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import math
import re

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]
AUTOMATIONS = yaml.safe_load((ROOT / "config/homeassistant/window_alert/automation.yaml").read_text())
NOW = datetime(2026, 11, 7, 18, tzinfo=timezone.utc)
SATELLITE = "media_player.wohnung_wohnzimmer_aivi_satellit_audioausgang"
LEFT = "binary_sensor.living_room_window_left_contact"


def automation(name):
    return next(a for a in AUTOMATIONS if a["id"] == name)


def is_number(value):
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def native(text):
    try:
        return literal_eval(text)
    except (ValueError, SyntaxError):
        return text


class Home:
    def __init__(self, **overrides):
        self.values = {
            "zone.home": "2",
            "input_number.window_alert_delta": "2.0",
            "input_number.window_alert_delay": "5.0",
            "sensor.living_room_climate_temperatur": "20.0",
            SATELLITE: "idle",
            **overrides,
        }
        self.attributes = {("weather.dwd", "temperature"): 16.0,
                           ("weather.forecast_zuhause", "temperature"): 15.5}
        self.changed = {}

    def states(self, entity):
        return self.values.get(entity, "unknown")

    def __getitem__(self, entity):
        if entity not in self.changed:
            return None
        return SimpleNamespace(last_changed=self.changed[entity])

    def state_attr(self, entity, attribute):
        if attribute == "friendly_name":
            return {LEFT: "Wohnzimmerfenster links",
                    "binary_sensor.living_room_balcony_door_contact": "Balkontür",
                    "binary_sensor.bathroom_window_contact": "Badfenster"}.get(entity)
        return self.attributes.get((entity, attribute))

    def environment(self):
        env = Environment(undefined=StrictUndefined)
        env.globals.update(
            states=self, is_number=is_number, state_attr=self.state_attr,
            is_state=lambda e, s: self.states(e) == s,
            as_timestamp=lambda value, default=None: value.timestamp(),
            integration_entities=lambda domain: [SATELLITE, "media_player.other_player",
                                                 "media_player.kitchen_aivi_satellit_audioausgang"],
        )
        env.filters["float"] = lambda v, d=None: float(v) if is_number(v) else d
        env.filters["int"] = lambda v, d=0: int(float(v)) if is_number(v) else d
        env.tests["match"] = lambda v, pattern: re.match(pattern, v) is not None
        env.tests["has_value"] = lambda e: self.states(e) not in ("unknown", "unavailable")
        return env

    def __call__(self, entity):
        return self.states(entity)


def run(name, home, trigger=None):
    """Render the variables, then return (variables, conditions met)."""
    config = automation(name)
    env = home.environment()
    scope = {"trigger": trigger or SimpleNamespace()}
    for key, value in config["variables"].items():
        scope[key] = native(env.from_string(value).render(scope).strip()) if isinstance(value, str) else value
    met = all(env.from_string(c["value_template"]).render(scope).strip() == "True"
              for c in config["conditions"])
    if met:
        for step in config["actions"]:
            for key, value in step.get("variables", {}).items():
                scope[key] = native(env.from_string(value).render(scope).strip())
    return scope, met


def announce(home, contact=LEFT):
    return run("window_alert_announcement", home, SimpleNamespace(entity_id=contact))


def test_trigger_and_room_table_list_the_same_contacts():
    config = automation("window_alert_announcement")
    table = {c for r in config["variables"]["rooms"].values() for c in r["contacts"]}
    assert set(config["triggers"][0]["entity_id"]) == table
    assert len(table) == 10
    assert automation("window_alert_leaving")["variables"]["rooms"] == config["variables"]["rooms"]


def test_colder_outside_announces_window_and_temperatures():
    scope, met = announce(Home())
    assert met
    assert scope["message"] == ("Im Wohnzimmer ist das linke Fenster seit 5 Minuten offen. "
                                "Draußen sind es 16 Grad, drinnen 20. Denk an die Heizung.")


@pytest.mark.parametrize("indoor, met", [("18.0", True), ("17.9", False), ("12.0", False)])
def test_delta_is_inclusive(indoor, met):
    assert announce(Home(**{"sensor.living_room_climate_temperatur": indoor}))[1] is met


def test_delta_helper_is_used():
    home = Home(**{"input_number.window_alert_delta": "5.0"})
    assert not announce(home)[1]


@pytest.mark.parametrize("value", ["unknown", "unavailable", "nan"])
def test_missing_room_temperature_says_nothing(value):
    assert not announce(Home(**{"sensor.living_room_climate_temperatur": value}))[1]


def test_met_no_is_the_fallback_and_both_missing_say_nothing():
    home = Home()
    home.attributes[("weather.dwd", "temperature")] = None
    scope, met = announce(home)
    assert met and scope["outdoor"] == 15.5
    home.attributes[("weather.forecast_zuhause", "temperature")] = None
    assert not announce(home)[1]


def test_nobody_home_says_nothing():
    assert not announce(Home(**{"zone.home": "0"}))[1]


def test_unknown_contact_says_nothing():
    assert not announce(Home(), "binary_sensor.corridor_apartment_door_contact")[1]


def test_frost_one_minute_and_balcony_door():
    home = Home(**{"input_number.window_alert_delay": "1.0"})
    home.attributes[("weather.dwd", "temperature")] = -3.4
    scope, met = announce(home, "binary_sensor.living_room_balcony_door_contact")
    assert met
    assert scope["message"].startswith("Im Wohnzimmer ist die Balkontür seit einer Minute offen. "
                                       "Draußen sind es minus 3 Grad")


def test_only_available_satellites_get_the_announcement():
    home = Home(**{"media_player.kitchen_aivi_satellit_audioausgang": "idle"})
    assert announce(home)[0]["players"] == [SATELLITE, "media_player.kitchen_aivi_satellit_audioausgang"]
    home = Home(**{SATELLITE: "unavailable"})
    scope, met = announce(home)
    assert scope["players"] == [] and not met


def leave(home):
    first, second = automation("window_alert_leaving")["variables"]["phones"]
    home.changed = {first: NOW - timedelta(minutes=30), second: NOW}
    return run("window_alert_leaving", home)


def test_leaving_lists_open_contacts_for_the_last_person():
    home = Home(**{LEFT: "on", "binary_sensor.bathroom_window_contact": "on"})
    scope, met = leave(home)
    assert met
    assert scope["last_person"] == "person.<person-2>"
    assert scope["open_contacts"] == ["Wohnzimmerfenster links", "Badfenster"]


def test_leaving_ignores_the_temperature_and_closed_windows():
    home = Home(**{LEFT: "on", "sensor.living_room_climate_temperatur": "10.0"})
    assert leave(home)[1]
    assert not leave(Home())[1]
