"""
Wake word + activation detection — 100% free, fully local.

Two activation methods (either one triggers Jarvis):

1. KEYWORD — "Jarvis"
   Uses openwakeword (free, offline neural wake-word engine).
   Model: the bundled 'hey_jarvis' openwakeword model (downloads ~5 MB on first run).
   Fallback: if openwakeword is unavailable, a lightweight Whisper-based keyword spotter
   is used instead (slightly higher CPU, same accuracy).

2. DOUBLE CLAP
   Pure energy-based detection — no model needed, zero cost.
   Detects two sharp transients (RMS spikes) within a 1-second window.

Install:
    pip install openwakeword sounddevice numpy

Usage (standalone test):
    python wake_word.py
"""

import logging
import time
from collections import deque
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────
SAMPLE_RATE = 16000          # Hz — openwakeword and Whisper both expect 16 kHz
CHUNK_DURATION_MS = 80       # ms per audio chunk fed to openwakeword
CHUNK_SIZE = int(SAMPLE_RATE * CHUNK_DURATION_MS / 1000)  # samples per chunk

# Clap detection tuning
# NOTE: 0.15 was way too sensitive for laptops — fan noise, keyboard clicks,
# and Windows system sounds all triggered false claps. 0.45 requires a real clap.
CLAP_RMS_THRESHOLD = 0.45    # fraction of max amplitude; lower = more sensitive
CLAP_MIN_GAP_S = 0.20        # min seconds between two clap peaks (avoid double-detect)
CLAP_MAX_GAP_S = 0.8         # max seconds between first and second clap
CLAP_WINDOW_S = 1.0          # rolling window we track clap history in

# Wake word confidence threshold (0–1)
WAKEWORD_THRESHOLD = 0.5

# Cooldown: after an activation, ignore further activations for this many seconds.
# Prevents rapid re-triggering while Jarvis is speaking or processing.
ACTIVATION_COOLDOWN_S = 4.0


# ─── Double-clap detector ─────────────────────────────────────────────────────

class ClapDetector:
    """
    Detects a double clap using RMS energy spikes.
    Feed 80 ms audio chunks via `process()`.
    Returns True from `process()` when a double clap is confirmed.
    """

    def __init__(
        self,
        rms_threshold: float = CLAP_RMS_THRESHOLD,
        min_gap: float = CLAP_MIN_GAP_S,
        max_gap: float = CLAP_MAX_GAP_S,
    ):
        self.rms_threshold = rms_threshold
        self.min_gap = min_gap
        self.max_gap = max_gap
        self._clap_times: deque = deque(maxlen=10)
        self._last_clap_time: float = 0.0
        self._in_peak = False

    def process(self, chunk: np.ndarray) -> bool:
        """
        Process one audio chunk.
        Returns True if a double clap was just detected.

        A clap must be:
          - A sharp RMS spike above threshold
          - Preceded and followed by relative quiet (the _in_peak flag)
          - Two claps within max_gap seconds
        """
        rms = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))

        now = time.monotonic()

        if rms > self.rms_threshold:
            if not self._in_peak and (now - self._last_clap_time) > self.min_gap:
                self._in_peak = True
                self._last_clap_time = now
                self._clap_times.append(now)
                logger.debug("Clap candidate at %.3f (rms=%.3f, threshold=%.3f)",
                             now, rms, self.rms_threshold)

                # Check for double clap: need exactly 2 claps within max_gap
                recent = [t for t in self._clap_times if now - t <= self.max_gap]
                if len(recent) >= 2:
                    self._clap_times.clear()
                    logger.info("Double clap detected! (rms=%.3f)", rms)
                    return True
        else:
            self._in_peak = False

        # Expire old clap events
        cutoff = now - CLAP_WINDOW_S
        while self._clap_times and self._clap_times[0] < cutoff:
            self._clap_times.popleft()

        return False


# ─── Wake word detector ───────────────────────────────────────────────────────

