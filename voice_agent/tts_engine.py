"""
Text-to-Speech engine.

Free default: edge-tts — Microsoft's neural TTS via Edge browser API.
  - No API key required
  - ~400 high-quality neural voices across 100+ languages
  - Install: pip install edge-tts

Premium (optional): ElevenLabs — set ELEVENLABS_API_KEY in .env for a
  richer, more expressive voice (free tier: 10,000 chars/month).

Usage:
    import asyncio
    from tts_engine import speak
    asyncio.run(speak("Online. What do you need?"))
"""

import asyncio
import logging
import os
import sys
import tempfile

logger = logging.getLogger(__name__)


async def _speak_edge_tts(text: str, voice: str, output_path: str) -> None:
    """Synthesise text to an MP3 file using edge-tts (free, no key)."""
    try:
        import edge_tts
    except ImportError:
        raise RuntimeError("edge-tts not installed. Run: pip install edge-tts")

    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(output_path)


async def _speak_elevenlabs(text: str, api_key: str, voice_id: str, output_path: str) -> None:
    """Synthesise text using ElevenLabs API (optional, paid)."""
    try:
        import httpx
    except ImportError:
        raise RuntimeError("httpx not installed.")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
    payload = {
        "text": text,
        "model_id": "eleven_turbo_v2_5",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        with open(output_path, "wb") as f:
            f.write(resp.content)


def _play_audio(path: str) -> None:
    """Play an MP3 audio file cross-platform."""
    import subprocess

    if sys.platform == "darwin":
        subprocess.run(["afplay", path], check=True)
    elif sys.platform == "win32":
        # winsound only supports WAV; use Windows MCI API (built-in, supports MP3)
        import ctypes
        mm = ctypes.windll.winmm
        path_w = str(path).replace("/", "\\")
        mm.mciSendStringW(f'open "{path_w}" type mpegvideo alias jarvistts', None, 0, None)
        mm.mciSendStringW("play jarvistts wait", None, 0, None)
        mm.mciSendStringW("close jarvistts", None, 0, None)
    else:
        for player in ["mpg123", "ffplay", "aplay"]:
            try:
                subprocess.run([player, "-q", path], check=True)
                break
            except FileNotFoundError:
                continue


async def speak(
    text: str,
    provider: str = "edge-tts",
    voice: str = "en-US-GuyNeural",
    elevenlabs_api_key: str = "",
    elevenlabs_voice_id: str = "",
) -> None:
    """
    Synthesise and play speech.

    Args:
        text: Text to speak
        provider: "edge-tts" (free) or "elevenlabs" (paid)
        voice: edge-tts voice name (ignored for ElevenLabs)
        elevenlabs_api_key: only needed when provider="elevenlabs"
        elevenlabs_voice_id: only needed when provider="elevenlabs"
    """
    if not text.strip():
        return

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        output_path = tmp.name

    try:
        if provider == "elevenlabs" and elevenlabs_api_key:
            await _speak_elevenlabs(text, elevenlabs_api_key, elevenlabs_voice_id, output_path)
        else:
            await _speak_edge_tts(text, voice, output_path)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _play_audio, output_path)
    finally:
        try:
            os.unlink(output_path)
        except OSError:
            pass


# Synchronous convenience wrapper for non-async callers
def speak_sync(text: str, **kwargs) -> None:
    asyncio.run(speak(text, **kwargs))
