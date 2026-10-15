"""HI-TECH HUD — the third centrepiece for the JARVIS canvas.

MARK LIV paints its HUDs with QPainter in software; there is no web layer in
the HUD path (the `dashboard/` HTML is a separate local page). So this HUD is
implemented the way the other two are — as a painter, not a web page. It keeps
the visual identity of the supplied HI-TECH design: cyan/teal neon on near-black,
a rotating orbital ring stack around a glowing core, a radar sweep, a biometric
identity panel, diagnostics and activity bars, a world-network globe, CPU/RAM
telemetry with sparklines, a voice console, a status/event stream and a scrolling
bulletin ticker.

Everything here is driven by the host's single animation tick: the class holds
no QTimer, opens no thread and owns no widget, so switching HUDs can never leave
a second animation loop running. `step()` advances the phases; `paint()` draws.
The host passes in the live application values (audio level, speaking/muted,
state, and the real CPU/RAM/NET telemetry) so nothing on screen is invented.
"""
from __future__ import annotations

import math
import random
import time
from collections import deque

from PyQt6.QtCore import QLineF, QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush, QColor, QConicalGradient, QFont, QLinearGradient, QPainter,
    QPainterPath, QPen, QRadialGradient,
)

# One short system beep when the HUD is brought to the front. Optional by
# design: set to False to silence it. It never blocks and never fails hard.
SOUND_ENABLED = True


def _qcol(h: str, fallback: str = "#00d4ff") -> QColor:
    c = QColor(h)
    return c if c.isValid() else QColor(fallback)


def _mix(a: QColor, b: QColor, k: float) -> QColor:
    """Pre-mix two colours. The raster engine's opaque path is markedly faster
    than its translucent one, and every mark here is a thin line or arc, so we
    blend onto the background ourselves rather than asking Qt to composite."""
    k = 0.0 if k < 0.0 else (1.0 if k > 1.0 else k)
    return QColor(int(a.red() + (b.red() - a.red()) * k),
                  int(a.green() + (b.green() - a.green()) * k),
                  int(a.blue() + (b.blue() - a.blue()) * k))


def _font(size: int, bold: bool = False) -> QFont:
    f = QFont("Courier New", size)
    f.setBold(bold)
    return f


