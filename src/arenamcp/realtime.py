"""GPT-Realtime backend for voice-to-voice coaching.

This module integrates Azure OpenAI's GPT-realtime API for low-latency
bidirectional voice conversations. Uses WebSocket for streaming audio.

Usage:
    from arenamcp.realtime import GPTRealtimeClient

    client = GPTRealtimeClient()
    client.connect()
    client.send_audio(audio_data)
    response = client.get_response()
"""

import base64
import json
import logging
import os
import queue
import threading
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)


def play_audio_pcm16(audio_bytes: bytes, sample_rate: int = 24000) -> None:
    """Play PCM16 audio bytes.

    Args:
        audio_bytes: Raw PCM16 audio data
        sample_rate: Sample rate in Hz (default 24000 for GPT-realtime)
    """
    if not audio_bytes:
        return

    try:
        # Convert bytes to numpy array
        audio_int16 = np.frombuffer(audio_bytes, dtype=np.int16)
        # Convert to float32 for sounddevice
        audio_float = audio_int16.astype(np.float32) / 32767.0
        # Play audio
        sd.play(audio_float, sample_rate, blocking=True)
    except Exception as e:
        logger.error(f"Failed to play audio: {e}")


def stop_audio() -> None:
    """Stop any currently playing audio."""
    try:
        sd.stop()
    except Exception as e:
        logger.error(f"Failed to stop audio: {e}")

# Default Azure endpoint and deployment from environment
DEFAULT_REALTIME_ENDPOINT = os.environ.get(
    "AZURE_REALTIME_ENDPOINT",
    ""
)
DEFAULT_REALTIME_DEPLOYMENT = os.environ.get("AZURE_REALTIME_DEPLOYMENT", "gpt-realtime")
DEFAULT_REALTIME_API_VERSION = os.environ.get("AZURE_REALTIME_API_VERSION", "2024-10-01-preview")


@dataclass
class RealtimeConfig:
    """Configuration for GPT-Realtime connection."""

    endpoint: str = DEFAULT_REALTIME_ENDPOINT
    deployment: str = DEFAULT_REALTIME_DEPLOYMENT
    api_version: str = DEFAULT_REALTIME_API_VERSION
    api_key: Optional[str] = None

    # Audio settings
    sample_rate: int = 24000  # GPT-realtime uses 24kHz
    input_sample_rate: int = 16000  # Our recording rate

    # Voice settings
    voice: str = "alloy"  # alloy, echo, fable, onyx, nova, shimmer

    # Session settings
    modalities: list[str] = None  # ["text", "audio"]
    instructions: str = ""
    temperature: float = 0.8
    max_response_tokens: int = 1024

    # VAD settings
    turn_detection_type: str = "server_vad"  # or "none" for manual
    vad_threshold: float = 0.5
    vad_prefix_padding_ms: int = 300
    vad_silence_duration_ms: int = 500

    def __post_init__(self):
        if self.modalities is None:
            self.modalities = ["text", "audio"]
        if not self.api_key:
            self.api_key = os.environ.get("AZURE_REALTIME_API_KEY")


