"""
Global hotkey listener — Ctrl+Shift+J anywhere on the desktop activates JARVIS.

Uses the `keyboard` library for true global capture on Windows. On Linux/Mac
this requires extra permissions, so it's wrapped in best-effort try/except.

Spec §2: "Hotkey Ctrl+Shift+J as third option" — alongside wake word + clap.
"""

import logging
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class HotkeyListener:
    """
    Registers a global hotkey and fires the callback when pressed.

    Usage:
        listener = HotkeyListener(on_activate=lambda src: print(src))
        listener.start()   # non-blocking
        ...
        listener.stop()
    """

    def __init__(
        self,
        on_activate: Callable[[str], None],
        hotkey: str = "ctrl+shift+j",
    ):
        self.on_activate = on_activate
        self.hotkey = hotkey
        self._stop_event = threading.Event()
        self._registered = False

    def start(self) -> None:
        """Register the global hotkey. Non-blocking."""
        try:
            import keyboard  # type: ignore
        except ImportError:
            logger.warning(
                "keyboard package not installed. Hotkey disabled. "
                "Run: pip install keyboard"
            )
            return
        except Exception as exc:
            logger.warning("Could not load keyboard module: %s", exc)
            return

        try:
            keyboard.add_hotkey(self.hotkey, self._fire)
            self._registered = True
            logger.info("Global hotkey registered: %s", self.hotkey.upper())
        except Exception as exc:
            # On Linux this often requires running as root; on macOS it
            # requires Accessibility permissions. Fail soft — voice still works.
            logger.warning(
                "Could not register hotkey %s (%s). "
                "Voice activation still works.",
                self.hotkey, exc,
            )

    def _fire(self) -> None:
        try:
            self.on_activate("hotkey")
        except Exception as exc:
            logger.error("Hotkey callback failed: %s", exc)

    def stop(self) -> None:
        if not self._registered:
            return
        try:
            import keyboard  # type: ignore
            keyboard.remove_hotkey(self.hotkey)
        except Exception:
            pass
        self._registered = False
