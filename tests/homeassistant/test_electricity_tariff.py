"""Tariff slots: the state templates of the work price and base rate helpers.

Run with pytest and Jinja2. `now()` is local time (Europe/Berlin) as in Home
Assistant; `match` is Home Assistant's regex test.
"""
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]
HELPERS = ROOT / "config/homeassistant/helpers"
WORK = (HELPERS / "electricity_work_price.jinja").read_text()
BASE = (HELPERS / "electricity_base_rate.jinja").read_text()
TARIFF = (HELPERS / "electricity_tariff.jinja").read_text()
TZ = ZoneInfo("Europe/Berlin")


def slots(*items):
    """States for up to three slots of (valid_from, ct/kWh, EUR/month)."""
    values = {}
    for i, (start, work, base) in enumerate(items, 1):
        values[f"input_datetime.electricity_tariff_{i}_valid_from"] = start
        values[f"input_number.electricity_tariff_{i}_work_price"] = str(work)
        values[f"input_number.electricity_tariff_{i}_base_price"] = str(base)
    return values


def render(template, values, now):
    env = Environment(undefined=StrictUndefined)
    env.tests["match"] = lambda value, pattern: re.match(pattern, str(value)) is not None
    out = env.from_string(template).render(
        states=lambda entity: values.get(entity, "unknown"),
        now=lambda: now,
        timedelta=timedelta,
        none=None,
    ).strip()
    return None if out == "None" else float(out)


TARIFFS = slots(("2026-11-01", 30, 12.5), ("2027-01-01", 31, 13.0), ("1970-01-01", 0, 0))


@pytest.mark.parametrize(
    ("now", "price"),
    [
        (datetime(2026, 11, 7, 13, 0, tzinfo=TZ), 0.30),
        (datetime(2026, 12, 31, 23, 59, 59, tzinfo=TZ), 0.30),
        (datetime(2027, 1, 1, 0, 0, tzinfo=TZ), 0.31),
        (datetime(2027, 6, 1, 12, 0, tzinfo=TZ), 0.31),
    ],
)
def test_work_price_follows_the_slot_valid_on_the_local_date(now, price):
    assert render(WORK, TARIFFS, now) == pytest.approx(price)


def test_slot_order_does_not_matter():
    values = slots(("2027-01-01", 31, 0), ("1970-01-01", 0, 0), ("2026-11-01", 30, 0))

    assert render(WORK, values, datetime(2026, 12, 1, tzinfo=TZ)) == pytest.approx(0.30)
    assert render(WORK, values, datetime(2027, 1, 2, tzinfo=TZ)) == pytest.approx(0.31)


def test_no_valid_slot_gives_no_price():
    future_only = slots(("2027-01-01", 31, 13.0))
    unused = slots(("2026-01-01", 0, 12.5))
    broken = slots(("unknown", 30, 12.5))
    now = datetime(2026, 11, 7, tzinfo=TZ)

    assert render(WORK, future_only, now) is None
    assert render(WORK, unused, now) is None
    assert render(WORK, broken, now) is None
    assert render(BASE, future_only, now) is None


def test_fractional_work_price():
    values = slots(("2026-11-01", 31.87, 0))

    assert render(WORK, values, datetime(2026, 11, 7, tzinfo=TZ)) == pytest.approx(0.3187)


@pytest.mark.parametrize(
    ("now", "base", "hours"),
    [
        (datetime(2026, 11, 7, 13, tzinfo=TZ), 12.5, 30 * 24),
        (datetime(2026, 12, 15, tzinfo=TZ), 12.5, 31 * 24),
        (datetime(2027, 2, 10, tzinfo=TZ), 13.0, 28 * 24),
    ],
)
def test_base_rate_spreads_the_monthly_price_over_the_month(now, base, hours):
    assert render(BASE, TARIFFS, now) == pytest.approx(base / hours, abs=1e-6)


def render_text(values, now):
    env = Environment(undefined=StrictUndefined)
    env.tests["match"] = lambda value, pattern: re.match(pattern, str(value)) is not None
    out = env.from_string(TARIFF).render(
        states=lambda entity: values.get(entity, "unknown"), now=lambda: now, none=None,
    ).strip()
    return None if out == "None" else out


def named(values, slot, provider, product):
    return {
        **values,
        f"input_text.electricity_tariff_{slot}_provider": provider,
        f"input_text.electricity_tariff_{slot}_product": product,
    }


def test_tariff_names_follow_the_active_slot():
    values = named(named(TARIFFS, 1, "Stadtwerke", "Basis"), 2, "Stadtwerke", "Öko 24")

    assert render_text(values, datetime(2026, 12, 31, 23, 59, tzinfo=TZ)) == "Stadtwerke · Basis"
    assert render_text(values, datetime(2027, 1, 1, 0, 0, tzinfo=TZ)) == "Stadtwerke · Öko 24"


def test_tariff_names_fall_back():
    now = datetime(2026, 11, 7, tzinfo=TZ)

    assert render_text(named(TARIFFS, 1, " Stadtwerke ", ""), now) == "Stadtwerke"
    assert render_text(named(TARIFFS, 1, "", ""), now) == "Tarif 1"
    assert render_text(TARIFFS, now) == "Tarif 1"  # helpers missing: unknown
    assert render_text(slots(("2027-01-01", 31, 0)), now) is None


def test_base_rate_without_base_price_is_zero():
    values = slots(("2026-11-01", 30, 0))

    assert render(BASE, values, datetime(2026, 11, 7, tzinfo=TZ)) == 0
