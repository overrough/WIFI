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
import json
import logging
import os
import sys
import threading
from typing import Optional

import numpy as np
from dotenv import load_dotenv

load_dotenv()

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
EDGE_TTS_VOICE = os.getenv("EDGE_TTS_VOICE", "en-US-GuyNeural")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")
WAKE_WORD_SENSITIVITY = float(os.getenv("WAKE_WORD_SENSITIVITY", "0.5"))
CLAP_RMS_THRESHOLD = float(os.getenv("CLAP_RMS_THRESHOLD", "0.15"))

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
    """Stream a message to the Jarvis backend and return the full response."""
    import httpx
    parts = []
    async with httpx.AsyncClient(timeout=60) as client:
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
                        if event.get("type") == "token":
                            parts.append(event.get("content", ""))
                        elif event.get("type") == "done":
                            break
                    except json.JSONDecodeError:
                        continue
    return "".join(parts)


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

    # Show LISTENING state immediately
    _hud_state("listening")

    # Acknowledgement tone
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
        _hud_state("idle")
        return

    # STT
    from stt_engine import transcribe_audio
    text = await loop.run_in_executor(
        None, transcribe_audio, audio, SAMPLE_RATE, WHISPER_MODEL
    )

    if not text.strip():
        logger.info("Nothing understood — staying idle.")
        _hud_state("idle")
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
        if _conversation_id is None:
            _conversation_id = await start_session()

        response = await call_jarvis(_conversation_id, text)
        if not response:
            response = "I'm not sure how to respond to that."

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
        _hud_state("idle")


async def _ack() -> None:
    """Short acknowledgement to confirm activation."""
    try:
        from tts_engine import speak
        await speak("Sir?", provider=TTS_PROVIDER, voice=EDGE_TTS_VOICE,
                    elevenlabs_api_key=ELEVENLABS_API_KEY,
                    elevenlabs_voice_id=ELEVENLABS_VOICE_ID)
    except Exception as exc:
        logger.debug("Ack failed: %s", exc)


# ─── Entry point ──────────────────────────────────────────────────────────────

def _run_asyncio_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Run the asyncio event loop in a background thread."""
    asyncio.set_event_loop(loop)
    loop.run_forever()


def main() -> None:
    global _hud

    from wake_word import ActivationListener

    # ── Try to start PyQt6 HUD ────────────────────────────────────────────────
    qt_app = None
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

    # Asyncio runs in a background thread; Qt (or blocking) takes the main thread
    asyncio_thread = threading.Thread(
        target=_run_asyncio_loop, args=(loop,), daemon=True
    )
    asyncio_thread.start()

    listener_thread = threading.Thread(target=listener.start, daemon=True)
    listener_thread.start()

    if qt_app is not None:
        # Qt event loop owns the main thread
        try:
            exit_code = qt_app.exec()
        except KeyboardInterrupt:
            exit_code = 0
        finally:
            listener.stop()
            loop.call_soon_threadsafe(loop.stop)
        sys.exit(exit_code)
    else:
        # Fallback: block main thread here
        try:
            asyncio_thread.join()
        except KeyboardInterrupt:
            print("\nShutting down JARVIS...")
            listener.stop()
            loop.call_soon_threadsafe(loop.stop)


if __name__ == "__main__":
    main()
