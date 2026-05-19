"""
FRIDAY HUD — futuristic floating glass overlay (Iron-Man style).

Visual states:
  IDLE      → dim breathing concentric rings, barely visible
  LISTENING → cyan waveform bars + glowing rings + diamond glyph
  THINKING  → spinning arcs + bouncing dots + diamond glyph
  SPEAKING  → pulsing rings + response text + diamond glyph

Layout:
  Top:    F R I D A Y wordmark + live date/clock
  Middle: 3 concentric rings (rotate at different speeds) around a
          central diamond glyph that pulses with state
  Bottom: STATE label (LISTENING / THINKING / SPEAKING)

Thread-safe: call set_state() from any thread via Qt signals.
Draggable: click-drag to reposition.
System tray: right-click tray icon to quit.
"""

import ctypes
import logging
import math
import random
import sys
from datetime import datetime
from enum import Enum

from PyQt6.QtCore import QPoint, QRect, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QIcon, QPainter, QPainterPath, QPen,
    QPixmap, QRadialGradient,
)
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

logger = logging.getLogger("jarvis.hud")

# ── Windows click-through helper ─────────────────────────────────────────────
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_LAYERED     = 0x00080000
_GWL_EXSTYLE       = -20


def _set_click_through(hwnd: int, enable: bool) -> None:
    """Toggle click-through (WS_EX_TRANSPARENT) on a Windows HWND.

    Only flips WS_EX_TRANSPARENT. WS_EX_LAYERED is intentionally left
    alone — Qt sets it itself via WA_TranslucentBackground, and toggling
    it here used to wipe the layered-window content (HUD invisible until
    next paint, which on rare ticks never happened).
    """
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        if enable:
            new_style = style | _WS_EX_TRANSPARENT
        else:
            new_style = style & ~_WS_EX_TRANSPARENT
        if new_style != style:
            user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, new_style)
    except Exception:
        pass


