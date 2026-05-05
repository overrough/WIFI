"""
Jarvis HUD — futuristic floating glass overlay.

Visual states:
  IDLE      → dim breathing ring, barely visible
  LISTENING → cyan waveform bars + ring glow
  THINKING  → spinning arc + bouncing dots
  SPEAKING  → pulsing rings + response text

Thread-safe: call set_state() from any thread via Qt signals.
Draggable: click-drag to reposition.
System tray: right-click tray icon to quit.
"""

import math
import random
import sys
from enum import Enum

from PyQt6.QtCore import QPoint, QRect, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QIcon, QPainter, QPainterPath, QPen,
    QPixmap, QRadialGradient,
)
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget


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
    R  = 108    # inner ring radius
    R2 = 126    # outer ring radius (tick marks)

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

        self.show()

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
        self._state = State(val)
        self._fade_tgt = {
            State.IDLE:      0.22,
            State.LISTENING: 1.0,
            State.THINKING:  1.0,
            State.SPEAKING:  1.0,
        }[self._state]
        if self._state != State.SPEAKING:
            pass    # keep text until explicitly cleared

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
        cx, cy = self.W // 2, self.H // 2

        self._paint_bg(p, cx, cy)
        self._paint_corners(p)
        self._paint_ticks(p, cx, cy)

        {
            State.IDLE:      self._paint_idle,
            State.LISTENING: self._paint_listening,
            State.THINKING:  self._paint_thinking,
            State.SPEAKING:  self._paint_speaking,
        }[self._state](p, cx, cy)

        self._paint_wordmark(p, cx)
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
        breathe = 0.5 + 0.5 * math.sin(self._idle_phase)
        alpha = int(22 + breathe * 38)
        p.setPen(QPen(QColor(0, 155, 215, alpha), 1.5))
        p.setPen(QPen(QColor(0, 155, 215, alpha), 1.5,
                      Qt.PenStyle.DotLine))
        p.drawEllipse(QPoint(cx, cy), self.R, self.R)

    def _paint_listening(self, p, cx, cy):
        # Glowing ring
        p.setPen(QPen(QColor(0, 215, 255, 155), 2))
        p.drawEllipse(QPoint(cx, cy), self.R, self.R)

        # Waveform bars
        n = len(self._bars)
        bw, gap = 5, 2
        total = n * (bw + gap) - gap
        x0 = cx - total // 2
        max_h = 68

        for i, h in enumerate(self._bars):
            bh = max(3, int(h * max_h))
            x = x0 + i * (bw + gap)
            alpha = min(255, int(55 + h * 200))
            p.fillRect(x, cy - bh // 2, bw, bh, QColor(0, 195, 255, alpha))

        self._draw_label(p, cx, cy + 88, "LISTENING")

    def _paint_thinking(self, p, cx, cy):
        # Dim base ring
        p.setPen(QPen(QColor(0, 75, 155, 48), 1.5))
        p.drawEllipse(QPoint(cx, cy), self.R, self.R)

        # Primary spinning arc
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

        # Bouncing dots
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(3):
            phase = (self._tick * 0.09 + i * 0.85) % (2 * math.pi)
            y_off = int(-9 * abs(math.sin(phase)))
            alpha = int(130 + 125 * abs(math.sin(phase)))
            p.setBrush(QColor(0, 195, 255, alpha))
            p.drawEllipse(QPoint(cx - 16 + i * 16, cy + 10 + y_off), 4, 4)

        self._draw_label(p, cx, cy + 88, "THINKING")

    def _paint_speaking(self, p, cx, cy):
        # Expanding pulse ring
        if self._pulse_r > 0:
            pr = int(self.R * 0.35 + self._pulse_r)
            p.setPen(QPen(QColor(0, 195, 255, self._pulse_a), 1))
            p.drawEllipse(QPoint(cx, cy), pr, pr)

        # Solid outer ring
        p.setPen(QPen(QColor(0, 210, 255, 175), 2))
        p.drawEllipse(QPoint(cx, cy), self.R, self.R)

        # Response text
        if self._text:
            font = QFont("Consolas", 8)
            p.setFont(font)
            p.setPen(_TEXT)
            p.drawText(
                QRectF(cx - 90, cy - 48, 180, 96),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                self._text[:160],
            )

        self._draw_label(p, cx, cy + 88, "SPEAKING")

    def _draw_label(self, p, cx, y, text: str):
        font = QFont("Consolas", 7)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 3.0)
        p.setFont(font)
        p.setPen(_LABEL)
        p.drawText(QRect(cx - 65, y, 130, 15), Qt.AlignmentFlag.AlignCenter, text)

    def _paint_wordmark(self, p, cx):
        font = QFont("Consolas", 8)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 5.0)
        font.setBold(True)
        p.setFont(font)
        alpha = 32 if self._state == State.IDLE else 68
        p.setPen(QColor(0, 165, 235, alpha))
        p.drawText(
            QRect(0, self.H - 25, self.W, 17),
            Qt.AlignmentFlag.AlignCenter,
            "J A R V I S",
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
    p.drawText(QRect(0, 0, 32, 32), Qt.AlignmentFlag.AlignCenter, "J")
    p.end()
    return QIcon(pm)


def setup_tray(app: QApplication, hud: JarvisHUD) -> QSystemTrayIcon:
    """Create a system tray icon with a right-click quit menu."""
    tray = QSystemTrayIcon(_make_tray_icon(), app)
    tray.setToolTip("JARVIS — Online")

    menu = QMenu()
    show_action = menu.addAction("Show / Hide")
    show_action.triggered.connect(
        lambda: hud.hide() if hud.isVisible() else hud.show()
    )
    menu.addSeparator()
    quit_action = menu.addAction("Quit Jarvis")
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
