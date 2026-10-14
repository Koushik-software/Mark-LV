"""Tests for core/macos.py.

The parsing functions are pure — they take command output as text — so the
macOS logic is fully testable on Linux, where this suite runs. The execution
wrappers are exercised only for their off-macOS behaviour (returning None / a
sentinel rather than raising).
"""
from __future__ import annotations

import sys

from core import macos


# ── battery (pmset -g batt) ──────────────────────────────────────────────────

PMSET_BATT = """Now drawing from 'Battery Power'
 -InternalBattery-0 (id=4653155)\t85%; discharging; 4:12 remaining present: true
"""

PMSET_CHARGING = """Now drawing from 'AC Power'
 -InternalBattery-0 (id=4653155)\t100%; charged; 0:00 remaining present: true
"""


def test_parse_battery_percent_and_state():
    b = macos.parse_pmset_battery(PMSET_BATT)
    assert b["percent"] == 85
    assert b["charging"] is False
    assert b["remaining"] == "4:12"
    assert b["present"] is True


def test_parse_battery_charged_means_charging():
    b = macos.parse_pmset_battery(PMSET_CHARGING)
    assert b["percent"] == 100
    assert b["charging"] is True


def test_parse_battery_empty_is_all_none():
    b = macos.parse_pmset_battery("")
    assert b == {"percent": None, "charging": None,
                 "remaining": None, "present": False}


# ── thermal (pmset -g therm) ─────────────────────────────────────────────────

PMSET_THERM = """Note: No thermal warning level has been recorded
CPU_Scheduler_Limit \t= 100
CPU_Available_CPUs \t= 4
CPU_Speed_Limit \t= 60
"""


def test_parse_therm_reads_speed_limit():
    t = macos.parse_therm(PMSET_THERM)
    assert t["CPU_Speed_Limit"] == 60
    assert t["CPU_Scheduler_Limit"] == 100


def test_parse_therm_ignores_non_numeric_and_absent():
    assert macos.parse_therm("nothing here") == {}


def test_parse_xcpm_level():
    assert macos.parse_xcpm_level("3\n") == 3
    assert macos.parse_xcpm_level("") is None
    assert macos.parse_xcpm_level("not a number") is None


# ── brightness (brightness -l) ───────────────────────────────────────────────

def test_parse_brightness_cli():
    assert macos.parse_brightness_cli("display 0: brightness 0.750000") == 75


def test_parse_brightness_clamped_and_invalid():
    assert macos.parse_brightness_cli("display 0: brightness 2.0") == 100
    assert macos.parse_brightness_cli("no brightness here") is None


# ── AppleScript builders ─────────────────────────────────────────────────────

def test_focus_script_escapes_quotes():
    """An app name is model-supplied. Stripping its quotes keeps the payload
    inside the AppleScript string literal — it cannot close the literal and run
    as a second statement."""
    hostile = 'Evil" & do shell script "rm -rf ~'
    script = macos.script_focus_app(hostile)
    assert hostile not in script            # the raw payload is not present
    assert '"' not in macos._escape_applescript(hostile)
    assert script.count('"') % 2 == 0       # literals stay balanced


def test_move_script_embeds_integers():
    s = macos.script_move_front_window(10, 20, 800, 600)
    assert "{10, 20}" in s and "{800, 600}" in s


def test_escape_applescript_strips_backslash_and_quote():
    assert macos._escape_applescript('a"b\\c') == "abc"


# ── off-macOS behaviour ──────────────────────────────────────────────────────

def test_available_matches_platform():
    assert macos.available() == (sys.platform == "darwin")


def test_off_macos_returns_none_not_raise():
    if sys.platform == "darwin":
        return
    assert macos.battery_status() == {"percent": None, "charging": None,
                                      "remaining": None, "present": False}
    assert macos.list_apps() == []
    assert macos.frontmost_app() is None
    assert macos.focus_app("Finder") is False
    assert macos.brightness_get() is None
    assert macos.system_telemetry() == {}


