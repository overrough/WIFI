"""
Text-input fallback for JARVIS.

When voice fails (noisy room, microphone busy, accent edge cases), Sir can
press Ctrl+Shift+T anywhere to type a command instead. The same handler
that processes voice input runs the typed command.

Visual: small frameless dialog matching the HUD palette, slides in just
above the HUD, dismisses on Esc / Enter / focus loss.
"""

import logging
from typing import Callable, Optional

from PyQt6.QtCore import Qt, QPoint, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPalette
from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget,
)

logger = logging.getLogger(__name__)


class TextInput(QWidget):
    """Frameless single-line command input.

    Spawns just above the HUD. Press Enter to submit, Esc to cancel.
    The submit callback receives the typed text (str) on the Qt main thread;
    forward it to the same handler that processes voice transcripts.
    """

    _sig_show = pyqtSignal()

    def __init__(self, on_submit: Callable[[str], None], hud: Optional[QWidget] = None):
        super().__init__(flags=(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        ))
        self._on_submit = on_submit
        self._hud = hud

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(420, 56)

        # Background frame
        wrapper = QWidget(self)
        wrapper.setObjectName("wrapper")
        wrapper.setStyleSheet("""
            QWidget#wrapper {
                background: rgba(4, 8, 18, 235);
                border: 1px solid rgba(0, 175, 245, 90);
                border-radius: 12px;
            }
        """)
        wrapper.setFixedSize(self.size())

        # Layout
        wlay = QHBoxLayout(wrapper)
        wlay.setContentsMargins(16, 8, 14, 8)
        wlay.setSpacing(10)

        prompt = QLabel("›")
        font = QFont("Consolas", 16, QFont.Weight.Bold)
        prompt.setFont(font)
        prompt.setStyleSheet("color: rgb(0, 195, 255);")
        wlay.addWidget(prompt)

        self.input = QLineEdit()
        self.input.setFont(QFont("Consolas", 11))
        self.input.setPlaceholderText("Type a command, Sir…")
        self.input.setStyleSheet("""
            QLineEdit {
                background: transparent;
                color: rgb(185, 228, 255);
                border: none;
                selection-background-color: rgba(0, 175, 245, 80);
            }
        """)
        self.input.returnPressed.connect(self._submit)
        wlay.addWidget(self.input)

        self._sig_show.connect(self._show_centered)
        self.hide()

    # ── Public, thread-safe ──────────────────────────────────────────────────

    def request_show(self) -> None:
        """Call from any thread to show the input box."""
        self._sig_show.emit()

    # ── Internals ────────────────────────────────────────────────────────────

    def _show_centered(self) -> None:
        # Position just above the HUD if we have one, else screen-center-bottom
        scr = QApplication.primaryScreen().availableGeometry()
        if self._hud is not None and self._hud.isVisible():
            hud_pos = self._hud.pos()
            x = hud_pos.x() + (self._hud.width() - self.width()) // 2
            y = hud_pos.y() - self.height() - 12
        else:
            x = scr.right() - self.width() - 22
            y = scr.bottom() - self.height() - 22 - 320   # above where HUD would be
        self.move(max(8, x), max(8, y))
        self.input.clear()
        self.show()
        self.raise_()
        self.activateWindow()
        # Make sure it actually grabs focus on Windows
        QTimer.singleShot(50, lambda: self.input.setFocus(Qt.FocusReason.ActiveWindowFocusReason))

    def _submit(self) -> None:
        text = self.input.text().strip()
        self.hide()
        if not text:
            return
        try:
            self._on_submit(text)
        except Exception as exc:
            logger.error("Text-input submit failed: %s", exc)

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(ev)

    def focusOutEvent(self, ev):
        # Hide when user clicks elsewhere
        self.hide()
        super().focusOutEvent(ev)
