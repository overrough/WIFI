"""
Jarvis Voice Agent — fully local, completely free pipeline.

Flow:
  Mic ──► Wake word / Double clap
              │
              ▼
         Record until silence
              │
              ▼
         faster-whisper (STT, local)
              │
              ▼
         Jarvis backend API (streaming)
              │
              ▼
         edge-tts (TTS, free)  ──► Speaker

No LiveKit, no ElevenLabs key, no OpenAI key required.

Optional upgrades (set in .env):
  TTS_PROVIDER=elevenlabs + ELEVENLABS_API_KEY  → richer voice
  WHISPER_MODEL=small                           → better accuracy (uses more RAM)

Setup:
  pip install -r requirements.txt
  python main.py

Then say "Jarvis" or double-clap to activate.
"""

import asyncio
import atexit
import json
import logging
import os
import sys
import threading
from typing import Optional

import numpy as np
from dotenv import load_dotenv
from pathlib import Path

# Load backend .env so voice agent shares the same config
_backend_env = Path(__file__).parent.parent / "backend" / ".env"
load_dotenv(_backend_env)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("jarvis.voice")

# ─── Config ───────────────────────────────────────────────────────────────────

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")
API_KEY = os.getenv("JARVIS_API_KEY", "local-dev-key")
DEFAULT_MODE = os.getenv("JARVIS_DEFAULT_MODE", "work")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "edge-tts")
EDGE_TTS_VOICE = os.getenv("EDGE_TTS_VOICE", "en-GB-RyanNeural")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")
WAKE_WORD_SENSITIVITY = float(os.getenv("WAKE_WORD_SENSITIVITY", "0.5"))
CLAP_RMS_THRESHOLD = float(os.getenv("CLAP_RMS_THRESHOLD", "0.45"))

# Recording settings
SAMPLE_RATE = 16000
SILENCE_THRESHOLD = 0.02      # RMS below this = silence
SILENCE_DURATION_S = 1.5      # stop recording after this many seconds of silence
MAX_RECORD_S = 30             # hard cap on recording length

# ─── Session state ────────────────────────────────────────────────────────────

_conversation_id: Optional[str] = None
_is_speaking = False          # TTS guard — don't activate while Jarvis is talking
_hud = None                   # JarvisHUD instance (set by main() if PyQt6 available)


# ─── Backend API ──────────────────────────────────────────────────────────────

async def start_session() -> str:
    import httpx
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{BACKEND_URL}/chat/start",
            json={"mode": DEFAULT_MODE},
            headers={"X-API-Key": API_KEY},
        )
        resp.raise_for_status()
        return resp.json()["conversation_id"]


async def call_jarvis(conversation_id: str, user_message: str) -> str:
    """Stream a message to the Jarvis backend and return the full response.

    Surfaces backend errors (type:"error" SSE events) instead of silently
    swallowing them, so the user actually hears what went wrong instead of
    the generic 'I'm not sure how to respond' fallback.
    """
    import httpx
    parts: list[str] = []
    error_msg: str | None = None
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST",
            f"{BACKEND_URL}/chat/{conversation_id}/message",
            json={"content": user_message},
            headers={"X-API-Key": API_KEY},
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    try:
                        event = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    etype = event.get("type")
                    if etype == "token":
                        parts.append(event.get("content", ""))
                    elif etype == "error":
                        error_msg = event.get("content", "Unknown backend error.")
                        logger.error("Backend stream error: %s", error_msg)
                        break
                    elif etype == "done":
                        break
    if error_msg and not parts:
        # Surface the actual error in JARVIS voice so Sir can see what broke
        return f"Sir, the backend reported a problem. {error_msg[:200]}"
    return "".join(parts)


async def fetch_pending(force_brief: bool = False) -> dict:
    """
    Ask the backend if it has anything proactive to say:
      - A morning/evening briefing (first activation of the day), OR
      - Queued notifications from background routines.
    Returns {} on failure so the caller can continue normally.
    """
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{BACKEND_URL}/jarvis/pending",
                params={"force_brief": str(force_brief).lower()},
                headers={"X-API-Key": API_KEY},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.debug("fetch_pending failed: %s", exc)
        return {}


