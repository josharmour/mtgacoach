"""Push-to-talk (hold-to-talk) voice input for Conversation Mode.

Hold the button to record; release to stop, transcribe, and send the text as
a chat message through the coach session. Recording (sounddevice + wave) and
transcription (faster-whisper) are lazily resolved so this module imports
cleanly when either dependency is missing — the button is then disabled with
a "voice input unavailable" tooltip.

Dependencies are injectable for tests: pass ``recorder=`` / ``transcriber=``
objects implementing ``start()``/``stop()`` and ``transcribe(bytes)``.
"""

from __future__ import annotations

import contextlib
import io
import logging
import wave
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QPushButton

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # int16

VOICE_UNAVAILABLE_TOOLTIP = "voice input unavailable"
LOCAL_FALLBACK_PREFIX = "[LOCAL FALLBACK]"


def _local_fallback_notice(message: str) -> str:
    """User-visible failure notice (T7): mic/transcription problems must be
    explained in the transcript, never silently dropped."""
    return f"{LOCAL_FALLBACK_PREFIX} {message}"


def _mic_failure_notice(message: str) -> str:
    return _local_fallback_notice(f"microphone: {message}")


def _transcription_failure_notice(message: str) -> str:
    return _local_fallback_notice(f"transcription failed: {message}")


def _no_speech_notice() -> str:
    return _local_fallback_notice("no speech detected — hold the button a little longer")


def check_dependencies() -> tuple[bool, str]:
    """Return (available, reason). Never raises."""
    try:
        import sounddevice  # noqa: F401
    except Exception as exc:
        return False, f"sounddevice unavailable: {exc}"
    try:
        import faster_whisper  # noqa: F401
    except Exception as exc:
        return False, f"faster-whisper unavailable: {exc}"
    return True, ""


class _SoundDeviceRecorder:
    """Records 16 kHz mono int16 WAV bytes while started."""

    def __init__(self, sample_rate: int = SAMPLE_RATE) -> None:
        import numpy as np
        import sounddevice as sd

        self._np = np
        self._sd = sd
        self._stream: sd.InputStream | None = None
        self._frames: list[Any] = []
        self._sample_rate = sample_rate

    def start(self) -> None:
        self._frames = []

        def _callback(indata, _frames, _time, _status) -> None:  # pragma: no cover
            self._frames.append(indata.copy())

        self._stream = self._sd.InputStream(
            samplerate=self._sample_rate,
            channels=CHANNELS,
            dtype="int16",
            callback=_callback,
        )
        self._stream.start()

    def stop(self) -> bytes:
        stream = self._stream
        self._stream = None
        if stream is None:
            return b""
        with contextlib.suppress(Exception):
            stream.stop()
            stream.close()
        frames = self._frames
        self._frames = []
        if not frames:
            return b""
        audio = self._np.concatenate(frames, axis=0)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(CHANNELS)
            wav.setsampwidth(SAMPLE_WIDTH)
            wav.setframerate(self._sample_rate)
            wav.writeframes(audio.tobytes())
        return buf.getvalue()


class _FasterWhisperTranscriber:
    """Lazy faster-whisper transcription of WAV bytes."""

    _model: Any = None

    def transcribe(self, wav_bytes: bytes) -> str:
        import faster_whisper

        cls = type(self)
        if cls._model is None:
            cls._model = faster_whisper.WhisperModel("base", compute_type="int8")
        import soundfile as sf

        data, samplerate = sf.read(io.BytesIO(wav_bytes), dtype="float32")
        segments, _info = cls._model.transcribe(data, language="en", beam_size=1)
        return " ".join(segment.text.strip() for segment in segments).strip()


def default_recorder() -> Any:
    """Build a recorder from sounddevice+wave; raises if unavailable."""
    import sounddevice  # noqa: F401

    return _SoundDeviceRecorder()


