"""Speech-to-text transcription using faster-whisper.

This module provides transcription of audio arrays to text using
the faster-whisper library (CTranslate2 backend, ~4x faster than OpenAI whisper).

NOTE: On first use, the Whisper model will be downloaded (~150MB for base model).
This is a one-time download stored in the user's cache directory.
"""

from typing import Optional

import numpy as np

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None  # type: ignore[assignment,misc]


class WhisperTranscriber:
    """Speech-to-text transcriber using faster-whisper.

    Uses lazy model loading to avoid startup delay. The model is only
    loaded when the first transcription is requested.

    Example:
        transcriber = WhisperTranscriber()
        # First call downloads/loads model (~150MB for base)
        text = transcriber.transcribe(audio_array)
        print(text)

    Note:
        Model download happens on first use. Uses int8 quantization
        on CPU for best speed without GPU requirements.
    """

    def __init__(
        self,
        model_size: str = "base",
        device: str = "cpu",
        compute_type: str = "int8",
        language: Optional[str] = None,
    ) -> None:
        """Initialize the transcriber.

        Args:
            model_size: Whisper model size. Options: 'tiny', 'base', 'small',
                       'medium', 'large-v2', 'large-v3'. Default 'base' for
                       good speed/accuracy tradeoff (~150MB).
            device: Device to run inference on. Default 'cpu'.
            compute_type: Quantization type. Use 'int8' for CPU, 'float16'
                         for GPU. Default 'int8'.
            language: Language code for transcription (e.g., 'en', 'nl', 'es').
                     If None, reads from settings (default: 'en').
        """
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._model: Optional["WhisperModel"] = None

        if language is not None:
            self._language = language
        else:
            try:
                from arenamcp.settings import get_settings
                self._language = get_settings().get("language", "en")
            except Exception:
                self._language = "en"

    def _ensure_model_loaded(self) -> None:
        """Lazy-load the Whisper model on first use.

        Downloads the model if not cached. This can take a moment
        on first run (~150MB for base model).
        """
        if self._model is None:
            if WhisperModel is None:
                raise ImportError(
                    "faster-whisper is not installed. "
                    "Install with: pip install faster-whisper"
                )
            self._model = WhisperModel(
                self._model_size,
                device=self._device,
                compute_type=self._compute_type,
            )

    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> str:
        """Transcribe audio array to text.

        Args:
            audio: Numpy array of audio samples. Should be float32 with
                  values in [-1.0, 1.0] range. Shape should be (samples,).
            sample_rate: Sample rate of the audio. Default 16000 (Whisper native).

        Returns:
            Transcribed text as a string. Returns empty string for
            silence or no detected speech.
        """
        self._ensure_model_loaded()

        if len(audio) == 0:
            return ""

        # Ensure correct dtype
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)

        # Transcribe with optimized settings
        segments, _ = self._model.transcribe(
            audio,
            beam_size=5,
            language=self._language,
            vad_filter=True,  # Filter out non-speech
        )

        # Concatenate all segment texts
        text_parts = [segment.text.strip() for segment in segments]
        return " ".join(text_parts).strip()
