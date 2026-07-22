"""OpenAI-compatible API backend for LLM coaching.

Handles both online (mtgacoach.com) and local (Ollama/LM Studio) modes
through the same OpenAI-compatible chat completions interface.
"""

import json
import logging
import os
import re
import threading
import time
from typing import Optional

from arenamcp.client_metadata import get_client_headers

logger = logging.getLogger(__name__)


# Prompt capture hook for the eval harness (tools/eval). Always-off unless
# MTGACOACH_PROMPT_DUMP_PATH points at a writable JSONL file. Each .complete()
# call appends one line: {"ts","model","system","user","max_tokens","temperature"}.
# Zero overhead when the env var is unset.
_CAPTURE_LOCK = threading.Lock()


def _maybe_capture_prompt(
    model: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    temperature: float,
) -> None:
    path = os.environ.get("MTGACOACH_PROMPT_DUMP_PATH", "")
    if not path:
        return
    try:
        record = {
            "ts": time.time(),
            "model": model,
            "system": system_prompt,
            "user": user_message,
            "max_tokens": max_tokens,
            "temperature": temperature,
            # Record which prompt variant produced this capture so the
            # eval-side ablation can split captures by variant later.
            # See coach.py: _build_context.
            "prompt_variant": os.environ.get("MTGACOACH_PROMPT_VARIANT", "default").lower(),
        }
        line = json.dumps(record, ensure_ascii=False)
        with _CAPTURE_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:
        # Capture must never break a real coach call.
        logger.debug(f"prompt-capture write failed: {e}")

# Closed think-tag blocks inside reasoning text (DeepSeek/Qwen style).
_THINK_BLOCK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.IGNORECASE | re.DOTALL)

# Leading phrases that mark deliberation rather than a final answer.
_COT_MARKERS = (
    "okay,", "okay ", "ok,", "ok ", "hmm", "wait,", "let me", "let's",
    "i need to", "i should", "we need", "we should", "the user", "first,",
    "so the", "thinking about", "looking at",
)


def _salvage_reasoning_answer(reasoning: str) -> str:
    """Extract a clean final answer from reasoning text, or return "".

    Some OpenAI-compatible servers put the entire response in the reasoning
    field when the visible content is empty. Speaking raw chain-of-thought
    to the user is worse than no advice, so this is deliberately
    conservative: strip closed think-tag blocks (the remainder is the real
    answer), reject truncated/unclosed thinking, and for tag-free text keep
    only a trailing paragraph that doesn't read like deliberation. An empty
    return sends the caller down the existing empty-advice fallback path.
    """
    text = (reasoning or "").strip()
    if not text:
        return ""
    stripped = _THINK_BLOCK_RE.sub("", text).strip()
    if "<think" in stripped.lower():
        # Unclosed think block — truncated chain-of-thought, no final answer.
        return ""
    if stripped and stripped != text:
        return stripped
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", stripped) if p.strip()]
    if not paragraphs:
        return ""
    candidate = paragraphs[-1]
    if any(candidate.lower().startswith(m) for m in _COT_MARKERS):
        return ""
    if len(paragraphs) == 1 and len(candidate) > 1200:
        # One giant undifferentiated block reads as chain-of-thought.
        return ""
    return candidate


# Online mode: hardcoded API endpoint
ONLINE_BASE_URL = "https://api.mtgacoach.com/v1"

# Default local endpoint (vLLM). Ollama lives at :11434 if a user wants to fall back.
DEFAULT_LOCAL_URL = "http://localhost:8000/v1"
DEFAULT_LOCAL_MODEL = "gemma4:e2b"


