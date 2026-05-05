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

import io
import logging
import os

import numpy as np

logger = logging.getLogger(__name__)

_model = None  # lazy-loaded singleton


def _get_model(model_name: str = "base"):
    global _model
    if _model is None:
        try:
            from faster_whisper import WhisperModel
            device = "cpu"
            compute_type = "int8"
            logger.info("Loading Whisper %s on %s ...", model_name, device)
            _model = WhisperModel(model_name, device=device, compute_type=compute_type)
            logger.info("Whisper model ready.")
        except ImportError:
            raise RuntimeError(
                "faster-whisper not installed. Run: pip install faster-whisper"
            )
    return _model


def transcribe_audio(audio_data: np.ndarray, sample_rate: int = 16000, model_name: str = "base") -> str:
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
        vad_filter=True,               # skip silent parts
        vad_parameters={"min_silence_duration_ms": 500},
    )

    text = " ".join(seg.text.strip() for seg in segments).strip()
    logger.debug("STT: %r (%.2fs audio)", text, len(audio_data) / 16000)
    return text
