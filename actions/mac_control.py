"""
mac_control — macOS-specific readouts and window control.

Everything here is a thin, defensive wrapper over core/macos.py. On a non-Mac
the tool reports that plainly instead of failing.

WHAT IT CAN DO
  telemetry    — battery %, charge state, time remaining, thermal throttle
                 level, CPU brand, load averages, current brightness. Read-only.
  list_apps    — apps with a visible window.
  frontmost    — the app currently in front.
  focus_app    — bring a named app to the front.
  move_window  — move/resize the front window to x,y,w,h.
  window_layout— snap the front window: left, right, top, bottom, corners,
                 full (maximise) or center.
  brightness_* — read, set, step.

WHAT IT CANNOT DO
  Report CPU temperature. macOS exposes none without extra software, and this
  tool does not guess. `telemetry` reports the thermal *throttle* level instead,
  which is the honest signal that the machine is running hot.

PERMISSIONS ON macOS
  Window operations go through System Events and need Accessibility access
  (System Settings → Privacy & Security → Accessibility). Without it they return
  "unavailable" rather than an error — see core/macos.py.
"""
from __future__ import annotations

from core import macos


def mac_control(parameters: dict, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = (params.get("action") or "telemetry").strip().lower()

    if not macos.available():
        return "This is a macOS-only feature, and this machine is not running macOS."

    if action in ("telemetry", "status", "battery", "info"):
        return _telemetry()

    if action in ("list_apps", "apps", "list_windows"):
        apps = macos.list_apps()
        if not apps:
            return ("I could not read the window list. This needs Accessibility "
                    "access for this app in System Settings → Privacy & Security.")
        return "Apps with a window open: " + ", ".join(apps) + "."

    if action in ("frontmost", "front_app", "active_app"):
        name = macos.frontmost_app()
        return f"The front app is {name}." if name else \
            "I could not tell which app is in front."

    if action in ("focus_app", "focus", "switch_app", "activate_app"):
        name = (params.get("app") or params.get("value") or "").strip()
        if not name:
            return "Which app should I bring to the front?"
        if macos.focus_app(name):
            return f"Brought {name} to the front."
        return (f"I could not find an app called {name}, or I lack Accessibility "
                f"access to switch to it.")

    if action in ("move_window", "move", "resize"):
        try:
            x = int(params.get("x", 0));  y = int(params.get("y", 0))
            w = int(params.get("width", 800)); h = int(params.get("height", 600))
        except (TypeError, ValueError):
            return "I need x, y, width and height as whole numbers."
        if macos.move_front_window(x, y, w, h):
            return f"Moved the front window to {x},{y} at {w}×{h}."
        return ("I could not move the window — this needs Accessibility access, "
                "or no window is in front.")

    if action in ("window_layout", "snap", "arrange", "tile", "layout"):
        name = (params.get("layout") or params.get("value")
                or params.get("position") or "").strip()
        if not name:
            return ("Which layout? I know left, right, top, bottom, the four "
                    "corners, full and center.")
        if macos.snap_front_window(name):
            return f"Snapped the front window to the {name.replace('_', ' ')}."
        if macos.resolve_layout(name) is None:
            return (f"I do not know a '{name}' layout. Try left, right, top, "
                    f"bottom, a corner, full or center.")
        return ("I could not move the window — this needs Accessibility access, "
                "or no window is in front.")

    if action in ("brightness_get", "get_brightness"):
        v = macos.brightness_get()
        return f"Brightness is at {v}%." if v is not None else \
            "I cannot read the exact brightness on this Mac. Installing the " \
            "`brightness` command-line tool (brew install brightness) would let me."

    if action in ("brightness_set", "set_brightness"):
        try:
            v = int(params.get("value", 50))
        except (TypeError, ValueError):
            return "Give me a brightness percentage from 0 to 100."
        if macos.brightness_set(v):
            return f"Brightness set to {v}%."
        # No CLI: fall back to relative steps toward the requested level, but be
        # honest that the exact value was not reached.
        step = 1 if v >= 50 else -1
        macos.brightness_step(step)
        return ("I nudged the brightness, but I cannot set an exact percentage "
                "without the `brightness` tool installed.")

    if action in ("brightness_up", "brighter"):
        cur = macos.brightness_get()
        if cur is not None and macos.brightness_set(min(100, cur + 10)):
            return f"Brightness up to {min(100, cur + 10)}%."
        macos.brightness_step(1)
        return "Brightness up."

    if action in ("brightness_down", "dimmer"):
        cur = macos.brightness_get()
        if cur is not None and macos.brightness_set(max(0, cur - 10)):
            return f"Brightness down to {max(0, cur - 10)}%."
        macos.brightness_step(-1)
        return "Brightness down."

    return f"Unknown macOS action: {action}"


def _telemetry() -> str:
    t = macos.system_telemetry()
    if not t:
        return "I could not read any telemetry from this Mac."

    parts: list[str] = []
    if "battery_percent" in t:
        line = f"Battery {t['battery_percent']}%"
        if t.get("battery_charging") is True:
            line += ", charging"
        elif t.get("battery_charging") is False:
            line += ", on battery"
        if t.get("battery_remaining"):
            line += f", {t['battery_remaining']} left"
        parts.append(line)
    if "brightness" in t:
        parts.append(f"brightness {t['brightness']}%")
    if t.get("cpu_brand"):
        parts.append(t["cpu_brand"])
    load = t.get("load") or {}
    if "load_1m" in load:
        parts.append(f"load {load['load_1m']:.2f}")

    therm = t.get("thermal") or {}
    limit = therm.get("CPU_Speed_Limit")
    if limit is not None and limit < 100:
        parts.append(f"CPU throttled to {limit}% (running hot)")
    elif "cpu_thermal_level" in therm:
        parts.append(f"CPU thermal level {therm['cpu_thermal_level']}")

    if not parts:
        return "This Mac reports no battery, brightness or thermal data."
    return "Mac status: " + "; ".join(parts) + "."


TOOL = {
    "name": "mac_control",
    "permission": "reversible",
    "description": (
        "macOS-only: read Mac telemetry (battery, charge, time remaining, "
        "thermal throttling, CPU brand, load averages, brightness), list or "
        "switch apps, move or snap the front window, and adjust brightness. Use "
        "for Mac-specific requests like 'how's my battery', 'what's in front', "
        "'switch to Safari', 'move this window to the left', 'maximise this', "
        "'dim the screen'. For volume use computer_settings. "
        "Does NOT report CPU temperature — macOS does not expose it."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "telemetry | list_apps | frontmost | focus_app | "
                    "move_window | window_layout | brightness_get | "
                    "brightness_set | brightness_up | brightness_down"
                ),
            },
            "app": {
                "type": "STRING",
                "description": "App name for focus_app",
            },
            "layout": {
                "type": "STRING",
                "description": ("Window layout for window_layout: left, right, "
                                "top, bottom, top_left, top_right, bottom_left, "
                                "bottom_right, full, center"),
            },
            "x": {"type": "INTEGER", "description": "Window x for move_window"},
            "y": {"type": "INTEGER", "description": "Window y for move_window"},
            "width": {"type": "INTEGER", "description": "Window width"},
            "height": {"type": "INTEGER", "description": "Window height"},
            "value": {
                "type": "STRING",
                "description": "Brightness 0-100 for brightness_set",
            },
        },
        "required": ["action"],
    },
    "handler": mac_control,
}
