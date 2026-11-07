"""Energy section of the living room display: the pure JavaScript functions.

The functions between the energy-lib markers in the template `wd_energy_base`
run in Node with the time zone of the display (Europe/Berlin). Skipped when
Node is not installed.
"""

import json
import os
import pathlib
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
ROOM = REPO / "config/homeassistant/dashboards/living_room_wall_display.yaml"
NODE = shutil.which("node")
TZ = ZoneInfo("Europe/Berlin")
HOUR = 3_600_000

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _lib_source():
    data = yaml.safe_load(ROOM.read_text(encoding="utf-8"))
    code = data["button_card_templates"]["wd_energy_base"]["variables"]["store"]["value"]
    match = re.search(r"// energy-lib:begin\n(.*)// energy-lib:end", code, re.S)
    assert match, "energy-lib markers missing"
    return match.group(1)


LIB = _lib_source() if NODE else ""


def run(expression, **inputs):
    """Evaluate `expression` with `lib` and the given JSON inputs in scope."""
    script = (
        LIB
        + "const input = JSON.parse(process.argv[1]);\n"
        + "const { "
        + ", ".join(inputs)
        + " } = input;\n"
        + f"process.stdout.write(JSON.stringify({expression}));\n"
    )
    env = dict(os.environ, TZ="Europe/Berlin")
    out = subprocess.run(
        [NODE, "-e", script, json.dumps(inputs)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return json.loads(out.stdout)


def ms(*args):
    return int(datetime(*args, tzinfo=TZ).timestamp() * 1000)


def hour_rows(start, count, kwh=0.25, meter=1000.0, skip=()):
    """Hourly statistic rows from `start` (ms) with a constant change."""
    rows = []
    state = meter
    for i in range(count):
        state += kwh
        if i in skip:
            continue
        begin = start + i * HOUR
        rows.append({"start": begin, "end": begin + HOUR, "change": kwh, "state": round(state, 3)})
    return rows


# ------------------------------------------------------------------ today --
def test_today_adds_closed_hours_and_the_live_delta():
    now = ms(2026, 11, 20, 11, 30)
    start = ms(2026, 11, 18, 22)  # long before yesterday: day fully covered
    rows = hour_rows(start, 37)  # up to 11:00 today
    meter = rows[-1]["state"] + 0.1

    t = run("lib.today(rows, meter, now, dataStart)", rows=rows, meter=meter, now=now, dataStart=start)

    assert t["closed"] == pytest.approx(11 * 0.25)
    assert t["total"] == pytest.approx(11 * 0.25 + 0.1)
    assert t["complete"] and not t["partial"] and not t["reset"]
    assert [b["status"] for b in t["bars"][10:13]] == ["done", "current", "future"]


def test_first_day_is_partial_and_has_no_comparison():
    now = ms(2026, 11, 7, 11, 30)
    first = ms(2026, 11, 7, 0)
    rows = hour_rows(first, 11)

    t = run("lib.today(rows, 0, now, dataStart)", rows=rows, now=now, dataStart=first)

    assert t["partial"] and not t["complete"]
    assert t["bars"][0]["partial"] and not t["bars"][1]["partial"]
    assert t["compare"] is None
    assert t["meterOk"]  # 0 is a valid number; the live delta just stays 0
    assert t["live"] == 0 and t["reset"]


def test_meter_reset_never_gives_negative_energy():
    now = ms(2026, 11, 20, 2, 30)
    start = ms(2026, 11, 18, 0)
    rows = hour_rows(start, 50)
    rows[-1]["change"] = -900.0

    t = run("lib.today(rows, 3, now, dataStart)", rows=rows, now=now, dataStart=start)

    assert t["reset"]
    assert all((b["value"] or 0) >= 0 for b in t["bars"])


def test_unavailable_meter_keeps_the_closed_hours():
    now = ms(2026, 11, 20, 3, 30)
    start = ms(2026, 11, 18, 0)
    rows = hour_rows(start, 51)

    t = run("lib.today(rows, null, now, dataStart)", rows=rows, now=now, dataStart=start)

    assert not t["meterOk"]
    assert t["total"] == pytest.approx(3 * 0.25)


def test_missing_hour_is_a_gap_not_zero():
    now = ms(2026, 11, 20, 6, 30)
    start = ms(2026, 11, 20, 0)
    rows = hour_rows(start - 48 * HOUR, 54, skip={48 + 3})

    t = run("lib.today(rows, null, now, dataStart)", rows=rows, now=now, dataStart=start - 48 * HOUR)

    assert t["bars"][3]["status"] == "gap" and t["bars"][3]["value"] is None
    assert t["gaps"] == 1 and not t["complete"]


def test_before_the_first_statistic_is_none():
    now = ms(2026, 11, 7, 11, 30)
    first = ms(2026, 11, 7, 5)

    t = run("lib.today(rows, null, now, dataStart)", rows=hour_rows(first, 6), now=now, dataStart=first)

    assert {b["status"] for b in t["bars"][:5]} == {"none"}
    assert all(b["value"] is None for b in t["bars"][:5])
    assert t["partial"]


# ---------------------------------------------------------------- compare --
def test_compare_until_the_same_clock_time_yesterday():
    now = ms(2026, 11, 20, 11, 30)
    start = ms(2026, 11, 18, 0)
    rows = hour_rows(start, 48, kwh=0.2) + hour_rows(ms(2026, 11, 20, 0), 11, kwh=0.3, meter=2000)

    c = run("lib.today(rows, null, now, dataStart).compare", rows=rows, now=now, dataStart=start)

    assert c["bars"] == 11
    assert c["cur"] == pytest.approx(3.3) and c["prev"] == pytest.approx(2.2)
    assert c["pct"] == pytest.approx(50)
    assert c["end"] == ms(2026, 11, 20, 11)


def test_compare_needs_a_complete_yesterday():
    now = ms(2026, 11, 8, 11, 30)
    first = ms(2026, 11, 7, 0)
    rows = hour_rows(first, 35)

    c = run("lib.today(rows, null, now, dataStart).compare", rows=rows, now=now, dataStart=first)

    assert c is None


def test_compare_after_the_spring_dst_change_uses_clock_time():
    # 2027-03-28 has 23 hours; 11:00 local is the 10th closed hour.
    now = ms(2027, 3, 28, 11, 30)
    day = ms(2027, 3, 28, 0)
    start = day - 48 * HOUR
    rows = hour_rows(start, 48 + 10)

    t = run("lib.today(rows, null, now, dataStart)", rows=rows, now=now, dataStart=start)

    assert len(t["bars"]) == 23
    assert t["compare"]["bars"] == 10
    # Yesterday until 11:00 local: 11 hours.
    assert t["compare"]["prev"] == pytest.approx(11 * 0.25)


# ------------------------------------------------------------ calendar --
@pytest.mark.parametrize(
    ("now", "bars"),
    [
        (ms(2027, 3, 28, 12), 23),
        (ms(2027, 10, 31, 12), 25),
        (ms(2026, 11, 20, 12), 24),
    ],
)
def test_a_day_has_23_24_or_25_hour_bars(now, bars):
    assert run("lib.slots(lib.range('day', 0, now)).length", now=now) == bars


def test_week_is_iso_monday_to_sunday():
    r = run("lib.range('week', 0, now)", now=ms(2026, 11, 8, 12))  # a Sunday

    assert r["start"] == ms(2026, 11, 2) and r["end"] == ms(2026, 11, 9)


def test_month_steps_by_calendar_months():
    now = ms(2027, 1, 31, 12)

    feb = run("lib.range('month', 1, now)", now=now)
    dec = run("lib.range('month', -1, now)", now=now)

    assert feb["start"] == ms(2027, 2, 1) and feb["end"] == ms(2027, 3, 1)
    assert dec["start"] == ms(2026, 12, 1)
    assert run("lib.slots(lib.range('month', 1, now)).length", now=now) == 28


def test_year_has_twelve_month_bars():
    assert run("lib.slots(lib.range('year', 0, now)).length", now=ms(2026, 11, 7)) == 12


def test_titles():
    now = ms(2026, 11, 7, 12)

    assert run("lib.title(lib.range('day', 0, now))", now=now) == "Heute"
    assert run("lib.title(lib.range('week', 0, now))", now=now) == "KW 45 · 2.–8. Nov"
    assert run("lib.title(lib.range('week', -1, now))", now=now) == "KW 44 · 26. Okt–1. Nov"
    assert run("lib.title(lib.range('month', 0, now))", now=now) == "November 2026"


def test_period_compare_over_the_same_number_of_days():
    now = ms(2026, 12, 10, 12)
    r = {"period": "month", "offset": 0}
    start = ms(2026, 11, 1)
    rows = []
    day = start
    while day < ms(2026, 12, 10):
        nxt = int((datetime.fromtimestamp(day / 1000, TZ) + timedelta(days=1)).timestamp() * 1000)
        rows.append({"start": day, "end": nxt, "change": 10.0 if day < ms(2026, 12, 1) else 12.0})
        day = nxt

    c = run(
        "lib.compare(lib.bars(lib.range(r.period, 0, now), rows, now, ds, 1),"
        " lib.bars(lib.range(r.period, -1, now), rows, now, ds, 0), r.period)",
        r=r, rows=rows, now=now, ds=start - HOUR,
    )

    assert c["bars"] == 9 and not c["full"]
    assert c["cur"] == pytest.approx(108) and c["prev"] == pytest.approx(90)


# -------------------------------------------------------------- figures --
def test_projection_needs_seven_complete_days():
    now = ms(2026, 11, 14, 12)
    first = ms(2026, 11, 7, 0)
    days = [
        {"start": ms(2026, 11, d), "end": ms(2026, 11, d + 1), "change": 8.0}
        for d in range(7, 14)
    ]

    p = run("lib.projection(days, now, first)", days=days, now=now, first=first)

    assert p == {"days": 6, "kwh": None}  # the first day is partial

    p = run("lib.projection(days, now, first)", days=days, now=ms(2026, 11, 15, 12), first=first - HOUR)
    assert p["days"] == 7 and p["kwh"] == pytest.approx(8 * 365)


def test_lowest_mean_and_peak():
    now = ms(2026, 11, 20, 12, 30)
    rows = [
        {"start": now - (i + 1) * HOUR, "end": now - i * HOUR, "mean": 250.0 + i, "max": 1000.0 + i}
        for i in range(24)
    ]

    assert run("lib.lowestMean(rows, now)", rows=rows, now=now) == 250
    assert run("lib.peak(rows)", rows=rows) == 1023


def test_formatting():
    assert run("lib.fmt.kwh(0.456)") == "0,46 kWh"
    assert run("lib.fmt.kwh(12.34)") == "12,3 kWh"
    assert run("lib.fmt.watts(253.4)") == "253 W"
    assert run("lib.fmt.watts(14244)") == "14,2 kW"
    assert run("lib.fmt.pct(-12.4)") == "−12 %"


# ------------------------------------------------------------ consumers --
def _grid(now, start, count=24, kwh=1.0):
    rows = hour_rows(start, count, kwh=kwh)
    return run("lib.bars(lib.range('day', -1, now), rows, now, ds, 0)", rows=rows, now=now, ds=start - HOUR)


def test_shares_and_rest():
    now = ms(2026, 11, 21, 12)
    day = ms(2026, 11, 20)
    bars = _grid(now, day)
    devices = [{"stat": "a", "name": "A", "parent": None}, {"stat": "b", "name": "B", "parent": None}]
    rows = {"a": hour_rows(day, 24, kwh=0.5), "b": hour_rows(day, 24, kwh=0.25)}

    c = run("lib.consumers(devices, rows, bars)", devices=devices, rows=rows, bars=bars)

    assert c["gridOk"] and c["grid"] == pytest.approx(24)
    assert [i["name"] for i in c["items"]] == ["A", "B"]
    assert c["items"][0]["share"] == pytest.approx(0.5)
    assert c["rest"] == pytest.approx(6) and c["mismatch"] is None


def test_device_without_full_history_has_no_share():
    now = ms(2026, 11, 21, 12)
    day = ms(2026, 11, 20)
    bars = _grid(now, day)
    devices = [{"stat": "a", "name": "A", "parent": None}, {"stat": "b", "name": "B", "parent": None}]
    rows = {"a": hour_rows(day, 24, kwh=0.5, skip={5}), "b": []}

    c = run("lib.consumers(devices, rows, bars)", devices=devices, rows=rows, bars=bars)

    by_name = {i["name"]: i for i in c["items"]}
    assert by_name["A"]["share"] is None and by_name["A"]["value"] == pytest.approx(11.5)
    assert by_name["B"]["value"] is None
    assert c["items"][-1]["name"] == "B"  # missing data sorts last, not as 0
    assert c["rest"] is None


def test_devices_above_the_grid_are_a_mismatch():
    now = ms(2026, 11, 21, 12)
    day = ms(2026, 11, 20)
    bars = _grid(now, day, kwh=0.5)
    devices = [{"stat": "a", "name": "A", "parent": None}]
    rows = {"a": hour_rows(day, 24, kwh=0.6)}

    c = run("lib.consumers(devices, rows, bars)", devices=devices, rows=rows, bars=bars)

    assert c["rest"] is None and c["mismatch"] == pytest.approx(2.4)


def test_included_devices_are_not_counted_twice():
    now = ms(2026, 11, 21, 12)
    day = ms(2026, 11, 20)
    bars = _grid(now, day)
    devices = [{"stat": "a", "name": "A", "parent": None}, {"stat": "b", "name": "B", "parent": "a"}]
    rows = {"a": hour_rows(day, 24, kwh=0.5), "b": hour_rows(day, 24, kwh=0.25)}

    c = run("lib.consumers(devices, rows, bars)", devices=devices, rows=rows, bars=bars)

    assert c["rest"] == pytest.approx(12)


def test_partial_grid_period_has_no_shares():
    now = ms(2026, 11, 7, 12)
    first = ms(2026, 11, 7, 0)
    bars = run(
        "lib.bars(lib.range('day', 0, now), rows, now, ds, 0)",
        rows=hour_rows(first, 12), now=now, ds=first,
    )
    devices = [{"stat": "a", "name": "A", "parent": None}]

    c = run(
        "lib.consumers(devices, rows, bars)",
        devices=devices, rows={"a": hour_rows(first, 12, kwh=0.1)}, bars=bars,
    )

    assert not c["gridOk"] and c["items"][0]["share"] is None and c["rest"] is None


def test_running_week_counts_the_compiled_part_of_today():
    now = ms(2026, 11, 18, 12)  # Wednesday
    start = ms(2026, 11, 1)
    rows = [
        {"start": ms(2026, 11, d), "end": ms(2026, 11, d + 1), "change": 10.0}
        for d in range(1, 19)
    ]
    bars = run(
        "lib.bars(lib.range('week', 0, now), rows, now, ds, 0.7)",
        rows=rows, now=now, ds=start - HOUR,
    )
    devices = [{"stat": "a", "name": "A", "parent": None}]
    dev = {"a": [dict(r, change=2.5) for r in rows]}

    c = run("lib.consumers(devices, rows, bars)", devices=devices, rows=dev, bars=bars)

    assert bars[2]["status"] == "current" and bars[2]["value"] == pytest.approx(10.7)
    assert c["grid"] == pytest.approx(30)  # Mon, Tue and today's compiled 10 kWh, no live delta
    assert c["items"][0]["share"] == pytest.approx(0.25)


def test_tariff_status_matches_the_helper_rule():
    slots = [
        {"slot": 1, "start": "2027-01-01", "work": 31, "base": 13},
        {"slot": 2, "start": "2026-11-01", "work": 30, "base": 0},
        {"slot": 3, "start": "1970-01-01", "work": 0, "base": 0},
    ]

    before = run("lib.tariffs(slots, '2026-12-31')", slots=slots)
    after = run("lib.tariffs(slots, '2027-01-01')", slots=slots)

    assert [(x["start"], x["status"]) for x in before] == [("2026-11-01", "active"), ("2027-01-01", "planned")]
    assert [(x["start"], x["status"]) for x in after] == [("2026-11-01", "past"), ("2027-01-01", "active")]


def test_tariff_card_shows_names_and_grid_operator():
    data = yaml.safe_load(ROOM.read_text(encoding="utf-8"))
    body = data["button_card_templates"]["wd_energy_tariffs"]["custom_fields"]["list"].strip()[3:-3]
    slots = [
        {"slot": 1, "start": "2026-11-01", "work": 30, "base": 12.5, "provider": "Stadtwerke", "product": "Basis"},
        {"slot": 2, "start": "2027-01-01", "work": 31, "base": 0, "provider": "<b>X</b>", "product": ""},
        {"slot": 3, "start": "2026-01-01", "work": 0, "base": 0, "provider": "", "product": ""},
    ]
    expression = (
        "new Function('states', 'variables', body)("
        "{'input_text.electricity_grid_operator': {state: 'Netz GmbH'}},"
        " {store: {lib, tariffs: () => lib.tariffs(slots, '2026-12-01')}})"
    )

    html = run(expression, body=body, slots=slots)

    assert "Stadtwerke · Basis" in html and "aktiv" in html
    assert "&#60;b&#62;X&#60;/b&#62;" in html and "<b>X" not in html
    assert "geplant" in html and "Grundpreis fehlt" in html
    assert "Netz GmbH" in html
    assert html.count("ct/kWh") == 2  # the unused slot is hidden


def test_empty_text_helpers_read_as_empty():
    assert run("['unknown', 'unavailable', '  ', null, ' Netz GmbH '].map(lib.text)") == [
        "", "", "", "", "Netz GmbH"
    ]


def test_every_energy_template_compiles():
    data = yaml.safe_load(ROOM.read_text(encoding="utf-8"))
    bodies = []

    def walk(value):
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
        elif isinstance(value, str) and value.strip().startswith("[[["):
            bodies.append(value.strip()[3:-3])

    for name, template in data["button_card_templates"].items():
        if name.startswith("wd_energy"):
            walk(template)

    assert len(bodies) > 20
    errors = run(
        "bodies.map((b, i) => { try { new Function('states', 'hass', 'variables', 'entity', 'user', 'html',"
        " 'helpers', b); return null; } catch (e) { return i + ': ' + e.message; } }).filter(Boolean)",
        bodies=bodies,
    )
    assert errors == []


def test_device_names():
    entities = {
        "s.tv": {"name": "Energie", "device_id": "d1"},
        "s.rack": {"name": "Rack USV", "device_id": "d2"},
        "s.none": {"name": "Energie"},
    }
    devices = {"d1": {"name": "tv_plug", "name_by_user": "Steckdose Highboard"}}

    names = run(
        "[{stat_consumption: 's.x', name: 'Fernseher'}, {stat_consumption: 's.tv'},"
        " {stat_consumption: 's.rack'}, {stat_consumption: 's.none'}]"
        ".map((dc, i) => lib.deviceName(dc, i, entities, devices))",
        entities=entities, devices=devices,
    )

    assert names == ["Fernseher", "Highboard", "Rack USV", "Gerät 4"]


def test_store_survives_renders_and_reinstalls_on_new_code():
    data = yaml.safe_load(ROOM.read_text(encoding="utf-8"))
    body = data["button_card_templates"]["wd_energy_base"]["variables"]["store"]["value"].strip()[3:-3]
    expression = (
        "(() => { globalThis.window = {};"
        " const f = new Function('states', 'hass', 'variables', body);"
        " const vars = {energy_entity: 'e', power_entity: 'p', price_entity: 'x', cost_entity: 'c',"
        " base_entity: 'b', tariff_slots: 3};"
        " const hass = {states: {}, connection: null};"
        " const a = f.call(null, {}, hass, vars); const b = f.call(null, {}, hass, vars);"
        " a.setView({period: 'month', offset: -1}); window.wdEnergy.version = 'old';"
        " const c = f.call(null, {}, hass, vars);"
        " return [a === b, c !== a, c.view.period, c.view.offset, typeof c.tariffs, c.today(Date.now()).loading === true]; })()"
    )

    assert run(expression, body=body) == [True, True, "month", -1, "function", True]


def test_chart_bars_are_tap_targets_with_a_tooltip():
    now = ms(2026, 11, 20, 11, 30)
    start = ms(2026, 11, 18, 0)
    expression = (
        "(() => { const t = lib.today(rows, null, now, ds);"
        " const plain = lib.chart(t.bars, [], 120);"
        " const picked = lib.chart(t.bars, [], 120, {pick: true, sel: 3, tip: 'TIP'});"
         " return [plain.includes('onpointer'), (picked.match(/onpointerup=\"window.wdEnergy &amp;&amp; window.wdEnergy.pick\\(\\d+\\)\"/g) || []).length,"
        " picked.includes('>TIP</div>'), picked.includes('left:14.58%;transform:translateX(-14.58%)'),"
         " picked.includes('onpointerleave=\"event.pointerType === \\'mouse\\''), (picked.match(/opacity:0.45\\b/g) || []).length,"
        " picked.includes('opacity:0.2025')]; })()"
    )

    plain_has_handlers, targets, tip, placed, leave, dimmed, running = run(
        expression, rows=hour_rows(start, 59), now=now, ds=start
    )

    assert not plain_has_handlers
    assert targets == 12  # 11 closed hours and the running one; no future hours
    assert tip and placed and leave
    assert dimmed == 10  # the other ten closed hours
    assert running  # the running hour: light (0.45) and dimmed (0.45)


def test_pick_redraws_only_on_change_and_a_new_period_clears_it():
    data = yaml.safe_load(ROOM.read_text(encoding="utf-8"))
    body = data["button_card_templates"]["wd_energy_base"]["variables"]["store"]["value"].strip()[3:-3]
    expression = (
        "(() => { globalThis.window = {}; const f = new Function('states', 'hass', 'variables', body);"
        " const s = f.call(null, {}, {states: {}, connection: null}, {tariff_slots: 3});"
        " let n = 0; s.refresh = () => { n += 1; };"
        " s.pick(4); s.pick(4); const a = s.view.sel; s.pick(null); const b = s.view.sel;"
        " s.pick(2); s.setView({period: 'week', offset: 0, sel: 2}); return [a, b, n, s.view.sel ?? null]; })()"
    )

    assert run(expression, body=body) == [4, None, 4, None]


def run_async(expression, **inputs):
    """Like run(), for an expression that yields a Promise."""
    script = (
        LIB
        + "const input = JSON.parse(process.argv[1]);\n"
        + "const { " + ", ".join(inputs) + " } = input;\n"
        + f"Promise.resolve({expression}).then((v) => process.stdout.write(JSON.stringify(v)));\n"
    )
    out = subprocess.run(
        [NODE, "-e", script, json.dumps(inputs)], capture_output=True, text=True,
        env=dict(os.environ, TZ="Europe/Berlin"), check=True,
    )
    return json.loads(out.stdout)


def test_running_cost_uses_the_entity_state_after_the_last_hour():
    data = yaml.safe_load(ROOM.read_text(encoding="utf-8"))
    body = data["button_card_templates"]["wd_energy_base"]["variables"]["store"]["value"].strip()[3:-3]
    first = ms(2026, 11, 7, 13)
    # The cost statistic starts at 13:00 with 0 €; HA priced 13:34–14:15 at
    # 14:15, so the entity already holds 0.237 € in the running hour.
    rows = [{"start": first, "end": first + HOUR, "change": 0.0, "state": 0.0, "sum": 0.0}]
    expression = (
        "(async () => { globalThis.window = {};"
        " Date.now = () => now;"
        " const callWS = async (msg) => ({ [msg.statistic_ids[0]]: rows });"
        " const hass = {states: {'sensor.cost': {state: '0.237'}, 'sensor.price': {state: '0.3'}},"
        " connection: {}, callWS};"
        " const vars = {energy_entity: 'sensor.e', power_entity: 'sensor.p', price_entity: 'sensor.price',"
        " cost_entity: 'sensor.cost', base_entity: 'sensor.base', tariff_slots: 3};"
        " const f = new Function('states', 'hass', 'variables', body);"
        " let s = f.call(null, {}, hass, vars);"
        " for (let i = 0; i < 5; i += 1) { s.cost(lib.range('day', 0, now), now); await new Promise((r) => setTimeout(r, 0)); }"
        " const k = s.cost(lib.range('day', 0, now), now);"
        " return [k.price, k.work.partial, +k.work.total.toFixed(3), k.work.bars[14].status, k.base]; })()"
    )

    price, partial, total, status, base = run_async(expression, body=body, rows=rows, now=ms(2026, 11, 7, 14, 25))

    assert price == pytest.approx(0.3)
    assert partial  # costs start within the day
    assert total == pytest.approx(0.237)
    assert status == "current"
    assert base is None  # a day shows the work price only