class GPTRealtimeClient:
    """WebSocket client for Azure OpenAI GPT-Realtime API.

    Handles bidirectional audio streaming for real-time voice conversations.
    """

    def __init__(self, config: Optional[RealtimeConfig] = None):
        """Initialize the realtime client.

        Args:
            config: Realtime configuration. Uses defaults if not provided.
        """
        self.config = config or RealtimeConfig()

        self._ws = None
        self._connected = False
        self._session_id: Optional[str] = None

        # Threading
        self._recv_thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()

        # Response handling
        self._response_queue: queue.Queue[dict] = queue.Queue()
        self._audio_queue: queue.Queue[bytes] = queue.Queue()
        self._transcript_buffer: str = ""
        self._audio_buffer: list[bytes] = []
        
        # State tracking
        self._response_in_progress = False

        # Callbacks
        self._on_transcript: Optional[Callable[[str], None]] = None
        self._on_audio: Optional[Callable[[bytes], None]] = None
        self._on_response_done: Optional[Callable[[str, bytes], None]] = None
        self._on_error: Optional[Callable[[str], None]] = None

        # Resampling for audio format conversion
        self._resampler = None

    @property
    def is_connected(self) -> bool:
        """Check if WebSocket is connected."""
        return self._connected and self._ws is not None

    def _build_url(self) -> str:
        """Build WebSocket URL with query parameters."""
        base = self.config.endpoint
        if not base.startswith("wss://"):
            base = f"wss://{base}"

        # Remove any existing path/query
        if "?" in base:
            base = base.split("?")[0]

        url = (
            f"{base}"
            f"?api-version={self.config.api_version}"
            f"&deployment={self.config.deployment}"
        )
        return url

    def connect(self) -> bool:
        """Establish WebSocket connection to GPT-Realtime.

        Returns:
            True if connection successful, False otherwise.
        """
        if self._connected:
            return True

        try:
            import websocket
        except ImportError:
            logger.error("websocket-client package required: pip install websocket-client")
            return False

        if not self.config.api_key:
            logger.error("AZURE_REALTIME_API_KEY not set")
            return False

        url = self._build_url()
        logger.info(f"Connecting to GPT-Realtime: {url}")

        try:
            self._ws = websocket.WebSocket()
            self._ws.connect(
                url,
                header=[f"api-key: {self.config.api_key}"],
                timeout=10
            )
            self._connected = True
            self._running = True
            self._response_in_progress = False

            # Start receive thread
            self._recv_thread = threading.Thread(
                target=self._receive_loop,
                daemon=True,
                name="GPTRealtimeRecv"
            )
            self._recv_thread.start()

            # Configure session
            self._send_session_update()

            logger.info("GPT-Realtime connected successfully")
            return True

        except Exception as e:
            logger.error(f"GPT-Realtime connection failed: {e}")
            self._connected = False
            return False

    def disconnect(self) -> None:
        """Close WebSocket connection."""
        self._running = False
        self._connected = False
        self._response_in_progress = False

        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

        if self._recv_thread and self._recv_thread.is_alive():
            self._recv_thread.join(timeout=2.0)

        logger.info("GPT-Realtime disconnected")

    def _send_session_update(self) -> None:
        """Send session configuration to server."""
        session_config = {
            "type": "session.update",
            "session": {
                "modalities": self.config.modalities,
                "instructions": self.config.instructions,
                "voice": self.config.voice,
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "input_audio_transcription": {
                    "model": "whisper-1",
                    "language": "en"
                },
                "turn_detection": {
                    "type": self.config.turn_detection_type,
                    "threshold": self.config.vad_threshold,
                    "prefix_padding_ms": self.config.vad_prefix_padding_ms,
                    "silence_duration_ms": self.config.vad_silence_duration_ms
                },
                "temperature": self.config.temperature,
                "max_response_output_tokens": self.config.max_response_tokens
            }
        }
        self._send(session_config)
        logger.info(f"Session config sent: voice={self.config.voice}, vad={self.config.turn_detection_type}")

    def _send(self, message: dict) -> None:
        """Send JSON message over WebSocket."""
        if not self._ws:
            return
        try:
            self._ws.send(json.dumps(message))
        except Exception as e:
            logger.error(f"Send error: {e}")
            self._connected = False

    def _receive_loop(self) -> None:
        """Background thread to receive WebSocket messages."""
        import websocket as ws_module

        while self._running and self._ws:
            try:
                self._ws.settimeout(0.5)
                data = self._ws.recv()
                if data:
                    self._handle_message(json.loads(data))
            except ws_module.WebSocketTimeoutException:
                continue
            except ws_module.WebSocketConnectionClosedException:
                logger.warning("WebSocket connection closed")
                self._connected = False
                break
            except Exception as e:
                if self._running:
                    logger.error(f"Receive error: {e}")
                break

    def _handle_message(self, msg: dict) -> None:
        """Process incoming WebSocket message."""
        msg_type = msg.get("type", "")
        # Filter very spammy delta logs if needed, but debug is fine
        # logger.debug(f"Received message type: {msg_type}")

        if msg_type == "session.created":
            self._session_id = msg.get("session", {}).get("id")
            logger.info(f"Session created: {self._session_id}")
            self._response_in_progress = False

        elif msg_type == "session.updated":
            logger.debug("Session updated")

        elif msg_type == "response.created":
            self._response_in_progress = True
            logger.debug("Response created (active)")

        elif msg_type == "response.audio.delta":
            # Streaming audio chunk
            audio_b64 = msg.get("delta", "")
            if audio_b64:
                audio_bytes = base64.b64decode(audio_b64)
                self._audio_buffer.append(audio_bytes)
                if self._on_audio:
                    self._on_audio(audio_bytes)

        elif msg_type == "response.audio_transcript.delta":
            # Streaming transcript
            delta = msg.get("delta", "")
            self._transcript_buffer += delta

        elif msg_type == "response.text.delta":
            # Text response delta
            delta = msg.get("delta", "")
            self._transcript_buffer += delta
            if self._on_transcript:
                self._on_transcript(delta)

        elif msg_type == "response.done":
            # Response complete
            self._response_in_progress = False
            response = msg.get("response", {})
            logger.debug(f"Response done: {response.get('id')}")

            # Combine audio buffer
            full_audio = b"".join(self._audio_buffer)
            full_transcript = self._transcript_buffer

            # Put in queue for sync access
            self._response_queue.put({
                "transcript": full_transcript,
                "audio": full_audio,
                "response": response
            })

            # Call done callback
            if self._on_response_done:
                self._on_response_done(full_transcript, full_audio)

            # Reset buffers
            self._audio_buffer = []
            self._transcript_buffer = ""

        elif msg_type == "input_audio_buffer.speech_started":
            logger.info(">>> Speech detected - listening...")
            # User speech interrupts the model automatically on server side,
            # but we update our flag to be safe
            self._response_in_progress = False

        elif msg_type == "input_audio_buffer.speech_stopped":
            logger.info(">>> Speech ended - processing...")

        elif msg_type == "conversation.item.input_audio_transcription.completed":
            # User's speech transcribed
            transcript = msg.get("transcript", "")
            logger.info(f"User said: {transcript}")

        elif msg_type == "error":
            error = msg.get("error", {})
            error_msg = error.get("message", str(error))

            # Ignore harmless cancellation errors
            if "no active response found" in error_msg:
                logger.debug(f"Ignored cancellation error: {error_msg}")
                return
            
            # Handle collision error specifically
            if "already has an active response" in error_msg:
                logger.warning(f"Response collision: {error_msg}")
                # We assume the previous response is still going, so we leave the flag True.
                # Or should we force it False to unstick? Safer to leave True until done.
                return

            logger.error(f"GPT-Realtime error: {error_msg}")
            # If a fatal error occurs, reset state
            self._response_in_progress = False
            
            if self._on_error:
                self._on_error(error_msg)

        elif msg_type == "rate_limits.updated":
            # Rate limit info
            pass

    def send_audio(self, audio_data: np.ndarray, sample_rate: int = 16000) -> None:
        """Send audio data to the realtime API.

        Args:
            audio_data: Audio as numpy float32 array (range -1 to 1)
            sample_rate: Sample rate of input audio (default 16000)
        """
        if not self._connected:
            logger.warning("Not connected, cannot send audio")
            return

        # Resample to 24kHz if needed (GPT-realtime expects 24kHz)
        if sample_rate != 24000:
            try:
                import scipy.signal
                # Calculate resampling ratio
                ratio = 24000 / sample_rate
                new_length = int(len(audio_data) * ratio)
                audio_24k = scipy.signal.resample(audio_data, new_length)
            except ImportError:
                # Fallback: simple linear interpolation
                indices = np.linspace(0, len(audio_data) - 1, int(len(audio_data) * 24000 / sample_rate))
                audio_24k = np.interp(indices, np.arange(len(audio_data)), audio_data)
        else:
            audio_24k = audio_data

        # Convert to 16-bit PCM
        audio_int16 = (audio_24k * 32767).astype(np.int16)
        audio_bytes = audio_int16.tobytes()

        # Encode as base64
        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")

        # Send audio buffer append
        if not hasattr(self, '_audio_send_count'):
            self._audio_send_count = 0
        self._audio_send_count += 1
        if self._audio_send_count % 50 == 1:  # Log every 50th packet (~5 seconds)
            logger.info(f"Sending audio packet #{self._audio_send_count}, {len(audio_bytes)} bytes")

        self._send({
            "type": "input_audio_buffer.append",
            "audio": audio_b64
        })

    def commit_audio(self) -> None:
        """Commit the audio buffer (when using manual turn detection)."""
        if not self._connected:
            return
        self._send({"type": "input_audio_buffer.commit"})

    def create_response(self, modalities: list[str] = None) -> None:
        """Request the model to generate a response.
        
        Args:
            modalities: Optional list of modalities to force for this response (e.g. ["text", "audio"])
        """
        if not self._connected:
            return
        
        # If we are interrupting, we rely on send_text to have cancelled already
        payload = {"type": "response.create"}
        if modalities:
            payload["response"] = {"modalities": modalities}
            
        self._send(payload)

    def cancel_response(self) -> None:
        """Cancel the current response generation."""
        if not self._connected:
            return
        logger.info("Canceling active response")
        self._send({"type": "response.cancel"})
        self._response_in_progress = False

    def clear_audio_buffer(self) -> None:
        """Clear the input audio buffer."""
        if not self._connected:
            return
        self._send({"type": "input_audio_buffer.clear"})

    def interrupt(self) -> None:
        """Interrupt current response and clear buffers."""
        self.cancel_response()
        self.clear_audio_buffer()

    def send_text(self, text: str, generate_response: bool = True) -> None:
        """Send a text message (as if user typed it).

        Args:
            text: The text message to send
            generate_response: Whether to immediately request a response
        """
        if not self._connected:
            logger.warning("send_text called but not connected")
            return
        
        # Prevent "conversation already has active response"
        if self._response_in_progress:
            logger.warning(f"Interrupting active response to send new text: {text[:30]}...")
            self.cancel_response()
            # Small delay to allow server to process cancel? Usually not needed if pipelining.
            # But let's be safe.
            # time.sleep(0.05) 

        logger.info(f"Sending text prompt ({len(text)} chars): {text[:100]}...")

        # Create conversation item
        self._send({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": text
                }]
            }
        })

        if generate_response:
            logger.info("Requesting response from GPT-Realtime (forcing text+audio)")
            self.create_response(modalities=["text", "audio"])

    def update_instructions(self, instructions: str) -> None:
        """Update the system instructions mid-session.

        Args:
            instructions: New system instructions
        """
        if not self._connected:
            return

        self.config.instructions = instructions
        self._send({
            "type": "session.update",
            "session": {
                "instructions": instructions
            }
        })

    def get_response(self, timeout: float = 30.0) -> Optional[dict]:
        """Wait for and return the next complete response.

        Args:
            timeout: Maximum time to wait in seconds

        Returns:
            Response dict with 'transcript' and 'audio' keys, or None on timeout
        """
        try:
            return self._response_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def set_callbacks(
        self,
        on_transcript: Optional[Callable[[str], None]] = None,
        on_audio: Optional[Callable[[bytes], None]] = None,
        on_response_done: Optional[Callable[[str, bytes], None]] = None,
        on_error: Optional[Callable[[str], None]] = None
    ) -> None:
        """Set callback functions for streaming events.

        Args:
            on_transcript: Called with text chunks as they arrive
            on_audio: Called with audio chunks as they arrive
            on_response_done: Called when full response is complete (text, audio)
            on_error: Called on errors
        """
        self._on_transcript = on_transcript
        self._on_audio = on_audio
        self._on_response_done = on_response_done
        self._on_error = on_error