class State(Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


# ── Palette ───────────────────────────────────────────────────────────────────
_BG      = QColor(4, 8, 18, 232)
_BORDER  = QColor(0, 160, 230, 65)
_RING    = QColor(0, 210, 255)
_TEXT    = QColor(185, 228, 255)
_LABEL   = QColor(0, 175, 245, 145)
_DIM     = QColor(0, 70, 130, 45)


# ── HUD widget ────────────────────────────────────────────────────────────────

class JarvisHUD(QWidget):
    """
    Frameless, always-on-top, semi-transparent overlay.
    Sits in the bottom-right corner; draggable by mouse.
    """

    _sig_state = pyqtSignal(str)
    _sig_text  = pyqtSignal(str)

    W = 300
    H = 300
    # Center of the ring stack — nudged down to leave room for the top
    # wordmark + date at y≈0..40, while still sitting clear of the
    # bottom STATE label at y≈262..282.
    CY_OFFSET = 14
    R0 = 64     # inner ring (closest to centre glyph)
    R  = 92     # middle ring (waveform sits on this)
    R2 = 116    # outer ring (tick marks live just outside this)
    R3 = 128    # outermost faint ring (decorative)

    def __init__(self):
        super().__init__(flags=(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        ))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(self.W, self.H)

        # State
        self._state      = State.IDLE
        self._text       = ""
        self._tick       = 0
        self._arc        = 0.0          # thinking arc angle (degrees)
        self._idle_phase = 0.0          # breathing sine phase
        self._bars       = [0.05] * 18  # waveform bar heights 0–1
        self._bar_tgt    = [0.05] * 18  # animation targets
        self._fade       = 0.22         # current window opacity
        self._fade_tgt   = 0.22
        self._pulse_r    = 0.0          # speaking pulse radius
        self._pulse_a    = 0            # speaking pulse alpha
        # Concentric ring rotation phases — different speeds per ring.
        # Outer ring rotates clockwise, middle counter-clockwise, inner
        # clockwise faster: gives the layered "alive" look from the ref image.
        self._ring_phase_outer = 0.0
        self._ring_phase_mid   = 0.0
        self._ring_phase_inner = 0.0
        self._drag_start = None

        # Thread-safe signal wiring
        self._sig_state.connect(self._apply_state)
        self._sig_text.connect(self._apply_text)

        # Position to bottom-right corner
        self._snap()

        # Animation: ~30 fps main tick, slower bar target randomisation
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick_fn)
        self._timer.start(33)

        self._bar_timer = QTimer(self)
        self._bar_timer.timeout.connect(self._randomise_bars)
        self._bar_timer.start(115)

        # Start hidden — HUD only appears when Jarvis is activated
        self.hide()

    # ── Public thread-safe API ────────────────────────────────────────────────

    def set_state(self, state: State, text: str = ""):
        """Call from any thread."""
        self._sig_state.emit(state.value)
        self._sig_text.emit(text)

    def set_audio_level(self, rms: float):
        """Optional: feed live mic RMS (0–1) to drive waveform energy."""
        self._sig_text.emit("")          # keep text slot clear
        # Scale bar targets by current RMS — call from asyncio thread is fine
        # because _randomise_bars handles the actual update on the Qt thread

    # ── Signal handlers (Qt main thread) ─────────────────────────────────────

    def _apply_state(self, val: str):
        prev = self._state
        self._state = State(val)
        # Diagnostic log so Sir can confirm Qt signals are reaching the
        # main thread. If the wake word fires but no "HUD state →"
        # appears, the chain is broken in main.py / handle_activation.
        # If this DOES log but the HUD still isn't visible, the problem
        # is at the Windows compositor / focus level.
        logger.info("HUD state \u2192 %s (was %s)", self._state.name, prev.name)

        if self._state == State.IDLE:
            # Hide completely when idle — no black box sitting on desktop
            self.hide()
            # Enable click-through so hidden window never blocks anything
            if sys.platform == "win32" and self.winId():
                _set_click_through(int(self.winId()), True)
        else:
            # Disable click-through so user can drag during interaction
            if sys.platform == "win32" and self.winId():
                _set_click_through(int(self.winId()), False)
            # Snap back to corner in case user moved it, then show
            self._snap()
            # Set opacity SYNCHRONOUSLY before show() so the very first
            # paint happens at a visible level. Without this, the first
            # paint used whatever opacity was last set (often 0.01 from
            # the previous IDLE animation), leaving a frame or two of
            # invisibility — which on slow machines was Sir's "the HUD
            # never appears" symptom. The timer still animates 0.55 → 1.0.
            self._fade = 0.55
            self._fade_tgt = 1.0
            self.setWindowOpacity(self._fade)
            self.show()
            self.raise_()
            self.activateWindow()  # nudge Z-order without stealing focus
            # On Windows, frameless top-most tool windows can lose their
            # always-on-top status to other top-most apps. Re-asserting
            # via SetWindowPos here is a no-op if we're already on top
            # but rescues the HUD from games / OBS / etc.
            self._force_topmost()

    def _apply_text(self, text: str):
        self._text = text

    # ── Animation tick ────────────────────────────────────────────────────────

    def _tick_fn(self):
        self._tick += 1

        # Smooth opacity fade
        self._fade += (self._fade_tgt - self._fade) * 0.07
        self.setWindowOpacity(max(0.01, min(1.0, self._fade)))

        # Smooth bar lerp
        for i in range(len(self._bars)):
            self._bars[i] += (self._bar_tgt[i] - self._bars[i]) * 0.16

        self._arc = (self._arc + 3.6) % 360
        self._idle_phase = (self._tick * 0.038) % (2 * math.pi)

        # Layered ring rotation — small constant deltas, different signs
        # so the rings drift past each other smoothly.
        self._ring_phase_outer = (self._ring_phase_outer + 0.45)  % 360
        self._ring_phase_mid   = (self._ring_phase_mid   - 0.70)  % 360
        self._ring_phase_inner = (self._ring_phase_inner + 1.10)  % 360

        if self._state == State.SPEAKING:
            self._pulse_r = (self._pulse_r + 1.4) % 88
            self._pulse_a = max(0, 215 - int(self._pulse_r * 2.6))

        self.update()

    def _randomise_bars(self):
        n = len(self._bar_tgt)
        if self._state == State.LISTENING:
            for i in range(n):
                weight = 1.0 - abs(i - (n - 1) / 2.0) / (n / 2.0)
                base = 0.06 + weight * 0.18
                self._bar_tgt[i] = base + random.uniform(0, 0.72)
        else:
            for i in range(n):
                self._bar_tgt[i] = 0.04

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = self.W // 2, self.H // 2 + self.CY_OFFSET

        self._paint_bg(p, cx, cy)
        self._paint_corners(p)
        self._paint_top_label(p)            # F R I D A Y + live clock
        self._paint_ticks(p, cx, cy)
        self._paint_concentric_rings(p, cx, cy)

        {
            State.IDLE:      self._paint_idle,
            State.LISTENING: self._paint_listening,
            State.THINKING:  self._paint_thinking,
            State.SPEAKING:  self._paint_speaking,
        }[self._state](p, cx, cy)

        self._paint_center_glyph(p, cx, cy)
        p.end()

    def _paint_bg(self, p, cx, cy):
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0, 0, self.W, self.H), 16, 16)
        p.fillPath(clip, _BG)

        # Soft center glow
        g = QRadialGradient(cx, cy, 115)
        g.setColorAt(0.0, QColor(0, 90, 190, 22))
        g.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillPath(clip, QBrush(g))

        p.setPen(QPen(_BORDER, 1.2))
        p.drawRoundedRect(QRectF(1, 1, self.W - 2, self.H - 2), 16, 16)

    def _paint_corners(self, p):
        """Small L-brackets at each corner — Iron Man HUD detail."""
        sz, mg = 14, 11
        p.setPen(QPen(QColor(0, 175, 245, 65), 1.4))
        for (x, y), (dx, dy) in [
            ((mg, mg),           ( 1,  1)),
            ((self.W - mg, mg),  (-1,  1)),
            ((mg, self.H - mg),  ( 1, -1)),
            ((self.W - mg, self.H - mg), (-1, -1)),
        ]:
            p.drawLine(x, y, x + dx * sz, y)
            p.drawLine(x, y, x, y + dy * sz)

    def _paint_ticks(self, p, cx, cy):
        """36 tick marks around the outer ring."""
        for i in range(36):
            angle = math.radians(i * 10)
            is_major = (i % 6 == 0)
            r_out = self.R2
            r_in  = r_out - (7 if is_major else 3)
            cos_a, sin_a = math.cos(angle), math.sin(angle)
            x1, y1 = cx + r_out * cos_a, cy + r_out * sin_a
            x2, y2 = cx + r_in  * cos_a, cy + r_in  * sin_a
            alpha = 75 if is_major else 38
            p.setPen(QPen(QColor(0, 175, 245, alpha), 1.2 if is_major else 0.8))
            p.drawLine(int(x1), int(y1), int(x2), int(y2))

    # ── Per-state paint ───────────────────────────────────────────────────────

    def _paint_idle(self, p, cx, cy):
        # The concentric rings already provide the breathing visual; the
        # idle state just tones everything down via window opacity (handled
        # in _apply_state). Nothing extra to paint here.
        pass

    def _paint_listening(self, p, cx, cy):
        # Brighten middle ring while listening
        p.setPen(QPen(QColor(0, 215, 255, 175), 2))
        p.drawEllipse(QPoint(cx, cy), self.R, self.R)

        # Waveform bars across the centre
        n = len(self._bars)
        bw, gap = 5, 2
        total = n * (bw + gap) - gap
        x0 = cx - total // 2
        max_h = 52   # smaller than before so it fits inside R0 ring

        for i, h in enumerate(self._bars):
            bh = max(3, int(h * max_h))
            x = x0 + i * (bw + gap)
            alpha = min(255, int(55 + h * 200))
            p.fillRect(x, cy - bh // 2, bw, bh, QColor(0, 195, 255, alpha))

        self._draw_label(p, cx, cy + 96, "LISTENING")

    def _paint_thinking(self, p, cx, cy):
        # Primary spinning arc on middle ring
        pen1 = QPen(QColor(0, 205, 255), 2.5)
        pen1.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen1)
        rect = QRectF(cx - self.R, cy - self.R, self.R * 2, self.R * 2)
        p.drawArc(rect, int((-self._arc + 90) * 16), 88 * 16)

        # Secondary arc (opposite)
        pen2 = QPen(QColor(0, 110, 195, 115), 1.8)
        pen2.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen2)
        p.drawArc(rect, int((-self._arc + 270) * 16), 48 * 16)

        # Bouncing dots beneath the centre glyph
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(3):
            phase = (self._tick * 0.09 + i * 0.85) % (2 * math.pi)
            y_off = int(-9 * abs(math.sin(phase)))
            alpha = int(130 + 125 * abs(math.sin(phase)))
            p.setBrush(QColor(0, 195, 255, alpha))
            p.drawEllipse(QPoint(cx - 16 + i * 16, cy + 38 + y_off), 4, 4)

        self._draw_label(p, cx, cy + 96, "THINKING")

    def _paint_speaking(self, p, cx, cy):
        # Expanding pulse ring — emanates outward from centre
        if self._pulse_r > 0:
            pr = int(self.R0 * 0.6 + self._pulse_r)
            p.setPen(QPen(QColor(0, 195, 255, self._pulse_a), 1))
            p.drawEllipse(QPoint(cx, cy), pr, pr)

        # Solid middle ring at higher intensity
        p.setPen(QPen(QColor(0, 210, 255, 195), 2))
        p.drawEllipse(QPoint(cx, cy), self.R, self.R)

        # Response text inside the inner ring
        if self._text:
            font = QFont("Consolas", 8)
            p.setFont(font)
            p.setPen(_TEXT)
            p.drawText(
                QRectF(cx - 78, cy - 36, 156, 72),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                self._text[:140],
            )

        self._draw_label(p, cx, cy + 96, "SPEAKING")

    def _draw_label(self, p, cx, y, text: str):
        font = QFont("Consolas", 7)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 3.0)
        p.setFont(font)
        p.setPen(_LABEL)
        p.drawText(QRect(cx - 65, y, 130, 15), Qt.AlignmentFlag.AlignCenter, text)

    # ── New paint helpers ─────────────────────────────────────────────────────

    def _paint_concentric_rings(self, p, cx, cy):
        """Three rotating concentric rings + a faint outermost halo.

        Different rotation speeds and break-points (gaps in the dashed
        stroke) make the rings feel alive without being noisy. This is
        the core visual cue that gives FRIDAY her Iron-Man look.
        """
        breathe = 0.5 + 0.5 * math.sin(self._idle_phase)
        idle = self._state == State.IDLE
        base_alpha = 30 if idle else 95

        # Outermost faint halo — always present, no rotation
        p.setPen(QPen(QColor(0, 130, 210, int(base_alpha * 0.45)), 1))
        p.drawEllipse(QPoint(cx, cy), self.R3, self.R3)

        # Outer ring — dashed, slow clockwise rotation
        pen = QPen(QColor(0, 175, 240, int(base_alpha * 0.85 + breathe * 25)), 1.2)
        pen.setDashPattern([3, 4])
        pen.setDashOffset(self._ring_phase_outer)
        p.setPen(pen)
        p.drawEllipse(QPoint(cx, cy), self.R2, self.R2)

        # Middle ring — mostly solid with a single rotating gap
        rect_mid = QRectF(cx - self.R, cy - self.R, self.R * 2, self.R * 2)
        p.setPen(QPen(QColor(0, 195, 255, base_alpha + int(breathe * 35)), 1.6))
        # Solid arc covering ~310 degrees, leaving a 50° "hatch" that rotates
        p.drawArc(rect_mid, int((-self._ring_phase_mid + 25) * 16), 310 * 16)

        # Inner ring — thin, fast inverse rotation, dotted
        pen_inner = QPen(QColor(0, 210, 255, base_alpha + int(breathe * 40)), 1.0)
        pen_inner.setStyle(Qt.PenStyle.DotLine)
        pen_inner.setDashOffset(self._ring_phase_inner)
        p.setPen(pen_inner)
        p.drawEllipse(QPoint(cx, cy), self.R0, self.R0)

    def _paint_center_glyph(self, p, cx, cy):
        """Central diamond/triangle glyph that pulses with state.

        Idle      → small dim diamond
        Listening → medium diamond, slight breathe
        Thinking  → small diamond (waveform/dots get focus)
        Speaking  → large bright diamond, hard pulse

        The waveform / response text in _paint_listening / _paint_speaking
        already overlays this region; the glyph is sized to peek out from
        underneath and act as the focal point during quiet states.
        """
        breathe = 0.5 + 0.5 * math.sin(self._idle_phase)
        if self._state == State.IDLE:
            r, alpha = 12 + breathe * 2.5, int(70 + breathe * 60)
        elif self._state == State.LISTENING:
            # Small — the waveform is the visual focus
            r, alpha = 9, 80
        elif self._state == State.THINKING:
            r, alpha = 8, 70
        else:                                          # SPEAKING
            r, alpha = 14 + breathe * 2.5, int(180 + breathe * 50)

        # Diamond outline
        path = QPainterPath()
        path.moveTo(cx,         cy - r)               # top
        path.lineTo(cx + r,     cy)                   # right
        path.lineTo(cx,         cy + r)               # bottom
        path.lineTo(cx - r,     cy)                   # left
        path.closeSubpath()

        # Soft glow fill (radial gradient)
        glow = QRadialGradient(cx, cy, r * 1.6)
        glow.setColorAt(0.0, QColor(120, 220, 255, min(255, alpha + 20)))
        glow.setColorAt(0.55, QColor(0, 195, 255, max(0, alpha - 60)))
        glow.setColorAt(1.0, QColor(0, 60, 140, 0))
        p.fillPath(path, QBrush(glow))

        # Crisp outline
        p.setPen(QPen(QColor(140, 230, 255, min(255, alpha + 40)), 1.4))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

    def _paint_top_label(self, p):
        """F R I D A Y wordmark + live date/time at the top of the HUD."""
        idle = self._state == State.IDLE

        # Wordmark — letter-spaced, slightly bold
        font = QFont("Consolas", 9)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 6.0)
        font.setBold(True)
        p.setFont(font)
        wm_alpha = 75 if idle else 175
        p.setPen(QColor(180, 230, 255, wm_alpha))
        p.drawText(
            QRect(0, 14, self.W, 16),
            Qt.AlignmentFlag.AlignCenter,
            "F R I D A Y",
        )

        # Date/time subtitle — e.g. "Wed, 13 May  •  14:31"
        sub_font = QFont("Consolas", 7)
        sub_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
        p.setFont(sub_font)
        sub_alpha = 55 if idle else 125
        p.setPen(QColor(0, 180, 240, sub_alpha))
        now = datetime.now()
        subtitle = now.strftime("%a, %d %b  \u2022  %H:%M")
        p.drawText(
            QRect(0, 32, self.W, 12),
            Qt.AlignmentFlag.AlignCenter,
            subtitle,
        )

    # ── Drag to reposition ────────────────────────────────────────────────────

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._drag_start = ev.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, ev):
        if ev.buttons() & Qt.MouseButton.LeftButton and self._drag_start:
            self.move(ev.globalPosition().toPoint() - self._drag_start)

    def mouseReleaseEvent(self, ev):
        self._drag_start = None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _snap(self):
        scr = QApplication.primaryScreen()
        if scr:
            g = scr.availableGeometry()
            self.move(g.right() - self.W - 22, g.bottom() - self.H - 22)

    def _force_topmost(self):
        """Re-assert HWND_TOPMOST without activating (no focus steal).

        Cheap insurance against another always-on-top window having stolen
        the top slot in z-order while we were hidden. Pure no-op on
        non-Windows.
        """
        if sys.platform != "win32":
            return
        try:
            HWND_TOPMOST = -1
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOACTIVATE = 0x0010
            SWP_SHOWWINDOW = 0x0040
            user32 = ctypes.windll.user32
            hwnd = int(self.winId())
            user32.SetWindowPos(
                hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
            )
        except Exception as exc:
            logger.debug("force_topmost failed: %s", exc)


