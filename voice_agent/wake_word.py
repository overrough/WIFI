"""
Wake word + activation detection — 100% free, fully local.

Three activation methods (any one triggers Friday):

1. PRIMARY KEYWORD — "Jarvis"
   Uses openwakeword (free, offline neural wake-word engine), real-time.
   Model: the bundled 'hey_jarvis' openwakeword model. First run auto-
   downloads the ONNX weights via openwakeword.utils.download_models()
   if they aren't already on disk — important: the pip package ships the
   loader code but NOT the model weights, so without the auto-download
   the listener silently falls back to the slow Whisper spotter.

2. SECONDARY KEYWORD — "Friday"
   Runs in parallel via a buffered Whisper-tiny check every ~2 seconds.
   Slower than openwakeword (2 s latency vs ~100 ms) but lets Sir use
   either name. When the official Friday openwakeword model is trained
   in a future session, this will be replaced by a real-time detector.

3. DOUBLE CLAP
   Pure energy-based detection — no model needed, zero cost.
   Detects two sharp transients (RMS spikes) within a 1-second window.

The ActivationListener owns the ONLY sounddevice InputStream in the
voice agent. The recorder in main.py pulls audio chunks from a queue
the listener fills — this avoids the silent-recording bug that happened
when two simultaneous InputStreams clashed on Windows WASAPI shared mode.

Install:
    pip install openwakeword sounddevice numpy

Usage (standalone test):
    python wake_word.py
"""

import logging
import queue
import threading
import time
from collections import deque
from typing import Callable, Optional, Sequence

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

# Wake word confidence thresholds (0–1).
#
# History:
#   - Started at 0.5: missed Sir's accent on the first call almost always
#   - Dropped to 0.35 + required 2 consecutive frames: false-positive rate
#     stayed low but introduced ~80 ms of latency AND the first "Jarvis"
#     still got eaten because frame 1 wasn't enough on its own.
#   - May 18: graduated confidence — fire immediately on high-confidence
#     frames, only require the 2-frame streak for marginal scores.
#
# Empirically on Sir's mic + accent, "Jarvis" enunciated clearly produces
# scores in the 0.55-0.75 range on the FIRST frame. Soft-spoken or rapid
# "Jarvis" scores 0.35-0.55. The split at HIGH_CONFIDENCE lets us catch
# the clear case immediately without raising the false-positive rate for
# the marginal case (which still needs the 2-frame confirm).
WAKEWORD_THRESHOLD = 0.35
WAKEWORD_HIGH_CONFIDENCE = 0.55  # fire on a single frame if score ≥ this

# Secondary keywords detected via the slower Whisper path. Sir wants
# both "Jarvis" (the original) and "Friday" (the persona name) to wake
# her up. "jarvis" is included here as a SAFETY NET in case openwakeword
# fails to load — normally the fast neural path catches it first.
SECONDARY_KEYWORDS = ("friday", "jarvis")