class GPTRealtimeBackend:
    """LLM Backend using GPT-Realtime for voice-to-voice coaching.

    This backend uses WebSocket streaming for low-latency voice interactions.
    It can handle both audio input and text input.
    """

    def __init__(
        self,
        model: str = "gpt-realtime",
        voice: str = "alloy",
        endpoint: Optional[str] = None,
        api_key: Optional[str] = None
    ):
        """Initialize GPT-Realtime backend.

        Args:
            model: Model/deployment name (default: gpt-realtime)
            voice: Voice for audio output (default: alloy)
            endpoint: Azure endpoint URL (uses env var if not provided)
            api_key: API key (uses env var if not provided)
        """
        self.model = model

        self.config = RealtimeConfig(
            deployment=model,
            voice=voice,
            api_key=api_key or os.environ.get("AZURE_REALTIME_API_KEY"),
        )

        if endpoint:
            self.config.endpoint = endpoint

        self._client: Optional[GPTRealtimeClient] = None
        self._last_audio_response: Optional[bytes] = None

    def _ensure_connected(self) -> bool:
        """Ensure client is connected."""
        if self._client is None:
            self._client = GPTRealtimeClient(self.config)

        if not self._client.is_connected:
            return self._client.connect()

        return True

    def complete(self, system_prompt: str, user_message: str) -> str:
        """Get completion via text (falls back to realtime API with text input).

        Args:
            system_prompt: System instructions
            user_message: User's message

        Returns:
            Response text
        """
        import time

        if not self._ensure_connected():
            return "Error: Could not connect to GPT-Realtime"

        start = time.perf_counter()

        # Update instructions if different
        if system_prompt != self.config.instructions:
            self._client.update_instructions(system_prompt)
            self.config.instructions = system_prompt

        # Send text message
        self._client.send_text(user_message)

        # Wait for response
        response = self._client.get_response(timeout=30.0)

        elapsed = (time.perf_counter() - start) * 1000

        if response:
            transcript = response.get("transcript", "")
            self._last_audio_response = response.get("audio")
            logger.info(f"[GPT-REALTIME] Text response in {elapsed:.0f}ms: {len(transcript)} chars")
            return transcript
        else:
            logger.warning("GPT-Realtime response timeout")
            return "Error: Response timeout"

    def complete_with_audio(
        self,
        system_prompt: str,
        context: str,
        audio_data: np.ndarray,
        sample_rate: int = 16000
    ) -> str:
        """Get completion from audio input.

        Args:
            system_prompt: System instructions
            context: Text context to include
            audio_data: Audio as numpy float32 array
            sample_rate: Audio sample rate

        Returns:
            Response text
        """
        import time

        if not self._ensure_connected():
            return "Error: Could not connect to GPT-Realtime"

        start = time.perf_counter()

        # Update instructions with context
        full_instructions = f"{system_prompt}\n\nCurrent context:\n{context}"
        if full_instructions != self.config.instructions:
            self._client.update_instructions(full_instructions)
            self.config.instructions = full_instructions

        # Clear any previous audio
        self._client.clear_audio_buffer()

        # Send audio
        self._client.send_audio(audio_data, sample_rate)

        # Commit and request response (for manual VAD mode)
        if self.config.turn_detection_type == "none":
            self._client.commit_audio()
            self._client.create_response()

        # Wait for response
        response = self._client.get_response(timeout=30.0)

        elapsed = (time.perf_counter() - start) * 1000

        if response:
            transcript = response.get("transcript", "")
            self._last_audio_response = response.get("audio")
            logger.info(f"[GPT-REALTIME] Audio response in {elapsed:.0f}ms: {len(transcript)} chars")
            return transcript
        else:
            logger.warning("GPT-Realtime response timeout")
            return "Error: Response timeout"

    def get_last_audio_response(self) -> Optional[bytes]:
        """Get the audio from the last response (PCM16 at 24kHz).

        Returns:
            Audio bytes or None if no audio available
        """
        return self._last_audio_response

    def disconnect(self) -> None:
        """Close the WebSocket connection."""
        if self._client:
            self._client.disconnect()
            self._client = None