# ─── Audio recording ──────────────────────────────────────────────────────────

MIN_SPEECH_RMS = 0.03
MIN_SPEECH_RATIO = 0.15


def record_until_silence(on_chunk=None) -> np.ndarray:
    """
    Record microphone audio until the user stops speaking.
    Returns float32 numpy array at SAMPLE_RATE, or empty array if no speech.
    on_chunk(rms: float) is called for each 100 ms chunk if provided.
    """
    try:
        import sounddevice as sd
    except ImportError:
        raise RuntimeError("sounddevice not installed. Run: pip install sounddevice")

    logger.info("Recording... (speak now)")
    frames = []
    silent_chunks = 0
    speech_chunks = 0
    chunk_size = int(SAMPLE_RATE * 0.1)
    chunks_for_silence = int(SILENCE_DURATION_S / 0.1)
    max_chunks = int(MAX_RECORD_S / 0.1)

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        blocksize=chunk_size) as stream:
        while len(frames) < max_chunks:
            chunk, _ = stream.read(chunk_size)
            mono = chunk[:, 0]
            frames.append(mono.copy())
            rms = float(np.sqrt(np.mean(mono ** 2)))

            if on_chunk:
                on_chunk(rms)

            if rms < SILENCE_THRESHOLD:
                silent_chunks += 1
                if silent_chunks >= chunks_for_silence and len(frames) > 5:
                    break
            else:
                silent_chunks = 0
                speech_chunks += 1

    if not frames:
        return np.array([], dtype=np.float32)

    audio = np.concatenate(frames)
    total_rms = float(np.sqrt(np.mean(audio ** 2)))
    speech_ratio = speech_chunks / max(len(frames), 1)

    logger.info(
        "Recorded %.1f seconds. RMS=%.4f, speech_ratio=%.2f",
        len(audio) / SAMPLE_RATE, total_rms, speech_ratio,
    )

    if total_rms < MIN_SPEECH_RMS or speech_ratio < MIN_SPEECH_RATIO:
        logger.info(
            "Audio too quiet (rms=%.4f, ratio=%.2f) — skipping.",
            total_rms, speech_ratio,
        )
        return np.array([], dtype=np.float32)

    return audio


# ─── HUD helpers ──────────────────────────────────────────────────────────────

def _hud_state(state_name: str, text: str = ""):
    """Update HUD state from any thread (no-op if HUD not available)."""
    if _hud is None:
        return
    try:
        from hud import State
        _hud.set_state(State(state_name), text)
    except Exception:
        pass


# ─── Main interaction loop ────────────────────────────────────────────────────