# Cooldown: after an activation, ignore further activations for this many
# seconds. Prevents rapid re-triggering while Jarvis is speaking. Lowered
# 4.0 → 2.5 so that if Sir calls "Jarvis" again because the first was
# missed, the second isn't gated by a still-active cooldown. The
# `_is_speaking` flag in main.py is the real guard during TTS playback.
ACTIVATION_COOLDOWN_S = 2.5


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

    def __init__(
        self,
        sensitivity: float = WAKEWORD_THRESHOLD,
        secondary_keywords: Sequence[str] = SECONDARY_KEYWORDS,
    ):
        self.sensitivity = sensitivity
        self.secondary_keywords = tuple(k.lower() for k in secondary_keywords)
        self._oww = None
        self._whisper_buffer: list[np.ndarray] = []
        self._whisper_buffer_s = 2.0    # seconds of audio to collect before whisper check
        self._use_oww = False
        # Counter for consecutive over-threshold frames (see _process_oww).
        # Two in a row at threshold 0.35 is roughly equivalent to one at 0.5
        # for false-positive rate, but is much more forgiving of accent.
        self._oww_streak = 0
        self._init()

    def _init(self):
        try:
            from openwakeword.model import Model
            try:
                self._oww = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
            except Exception as load_exc:
                # Common case on fresh installs: openwakeword's pip package
                # ships the loader code but NOT the model weights. The first
                # Model(...) call fails with NoSuchFile. We auto-download
                # the missing ONNX files and retry once. Without this, the
                # listener silently falls back to the slow Whisper spotter
                # and Sir has to say "Jarvis" 5 times before activation.
                logger.warning("openwakeword load failed (%s). Attempting model download...", load_exc)
                try:
                    from openwakeword.utils import download_models
                    download_models(["hey_jarvis_v0.1"])
                    self._oww = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
                except Exception as dl_exc:
                    raise dl_exc
            self._use_oww = True
            logger.info("openwakeword loaded — wake word: 'Jarvis'")
        except ImportError:
            logger.warning(
                "openwakeword not installed. Falling back to Whisper keyword spotter. "
                "For lower CPU use: pip install openwakeword"
            )
        except Exception as exc:
            logger.warning("openwakeword init failed (%s). Using Whisper fallback.", exc)

    def process_fast(self, chunk: np.ndarray) -> bool:
        """Fast path — openwakeword only. Safe to call from the audio
        callback because openwakeword.predict on an 80 ms chunk runs in
        a few milliseconds.

        The slow Whisper path is NOT called here — it lives on a separate
        background thread (`ActivationListener._whisper_loop`) so it can
        never block the sounddevice audio callback. Blocking the callback
        for >80 ms drops audio chunks, breaks wake-word detection, and was
        the actual cause of the May 14 'I have to say Jarvis 5 times' bug.
        """
        if self._use_oww:
            return self._process_oww(chunk)
        return False

    def _process_oww(self, chunk: np.ndarray) -> bool:
        # openwakeword expects int16
        if chunk.dtype != np.int16:
            chunk_i16 = (chunk * 32767).astype(np.int16)
        else:
            chunk_i16 = chunk

        self._oww.predict(chunk_i16)
        scores = self._oww.prediction_buffer.get("hey_jarvis", [0])
        score = float(scores[-1]) if scores else 0.0

        # Graduated confidence:
        #   - score ≥ HIGH_CONFIDENCE (0.55): trust a single frame.
        #     This is the path that fixes "Jarvis on the first try."
        #   - score in [THRESHOLD, HIGH_CONFIDENCE): require two
        #     consecutive frames. Catches mumbled / fast pronunciations
        #     without raising false-positive rate (random ambient sound
        #     rarely scores >= 0.35 twice in a row).
        if score >= WAKEWORD_HIGH_CONFIDENCE:
            self._oww_streak = 0
            logger.info(
                "Wake word 'Jarvis' detected — single-frame trigger "
                "(score=%.3f, high-confidence)", score,
            )
            return True
        if score >= self.sensitivity:
            self._oww_streak += 1
            if self._oww_streak >= 2:
                self._oww_streak = 0
                logger.info(
                    "Wake word 'Jarvis' detected — 2-frame confirm "
                    "(score=%.3f)", score,
                )
                return True
        else:
            self._oww_streak = 0
        return False

    def detect_in_audio(self, audio: np.ndarray) -> Optional[str]:
        """Run Whisper-tiny on a buffered audio segment and look for any
        secondary keyword. Returns the keyword that matched, or None.

        This is intended to be called from a BACKGROUND thread, not from
        the audio callback — transcription takes 200–500 ms on CPU which
        is fatal to the audio stream if invoked synchronously.
        """
        if audio.size == 0:
            return None

        # Skip silent buffers (no point transcribing pure room tone)
        rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
        if rms < 0.02:
            return None

        try:
            from stt_engine import transcribe_audio
            text = transcribe_audio(audio, sample_rate=SAMPLE_RATE, model_name="tiny")
            text_lower = text.lower()
            for kw in self.secondary_keywords:
                if kw in text_lower:
                    logger.info("Wake word %r detected via Whisper: %r", kw, text)
                    return kw
        except Exception as exc:
            logger.debug("Whisper wake-word check failed: %s", exc)

        return None


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
        secondary_keywords: Sequence[str] = SECONDARY_KEYWORDS,
    ):
        self.on_activate = on_activate
        self._ww = WakeWordDetector(
            sensitivity=sensitivity, secondary_keywords=secondary_keywords
        )
        self._clap = ClapDetector(rms_threshold=rms_threshold)
        self._running = False
        self._cooldown = cooldown
        self._last_activation: float = 0.0   # monotonic timestamp

        # ── Shared audio stream / recorder hand-off ─────────────────────
        # The listener owns the ONE sd.InputStream. When the recorder in
        # main.py needs audio, it calls start_recording() and pulls chunks
        # from this queue — instead of opening its own InputStream which
        # caused silent (rms=0) recordings on Windows WASAPI shared mode.
        self._record_queue: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=200)
        self._recording = False

        # ── Whisper background detector ─────────────────────────────────────────
        # Whisper transcription on the audio_callback thread blocks the
        # mic stream for 200–500 ms every time it runs. We push 80 ms
        # chunks to this queue from the callback (non-blocking) and a
        # dedicated daemon thread accumulates ~2 s buffers and runs the
        # tiny model on them. When a secondary keyword is detected the
        # thread invokes on_activate("wake_word") just like the fast path.
        # Sized for ~16 s of backlog: if the thread can't keep up we drop
        # oldest chunks instead of blocking the audio callback.
        self._whisper_audio_queue: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=200)
        self._whisper_running = False
        self._whisper_thread: Optional[threading.Thread] = None
        self._whisper_buffer_s = 2.0

    # ── Recorder hand-off API ──────────────────────────────────────────────

    def start_recording(self) -> None:
        """Switch the audio callback into recording mode.

        While recording, audio chunks are pushed to `self._record_queue`
        instead of being fed to the wake-word / clap detectors. Any stale
        chunks left from a previous recording are drained first so the
        recorder doesn't see audio captured before activation fired.
        """
        while True:
            try:
                self._record_queue.get_nowait()
            except queue.Empty:
                break
        self._recording = True

    def stop_recording(self) -> None:
        """Switch back to wake-word / clap detection mode."""
        self._recording = False

    # ── Whisper background detector ─────────────────────────────────────────────

    def _whisper_loop(self) -> None:
        """Background thread that runs Whisper-tiny on accumulated audio.

        Pulls 80 ms chunks from `self._whisper_audio_queue`, accumulates
        until ~2 s, and asks `WakeWordDetector.detect_in_audio()` whether
        any secondary keyword fired. On a hit, calls `on_activate` exactly
        like the openwakeword fast path — same cooldown, same dispatcher.
        """
        chunk_dur = CHUNK_DURATION_MS / 1000.0
        buffer: list[np.ndarray] = []
        buffered_s = 0.0

        while self._whisper_running:
            try:
                chunk = self._whisper_audio_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if chunk.dtype == np.int16:
                chunk = chunk.astype(np.float32) / 32767.0

            buffer.append(chunk)
            buffered_s += chunk_dur
            if buffered_s < self._whisper_buffer_s:
                continue

            audio = np.concatenate(buffer)
            buffer.clear()
            buffered_s = 0.0

            # Cheap cooldown check before paying the Whisper cost
            now = time.monotonic()
            if (now - self._last_activation) < self._cooldown:
                continue
            # Skip while the user is being recorded — their command audio
            # is meant for the recorder, not for fresh wake-word detection.
            if self._recording:
                continue

            kw = self._ww.detect_in_audio(audio)
            if kw is not None:
                self._last_activation = time.monotonic()
                try:
                    self.on_activate("wake_word")
                except Exception as exc:
                    logger.error("on_activate raised from Whisper thread: %s", exc)

    def _enqueue_for_whisper(self, chunk: np.ndarray) -> None:
        """Non-blocking push to the Whisper queue. Drops oldest on overflow."""
        try:
            self._whisper_audio_queue.put_nowait(chunk)
        except queue.Full:
            try:
                self._whisper_audio_queue.get_nowait()
                self._whisper_audio_queue.put_nowait(chunk)
            except (queue.Empty, queue.Full):
                pass

    def read_chunk(self, timeout: float = 1.0) -> Optional[np.ndarray]:
        """Pull one audio chunk (~80 ms float32 mono @16 kHz) from the queue.

        Returns None on timeout so the recorder can decide whether to keep
        waiting or give up.
        """
        try:
            return self._record_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def start(self) -> None:
        """Block and listen. Call stop() from another thread to exit."""
        try:
            import sounddevice as sd
        except ImportError:
            raise RuntimeError("sounddevice not installed. Run: pip install sounddevice")

        self._running = True
        logger.info(
            "Activation listener started. Say 'Jarvis' (fast), 'Friday' "
            "(slower, ~2s buffered), or double-clap to activate. "
            "(clap threshold=%.2f, cooldown=%.1fs)",
            self._clap.rms_threshold, self._cooldown,
        )

        # Spin up the Whisper background detector. Daemon = dies with the
        # process; explicit stop happens in self.stop().
        self._whisper_running = True
        self._whisper_thread = threading.Thread(
            target=self._whisper_loop,
            name="FridayWhisperDetector",
            daemon=True,
        )
        self._whisper_thread.start()

        def audio_callback(indata, frames, time_info, status):
            if status:
                logger.debug("sounddevice status: %s", status)
            chunk = indata[:, 0].copy()   # mono

            # Recording mode: route audio to the recorder's queue and skip
            # wake-word detection. The recorder will set self._recording
            # to False when it's done, restoring normal listening.
            if self._recording:
                try:
                    self._record_queue.put_nowait(chunk)
                except queue.Full:
                    # Drop the oldest chunk to make room — better than
                    # blocking the audio callback (which would underrun
                    # the OS buffer and produce real silence).
                    try:
                        self._record_queue.get_nowait()
                        self._record_queue.put_nowait(chunk)
                    except (queue.Empty, queue.Full):
                        pass
                return

            # Cooldown guard: don't fire again if we just activated
            now = time.monotonic()
            if (now - self._last_activation) < self._cooldown:
                return

            # Fast path — openwakeword (a few milliseconds at most).
            if self._ww.process_fast(chunk):
                self._last_activation = now
                self.on_activate("wake_word")
                return

            # Clap detector — microseconds.
            if self._clap.process(chunk):
                self._last_activation = now
                self.on_activate("clap")
                return

            # Slow path — hand the chunk to the Whisper background thread
            # and return immediately. NEVER call Whisper here; it would
            # block the audio callback for hundreds of milliseconds and
            # cause the wake-word reliability bug.
            self._enqueue_for_whisper(chunk)

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=CHUNK_SIZE,
            callback=audio_callback,
        ):
            while self._running:
                time.sleep(0.1)

        # Tear down Whisper thread when the stream exits
        self._whisper_running = False
        if self._whisper_thread is not None:
            self._whisper_thread.join(timeout=2.0)
            self._whisper_thread = None

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
