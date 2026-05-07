"""
Speech-to-Text engine — faster-whisper (local, completely free).

faster-whisper runs OpenAI Whisper models locally with CTranslate2 backend.
Models (auto-downloaded on first run):
  tiny   (~39 MB) — fastest, good for short commands
  base   (~74 MB) — recommended default
  small  (~244 MB) — better accuracy
  medium (~769 MB) — high accuracy
  large-v3 (~1.5 GB) — best accuracy

Install: pip install faster-whisper
"""

import logging
import os
import threading

import numpy as np

logger = logging.getLogger(__name__)

_models: dict = {}          # model_name → WhisperModel
_model_lock = threading.Lock()

# Whisper initial_prompt biases the model toward expected vocabulary.
# This dramatically improves accuracy for short voice commands.
#
# The vocabulary below is hand-tuned for Sir's daily usage. Adding the
# proper-noun spellings (Wispr Flow, ElevateWebWorks, qwen, etc.)
# stops Whisper from "auto-correcting" them into garbage like
# "VS Perflow" or "WordPad" → "Word Term".
#
# Rule of thumb: any word JARVIS has misheard once should be added here
# in its CORRECT spelling, ideally inside a natural sentence.
INITIAL_PROMPT = (
    # Identity & owner
    "Hello Jarvis. Yes Sir, at your service. Saksham, ElevateWebWorks. "
    # Apps that have actually been mis-transcribed
    "Open Wispr Flow. Open VS Code. Open Visual Studio Code. Open WordPad. "
    "Open Microsoft Word. Open Microsoft Excel. Open Microsoft PowerPoint. "
    "Open Notepad. Open Outlook. Open Teams. Open Slack. Open Discord. "
    "Open Chrome. Open Firefox. Open Edge. Open Brave. Open Cursor. "
    "Open Spotify. Open Photoshop. Open Premiere Pro. Open After Effects. "
    "Open Figma. Open Canva. Open Notion. Open ChatGPT. Open Claude. "
    "Open File Explorer. Open Task Manager. Open Settings. Open Calculator. "
    "Open the Camera. Open the Recycle Bin. Empty the Recycle Bin. "
    # Common verbs
    "Search for. Look up. Find. Send an email. Reply to. Draft a message. "
    "Schedule a meeting. Create a task. Mark as done. Remind me to. "
    # System
    "Lock the screen. Sleep the computer. Shut down. Restart. "
    "Volume up. Volume down. Mute. Take a screenshot. "
    # Web
    "Go to YouTube. Go to GitHub. Go to LinkedIn. Go to Gmail. "
    "Go to Google. Go to Instagram. Go to Twitter. "
    # Brand and project terms Whisper should NOT mangle
    "ElevateWebWorks. elevatewebworks.in. LinkedIn. Instagram. Saksham Sanjmalani. "
    "Ollama. qwen. Whisper. ChromaDB. Playwright. PyAutoGUI. SQLite. "
)


def _get_model(model_name: str = "small.en"):
    if model_name in _models:
        return _models[model_name]
    with _model_lock:
        # Double-check after acquiring lock
        if model_name in _models:
            return _models[model_name]
        try:
            from faster_whisper import WhisperModel
            device = "cpu"
            compute_type = "int8"
            logger.info("Loading Whisper %s on %s ...", model_name, device)
            model = WhisperModel(model_name, device=device, compute_type=compute_type)
            _models[model_name] = model
            logger.info("Whisper model ready.")
            return model
        except ImportError:
            raise RuntimeError(
                "faster-whisper not installed. Run: pip install faster-whisper"
            )


def transcribe_audio(audio_data: np.ndarray, sample_rate: int = 16000, model_name: str = "small.en") -> str:
    """
    Transcribe raw audio samples to text.

    Args:
        audio_data: float32 numpy array, mono, any sample rate
        sample_rate: sample rate of audio_data
        model_name: whisper model size

    Returns:
        Transcribed text string (empty string if silent/inaudible)
    """
    model = _get_model(model_name)

    # Resample to 16 kHz if needed (Whisper expects 16 kHz)
    if sample_rate != 16000:
        try:
            import resampy
            audio_data = resampy.resample(audio_data, sample_rate, 16000)
        except ImportError:
            # Simple linear interpolation fallback
            factor = 16000 / sample_rate
            new_len = int(len(audio_data) * factor)
            audio_data = np.interp(
                np.linspace(0, len(audio_data), new_len),
                np.arange(len(audio_data)),
                audio_data,
            ).astype(np.float32)

    # Normalize to [-1, 1]
    if audio_data.dtype != np.float32:
        audio_data = audio_data.astype(np.float32)
    max_val = np.abs(audio_data).max()
    if max_val > 0:
        audio_data = audio_data / max_val

    segments, info = model.transcribe(
        audio_data,
        beam_size=5,
        language="en",
        initial_prompt=INITIAL_PROMPT,
        # Tighter VAD — don't aggressively chop mid-sentence pauses, which
        # was clipping commands like "Go on Chrome and then search ...".
        vad_filter=True,
        vad_parameters={
            "min_silence_duration_ms": 800,   # was 500, more forgiving of natural pauses
            "speech_pad_ms": 200,             # add cushion either side so we don't lose first/last consonant
        },
        # Slight compression-aware decoding — helps with hesitations
        no_speech_threshold=0.45,
        condition_on_previous_text=False,    # don't let one mistake snowball
    )

    text = " ".join(seg.text.strip() for seg in segments).strip()
    logger.debug("STT: %r (%.2fs audio)", text, len(audio_data) / 16000)
    return text