async def handle_activation(source: str) -> None:
    global _conversation_id, _is_speaking

    logger.info("Activated via %s", source)
    if _is_speaking:
        logger.debug("Ignoring activation — Jarvis is speaking.")
        return

    try:
        # Show LISTENING state immediately
        _hud_state("listening")

        # ── Proactive opener ────────────────────────────────────────────────
        # Before asking "what?", check if JARVIS has something queued:
        # a morning briefing, an overdue task nudge, a meeting reminder, etc.
        proactive = await fetch_pending()
        opener_text = proactive.get("briefing")
        if not opener_text:
            queued = proactive.get("notifications") or []
            if queued:
                # Speak top 1-2 notifications back-to-back
                opener_text = " ".join(n["content"] for n in queued[:2])

        if opener_text:
            logger.info("Proactive opener: %s", opener_text[:100])
            print(f"\nJarvis: {opener_text}\n")
            _hud_state("speaking", opener_text)
            _is_speaking = True
            try:
                from tts_engine import speak
                await speak(
                    opener_text,
                    provider=TTS_PROVIDER,
                    voice=EDGE_TTS_VOICE,
                    elevenlabs_api_key=ELEVENLABS_API_KEY,
                    elevenlabs_voice_id=ELEVENLABS_VOICE_ID,
                )
            finally:
                _is_speaking = False
            _hud_state("listening")
        else:
            # Standard short acknowledgement
            await _ack()

        # Record — feed live RMS to HUD waveform
        def on_chunk(rms: float):
            if _hud:
                try:
                    _hud.set_audio_level(min(rms / 0.18, 1.0))
                except Exception:
                    pass

        loop = asyncio.get_event_loop()
        audio = await loop.run_in_executor(
            None, lambda: record_until_silence(on_chunk=on_chunk)
        )

        if audio.size == 0:
            logger.info("No speech detected — staying idle.")
            return

        # STT
        from stt_engine import transcribe_audio
        text = await loop.run_in_executor(
            None, transcribe_audio, audio, SAMPLE_RATE, WHISPER_MODEL
        )

        if not text.strip():
            logger.info("Nothing understood — staying idle.")
            return

        logger.info("You said: %r", text)
        print(f"\nYou: {text}")

        # Thinking state while processing
        _hud_state("thinking")

        # Try local command router first
        from command_router import route_command
        cmd_result = route_command(text)

        if cmd_result.handled:
            response = cmd_result.response
            logger.info("Local command: %r → %r", text, response)
            print(f"Jarvis: {response}\n")
        else:
            # Fall through to Jarvis backend
            try:
                if _conversation_id is None:
                    _conversation_id = await start_session()

                response = await call_jarvis(_conversation_id, text)
                if not response:
                    response = "I'm not sure how to respond to that."
            except Exception as exc:
                logger.error("Backend error: %s", exc)
                response = "Sorry, the backend is unavailable right now."
                _conversation_id = None

            logger.info("Jarvis: %r", response)
            print(f"Jarvis: {response}\n")

        # Speaking state with response text visible on HUD
        _hud_state("speaking", response)
        _is_speaking = True
        try:
            from tts_engine import speak
            await speak(
                response,
                provider=TTS_PROVIDER,
                voice=EDGE_TTS_VOICE,
                elevenlabs_api_key=ELEVENLABS_API_KEY,
                elevenlabs_voice_id=ELEVENLABS_VOICE_ID,
            )
        finally:
            _is_speaking = False

    except Exception as exc:
        logger.error("Activation handler error: %s", exc, exc_info=True)
    finally:
        _hud_state("idle")


async def _handle_typed_command(text: str) -> None:
    """Process a command typed in the text-input fallback.

    Same pipeline as a voice command, but skips STT entirely.
    """
    global _conversation_id, _is_speaking
    if _is_speaking:
        logger.debug("Ignoring typed command — Jarvis is speaking.")
        return

    text = (text or "").strip()
    if not text:
        return

    logger.info("You typed: %r", text)
    print(f"\nYou (typed): {text}")

    _hud_state("thinking")

    # Try local command router first
    try:
        from command_router import route_command
        cmd_result = route_command(text)
    except Exception as exc:
        logger.error("Local command routing error: %s", exc)
        cmd_result = None

    if cmd_result is not None and cmd_result.handled:
        response = cmd_result.response
    else:
        try:
            if _conversation_id is None:
                _conversation_id = await start_session()
            response = await call_jarvis(_conversation_id, text)
            if not response:
                response = "I'm not sure how to respond to that."
        except Exception as exc:
            logger.error("Backend error: %s", exc)
            response = "Sorry, the backend is unavailable right now."
            _conversation_id = None

    print(f"Jarvis: {response}\n")
    _hud_state("speaking", response)
    _is_speaking = True
    try:
        from tts_engine import speak
        await speak(
            response,
            provider=TTS_PROVIDER,
            voice=EDGE_TTS_VOICE,
            elevenlabs_api_key=ELEVENLABS_API_KEY,
            elevenlabs_voice_id=ELEVENLABS_VOICE_ID,
        )
    finally:
        _is_speaking = False
        _hud_state("idle")


_ACK_BEEP_HZ = 1320            # short pleasant chime
_ACK_BEEP_MS = 90
_VERBAL_ACK = os.getenv("JARVIS_VERBAL_ACK", "false").lower() == "true"


