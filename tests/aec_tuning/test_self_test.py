"""Echo canceller self test engine against a simulated room.

room_simulator.cpp drives esphome/components/aec_tuning/analysis.h like the
aec_tuning component does on the satellite; it is compiled once per session
with the host's C++ compiler.
"""

import json
import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
ENGINE_DIR = REPO / "esphome" / "components" / "aec_tuning"
SIMULATOR = pathlib.Path(__file__).with_name("room_simulator.cpp")

# Warning bits of analysis.h.
DELAY_UNSTABLE = 1 << 0
DELAY_CLAMPED = 1 << 1
CLIPPING = 1 << 2
ECHO_LOUD = 1 << 3
REVERB_LONG = 1 << 4
LOW_ERLE = 1 << 7
REF_CLIPPED = 1 << 8

# The simulator's XVF3800 starts with the values of formatBCE's firmware.
SYS_DELAY_BEFORE = -30
REF_GAIN_BEFORE = 8.0
# A full-scale signal should reach -2 dBFS after REF_GAIN.
TARGET_GAIN = 10 ** (-2 / 20)


@pytest.fixture(scope="session")
def simulator(tmp_path_factory) -> pathlib.Path:
    compiler = shutil.which("c++")
    if compiler is None:
        pytest.skip("no C++ compiler")
    binary = tmp_path_factory.mktemp("aec_tuning") / "room_simulator"
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            f"-I{ENGINE_DIR}",
            str(SIMULATOR),
            "-o",
            str(binary),
        ],
        check=True,
    )
    return binary