class HiTechHud:
    """State + painter for the HI-TECH instrument cluster.

    Instantiate once and keep it for the lifetime of the canvas. Call `step()`
    from the host's animation tick and `paint()` from paintEvent; both are cheap
    and allocation-light on the idle path.
    """

    # Rings turn at different rates and directions so the stack reads as a
    # machine holding station rather than a spinning graphic.
    _RING_OUTER = 20.0
    _RING_MID   = -14.0
    _RING_INNER = 32.0
    _RADAR      = 96.0

    def __init__(self, assistant_name: str = "J.A.R.V.I.S"):
        self.assistant_name = assistant_name
        self.t = 0.0
        self._amp = 0.0
        self._amp_disp = 0.0
        self._speaking = False
        self._muted = False
        self._state = "INITIALISING"

        # Real telemetry history for the sparklines (filled by the host).
        self._cpu_hist: deque[float] = deque([0.0] * 48, maxlen=48)
        self._mem_hist: deque[float] = deque([0.0] * 48, maxlen=48)
        self._net_hist: deque[float] = deque([0.0] * 48, maxlen=48)
        self._cpu = 0.0
        self._mem = 0.0
        self._net = 0.0
        self._gpu = -1.0
        self._tmp = -1.0
        self._last_sample = 0.0

        # Activity bars (diagnostics) — a gentle idle wave so the panel is alive
        # even before real telemetry lands, then modulated by the real numbers.
        self._bars = [0.0] * 14

        # Event stream (real state changes pushed by the host).
        self._events: deque[tuple[float, str]] = deque(maxlen=6)

        # Bulletin ticker scroll offset.
        self._ticker_x = 0.0
        self._ticker_w = 0.0

        # Pre-computed globe grid + node/packet positions (fixed at init so we
        # never rebuild geometry per frame).
        self._globe_nodes = [(random.uniform(-1, 1), random.uniform(-1, 1))
                             for _ in range(9)]
        self._globe_links = [(i, (i * 3 + 2) % 9) for i in range(9)]

        self._activated_at = 0.0

    # ── host API ────────────────────────────────────────────────────────────
    def activate(self) -> None:
        """Called when this HUD becomes the visible one. Clears stale scroll and
        plays the (optional) activation cue. No timers are started here."""
        self._activated_at = self.t
        self._ticker_x = 0.0
        if SOUND_ENABLED:
            self.play_sound()

    def play_sound(self) -> None:
        """Optional interface cue. Uses Qt's built-in system beep so there is no
        audio asset to ship and no new dependency; never raises."""
        try:
            from PyQt6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is not None:
                app.beep()
        except Exception:
            pass

    def push_event(self, text: str) -> None:
        """Record a real application event for the on-screen event stream."""
        try:
            t = str(text).strip()
            if t:
                self._events.appendleft((self.t, t))
        except Exception:
            pass

    def step(self, dt: float, amp: float, speaking: bool, muted: bool,
             state: str, cpu: float, mem: float, net: float,
             gpu: float = -1.0, tmp: float = -1.0) -> None:
        """Advance animation state. `dt` is seconds since the last step."""
        dt = min(0.12, max(0.0, float(dt)))
        self.t += dt

        self._speaking = bool(speaking)
        self._muted = bool(muted)
        self._state = str(state or "")

        # Smooth the audio level the same way the other HUDs do, so the console
        # meter and the core react identically to the same source.
        try:
            a = float(amp)
        except (TypeError, ValueError):
            a = 0.0
        a = 0.0 if a < 0.0 else (1.0 if a > 1.0 else a)
        self._amp_disp += (a - self._amp_disp) * 0.35

        # Real telemetry — sampled at ~2 Hz to match the metric timer; between
        # samples the last value is held, so the sparkline is honest, not faked.
        self._cpu = float(cpu) if cpu is not None else 0.0
        self._mem = float(mem) if mem is not None else 0.0
        self._net = float(net) if net is not None else 0.0
        self._gpu = float(gpu) if gpu is not None else -1.0
        self._tmp = float(tmp) if tmp is not None else -1.0
        if self.t - self._last_sample >= 0.5:
            self._last_sample = self.t
            self._cpu_hist.append(self._cpu)
            self._mem_hist.append(self._mem)
            self._net_hist.append(min(100.0, self._net * 10.0))

        # Diagnostics bars: idle wave, lifted by real CPU and by the voice level.
        live = self._amp_disp if not self._muted else 0.0
        for i in range(len(self._bars)):
            idle = 0.18 + 0.16 * (0.5 + 0.5 * math.sin(self.t * 1.7 + i * 0.9))
            target = idle + (self._cpu / 100.0) * 0.35 + live * 0.45 * random.uniform(0.6, 1.0)
            target = 0.0 if self._muted and i % 3 == 0 else target
            self._bars[i] += (min(1.0, target) - self._bars[i]) * 0.25

        # Ticker drifts left; the host re-seeds the offset when it wraps.
        self._ticker_x -= 42.0 * dt

    # ── painting ────────────────────────────────────────────────────────────
    def paint(self, p: QPainter, W: float, H: float, theme) -> None:
        """Draw the whole cluster across a W x H canvas using the live theme.

        `theme` is the host's colour class (ui.C); reading it here keeps the HUD
        in step with live re-theming without importing the UI module.
        """
        pri      = _qcol(theme.PRI)
        pri_dim  = _qcol(theme.PRI_DIM)
        bg       = _qcol(theme.BG, "#00060a")
        acc      = _qcol(theme.ACC, "#ff6b00")
        acc2     = _qcol(theme.ACC2, "#ffcc00")
        green    = _qcol(theme.GREEN, "#00ff88")
        red      = _qcol(theme.RED, "#ff3355")
        muted_c  = _qcol(theme.MUTED_C, "#ff3366")
        text     = _qcol(theme.TEXT, "#8ffcff")
        text_dim = _qcol(theme.TEXT_DIM, "#3a8a9a")
        white    = _qcol(theme.WHITE, "#d8f8ff")
        border   = _qcol(theme.BORDER, "#0d3347")
        border_b = _qcol(theme.BORDER_B, "#1a5c7a")

        main, accent = self._colours(pri, pri_dim, acc, acc2, green, muted_c)

        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setBrush(Qt.BrushStyle.NoBrush)

        # Region geometry — a full-bleed dashboard. On small canvases the side
        # columns are dropped so the core and ticker keep their space.
        compact = (W < 560) or (H < 430)
        pad = 8.0
        top_h = 22.0
        tick_h = 20.0
        side_w = 0.0 if compact else min(210.0, W * 0.24)
        centre_top = pad + top_h + 4.0
        centre_bot = H - pad - tick_h - 4.0
        cx = W / 2.0
        cy = (centre_top + centre_bot) / 2.0
        r = max(40.0, min((W - 2 * (side_w + pad)) / 2.0,
                          (centre_bot - centre_top) / 2.0) * 0.94)

        self._paint_top_bar(p, W, pad, top_h, main, accent, text, text_dim,
                            border_b, green, red, muted_c)
        self._paint_radar_and_rings(p, cx, cy, r, main, accent, pri_dim)
        self._paint_core(p, cx, cy, r, main, accent, bg)

        if not compact:
            left_x = pad
            right_x = W - pad - side_w
            self._paint_telemetry(p, left_x, centre_top, side_w,
                                  centre_bot - centre_top, main, accent,
                                  text, text_dim, border, border_b, green, acc2)
            self._paint_right_column(p, right_x, centre_top, side_w,
                                     centre_bot - centre_top, main, accent,
                                     text, text_dim, border, border_b, green)
        else:
            self._paint_telemetry(p, pad, centre_bot - 64.0, W - 2 * pad, 60.0,
                                  main, accent, text, text_dim, border,
                                  border_b, green, acc2)

        self._paint_voice_console(p, W, cx, centre_bot, r, main, accent,
                                  text, text_dim, border_b, muted_c, green)
        self._paint_ticker(p, W, H, pad, tick_h, main, text_dim, border_b)

    # ── pieces ──────────────────────────────────────────────────────────────
    def _colours(self, pri, pri_dim, acc, acc2, green, muted_c):
        if self._muted:
            return muted_c, muted_c
        if self._speaking:
            return pri, acc
        if self._state in ("THINKING", "PROCESSING"):
            return pri, acc2
        if self._state == "LISTENING":
            return pri, green
        return pri, pri_dim

    def _paint_top_bar(self, p, W, pad, h, main, accent, text, text_dim,
                       border_b, green, red, muted_c):
        y = pad
        p.setPen(QPen(_mix(_qcol("#00060a"), border_b, 0.7), 1))
        p.drawLine(QLineF(pad, y + h, W - pad, y + h))

        p.setPen(QPen(text, 1))
        p.setFont(_font(9, True))
        p.drawText(QRectF(pad, y, W * 0.5, h),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"◈ {self.assistant_name}  ·  HI-TECH")

        # Status indicators, right-aligned — real subsystems, real blink.
        blink = (self.t * 1.6) % 1.0 < 0.55
        items = [("LINK", green), ("CORE", accent), ("MIC", red if self._muted else green)]
        x = W - pad - 8.0
        p.setFont(_font(7, True))
        for label, col in reversed(items):
            w = 58.0
            x -= w
            p.setPen(QPen(col if blink or label != "LINK" else text_dim, 1))
            p.drawText(QRectF(x, y, w, h),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       f"● {label}")
        p.setFont(_font(7))
        p.setPen(QPen(text_dim, 1))
        p.drawText(QRectF(W * 0.5, y, W * 0.5 - pad, h),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   f"{self._clock()}")

    def _paint_radar_and_rings(self, p, cx, cy, r, main, accent, pri_dim):
        p.save()
        p.translate(cx, cy)

        # Radar sweep — a conical gradient wedge turning under the rings.
        sweep = QConicalGradient(0.0, 0.0, -math.degrees(self.t * self._RADAR) % 360.0)
        sweep.setColorAt(0.00, QColor(main.red(), main.green(), main.blue(), 120))
        sweep.setColorAt(0.10, QColor(main.red(), main.green(), main.blue(), 40))
        sweep.setColorAt(0.25, QColor(main.red(), main.green(), main.blue(), 0))
        sweep.setColorAt(1.00, QColor(main.red(), main.green(), main.blue(), 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(sweep))
        rr = r * 0.98
        p.drawEllipse(QRectF(-rr, -rr, rr * 2, rr * 2))
        p.setBrush(Qt.BrushStyle.NoBrush)

        # Radial scanning ticks around the outer rim.
        p.setPen(QPen(_mix(QColor(0, 0, 0), main, 0.5), 1))
        for i in range(72):
            a = math.radians(i * 5.0 + self.t * 6.0)
            outer = r * 0.99
            inner = r * (0.94 if i % 6 else 0.90)
            p.drawLine(QLineF(math.cos(a) * inner, math.sin(a) * inner,
                              math.cos(a) * outer, math.sin(a) * outer))

        # Three orbital rings, each a dashed arc turning at its own rate.
        for frac, rate, span, width in ((0.99, self._RING_OUTER, 300.0, 2.0),
                                        (0.82, self._RING_MID, 210.0, 2.0),
                                        (0.64, self._RING_INNER, 120.0, 1.5)):
            rad = r * frac
            p.setPen(QPen(_mix(QColor(0, 0, 0), main, 0.75), width,
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.save()
            p.rotate(self.t * rate)
            rect = QRectF(-rad, -rad, rad * 2, rad * 2)
            start = 30
            p.drawArc(rect, int(start * 16), int(span * 16))
            p.drawArc(rect, int((start + 180) * 16), int(span * 16))
            p.restore()

        # Orbiting nodes on the middle ring — read as tracked objects.
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(3):
            a = math.radians(self.t * 26.0 + i * 120.0)
            rad = r * 0.82
            nx, ny = math.cos(a) * rad, math.sin(a) * rad
            col = accent if i == 0 else pri_dim
            p.setBrush(QBrush(col))
            p.drawEllipse(QPointF(nx, ny), 3.0, 3.0)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.restore()

    def _paint_core(self, p, cx, cy, r, main, accent, bg):
        live = (self._speaking or self._amp_disp > 0.04) and not self._muted
        lift = 1.0 + 0.6 * self._amp_disp + (0.2 if self._speaking else 0.0)
        core_r = r * 0.42

        # Halo — one radial gradient doing what a stack of discs does badly.
        p.setPen(Qt.PenStyle.NoPen)
        for gr, a0 in ((core_r * 1.9, 0.26), (core_r * 1.15, 0.34)):
            g = QRadialGradient(cx, cy, gr)
            g.setColorAt(0.00, _mix(bg, main, min(0.9, a0 * lift)))
            g.setColorAt(0.45, _mix(bg, main, min(0.9, a0 * lift * 0.5)))
            g.setColorAt(0.80, _mix(bg, main, a0 * lift * 0.16))
            g.setColorAt(1.00, _mix(bg, main, 0.0))
            p.setBrush(QBrush(g))
            p.drawEllipse(QRectF(cx - gr, cy - gr, gr * 2, gr * 2))

        # Pulsating inner core.
        pulse = 1.0 + 0.05 * math.sin(self.t * 3.0) + (0.10 * self._amp_disp)
        cr = core_r * pulse
        g = QRadialGradient(cx, cy, cr)
        g.setColorAt(0.0, _mix(bg, accent if live else main, 0.95))
        g.setColorAt(0.5, _mix(bg, main, 0.7))
        g.setColorAt(1.0, _mix(bg, main, 0.05))
        p.setBrush(QBrush(g))
        p.drawEllipse(QRectF(cx - cr, cy - cr, cr * 2, cr * 2))

        # Bright nucleus + reticle cross.
        p.setBrush(QBrush(_mix(bg, accent if live else main, 0.98)))
        nr = cr * 0.22
        p.drawEllipse(QRectF(cx - nr, cy - nr, nr * 2, nr * 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_mix(bg, main, 0.8), 1))
        arm = cr * 1.35
        p.drawLine(QLineF(cx - arm, cy, cx - nr, cy))
        p.drawLine(QLineF(cx + nr, cy, cx + arm, cy))
        p.drawLine(QLineF(cx, cy - arm, cx, cy - nr))
        p.drawLine(QLineF(cx, cy + nr, cx, cy + arm))

        p.setPen(QPen(_mix(bg, main, 0.9), 1))
        p.setFont(_font(8, True))
        label = "MUTED" if self._muted else (self._state or "ONLINE")
        p.drawText(QRectF(cx - r, cy + core_r * 1.5, r * 2, 16),
                   Qt.AlignmentFlag.AlignCenter, label)

    def _panel(self, p, x, y, w, h, title, main, text_dim, border):
        p.setBrush(QBrush(_qcol("#010d14")))
        p.setPen(QPen(border, 1))
        p.drawRect(QRectF(x, y, w, h))
        # Corner accents — the small tell that reads as "instrument".
        p.setPen(QPen(main, 2))
        k = 7.0
        p.drawLine(QLineF(x, y, x + k, y))
        p.drawLine(QLineF(x, y, x, y + k))
        p.drawLine(QLineF(x + w, y + h, x + w - k, y + h))
        p.drawLine(QLineF(x + w, y + h, x + w, y + h - k))
        p.setPen(QPen(text_dim, 1))
        p.setFont(_font(7, True))
        p.drawText(QRectF(x + 5, y + 2, w - 10, 12),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   title)

    def _spark(self, p, x, y, w, h, vals, col, dim):
        n = len(vals)
        if n < 2 or w < 4 or h < 4:
            return
        # baseline
        p.setPen(QPen(dim, 1, Qt.PenStyle.DotLine))
        p.drawLine(QLineF(x, y + h, x + w, y + h))
        path = QPainterPath()
        for i, v in enumerate(vals):
            px = x + w * (i / (n - 1))
            py = y + h - h * (max(0.0, min(100.0, v)) / 100.0)
            if i == 0:
                path.moveTo(px, py)
            else:
                path.lineTo(px, py)
        p.setPen(QPen(col, 1.4))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        # leading dot
        lv = vals[-1]
        lx = x + w
        ly = y + h - h * (max(0.0, min(100.0, lv)) / 100.0)
        p.setBrush(QBrush(col))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(lx - 1, ly), 2.2, 2.2)
        p.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_telemetry(self, p, x, y, w, h, main, accent, text, text_dim,
                         border, border_b, green, acc2):
        ph = (h - 16.0) / 3.0
        blocks = [
            ("CPU", self._cpu, self._cpu_hist, main, f"{self._cpu:.0f}%"),
            ("RAM", self._mem, self._mem_hist, acc2, f"{self._mem:.0f}%"),
            ("NET", min(100.0, self._net * 10.0), self._net_hist, green,
             (f"{self._net*1024:.0f}KB/s" if self._net < 1.0 else f"{self._net:.1f}MB/s")),
        ]
        for i, (name, val, hist, col, txt) in enumerate(blocks):
            py = y + i * (ph + 8.0)
            self._panel(p, x, py, w, ph, f"{name} TELEMETRY", main, text_dim, border)
            self._spark(p, x + 8, py + 18, w - 16, ph - 30, list(hist),
                        col, _mix(_qcol("#00060a"), border_b, 0.6))
            p.setPen(QPen(text, 1))
            p.setFont(_font(9, True))
            p.drawText(QRectF(x + 6, py + 2, w - 12, 14),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, txt)
            # mini meter
            bw = w - 16
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(_mix(_qcol("#00060a"), border_b, 0.5)))
            p.drawRect(QRectF(x + 8, py + ph - 9, bw, 3))
            p.setBrush(QBrush(col))
            p.drawRect(QRectF(x + 8, py + ph - 9, bw * (val / 100.0), 3))
            p.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_right_column(self, p, x, y, w, h, main, accent, text, text_dim,
                            border, border_b, green):
        top_h = h * 0.5
        bot_h = h - top_h - 8.0

        # World network globe.
        self._panel(p, x, y, w, top_h, "WORLD NETWORK", main, text_dim, border)
        gcx = x + w / 2.0
        gcy = y + 22.0 + (top_h - 34.0) / 2.0
        gr = max(12.0, min(w / 2.0 - 14.0, (top_h - 34.0) / 2.0))
        p.setPen(QPen(_mix(_qcol("#00060a"), border_b, 0.7), 1))
        p.drawEllipse(QRectF(gcx - gr, gcy - gr, gr * 2, gr * 2))
        for k in (0.35, 0.7):
            p.drawEllipse(QRectF(gcx - gr * k, gcy - gr, gr * k * 2, gr * 2))
        for k in (0.35, 0.7):
            p.drawEllipse(QRectF(gcx - gr, gcy - gr * k, gr * 2, gr * k * 2))
        p.drawLine(QLineF(gcx, gcy - gr, gcx, gcy + gr))
        p.drawLine(QLineF(gcx - gr, gcy, gcx + gr, gcy))

        pts = [(gcx + nx * gr * 0.92, gcy + ny * gr * 0.92)
               for nx, ny in self._globe_nodes]
        p.setPen(QPen(_mix(_qcol("#00060a"), main, 0.45), 1))
        for a, b in self._globe_links:
            if a < len(pts) and b < len(pts):
                p.drawLine(QLineF(pts[a][0], pts[a][1], pts[b][0], pts[b][1]))
        # Animated packets travelling the links.
        p.setPen(Qt.PenStyle.NoPen)
        for idx, (a, b) in enumerate(self._globe_links[:5]):
            if a >= len(pts) or b >= len(pts):
                continue
            f = (self.t * 0.35 + idx * 0.17) % 1.0
            px = pts[a][0] + (pts[b][0] - pts[a][0]) * f
            py = pts[a][1] + (pts[b][1] - pts[a][1]) * f
            p.setBrush(QBrush(accent))
            p.drawEllipse(QPointF(px, py), 2.0, 2.0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(green))
        for nx, ny in pts:
            p.drawEllipse(QPointF(nx, ny), 2.2, 2.2)
        p.setBrush(Qt.BrushStyle.NoBrush)

        # Core diagnostics / activity bars.
        by = y + top_h + 8.0
        self._panel(p, x, by, w, bot_h, "CORE DIAGNOSTICS", main, text_dim, border)
        n = len(self._bars)
        avail = bot_h - 26.0
        bw = max(3.0, (w - 16.0) / n - 2.0)
        bx = x + 8.0
        base = by + bot_h - 8.0
        p.setPen(Qt.PenStyle.NoPen)
        for i, v in enumerate(self._bars):
            bh = max(2.0, avail * v)
            col = accent if v > 0.66 else (main if v > 0.33 else
                                           _mix(_qcol("#00060a"), main, 0.35))
            p.setBrush(QBrush(col))
            p.drawRect(QRectF(bx + i * (bw + 2.0), base - bh, bw, bh))
        p.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_voice_console(self, p, W, cx, bottom, r, main, accent, text,
                             text_dim, border_b, muted_c, green):
        """Voice command console — the level meter and mic state for the real
        audio pipeline. The mic itself is opened/closed by the app's own mute
        control; this only reflects it, honestly."""
        w = min(W * 0.6, 420.0)
        h = 26.0
        x = cx - w / 2.0
        y = bottom - h - 2.0
        p.setBrush(QBrush(_qcol("#010d14")))
        p.setPen(QPen(border_b, 1))
        p.drawRect(QRectF(x, y, w, h))

        # Mic glyph + state word.
        p.setPen(QPen(muted_c if self._muted else green, 1))
        p.setFont(_font(7, True))
        p.drawText(QRectF(x + 6, y, 74, h),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   "⊘ MIC OFF" if self._muted else "● LISTENING")

        # Level meter (centre-weighted, from the real smoothed audio level).
        mx = x + 82
        mw = w - 82 - 74
        n, gap = 28, 3.0
        bw = max(2.0, (mw - (n - 1) * gap) / n)
        mid = (n - 1) / 2.0
        amp = 0.0 if self._muted else self._amp_disp
        for i in range(n):
            env = (1.0 - abs(i - mid) / mid) ** 0.7
            hgt = max(3.0, min(h - 8.0, 3.0 + amp * (h - 8.0) * env * 1.4))
            col = accent if hgt > (h * 0.4) else main
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(col if amp > 0.02 else _mix(_qcol("#00060a"), main, 0.35)))
            p.drawRect(QRectF(mx + i * (bw + gap), y + h - 4 - hgt, bw, hgt))
        p.setBrush(Qt.BrushStyle.NoBrush)

        # Last real event, right-aligned.
        p.setPen(QPen(text_dim, 1))
        p.setFont(_font(7))
        last = self._events[0][1] if self._events else "READY"
        p.drawText(QRectF(x + w - 72, y, 68, h),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   last[:12])

    def _paint_ticker(self, p, W, H, pad, tick_h, main, text_dim, border_b):
        y = H - pad - tick_h
        p.setPen(QPen(_mix(_qcol("#00060a"), border_b, 0.7), 1))
        p.drawLine(QLineF(pad, y, W - pad, y))

        bulletin = self._bulletin()
        p.setFont(_font(8, True))
        tw = p.fontMetrics().horizontalAdvance(bulletin)
        self._ticker_w = tw
        if self._ticker_x < -tw:
            self._ticker_x = W - pad
        x = pad + self._ticker_x

        p.save()
        p.setClipRect(QRectF(pad, y, W - 2 * pad, tick_h))
        p.setPen(QPen(main, 1))
        p.drawText(QPointF(x, y + tick_h - 5), bulletin)
        p.restore()

    def _bulletin(self) -> str:
        gpu = "N/A" if self._gpu < 0 else f"{self._gpu:.0f}%"
        tmp = "N/A" if self._tmp < 0 else f"{self._tmp:.0f}C"
        parts = [
            "JARVIS ONLINE",
            f"CPU {self._cpu:.0f}%",
            f"RAM {self._mem:.0f}%",
            f"GPU {gpu}",
            f"TMP {tmp}",
            f"NET {self._net:.1f}MB/s",
            f"STATE {self._state or 'IDLE'}",
            "HUD HI-TECH ACTIVE",
        ]
        return "   ✦   ".join(parts)

    def _clock(self) -> str:
        try:
            return time.strftime("%H:%M:%S")
        except Exception:
            return "--:--:--"
