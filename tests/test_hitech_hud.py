"""Tests for core/hitech_hud.py — the third HUD painter.

These exercise the real painter, not a mock: a QGuiApplication in offscreen
mode, a real QImage to paint into, and assertions on the pixels and on the
object's own state. If PyQt6 is unavailable the module is skipped, matching the
suite's rule that nothing heavy is imported on a bare interpreter.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# PyQt6 may be absent, or present but unloadable (a stripped container with no
# GL stack). Either way these tests skip rather than fail collection — the rest
# of the suite must keep running on a bare interpreter, as conftest promises.
try:
    from PyQt6.QtGui import QColor, QGuiApplication, QImage, QPainter
except Exception as _e:  # noqa: BLE001
    pytest.skip(f"PyQt6 unavailable ({_e})", allow_module_level=True)

from core.hitech_hud import HiTechHud  # noqa: E402


class _Theme:
    """Minimal stand-in for ui.C — only the colour attributes the painter reads."""
    BG = "#00060a"
    PANEL = "#010d14"
    PANEL2 = "#010f18"
    BORDER = "#0d3347"
    BORDER_B = "#1a5c7a"
    BORDER_A = "#0f4060"
    PRI = "#00d4ff"
    PRI_DIM = "#007a99"
    PRI_GHO = "#001f2e"
    ACC = "#ff6b00"
    ACC2 = "#ffcc00"
    GREEN = "#00ff88"
    RED = "#ff3355"
    MUTED_C = "#ff3366"
    TEXT = "#8ffcff"
    TEXT_DIM = "#3a8a9a"
    TEXT_MED = "#5ab8cc"
    WHITE = "#d8f8ff"


@pytest.fixture(scope="module")
def qapp():
    # QApplication (not just QGuiApplication) so the widget-level test below can
    # build the real HudCanvas; it is a subclass, so the painter tests are happy.
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _render(hud: HiTechHud, w: int = 900, h: int = 620) -> QImage:
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor("#00060a"))
    p = QPainter(img)
    hud.paint(p, float(w), float(h), _Theme)
    p.end()
    return img


def _stepped(hud: HiTechHud, n: int = 60, amp: float = 0.0) -> None:
    for _ in range(n):
        hud.step(0.016, amp, amp > 0.3, False, "LISTENING",
                 cpu=23.0, mem=41.0, net=0.4, gpu=-1.0, tmp=-1.0)


def test_paints_without_error(qapp):
    hud = HiTechHud("J.A.R.V.I.S")
    _stepped(hud)
    img = _render(hud)
    assert not img.isNull()


def test_paints_something_other_than_background(qapp):
    """The cluster must actually draw — not return a blank canvas."""
    hud = HiTechHud("J.A.R.V.I.S")
    _stepped(hud, 90)
    img = _render(hud)
    bg = QColor("#00060a").rgb()
    non_bg = 0
    for y in range(0, img.height(), 7):
        for x in range(0, img.width(), 7):
            if img.pixel(x, y) != bg:
                non_bg += 1
    assert non_bg > 200, f"only {non_bg} non-background samples — HUD looks blank"


def test_speaking_amp_advances_state(qapp):
    hud = HiTechHud()
    hud.step(0.05, 0.8, True, False, "SPEAKING", 50, 50, 1.0)
    assert hud._amp_disp > 0.0
    assert hud._speaking is True


def test_muted_is_reflected(qapp):
    hud = HiTechHud()
    hud.step(0.05, 0.9, False, True, "MUTED", 10, 10, 0.0)
    assert hud._muted is True


def test_repeated_switch_paint_is_stable(qapp):
    """Simulate many switch-in/switch-out cycles: activate + step + paint must
    never raise and must never accumulate resources (no timers exist here)."""
    hud = HiTechHud()
    for _ in range(30):
        hud.activate()
        _stepped(hud, 10, amp=0.5)
        _render(hud, 500, 360)
        _render(hud, 1200, 800)
    # No QTimer is owned by the HUD — the host drives everything.
    assert not hasattr(hud, "_tmr")


def test_event_stream_records_real_events(qapp):
    hud = HiTechHud()
    hud.push_event("LISTENING")
    hud.push_event("THINKING")
    assert hud._events[0][1] == "THINKING"


def test_compact_layout_does_not_raise(qapp):
    hud = HiTechHud()
    _stepped(hud)
    img = _render(hud, 320, 260)          # below the compact threshold
    assert not img.isNull()


def test_telemetry_history_is_bounded(qapp):
    hud = HiTechHud()
    for _ in range(500):
        hud.step(0.02, 0.0, False, False, "IDLE", 10, 10, 0.0)
    assert len(hud._cpu_hist) <= 48
    assert len(hud._events) <= 6


def test_real_canvas_paints_hitech_without_regressing_others(qapp):
    """The integration path the app actually uses: build the real HudCanvas,
    select HI-TECH, drive its own _step(), and grab the widget. Then confirm the
    other two styles still paint — the whole point of a zero-regression change."""
    from PyQt6.QtGui import QColor

    import ui

    assert ui.HiTechHud is not None, "HI-TECH painter failed to import in ui"

    canvas = ui.HudCanvas("definitely_missing_face.png", "J.A.R.V.I.S")
    canvas.resize(900, 620)
    bg = QColor("#00060a").rgb()

    def non_bg(image):
        return sum(1 for y in range(0, image.height(), 8)
                   for x in range(0, image.width(), 8)
                   if image.pixel(x, y) != bg)

    canvas.hud_style = "hitech"
    for _ in range(60):
        canvas._step()
    hitech_drawn = non_bg(canvas.grab().toImage())
    assert hitech_drawn > 500, f"HI-TECH canvas looks blank ({hitech_drawn})"

    # The other two centrepieces must still render after the third was added.
    for style in ("face", "core"):
        canvas.hud_style = style
        canvas._step()
        assert non_bg(canvas.grab().toImage()) > 200, f"{style} HUD regressed"


def test_selector_offers_all_three_styles(qapp):
    import ui
    keys = [k for k, _ in ui.MainWindow._hud_choices()]
    assert keys == ["face", "core", "hitech"]
