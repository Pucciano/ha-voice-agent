"""Evaluate the shipped Jinja templates with controlled HA state snapshots.

Run with pytest, PyYAML and Jinja2. This does not simulate HA scheduling.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import math

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config/homeassistant/tv_power"
AUTOMATIONS = yaml.safe_load((CONFIG / "automation.yaml").read_text())
OFF = yaml.safe_load((CONFIG / "script.yaml").read_text())["tv_power_off"]
NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)
POWER = "sensor.living_room_lowboard_tv_plug_power"
TV = "media_player.sony_xr_65x90k_2"
PLUG = "switch.living_room_lowboard_tv_plug"


def is_number(value):
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


class States:
    def __init__(self, power="28", age=2, **overrides):
        self.values = {POWER: power, TV: "off", PLUG: "on", **overrides}
        self.reported = NOW - timedelta(seconds=age)

    def __call__(self, entity):
        return self.values.get(entity, "off")

    def __getitem__(self, entity):
        return SimpleNamespace(state=self(entity), last_reported=self.reported)

    @property
    def sensor(self):
        return SimpleNamespace(living_room_lowboard_tv_plug_power=self[POWER])


def render(template, states):
    env = Environment(undefined=StrictUndefined)
    return env.from_string(template).render(
        states=states, is_state=lambda e, s: states(e) == s,
        is_number=is_number, now=lambda: NOW, timedelta=timedelta,
        power=POWER, tv=TV, standby_w=35, power_max_age_s=30,
        trigger=SimpleNamespace(id="end"),
    ).strip() == "True"


def automation(name):
    return next(a for a in AUTOMATIONS if a["id"] == name)


def auto_off(states):
    return render(automation("tv_power_auto_off")["triggers"][0]["value_template"], states)


def invalid_measurement(states):
    step = next(s for s in OFF["sequence"] if s.get("then", [{}])[0].get("action") == "persistent_notification.create")
    return render(step["if"], states)


@pytest.mark.parametrize("value", ["unknown", "unavailable", "garbage", "nan", "inf", "-1"])
def test_bad_measurement_never_allows_power_cut(value):
    states = States(value, **{TV: "unavailable"})
    assert not auto_off(states)
    assert invalid_measurement(states)
    end = automation("tv_power_update_window")["actions"][0]["choose"][1]["conditions"]
    assert not render(end, states)


@pytest.mark.parametrize("age", [30, 120])
def test_stale_standby_never_allows_power_cut(age):
    states = States(age=age)
    assert not auto_off(states)
    assert invalid_measurement(states)


def test_fresh_standby_allows_shutdown_but_picture_does_not():
    assert auto_off(States())
    assert not invalid_measurement(States())
    assert not auto_off(States("70"))
    assert not auto_off(States(**{TV: "on"}))


@pytest.mark.parametrize("blocker", ["script.tv_power_on", "script.tv_power_off", "window"])
def test_releasing_inhibitor_creates_new_trigger_edge(blocker):
    states = States()
    if blocker == "window":
        states.values.update({"input_boolean.tv_update_window_enabled": "on", "schedule.tv_update_window": "on"})
    else:
        states.values[blocker] = "on"
    assert not auto_off(states)
    states.values["schedule.tv_update_window" if blocker == "window" else blocker] = "off"
    assert auto_off(states)
    assert automation("tv_power_auto_off")["triggers"][0]["for"] == "00:05:00"


def test_invalid_measurement_aborts_before_switch_off():
    sequence = OFF["sequence"]
    guard = next(i for i, s in enumerate(sequence) if "if" in s and isinstance(s["if"], str) and "is_number" in s["if"])
    cut = next(i for i, s in enumerate(sequence) if s.get("action") == "switch.turn_off")
    assert guard < cut
    assert sequence[guard]["then"][-1]["error"] is True
