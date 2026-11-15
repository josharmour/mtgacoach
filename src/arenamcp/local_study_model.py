"""Explicit Decks-only inference using an already installed on-device Ollama model."""

import http.client
import json
import logging
import re
import socket
import threading
import time

_HOST = "127.0.0.1"
_PORT = 11434
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_DIGEST = re.compile(r"(?:sha256:)?[a-f0-9]{64}")


def _request(route, body=None, timeout=5):
    # A direct connection cannot use HTTP proxy environment variables, resolve
    # another host or follow redirects. Never call a pull, create or push route.
    conn = http.client.HTTPConnection(_HOST, _PORT, timeout=timeout)
    try:
        conn.request(
            "GET" if body is None else "POST",
            route,
            None if body is None else json.dumps(body),
            {"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        if response.status != 200:
            raise ValueError("The local model service declined the request")
        raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("The local model response exceeds the supported size")
        return json.loads(raw)
    finally:
        conn.close()


def installed_models():
    result = {}
    for row in _request("/api/tags").get("models", []):
        name = row.get("name")
        if (
            isinstance(name, str)
            and _NAME.fullmatch(name)
            and "cloud" not in name.lower()
            and type(row.get("size")) is int
            and row["size"] > 1_000_000
            and isinstance(row.get("digest"), str)
            and _DIGEST.fullmatch(row["digest"])
            and not row.get("remote_host")
            and not row.get("remote_model")
            and (row.get("details") or {}).get("format") in {"gguf", "safetensors"}
        ):
            result[name] = (row["size"], row["digest"])
    return {name: result[name][1] for name in sorted(result, key=lambda name: (result[name][0], name))}


class LocalStudyModel:
    def __init__(self, name):
        self.name = name
        self.digest = installed_models().get(name)
        if not self.digest:
            raise ValueError("Choose an installed local model with weights")
        meta = _request("/api/show", {"model": name})
        params = (meta.get("model_info") or {}).get("general.parameter_count")
        if (
            meta.get("remote_model")
            or meta.get("remote_host")
            or (meta.get("details") or {}).get("format") not in {"gguf", "safetensors"}
            or type(params) is not int
            or params <= 0
            or "completion" not in (meta.get("capabilities") or [])
        ):
            raise ValueError("The selected model has no verified local weights")
        levels = meta.get("thinking") or {}
        supported = levels.get("values", []) if isinstance(levels, dict) else []
        self.thinks = "thinking" in (meta.get("capabilities") or [])
        if self.thinks and "low" in supported:
            self.thinks = "low"
        self.last_metrics = {}

    def complete(self, system, prompt, *, allowed, response_format=None, max_tokens=4096, max_seconds=120):
        if not allowed() or self.digest != installed_models().get(self.name):
            raise ValueError("The deck, match or selected model changed")
        body = {
            "model": self.name,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "format": response_format if response_format is not None else "json",
            "stream": True,
            "options": {"num_predict": max_tokens, "temperature": 0.2},
        }
        if self.thinks:
            body["think"] = self.thinks
        conn = http.client.HTTPConnection(_HOST, _PORT, timeout=5)
        ended = threading.Event()
        started = time.monotonic()
        until = started + max_seconds
        self.last_metrics = {
            "ended": "unknown",
            "generated_tokens": None,
            "thinking_tokens": None,
            "cancelled": False,
        }

        def watch(connection):
            while not ended.wait(0.25):
                try:
                    permitted = allowed()
                    if permitted and time.monotonic() < until:
                        continue
                    self.last_metrics["cancelled"] = not permitted
                except Exception:
                    pass  # Unknown match state also cancels the study.
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return

        try:
            conn.connect()
            connection = conn.sock
            connection.settimeout(max_seconds)
            if not allowed():
                raise ValueError("Study cancelled before inference")
            watcher = threading.Thread(target=watch, args=(connection,), daemon=True)
            watcher.start()
            conn.request("POST", "/api/chat", json.dumps(body), {"Content-Type": "application/json"})
            response = conn.getresponse()
            if response.status != 200:
                raise ValueError("The local model could not start the study")
            parts, received = [], 0
            while allowed():
                line = response.readline(65_537)
                received += len(line)
                if not line or len(line) > 65_536 or received > 4_000_000:
                    raise ValueError("Incomplete local model response")
                chunk = json.loads(line)
                if chunk.get("error") or chunk.get("model") != self.name:
                    raise ValueError("The local model response could not be verified")
                message = chunk.get("message", {})
                if not isinstance(message, dict) or message.get("tool_calls") or message.get("tool_name"):
                    raise ValueError("Unexpected local model output channel")
                role, content = message.get("role"), message.get("content", "")
                if (
                    role not in (None, "", "assistant")
                    or not isinstance(content, str)
                    or (
                        role != "assistant"
                        and (content or message.get("thinking") or chunk.get("done") is not True)
                    )
                ):
                    raise ValueError("Unexpected local model output channel")
                parts.append(content)
                if chunk.get("done") is True:
                    reason = chunk.get("done_reason")
                    self.last_metrics.update(
                        {
                            "ended": reason if reason in ("stop", "length") else "unknown",
                            "generated_tokens": chunk.get("eval_count")
                            if type(chunk.get("eval_count")) is int
                            else None,
                            "thinking_tokens": chunk.get("thinking_eval_count")
                            if type(chunk.get("thinking_eval_count")) is int
                            else None,
                        }
                    )
                    if not allowed():
                        self.last_metrics["cancelled"] = True
                        raise ValueError("The deck or match changed before the study completed")
                    if reason == "length":
                        raise ValueError("The local card study reached its output token limit")
                    if reason != "stop":
                        raise ValueError("The local model did not confirm complete output")
                    if self.digest != installed_models().get(self.name):
                        raise ValueError("The selected local model weights changed during the study")
                    if not allowed() or time.monotonic() >= until:
                        raise ValueError("The local study was interrupted before verification")
                    return json.loads("".join(parts))
            self.last_metrics["cancelled"] = True
            raise ValueError("Card study paused when the deck or match changed")
        finally:
            ended.set()
            self.last_metrics["seconds"] = round(time.monotonic() - started, 2)
            logging.getLogger(__name__).info("Local card study completion: %s", self.last_metrics)
            conn.close()
