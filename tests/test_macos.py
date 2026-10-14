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