def default_transcriber() -> Any:
    """Build a transcriber from faster_whisper; raises if unavailable."""
    import faster_whisper  # noqa: F401

    return _FasterWhisperTranscriber()


class PttController(QObject):
    """Wires a hold-to-talk button to record → transcribe → send chat.

    On press: ``session.stop_speaking()`` then ``recorder.start()``.
    On release: ``recorder.stop()`` → WAV bytes → ``transcriber.transcribe`` →
    ``session.send_chat(text)``.
    """

    listeningChanged = Signal(bool)

    def __init__(
        self,
        button: QPushButton,
        session: Any,
        recorder: Any = None,
        transcriber: Any = None,
        on_send: Callable[[str], None] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent or button)
        self._button = button
        self._session = session
        self._recorder = recorder
        self._transcriber = transcriber
        self._on_send = on_send
        self._listening = False

        if recorder is None:
            self._recorder = None  # explicit None means "not provided"; probe below
            with contextlib.suppress(Exception):
                self._recorder = default_recorder()
        if transcriber is None:
            with contextlib.suppress(Exception):
                self._transcriber = default_transcriber()

        recorder_ready = self._recorder is not None and callable(
            getattr(self._recorder, "start", None)
        )
        transcriber_ready = self._transcriber is not None and callable(
            getattr(self._transcriber, "transcribe", None)
        )
        available = recorder_ready and transcriber_ready
        button.setCheckable(True)
        if available:
            button.setToolTip("Hold to talk — release to send")
            button.pressed.connect(self._on_pressed)
            button.released.connect(self._on_released)
        else:
            button.setEnabled(False)
            button.setToolTip(VOICE_UNAVAILABLE_TOOLTIP)

    @property
    def listening(self) -> bool:
        return self._listening

    def _on_pressed(self) -> None:
        if self._recorder is None:
            return
        try:
            if hasattr(self._session, "stop_speaking"):
                self._session.stop_speaking()
            self._recorder.start()
        except Exception as exc:
            logger.warning("PTT recorder start failed: %s", exc)
            self._notice(_mic_failure_notice(str(exc) or "could not start recording"))
            return
        self._listening = True
        self._button.setText("🎙 Listening…")
        self.listeningChanged.emit(True)

    def _on_released(self) -> None:
        if not self._listening:
            return
        self._listening = False
        self._button.setText("🎙 Hold to talk")
        self.listeningChanged.emit(False)
        try:
            wav_bytes = self._recorder.stop() or b""
        except Exception as exc:
            logger.warning("PTT recorder stop failed: %s", exc)
            self._notice(_mic_failure_notice(str(exc) or "could not finish recording"))
            return
        if not wav_bytes:
            # No audio captured: an info-level condition, but the user should
            # know the press produced nothing (T7 — failures are explained).
            self._notice(_no_speech_notice())
            return
        try:
            text = (self._transcriber.transcribe(wav_bytes) or "").strip()
        except Exception as exc:
            logger.warning("PTT transcription failed: %s", exc)
            self._notice(_transcription_failure_notice(str(exc) or "transcriber error"))
            return
        if not text:
            logger.info("PTT transcription produced no text")
            self._notice(_no_speech_notice())
            return
        if self._on_send is not None:
            self._on_send(text)
        else:
            self._session.send_chat(text)

    def _notice(self, message: str) -> None:
        """Surface a user-visible notice through the transcript surface.

        Prefers ``emit_local_fallback_notice`` on the session (wired by the
        desktop panel), then ``send_chat`` as a generic channel.
        """
        emitter = getattr(self._session, "emit_local_fallback_notice", None)
        if callable(emitter):
            try:
                emitter(message)
                return
            except Exception:  # pragma: no cover - defensive
                logger.debug("emit_local_fallback_notice failed", exc_info=True)
        sender = getattr(self._session, "send_chat", None)
        if callable(sender):
            try:
                sender(message)
            except Exception:  # pragma: no cover - defensive
                logger.debug("PTT notice send_chat failed", exc_info=True)
