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


# ── CPU identity and load (sysctl) ───────────────────────────────────────────

def parse_brand_string(text: str) -> str | None:
    """Parse `sysctl -n machdep.cpu.brand_string`. Pure.

    Sample: `Intel(R) Core(TM) i5-8210Y CPU @ 1.60GHz`"""
    t = (text or "").strip()
    return t or None


def cpu_brand() -> str | None:
    """The CPU's marketing name, or None when sysctl will not say."""
    return parse_brand_string(
        _run(["sysctl", "-n", "machdep.cpu.brand_string"]) or "")


def parse_loadavg(text: str) -> dict:
    """Parse `sysctl -n vm.loadavg`. Pure.

    Sample: `{ 1.52 1.44 1.38 }` — 1, 5 and 15-minute load averages."""
    nums = re.findall(r"[0-9]+(?:\.[0-9]+)?", text or "")
    if len(nums) < 3:
        return {}
    return {"load_1m": float(nums[0]),
            "load_5m": float(nums[1]),
            "load_15m": float(nums[2])}


def load_average() -> dict:
    """1/5/15-minute load averages, or {} when unavailable."""
    return parse_loadavg(_run(["sysctl", "-n", "vm.loadavg"]) or "")


def cpu_core_count() -> int | None:
    """Logical core count via sysctl, or None."""
    raw = _run(["sysctl", "-n", "hw.logicalcpu"]) or ""
    try:
        return int(raw.strip())
    except ValueError:
        return None


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


# ── Volume (System Events) ───────────────────────────────────────────────────
# `get volume settings` is the one AppleScript read that needs no extra
# permission. Setting volume is absolute; the subtlety is that `set volume` also
# *unmutes*, so a set performed while muted would silently blast audio. The
# setters here read the mute flag first and restore it afterwards.

def parse_volume_settings(text: str) -> dict:
    """Parse `get volume settings` → {'output_volume': int, 'muted': bool}. Pure.

    Sample: `output volume:50, input volume:100, alert volume:100, output muted:false`"""
    out: dict = {}
    m = re.search(r"output volume:\s*(\d+)", text or "")
    if m:
        out["output_volume"] = max(0, min(100, int(m.group(1))))
    m = re.search(r"output muted:\s*(true|false)", text or "", re.IGNORECASE)
    if m:
        out["muted"] = m.group(1).lower() == "true"
    return out


def script_get_volume() -> str:
    return "get volume settings"


def script_set_volume(value: int, preserve_mute: bool = True) -> str:
    """AppleScript to set output volume, optionally keeping the mute flag.

    When preserving, the current mute state is read *inside* the script and
    re-applied, so there is no window where a muted Mac becomes audible."""
    v = max(0, min(100, int(value)))
    if not preserve_mute:
        return f"set volume output volume {v}"
    return (
        "set m to output muted of (get volume settings)\n"
        f"set volume output volume {v}\n"
        "set volume output muted m"
    )


def volume_get() -> int | None:
    """Current output volume 0-100, or None when it cannot be read."""
    return parse_volume_settings(_osascript(script_get_volume()) or "").get(
        "output_volume")


def is_muted() -> bool | None:
    """Whether output is muted, or None when unknown."""
    return parse_volume_settings(_osascript(script_get_volume()) or "").get("muted")


def volume_set(value: int, preserve_mute: bool = True) -> bool:
    """Set output volume 0-100. Preserves the mute flag by default.

    Returns whether the script ran; the exact resulting volume is whatever
    macOS rounded it to."""
    return _osascript(script_set_volume(value, preserve_mute)) is not None


def mute(on: bool = True) -> bool:
    return _osascript(
        f"set volume output muted {'true' if on else 'false'}") is not None


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


# Named layouts. Fractions of the screen so the AppleScript can compute the
# pixel rectangle itself, and a pure helper can be tested without a Mac.
WINDOW_LAYOUTS: dict[str, tuple[float, float, float, float]] = {
    "left":      (0.0, 0.0, 0.5, 1.0),
    "right":     (0.5, 0.0, 0.5, 1.0),
    "top":       (0.0, 0.0, 1.0, 0.5),
    "bottom":    (0.0, 0.5, 1.0, 0.5),
    "top_left":  (0.0, 0.0, 0.5, 0.5),
    "top_right": (0.5, 0.0, 0.5, 0.5),
    "bottom_left":  (0.0, 0.5, 0.5, 0.5),
    "bottom_right": (0.5, 0.5, 0.5, 0.5),
    "full":      (0.0, 0.0, 1.0, 1.0),
    "center":    (0.15, 0.15, 0.7, 0.7),
}

# Aliases the model is likely to say.
_LAYOUT_ALIASES = {
    "maximize": "full", "maximise": "full", "maximized": "full",
    "fullscreen": "full", "full_screen": "full", "full-screen": "full",
    "left_half": "left", "left-half": "left", "lefthalf": "left",
    "right_half": "right", "right-half": "right", "righthalf": "right",
    "top-left": "top_left", "topleft": "top_left",
    "top-right": "top_right", "topright": "top_right",
    "bottom-left": "bottom_left", "bottomleft": "bottom_left",
    "bottom-right": "bottom_right", "bottomright": "bottom_right",
    "middle": "center", "centered": "center",
}


def resolve_layout(name: str) -> str | None:
    """Map a spoken layout name to a key of WINDOW_LAYOUTS, or None. Pure."""
    n = (name or "").strip().lower().replace(" ", "_")
    n = _LAYOUT_ALIASES.get(n, n)
    return n if n in WINDOW_LAYOUTS else None


def layout_geometry(layout: str, screen_w: int, screen_h: int
                    ) -> tuple[int, int, int, int] | None:
    """Pixel rectangle (x, y, w, h) for a layout on a given screen. Pure."""
    key = resolve_layout(layout)
    if key is None:
        return None
    fx, fy, fw, fh = WINDOW_LAYOUTS[key]
    return (int(screen_w * fx), int(screen_h * fy),
            int(screen_w * fw), int(screen_h * fh))


def script_window_layout(layout: str) -> str | None:
    """AppleScript that snaps the front window to a named layout, or None when
    the name is unknown. Screen size is read inside the script."""
    key = resolve_layout(layout)
    if key is None:
        return None
    fx, fy, fw, fh = WINDOW_LAYOUTS[key]
    return (
        'tell application "System Events"\n'
        '  tell (first process whose frontmost is true)\n'
        '    set sb to bounds of window of desktop\n'
        '    set sw to item 3 of sb\n'
        '    set sh to item 4 of sb\n'
        f'    set position of front window to {{sw * {fx}, sh * {fy}}}\n'
        f'    set size of front window to {{sw * {fw}, sh * {fh}}}\n'
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


def snap_front_window(layout: str) -> bool:
    """Snap the front window to a named layout. False on unknown name or error."""
    script = script_window_layout(layout)
    if script is None:
        return False
    return _osascript(script) is not None


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
    brand = cpu_brand()
    if brand:
        out["cpu_brand"] = brand
    load = load_average()
    if load:
        out["load"] = load
    bright = brightness_get()
    if bright is not None:
        out["brightness"] = bright
    return out