async def _ack() -> None:
    """Activation acknowledgement.

    Default: a tiny beep. Set JARVIS_VERBAL_ACK=true in .env if Sir
    actually wants the spoken "Sir?" / "Listening, Sir." each time —
    most of the time the beep is faster and far less annoying.
    """
    if _VERBAL_ACK:
        try:
            from tts_engine import speak
            await speak("Sir.", provider=TTS_PROVIDER, voice=EDGE_TTS_VOICE,
                        elevenlabs_api_key=ELEVENLABS_API_KEY,
                        elevenlabs_voice_id=ELEVENLABS_VOICE_ID)
            return
        except Exception as exc:
            logger.debug("Verbal ack failed (%s) — falling back to beep.", exc)

    # Beep — instantaneous, never overlaps with TTS, gives clear feedback.
    try:
        if sys.platform == "win32":
            import winsound
            winsound.Beep(_ACK_BEEP_HZ, _ACK_BEEP_MS)
    except Exception as exc:
        logger.debug("Ack beep failed: %s", exc)


# ─── Single-instance guard ────────────────────────────────────────────────────

def _ensure_single_instance() -> bool:
    """Prevent two JARVIS voices running at once.

    Uses a Windows named mutex (cross-process). Returns True if we're the
    only instance; False if another JARVIS is already running — in which
    case the caller should exit cleanly so we don't speak in stereo.
    """
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        ERROR_ALREADY_EXISTS = 183
        kernel32 = ctypes.windll.kernel32
        # The handle is intentionally leaked — it's released only when
        # this Python process exits, which is exactly what we want.
        handle = kernel32.CreateMutexW(None, False, "Global\\JARVIS_VoiceAgent_Singleton")
        last_error = ctypes.GetLastError()
        if last_error == ERROR_ALREADY_EXISTS:
            return False
        # Keep handle alive for the lifetime of the process
        global _MUTEX_HANDLE
        _MUTEX_HANDLE = handle
        return True
    except Exception as exc:
        logger.debug("Single-instance check failed (%s) — proceeding anyway.", exc)
        return True


_MUTEX_HANDLE = None  # populated by _ensure_single_instance() to keep it alive


# ─── Entry point ──────────────────────────────────────────────────────────────

