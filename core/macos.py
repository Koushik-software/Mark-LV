"""
core/macos.py — macOS integration: telemetry, brightness, window management.

Target: macOS Big Sur 11.7 on Intel. Everything here is additive — the app ran
on Windows and Linux before and those paths are untouched.

Two rules shape this module:

  * Nothing raises. A missing tool, a denied permission or an absent key returns
    None / a sentinel. Callers invoke these unconditionally and show what they
    get; a Mac that cannot report its temperature still gets a working assistant.
  * Parsing is separated from execution. The `parse_*` functions take command
    output as text, so they are testable on any machine — which matters, because
    the machine this was written on is not a Mac.

WHAT NEEDS THE USER'S PERMISSION ON macOS
    Reading the battery and the thermal/power state needs nothing. Moving or
    listing *other apps' windows* goes through System Events and needs the app
    granted Accessibility access (System Settings → Privacy & Security →
    Accessibility). Without it, osascript returns error -1743 and every window
    function here reports unavailable rather than failing loudly.
"""
from __future__ import annotations

import platform
import re
import shutil
import subprocess

IS_MACOS = platform.system() == "Darwin"

# Every osascript / pmset / sysctl call is bounded. A wedged System Events
# prompt must not freeze the assistant.
_TIMEOUT = 5.0


def available() -> bool:
    return IS_MACOS


def _run(args: list[str], timeout: float = _TIMEOUT) -> str | None:
    """Run a command and return stdout, or None on any failure. Never raises."""
    if not IS_MACOS:
        return None
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            return None
        return r.stdout
    except Exception:
        return None


def _osascript(script: str, timeout: float = _TIMEOUT) -> str | None:
    return _run(["osascript", "-e", script], timeout=timeout)


# ── Battery (pmset -g batt) ──────────────────────────────────────────────────
# Sample:
#   Now drawing from 'Battery Power'
#    -InternalBattery-0 (id=4653155)	85%; discharging; 4:12 remaining present: true

def parse_pmset_battery(text: str) -> dict:
    """Parse `pmset -g batt`. Pure — takes the command's text, returns a dict."""
    out: dict = {"percent": None, "charging": None,
                 "remaining": None, "present": False}
    if not text:
        return out

    m = re.search(r"(\d+)%", text)
    if m:
        out["percent"] = int(m.group(1))

    low = text.lower()
    if "discharging" in low:
        out["charging"] = False
    elif "charging" in low or "charged" in low or "ac attached" in low:
        out["charging"] = True

    m = re.search(r"(\d+:\d+)\s+remaining", text)
    if m:
        out["remaining"] = m.group(1)

    if "present: true" in low:
        out["present"] = True
    return out


def battery_status() -> dict:
    """Battery percentage and charge state, or an all-None dict off macOS."""
    return parse_pmset_battery(_run(["pmset", "-g", "batt"]) or "")


# ── Thermal / power (pmset -g therm, sysctl xcpm) ────────────────────────────
# Sample `pmset -g therm`:
#   CPU_Scheduler_Limit 	= 100
#   CPU_Available_CPUs 	= 4
#   CPU_Speed_Limit 	= 60
# CPU_Speed_Limit below 100 means macOS is throttling because the machine is hot.

def parse_therm(text: str) -> dict:
    """Parse `pmset -g therm`. Pure."""
    out: dict = {}
    for line in (text or "").splitlines():
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key in ("CPU_Speed_Limit", "CPU_Scheduler_Limit"):
            try:
                out[key] = int(val.strip())
            except ValueError:
                pass
    return out


def parse_xcpm_level(text: str) -> int | None:
    """Parse `sysctl -n machdep.xcpm.cpu_thermal_level`. Pure.

    Returns None when the key is absent — it is not present on every Intel Mac,
    and its absence is normal, not an error."""
    t = (text or "").strip()
    if not t:
        return None
    try:
        return int(t)
    except ValueError:
        return None


def thermal_status() -> dict:
    """Throttle limit and, where available, the CPU thermal level."""
    out = parse_therm(_run(["pmset", "-g", "therm"]) or "")
    level = parse_xcpm_level(
        _run(["sysctl", "-n", "machdep.xcpm.cpu_thermal_level"]) or "")
    if level is not None:
        out["cpu_thermal_level"] = level
    return out


