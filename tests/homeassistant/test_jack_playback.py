"""Exercise the deployed resume guard with manual and automatic state events."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]
CONFIG = yaml.safe_load((ROOT / "config/homeassistant/aivi_jack_playback.yaml").read_text())
GUARD = CONFIG["actions"][0]["choose"][1]["conditions"][1]["value_template"]
NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)


def allowed(pause, resume, last=0, parent=None, queued_delay=0):
    trigger = SimpleNamespace(
        from_state=SimpleNamespace(last_changed=NOW + timedelta(seconds=pause)),
        to_state=SimpleNamespace(last_changed=NOW + timedelta(seconds=resume),
                                 context=SimpleNamespace(parent_id=parent)),
    )
    return Environment(undefined=StrictUndefined).from_string(GUARD).render(
        trigger=trigger, jack_automation="automation.aivi_klinkenausgang",
        state_attr=lambda *_: None if last is None else NOW + timedelta(seconds=last),
        now=lambda: NOW + timedelta(seconds=resume + queued_delay),
        timedelta=timedelta,
    ).strip() == "True"


def test_manual_pause_after_music_start_can_resume_immediately():
    assert allowed(pause=2, resume=3)


def test_delayed_contextless_restart_does_not_resume_music():
    assert not allowed(pause=-60, resume=3)


def test_queue_delay_does_not_bypass_restart_guard():
    assert not allowed(pause=-60, resume=3, queued_delay=30)


@pytest.mark.parametrize("pause", [-60, 2])
def test_automation_context_never_resumes_music(pause):
    assert not allowed(pause=pause, resume=3, parent="automation-context")


def test_manual_resume_without_recent_automation():
    assert allowed(pause=-60, resume=20)
    assert allowed(pause=-60, resume=3, last=None)
