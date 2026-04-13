"""OpenAI-compatible API backend for LLM coaching.

Handles both online (mtgacoach.com) and local (Ollama/LM Studio) modes
through the same OpenAI-compatible chat completions interface.
"""

import logging
import threading
from typing import Optional

from arenamcp.client_metadata import get_client_headers

logger = logging.getLogger(__name__)

# Online mode: hardcoded API endpoint
ONLINE_BASE_URL = "https://api.mtgacoach.com/v1"

# Default local endpoint (Ollama)
DEFAULT_LOCAL_URL = "http://localhost:11434/v1"
DEFAULT_LOCAL_MODEL = "llama3.2"


class ProxyBackend:
    """LLM backend using OpenAI-compatible chat completions API.

    In online mode, routes through api.mtgacoach.com with the user's
    license key. In local mode, connects to a user-configured endpoint
    (Ollama, LM Studio, or any OpenAI-compatible server).
    """

    def __init__(
        self,
        model: str = "gpt-5.4",
        enable_thinking: bool = False,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        self.model = model
        self.enable_thinking = enable_thinking
        self._base_url = base_url
        self._api_key = api_key
        self._client = None

        # Fire-and-forget warmup for Ollama to pre-load the model
        self._ollama_warmup()

    @classmethod
    def create_online(cls, model: Optional[str] = None, license_key: str = "") -> "ProxyBackend":
        """Create a backend configured for online mode (mtgacoach.com)."""
        return cls(
            model=model or "gpt-5.4",
            base_url=ONLINE_BASE_URL,
            api_key=license_key,
        )

    @classmethod
    def create_local(
        cls,
        model: Optional[str] = None,
        url: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> "ProxyBackend":
        """Create a backend configured for local mode (Ollama/LM Studio)."""
        return cls(
            model=model or DEFAULT_LOCAL_MODEL,
            base_url=url or DEFAULT_LOCAL_URL,
            api_key=api_key or "ollama",
        )

    def _get_client(self):
        """Lazy init of OpenAI client."""
        if self._client is None:
            try:
                from openai import OpenAI

                url = self._base_url or DEFAULT_LOCAL_URL
                key = self._api_key or "ollama"
                client_headers = get_client_headers() if url == ONLINE_BASE_URL else None

                self._client = OpenAI(
                    base_url=url,
                    api_key=key,
                    default_headers=client_headers,
                )
            except ImportError:
                raise ImportError("openai package required: pip install openai")
        return self._client

    def _ollama_warmup(self) -> None:
        """Send a minimal warmup request to Ollama in a background thread."""
        url = self._base_url or ""
        key = self._api_key or ""
        is_ollama = ("localhost:11434" in url or "127.0.0.1:11434" in url or
                     key == "ollama")
        if not is_ollama:
            return

        def _warmup():
            try:
                client = self._get_client()
                client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": "hi"}],
                    max_tokens=1,
                )
                logger.info(f"[PROXY] Ollama warmup complete for model {self.model}")
            except Exception as e:
                logger.debug(f"[PROXY] Ollama warmup failed (non-fatal): {e}")

        t = threading.Thread(target=_warmup, daemon=True)
        t.start()

    def complete(self, system_prompt: str, user_message: str, max_tokens: int = 400) -> str:
        """Get completion from the API endpoint."""
        import time

        try:
            client = self._get_client()

            params = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                "max_completion_tokens": max_tokens,
                "temperature": 0.3,
            }

            model_lower = self.model.lower()
            extra = {}
            if self.enable_thinking:
                if "claude" in model_lower:
                    extra["thinking"] = {"type": "enabled", "budget_tokens": 8000}
                    params["max_completion_tokens"] = max_tokens + 8000
                elif "gemini" in model_lower:
                    extra["thinking_config"] = {"thinking_budget": 4096}
            else:
                if "claude" in model_lower:
                    extra["thinking"] = {"type": "disabled"}
                if "gemini" in model_lower:
                    extra["thinking_config"] = {"thinking_budget": 0}
            if extra:
                params["extra_body"] = extra

            request_start = time.perf_counter()

            # Try streaming first for lower perceived latency
            try:
                stream = client.chat.completions.create(**params, stream=True)
                chunks: list[str] = []
                for chunk in stream:
                    if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                        chunks.append(chunk.choices[0].delta.content)
                content = "".join(chunks)
                request_time = (time.perf_counter() - request_start) * 1000
                logger.info(
                    f"[PROXY] API (streamed): {request_time:.0f}ms, model: {self.model}"
                )
                return content
            except Exception as stream_err:
                logger.debug(f"[PROXY] Streaming failed, falling back to non-streaming: {stream_err}")

            # Fallback: non-streaming request
            request_start = time.perf_counter()
            response = client.chat.completions.create(**params)
            request_time = (time.perf_counter() - request_start) * 1000

            content = response.choices[0].message.content
            usage = getattr(response, 'usage', None)
            tokens_info = ""
            if usage:
                tokens_info = f", in={usage.prompt_tokens}, out={usage.completion_tokens}"
            logger.info(
                f"[PROXY] API: {request_time:.0f}ms, model: {self.model}{tokens_info}"
            )
            return content
        except Exception as e:
            logger.error(f"API error: {e}")
            return f"Error getting advice: {e}"

    def complete_with_image(self, system_prompt: str, user_message: str, image_bytes: bytes) -> str:
        """Get completion with an image via the OpenAI multimodal message format."""
        import base64
        import time

        try:
            client = self._get_client()
            b64 = base64.b64encode(image_bytes).decode("utf-8")

            params = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                        {"type": "text", "text": user_message},
                    ]},
                ],
                "max_completion_tokens": 600,
                "temperature": 0.3,
            }

            model_lower = self.model.lower()
            extra = {}
            if "claude" in model_lower:
                extra["thinking"] = {"type": "disabled"}
            if "gemini" in model_lower:
                extra["thinking_config"] = {"thinking_budget": 0}
            if extra:
                params["extra_body"] = extra

            request_start = time.perf_counter()
            response = client.chat.completions.create(**params)
            request_time = (time.perf_counter() - request_start) * 1000

            content = response.choices[0].message.content
            logger.info(f"[PROXY] Vision API: {request_time:.0f}ms, model: {self.model}")
            return content
        except Exception as e:
            logger.error(f"Vision API error: {e}")
            return f"Error getting vision analysis: {e}"

    def list_models(self) -> list[str]:
        """List available models from the endpoint."""
        try:
            client = self._get_client()
            models = client.models.list()
            return [m.id for m in models.data]
        except Exception as e:
            logger.error(f"Failed to list models: {e}")
            return []