def _run_asyncio_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Run the asyncio event loop in a background thread."""
    asyncio.set_event_loop(loop)
    loop.run_forever()


def main() -> None:
    global _hud

    # ── Single-instance guard ───────────────────────────────────────────────
    # If another JARVIS is already running, exit silently instead of
    # speaking in stereo. Solves the "two Jarvises talking at once" issue.
    if not _ensure_single_instance():
        print("JARVIS is already running — closing this duplicate.")
        return

    from wake_word import ActivationListener

    # ── Try to start PyQt6 HUD ────────────────────────────────────────────────
    qt_app = None
    text_input = None
    try:
        from PyQt6.QtWidgets import QApplication
        from hud import JarvisHUD, setup_tray
        qt_app = QApplication.instance() or QApplication(sys.argv)
        qt_app.setQuitOnLastWindowClosed(False)
        _hud = JarvisHUD()
        setup_tray(qt_app, _hud)
        logger.info("HUD started.")
    except ImportError:
        logger.warning(
            "PyQt6 not installed — running in terminal mode. "
            "Install with: pip install PyQt6"
        )
    except Exception as exc:
        logger.warning("HUD failed to start: %s — running in terminal mode.", exc)

    # ── Asyncio loop (background thread) ─────────────────────────────────────
    loop = asyncio.new_event_loop()

    # ── Text-input fallback ─────────────────────────────────────────────────
    # Built AFTER `loop` exists so the on_submit callback can post coroutines
    # to it. Triggered globally by Ctrl+Shift+T (registered further below).
    if qt_app is not None:
        try:
            from text_input import TextInput
            text_input = TextInput(
                on_submit=lambda s: asyncio.run_coroutine_threadsafe(
                    _handle_typed_command(s), loop
                ),
                hud=_hud,
            )
            logger.info("Text-input fallback ready (Ctrl+Shift+T).")
        except Exception as exc:
            logger.debug("Text-input fallback unavailable: %s", exc)

    def on_activate(source: str) -> None:
        asyncio.run_coroutine_threadsafe(handle_activation(source), loop)

    if qt_app is None:
        # No HUD — print the classic banner
        print("=" * 55)
        print("  JARVIS — Local Voice Assistant")
        print("  Say 'Jarvis' or double-clap to activate.")
        print("  Ctrl+C to quit.")
        print("=" * 55)

    # Pre-warm Whisper so first response is fast
    try:
        from stt_engine import _get_model
        threading.Thread(
            target=_get_model, args=(WHISPER_MODEL,), daemon=True
        ).start()
    except Exception as exc:
        logger.warning("Could not pre-warm Whisper: %s", exc)

    listener = ActivationListener(
        on_activate=on_activate,
        sensitivity=WAKE_WORD_SENSITIVITY,
        rms_threshold=CLAP_RMS_THRESHOLD,
    )

    # ── Optional global hotkeys: Ctrl+Shift+J (voice), Ctrl+Shift+T (text) ──
    hotkey = None
    try:
        from hotkey import HotkeyListener
        hotkey = HotkeyListener(on_activate=on_activate)
    except Exception as exc:
        logger.debug("Hotkey listener unavailable: %s", exc)

    # Ctrl+Shift+T → summon the text-input box. Best-effort.
    if text_input is not None:
        try:
            import keyboard  # type: ignore
            keyboard.add_hotkey("ctrl+shift+t", text_input.request_show)
            logger.info("Registered text-input hotkey: Ctrl+Shift+T")
        except Exception as exc:
            logger.debug("Could not register Ctrl+Shift+T (%s)", exc)

    # Asyncio runs in a background thread; Qt (or blocking) takes the main thread
    asyncio_thread = threading.Thread(
        target=_run_asyncio_loop, args=(loop,), daemon=True
    )
    asyncio_thread.start()

    listener_thread = threading.Thread(target=listener.start, daemon=True)
    listener_thread.start()

    if hotkey is not None:
        hotkey.start()  # non-blocking — hooks into the global event loop

    # ── Cleanup function used by all exit paths ─────────────────────────────
    def _cleanup(*_):
        listener.stop()
        if hotkey is not None:
            try:
                hotkey.stop()
            except Exception:
                pass
        loop.call_soon_threadsafe(loop.stop)
        if qt_app is not None:
            qt_app.quit()

    # atexit — catches normal interpreter shutdown AND Windows console close
    atexit.register(_cleanup)

    # Windows console close handler (X button, logoff, shutdown)
    if sys.platform == "win32":
        try:
            import ctypes
            _CTRL_C_EVENT = 0
            _CTRL_CLOSE_EVENT = 2
            _kernel32 = ctypes.windll.kernel32

            @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)
            def _console_handler(event):
                if event in (_CTRL_C_EVENT, _CTRL_CLOSE_EVENT):
                    _cleanup()
                    return True
                return False

            _kernel32.SetConsoleCtrlHandler(_console_handler, True)
        except Exception as exc:
            logger.debug("Could not set Windows console handler: %s", exc)

    if qt_app is not None:
        import signal
        from PyQt6.QtCore import QTimer

        # SIGINT (Ctrl+C) and SIGTERM both quit cleanly
        signal.signal(signal.SIGINT, _cleanup)
        signal.signal(signal.SIGTERM, _cleanup)

        # Qt blocks Python's signal handler while its event loop runs.
        # A no-op timer every 300 ms gives Python a chance to check signals.
        _sig_timer = QTimer()
        _sig_timer.start(300)
        _sig_timer.timeout.connect(lambda: None)

        exit_code = qt_app.exec()
        sys.exit(exit_code)
    else:
        # Fallback: block main thread here
        try:
            asyncio_thread.join()
        except KeyboardInterrupt:
            print("\nShutting down JARVIS...")
            _cleanup()


if __name__ == "__main__":
    main()