class WakeWordDetector:
    """
    Detects the "Jarvis" wake word using openwakeword.

    openwakeword ships a 'hey_jarvis' model trained on many "Jarvis" pronunciations.
    The model auto-downloads (~5 MB) on first use.

    Falls back to a Whisper-based keyword spotter if openwakeword isn't installed.
    """

    def __init__(self, sensitivity: float = WAKEWORD_THRESHOLD):
        self.sensitivity = sensitivity
        self._oww = None
        self._whisper_buffer: list[np.ndarray] = []
        self._whisper_buffer_s = 2.0    # seconds of audio to collect before whisper check
        self._use_oww = False
        self._init()

    def _init(self):
        try:
            from openwakeword.model import Model
            # Load the hey_jarvis model (downloads automatically on first run)
            self._oww = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
            self._use_oww = True
            logger.info("openwakeword loaded — wake word: 'Jarvis'")
        except ImportError:
            logger.warning(
                "openwakeword not installed. Falling back to Whisper keyword spotter. "
                "For lower CPU use: pip install openwakeword"
            )
        except Exception as exc:
            logger.warning("openwakeword init failed (%s). Using Whisper fallback.", exc)

    def process(self, chunk: np.ndarray) -> bool:
        """
        Feed one audio chunk (16 kHz, float32 or int16).
        Returns True when 'Jarvis' is detected.
        """
        if self._use_oww:
            return self._process_oww(chunk)
        return self._process_whisper(chunk)

    def _process_oww(self, chunk: np.ndarray) -> bool:
        # openwakeword expects int16
        if chunk.dtype != np.int16:
            chunk_i16 = (chunk * 32767).astype(np.int16)
        else:
            chunk_i16 = chunk

        self._oww.predict(chunk_i16)
        scores = self._oww.prediction_buffer.get("hey_jarvis", [0])
        score = float(scores[-1]) if scores else 0.0
        if score >= self.sensitivity:
            logger.info("Wake word 'Jarvis' detected (score=%.3f)", score)
            return True
        return False

    def _process_whisper(self, chunk: np.ndarray) -> bool:
        """Lightweight keyword spotter: accumulate audio then check with Whisper tiny."""
        if chunk.dtype == np.int16:
            chunk = chunk.astype(np.float32) / 32767.0

        self._whisper_buffer.append(chunk)
        buffered_s = len(self._whisper_buffer) * CHUNK_DURATION_MS / 1000

        if buffered_s < self._whisper_buffer_s:
            return False

        audio = np.concatenate(self._whisper_buffer)
        self._whisper_buffer.clear()

        try:
            from stt_engine import transcribe_audio
            text = transcribe_audio(audio, sample_rate=SAMPLE_RATE, model_name="tiny")
            if "jarvis" in text.lower():
                logger.info("Wake word 'Jarvis' detected via Whisper: %r", text)
                return True
        except Exception as exc:
            logger.debug("Whisper wake-word check failed: %s", exc)

        return False


# ─── Combined listener ────────────────────────────────────────────────────────

class ActivationListener:
    """
    Continuously listens on the microphone and fires a callback when:
      - The wake word "Jarvis" is spoken, OR
      - A double clap is detected

    Usage:
        def on_activate(source):
            print(f"Activated by: {source}")   # "wake_word" or "clap"

        listener = ActivationListener(on_activate)
        listener.start()   # blocking; run in a thread
    """

    def __init__(
        self,
        on_activate: Callable[[str], None],
        sensitivity: float = WAKEWORD_THRESHOLD,
        rms_threshold: float = CLAP_RMS_THRESHOLD,
        cooldown: float = ACTIVATION_COOLDOWN_S,
    ):
        self.on_activate = on_activate
        self._ww = WakeWordDetector(sensitivity=sensitivity)
        self._clap = ClapDetector(rms_threshold=rms_threshold)
        self._running = False
        self._cooldown = cooldown
        self._last_activation: float = 0.0   # monotonic timestamp

    def start(self) -> None:
        """Block and listen. Call stop() from another thread to exit."""
        try:
            import sounddevice as sd
        except ImportError:
            raise RuntimeError("sounddevice not installed. Run: pip install sounddevice")

        self._running = True
        logger.info(
            "Activation listener started. Say 'Jarvis' or double-clap to activate. "
            "(clap threshold=%.2f, cooldown=%.1fs)",
            self._clap.rms_threshold, self._cooldown,
        )

        def audio_callback(indata, frames, time_info, status):
            if status:
                logger.debug("sounddevice status: %s", status)
            chunk = indata[:, 0].copy()   # mono

            # Cooldown guard: don't fire again if we just activated
            now = time.monotonic()
            if (now - self._last_activation) < self._cooldown:
                return

            if self._ww.process(chunk):
                self._last_activation = now
                self.on_activate("wake_word")
            elif self._clap.process(chunk):
                self._last_activation = now
                self.on_activate("clap")

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=CHUNK_SIZE,
            callback=audio_callback,
        ):
            while self._running:
                time.sleep(0.1)

    def stop(self) -> None:
        self._running = False


# ─── Standalone test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    def on_activate(source: str):
        print(f"\n*** JARVIS ACTIVATED via {source.upper()} ***\n")

    print("Listening... Say 'Jarvis' or double-clap. Ctrl+C to stop.")
    listener = ActivationListener(on_activate)
    try:
        listener.start()
    except KeyboardInterrupt:
        listener.stop()
        print("Stopped.")
