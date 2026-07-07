"""AI provider routing — forwards requests to the best available provider."""

import logging
import os
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class Provider:
    """A single AI provider endpoint."""

    def __init__(self, name: str, base_url: str, api_key: str,
                 models: list[str], priority: int = 10, api_version: str = ""):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = self._resolve_env(api_key)
        self.models = models
        self.priority = priority
        self.api_version = api_version
        self.enabled = bool(self.api_key)
        self._consecutive_failures = 0
        self._last_failure_time = 0.0
        self._backoff_until = 0.0

    @staticmethod
    def _resolve_env(value: str) -> str:
        """Resolve ${ENV_VAR} references in config values."""
        if value.startswith("${") and value.endswith("}"):
            env_name = value[2:-1]
            return os.environ.get(env_name, "")
        return value

    @property
    def available(self) -> bool:
        """Check if provider is enabled and not in backoff."""
        if not self.enabled:
            return False
        if self._backoff_until > time.time():
            return False
        return True

    def mark_failure(self):
        """Mark a request failure — triggers exponential backoff."""
        self._consecutive_failures += 1
        self._last_failure_time = time.time()
        backoff = min(300, 2 ** self._consecutive_failures)
        self._backoff_until = time.time() + backoff
        logger.warning(f"Provider {self.name} failed ({self._consecutive_failures}x), "
                       f"backing off {backoff}s")

    def mark_success(self):
        """Reset failure state on success."""
        self._consecutive_failures = 0
        self._backoff_until = 0.0

    @property
    def is_azure(self) -> bool:
        return "azure" in self.name.lower()

    @property
    def is_gemini(self) -> bool:
        return "gemini" in self.name.lower() or "generativelanguage.googleapis" in self.base_url

    def _build_headers(self) -> dict:
        """Build provider-specific headers."""
        headers = {"Content-Type": "application/json"}

        if "anthropic" in self.base_url:
            headers["x-api-key"] = self.api_key
            headers["anthropic-version"] = "2023-06-01"
        elif self.is_azure:
            headers["api-key"] = self.api_key
        else:
            headers["Authorization"] = f"Bearer {self.api_key}"

        return headers

    def _prepare_body(self, body: dict) -> dict:
        """Adjust request body for provider-specific quirks."""
        if self.is_gemini:
            body = dict(body)
            # Gemini's OpenAI-compat layer rejects unknown vendor-specific
            # fields with a 400. Strip the ones older desktop clients still
            # send (thinking_config is native-Gemini-only — the OpenAI-compat
            # path uses reasoning_effort instead).
            for k in ("thinking_config", "thinking", "verbosity"):
                body.pop(k, None)
            # Cross-provider failover: when Azure 429s and we fail over to
            # Gemini, the body still carries model="gpt-5.4". Gemini rejects
            # that with 404. Substitute the first configured Gemini model
            # so failover actually serves a response.
            req_model = body.get("model", "")
            if req_model and req_model not in self.models and self.models:
                body["model"] = self.models[0]
            return body
        if not self.is_azure:
            # OpenAI / Anthropic / local vLLM: remap unknown model → first configured.
            body = dict(body)
            req_model = body.get("model", "")
            remapped = bool(req_model and self.models and req_model not in self.models)
            if remapped:
                body["model"] = self.models[0]
                # The client thought it was talking to a GPT-5-class reasoning
                # model (e.g. "gpt-5.4") and may have sent reasoning_effort /
                # verbosity. A local model like Gemma 4 *honors* reasoning_effort
                # by emitting everything into the reasoning channel and leaving
                # `content` empty — which surfaces to the user as "empty advice".
                # Since we've remapped to a different model, strip those
                # GPT-5-specific controls so the model returns normal content.
                for k in (
                    "reasoning_effort",
                    "reasoning",
                    "thinking_config",
                    "thinking",
                    "verbosity",
                ):
                    body.pop(k, None)
            return body
        body = dict(body)
        # Azure uses the deployment name in the URL, not the model field —
        # remove it to avoid 400 errors when the client sends a non-Azure
        # model name (e.g. "claude-sonnet-4-5-20250929").
        body.pop("model", None)
        # Azure uses max_completion_tokens, not max_tokens
        if "max_tokens" in body:
            body["max_completion_tokens"] = body.pop("max_tokens")
        return body

    def _build_url(self, path: str, model: str = "") -> str:
        """Build the full URL for a request."""
        if self.is_azure and self.api_version:
            # Azure uses: {base}/deployments/{deployment}/chat/completions?api-version=X
            # If the requested model name matches an Azure deployment we serve,
            # route there; otherwise fall back to the first configured deployment
            # (covers cases where the client sends a non-Azure model name like
            # "claude-sonnet" but we still want to serve via our default Azure
            # deployment).
            if model and model in self.models:
                deployment = model
            else:
                deployment = self.models[0] if self.models else "gpt-5.4"
            url = f"{self.base_url}/deployments/{deployment}/{path.lstrip('/')}"
            sep = "&" if "?" in url else "?"
            return f"{url}{sep}api-version={self.api_version}"
        return f"{self.base_url}/{path.lstrip('/')}"

    async def forward_chat(self, body: dict, client: httpx.AsyncClient) -> httpx.Response:
        """Forward a chat completion request to this provider."""
        model = body.get("model", "")
        url = self._build_url("chat/completions", model=model)
        headers = self._build_headers()
        body = self._prepare_body(body)

        response = await client.post(
            url,
            json=body,
            headers=headers,
            timeout=120.0,
        )
        return response

    async def forward_chat_stream(self, body: dict, client: httpx.AsyncClient):
        """Forward a streaming chat completion request."""
        model = body.get("model", "")
        url = self._build_url("chat/completions", model=model)
        headers = self._build_headers()
        body = self._prepare_body(body)

        async with client.stream(
            "POST",
            url,
            json=body,
            headers=headers,
            timeout=120.0,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                yield line


class ProviderRouter:
    """Routes requests to the best available provider."""

    def __init__(self):
        self.providers: list[Provider] = []

    def load_from_config(self, provider_configs: list[dict]):
        """Load providers from config dicts. Replaces the current provider list."""
        new_providers: list[Provider] = []
        for cfg in provider_configs:
            if not cfg.get("enabled", True):
                continue
            p = Provider(
                name=cfg["name"],
                base_url=cfg["base_url"],
                api_key=cfg.get("api_key", ""),
                models=cfg.get("models", []),
                priority=cfg.get("priority", 10),
                api_version=cfg.get("api_version", ""),
            )
            if p.enabled:
                new_providers.append(p)
                logger.info(f"Loaded provider: {p.name} (priority={p.priority}, "
                            f"models={p.models})")
            else:
                logger.warning(f"Skipping provider {p.name}: no API key")

        new_providers.sort(key=lambda p: p.priority)
        self.providers = new_providers

    def get_all_models(self) -> list[dict]:
        """Return all available models across providers."""
        models = []
        seen = set()
        for p in self.providers:
            if not p.available:
                continue
            for model_id in p.models:
                if model_id not in seen:
                    seen.add(model_id)
                    models.append({
                        "id": model_id,
                        "object": "model",
                        "owned_by": p.name,
                    })
        return models

    def select_provider(self, model: Optional[str] = None) -> Optional[Provider]:
        """Select the best available provider for a given model.

        If model is specified, picks the highest-priority provider that
        serves that model. Otherwise picks the highest-priority provider.
        """
        for p in self.providers:
            if not p.available:
                continue
            if model and model not in p.models:
                continue
            return p

        # If specific model not found, try any available provider
        if model:
            for p in self.providers:
                if p.available:
                    return p

        return None