# ── Brightness ───────────────────────────────────────────────────────────────
# macOS has no built-in way to READ or SET brightness from a shell. The honest
# options are:
#
#   1. The `brightness` CLI (brew install brightness) — reads and sets exactly.
#   2. AppleScript F1/F2 key codes — sets only, relative, and only when the app
#      has been granted Accessibility.
#
# So: use the CLI when present (exact), else fall back to key presses (relative),
# and say so rather than pretending a precise value was set.

def parse_brightness_cli(text: str) -> int | None:
    """Parse `brightness -l` output → 0-100. Pure.
    Sample: `display 0: brightness 0.750000`"""
    if not text:
        return None
    m = re.search(r"brightness\s+([0-9]*\.?[0-9]+)", text)
    if not m:
        return None
    try:
        return max(0, min(100, round(float(m.group(1)) * 100)))
    except ValueError:
        return None


def _has_brightness_cli() -> bool:
    return IS_MACOS and shutil.which("brightness") is not None


def brightness_get() -> int | None:
    """Current brightness 0-100, or None when it cannot be read."""
    if not _has_brightness_cli():
        return None
    return parse_brightness_cli(_run(["brightness", "-l"]) or "")


def brightness_set(value: int) -> bool:
    """Set an absolute brightness 0-100. Returns whether it was applied exactly.

    False means the value was NOT set precisely (no CLI) — the caller should not
    push an undo entry claiming a value that was never reached."""
    value = max(0, min(100, int(value)))
    if _has_brightness_cli():
        return _run(["brightness", f"{value / 100:.4f}"]) is not None
    return False


def brightness_step(direction: int) -> bool:
    """Relative nudge via the F1/F2 key codes (144 down / 145 up).

    Used only when the `brightness` CLI is absent, so the assistant can still
    respond to "brighter"/"dimmer" — just not to an exact percentage."""
    key = 144 if direction > 0 else 145
    return _osascript(
        f'tell application "System Events" to key code {key}') is not None


# ── Windows (System Events) ──────────────────────────────────────────────────
# Each builder returns the AppleScript as a string, so the script text is
# testable without a Mac; the wrapper only runs it.

def script_list_apps() -> str:
    return ('tell application "System Events" to get name of '
            '(every process whose background only is false)')


def script_frontmost_app() -> str:
    return ('tell application "System Events" to get name of '
            '(first process whose frontmost is true)')


def script_focus_app(name: str) -> str:
    safe = _escape_applescript(name)
    return (f'tell application "System Events" to set frontmost of '
            f'(first process whose name is "{safe}") to true')


def script_move_front_window(x: int, y: int, w: int, h: int) -> str:
    return (
        'tell application "System Events"\n'
        '  tell (first process whose frontmost is true)\n'
        f'    set position of front window to {{{int(x)}, {int(y)}}}\n'
        f'    set size of front window to {{{int(w)}, {int(h)}}}\n'
        '  end tell\n'
        'end tell'
    )


def _escape_applescript(s: str) -> str:
    """Strip the two characters that could end the string literal early.

    The app name reaches this from a model-supplied parameter, so it is treated
    as untrusted: a `"` would close the literal and let the rest run as script."""
    return str(s).replace("\\", "").replace('"', "")


def list_apps() -> list[str]:
    """Names of apps with a visible window, or [] if unavailable."""
    out = _osascript(script_list_apps())
    if not out:
        return []
    return [a.strip() for a in out.split(",") if a.strip()]


def frontmost_app() -> str | None:
    out = _osascript(script_frontmost_app())
    return out.strip() if out and out.strip() else None


def focus_app(name: str) -> bool:
    if not name:
        return False
    return _osascript(script_focus_app(name)) is not None


def move_front_window(x: int, y: int, w: int, h: int) -> bool:
    return _osascript(script_move_front_window(x, y, w, h)) is not None


# ── Combined readout ─────────────────────────────────────────────────────────

def system_telemetry() -> dict:
    """One call for the HUD and the system_monitor tool: battery + thermal.

    Keys are omitted rather than set to None when unavailable, so a consumer can
    tell "no battery" from "battery at 0%"."""
    if not IS_MACOS:
        return {}
    out: dict = {}
    batt = battery_status()
    if batt.get("present"):
        out["battery_percent"] = batt.get("percent")
        out["battery_charging"] = batt.get("charging")
        if batt.get("remaining"):
            out["battery_remaining"] = batt["remaining"]
    therm = thermal_status()
    if therm:
        out["thermal"] = therm
    bright = brightness_get()
    if bright is not None:
        out["brightness"] = bright
    return out