# ── System tray ───────────────────────────────────────────────────────────────

def _make_tray_icon() -> QIcon:
    """Draw a simple 'J' circle as the tray icon."""
    pm = QPixmap(32, 32)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QBrush(QColor(0, 180, 255)))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(2, 2, 28, 28)
    p.setPen(QPen(QColor(4, 8, 18), 2))
    font = QFont("Consolas", 14, QFont.Weight.Bold)
    p.setFont(font)
    p.drawText(QRect(0, 0, 32, 32), Qt.AlignmentFlag.AlignCenter, "F")
    p.end()
    return QIcon(pm)


def setup_tray(app: QApplication, hud: JarvisHUD) -> QSystemTrayIcon:
    """Create a system tray icon with a right-click quit menu."""
    tray = QSystemTrayIcon(_make_tray_icon(), app)
    tray.setToolTip("FRIDAY — Online")

    menu = QMenu()
    show_action = menu.addAction("Show / Hide")
    show_action.triggered.connect(
        lambda: hud.hide() if hud.isVisible() else hud.show()
    )
    menu.addSeparator()
    quit_action = menu.addAction("Quit Friday")
    quit_action.triggered.connect(app.quit)

    tray.setContextMenu(menu)
    tray.show()
    return tray


# ── Standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import time, threading

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    hud = JarvisHUD()
    hud.show()          # show immediately for standalone test
    tray = setup_tray(app, hud)

    def _cycle():
        """Walk through all states so you can see the animations."""
        for state, text, secs in [
            (State.LISTENING, "", 4),
            (State.THINKING,  "", 4),
            (State.SPEAKING,  "Online. Ready to assist, Saksham.", 5),
            (State.IDLE,      "", 3),
        ]:
            hud.set_state(state, text)
            time.sleep(secs)

    t = threading.Thread(target=_cycle, daemon=True)
    t.start()
    sys.exit(app.exec())