def run(simulator: pathlib.Path, *args: object) -> dict:
    result = subprocess.run(
        [str(simulator), *map(str, args)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return json.loads(result.stdout)


def test_bluesound_like_room(simulator) -> None:
    # 55 ms echo delay as behind the Bluesound's analog input.
    r = run(simulator, "--delay", 880, "--noise-dbfs", -80)
    assert r["step"] == "done" and r["error"] == "none"
    assert r["valid_bursts"] == 3
    assert abs(r["delay_samples"] - 880) <= 2
    assert r["delay_spread"] <= 2
    # More delay than SYS_DELAY can take: the maximum, and a warning.
    assert r["sys_delay_new"] == 256 and r["final_sys_delay"] == 256
    assert r["warnings"] & DELAY_CLAMPED
    assert r["residual_ms"] == pytest.approx((880 - 256) / 16, abs=0.2)
    assert r["usable_tail_ms"] == pytest.approx(192 - (880 - 256) / 16, abs=0.2)
    # The simulated reference path has no gain of its own.
    assert r["ref_gain_old"] == pytest.approx(REF_GAIN_BEFORE)
    assert r["ref_gain_new"] == pytest.approx(TARGET_GAIN, rel=0.03)
    assert r["final_ref_gain"] == pytest.approx(r["ref_gain_new"])
    assert r["echo_to_ref_db"] < -6
    assert not r["warnings"] & (CLIPPING | ECHO_LOUD | LOW_ERLE)
    assert r["erle_db"] == pytest.approx(20, abs=1.0)
    assert r["converged"]
    assert not r["restored"]


@pytest.mark.parametrize(
    ("delay", "sys_delay"), [(100, 76), (300, 256), (10, -14)]
)
def test_sys_delay_keeps_24_samples_of_margin(simulator, delay, sys_delay) -> None:
    r = run(simulator, "--delay", delay)
    assert r["step"] == "done"
    assert abs(r["delay_samples"] - delay) <= 2
    assert r["sys_delay_new"] == pytest.approx(sys_delay, abs=2)
    assert bool(r["warnings"] & DELAY_CLAMPED) == (delay - 24 > 256)


@pytest.mark.parametrize(
    ("path_db", "before", "expected"),
    [
        # An attenuated reference path needs more gain.
        (-12, 8.0, TARGET_GAIN * 10 ** (12 / 20)),
        # Less than 1 dB off: the gain stays.
        (0, 0.8, 0.8),
        # More than the maximum: capped at 8.
        (-24, 1.0, 8.0),
    ],
)
def test_reference_gain(simulator, path_db, before, expected) -> None:
    r = run(simulator, "--ref-path-db", path_db, "--ref-gain", before)
    assert r["step"] == "done"
    assert r["ref_gain_new"] == pytest.approx(expected, rel=0.03)
    assert r["final_ref_gain"] == pytest.approx(r["ref_gain_new"])


def test_clipping_reference_keeps_the_gain(simulator) -> None:
    # +10 dB in the path: the -6 dBFS tones clip even at REF_GAIN 1.
    r = run(simulator, "--ref-path-db", 10)
    assert r["step"] == "done"
    assert r["ref_clipped"]
    assert r["warnings"] & REF_CLIPPED
    assert r["ref_gain_new"] == pytest.approx(REF_GAIN_BEFORE)


@pytest.mark.parametrize("rt60", [250, 400, 600, 800])
def test_reverberation_time(simulator, rt60) -> None:
    r = run(simulator, "--rt60", rt60, "--noise-dbfs", -80)
    assert r["step"] == "done"
    assert r["rt60_ms"] == pytest.approx(rt60, rel=0.15)
    expected_coverage = min(60, 60 * r["usable_tail_ms"] / r["rt60_ms"])
    assert r["tail_coverage_db"] == pytest.approx(expected_coverage, rel=0.01)
    assert bool(r["warnings"] & REVERB_LONG) == (expected_coverage < 20)


def test_reverberation_time_with_room_noise(simulator) -> None:
    r = run(simulator, "--rt60", 400, "--noise-dbfs", -60)
    assert r["rt60_ms"] == pytest.approx(400, rel=0.3)
    # Bands too close to the floor report no time instead of a wrong one.
    for band in r["bands"]:
        if band["rt_range_db"] == 0:
            assert band["rt60_ms"] is None


def test_echo_attenuation(simulator) -> None:
    good = run(simulator, "--erle", 20, "--noise-dbfs", -80)
    assert good["erle_db"] == pytest.approx(20, abs=1.0)
    for band in good["bands"]:
        assert band["erle_db"] == pytest.approx(20, abs=1.5)
    weak = run(simulator, "--erle", 5)
    assert weak["step"] == "done"
    assert weak["erle_db"] == pytest.approx(5, abs=1.0)
    assert weak["warnings"] & LOW_ERLE


def test_speaker_switch_time(simulator) -> None:
    # The room speaker plays the jack only 2.5 s after the first reference.
    r = run(simulator, "--switch-ms", 2500)
    assert r["step"] == "done"
    assert r["switch_ms"] == pytest.approx(2500, abs=30)


def test_unstable_delay_keeps_sys_delay(simulator) -> None:
    r = run(simulator, "--unstable")
    assert r["step"] == "done"
    assert r["delay_spread"] >= 150
    assert r["warnings"] & DELAY_UNSTABLE
    assert r["sys_delay_new"] == SYS_DELAY_BEFORE
    assert r["final_sys_delay"] == SYS_DELAY_BEFORE


def test_loud_echo(simulator) -> None:
    r = run(simulator, "--echo-db", 6)
    assert r["step"] == "done"
    assert r["clipped"]
    assert r["warnings"] & CLIPPING and r["warnings"] & ECHO_LOUD


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (("--no-reference",), "no_reference"),
        (("--no-echo",), "no_echo"),
        (("--echo-db", -40), "no_echo"),
        (("--abort-ms", 5000), "aborted"),
    ],
)
def test_failures_restore_the_settings(simulator, args, error) -> None:
    r = run(simulator, *args)
    assert r["step"] == "failed"
    assert r["error"] == error
    assert r["restored"]
    assert r["final_sys_delay"] == SYS_DELAY_BEFORE
    assert r["final_ref_gain"] == pytest.approx(REF_GAIN_BEFORE)