class BackendError(Exception):
    """Typed API failure for consumers that must branch on error semantics.

    Replaces the "Error getting advice: ..." prose sentinel for callers
    that pass raise_on_error=True (the autopilot planner). Carries enough
    structure that retry policy lives HERE, not in string matching.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        retry_after_s: Optional[float] = None,
        status_code: Optional[int] = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after_s = retry_after_s
        self.status_code = status_code


# HTTP statuses worth one bounded retry: transient gateway/origin trouble.
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def _classify_api_error(e: Exception) -> BackendError:
    """Wrap an SDK/network exception in a BackendError with retry semantics."""
    status = getattr(e, "status_code", None)
    retry_after: Optional[float] = None
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        ra = body.get("retry_after")
        try:
            retry_after = float(ra) if ra is not None else None
        except (TypeError, ValueError):
            retry_after = None
    retryable = status in _RETRYABLE_STATUS_CODES
    if status is None:
        # Connection-level failures (reset, refused, DNS) — the SDK raises
        # APIConnectionError without a status. Treat as retryable.
        retryable = type(e).__name__ in (
            "APIConnectionError",
            "APITimeoutError",
            "ConnectionError",
        )
    return BackendError(
        str(e), retryable=retryable, retry_after_s=retry_after, status_code=status
    )


class ProxyBackend:
    """LLM backend using OpenAI-compatible chat completions API.

    In online mode, routes through api.mtgacoach.com with the user's
    license key. In local mode, connects to a user-configured endpoint
    (Ollama, LM Studio, or any OpenAI-compatible server).
    """

    def __init__(
        self,
        model: str = "gemma-4-12b-it",
        enable_thinking: bool = False,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        self.model = model
        self.enable_thinking = enable_thinking
        self._base_url = base_url
        self._api_key = api_key
        self._client = None
        # What the server said it actually ran (R4: gateway aliases lie).
        self.last_served_model: Optional[str] = None
        self._served_model_warned = False

        # Fire-and-forget warmup for any local backend to pre-load weights/KV cache
        self._local_warmup()

    @classmethod
    def create_online(cls, model: Optional[str] = None, license_key: str = "") -> "ProxyBackend":
        """Create a backend configured for online mode (mtgacoach.com)."""
        return cls(
            # The api.mtgacoach.com gateway (LiteLLM on the NAS) owns model
            # routing. Default to the REAL served model name, not the legacy
            # 'nemotron-3-super' alias (which just routes to deepseek-v4-flash
            # and confusingly implies a Nemotron model that isn't there).
            model=model or "gemma-4-12b-it",
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
        """Create a backend configured for local mode (vLLM/Ollama/LM Studio)."""
        return cls(
            model=model or DEFAULT_LOCAL_MODEL,
            base_url=url or DEFAULT_LOCAL_URL,
            api_key=api_key or "vllm",
        )

    # Hard ceiling applied at the SDK level. Per-call request_timeout_s in
    # complete() can tighten this. Without a finite client-level timeout,
    # the OpenAI SDK defaults to ~10 minutes — which means a hung backend
    # leaves the worker thread alive long after the future times out, and
    # that's the thread leak the autopilot+coach were paying for.
    _CLIENT_HARD_TIMEOUT_S = 60.0

    def _get_client(self):
        """Lazy init of OpenAI client."""
        if self._client is None:
            try:
                from openai import OpenAI

                url = self._base_url or DEFAULT_LOCAL_URL
                key = self._api_key or "ollama"
                client_headers = get_client_headers() if url == ONLINE_BASE_URL else None

                # max_retries=0: the SDK's default (2) honors Retry-After
                # headers between attempts, and those sleeps are NOT capped
                # by the request timeout — a gateway 502 with Retry-After: 60
                # wedged one decision call for 2+ minutes while the mulligan
                # window flew by (2026-07-01). Real-time coaching would rather
                # fail fast and let the trigger/fallback layer decide.
                self._client = OpenAI(
                    base_url=url,
                    api_key=key,
                    default_headers=client_headers,
                    timeout=self._CLIENT_HARD_TIMEOUT_S,
                    max_retries=0,
                )
            except ImportError:
                raise ImportError("openai package required: pip install openai")
        return self._client

    def _local_warmup(self) -> None:
        """Send a minimal warmup request to a local backend in a background thread.

        Fires for any non-online endpoint (vLLM/Ollama/LM Studio/etc.) so the
        first real coach call doesn't pay the cold-start cost.
        """
        url = self._base_url or ""
        if not url or url == ONLINE_BASE_URL:
            return
        # Only warm up obvious local URLs to avoid surprising arbitrary endpoints.
        is_local = ("localhost" in url or "127.0.0.1" in url or
                    url.startswith("http://0.0.0.0"))
        if not is_local:
            return

        def _warmup():
            try:
                client = self._get_client()
                client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": "hi"}],
                    max_tokens=1,
                )
                logger.info(f"[PROXY] Local warmup complete for model {self.model}")
            except Exception as e:
                logger.debug(f"[PROXY] Local warmup failed (non-fatal): {e}")

        t = threading.Thread(target=_warmup, daemon=True)
        t.start()

    def complete(
        self,
        system_prompt: str,
        user_message: str,
        # 4096 (was 1500): thinking-variant local models (gemma-4-31b-it)
        # burn hidden reasoning tokens inside this cap before any visible
        # output — 1500 returned EMPTY advice on large game-state prompts
        # (same failure as the eval harness hit 2026-06-09). A cap, not a
        # target: online models are unaffected.
        max_tokens: int = 4096,
        temperature: float = 0.3,
        request_timeout_s: Optional[float] = None,
        raise_on_error: bool = False,
    ) -> str:
        """Get completion from the API endpoint.

        Args:
            temperature: Sampling temperature. Default 0.3 for flavorful
                coach advice; pass 0.0 for deterministic planner calls
                (avoids cross-priority-window flip-flops).
            request_timeout_s: Hard deadline for the underlying HTTP call.
                When the SDK hits this, it tears down the socket and raises,
                which lets the calling worker thread exit cleanly. Without
                it, hung backends silently leak threads forever (the future
                timeout only abandons the thread, it doesn't kill it).
            raise_on_error: Re-raise API errors instead of returning the
                "Error getting advice: ..." sentinel string. The autopilot
                planner sets this — during the 2026-07-05 gateway outage the
                sentinel was fed to the JSON parser, parsed to 0 actions, and
                the fallback then SUBMITTED real passes on windows with
                castable spells. Sentinel-string returns are only safe for
                consumers that display text to a human.
        """
        import time

        _maybe_capture_prompt(self.model, system_prompt, user_message, max_tokens, temperature)

        try:
            client = self._get_client()
            if request_timeout_s is not None:
                client = client.with_options(timeout=request_timeout_s)

            params = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                "temperature": temperature,
            }

            model_lower = self.model.lower()
            is_gpt5 = "gpt-5" in model_lower or "gpt5" in model_lower or "o1" in model_lower or "o3" in model_lower
            is_gemini = "gemini" in model_lower

            if is_gpt5:
                params["max_completion_tokens"] = max_tokens
            else:
                params["max_tokens"] = max_tokens

            extra = {}
            if self.enable_thinking:
                if "claude" in model_lower:
                    extra["thinking"] = {"type": "enabled", "budget_tokens": 8000}
                    if "max_completion_tokens" in params:
                        params["max_completion_tokens"] = max_tokens + 8000
                    else:
                        params["max_tokens"] = max_tokens + 8000
                elif is_gemini:
                    # Gemini's OpenAI-compat endpoint rejects native fields
                    # like thinking_config. Use reasoning_effort instead.
                    params["reasoning_effort"] = "medium"
                elif is_gpt5:
                    params["reasoning_effort"] = "medium"
                    params["verbosity"] = "medium"
            else:
                extra["think"] = False
                if "claude" in model_lower:
                    extra["thinking"] = {"type": "disabled"}
                if is_gemini:
                    params["reasoning_effort"] = "none"
                if is_gpt5:
                    params["reasoning_effort"] = "minimal"
                    params["verbosity"] = "low"
            # GPT-5 reasoning models reject any temperature other than 1.0 when
            # reasoning_effort is set. Drop the param so Azure uses the default.
            if is_gpt5:
                params.pop("temperature", None)
            if extra:
                params["extra_body"] = extra

            # Bounded retry: one extra attempt for transient failures
            # (429/5xx/connection). A 60s Retry-After is useless mid-match —
            # skip the retry entirely when the server asks for a long wait.
            last_err: Optional[BackendError] = None
            for attempt in (1, 2):
                try:
                    return self._complete_once(client, params)
                except Exception as e:
                    err = _classify_api_error(e)
                    if (
                        err.retryable
                        and attempt == 1
                        and (err.retry_after_s is None or err.retry_after_s <= 5.0)
                    ):
                        wait = min(err.retry_after_s or 0.5, 1.0)
                        logger.warning(
                            f"API error (retryable): {e} — one retry in {wait:.1f}s"
                        )
                        time.sleep(wait)
                        continue
                    last_err = err
                    break
            last_err = last_err or BackendError("unknown API failure")
            logger.error(f"API error: {last_err}")
            if raise_on_error:
                raise last_err
            return f"Error getting advice: {last_err}"
        except BackendError:
            raise
        except Exception as e:
            # Setup failures (client init, params construction).
            logger.error(f"API error: {e}")
            if raise_on_error:
                raise _classify_api_error(e) from e
            return f"Error getting advice: {e}"

    def _note_served_model(self, served: Optional[str]) -> None:
        """Record the model the server actually ran (gateway aliases lie).

        The 2026-07-05 misroute (alias 'nemotron-3-super' → an ollama 12B)
        was invisible because [PROXY] logs printed the configured alias.
        """
        if not served:
            return
        previous = self.last_served_model
        self.last_served_model = served
        if served != previous:
            # Discoverability: one INFO line on first use and on every
            # routing change, so "what model am I talking to?" is always
            # answerable from the logs without living in the UI.
            logger.info(f"[PROXY] gateway served model: {served}")
        if served != self.model and not getattr(self, "_served_model_warned", False):
            self._served_model_warned = True
            logger.warning(
                f"[PROXY] served model {served!r} != configured alias "
                f"{self.model!r} — routing is gateway-side"
            )

    def _complete_once(self, client, params) -> str:
        """Single request attempt: streaming first, non-streaming fallback."""
        import time

        request_start = time.perf_counter()

        # Try streaming first for lower perceived latency
        try:
            stream = client.chat.completions.create(**params, stream=True)
            chunks: list[str] = []
            reasoning_chunks: list[str] = []
            served_model = None
            for chunk in stream:
                if served_model is None and getattr(chunk, "model", None):
                    served_model = chunk.model
                if chunk.choices and chunk.choices[0].delta:
                    delta = chunk.choices[0].delta

                    # Extract reasoning token if present
                    reasoning_token = None
                    if getattr(delta, "reasoning_content", None):
                        reasoning_token = delta.reasoning_content
                    elif getattr(delta, "model_extra", None) and delta.model_extra.get("reasoning"):
                        reasoning_token = delta.model_extra.get("reasoning")
                    elif getattr(delta, "reasoning", None):
                        reasoning_token = delta.reasoning

                    if reasoning_token:
                        reasoning_chunks.append(reasoning_token)

                    if delta.content:
                        chunks.append(delta.content)

            if reasoning_chunks:
                reasoning_str = "".join(reasoning_chunks)
                logger.debug(f"[PROXY] Model reasoning:\n{reasoning_str}")

            self._note_served_model(served_model)
            content = "".join(chunks)
            if not content and reasoning_chunks:
                salvaged = _salvage_reasoning_answer("".join(reasoning_chunks))
                if salvaged:
                    logger.warning(
                        f"[PROXY] Empty streamed content — using answer "
                        f"salvaged from reasoning text ({len(salvaged)} chars)"
                    )
                    content = salvaged
                else:
                    logger.warning(
                        "[PROXY] Empty streamed content and reasoning text "
                        "has no clean final answer — returning empty so the "
                        "caller's fallback advice takes over"
                    )
            request_time = (time.perf_counter() - request_start) * 1000
            logger.info(
                f"[PROXY] API (streamed): {request_time:.0f}ms, "
                f"model: {self.model}, served: {served_model or '?'}"
            )
            return content or ""
        except Exception as stream_err:
            # Streaming transport quirks fall through to non-streaming, but a
            # real API failure (auth, 5xx, connection) would fail identically
            # there — reclassify and re-raise those for the retry loop.
            err = _classify_api_error(stream_err)
            if err.retryable or err.status_code is not None:
                raise
            logger.debug(f"[PROXY] Streaming failed, falling back to non-streaming: {stream_err}")

        # Fallback: non-streaming request
        request_start = time.perf_counter()
        response = client.chat.completions.create(**params)
        request_time = (time.perf_counter() - request_start) * 1000

        message = response.choices[0].message
        content = message.content

        # Extract reasoning
        reasoning = None
        if getattr(message, "reasoning_content", None):
            reasoning = message.reasoning_content
        elif getattr(message, "model_extra", None) and message.model_extra.get("reasoning"):
            reasoning = message.model_extra.get("reasoning")
        elif getattr(message, "reasoning", None):
            reasoning = message.reasoning
        if reasoning:
            logger.debug(f"[PROXY] Model reasoning:\n{reasoning}")

        if not content and reasoning:
            salvaged = _salvage_reasoning_answer(reasoning)
            if salvaged:
                logger.warning(
                    f"[PROXY] Empty content — using answer salvaged from "
                    f"reasoning text ({len(salvaged)} chars)"
                )
                content = salvaged
            else:
                logger.warning(
                    "[PROXY] Empty content and reasoning text has no clean "
                    "final answer — returning empty so the caller's "
                    "fallback advice takes over"
                )

        served_model = getattr(response, "model", None)
        self._note_served_model(served_model)
        usage = getattr(response, 'usage', None)
        tokens_info = ""
        if usage:
            tokens_info = f", in={usage.prompt_tokens}, out={usage.completion_tokens}"
        logger.info(
            f"[PROXY] API: {request_time:.0f}ms, model: {self.model}, "
            f"served: {served_model or '?'}{tokens_info}"
        )
        return content or ""

    # Consecutive image-completion failures before the vision path disables
    # itself. Live 2026-07-06: the gateway model (deepseek-v4-flash) can't
    # serve vision and the tunnel chokes on MB image payloads — the vision
    # watchdog burned a failing call pair every ~40s all match.
    _VISION_DISABLE_AFTER = 3

    def complete_with_image(
        self,
        system_prompt: str,
        user_message: str,
        image_bytes: bytes,
        request_timeout_s: Optional[float] = None,
    ) -> str:
        """Get completion with an image via the OpenAI multimodal message format."""
        import base64
        import time

        if getattr(self, "_vision_dead", False):
            return (
                "Error getting vision analysis: vision endpoint disabled "
                "after repeated failures"
            )

        try:
            client = self._get_client()
            if request_timeout_s is not None:
                client = client.with_options(timeout=request_timeout_s)
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
            is_gpt5 = "gpt-5" in model_lower or "gpt5" in model_lower
            is_gemini = "gemini" in model_lower
            if "claude" in model_lower:
                extra["thinking"] = {"type": "disabled"}
            if is_gemini:
                # OpenAI-compat path: use reasoning_effort, not thinking_config.
                params["reasoning_effort"] = "none"
            if is_gpt5:
                params["reasoning_effort"] = "minimal"
                params["verbosity"] = "low"
                # GPT-5 reasoning models reject temperature != 1.0 when
                # reasoning_effort is set. Drop it so Azure uses the default.
                params.pop("temperature", None)
            if extra:
                params["extra_body"] = extra

            request_start = time.perf_counter()
            response = client.chat.completions.create(**params)
            request_time = (time.perf_counter() - request_start) * 1000

            content = response.choices[0].message.content
            logger.info(f"[PROXY] Vision API: {request_time:.0f}ms, model: {self.model}")
            self._vision_fail_count = 0
            return content
        except Exception as e:
            logger.error(f"Vision API error: {e}")
            self._vision_fail_count = getattr(self, "_vision_fail_count", 0) + 1
            if self._vision_fail_count >= self._VISION_DISABLE_AFTER:
                self._vision_dead = True
                logger.warning(
                    "[PROXY] Disabling image completions after "
                    f"{self._vision_fail_count} consecutive failures — the "
                    "configured backend/gateway cannot serve vision requests"
                )
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