def test_run_never_raises_on_bad_command():
    # A command that cannot exist must come back as None, not an exception.
    assert macos._run(["/nonexistent/binary/xyz"]) is None


# ── CPU brand and load (sysctl) ──────────────────────────────────────────────

def test_parse_brand_string():
    assert macos.parse_brand_string("Intel(R) Core(TM) i5-8210Y CPU @ 1.60GHz\n") \
        == "Intel(R) Core(TM) i5-8210Y CPU @ 1.60GHz"
    assert macos.parse_brand_string("") is None
    assert macos.parse_brand_string("   ") is None


def test_parse_loadavg():
    load = macos.parse_loadavg("{ 1.52 1.44 1.38 }")
    assert load == {"load_1m": 1.52, "load_5m": 1.44, "load_15m": 1.38}


def test_parse_loadavg_malformed_is_empty():
    assert macos.parse_loadavg("") == {}
    assert macos.parse_loadavg("nonsense") == {}
    assert macos.parse_loadavg("{ 1.0 2.0 }") == {}   # fewer than three numbers


# ── volume (get/set, mute preservation) ──────────────────────────────────────

VOLUME_SETTINGS = ("output volume:50, input volume:100, "
                   "alert volume:100, output muted:false")


def test_parse_volume_settings():
    v = macos.parse_volume_settings(VOLUME_SETTINGS)
    assert v == {"output_volume": 50, "muted": False}


def test_parse_volume_settings_muted_true():
    v = macos.parse_volume_settings("output volume:0, output muted:true")
    assert v == {"output_volume": 0, "muted": True}


def test_parse_volume_settings_empty():
    assert macos.parse_volume_settings("") == {}


def test_script_set_volume_preserves_mute_by_default():
    script = macos.script_set_volume(70)
    assert "set volume output volume 70" in script
    # reads the flag first, restores it after — never a window of unwanted audio
    assert "output muted of (get volume settings)" in script
    assert script.strip().endswith("set volume output muted m")


def test_script_set_volume_without_preserve():
    assert macos.script_set_volume(70, preserve_mute=False) == \
        "set volume output volume 70"


def test_script_set_volume_clamps():
    assert "output volume 100" in macos.script_set_volume(999)
    assert "output volume 0" in macos.script_set_volume(-5)


# ── window layouts ───────────────────────────────────────────────────────────

def test_resolve_layout_names_and_aliases():
    assert macos.resolve_layout("left") == "left"
    assert macos.resolve_layout("maximize") == "full"
    assert macos.resolve_layout("Left Half") == "left"
    assert macos.resolve_layout("top-right") == "top_right"
    assert macos.resolve_layout("middle") == "center"


def test_resolve_layout_unknown_is_none():
    assert macos.resolve_layout("diagonal") is None
    assert macos.resolve_layout("") is None


def test_layout_geometry_left_half():
    assert macos.layout_geometry("left", 1440, 900) == (0, 0, 720, 900)


def test_layout_geometry_right_half():
    assert macos.layout_geometry("right", 1440, 900) == (720, 0, 720, 900)


def test_layout_geometry_full():
    assert macos.layout_geometry("full", 1440, 900) == (0, 0, 1440, 900)


def test_layout_geometry_unknown_is_none():
    assert macos.layout_geometry("sideways", 1440, 900) is None


def test_script_window_layout_reads_screen_inside_script():
    script = macos.script_window_layout("right")
    assert "bounds of window of desktop" in script
    assert "0.5" in script
    assert macos.script_window_layout("sideways") is None


def test_snap_off_macos_is_false_not_raise():
    if sys.platform == "darwin":
        return
    assert macos.snap_front_window("left") is False
    assert macos.snap_front_window("bogus") is False
    assert macos.volume_get() is None
    assert macos.cpu_brand() is None
    assert macos.load_average() == {}
