"""Coach engine with pluggable LLM backends for MTG game coaching.

This module provides the CoachEngine for getting strategic advice from LLMs,
with support for Claude Code CLI, Gemini CLI, and cli-api-proxy.
"""

import json
import logging
import os
import queue
import subprocess
import threading
import time
from collections import Counter
from typing import Any, Optional, Protocol

logger = logging.getLogger(__name__)


# LLM Backend Protocol and Implementations


class LLMBackend(Protocol):
    """Protocol for LLM backends that can provide coaching advice."""

    def complete(self, system_prompt: str, user_message: str) -> str:
        """Get a completion from the LLM."""
        ...

    def list_models(self) -> list[str]:
        """List available models (optional)."""
        return []


class ClaudeCodeBackend:
    """LLM backend using Claude Code CLI (subscription session, no API key).

    Uses a persistent subprocess with stream-json I/O for low latency.
    """

    def __init__(
        self,
        model: Optional[str] = None,
        command: Optional[str] = None,
        max_turns: Optional[int] = None,
        timeout_s: Optional[float] = None,
        add_dirs: Optional[list[str]] = None,
        tools: Optional[list[str]] = None,
        permission_mode: Optional[str] = None,
        progress_callback: Optional[Any] = None,
    ) -> None:
        self.model = model
        self.command = command or os.environ.get("CLAUDE_CODE_CMD", "claude")
        self.max_turns = int(os.environ.get("CLAUDE_CODE_MAX_TURNS", max_turns or 40))
        self.timeout_s = float(
            os.environ.get("CLAUDE_CODE_TIMEOUT_S", timeout_s or 12.0)
        )
        self.add_dirs = add_dirs or []
        self.tools = tools or []
        self.permission_mode = permission_mode or "dontAsk"
        self.progress_callback = progress_callback

        self._base_system_prompt = os.environ.get(
            "CLAUDE_CODE_SYSTEM_PROMPT",
            "You are an MTG coach. Follow the instructions in the user message. Do not use tools.",
        )

        self._proc: Optional[subprocess.Popen[str]] = None
        self._queue: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._lock = threading.Lock()
        self._init_event = threading.Event()
        self._turns = 0
        self._session_id: Optional[str] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None

    def _start_process(self) -> None:
        args = [
            self.command,
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--replay-user-messages",
            "--verbose",
            "--permission-mode",
            self.permission_mode,
            "--no-session-persistence",
        ]
        if self.tools:
            args += ["--tools", ",".join(self.tools)]
        else:
            args += ["--tools", ""]
        for d in self.add_dirs:
            args += ["--add-dir", d]
        if self.model:
            args += ["--model", self.model]
        if self._base_system_prompt:
            args += ["--system-prompt", self._base_system_prompt]

        try:
            self._proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            raise FileNotFoundError(
                f"Claude Code CLI not found at '{self.command}'. "
                "Install it or set CLAUDE_CODE_CMD to the correct path."
            )

        self._init_event.clear()
        self._queue = queue.Queue()

        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            args=(self._proc.stdout, False),
            daemon=True,
        )
        self._reader_thread.start()

        self._stderr_thread = threading.Thread(
            target=self._reader_loop,
            args=(self._proc.stderr, True),
            daemon=True,
        )
        self._stderr_thread.start()

        if not self._init_event.wait(timeout=5.0):
            logger.warning("Claude Code CLI did not emit init event in time")

    def _reader_loop(self, stream, is_stderr: bool) -> None:
        if stream is None:
            return
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                if is_stderr:
                    logger.debug(f"[CLAUDE-CLI][stderr] {line}")
                else:
                    logger.debug(f"[CLAUDE-CLI] Non-JSON line: {line}")
                continue

            if data.get("type") == "system" and data.get("subtype") == "init":
                self._session_id = data.get("session_id")
                self._init_event.set()

            self._queue.put(data)

    def _ensure_process(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            self._start_process()

    def _restart_process(self) -> None:
        self.close()
        self._turns = 0
        self._start_process()

    def _extract_assistant_text(self, message: dict[str, Any]) -> str:
        content = message.get("content", [])
        parts: list[str] = []
        if isinstance(content, list):
            for part in content:
                if part.get("type") == "text" and part.get("text"):
                    parts.append(part["text"])
        return "".join(parts)

    def complete(self, system_prompt: str, user_message: str) -> str:
        """Get completion via Claude Code CLI."""
        if not self._lock.acquire(timeout=3.0):
            logger.warning(
                "[CLAUDE-CLI] Lock busy (another call in progress), skipping"
            )
            return ""
        try:
            if self._turns >= self.max_turns:
                logger.info(
                    f"[CLAUDE-CLI] Max turns reached ({self.max_turns}); restarting session"
                )
                self._restart_process()

            self._ensure_process()

            # Embed dynamic system prompt in the user message to avoid restarts.
            combined = (
                "SYSTEM INSTRUCTIONS:\n"
                f"{system_prompt}\n\n"
                "USER MESSAGE:\n"
                f"{user_message}"
            )

            payload = {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": combined}],
                },
            }

            try:
                assert self._proc is not None and self._proc.stdin is not None
                self._proc.stdin.write(json.dumps(payload) + "\n")
                self._proc.stdin.flush()
            except Exception as e:
                logger.error(f"[CLAUDE-CLI] Failed to write to subprocess: {e}")
                self._restart_process()
                return "Error: Claude Code CLI write failed"

            assistant_text = ""
            result_text = ""
            deadline = time.time() + self.timeout_s

            # Emit initial progress
            if self.progress_callback:
                self.progress_callback("Thinking...")

            while time.time() < deadline:
                try:
                    data = self._queue.get(timeout=0.25)
                except queue.Empty:
                    continue

                msg_type = data.get("type")
                if msg_type == "assistant":
                    message = data.get("message", {})
                    assistant_text = (
                        self._extract_assistant_text(message) or assistant_text
                    )
                    # Report subtask progress from tool_use blocks
                    if self.progress_callback:
                        content = message.get("content", [])
                        if isinstance(content, list):
                            for part in content:
                                part_type = part.get("type", "")
                                if part_type == "tool_use":
                                    tool_name = part.get("name", "tool")
                                    self.progress_callback(f"Using {tool_name}...")
                                elif part_type == "text" and part.get("text"):
                                    # Trim to first 60 chars for status display
                                    snippet = part["text"][:60].replace("\n", " ")
                                    self.progress_callback(f"Responding: {snippet}...")
                elif msg_type == "result":
                    result_text = data.get("result") or result_text
                    if self.progress_callback:
                        self.progress_callback("")
                    break
                elif msg_type == "error":
                    if self.progress_callback:
                        self.progress_callback("")
                    return f"Error from Claude CLI: {data}"

            if self.progress_callback:
                self.progress_callback("")

            if not assistant_text:
                assistant_text = result_text

            self._turns += 1
            return assistant_text or "Error: No response from Claude Code"
        finally:
            self._lock.release()

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            self._proc.wait(timeout=2.0)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class GeminiCliBackend:
    """LLM backend using Gemini CLI (subscription session, no API key)."""

    def __init__(
        self,
        model: Optional[str] = None,
        command: Optional[str] = None,
        persistent: Optional[bool] = None,
        max_turns: Optional[int] = None,
        timeout_s: Optional[float] = None,
        progress_callback: Optional[Any] = None,
    ) -> None:
        self.model = model
        self.command = command or os.environ.get("GEMINI_CLI_CMD", "gemini")
        self.persistent = bool(
            int(os.environ.get("GEMINI_CLI_PERSISTENT", "1"))
            if persistent is None
            else persistent
        )
        self.max_turns = int(os.environ.get("GEMINI_CLI_MAX_TURNS", max_turns or 40))
        self.timeout_s = float(
            os.environ.get("GEMINI_CLI_TIMEOUT_S", timeout_s or 20.0)
        )
        self._proc: Optional[subprocess.Popen[str]] = None
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._lock = threading.Lock()
        self._reader_thread: Optional[threading.Thread] = None
        self._turns = 0
        self._persistent_failed = False
        self._initial_system_prompt: str = ""
        self.progress_callback = progress_callback

    def complete(self, system_prompt: str, user_message: str) -> str:
        """Get completion via Gemini CLI (non-interactive)."""
        if self.persistent and not self._persistent_failed:
            return self._complete_persistent(system_prompt, user_message)
        return self._complete_one_shot(system_prompt, user_message)

    def _build_args(self) -> list[str]:
        import shutil

        cmd = self.command
        if cmd and os.path.isabs(cmd):
            resolved = cmd
        else:
            resolved = shutil.which(cmd) or ""

        # Try common Windows shim names if direct resolution fails
        if not resolved and os.name == "nt":
            resolved = (
                shutil.which(f"{cmd}.ps1")
                or shutil.which(f"{cmd}.cmd")
                or shutil.which("gemini.ps1")
                or shutil.which("gemini.cmd")
                or ""
            )

        if resolved.lower().endswith(".ps1"):
            return [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                resolved,
            ]

        return [resolved or cmd]

    def _start_process(self, system_prompt: str) -> None:
        args = self._build_args() + [
            "--prompt-interactive",
            system_prompt,
            "--output-format",
            "text",
            "--raw-output",
            "--accept-raw-output-risk",
            "--approval-mode",
            "plan",
        ]
        if self.model:
            args += ["--model", self.model]

        try:
            # OPTIMIZATION: Use CREATE_NO_WINDOW to hide the console window on Windows
            # and ensure UTF-8 encoding for reliable IPC.
            creationflags = 0
            if os.name == "nt":
                creationflags = subprocess.CREATE_NO_WINDOW

            self._proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                creationflags=creationflags,
            )
        except FileNotFoundError:
            raise FileNotFoundError(
                "Gemini CLI not found. Set GEMINI_CLI_CMD to the full path of gemini.ps1 or gemini.cmd."
            )

        # Track the base system prompt so we don't re-send it every call
        self._initial_system_prompt = system_prompt

        self._queue = queue.Queue()
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def _reader_loop(self) -> None:
        if not self._proc or self._proc.stdout is None:
            return
        for line in self._proc.stdout:
            if line:
                self._queue.put(line)

    def _ensure_process(self, system_prompt: str) -> None:
        if self._proc is None or self._proc.poll() is not None:
            self._start_process(system_prompt)

    def _restart_process(self, system_prompt: str) -> None:
        self.close()
        self._turns = 0
        self._start_process(system_prompt)

    def _complete_persistent(self, system_prompt: str, user_message: str) -> str:
        import uuid

        fallback_to_one_shot = False

        if not self._lock.acquire(timeout=3.0):
            logger.warning(
                "[GEMINI-CLI] Lock busy (another call in progress), skipping"
            )
            return ""
        try:
            if self._turns >= self.max_turns:
                logger.info(
                    f"[GEMINI-CLI] Max turns reached ({self.max_turns}); restarting session"
                )
                self._restart_process(system_prompt)

            try:
                self._ensure_process(system_prompt)
            except FileNotFoundError as e:
                return str(e)

            marker = f"<<END-{uuid.uuid4()}>>"

            # Only send dynamic additions, not the full system prompt —
            # the base prompt was already set via --prompt-interactive at startup.
            # This cuts ~3000 chars (~750 tokens) from every call.
            dynamic_context = ""
            base = getattr(self, "_initial_system_prompt", "")
            if system_prompt and system_prompt != base:
                if base and system_prompt.startswith(base):
                    # Extract only the new suffix (deck strategy, rules, etc.)
                    dynamic_context = system_prompt[len(base) :]
                else:
                    # System prompt changed completely — include it all
                    dynamic_context = f"SYSTEM INSTRUCTIONS:\n{system_prompt}\n\n"

            combined_parts = []
            if dynamic_context:
                combined_parts.append(f"ADDITIONAL CONTEXT:{dynamic_context}")
            combined_parts.append(user_message)
            combined_parts.append(
                f"\nEnd your response with this exact marker on its own line:\n{marker}"
            )
            combined = "\n\n".join(combined_parts)

            try:
                assert self._proc is not None and self._proc.stdin is not None
                self._proc.stdin.write(combined + "\n")
                self._proc.stdin.flush()
            except Exception as e:
                logger.warning(
                    f"[GEMINI-CLI] Persistent write failed, falling back: {e}"
                )
                self._persistent_failed = True
                self.close()
                fallback_to_one_shot = True

            if not fallback_to_one_shot:
                deadline = time.time() + self.timeout_s
                buffer: list[str] = []

                # Emit initial progress
                if self.progress_callback:
                    self.progress_callback("Thinking...")

                while time.time() < deadline:
                    try:
                        chunk = self._queue.get(timeout=0.25)
                    except queue.Empty:
                        # Update elapsed time in progress
                        if self.progress_callback:
                            elapsed = time.time() - (deadline - self.timeout_s)
                            self.progress_callback(f"Thinking... ({elapsed:.0f}s)")
                        continue
                    buffer.append(chunk)
                    if self.progress_callback and len(buffer) == 1:
                        self.progress_callback("Generating response...")
                    if marker in chunk or marker in "".join(buffer[-5:]):
                        break

                if self.progress_callback:
                    self.progress_callback("")

                text = "".join(buffer)
                if marker in text:
                    text = text.split(marker, 1)[0]
                text = text.strip()
                if not text:
                    # Fallback to one-shot if persistent didn't yield output
                    self._persistent_failed = True
                    fallback_to_one_shot = True
                else:
                    self._turns += 1
                    return text
        finally:
            self._lock.release()

        # One-shot fallback runs OUTSIDE the lock so it doesn't block
        # other callers (e.g. advice calls blocked by deck analysis)
        if fallback_to_one_shot:
            return self._complete_one_shot(system_prompt, user_message)

        return ""

    def _complete_one_shot(self, system_prompt: str, user_message: str) -> str:
        combined = (
            f"SYSTEM INSTRUCTIONS:\n{system_prompt}\n\nUSER MESSAGE:\n{user_message}"
        )
        args = self._build_args() + [
            "-p",
            combined,
            "--output-format",
            "text",
            "--raw-output",
            "--accept-raw-output-risk",
            "--approval-mode",
            "plan",
        ]
        if self.model:
            args += ["--model", self.model]

        if self.progress_callback:
            self.progress_callback("Thinking (one-shot)...")

        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
        except FileNotFoundError:
            if self.progress_callback:
                self.progress_callback("")
            return (
                "Error: Gemini CLI not found. "
                "Set GEMINI_CLI_CMD to the full path of gemini.ps1 or gemini.cmd."
            )
        except subprocess.TimeoutExpired:
            if self.progress_callback:
                self.progress_callback("")
            return "Error: Gemini CLI request timed out"
        except Exception as e:
            if self.progress_callback:
                self.progress_callback("")
            return f"Error running Gemini CLI: {e}"

        if self.progress_callback:
            self.progress_callback("")

        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            return f"Error: Gemini CLI failed: {err}"

        return (result.stdout or "").strip()

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            self._proc.wait(timeout=2.0)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None


class CodexCliBackend:
    """LLM backend using Codex CLI (one-shot subprocess, similar to Gemini one-shot)."""

    def __init__(
        self,
        model: Optional[str] = None,
        command: Optional[str] = None,
        timeout_s: Optional[float] = None,
        progress_callback: Optional[Any] = None,
    ) -> None:
        self.model = model
        self.command = command or os.environ.get("CODEX_CLI_CMD", "codex")
        self.timeout_s = float(
            os.environ.get("CODEX_CLI_TIMEOUT_S", timeout_s or 30.0)
        )
        self.progress_callback = progress_callback

    def _build_args(self) -> list[str]:
        import shutil

        cmd = self.command
        if cmd and os.path.isabs(cmd):
            resolved = cmd
        else:
            resolved = shutil.which(cmd) or ""

        if not resolved and os.name == "nt":
            resolved = (
                shutil.which(f"{cmd}.ps1")
                or shutil.which(f"{cmd}.cmd")
                or ""
            )

        if resolved and resolved.lower().endswith(".ps1"):
            return [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                resolved,
            ]

        return [resolved or cmd]

    def complete(self, system_prompt: str, user_message: str) -> str:
        """Get completion via Codex CLI (one-shot)."""
        combined = (
            f"SYSTEM INSTRUCTIONS:\n{system_prompt}\n\nUSER MESSAGE:\n{user_message}"
        )
        args = self._build_args() + ["-q", combined]
        if self.model:
            args += ["--model", self.model]

        if self.progress_callback:
            self.progress_callback("Thinking (codex)...")

        try:
            creationflags = 0
            if os.name == "nt":
                creationflags = subprocess.CREATE_NO_WINDOW

            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                creationflags=creationflags,
            )
        except FileNotFoundError:
            if self.progress_callback:
                self.progress_callback("")
            return (
                "Error: Codex CLI not found. "
                "Install it or set CODEX_CLI_CMD to the full path."
            )
        except subprocess.TimeoutExpired:
            if self.progress_callback:
                self.progress_callback("")
            return "Error: Codex CLI request timed out"
        except Exception as e:
            if self.progress_callback:
                self.progress_callback("")
            return f"Error running Codex CLI: {e}"

        if self.progress_callback:
            self.progress_callback("")

        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            return f"Error: Codex CLI failed: {err}"

        return (result.stdout or "").strip()

    def list_models(self) -> list[str]:
        return []

    def close(self) -> None:
        pass


class ProxyBackend:
    """LLM backend using CLI Proxy API (OpenAI-compatible endpoint).

    Routes requests through a local cli-proxy-api server that load-balances
    across multiple OAuth providers (Antigravity, Claude, Codex, etc.).
    """

    def __init__(
        self,
        model: str = "claude-sonnet-4-5-20250929",
        enable_thinking: bool = False,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        """Initialize Proxy backend.

        Args:
            model: Model name as exposed by the proxy (default: claude-sonnet-4-5-20250929)
            enable_thinking: If True, enable extended thinking for models that support it.
                             Used by background win-plan workers for deeper analysis.
            base_url: Override the proxy endpoint URL. Falls back to PROXY_BASE_URL
                     env var, then settings, then http://127.0.0.1:8080/v1.
            api_key: Override the API key. Falls back to PROXY_API_KEY env var,
                    then settings, then default placeholder.
        """
        self.model = model or "claude-sonnet-4-5-20250929"
        self.enable_thinking = enable_thinking
        self._base_url = base_url
        self._api_key = api_key
        self._client = None

    def _get_client(self):
        """Lazy init of OpenAI client pointed at proxy."""
        if self._client is None:
            try:
                from openai import OpenAI

                # Resolve URL: explicit param > env var > settings > default
                if self._base_url:
                    url = self._base_url
                elif os.environ.get("PROXY_BASE_URL"):
                    url = os.environ["PROXY_BASE_URL"]
                else:
                    try:
                        from arenamcp.settings import get_settings
                        url = get_settings().get("proxy_url") or "http://127.0.0.1:8080/v1"
                    except Exception:
                        url = "http://127.0.0.1:8080/v1"

                # Resolve key: explicit param > env var > settings > default
                if self._api_key:
                    key = self._api_key
                elif os.environ.get("PROXY_API_KEY"):
                    key = os.environ["PROXY_API_KEY"]
                else:
                    try:
                        from arenamcp.settings import get_settings
                        key = get_settings().get("proxy_api_key") or "your-api-key-1"
                    except Exception:
                        key = "your-api-key-1"

                self._client = OpenAI(base_url=url, api_key=key)
            except ImportError:
                raise ImportError("openai package required: pip install openai")
        return self._client

    def complete(self, system_prompt: str, user_message: str, max_tokens: int = 400) -> str:
        """Get completion from proxy."""
        import time

        try:
            client = self._get_client()

            # Build request params optimized for low-latency real-time advice
            params = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                "max_completion_tokens": max_tokens,
                "temperature": 0.3,
            }

            # Configure thinking/reasoning based on enable_thinking flag.
            # NOTE: Must use extra_body for non-standard params (OpenAI SDK rejects unknown kwargs)
            model_lower = self.model.lower()
            extra = {}
            if self.enable_thinking:
                # Enable extended thinking for deeper analysis (win plans)
                if "claude" in model_lower:
                    extra["thinking"] = {"type": "enabled", "budget_tokens": 8000}
                    params["max_completion_tokens"] = max_tokens + 8000
                elif "gemini" in model_lower:
                    extra["thinking_config"] = {"thinking_budget": 4096}
                # Other models: no thinking config needed
            else:
                # Disable thinking for low-latency real-time advice
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
            usage = getattr(response, 'usage', None)
            tokens_info = ""
            if usage:
                tokens_info = f", in={usage.prompt_tokens}, out={usage.completion_tokens}"
            logger.info(
                f"[PROXY] API: {request_time:.0f}ms, model: {self.model}{tokens_info}"
            )
            return content
        except Exception as e:
            logger.error(f"Proxy API error: {e}")
            return f"Error getting advice from proxy: {e}"

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
            logger.error(f"Proxy vision API error: {e}")
            return f"Error getting vision analysis from proxy: {e}"

    def list_models(self) -> list[str]:
        """List available models from the proxy."""
        try:
            client = self._get_client()
            models = client.models.list()
            return [m.id for m in models.data]
        except Exception as e:
            logger.error(f"Failed to list proxy models: {e}")
            return []


def fetch_proxy_models() -> list[tuple[str, str]]:
    """Fetch available models from the CLI Proxy API at startup.

    Returns a list of (display_name, "proxy/model-id") tuples suitable for
    populating a UI dropdown. Falls back to a static list if the proxy is
    unreachable.
    """
    import urllib.request

    # Resolve URL/key: env var > settings > default
    base_url = os.environ.get("PROXY_BASE_URL", "")
    api_key = os.environ.get("PROXY_API_KEY", "")
    if not base_url:
        try:
            from arenamcp.settings import get_settings
            s = get_settings()
            base_url = s.get("proxy_url") or "http://127.0.0.1:8080/v1"
            if not api_key:
                api_key = s.get("proxy_api_key") or "your-api-key-1"
        except Exception:
            base_url = "http://127.0.0.1:8080/v1"
    if not api_key:
        api_key = "your-api-key-1"

    # Display name mapping for known model prefixes
    OWNER_LABELS = {
        "anthropic": "Claude",
        "google": "Gemini",
        "openai": "Codex",
        "antigravity": "AG",
    }

    results = []

    # Try proxy
    try:
        req = urllib.request.Request(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())

        proxy_results = []
        for m in data.get("data", []):
            model_id = m["id"]
            owner = m.get("owned_by", "unknown")
            label = OWNER_LABELS.get(owner, owner.title())
            display = f"Proxy: {model_id} ({label})"
            proxy_results.append((display, f"proxy/{model_id}"))

        # Sort: Claude first, then Google, then others
        owner_order = {"anthropic": 0, "google": 1, "openai": 2, "antigravity": 3}
        model_owners = {m["id"]: m.get("owned_by", "zzz") for m in data.get("data", [])}
        proxy_results.sort(key=lambda x: (
            owner_order.get(model_owners.get(x[1].split("/", 1)[1], "zzz"), 9),
            x[1],
        ))

        logger.info(f"Fetched {len(proxy_results)} models from proxy")
        results.extend(proxy_results)
    except Exception as e:
        logger.warning(f"Could not fetch proxy models: {e}")

    # Try Ollama (always, not just as fallback — user may have both)
    try:
        from arenamcp.settings import get_settings
        ollama_url = get_settings().get("ollama_url") or "http://localhost:11434/v1"
        # Skip if Ollama URL is the same as proxy URL (avoid duplicates)
        if ollama_url.rstrip("/") != base_url.rstrip("/"):
            req = urllib.request.Request(f"{ollama_url}/models")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
            for m in data.get("data", []):
                model_id = m["id"]
                display = f"Ollama: {model_id}"
                results.append((display, f"ollama/{model_id}"))
            if results:
                logger.info(f"Fetched Ollama models, total now: {len(results)}")
    except Exception:
        pass

    # Try generic API endpoint (api_url/api_key from settings)
    try:
        from arenamcp.settings import get_settings
        s = get_settings()
        api_url = s.get("api_url") or ""
        api_key_val = s.get("api_key") or ""
        if api_url and api_url.rstrip("/") != base_url.rstrip("/"):
            headers = {}
            if api_key_val:
                headers["Authorization"] = f"Bearer {api_key_val}"
            req = urllib.request.Request(f"{api_url.rstrip('/')}/models", headers=headers)
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
            for m in data.get("data", []):
                model_id = m["id"]
                display = f"API: {model_id}"
                results.append((display, f"api/{model_id}"))
            logger.info(f"Fetched API endpoint models, total now: {len(results)}")
    except Exception:
        pass

    if results:
        return results

    # Static fallback only if nothing was reachable
    return [
        ("Proxy: claude-sonnet-4-5-20250929 (Claude)", "proxy/claude-sonnet-4-5-20250929"),
        ("Proxy: gemini-2.5-pro (Gemini)", "proxy/gemini-2.5-pro"),
    ]


THINKING_MODEL_PREFERENCE = [
    "claude-opus-4-6",
    "claude-sonnet-4-5-20250929",
    "gemini-2.5-pro",
    "gpt-5.3-codex",
]


def pick_thinking_model() -> Optional[str]:
    """Auto-select the best available thinking model from the proxy.

    Queries the proxy /v1/models endpoint and returns the first match
    from THINKING_MODEL_PREFERENCE. Returns None if proxy is unavailable
    or no preferred model is found.
    """
    import urllib.request

    base_url = os.environ.get("PROXY_BASE_URL", "http://127.0.0.1:8080/v1")
    api_key = os.environ.get("PROXY_API_KEY", "your-api-key-1")

    try:
        req = urllib.request.Request(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())

        available_ids = {m["id"] for m in data.get("data", [])}
        for model_id in THINKING_MODEL_PREFERENCE:
            if model_id in available_ids:
                logger.info(f"Thinking model selected: {model_id}")
                return model_id

        logger.info(f"No preferred thinking model found among {len(available_ids)} proxy models")
        return None
    except Exception as e:
        logger.warning(f"Could not pick thinking model (proxy unavailable): {e}")
        return None


def create_backend(
    backend_type: str,
    model: Optional[str] = None,
    progress_callback: Optional[Any] = None,
) -> LLMBackend:
    """Factory function to create LLM backends by name.

    Args:
        backend_type: One of "proxy", "claude-code", "gemini-cli"
        model: Optional model override (uses backend default if not specified)
        progress_callback: Optional callback(status: str) for real-time subtask updates

    Returns:
        Configured LLMBackend instance

    Raises:
        ValueError: If backend_type is not recognized
    """
    backend_type = backend_type.lower()

    if backend_type in (
        "claude",
        "claude-code",
        "claude_code",
        "claude-cli",
        "claude_cli",
    ):
        return ClaudeCodeBackend(model=model, progress_callback=progress_callback)
    elif backend_type in ("gemini", "gemini-cli", "gemini_cli"):
        return GeminiCliBackend(model=model, progress_callback=progress_callback)
    elif backend_type == "proxy":
        return ProxyBackend(model=model or "claude-sonnet-4-5-20250929")
    elif backend_type in ("codex", "codex-cli", "codex_cli"):
        return CodexCliBackend(model=model, progress_callback=progress_callback)
    elif backend_type == "api":
        # Generic OpenAI-compatible API endpoint
        try:
            from arenamcp.settings import get_settings
            s = get_settings()
            url = s.get("api_url") or "https://api.openai.com/v1"
            key = s.get("api_key") or ""
        except Exception:
            url = "https://api.openai.com/v1"
            key = ""
        return ProxyBackend(model=model or "gpt-4o", base_url=url, api_key=key)
    elif backend_type == "ollama":
        # Ollama exposes an OpenAI-compatible API at localhost:11434/v1
        try:
            from arenamcp.settings import get_settings
            ollama_url = get_settings().get("ollama_url") or "http://localhost:11434/v1"
        except Exception:
            ollama_url = "http://localhost:11434/v1"
        return ProxyBackend(
            model=model or "llama3.2",
            base_url=ollama_url,
            api_key="ollama",  # Ollama ignores the key but OpenAI client requires one
        )
    else:
        raise ValueError(
            f"Unknown backend type: {backend_type}. "
            "Use 'proxy', 'claude-code', 'gemini-cli', 'codex-cli', 'api', or 'ollama'"
        )


# Default MTG coach system prompt
DEFAULT_SYSTEM_PROMPT = """You are an expert MTG coach providing real-time advice during Arena games.

Keep responses concise (2-3 sentences max) since they'll be spoken aloud.
Focus ONLY on the final strategic recommendation.
Do NOT show your thinking process, "reasoning", or "corrections".
Do NOT use internal monologue tags like [plan] or [thought].
Do NOT second-guess yourself in the text (e.g., "Wait, I need to check...").
Be authoritative and decisive. Start your response immediately with the command.

CRITICAL GAME RULES:
- The "Legal:" line lists ALL valid actions. ONLY suggest actions listed there.
- NEVER suggest actions not in the Legal: line. If you want to cast a spell, it MUST appear as "Cast [card name]" in Legal:.
- Do NOT hallucinate actions like "flash in" or "hold up" unless they are explicitly legal actions.
- Creatures tagged [SS] have SUMMONING SICKNESS — they CANNOT attack or use tap abilities this turn.
- Do NOT suggest attacking with [SS] creatures. Check the "Declare Attackers:" list for legal attackers.
- DEFAULT: You can only play ONE LAND per turn unless a card grants additional land drops.
- Check the LAND DROP status to see if a land can still be played this turn.
- LAND DROP PRIORITY: If the LAND status shows 'AVAILABLE' and you have lands in hand, ALWAYS suggest playing a land FIRST before any spell. Land drops are free and should not be skipped. Say 'Play [land name]' as your advice when a land drop is available, UNLESS you specifically need to cast a spell first for strategic reasons (e.g., you need to tap specific lands before playing a new one).
- THEN LINE: If a "THEN:" line appears after Legal, it shows what spells become castable after playing each land. ALWAYS give the full play sequence: "Play [land], then cast [spell]". Choose the land that enables the best follow-up spell.
- Cards marked [INSTANT] or [I] can be cast anytime you have priority
- Cards marked [SORCERY SPEED] or [S] can ONLY be cast during YOUR Main phase with empty stack
- During opponent's turn or combat: ONLY suggest instants/flash cards or activated abilities
- If it's not your Main phase, do NOT suggest casting creatures or sorceries (unless they have flash)

CRITICAL MANA RULES:
- Cards tagged [OK] or [CAN CAST] are castable RIGHT NOW with available mana - no additional mana needed!
- Cards tagged [NEED:{G}] need GREEN mana specifically — adding non-green sources won't help!
- Cards tagged [NEED:{R}{R}] need TWO RED mana — check which lands produce that color.
- Cards tagged [NEED:3] need 3 more TOTAL mana from any source.
- Cards tagged [NEED X] CANNOT be cast - do NOT suggest or mention them! Focus only on playable options.
- Do NOT perform your own mana calculations - trust the tags completely.
- The "Mana: X" line shows ONLY mana from UNTAPPED LANDS ON THE BATTLEFIELD. Lands in hand are NOT mana.
- NEVER count lands in hand as available mana. A Plains in hand produces 0 mana until played.
- If a card shows [OK], you already have enough mana. Don't suggest paying extra life/resources for more mana.
- RESOURCE EFFICIENCY: Don't waste life or mana. If you can cast a spell with current mana, don't pay extra.
- The "sources:" display shows what mana EACH source can produce (e.g., "{U/G}" means one source producing U OR G, not both).
- If ALL cards show [NEED X], say "pass priority" - you cannot cast anything.

CRITICAL MATH RULES:
- When suggesting removal, check the creature's TOUGHNESS (second number, e.g., 4/5 has 5 toughness).
- -2/-2 or 2 damage ONLY kills toughness 2 or less (unless damaged).
- Do NOT suggest removal that won't kill the target unless it enables a profitable attack.
- Cards tagged [NO TARGETS] have NO VALID TARGETS on the opponent's board right now. Do NOT cast them — it wastes the card for no effect. Even if the card appears in the Legal: line, casting it without targets is a mistake.

CRITICAL BLOCKING RULES:
- Creatures tagged [FLYING] can ONLY be blocked by creatures with [FLYING] or [REACH].
- Do NOT suggest blocking a [FLYING] creature with a ground creature (no [FLYING]/[REACH]).
- If enemy attackers have [FLYING] and you have no flyers/reach, you CANNOT block them.
- HOWEVER: A creature WITH [FLYING] CAN block ground creatures! Flying only restricts what blocks THEM, not what they block. A flyer is a valid blocker for any attacker.
- DEATHTOUCH [DTH]: A creature with deathtouch KILLS any creature that blocks it, regardless of toughness. Do NOT block a deathtouch creature with a valuable creature just to prevent 1-2 damage — you lose the blocker! Only block deathtouch if the blocker is expendable or you MUST block to survive.

CRITICAL STRATEGY RULES:
- LETHAL CHECK: Before anything else, count your total attack power vs opponent life and blockers.
  If you can deal lethal, go aggressive — remove a blocker or just attack. Don't play defensively!
- ONLY claim "lethal" if the combat summary line shows "Atk: ... vs LETHAL".
- TRADE CHECK: Read the "If X blocks Y:" lines below the Atk: summary. Lines marked "BAD" mean the attacker dies for free or bounces off. Do NOT attack into a BAD trade unless it enables lethal or a critical strategy. If every possible block is BAD, don't attack with that creature.
- ATTACK DEFAULT: When declaring attackers, attack with ALL eligible creatures (listed after "can attack:" in the Atk: line) unless you have a concrete reason to hold one back (e.g., BAD trade, need it to block a lethal crackback). Do NOT suggest attacking with only one creature when multiple are available without explaining why the others should stay back.
- CRACKBACK CHECK: Before attacking, count opponent's total power on board vs YOUR life total.
  If opponent can kill you on their next attack and you need creatures to block, do NOT attack with them.
  Holding back blockers to survive is more important than dealing a few damage.
  The "Crackback:" line already accounts for your blockers — trust its damage-through number.
- BLOCKING MATH: The "Best blocks → X dmg" line shows MINIMUM damage after optimal blocking. Trust this number, not the raw attacker power.
  Use the "Best blocks" life total for survival math, not the "No blocks" total.
  Do NOT re-derive blocking math yourself — the computed numbers already account for flying, trample, and blocker assignment.
- IMPENDING: Cards flagged [IMPENDING] are enchantments with time counters — they are NOT creatures yet and cannot attack, block, or be counted as combat threats. Ignore them in damage/lethal math until the counters are gone.
- Bounce/removal spells can target OPPONENT creatures too. Bouncing a blocker for lethal > saving your creature.
- When opponent has a removal spell on the stack, weigh "save my creature" vs "ignore it and go for the kill."
- Creatures have power/toughness (e.g. 5/5). Don't call creatures "planeswalkers."
- ORACLE TEXT: Only reference card abilities that are explicitly shown in the game state. Do NOT guess or infer oracle text from memory — if the text isn't shown, say so.

Analyze: phase (critical for timing!), board state, life totals, cards in hand, mana available.
Output directly as the coach. No preamble, no meta-commentary.
Do NOT mention cards you can't cast yet due to mana — focus only on playable options. The player can see their hand."""

CONCISE_SYSTEM_PROMPT = """You are an expert MTG coach giving real-time spoken advice.
Give ONE action for the CURRENT phase only. You will be re-consulted as the turn progresses.

PHASE GUIDE:
- Main phase: Suggest ONE play (land OR spell). You'll advise again after it resolves.
- Combat/DeclareAttack: Say who to attack with (or "don't attack").
- Combat/DeclareBlock: Say how to block (or "don't block, take the damage").
- Opponent's turn: React to what's happening (instants/abilities only).
- Stack: Say whether to respond or let it resolve.

After your ONE action, you may add a brief reason or hint at the next step.

Examples:
"Play Mountain. Sets up Geological Appraiser next turn."
"Cast Etali's Favor on Laelia — triggers discover for the cascade chain."
"Attack with Laelia, the Blade Reforged. She exiles and grows."
"Don't block. Take the 3 damage, you're at 20."
"Let it resolve. Nothing worth countering."
"Pass priority."

STRATEGY:
- LETHAL CHECK: Before anything else, count your total attack power vs opponent life and blockers.
  If you can deal lethal, go aggressive — remove a blocker or just attack. Don't play defensively!
- ONLY claim "lethal" if the combat summary line shows "Atk: ... vs LETHAL".
- TRADE CHECK: Read "If X blocks Y:" lines. "BAD" = attacker dies for free. Don't attack into BAD trades unless it enables lethal.
- ATTACK DEFAULT: Attack with ALL eligible creatures (listed after "can attack:" in the Atk: line) unless the trade is BAD or you need to hold back a blocker to survive crackback. Never say a creature is your "only" attacker without checking the full list.
- CRACKBACK CHECK: Before attacking, count opponent's total power vs YOUR life. If they can kill you next turn and you need blockers to survive, do NOT attack with those creatures. The "Crackback:" line already accounts for your blockers — trust its damage-through number.
- BLOCKING MATH: The "Best blocks → X dmg" line shows MINIMUM damage after optimal blocking. Use this number for survival math, not the "No blocks" total. Do NOT re-derive blocking math yourself.
- IMPENDING: Cards flagged [IMPENDING] are NOT creatures yet — ignore them in combat/lethal math.
- Bounce/removal spells can target OPPONENT creatures too. Bouncing a blocker for lethal > saving your creature.
- When opponent has a removal spell on the stack, weigh "save my creature" vs "ignore it and go for the kill."
- ORACLE TEXT: Only reference abilities explicitly shown. Do NOT guess card text from memory.

RULES:
- The "Legal:" line lists ALL valid actions. ONLY suggest actions listed there. No exceptions!
- NEVER suggest actions not in Legal:. If you want to "flash in" a creature, it MUST show "Cast [creature]" in Legal:.
- Creatures tagged [SS] have SUMMONING SICKNESS — they CANNOT attack. Check "Declare Attackers:" for legal attackers.
- Cards tagged [OK] are castable NOW with current mana - no additional mana needed! Don't waste life for more mana.
- Cards tagged [NEED X] CANNOT be cast - do NOT suggest or mention them! Focus only on playable options.
- RESOURCE EFFICIENCY: If a card shows [OK], you already have enough. Don't pay extra life/mana unnecessarily.
- LAND DROP PRIORITY: If LAND status shows 'AVAILABLE' and you have lands in hand, suggest playing a land FIRST.
- THEN LINE: If "THEN:" appears after Legal, give the full sequence: "Play [land], then cast [spell]". Pick the land enabling the best follow-up.
- Use exact FULL card names from the game state. Never abbreviate.
- Only suggest lands shown in HAND. If no land in hand, don't suggest playing one.
- Say "pass priority" not just "pass" to avoid sounding like a card name.
- Creatures have power/toughness (e.g. 5/5). Don't call creatures "planeswalkers."
- [FLYING] attackers can only be blocked by [FLYING] or [REACH]. But flyers CAN block ground creatures — flying restricts what blocks them, not what they block.
- This is spoken aloud — keep it natural and under 30 words.
"""

# PHASE 2: Decision-specific prompt guidance
DECISION_PROMPTS = {
    "scry": """
SCRY DECISION: Decide whether to keep the card on top or put it on bottom.
- KEEP if: It's a land and you need mana, OR it's a threat you can cast soon
- BOTTOM if: It's redundant/dead right now, or you need to dig for answers
Evaluate based on: current mana, hand quality, board state urgency.
Answer: "Keep" or "Bottom" with brief reason (1 sentence).
""",
    "surveil": """
SURVEIL DECISION: Decide whether to keep cards on top or put in graveyard.
- KEEP if: You want to draw them next (lands if ramping, threats if you have mana)
- GRAVEYARD if: Enables graveyard synergies OR you want to dig deeper
Answer: "Keep [card names]" or "Graveyard [card names]" with brief reason.
""",
    "discard": """
DISCARD DECISION: Choose which card(s) to discard.
Priority (discard FIRST):
1. Excess lands if you have 4+ in hand
2. Highest CMC card you can't cast this turn or next
3. Redundant copies of cards already in play
4. KEEP: Removal, counters, win conditions
Answer: "Discard [card name]" with brief reason (1 sentence).
""",
    "target_selection": """
TARGET SELECTION: Choose the best target for this spell/ability.
Evaluate each potential target:
- Which target solves the biggest immediate threat?
- Which target advances your win condition?
- Consider opponent's likely responses (do they have protection?)
Answer: "Target [card name]" with brief tactical reason.
""",
    "modal_choice": """
MODAL SPELL: Choose which mode to use.
Compare each mode's impact:
- Which mode answers the most pressing threat?
- Which mode creates the best advantage?
- Consider mana efficiency and follow-up plays
Answer: "Choose mode [X]" with brief reason (1 sentence).
""",
}

WIN_PLAN_PROMPT = """You are a Magic: The Gathering strategic planner. Given the current board state, hand, mana, and library summary, create a concrete turn-by-turn plan to win in exactly {n} turns.

For each turn, specify:
- Which land to play (if any)
- Which spells to cast and in what order
- Combat attacks and expected blocks
- Key interactions or responses to hold up

Consider:
- Mana growth trajectory (current lands + land drops)
- High-impact draws from the library summary
- How the opponent might respond (removal, blockers, counterspells)
- Combo potential or synergies between cards
- Whether to race or control the board

CRITICAL: Only reference card abilities that are explicitly shown in the provided game state or library summary.
Do NOT invent or guess what a card does — if no oracle text is provided for a card, refer to it only by name and mana cost.

Be specific with card names and mana costs. If the plan requires drawing specific cards, note the probability.
Keep the plan speakable in about 60 seconds — be concise but precise.

Start your response with exactly one of:
  VIABLE: YES — if this plan can realistically win in {n} turns using mostly cards already in hand/on board
  VIABLE: NO — if it requires specific draws, opponent misplays, or is highly speculative

Then provide the plan."""

DECK_ANALYSIS_PROMPT = """Analyze this Magic: The Gathering deck list. Provide a brief strategic summary:
1. ARCHETYPE: One-line description (e.g. "Mono-Red Aggro", "Dimir Control")
2. WIN CONDITION: How does this deck win?
3. KEY CARDS: 3-5 most important cards and why
4. PLAY PATTERN: Ideal curve and sequencing (e.g. "Play threats T1-T3, hold up removal T4+")
5. WATCH OUT: Key weaknesses or cards to play around

Keep the entire analysis under 300 characters. Be specific to THIS deck, not generic advice."""


# Words that tend to be overused by LLMs in coaching contexts
OVERUSE_CANDIDATES = {
    "consider",
    "considering",
    "important",
    "crucial",
    "critical",
    "definitely",
    "absolutely",
    "certainly",
    "essentially",
    "basically",
    "potentially",
    "priority",
    "prioritize",
    "focus",
    "key",
}

# Threshold for blacklisting (uses in window)
OVERUSE_THRESHOLD = 3
OVERUSE_WINDOW_SECONDS = 120


class WordUsageTracker:
    """Tracks word usage over time to detect overused words."""

    def __init__(
        self,
        threshold: int = OVERUSE_THRESHOLD,
        window_seconds: float = OVERUSE_WINDOW_SECONDS,
    ):
        self._threshold = threshold
        self._window = window_seconds
        self._usage: list[tuple[float, str]] = []  # (timestamp, word)

    def record(self, text: str, exclude_words: Optional[set[str]] = None) -> None:
        """Record words from a response.

        Args:
            text: The response text to analyze
            exclude_words: Set of words to ignore (e.g., card names)
        """
        import time
        import re

        now = time.time()

        exclude = exclude_words or set()

        # Extract words, lowercase
        words = re.findall(r"\b[a-z]+\b", text.lower())

        # Only track candidate words that aren't excluded
        for word in words:
            if word in OVERUSE_CANDIDATES and word not in exclude:
                self._usage.append((now, word))

        # Prune old entries
        cutoff = now - self._window
        self._usage = [(t, w) for t, w in self._usage if t > cutoff]

    def get_blacklisted(self, exclude_words: Optional[set[str]] = None) -> list[str]:
        """Get words that have been overused in the current window.

        Args:
            exclude_words: Set of words to never blacklist (e.g., card names)
        """
        import time
        from collections import Counter

        exclude = exclude_words or set()
        now = time.time()
        cutoff = now - self._window

        # Count words in window
        recent_words = [w for t, w in self._usage if t > cutoff]
        counts = Counter(recent_words)

        # Return words over threshold, excluding protected words
        return [
            word
            for word, count in counts.items()
            if count >= self._threshold and word not in exclude
        ]


class CoachEngine:
    """Engine for getting MTG coaching advice from an LLM backend."""

    def __init__(
        self, backend: Optional[LLMBackend] = None, system_prompt: Optional[str] = None
    ):
        """Initialize the coach engine.

        Args:
            backend: LLM backend to use (default: ProxyBackend)
            system_prompt: Custom system prompt (default: MTG coach persona)
        """
        self._backend = backend if backend is not None else ProxyBackend()
        self._system_prompt = (
            system_prompt if system_prompt is not None else DEFAULT_SYSTEM_PROMPT
        )
        self._word_tracker = WordUsageTracker()
        self._deck_strategy: Optional[str] = None
        self._deck_strategy_pending = False
        self._rules_db: Optional["RulesDB"] = None

    def clear_deck_strategy(self) -> None:
        """Reset deck strategy for a new match."""
        self._deck_strategy = None
        self._deck_strategy_pending = False

    def analyze_deck(
        self, deck_cards: list[tuple[str, str, str]], backend=None
    ) -> Optional[str]:
        """Analyze a deck list and store the strategy summary.

        Args:
            deck_cards: List of (card_name, card_type, oracle_text) tuples
            backend: Optional separate backend instance (avoids lock contention
                     with advice calls when run on a background thread)

        Returns:
            Strategy string, or None on failure
        """
        import time

        start = time.perf_counter()
        self._deck_strategy_pending = True

        # Use dedicated backend if provided, otherwise fall back to shared one
        be = backend or self._backend

        try:
            # Group duplicates compactly: "4x Mountain (Basic Land)"
            from collections import Counter

            # Group by (name, type) for counting, but keep oracle text
            oracle_by_name: dict[str, str] = {}
            count_key = Counter()
            for name, card_type, oracle in deck_cards:
                count_key[(name, card_type)] += 1
                if oracle and name not in oracle_by_name:
                    oracle_by_name[name] = oracle

            deck_lines = []
            for (name, card_type), count in count_key.most_common():
                type_short = card_type.split("—")[0].strip() if card_type else "Unknown"
                line = f"{count}x {name} ({type_short})"
                # Include oracle text for non-basic-land spells so the LLM
                # knows what the card actually does instead of guessing
                oracle = oracle_by_name.get(name, "")
                is_basic = "basic" in (card_type or "").lower()
                if oracle and not is_basic:
                    # Strip reminder text and truncate for token budget
                    oracle_short = self._remove_reminder_text(oracle).strip()
                    if len(oracle_short) > 120:
                        oracle_short = oracle_short[:117] + "..."
                    if oracle_short:
                        line += f" — {oracle_short}"
                deck_lines.append(line)

            deck_text = "\n".join(deck_lines)
            user_message = f"DECK LIST ({len(deck_cards)} cards):\n{deck_text}"

            # Deck analysis benefits from thinking (one-time, not real-time).
            # Also needs more tokens than game advice for the full strategy output.
            try:
                strategy = be.complete(
                    DECK_ANALYSIS_PROMPT,
                    user_message,
                    max_tokens=2048,
                    use_thinking=True,
                )
            except TypeError:
                # Backend doesn't support max_tokens parameter
                strategy = be.complete(DECK_ANALYSIS_PROMPT, user_message)

            # Don't store error/fallback messages as deck strategy
            if (
                not strategy
                or strategy.startswith("Error")
                or "didn't catch that" in strategy
            ):
                logger.warning(
                    f"Deck analysis returned error-like response: {strategy[:80] if strategy else 'empty'}"
                )
                return None

            # Truncate if too long
            if len(strategy) > 400:
                strategy = strategy[:397] + "..."

            self._deck_strategy = strategy
            elapsed = (time.perf_counter() - start) * 1000
            logger.info(
                f"Deck analysis complete: {elapsed:.0f}ms, {len(strategy)} chars"
            )
            return strategy
        except Exception as e:
            logger.error(f"Deck analysis failed: {e}")
            return None
        finally:
            self._deck_strategy_pending = False

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Rough token estimate for logging: ~4 chars per token.

        OPTIMIZATION: Added for prompt size monitoring.
        """
        return len(text) // 4

    def _remove_reminder_text(self, text: str) -> str:
        """Remove reminder text (text in parentheses) from oracle text."""
        import re

        # Handle nested parens if possible, but simple greedy match usually works for MTG
        # Use simple non-greedy match for multiple parens
        return re.sub(r"\(.*?\)", "", text)

    @staticmethod
    def _is_impending(card: dict) -> bool:
        """Check if a creature is in impending state (enchantment with time counters).

        When cast with impending, a card enters as an enchantment with time
        counters.  It is NOT a creature until the last counter is removed, so
        it should not be counted as an attacker, blocker, or combat threat.
        """
        counters = card.get("counters", {})
        has_time = any("time" in k.lower() for k in counters) if counters else False
        if not has_time:
            return False
        # Confirm oracle text mentions impending (avoids false positives on
        # other cards with time counters like suspend/vanishing)
        oracle = card.get("oracle_text", "").lower()
        return "impending" in oracle

    @staticmethod
    def _get_cmc(mana_cost: str) -> int:
        """Calculate converted mana cost from a mana cost string like '{1}{W}{W}'."""
        import re
        if not mana_cost:
            return 0
        cmc = 0
        generic = re.findall(r"\{(\d+)\}", mana_cost)
        cmc += sum(int(g) for g in generic)
        for color in "WUBRGC":
            cmc += len(re.findall(rf"\{{{color}\}}", mana_cost))
        hybrid = re.findall(r"\{[^}]+/[^}]+\}", mana_cost)
        cmc += len(hybrid)
        return cmc

    def _format_game_context(
        self, game_state: dict[str, Any], question: str = ""
    ) -> str:
        """Format the game state into a COMPACT context for the LLM.

        OPTIMIZATION: Heavily compressed to reduce token usage while maintaining accuracy.
        - Uses symbols (T=tapped, FLY=flying, SS=summoning sick)
        - Only shows oracle text for relevant cards (not basic lands)
        - Terse removal analysis (kill range, not individual targets)
        - Consolidated combat/blocking info
        - Removed redundant rule explanations (LLM knows MTG rules)
        """

        # Determine local player seat and active turn
        players = game_state.get("players", [])
        local_player = next((p for p in players if p.get("is_local")), None)
        local_seat = local_player.get("seat_id") if local_player else 1

        turn = game_state.get("turn", {})
        active_seat = turn.get("active_player", 0)
        priority_seat = turn.get("priority_player", 0)
        is_my_turn = active_seat == local_seat
        has_priority = priority_seat == local_seat

        phase = turn.get("phase", "Unknown").replace("Phase_", "")
        step = turn.get("step", "").replace("Step_", "")
        turn_num = turn.get("turn_number", 0)

        # Get legal moves (still useful for complex decisions)
        # MULLIGAN OVERRIDE: During mulligan, the player's only choices are
        # KEEP or MULLIGAN — priority_player is irrelevant, so skip the rules
        # engine which would say "Wait (Opponent has priority)".
        pending = game_state.get("pending_decision")
        if pending == "Mulligan":
            valid_moves = ["KEEP", "MULLIGAN"]
            valid_moves_str = "KEEP, MULLIGAN"
        elif pending == "Mulligan Bottom":
            # London Mulligan: player kept but must put N cards on bottom
            hand_cards = game_state.get("hand", [])
            card_names = [c.get("name", "Unknown") for c in hand_cards]
            valid_moves = [f"Bottom: {n}" for n in card_names]
            valid_moves_str = ", ".join(card_names)
        else:
            try:
                from arenamcp.rules_engine import RulesEngine

                valid_moves = RulesEngine.get_legal_actions(game_state)
                # OPTIMIZATION: Join inline instead of list, limit to 8 most important
                if not valid_moves:
                    valid_moves_str = 'NONE — say "pass priority"'
                else:
                    valid_moves_str = ", ".join(valid_moves[:8])
                    if len(valid_moves) > 8:
                        valid_moves_str += f"... (+{len(valid_moves) - 8})"
            except Exception as e:
                logger.error(f"RulesEngine error: {e}")
                valid_moves = []
                valid_moves_str = "Error"

        lines = []
        lines.append("=== GAME ===")
        lines.append(f"Legal: {valid_moves_str}")

        # POST-LAND PLANNING: When land drop is available, show what spells
        # become castable after playing each land. This lets the LLM give
        # multi-step advice ("Play Forest, then cast Llanowar Elves") instead
        # of just "Play land" followed by silence while the player acts.
        lands_played_count = local_player.get("lands_played", 0) if local_player else 0
        _stack = game_state.get("stack", [])
        has_land_drop = (
            is_my_turn
            and "Main" in phase
            and len(_stack) == 0
            and lands_played_count == 0
        )
        if has_land_drop and valid_moves:
            import re as _re_plan

            hand_cards = game_state.get("hand", [])
            bf = game_state.get("battlefield", [])

            # Current mana sources
            cur_mana = RulesEngine._count_available_mana(game_state, local_seat)

            # Find distinct land types in hand
            hand_lands = {}
            for c in hand_cards:
                if "Land" in c.get("type_line", ""):
                    name = c.get("name", "")
                    if name not in hand_lands:
                        hand_lands[name] = c

            # For each distinct land, compute what new spells become castable
            # Also track what colors each land adds for basic color checking
            if hand_lands:
                post_land_parts = []
                for land_name, land_card in hand_lands.items():
                    post_mana = cur_mana + 1
                    # Determine what color this land adds
                    land_oracle = land_card.get("oracle_text", "")
                    land_colors = set()
                    for color, basic in [
                        ("W", "Plains"),
                        ("U", "Island"),
                        ("B", "Swamp"),
                        ("R", "Mountain"),
                        ("G", "Forest"),
                    ]:
                        if basic in land_name or f"{{{color}}}" in land_oracle:
                            land_colors.add(color)
                    if "any color" in land_oracle.lower():
                        land_colors = {"W", "U", "B", "R", "G"}

                    new_casts = []
                    for c in hand_cards:
                        if "Land" in c.get("type_line", ""):
                            continue
                        cost = c.get("mana_cost", "")
                        cmc = RulesEngine._parse_cmc(cost)
                        # Currently not castable, but castable after land
                        if cur_mana < cmc <= post_mana:
                            # Basic color check: if spell has colored pips,
                            # verify at least one matches the land's colors
                            colored_pips = set(_re_plan.findall(r"\{([WUBRG])\}", cost))
                            # Also consider existing mana pool colors
                            existing_colors = set()
                            for bf_card in bf:
                                if bf_card.get(
                                    "owner_seat_id"
                                ) == local_seat and not bf_card.get("is_tapped"):
                                    bf_oracle = bf_card.get("oracle_text", "")
                                    bf_name = bf_card.get("name", "")
                                    for clr, bsc in [
                                        ("W", "Plains"),
                                        ("U", "Island"),
                                        ("B", "Swamp"),
                                        ("R", "Mountain"),
                                        ("G", "Forest"),
                                    ]:
                                        if bsc in bf_name or f"{{{clr}}}" in bf_oracle:
                                            existing_colors.add(clr)
                            available_colors = land_colors | existing_colors
                            if not colored_pips or colored_pips & available_colors:
                                new_casts.append(c.get("name", "?"))
                    if new_casts:
                        post_land_parts.append(
                            f"Play {land_name} → Cast {', '.join(new_casts)}"
                        )
                if post_land_parts:
                    lines.append(f"THEN: {'; '.join(post_land_parts)}")

        # Get player info
        opponent_player = None
        for p in players:
            if not p.get("is_local"):
                opponent_player = p
                break

        opp_seat = opponent_player.get("seat_id") if opponent_player else None

        # OPTIMIZATION: Compact turn info - one line
        active_label = "YOUR" if active_seat == local_seat else "OPP"
        priority_label = "You" if priority_seat == local_seat else "Opp"

        # Timing context
        is_main_phase = "Main" in phase
        is_your_turn = active_seat == local_seat
        stack = game_state.get("stack", [])
        stack_empty = len(stack) == 0
        can_cast_sorcery = (
            is_your_turn and is_main_phase and stack_empty and has_priority
        )
        is_blocking = "DeclareBlock" in step and not is_your_turn

        # Decision Check
        pending_decision = game_state.get("pending_decision")
        decision_context = game_state.get("decision_context")

        if pending_decision:
            # PHASE 1+2: Enhanced decision display with context
            if decision_context:
                dec_type = decision_context.get("type", "unknown")

                if dec_type == "mulligan_bottom":
                    # London Mulligan: choose cards to put on bottom
                    hand = game_state.get("hand", [])
                    num_to_bottom = max(1, 7 - len(hand) + 1)  # estimate from hand size
                    lines.append(f"!!! DECISION: MULLIGAN - PUT {num_to_bottom} CARD(S) ON BOTTOM !!!")
                    lines.append("Keep: lands + on-curve plays | Bottom: expensive/off-color/redundant")

                elif dec_type == "discard":
                    count = decision_context.get("count", 1)
                    lines.append(f"!!! DECISION: DISCARD {count} card(s) !!!")
                    lines.append(
                        "Choose: excess lands > high CMC uncastables > redundant copies"
                    )

                elif dec_type == "scry":
                    count = decision_context.get("count", 1)
                    lines.append(f"!!! DECISION: SCRY {count} !!!")
                    lines.append("Keep: needed lands/threats | Bottom: dead cards")

                elif dec_type == "surveil":
                    count = decision_context.get("count", 1)
                    lines.append(f"!!! DECISION: SURVEIL {count} !!!")
                    lines.append("Keep: want to draw | Graveyard: synergy or digging")

                elif dec_type == "target_selection":
                    source = decision_context.get("source_card", "spell")
                    lines.append(f"!!! DECISION: TARGET for {source} !!!")
                    lines.append("Choose: biggest threat or best value target")

                elif dec_type == "modal_choice":
                    num_opts = decision_context.get("num_options", "?")
                    lines.append(f"!!! DECISION: CHOOSE MODE ({num_opts} options) !!!")
                    lines.append("Evaluate: which mode solves current problem best")

                elif dec_type == "declare_attackers":
                    legal = decision_context.get("legal_attackers", [])
                    lines.append(f"!!! DECISION: DECLARE ATTACKERS ({len(legal)} legal) !!!")
                    if legal:
                        lines.append(f"Can attack: {', '.join(legal[:8])}")
                    lines.append("Choose: maximize damage while keeping safe blockers back")

                elif dec_type == "declare_blockers":
                    legal = decision_context.get("legal_blockers", [])
                    lines.append(f"!!! DECISION: DECLARE BLOCKERS ({len(legal)} legal) !!!")
                    if legal:
                        lines.append(f"Can block: {', '.join(legal[:8])}")
                    lines.append("Choose: trade up, double-block threats, protect life total")

                elif dec_type == "assign_damage":
                    lines.append("!!! DECISION: ASSIGN COMBAT DAMAGE !!!")
                    lines.append("Order: kill most important blocker/attacker first")

                elif dec_type == "order_combat_damage":
                    lines.append("!!! DECISION: ORDER COMBAT DAMAGE !!!")
                    lines.append("Order: prioritize killing the biggest threat")

                elif dec_type == "pay_costs":
                    source = decision_context.get("source_card", "spell")
                    lines.append(f"!!! DECISION: PAY COSTS for {source} !!!")
                    lines.append("Choose: tap lands that leave best mana open for responses")

                elif dec_type == "search":
                    lines.append("!!! DECISION: SEARCH LIBRARY !!!")
                    lines.append("Choose: what you need most — land, removal, threat, or answer")

                elif dec_type == "distribution":
                    source = decision_context.get("source_card", "effect")
                    total = decision_context.get("total", "?")
                    lines.append(f"!!! DECISION: DISTRIBUTE {total} from {source} !!!")
                    lines.append("Distribute: maximize kills, finish off wounded targets first")

                elif dec_type == "numeric_input":
                    source = decision_context.get("source_card", "effect")
                    min_v = decision_context.get("min", 0)
                    max_v = decision_context.get("max", "?")
                    lines.append(f"!!! DECISION: CHOOSE NUMBER for {source} ({min_v}-{max_v}) !!!")
                    lines.append("Choose: balance value vs. cost (life, mana, etc.)")

                elif dec_type == "choose_starting_player":
                    lines.append("!!! DECISION: PLAY OR DRAW !!!")
                    lines.append("Aggro decks: PLAY (tempo). Control/limited: DRAW (card advantage)")

                elif dec_type == "select_replacement":
                    lines.append("!!! DECISION: ORDER REPLACEMENT EFFECTS !!!")
                    lines.append("Choose: apply the replacement that gives most advantage first")

                elif dec_type == "casting_time_options":
                    lines.append("!!! DECISION: CHOOSE CASTING OPTION !!!")
                    lines.append("Evaluate: alternative cost vs normal cost (Foretell, Flashback, Escape)")

                elif dec_type == "select_counters":
                    lines.append("!!! DECISION: SELECT COUNTERS !!!")
                    lines.append("Choose: remove least valuable counters, keep most impactful")

                elif dec_type == "order_triggers":
                    lines.append("!!! DECISION: ORDER TRIGGERED ABILITIES !!!")
                    lines.append("Order: resolve most impactful trigger last (it resolves first)")

                elif dec_type == "select_n_group":
                    lines.append("!!! DECISION: SELECT FROM GROUP !!!")

                elif dec_type == "select_from_groups":
                    lines.append("!!! DECISION: SELECT FROM GROUPS !!!")

                elif dec_type == "search_from_groups":
                    lines.append("!!! DECISION: SEARCH FROM GROUPS !!!")

                elif dec_type == "gather":
                    lines.append("!!! DECISION: GATHER !!!")

                else:
                    # Fallback for other decision types
                    lines.append(f"!!! DECISION: {pending_decision} !!!")
            else:
                # No context available - generic display
                lines.append(f"!!! DECISION: {pending_decision} !!!")

            # Special handling for Mulligan - show hand summary for LLM
            if pending_decision == "Mulligan":
                my_hand = game_state.get("hand", [])
                if not my_hand:
                    lines.append("Waiting for hand...")
                else:
                    lands = [
                        c for c in my_hand if "land" in c.get("type_line", "").lower()
                    ]
                    creatures = [
                        c
                        for c in my_hand
                        if "creature" in c.get("type_line", "").lower()
                    ]
                    spells = [
                        c for c in my_hand if c not in lands and c not in creatures
                    ]
                    cmcs = []
                    for c in my_hand:
                        cost = c.get("mana_cost", "")
                        if cost:
                            import re as _re

                            generic = sum(
                                int(g) for g in _re.findall(r"\{(\d+)\}", cost)
                            )
                            pips = len(_re.findall(r"\{[WUBRGC]\}", cost))
                            cmcs.append(generic + pips)
                        else:
                            cmcs.append(0)
                    avg_cmc = sum(cmcs) / len(cmcs) if cmcs else 0
                    land_names = [c.get("name", "?") for c in lands]
                    nonland_names = [
                        f"{c.get('name', '?')} ({c.get('mana_cost', '')})"
                        for c in my_hand
                        if c not in lands
                    ]
                    lines.append(
                        f"MULLIGAN HAND: {len(lands)} lands, {len(creatures)} creatures, {len(spells)} spells, avg CMC {avg_cmc:.1f}"
                    )
                    lines.append(
                        f"  Lands: {', '.join(land_names) if land_names else 'NONE'}"
                    )
                    lines.append(
                        f"  Nonland: {', '.join(nonland_names) if nonland_names else 'NONE'}"
                    )
                    lines.append(
                        "Decide: KEEP or MULLIGAN based on curve, colors, and land count"
                    )

        # OPTIMIZATION: Single line for turn/phase/priority
        # During mulligan, override the turn line to avoid "Pri:Opp" confusing the LLM
        if pending_decision in ("Mulligan", "Mulligan Bottom"):
            lines.append("YOUR MULLIGAN DECISION")
        else:
            phase_str = f"{phase}/{step}" if step else phase
            lines.append(f"T{turn_num} {active_label} | {phase_str} | Pri:{priority_label}")

        # OPTIMIZATION: Compact timing rules - single line
        # Skip timing info during mulligan - it's irrelevant and confusing
        if pending_decision not in ("Mulligan", "Mulligan Bottom"):
            if can_cast_sorcery:
                lines.append("Timing: ALL SPELLS")
            elif is_blocking:
                lines.append("ACTION: DECLARE BLOCKERS")
            elif is_your_turn and is_main_phase and not stack_empty:
                lines.append("Timing: ALL SPELLS (after stack resolves)")
            else:
                lines.append("Timing: INSTANTS ONLY")

        # OPTIMIZATION: Compact life totals - single line with damage tracking
        your_life = local_player.get("life_total", "?") if local_player else "?"
        opp_life = opponent_player.get("life_total", "?") if opponent_player else "?"
        damage_taken = game_state.get("damage_taken", {})
        your_dmg = damage_taken.get(str(local_seat), damage_taken.get(local_seat, 0))
        opp_dmg = damage_taken.get(str(opp_seat), damage_taken.get(opp_seat, 0)) if opp_seat else 0
        your_dmg_str = f" (taken {your_dmg})" if your_dmg else ""
        opp_dmg_str = f" (taken {opp_dmg})" if opp_dmg else ""
        lines.append(f"Life: You={your_life}{your_dmg_str} Opp={opp_life}{opp_dmg_str}")

        # Battlefield - grouped by owner
        battlefield = game_state.get("battlefield", [])
        your_cards = [c for c in battlefield if c.get("owner_seat_id") == local_seat and c.get("type_line", "").lower() != "ability"]
        opp_cards = [c for c in battlefield if c.get("owner_seat_id") != local_seat and c.get("type_line", "").lower() != "ability"]

        # OPTIMIZATION: Compact mana calculation
        # Track mana sources individually to avoid misleading dual-land displays
        mana_pool = {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0, "Any": 0}
        mana_sources = []  # Track individual sources for clearer display
        total_mana = 0
        creature_mana_source_count = 0  # For bonus-mana detection

        for card in your_cards:
            type_line = card.get("type_line", "").lower()
            oracle = card.get("oracle_text", "")
            is_creature = "creature" in type_line
            is_land = "land" in type_line
            # Check for casting sickness (unless it has haste)
            has_haste = "haste" in self._remove_reminder_text(oracle).lower()
            is_summoning_sick = (
                is_creature
                and card.get("turn_entered_battlefield") == turn_num
                and not has_haste
            )

            # Count mana sources (Lands AND Creatures)
            # Logic: Untapped AND (Land OR (Creature AND "add {" in oracle AND not summoning sick))
            # Relaxed check: Just looking for "Add " or "{T}: Add" is surprisingly robust for dorks
            has_mana_ability = (
                "add {" in oracle.lower() or "add one mana" in oracle.lower()
            )
            # Some lands say "this land is the chosen type" (e.g. Multiversal Passage)
            # — they produce mana of whatever basic type they are but don't have "add {" in oracle.
            # Detect via basic land subtypes in the type_line.
            if is_land and not has_mana_ability:
                for basic in ("plains", "island", "swamp", "mountain", "forest"):
                    if basic in type_line:
                        has_mana_ability = True
                        break

            if not card.get("is_tapped"):
                if is_land or (
                    is_creature and has_mana_ability and not is_summoning_sick
                ):
                    total_mana += 1
                    name = card.get("name", "")

                    # Track creature mana sources for bonus-mana effects
                    if is_creature and has_mana_ability and not is_summoning_sick:
                        creature_mana_source_count += 1

                    # Detect what colors this specific source can produce
                    source_colors = []
                    if "Plains" in name or "plains" in type_line or "{W}" in oracle:
                        mana_pool["W"] += 1
                        source_colors.append("W")
                    if "Island" in name or "island" in type_line or "{U}" in oracle:
                        mana_pool["U"] += 1
                        source_colors.append("U")
                    if "Swamp" in name or "swamp" in type_line or "{B}" in oracle:
                        mana_pool["B"] += 1
                        source_colors.append("B")
                    if "Mountain" in name or "mountain" in type_line or "{R}" in oracle:
                        mana_pool["R"] += 1
                        source_colors.append("R")
                    if "Forest" in name or "forest" in type_line or "{G}" in oracle:
                        mana_pool["G"] += 1
                        source_colors.append("G")
                    if "{C}" in oracle:
                        mana_pool["C"] += 1
                        source_colors.append("C")
                    if "any color" in oracle.lower():
                        mana_pool["Any"] += 1
                        source_colors.append("Any")

                    # Track this source for clearer display
                    if len(source_colors) > 1:
                        # Multi-color source (dual land, etc.)
                        mana_sources.append("/".join(source_colors))
                    elif len(source_colors) == 1:
                        mana_sources.append(source_colors[0])

        # Detect bonus-mana effects: "whenever you tap a creature for mana, add"
        # e.g. Badgermole Cub, Leyline of Abundance
        mana_bonus_notes = []
        import re
        for card in your_cards:
            oracle_lower = card.get("oracle_text", "").lower()
            name = card.get("name", "")
            # Pattern: "whenever you tap a creature for mana, add an additional {X}"
            bonus_match = re.search(r"whenever you tap a creature for mana,?\s*add an additional \{(\w)\}", oracle_lower)
            if bonus_match and creature_mana_source_count > 0:
                bonus_color = bonus_match.group(1).upper()
                bonus_total = creature_mana_source_count
                total_mana += bonus_total
                if bonus_color in mana_pool:
                    mana_pool[bonus_color] += bonus_total
                # Add bonus sources to display
                for _ in range(bonus_total):
                    mana_sources.append(f"+{bonus_color}")
                logger.info(f"Mana bonus from {name}: +{bonus_total} {{{bonus_color}}} ({creature_mana_source_count} creature sources)")

            # Detect untap-on-cast effects for annotation
            if "untap" in oracle_lower and ("mana value" in oracle_lower or "converted mana cost" in oracle_lower):
                untap_match = re.search(r"(?:mana value|converted mana cost)\s*(\d+)\s*or greater.*untap|cast.*(?:mana value|converted mana cost)\s*(\d+).*untap|untap.*(?:mana value|converted mana cost)\s*(\d+)", oracle_lower)
                if untap_match:
                    threshold = untap_match.group(1) or untap_match.group(2) or untap_match.group(3)
                    mana_bonus_notes.append(f"{name} untaps on MV{threshold}+ cast → tap again for extra mana")

        logger.info(f"Mana: {mana_pool} (Total: {total_mana})")

        # IMPROVED: Show mana sources more clearly to avoid dual-land confusion
        # Old: "Mana: 1 (U:1 G:1)" - misleading, looks like 2 mana!
        # New: "Mana: 1 (sources: {U/G})" - clear that it's one source with options
        if mana_sources:
            # Group identical sources: ["W", "W", "U/G"] -> "W W {U/G}"
            source_display = " ".join(
                f"{{{s}}}" if "/" in s else s for s in mana_sources
            )
            lines.append(f"Mana: {total_mana} (sources: {source_display})")
        else:
            lines.append(f"Mana: 0")
        for note in mana_bonus_notes:
            lines.append(f"⚠️ {note}")

        # OPTIMIZATION: Compact land drop status
        lands_played = local_player.get("lands_played", 0) if local_player else 0
        if is_your_turn and lands_played == 0:
            lines.append("Land: AVAILABLE")
        elif is_your_turn:
            lines.append(f"Land: USED ({lands_played})")
        else:
            lines.append("Land: N/A (opp turn)")

        # OPTIMIZATION: Compact battlefield display with symbols
        # T=tapped, FLY=flying, RCH=reach, SS=summoning sick, ATK=attacking, BLK=blocking
        if battlefield:
            lines.append("")
            lines.append(f"YOUR BOARD:")
            if your_cards:
                # Disambiguate duplicate names: "Gene Pollinator #1", "Gene Pollinator #2"
                your_name_counts = Counter(c.get("name", "Unknown") for c in your_cards)
                your_name_seen = {}
                for card in your_cards:
                    name = card.get("name", "Unknown")
                    type_line = card.get("type_line", "").lower()

                    # Disambiguate duplicate names
                    if your_name_counts[name] > 1:
                        your_name_seen[name] = your_name_seen.get(name, 0) + 1
                        display_name = f"{name} #{your_name_seen[name]}"
                    else:
                        display_name = name

                    # P/T for creatures (use type_line, handle null power)
                    pt = (
                        f" {card.get('power') or 0}/{card.get('toughness') or 0}"
                        if "creature" in type_line or card.get("power") is not None
                        else ""
                    )

                    # Status flags (compact symbols)
                    flags = []

                    # Type tags for non-creature non-land permanents
                    is_creature = "creature" in type_line
                    is_land = "land" in type_line
                    if not is_creature and not is_land:
                        if "equipment" in type_line:
                            flags.append("EQUIPMENT")
                        elif "artifact" in type_line:
                            flags.append("ARTIFACT")
                        if "enchantment" in type_line:
                            flags.append("ENCHANT")
                        if "planeswalker" in type_line:
                            flags.append("PW")

                    if card.get("is_tapped"):
                        flags.append("T")

                    oracle_text = self._remove_reminder_text(
                        card.get("oracle_text", "")
                    ).lower()
                    is_creature = "creature" in type_line

                    # Keywords that matter for combat
                    if "flying" in oracle_text:
                        flags.append("FLY")
                    if "reach" in oracle_text:
                        flags.append("RCH")
                    if "haste" in oracle_text:
                        flags.append("HST")
                    if "vigilance" in oracle_text:
                        flags.append("VIG")
                    if "trample" in oracle_text:
                        flags.append("TRM")
                    if "first strike" in oracle_text:
                        flags.append("FS")
                    if "deathtouch" in oracle_text:
                        flags.append("DTH")

                    # Summoning sickness check
                    if (
                        is_creature
                        and card.get("turn_entered_battlefield") == turn_num
                        and "haste" not in oracle_text
                    ):
                        flags.append("SS")

                    # Impending: enchantment with time counters, not a creature yet
                    if self._is_impending(card):
                        flags.append("IMPENDING")

                    if card.get("is_attacking"):
                        flags.append("ATK")
                    if card.get("is_blocking"):
                        flags.append("BLK")

                    # Token and counter annotations
                    obj_kind = card.get("object_kind", "")
                    if obj_kind == "TOKEN":
                        display_name = f"*{display_name}"
                    counters = card.get("counters", {})
                    counter_str = ""
                    if counters:
                        cparts = []
                        for ctype, ccount in counters.items():
                            clean = ctype.replace("CounterType_", "")[:4]
                            cparts.append(f"{ccount}{clean}")
                        counter_str = f" ({','.join(cparts)})"

                    flag_str = f" [{','.join(flags)}]" if flags else ""
                    lines.append(f"  {display_name}{pt}{counter_str}{flag_str}")

                    # Show compact oracle text for non-vanilla permanents so the
                    # LLM doesn't hallucinate abilities.  Skip basic lands and
                    # cards where the keywords already capture everything.
                    raw_oracle = card.get("oracle_text", "")
                    if raw_oracle and not is_land:
                        stripped = self._remove_reminder_text(raw_oracle).strip()
                        # Skip if the oracle text is ONLY keywords already captured by flags
                        keyword_only = all(
                            w in {"flying", "reach", "haste", "vigilance", "trample",
                                  "first", "strike", "double", "deathtouch", "lifelink",
                                  "menace", "ward", "hexproof", "indestructible", "defender"}
                            for w in stripped.lower().replace(",", " ").replace("\n", " ").split()
                            if w
                        )
                        if not keyword_only and len(stripped) > 0:
                            oracle_compact = stripped[:120] + ("..." if len(stripped) > 120 else "")
                            lines.append(f"    {oracle_compact}")

            else:
                lines.append("  (empty)")

            # Pre-compute inferred attackers for DeclareBlock display
            # MTGA doesn't always send isAttacking on game objects, so we infer
            # from tapped non-SS creatures during opponent's combat
            _inferred_atk_ids = set()
            if "Combat" in phase and not is_your_turn and "DeclareBlock" in step:
                has_explicit_atk = any(c.get("is_attacking") for c in opp_cards)
                if not has_explicit_atk:
                    for c in opp_cards:
                        c_type = c.get("type_line", "").lower()
                        c_oracle = self._remove_reminder_text(
                            c.get("oracle_text", "")
                        ).lower()
                        is_ss = (
                            c.get("turn_entered_battlefield") == turn_num
                            and "haste" not in c_oracle
                        )
                        if (
                            c.get("is_tapped")
                            and "creature" in c_type
                            and not is_ss
                        ):
                            _inferred_atk_ids.add(c.get("instance_id"))

            lines.append(f"OPP BOARD:")
            if opp_cards:
                opp_name_counts = Counter(c.get("name", "Unknown") for c in opp_cards)
                opp_name_seen = {}
                for card in opp_cards:
                    name = card.get("name", "Unknown")
                    type_line = card.get("type_line", "").lower()

                    # Disambiguate duplicate names
                    if opp_name_counts[name] > 1:
                        opp_name_seen[name] = opp_name_seen.get(name, 0) + 1
                        display_name = f"{name} #{opp_name_seen[name]}"
                    else:
                        display_name = name

                    pt = (
                        f" {card['power']}/{card['toughness']}"
                        if card.get("power") is not None
                        else ""
                    )

                    flags = []

                    # Type tags for non-creature non-land permanents
                    is_creature = "creature" in type_line
                    is_land = "land" in type_line
                    if not is_creature and not is_land:
                        if "equipment" in type_line:
                            flags.append("EQUIPMENT")
                        elif "artifact" in type_line:
                            flags.append("ARTIFACT")
                        if "enchantment" in type_line:
                            flags.append("ENCHANT")
                        if "planeswalker" in type_line:
                            flags.append("PW")

                    if card.get("is_tapped"):
                        flags.append("T")

                    oracle_text = self._remove_reminder_text(
                        card.get("oracle_text", "")
                    ).lower()
                    if "flying" in oracle_text:
                        flags.append("FLY")
                    if "reach" in oracle_text:
                        flags.append("RCH")
                    if "vigilance" in oracle_text:
                        flags.append("VIG")
                    if "trample" in oracle_text:
                        flags.append("TRM")
                    if "first strike" in oracle_text:
                        flags.append("FS")
                    if "deathtouch" in oracle_text:
                        flags.append("DTH")

                    # Summoning sickness check for opponent creatures
                    if (
                        is_creature
                        and card.get("turn_entered_battlefield") == turn_num
                        and "haste" not in oracle_text
                    ):
                        flags.append("SS")

                    # Impending: enchantment with time counters, not a creature yet
                    if self._is_impending(card):
                        flags.append("IMPENDING")

                    if (
                        card.get("is_attacking")
                        or card.get("instance_id") in _inferred_atk_ids
                    ):
                        flags.append("ATK")
                    if card.get("is_blocking"):
                        flags.append("BLK")

                    # Token and counter annotations (opponent)
                    obj_kind = card.get("object_kind", "")
                    if obj_kind == "TOKEN":
                        display_name = f"*{display_name}"
                    counters = card.get("counters", {})
                    counter_str = ""
                    if counters:
                        cparts = []
                        for ctype, ccount in counters.items():
                            clean = ctype.replace("CounterType_", "")[:4]
                            cparts.append(f"{ccount}{clean}")
                        counter_str = f" ({','.join(cparts)})"

                    flag_str = f" [{','.join(flags)}]" if flags else ""
                    lines.append(f"  {display_name}{pt}{counter_str}{flag_str}")

                    # Show compact oracle text for opponent permanents too
                    raw_oracle = card.get("oracle_text", "")
                    if raw_oracle and "land" not in type_line:
                        stripped = self._remove_reminder_text(raw_oracle).strip()
                        keyword_only = all(
                            w in {"flying", "reach", "haste", "vigilance", "trample",
                                  "first", "strike", "double", "deathtouch", "lifelink",
                                  "menace", "ward", "hexproof", "indestructible", "defender"}
                            for w in stripped.lower().replace(",", " ").replace("\n", " ").split()
                            if w
                        )
                        if not keyword_only and len(stripped) > 0:
                            oracle_compact = stripped[:120] + ("..." if len(stripped) > 120 else "")
                            lines.append(f"    {oracle_compact}")
            else:
                lines.append("  (empty)")

            # OPTIMIZATION: Compact combat analysis (was 70+ lines, now ~20)
            if ("Combat" in phase or "Main" in phase) and is_your_turn:
                your_creatures = [
                    c
                    for c in your_cards
                    if "creature" in c.get("type_line", "").lower()
                    and not self._is_impending(c)
                ]

                valid_attackers = [
                    c
                    for c in your_creatures
                    if not c.get("is_tapped")
                    and not (
                        c.get("turn_entered_battlefield") == turn_num
                        and "haste"
                        not in self._remove_reminder_text(
                            c.get("oracle_text", "")
                        ).lower()
                    )
                ]

                your_attack_power = sum(c.get("power") or 0 for c in valid_attackers)

                opp_creatures = [
                    c
                    for c in opp_cards
                    if "creature" in c.get("type_line", "").lower()
                    and not self._is_impending(c)
                ]
                opp_blockers = [c for c in opp_creatures if not c.get("is_tapped")]
                opp_block_count = len(opp_blockers)
                opp_life = (
                    opponent_player.get("life_total", 20) if opponent_player else 20
                )

                # Single line combat summary
                if valid_attackers:
                    lethal = (
                        "LETHAL"
                        if (opp_block_count == 0 and your_attack_power >= opp_life)
                        else f"{opp_block_count}blk"
                    )
                    attacker_names = [c.get("name", "?") for c in valid_attackers]
                    # Disambiguate duplicate attacker names
                    atk_name_counts = Counter(attacker_names)
                    atk_name_seen: dict[str, int] = {}
                    deduped_names = []
                    for n in attacker_names:
                        if atk_name_counts[n] > 1:
                            atk_name_seen[n] = atk_name_seen.get(n, 0) + 1
                            deduped_names.append(f"{n} #{atk_name_seen[n]}")
                        else:
                            deduped_names.append(n)
                    attacker_list = ", ".join(deduped_names)
                    lines.append(
                        f"Atk: {len(valid_attackers)}cr/{your_attack_power}pwr vs {lethal} — can attack: {attacker_list}"
                    )
                    # Trade analysis for attacks: show what happens if each blocker blocks each attacker
                    if valid_attackers and opp_blockers:
                        for atk in valid_attackers:
                            atk_name = atk.get("name", "?")
                            atk_pow = atk.get("power") or 0
                            atk_tgh = atk.get("toughness") or 0
                            atk_oracle = self._remove_reminder_text(
                                atk.get("oracle_text", "")
                            ).lower()
                            atk_has_fly = "flying" in atk_oracle
                            atk_has_dth = "deathtouch" in atk_oracle
                            atk_has_trample = "trample" in atk_oracle
                            atk_has_fs = "first strike" in atk_oracle or "double strike" in atk_oracle
                            for blk in opp_blockers:
                                blk_name = blk.get("name", "?")
                                blk_pow = blk.get("power") or 0
                                blk_tgh = blk.get("toughness") or 0
                                blk_oracle = self._remove_reminder_text(
                                    blk.get("oracle_text", "")
                                ).lower()
                                blk_has_fly = "flying" in blk_oracle
                                blk_has_reach = "reach" in blk_oracle
                                blk_has_dth = "deathtouch" in blk_oracle
                                blk_has_fs = "first strike" in blk_oracle or "double strike" in blk_oracle
                                # Skip if blocker can't legally block (flying vs no fly/reach)
                                if atk_has_fly and not blk_has_fly and not blk_has_reach:
                                    continue
                                # Determine outcomes
                                atk_dies = (blk_pow >= atk_tgh) or blk_has_dth
                                blk_dies = (atk_pow >= blk_tgh) or atk_has_dth
                                if atk_has_fs and not blk_has_fs:
                                    if atk_pow >= blk_tgh or atk_has_dth:
                                        atk_dies = False
                                elif blk_has_fs and not atk_has_fs:
                                    if blk_pow >= atk_tgh or blk_has_dth:
                                        blk_dies = False
                                if atk_dies and blk_dies:
                                    result = "TRADE (both die)"
                                elif atk_dies:
                                    result = f"BAD — {atk_name} dies, {blk_name} lives ({blk_tgh - atk_pow} left)"
                                elif blk_dies:
                                    trample_note = ""
                                    if atk_has_trample:
                                        spillover = atk_pow - blk_tgh
                                        if spillover > 0:
                                            trample_note = f", {spillover} trample through"
                                    result = f"GOOD — {blk_name} dies, {atk_name} lives ({atk_tgh - blk_pow} left){trample_note}"
                                else:
                                    result = f"BAD — both live, {atk_name} just bounces off"
                                lines.append(
                                    f"  If {blk_name} {blk_pow}/{blk_tgh} blocks {atk_name} {atk_pow}/{atk_tgh}: {result}"
                                )
                else:
                    lines.append(f"Atk: None (T/SS)")

                # Crackback warning: opponent's potential attack power next turn
                opp_attack_power = sum(
                    c.get("power") or 0 for c in opp_creatures
                )
                your_life = (
                    local_player.get("life_total", 20) if local_player else 20
                )
                if opp_attack_power > 0:
                    # Helper: compute crackback damage through optimal blocking
                    def _crackback_dmg(blockers_list):
                        cb_avail = list(blockers_list)
                        dmg = 0
                        cb_sorted = sorted(
                            opp_creatures, key=lambda c: c.get("power") or 0, reverse=True
                        )
                        for opp_c in cb_sorted:
                            opp_pow = opp_c.get("power") or 0
                            opp_oracle = self._remove_reminder_text(
                                opp_c.get("oracle_text", "")
                            ).lower()
                            opp_has_fly = "flying" in opp_oracle
                            opp_has_trample = "trample" in opp_oracle
                            cb_valid = []
                            for i, blk in enumerate(cb_avail):
                                blk_oracle = self._remove_reminder_text(
                                    blk.get("oracle_text", "")
                                ).lower()
                                if opp_has_fly and "flying" not in blk_oracle and "reach" not in blk_oracle:
                                    continue
                                cb_valid.append((i, blk))
                            if cb_valid:
                                if opp_has_trample:
                                    idx, blocker = max(
                                        cb_valid, key=lambda x: x[1].get("toughness") or 0
                                    )
                                else:
                                    idx, blocker = min(
                                        cb_valid, key=lambda x: x[1].get("toughness") or 0
                                    )
                                cb_avail.pop(idx)
                                if opp_has_trample:
                                    spillover = max(
                                        0, opp_pow - (blocker.get("toughness") or 0)
                                    )
                                    dmg += spillover
                            else:
                                dmg += opp_pow
                        return dmg

                    # Scenario 1: All-out attack (only non-attackers can block)
                    non_attackers = [
                        c for c in your_creatures if c not in valid_attackers
                    ]
                    allout_dmg = _crackback_dmg(non_attackers)
                    life_after_allout = your_life - allout_dmg

                    # Scenario 2: No attack (all creatures available to block)
                    noatk_dmg = _crackback_dmg(your_creatures)
                    life_after_noatk = your_life - noatk_dmg

                    life_margin = your_life - opp_attack_power
                    if life_after_allout <= 0:
                        if life_after_noatk > 0:
                            # All-out is lethal but holding back is safe
                            lines.append(
                                f"⚠️ Crackback: opp {opp_attack_power}pwr — ALL-OUT lethal "
                                f"({allout_dmg} through vs {your_life} life), but holding all "
                                f"{len(your_creatures)} blockers → only {noatk_dmg} through → "
                                f"SAFE at {life_after_noatk} life. Attack selectively!"
                            )
                        else:
                            lines.append(
                                f"⚠️ Crackback: opp {opp_attack_power}pwr → LETHAL even with "
                                f"all {len(your_creatures)} blockers ({noatk_dmg} through vs "
                                f"{your_life} life)! Must race or remove threats!"
                            )
                    elif life_margin <= 0:
                        if allout_dmg < opp_attack_power and len(non_attackers) > 0:
                            lines.append(
                                f"Crackback: opp {opp_attack_power}pwr, but your {len(non_attackers)} blocker(s) absorb "
                                f"{opp_attack_power - allout_dmg} → only {allout_dmg} through vs {your_life} life — "
                                f"{'safe' if life_after_allout > 3 else 'tight'}"
                            )
                        else:
                            lines.append(
                                f"Crackback: {opp_attack_power}pwr vs your {your_life} life — LETHAL if no blockers held!"
                            )
                    elif life_margin <= 3:
                        lines.append(
                            f"Crackback: {opp_attack_power}pwr vs your {your_life} life — DANGER (only {life_margin} margin!)"
                        )
                    else:
                        lines.append(
                            f"Crackback: {opp_attack_power}pwr vs your {your_life} life — safe"
                        )

            # OPTIMIZATION: Compact blocking analysis (was 50+ lines, now ~10)
            elif "Combat" in phase and not is_your_turn:
                attacking = [c for c in opp_cards if c.get("is_attacking")]
                # Fallback: reuse pre-computed inferred attackers from display section
                if not attacking and _inferred_atk_ids:
                    attacking = [
                        c
                        for c in opp_cards
                        if c.get("instance_id") in _inferred_atk_ids
                    ]
                flying_atk = [
                    c
                    for c in attacking
                    if "flying"
                    in self._remove_reminder_text(c.get("oracle_text", "")).lower()
                ]
                ground_atk = [c for c in attacking if c not in flying_atk]

                your_creatures = [
                    c
                    for c in your_cards
                    if "creature" in c.get("type_line", "").lower()
                    and not c.get("is_tapped")
                    and not self._is_impending(c)
                ]

                flyer_blockers = [
                    c
                    for c in your_creatures
                    if any(
                        kw
                        in self._remove_reminder_text(c.get("oracle_text", "")).lower()
                        for kw in ["flying", "reach"]
                    )
                ]

                # Single line blocking summary
                if attacking:
                    fly_dmg = sum(c.get("power") or 0 for c in flying_atk)
                    gnd_dmg = sum(c.get("power") or 0 for c in ground_atk)
                    total_incoming = fly_dmg + gnd_dmg
                    your_life = (
                        local_player.get("life_total", 20) if local_player else 20
                    )
                    lines.append(
                        f"Blk: {fly_dmg}fly/{gnd_dmg}gnd dmg | {len(flyer_blockers)}FLY-blk avail"
                    )
                    # Show total incoming damage and post-combat life if unblocked
                    life_after_no_blocks = your_life - total_incoming
                    if life_after_no_blocks <= 0:
                        lines.append(
                            f"⚠️ No blocks → {total_incoming} dmg → DEAD (from {your_life} life)! Must block!"
                        )
                    else:
                        lines.append(
                            f"No blocks → take {total_incoming} dmg → {life_after_no_blocks} life remaining"
                        )
                    if flying_atk and not flyer_blockers:
                        lines.append(f"⚠️ {fly_dmg} UNBLOCKABLE!")
                    # Deathtouch warning — critical for blocking decisions
                    dth_atk = [
                        c for c in attacking
                        if "deathtouch" in self._remove_reminder_text(c.get("oracle_text", "")).lower()
                    ]
                    if dth_atk:
                        dth_names = ", ".join(c.get("name", "?") for c in dth_atk)
                        lines.append(f"⚠️ DEATHTOUCH: {dth_names} — any blocker DIES regardless of toughness!")

                    # Compute minimum damage after optimal blocking
                    available_blk = list(your_creatures)
                    damage_through = 0
                    # Sort attackers by power descending (block biggest threats first)
                    sorted_atk = sorted(
                        attacking, key=lambda c: c.get("power") or 0, reverse=True
                    )
                    for atk in sorted_atk:
                        atk_pow = atk.get("power") or 0
                        atk_oracle = self._remove_reminder_text(
                            atk.get("oracle_text", "")
                        ).lower()
                        atk_has_fly = "flying" in atk_oracle
                        atk_has_trample = "trample" in atk_oracle
                        # Find valid blockers for this attacker
                        valid = []
                        for i, blk in enumerate(available_blk):
                            blk_oracle = self._remove_reminder_text(
                                blk.get("oracle_text", "")
                            ).lower()
                            if atk_has_fly and "flying" not in blk_oracle and "reach" not in blk_oracle:
                                continue
                            valid.append((i, blk))
                        if valid:
                            if atk_has_trample:
                                # Use highest-toughness blocker to minimize trample spillover
                                idx, blocker = max(
                                    valid, key=lambda x: x[1].get("toughness") or 0
                                )
                            else:
                                # Use smallest blocker to preserve bigger ones
                                idx, blocker = min(
                                    valid, key=lambda x: x[1].get("toughness") or 0
                                )
                            available_blk.pop(idx)
                            if atk_has_trample:
                                spillover = max(
                                    0, atk_pow - (blocker.get("toughness") or 0)
                                )
                                damage_through += spillover
                            # else: no damage through (blocked without trample)
                        else:
                            damage_through += atk_pow  # unblockable

                    life_after_blocks = your_life - damage_through
                    if damage_through < total_incoming:
                        if life_after_blocks <= 0:
                            lines.append(
                                f"⚠️ Best blocks → take {damage_through} dmg → DEAD (from {your_life} life)! Not enough blockers!"
                            )
                        else:
                            lines.append(
                                f"Best blocks → take {damage_through} dmg → {life_after_blocks} life"
                            )
                    else:
                        # All blockers used but no damage reduction (e.g. all trample)
                        life_after_blocks = life_after_no_blocks

                    # Trade analysis: show what happens for each block
                    if your_creatures and attacking:
                        for atk in attacking:
                            atk_name = atk.get("name", "?")
                            atk_pow = atk.get("power") or 0
                            atk_tgh = atk.get("toughness") or 0
                            atk_oracle = self._remove_reminder_text(
                                atk.get("oracle_text", "")
                            ).lower()
                            atk_has_fly = "flying" in atk_oracle
                            atk_has_dth = "deathtouch" in atk_oracle
                            atk_has_trample = "trample" in atk_oracle
                            atk_has_fs = "first strike" in atk_oracle or "double strike" in atk_oracle
                            for blk in your_creatures:
                                blk_name = blk.get("name", "?")
                                blk_pow = blk.get("power") or 0
                                blk_tgh = blk.get("toughness") or 0
                                blk_oracle = self._remove_reminder_text(
                                    blk.get("oracle_text", "")
                                ).lower()
                                blk_has_fly = "flying" in blk_oracle
                                blk_has_reach = "reach" in blk_oracle
                                blk_has_dth = "deathtouch" in blk_oracle
                                blk_has_fs = "first strike" in blk_oracle or "double strike" in blk_oracle
                                # Skip if blocker can't legally block (flying vs no fly/reach)
                                if atk_has_fly and not blk_has_fly and not blk_has_reach:
                                    continue
                                # Determine outcomes
                                atk_dies = (blk_pow >= atk_tgh) or blk_has_dth
                                blk_dies = (atk_pow >= blk_tgh) or atk_has_dth
                                # First strike advantage
                                if atk_has_fs and not blk_has_fs:
                                    if atk_pow >= blk_tgh or atk_has_dth:
                                        atk_dies = False  # blocker dies before dealing damage
                                elif blk_has_fs and not atk_has_fs:
                                    if blk_pow >= atk_tgh or blk_has_dth:
                                        blk_dies = False  # attacker dies before dealing damage
                                if atk_dies and blk_dies:
                                    result = "TRADE (both die)"
                                elif atk_dies:
                                    result = f"{atk_name} dies, {blk_name} lives ({blk_tgh - atk_pow} left)"
                                elif blk_dies:
                                    trample_dmg = ""
                                    if atk_has_trample:
                                        spillover = atk_pow - blk_tgh
                                        if spillover > 0:
                                            trample_dmg = f", {spillover} trample through"
                                    result = f"{blk_name} dies, {atk_name} lives ({atk_tgh - blk_pow} left){trample_dmg}"
                                else:
                                    result = "both live"
                                lines.append(
                                    f"  If {blk_name} {blk_pow}/{blk_tgh} blocks {atk_name} {atk_pow}/{atk_tgh}: {result}"
                                )

                    # Next-turn danger: opponent's non-attacking creatures can
                    # attack again next turn alongside any surviving attackers
                    opp_non_attacking = [
                        c for c in opp_cards
                        if "creature" in c.get("type_line", "").lower()
                        and c not in attacking
                        and not self._is_impending(c)
                    ]
                    opp_next_turn_power = (
                        sum(c.get("power") or 0 for c in attacking)
                        + sum(c.get("power") or 0 for c in opp_non_attacking)
                    )
                    if opp_next_turn_power > 0 and life_after_blocks > 0:
                        if opp_next_turn_power >= life_after_blocks:
                            lines.append(
                                f"⚠️ Next turn: opp can attack for up to {opp_next_turn_power}pwr — "
                                f"LETHAL if you're at {life_after_blocks} life after this combat! Preserve blockers!"
                            )
        else:
            lines.append("")
            lines.append("BOARD: Empty")

        # Recent events (damage, zone transfers, reveals, etc.)
        recent_events = game_state.get("recent_events", [])
        if recent_events:
            event_strs = []
            for evt in recent_events[-5:]:
                etype = evt.get("type", "")
                if etype == "damage_dealt":
                    event_strs.append(f"{evt.get('source','?')} dealt {evt.get('amount',0)} to {evt.get('target','?')}")
                elif etype == "zone_transfer":
                    event_strs.append(f"{evt.get('card','?')} moved zones")
                elif etype == "counter_added":
                    event_strs.append(f"+{evt.get('amount',1)} counter on {evt.get('card','?')}")
                elif etype == "counter_removed":
                    event_strs.append(f"-{evt.get('amount',1)} counter from {evt.get('card','?')}")
                elif etype == "token_created":
                    event_strs.append(f"Token: {evt.get('card','?')}")
                elif etype == "card_revealed":
                    event_strs.append(f"Revealed: {evt.get('card','?')}")
                elif etype == "controller_changed":
                    event_strs.append(f"{evt.get('card','?')} changed controller")
            if event_strs:
                lines.append(f"Recent: {'; '.join(event_strs)}")

        # Revealed cards from opponent
        revealed = game_state.get("revealed_cards", {})
        if revealed and opp_seat is not None:
            opp_revealed = revealed.get(str(opp_seat), revealed.get(opp_seat, []))
            if opp_revealed:
                # opp_revealed is a list of grp_ids; try to resolve names from battlefield/graveyard
                lines.append(f"Opp revealed {len(opp_revealed)} card(s) this game")

        # OPTIMIZATION: Compact hand display
        hand = game_state.get("hand", [])
        lines.append("")
        lines.append(f"HAND:")

        # Pre-compute opponent battlefield subsets for removal target analysis
        opp_creatures = [
            c
            for c in opp_cards
            if "creature" in c.get("type_line", "").lower()
            and not self._is_impending(c)
        ]
        opp_nonland = [
            c for c in opp_cards
            if "land" not in c.get("type_line", "").lower()
        ]

        if hand:
            import re

            # OPTIMIZATION: Simplified mana checking - just need to know if castable
            can_play_land = (
                (lands_played == 0) and is_your_turn and is_main_phase and stack_empty
            )

            # Disambiguate duplicate card names in hand
            hand_name_counts = Counter(c.get("name", "Unknown") for c in hand)
            hand_name_seen = {}

            for card in hand:
                name = card.get("name", "Unknown")
                cost = card.get("mana_cost", "")
                type_line = card.get("type_line", "").lower()
                oracle_text = card.get("oracle_text", "")
                oracle_lower = oracle_text.lower()

                # OPTIMIZATION: Simplified timing - just need instant vs sorcery
                is_instant = "instant" in type_line or "flash" in oracle_lower
                timing = "I" if is_instant else "S"

                # OPTIMIZATION: Simplified CMC calculation
                cmc = 0
                reqs = {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0}
                if cost:
                    generic = re.findall(r"\{(\d+)\}", cost)
                    cmc += sum(int(g) for g in generic)
                    for color in "WUBRGC":
                        count = len(re.findall(rf"\{{{color}\}}", cost))
                        reqs[color] += count
                        cmc += count
                    hybrid = re.findall(r"\{[^}]+/[^}]+\}", cost)
                    cmc += len(hybrid)

                # OPTIMIZATION: Simple castability check
                # This logic is CORRECT: it checks total_mana (actual count) first, then colors.
                # For dual lands: Temple of Mystery counts as 1 total_mana, but mana_pool shows
                # both U:1 and G:1 since it CAN produce either. The color check ensures you can
                # pay colored requirements, while total_mana >= cmc ensures you have enough sources.
                castable = ""
                if "land" in type_line:
                    castable = "LAND" if can_play_land else "HOLD"
                elif total_mana >= cmc:
                    # Color check: verify we can meet colored requirements
                    # Note: mana_pool[color] counts sources that CAN produce that color
                    # For dual lands, this may overcount, but we check total_mana first to ensure
                    # we have enough actual mana sources, then verify color requirements can be met.
                    color_ok = all(
                        mana_pool.get(c, 0) + mana_pool.get("Any", 0) >= reqs[c]
                        for c in "WUBRGC"
                        if reqs[c] > 0
                    )
                    if color_ok:
                        castable = "OK"
                    else:
                        # Missing specific color — show which colors are needed
                        missing_pips = ""
                        for c in "WUBRGC":
                            if reqs[c] > 0:
                                shortfall = reqs[c] - mana_pool.get(c, 0) - mana_pool.get("Any", 0)
                                if shortfall > 0:
                                    missing_pips += f"{{{c}}}" * shortfall
                        castable = f"NEED:{missing_pips}" if missing_pips else f"NEED:{max(1, cmc - total_mana)}"
                else:
                    # Not enough total mana — also show color requirements so
                    # the LLM knows WHICH colors are needed (e.g. NEED:2+{G}{G}
                    # means 2 more mana including 2 green specifically)
                    missing_pips = ""
                    for c in "WUBRGC":
                        if reqs[c] > 0:
                            shortfall = reqs[c] - mana_pool.get(c, 0) - mana_pool.get("Any", 0)
                            if shortfall > 0:
                                missing_pips += f"{{{c}}}" * shortfall
                    generic_short = cmc - total_mana
                    if missing_pips:
                        castable = f"NEED:{generic_short}+{missing_pips}"
                    else:
                        castable = f"NEED:{generic_short}"

                # OPTIMIZATION: Terse removal analysis - show kill RANGE, not every target
                removal_info = ""
                damage_match = re.search(r"deals?\s+(\d+)\s+damage", oracle_lower)
                minus_match = re.search(r"gets?\s+(-\d+)/(-\d+)", oracle_lower)

                is_destroy_creature = "destroy target creature" in oracle_lower
                is_exile_creature = "exile target creature" in oracle_lower

                # Enhanced removal detection (includes artifacts/enchantments/permanents)
                is_destroy_permanent = (
                    "destroy target permanent" in oracle_lower
                    or "destroy target nonland permanent" in oracle_lower
                )
                is_destroy_art_ench = (
                    "destroy target artifact" in oracle_lower
                    or "destroy target enchantment" in oracle_lower
                    or "naturalize" in oracle_lower
                )
                is_exile_permanent = (
                    "exile target permanent" in oracle_lower
                    or "exile target nonland permanent" in oracle_lower
                    or "exile target artifact" in oracle_lower
                    or "exile target enchantment" in oracle_lower
                )

                if (
                    damage_match
                    or minus_match
                    or is_destroy_creature
                    or is_exile_creature
                    or is_destroy_permanent
                    or is_destroy_art_ench
                    or is_exile_permanent
                ):
                    if is_destroy_permanent or is_exile_permanent:
                        removal_info = " [RM:perm]"
                    elif is_destroy_art_ench:
                        removal_info = " [RM:art/ench]"
                    elif is_destroy_creature or is_exile_creature:
                        removal_info = " [RM:creat]"
                    elif damage_match:
                        dmg = int(damage_match.group(1))
                        removal_info = f" [RM:<={dmg}T]"
                    elif minus_match:
                        tough_reduction = abs(int(minus_match.group(2)))
                        removal_info = f" [RM:<={tough_reduction}T]"

                    # Target availability check: warn if no valid targets exist
                    targets_nonland = "nonland" in oracle_lower
                    targets_creature = is_destroy_creature or is_exile_creature
                    targets_art_ench = is_destroy_art_ench
                    # Check MV restriction (e.g., "mana value 2 or less")
                    mv_match = re.search(r"mana value (\d+) or less", oracle_lower)
                    mv_limit = int(mv_match.group(1)) if mv_match else None

                    # Determine the valid target pool
                    if targets_creature:
                        target_pool = opp_creatures
                    elif targets_nonland or is_destroy_permanent or is_exile_permanent:
                        target_pool = opp_nonland
                    elif targets_art_ench:
                        target_pool = [
                            c for c in opp_nonland
                            if any(t in c.get("type_line", "").lower()
                                   for t in ["artifact", "enchantment"])
                        ]
                    else:
                        target_pool = opp_creatures  # damage/minus targets creatures

                    # Apply MV filter if present
                    if mv_limit is not None and target_pool:
                        target_pool = [
                            c for c in target_pool
                            if self._get_cmc(c.get("mana_cost", "")) <= mv_limit
                        ]

                    if not target_pool:
                        removal_info += " [NO TARGETS]"

                # OPTIMIZATION: Only show oracle text for non-basic, non-land cards with relevant text
                is_basic_land = "land" in type_line and (
                    "basic" in type_line
                    or name in ["Plains", "Island", "Swamp", "Mountain", "Forest"]
                )
                is_aura = "enchantment" in type_line and "aura" in type_line
                # Auras ALWAYS show oracle text — targeting (own vs opponent) depends on effect
                # Check length AFTER removing reminder text so cards with long keyword
                # reminders (e.g. Harmonize) aren't wrongly hidden
                oracle_stripped = self._remove_reminder_text(oracle_text) if oracle_text else ""
                show_oracle = (
                    oracle_text
                    and not is_basic_land
                    and (is_aura or len(oracle_stripped) < 200)
                )

                # Type tag for non-creature, non-land cards so LLM knows what it is
                type_tag = ""
                if "creature" not in type_line and "land" not in type_line:
                    if "enchantment" in type_line and "aura" in type_line:
                        type_tag = " (AURA)"
                    elif "enchantment" in type_line:
                        type_tag = " (ENCHANT)"
                    elif "equipment" in type_line:
                        type_tag = " (EQUIP)"
                    elif "artifact" in type_line:
                        type_tag = " (ART)"
                    elif "planeswalker" in type_line:
                        type_tag = " (PW)"

                # Disambiguate duplicate names in hand
                if hand_name_counts[name] > 1:
                    hand_name_seen[name] = hand_name_seen.get(name, 0) + 1
                    display_name = f"{name} #{hand_name_seen[name]}"
                else:
                    display_name = name

                # OPTIMIZATION: Compact card display
                lines.append(
                    f"  {display_name}{type_tag} {cost} [{timing},{castable}]{removal_info}"
                )
                if show_oracle:
                    # Use pre-stripped text (reminder text already removed above)
                    oracle_compact = oracle_stripped
                    if is_aura:
                        # Auras: show full text (targeting depends on knowing effect)
                        if len(oracle_compact) > 160:
                            oracle_compact = oracle_compact[:157] + "..."
                    elif len(oracle_compact) > 150:
                        oracle_compact = oracle_compact[:147] + "..."
                    lines.append(f"    {oracle_compact}")
        else:
            lines.append("  (empty)")

        # OPTIMIZATION: Compact stack display
        stack = game_state.get("stack", [])
        if stack:
            stack_items = []
            for card in stack:
                name = card.get("name", "Unknown")
                owner = "Y" if card.get("owner_seat_id") == local_seat else "O"
                stack_items.append(f"{owner}:{name}")
            lines.append(f"Stack: {' > '.join(stack_items)}")

        # OPTIMIZATION: Compact graveyard counts
        graveyard = game_state.get("graveyard", [])
        if graveyard:
            your_gy = len(
                [c for c in graveyard if c.get("owner_seat_id") == local_seat]
            )
            opp_gy = len([c for c in graveyard if c.get("owner_seat_id") != local_seat])
            if your_gy > 0 or opp_gy > 0:
                lines.append(f"GY: Y={your_gy} O={opp_gy}")

        # Command zone (Commander/Brawl)
        command = game_state.get("command", [])
        if command:
            cmd_names = [c.get("name", "Unknown") for c in command]
            lines.append(f"CMD: {', '.join(cmd_names)}")

        return "\n".join(lines)

    def _extract_card_name_words(self, game_state: dict[str, Any]) -> set[str]:
        """Extract all words from card names in the current game state.

        These words are excluded from overuse tracking since they're card names.
        """
        import re

        card_words: set[str] = set()

        # Collect card names from all zones
        for zone in ["battlefield", "hand", "graveyard", "stack", "exile", "command"]:
            for card in game_state.get(zone, []):
                name = card.get("name", "")
                # Extract words from card name
                words = re.findall(r"\b[a-z]+\b", name.lower())
                card_words.update(words)

        return card_words

    def get_advice(
        self,
        game_state: dict[str, Any],
        question: Optional[str] = None,
        trigger: Optional[str] = None,
        style: str = "concise",
    ) -> str:
        """Get coaching advice for the current game state.

        Args:
            game_state: Dict from get_game_state() MCP tool
            question: Optional user question to answer
            trigger: Optional trigger name (e.g., "combat_attackers", "low_life")
            style: Advice style ("concise" or "verbose")

        Returns:
            Advice string from the LLM
        """
        import time

        total_start = time.perf_counter()

        # Build context
        context_start = time.perf_counter()
        context = self._format_game_context(game_state)
        context_time = (time.perf_counter() - context_start) * 1000

        # Get card name words to exclude from overuse tracking
        card_words = self._extract_card_name_words(game_state)

        # Check for overused words to avoid (excluding card names)
        blacklisted = self._word_tracker.get_blacklisted(exclude_words=card_words)

        # Build dynamic system prompt
        system_prompt = self._system_prompt

        # Adjust for style
        if style == "verbose":
            system_prompt = system_prompt.replace(
                "Keep responses concise (2-3 sentences max)",
                "Provide detailed strategic reasoning (4-5 sentences)",
            )
            # Remove "Be direct... no 'consider'" constraint for verbose mode to allow more nuance
            system_prompt = system_prompt.replace(
                'Be direct and specific - tell the player exactly what to do, not what to "consider".',
                "Explain the 'why' behind your advice, discussing alternatives if relevant.",
            )

        if blacklisted:
            avoid_list = ", ".join(blacklisted)
            system_prompt += f"\n\nIMPORTANT: Avoid using these overused words: {avoid_list}. Use different phrasing."
            logger.debug(f"Blacklisted words: {blacklisted}")

        # PHASE 2: Inject decision-specific guidance when a decision is pending
        decision_context = game_state.get("decision_context")
        if decision_context:
            dec_type = decision_context.get("type", "unknown")
            decision_guidance = DECISION_PROMPTS.get(dec_type)
            if decision_guidance:
                system_prompt += f"\n\n{decision_guidance}"
                logger.debug(f"Injected decision prompt for type: {dec_type}")

        # Build user message
        if question:
            user_message = f"{context}\n\nThe player asks: {question}"
        elif trigger:
            trigger_descriptions = {
                "new_turn": "Your turn just started (Main 1). What is the ONE best play right now?",
                "opponent_turn": (
                    "Opponent's turn just started. Briefly analyze their board and strategy. "
                    "What is their game plan? What threats should we prepare for? "
                    "What should we do on our next turn to counter them? "
                    "Keep it to 2-3 sentences focused on opponent's strategy and your plan."
                ),
                "land_played": "A land was just played. What is the ONE next play?",
                "spell_resolved": "A spell just resolved. What is the ONE next play?",
                "priority_gained": "You have priority. Respond or pass?",
                "combat_attackers": "Combat: Declare attackers. Which creatures should attack? Default: attack with ALL eligible creatures unless you have a specific reason to hold one back (e.g., need a blocker to survive crackback).",
                "combat_blockers": "Combat: Opponent is attacking. How should you block?",
                "low_life": "Your life is dangerously low! What's the survival plan?",
                "opponent_low_life": "Opponent's life is low — can you finish them?",
                "stack_spell": "Something was just cast. Respond or let it resolve?",
                "stack_spell_yours": "Your spell is on the stack. Pass priority or hold?",
                "stack_spell_opponent": "Opponent just cast something. Respond or let it resolve?",
                "user_request": "Give quick strategic advice for this moment.",
                "decision_required": "Decision required (scry, discard, target, mulligan, etc). What should the player choose?",
                "threat_detected": "ALERT: A dangerous card just hit the battlefield!",
            }
            trigger_desc = trigger_descriptions.get(trigger, f"Trigger: {trigger}")
            user_message = f"{context}\n\n{trigger_desc}"
        else:
            user_message = f"{context}\n\nWhat's the best play right now?"

        # OPTIMIZATION: Log prompt size with token estimate
        prompt_chars = len(system_prompt) + len(user_message)
        prompt_tokens_est = self._estimate_tokens(system_prompt + user_message)
        context_lines = context.count("\n") + 1
        logger.info(
            f"[PROMPT] {context_lines} lines, {prompt_chars} chars, ~{prompt_tokens_est} tokens | context: {context_time:.1f}ms"
        )

        # Select system prompt based on style
        # Priority: explicit arg > object property > default
        selected_style = style if style else getattr(self, "advice_style", "concise")
        style_key = selected_style.lower()

        # Define style prompts (lazy loaded or defined here)
        prompts = {
            "concise": CONCISE_SYSTEM_PROMPT,
            "normal": DEFAULT_SYSTEM_PROMPT,
            "explain": DEFAULT_SYSTEM_PROMPT.replace(
                "Keep responses concise (2-3 sentences max)",
                "Explain your reasoning clearly but briefly.",
            )
            + "\nInclude a short explanation of WHY this is the best line.",
            "pirate": "You are a ruthless pirate captain coaching a swabby! Speak like a pirate! Yarr! Keep it short!",
        }

        effective_system_prompt = prompts.get(style_key, CONCISE_SYSTEM_PROMPT)

        # Inject deck strategy if available — instruct model to reference it
        if self._deck_strategy:
            effective_system_prompt += (
                f"\n\nDECK STRATEGY:\n{self._deck_strategy}"
                "\n\nConnect your advice to this strategy — briefly explain WHY a play "
                "matters for the deck's game plan (e.g. 'Cast X to trigger cascade into combo pieces')."
            )

        # Re-inject blacklisted words and decision guidance into effective prompt
        if blacklisted:
            avoid_list = ", ".join(blacklisted)
            effective_system_prompt += f"\n\nIMPORTANT: Avoid using these overused words: {avoid_list}. Use different phrasing."

        if decision_context:
            dec_type = decision_context.get("type", "unknown")
            decision_guidance = DECISION_PROMPTS.get(dec_type)
            if decision_guidance:
                effective_system_prompt += f"\n\n{decision_guidance}"

        # RAG: Inject relevant MTG rules for this situation
        try:
            if self._rules_db is None:
                from arenamcp.rules_db import RulesDB

                self._rules_db = RulesDB()
            rules = self._rules_db.get_rules_for_situation(game_state, trigger, limit=5)
            if rules:
                rules_lines = [f"- Rule {r['number']}: {r['text']}" for r in rules]
                effective_system_prompt += (
                    "\n\nRELEVANT MTG RULES (official — these override any conflicting assumptions):\n"
                    + "\n".join(rules_lines)
                )
                logger.debug(
                    f"Injected {len(rules)} rules: {[r['number'] for r in rules]}"
                )
        except Exception as e:
            logger.warning(f"Rules RAG error (non-fatal): {e}")

        # Get response with timeout to prevent hanging on slow models
        # Gemini CLI uses a subprocess which has startup overhead
        if isinstance(self._backend, GeminiCliBackend):
            api_timeout = 20
        elif isinstance(self._backend, ProxyBackend) and getattr(self._backend, '_api_key', None) == "ollama":
            api_timeout = 45  # Local Ollama models need much more time
        elif isinstance(self._backend, ProxyBackend):
            api_timeout = 15  # Proxy adds ~1-2s overhead to upstream calls
        else:
            api_timeout = 12
        api_start = time.perf_counter()
        import concurrent.futures

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(
            self._backend.complete, effective_system_prompt, user_message
        )
        try:
            response = future.result(timeout=api_timeout)
        except concurrent.futures.TimeoutError:
            is_ollama = isinstance(self._backend, ProxyBackend) and getattr(self._backend, '_api_key', None) == "ollama"
            hint = " — try a smaller model (e.g. llama3.2:1b) or use a cloud backend" if is_ollama else ""
            logger.warning(
                f"LLM API call timed out after {api_timeout}s (model may be too slow for real-time coaching){hint}"
            )
            response = ""
        # Don't wait for thread completion — shutdown(wait=True) would block
        # until the backend call finishes, defeating the timeout entirely.
        # The backend's own timeout will clean up the subprocess.
        executor.shutdown(wait=False)
        api_time = (time.perf_counter() - api_start) * 1000

        # POST-PROCESSING: Validate and fix common LLM issues (especially for smaller models)
        response = self._postprocess_advice(response, game_state)

        self._word_tracker.record(response, exclude_words=card_words)

        total_time = (time.perf_counter() - total_start) * 1000
        logger.info(
            f"[TIMING] API call: {api_time:.0f}ms, total: {total_time:.0f}ms, response: {len(response)} chars"
        )

        return response

    def get_win_plan(
        self,
        game_state: dict[str, Any],
        turns: int,
        library_summary: str = "",
        backend=None,
    ) -> str:
        """Get a multi-turn strategic plan for winning in N turns.

        Args:
            game_state: Dict from get_game_state() MCP tool
            turns: Number of turns to plan for (2-8)
            library_summary: Compact summary of remaining library cards
            backend: Optional separate backend instance (e.g. thinking-enabled).
                     If provided, used instead of self._backend.

        Returns:
            Strategic plan string from the LLM
        """
        import time
        import concurrent.futures

        total_start = time.perf_counter()
        be = backend or self._backend

        # Build context (reuse existing formatter)
        context = self._format_game_context(game_state)

        # Build system prompt with turn count injected
        system_prompt = WIN_PLAN_PROMPT.format(n=turns)

        # Inject deck strategy if available
        if self._deck_strategy:
            system_prompt += (
                f"\n\nDECK STRATEGY:\n{self._deck_strategy}"
                "\n\nAlign the plan with this deck's win conditions and play patterns."
            )

        # Build user message with game context and library
        user_message = context
        if library_summary:
            user_message += f"\n\nLIBRARY REMAINING:\n{library_summary}"
        user_message += f"\n\nCreate a plan to win in exactly {turns} turns."

        # Longer timeout for strategic plans (more tokens to generate).
        # Thinking-enabled backends need extra time for reasoning.
        is_thinking = isinstance(be, ProxyBackend) and be.enable_thinking
        if isinstance(be, GeminiCliBackend):
            api_timeout = 45
        elif is_thinking:
            api_timeout = 90
        elif isinstance(be, ProxyBackend) and getattr(be, '_api_key', None) == "ollama":
            api_timeout = 90  # Local Ollama models need much more time
        elif isinstance(be, ProxyBackend):
            api_timeout = 45
        else:
            api_timeout = 35

        api_start = time.perf_counter()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        # Win plans need more tokens than standard advice (400).
        # Only ProxyBackend supports the max_tokens kwarg.
        if isinstance(be, ProxyBackend):
            future = executor.submit(
                be.complete, system_prompt, user_message, 1200
            )
        else:
            future = executor.submit(
                be.complete, system_prompt, user_message
            )
        try:
            response = future.result(timeout=api_timeout)
        except concurrent.futures.TimeoutError:
            logger.warning(
                f"Win plan API call timed out after {api_timeout}s"
            )
            response = ""
        executor.shutdown(wait=False)
        api_time = (time.perf_counter() - api_start) * 1000

        total_time = (time.perf_counter() - total_start) * 1000
        logger.info(
            f"[TIMING] Win plan API: {api_time:.0f}ms, total: {total_time:.0f}ms, "
            f"turns={turns}, response: {len(response)} chars"
        )

        return response

    def _postprocess_advice(self, advice: str, game_state: dict[str, Any]) -> str:
        """Post-process LLM advice to fix common issues with smaller models.

        1. Remove 'Play [Land]' suggestions when no land is in hand
        2. Fix typos in card names using fuzzy matching
        """
        if not advice:
            return ""

        import re

        def _combat_attack_summary() -> Optional[tuple[int, int, int]]:
            """Return (attack_power, opp_life, opp_blockers) if computable."""
            turn = game_state.get("turn", {})
            turn_num = turn.get("turn_number", 0)
            phase = turn.get("phase", "")

            players = game_state.get("players", [])
            local_player = next((p for p in players if p.get("is_local")), None)
            if not local_player:
                return None
            local_seat = local_player.get("seat_id")
            opponent_player = next(
                (p for p in players if p.get("seat_id") != local_seat), None
            )
            if not opponent_player:
                return None

            if turn.get("active_player") != local_seat:
                return None
            if "Main" not in phase and "Combat" not in phase:
                return None

            battlefield = game_state.get("battlefield", [])
            your_creatures = [
                c
                for c in battlefield
                if c.get("owner_seat_id") == local_seat
                and "creature" in c.get("type_line", "").lower()
                and not self._is_impending(c)
            ]

            def _has_haste(card: dict[str, Any]) -> bool:
                return (
                    "haste"
                    in self._remove_reminder_text(card.get("oracle_text", "")).lower()
                )

            valid_attackers = [
                c
                for c in your_creatures
                if not c.get("is_tapped")
                and not (
                    c.get("turn_entered_battlefield") == turn_num and not _has_haste(c)
                )
            ]
            attack_power = sum(c.get("power") or 0 for c in valid_attackers)

            opp_creatures = [
                c
                for c in battlefield
                if c.get("owner_seat_id") != local_seat
                and "creature" in c.get("type_line", "").lower()
                and not self._is_impending(c)
            ]
            opp_blockers = len([c for c in opp_creatures if not c.get("is_tapped")])
            opp_life = opponent_player.get("life_total", 20)

            return attack_power, opp_life, opp_blockers

        # Get cards in hand
        hand_cards = game_state.get("hand", [])
        hand_names = {c.get("name", "").lower() for c in hand_cards}

        # Get all card names in game state for fuzzy matching
        all_cards = []
        for zone in ["hand", "battlefield", "graveyard", "stack", "exile"]:
            all_cards.extend(game_state.get(zone, []))
        all_card_names = {c.get("name", "") for c in all_cards if c.get("name")}

        # Check for land names in hand
        land_types = {"forest", "island", "swamp", "mountain", "plains"}
        lands_in_hand = {
            name for name in hand_names if any(lt in name for lt in land_types)
        }

        # 1. Remove "Play [Land]" if no land in hand
        if not lands_in_hand:
            # Remove patterns like "Play Forest.", "Play Island,", "Play a land."
            advice = re.sub(
                r"Play\s+(Forest|Island|Swamp|Mountain|Plains|a land)[.,]?\s*",
                "",
                advice,
                flags=re.IGNORECASE,
            )
            # Clean up any resulting double spaces or leading/trailing spaces
            advice = re.sub(r"\s+", " ", advice).strip()

        # 2. Fix typos in card names using simple fuzzy matching
        # Common typos seen from Gemma 3N:
        typo_fixes = {
            "brerak out": "Break Out",
            "braimble familiar": "Bramble Familiar",
            "llanowar eves": "Llanowar Elves",
            "llanowar elfs": "Llanowar Elves",
            "craterhood behemoth": "Craterhoof Behemoth",
            "creterhoof behemoth": "Craterhoof Behemoth",
            "crterhoof behemoth": "Craterhoof Behemoth",
            "baadgermole cub": "Badgermole Cub",
            "badgremole cub": "Badgermole Cub",
        }

        advice_lower = advice.lower()
        for typo, correct in typo_fixes.items():
            if typo in advice_lower:
                # Case-insensitive replacement
                pattern = re.compile(re.escape(typo), re.IGNORECASE)
                advice = pattern.sub(correct, advice)

        # Also try to match against actual card names in game state
        # Split advice into words and check for near-matches
        for card_name in all_card_names:
            if len(card_name) < 4:
                continue  # Skip short names to avoid false matches
            # Check if card name appears with typos (simple Levenshtein-like check)
            card_words = card_name.lower().split()
            for word in card_words:
                if len(word) < 4:
                    continue
                # Look for similar words in advice
                advice_words = advice.lower().split()
                for i, advice_word in enumerate(advice_words):
                    if len(advice_word) >= 4 and self._is_similar(word, advice_word):
                        # Replace the typo with correct spelling
                        # Find the actual word in original advice and replace
                        original_words = advice.split()
                        if i < len(original_words):
                            # Only replace if first letter matches (to avoid false positives)
                            if original_words[i][0].lower() == word[0].lower():
                                original_words[i] = (
                                    word.capitalize()
                                    if original_words[i][0].isupper()
                                    else word
                                )
                                advice = " ".join(original_words)

        # 3. Remove Cast suggestions for cards that cost more mana than available
        # Calculate available mana (lands on battlefield + land drop potential)
        battlefield = game_state.get("battlefield", [])
        local_seat = None
        for p in game_state.get("players", []):
            if p.get("is_local"):
                local_seat = p.get("seat_id")
                break

        # Count untapped lands we control
        untapped_lands = 0
        for card in battlefield:
            if (
                card.get("controller_seat_id") == local_seat
                or card.get("owner_seat_id") == local_seat
            ):
                type_line = card.get("type_line", "").lower()
                if "land" in type_line and not card.get("is_tapped"):
                    untapped_lands += 1

        # Check if we have a land in hand (potential +1 mana)
        has_land_in_hand = lands_in_hand  # already computed above
        potential_mana = untapped_lands + (1 if has_land_in_hand else 0)

        # Check each card in hand for mana cost violations
        seen_card_names = set()
        for card in hand_cards:
            card_name = card.get("name", "")
            mana_cost = card.get("mana_cost", "")
            if card_name in seen_card_names:
                continue
            seen_card_names.add(card_name)
            if not card_name or not mana_cost:
                continue

            # Parse CMC from mana cost (simple heuristic)
            cmc = 0
            import re as re_inner

            # Count {X} symbols
            symbols = re_inner.findall(r"\{([^}]+)\}", mana_cost)
            for sym in symbols:
                if sym.isdigit():
                    cmc += int(sym)
                elif sym in ["W", "U", "B", "R", "G", "C"]:
                    cmc += 1
                elif "/" in sym:  # Hybrid like {R/G}
                    cmc += 1

            # If this card costs more than we can have, remove Cast suggestions for it
            if cmc > potential_mana:
                # Remove "Cast [Card Name]" as a standalone command (e.g. "Cast X." or "Cast X,")
                # but NOT when the card name appears mid-sentence (e.g. "find mana to cast X or Y")
                # to avoid leaving garbled text like "find mana to or Y"
                standalone_pattern = re.compile(
                    rf"(?:^|(?<=\.\s)|(?<=\n))Cast\s+{re.escape(card_name)}[.,]?\s*",
                    re.IGNORECASE,
                )
                if standalone_pattern.search(advice):
                    advice = standalone_pattern.sub("", advice)
                    logger.debug(
                        f"Removed uncastable suggestion: {card_name} (needs {cmc}, have {potential_mana})"
                    )
                else:
                    # Card mentioned mid-sentence — replace name with "[uncastable]" hint
                    # so the sentence stays grammatical
                    mid_pattern = re.compile(
                        rf"(?:cast\s+)?{re.escape(card_name)}", re.IGNORECASE
                    )
                    if mid_pattern.search(advice):
                        advice = mid_pattern.sub(
                            f"{card_name} (not enough mana)", advice, count=1
                        )
                        logger.debug(
                            f"Annotated uncastable mid-sentence: {card_name} (needs {cmc}, have {potential_mana})"
                        )

        # 4. Remove incorrect lethal/win claims when math doesn't support it
        if re.search(
            r"(?i)\blethal\b|\bfor the win\b|\bthat'?s the win\b|\bwin!\b", advice
        ):
            summary = _combat_attack_summary()
            if summary:
                attack_power, opp_life, opp_blockers = summary
                if opp_blockers > 0 or attack_power < opp_life:
                    advice = re.sub(r"(?i)\blethal\b", "damage", advice)
                    advice = re.sub(r"(?i)\bfor the win\b", "for damage", advice)
                    advice = re.sub(r"(?i)\bthat'?s the win\b", "", advice)
                    advice = re.sub(r"(?i)\bwin!\b", "", advice)
                    advice = advice.replace("lethal on board", "pressure on board")

        # Clean up double spaces
        advice = re.sub(r"\s+", " ", advice).strip()

        # 5. Enforce Legal actions only (hard filter)
        # MULLIGAN OVERRIDE: During mulligan, RulesEngine returns "Wait (Opponent
        # has priority)" because priority_player != local_seat. Override here just
        # like _format_game_context does (line ~1384).
        pending = game_state.get("pending_decision")
        if pending == "Mulligan":
            legal_actions = ["KEEP", "MULLIGAN"]
        elif pending == "Mulligan Bottom":
            # During bottom-card selection, any card name advice is valid
            legal_actions = []
        else:
            try:
                from arenamcp.rules_engine import RulesEngine

                legal_actions = RulesEngine.get_legal_actions(game_state) or []
            except Exception as e:
                logger.warning(f"RulesEngine error in postprocess: {e}")
                legal_actions = []

        if legal_actions:

            def _score_action(action: str) -> int:
                """Heuristic score for legal actions (higher is better)."""
                score = 0
                act = action.lower()
                turn = game_state.get("turn", {})
                phase = turn.get("phase", "")
                step = turn.get("step", "")
                players = game_state.get("players", [])
                local_player = next((p for p in players if p.get("is_local")), None)
                local_seat = local_player.get("seat_id") if local_player else None

                # Prefer land drop if available
                if act.startswith("play land:"):
                    score += 80

                # Combat step priorities
                if (
                    "declare attackers" in act
                    and "combat" in phase
                    and "declareattack" in step
                ):
                    score += 90
                if "block with" in act and "combat" in phase and "declareblock" in step:
                    score += 90

                # Casting is generally higher priority than activating
                if act.startswith("cast "):
                    score += 60
                if act.startswith("activate "):
                    score += 40

                # Penalize "wait/pass" actions if anything else exists
                if "wait" in act or "pass priority" in act:
                    score -= 50

                # If we can detect a legal "Play Land" and lands available, boost it
                if "play land" in act and local_seat is not None:
                    # If a land is in hand, it's likely valid to play
                    hand = game_state.get("hand", [])
                    if any("land" in c.get("type_line", "").lower() for c in hand):
                        score += 15

                return score

            advice_lower = advice.lower()
            legal_lower = [a.lower() for a in legal_actions]
            matches = any(l in advice_lower for l in legal_lower)

            # "Don't attack", "don't block", "pass priority", "no attacks" are
            # always valid strategic choices — the player can decline to act.
            PASSTHROUGH_PHRASES = [
                "don't attack", "don\u2019t attack", "do not attack", "no attack",
                "don't block", "don\u2019t block", "do not block", "no block",
                "pass priority", "take the damage",
            ]
            if not matches and any(p in advice_lower for p in PASSTHROUGH_PHRASES):
                matches = True

            # Special-case "Play <land>" suggestions to match "Play Land: <land>"
            if not matches and advice_lower.startswith("play "):
                for act in legal_actions:
                    if act.lower().startswith("play land:"):
                        matches = True
                        break

            # Special-case "Attack with X" to match "Declare Attackers: X, Y, ..."
            # LLMs frequently say "attack with" instead of "declare attackers"
            if not matches and "attack" in advice_lower:
                for act in legal_actions:
                    act_lower = act.lower()
                    if act_lower.startswith("declare attackers:"):
                        # Extract creature names from the legal action
                        names = [n.strip() for n in act_lower.split(":", 1)[1].split(",")]
                        if any(name in advice_lower for name in names):
                            matches = True
                            break

            # Special-case "Block X with Y" / "Block with Y" to match "Block with: X, Y, ..."
            if not matches and "block" in advice_lower:
                for act in legal_actions:
                    act_lower = act.lower()
                    if act_lower.startswith("block with:"):
                        names = [n.strip() for n in act_lower.split(":", 1)[1].split(",")]
                        if any(name in advice_lower for name in names):
                            matches = True
                            break

            if not matches:
                # Force to best legal action to avoid illegal recommendations
                best = max(legal_actions, key=_score_action)
                logger.info(f"Replaced illegal advice with legal action: {best} (original: {advice[:80]})")
                advice = best
        else:
            # If no legal actions, instruct pass priority explicitly
            advice = "pass priority"

        # Clean up internal action format for spoken output:
        # "Play Land: Plains" → "Play Plains"
        advice = re.sub(r"(?i)^Play Land:\s*", "Play ", advice)
        advice = re.sub(r"(?i)Play Land:\s*", "Play ", advice)

        return advice

    def _is_similar(self, a: str, b: str, threshold: float = 0.7) -> bool:
        """Check if two strings are similar using simple character overlap."""
        if a == b:
            return True
        if abs(len(a) - len(b)) > 3:
            return False
        # Count matching characters
        matches = sum(1 for c1, c2 in zip(a.lower(), b.lower()) if c1 == c2)
        similarity = matches / max(len(a), len(b))
        return similarity >= threshold

    def complete_with_image(
        self, system_prompt: str, user_message: str, image_data: bytes
    ) -> str:
        """Call complete_with_image on backend if supported."""
        if hasattr(self._backend, "complete_with_image"):
            return self._backend.complete_with_image(
                system_prompt, user_message, image_data
            )
        logger.error(
            f"Backend {type(self._backend).__name__} does not support complete_with_image"
        )
        return "Image analysis not supported by current backend."


class GameStateTrigger:
    """Detects trigger conditions by comparing game states."""

    # Tier list of dangerous cards that warrant immediate warning
    # Format: card_name -> brief description of the threat
    THREAT_CARDS = {
        # Board wipes
        "Wrath of God": "Board wipe! Destroys all creatures.",
        "Damnation": "Board wipe! Destroys all creatures.",
        "Farewell": "Exiles ALL permanents of chosen types!",
        "Sunfall": "Exiles all creatures, makes a big token.",
        "Depopulate": "Board wipe, draws if you have multicolor.",
        "Temporary Lockdown": "Exiles all permanents MV 2 or less!",
        "Meticulous Archive": "Can find board wipes or removal.",
        # Combo pieces / Must-answer threats
        "Sheoldred, the Apocalypse": "Drains 2 on your draws, heals on theirs!",
        "Atraxa, Grand Unifier": "Draws 10+ cards on ETB, lifelink flyer.",
        "Raffine, Scheming Seer": "Grows attackers and filters cards.",
        "The Wandering Emperor": "Flash! Can exile or make blockers anytime.",
        "Teferi, Time Raveler": "Shuts off your instant-speed plays!",
        "Narset, Parter of Veils": "You can only draw 1 card per turn!",
        "Omnath, Locus of Creation": "Massive value engine, gains life.",
        "Vorinclex, Voice of Hunger": "Doubles their counters, halves yours.",
        # Powerful planeswalkers
        "Oko, Thief of Crowns": "Elks your best creatures!",
        "Karn, the Great Creator": "Shuts off artifacts, grabs from sideboard.",
        "Wrenn and Six": "Recurring lands and pinging creatures.",
        # Lock pieces
        "Drannith Magistrate": "You can't cast from graveyard/exile!",
        "Archon of Emeria": "Only 1 spell per turn, lands ETB tapped.",
        "Thalia, Guardian of Thraben": "Noncreature spells cost 1 more.",
        "Authority of the Consuls": "Your creatures ETB tapped.",
        "High Noon": "Only 1 spell per turn for everyone.",
        # Removal magnets
        "Questing Beast": "Can't be chumped, damages walkers!",
        "Elder Gargaroth": "Massive value every combat.",
        "Cruelty of Gix": "3-mode saga, steals creatures!",
        # Enchantment threats
        "Monument to Endurance": "Grows huge with counters, gains deathtouch + indestructible!",
    }

    def __init__(self, life_threshold: int = 5):
        """Initialize trigger detector.

        Args:
            life_threshold: Life total below which "low_life" triggers (default: 5)
        """
        self.life_threshold = life_threshold
        # Track threats we've already warned about (by instance_id)
        self._seen_threats: set[int] = set()

    def _get_local_player(self, state: dict[str, Any]) -> Optional[dict]:
        """Get the local player dict from game state."""
        for p in state.get("players", []):
            if p.get("is_local"):
                return p
        return None

    def _get_opponent_player(self, state: dict[str, Any]) -> Optional[dict]:
        """Get the opponent player dict from game state."""
        for p in state.get("players", []):
            if not p.get("is_local"):
                return p
        return None

    def _has_castable_instants(self, state: dict[str, Any]) -> bool:
        """Check if player has any instant-speed cards they can cast.

        Returns True if hand contains instants or flash cards that can be
        cast with the current available mana.
        """
        import re

        # Count untapped lands for mana
        local_seat = None
        for p in state.get("players", []):
            if p.get("is_local"):
                local_seat = p.get("seat_id")
                break

        if local_seat is None:
            return False

        battlefield = state.get("battlefield", [])
        untapped_lands = sum(
            1
            for c in battlefield
            if c.get("owner_seat_id") == local_seat
            and "land" in c.get("type_line", "").lower()
            and not c.get("is_tapped")
        )

        # Check hand for castable instants/flash
        hand = state.get("hand", [])
        for card in hand:
            type_line = card.get("type_line", "").lower()
            oracle_text = card.get("oracle_text", "").lower()

            # Check if instant speed
            is_instant_speed = "instant" in type_line or "flash" in oracle_text
            if not is_instant_speed:
                continue

            # Calculate CMC
            cost = card.get("mana_cost", "")
            cmc = 0
            if cost:
                generic = re.findall(r"\{(\d+)\}", cost)
                cmc += sum(int(g) for g in generic)
                colored = re.findall(r"\{[WUBRGC]\}", cost)
                cmc += len(colored)
                hybrid = re.findall(r"\{[^}]+/[^}]+\}", cost)
                cmc += len(hybrid)

            if untapped_lands >= cmc:
                return True

        return False

    def check_triggers(
        self, prev_state: dict[str, Any], curr_state: dict[str, Any]
    ) -> list[str]:
        """Compare two game states and return triggered condition names.

        Args:
            prev_state: Previous game state dict
            curr_state: Current game state dict

        Returns:
            List of trigger names that fired (may be empty)
        """
        triggers = []

        prev_turn = prev_state.get("turn", {})
        curr_turn = curr_state.get("turn", {})

        # Retrieve phase and step early (fix scoping issues)
        curr_phase = curr_turn.get("phase", "")
        curr_step = curr_turn.get("step", "")

        # Get local player info first (needed for turn detection)
        prev_local = self._get_local_player(prev_state)
        curr_local = self._get_local_player(curr_state)
        local_seat = curr_local.get("seat_id") if curr_local else None

        # FIRST CONNECTION: If prev_state has no turn info but curr_state does,
        # we just connected mid-game. Fire a trigger to give immediate advice.
        prev_turn_num = prev_turn.get("turn_number", 0)
        curr_turn_num = curr_turn.get("turn_number", 0)
        curr_active = curr_turn.get("active_player", 0)

        if prev_turn_num == 0 and curr_turn_num > 0:
            # Just connected to an active game
            is_your_turn = curr_active == local_seat
            if is_your_turn:
                logger.info(
                    f"First connection mid-game, triggering new_turn (turn {curr_turn_num})"
                )
                triggers.append("new_turn")
            # Also check for pending decision on first connection
            pending = curr_state.get("pending_decision")
            if pending:
                logger.info(f"First connection with pending decision: {pending}")
                triggers.append("decision_required")

        # New turn detection
        if curr_turn_num > prev_turn_num:
            triggers.append("new_turn")

        # Check if it's your turn or opponent's turn
        is_your_turn = curr_active == local_seat

        # Priority gained - trigger when priority shifts to you
        prev_priority = prev_turn.get("priority_player", 0)
        curr_priority = curr_turn.get("priority_player", 0)
        if local_seat and curr_priority == local_seat and prev_priority != local_seat:
            # Always trigger on your turn
            # On opponent's turn, trigger if:
            #   1. You have castable instants
            #   2. There's something on the stack to consider
            #   3. We're in a significant phase (combat, main)
            has_options = self._has_castable_instants(curr_state)
            has_stack = len(curr_state.get("stack", [])) > 0
            # Retrieve phase and step early
            curr_phase = curr_turn.get("phase", "")
            curr_step = curr_turn.get("step", "")

            if (
                is_your_turn
                or has_options
                or has_stack
                or (any(p in curr_phase for p in ["Main", "Combat", "Beginning"]))
            ):
                triggers.append("priority_gained")

        # Check explicit pending decisions (like Mulligan) or legal action changes
        pending_decision = curr_state.get("pending_decision")
        legal_actions = curr_state.get("legal_actions", [])
        prev_legal = prev_state.get("legal_actions", [])
        
        # Trigger if decision label changed OR if we got a new list of legal actions from GRE
        if pending_decision and pending_decision != prev_state.get("pending_decision"):
            logger.info(f"Triggering decision: {pending_decision}")
            triggers.append("decision_required")
        elif legal_actions and legal_actions != prev_legal:
            logger.info(f"Triggering decision due to legal_actions update: {legal_actions}")
            if "decision_required" not in triggers:
                triggers.append("decision_required")
        elif pending_decision in ("Mulligan", "Mulligan Bottom"):
                # Mulligan re-fire: the hand zone may not be populated on
                # the first poll (SubmitDeckReq arrives before GameState).
                # Re-trigger once when the hand appears so the player gets
                # proper keep/mull advice even if the first fire was empty.
                prev_hand = prev_state.get("hand", [])
                curr_hand = curr_state.get("hand", [])
                if curr_hand and not prev_hand:
                    logger.info(
                        f"Re-triggering Mulligan decision (hand appeared: {len(curr_hand)} cards)"
                    )
                    triggers.append("decision_required")

        # Combat phase detection - use pending steps to catch fast combat phases
        pending_steps = curr_turn.get("pending_combat_steps", [])

        for step_info in pending_steps:
            step = step_info.get("step", "")
            step_active = step_info.get("active_player", 0)
            step_is_your_turn = step_active == local_seat

            logger.debug(
                f"Processing pending combat step: {step}, active={step_active}, step_is_your_turn={step_is_your_turn}, current_is_your_turn={is_your_turn}"
            )

            # Double-check both the step's active player AND current turn state
            # This prevents stale pending steps from firing triggers after turn changes
            if "DeclareAttack" in step and step_is_your_turn and is_your_turn:
                if "combat_attackers" not in triggers:
                    logger.info(f"Combat attackers trigger from pending: {step}")
                    triggers.append("combat_attackers")
            elif "DeclareBlock" in step and not step_is_your_turn and not is_your_turn:
                if "combat_blockers" not in triggers:
                    logger.info(f"Combat blockers trigger from pending: {step}")
                    triggers.append("combat_blockers")

        # Also check current step (in case we're still in combat)
        # curr_phase and curr_step are already defined above

        if "Combat" in curr_phase:
            prev_step = prev_turn.get("step", "")
            # Only trigger on STEP CHANGE to avoid spamming every polling cycle
            if curr_step != prev_step:
                if (
                    "DeclareAttack" in curr_step
                    and is_your_turn
                    and "combat_attackers" not in triggers
                ):
                    logger.info(f"Combat attackers trigger: step={curr_step}")
                    triggers.append("combat_attackers")
                elif (
                    "DeclareBlock" in curr_step
                    and not is_your_turn
                    and "combat_blockers" not in triggers
                ):
                    logger.info(f"Combat blockers trigger: step={curr_step}")
                    triggers.append("combat_blockers")

        # Low life detection - always important
        if curr_local:
            curr_life = curr_local.get("life_total", 20)
            prev_life = prev_local.get("life_total", 20) if prev_local else 20
            if curr_life < self.life_threshold and prev_life >= self.life_threshold:
                triggers.append("low_life")

        # Opponent low life detection - always important
        prev_opp = self._get_opponent_player(prev_state)
        curr_opp = self._get_opponent_player(curr_state)
        if curr_opp:
            curr_opp_life = curr_opp.get("life_total", 20)
            prev_opp_life = prev_opp.get("life_total", 20) if prev_opp else 20
            if (
                curr_opp_life < self.life_threshold
                and prev_opp_life >= self.life_threshold
            ):
                triggers.append("opponent_low_life")

        # Stack spell detection - differentiate between your spells and opponent's
        prev_stack = prev_state.get("stack", [])
        curr_stack = curr_state.get("stack", [])
        if len(curr_stack) > len(prev_stack):
            # Check who owns the newest spell on the stack
            newest_spell = curr_stack[-1] if curr_stack else None
            if newest_spell:
                spell_owner = newest_spell.get("owner_seat_id")
                if spell_owner == local_seat:
                    triggers.append("stack_spell_yours")
                else:
                    triggers.append("stack_spell_opponent")

        # Land played detection - only on your turn, only in main phases
        if is_your_turn and "Main" in curr_phase:
            prev_battlefield = prev_state.get("battlefield", [])
            curr_battlefield = curr_state.get("battlefield", [])

            # Count YOUR lands before and after
            prev_land_count = sum(
                1
                for obj in prev_battlefield
                if obj.get("owner_seat_id") == local_seat
                and "land" in obj.get("type_line", "").lower()
            )
            curr_land_count = sum(
                1
                for obj in curr_battlefield
                if obj.get("owner_seat_id") == local_seat
                and "land" in obj.get("type_line", "").lower()
            )

            if curr_land_count > prev_land_count:
                logger.info(
                    f"Land played trigger: {prev_land_count} -> {curr_land_count}"
                )
                triggers.append("land_played")

        # Spell resolved detection - your spell left the stack on your turn
        if is_your_turn and len(curr_stack) < len(prev_stack):
            # Check if a spell you owned just resolved
            prev_your_spells = [
                s for s in prev_stack if s.get("owner_seat_id") == local_seat
            ]
            curr_your_spells = [
                s for s in curr_stack if s.get("owner_seat_id") == local_seat
            ]
            if len(curr_your_spells) < len(prev_your_spells):
                # Your spell resolved - what's next?
                logger.info("Spell resolved trigger: your spell left the stack")
                triggers.append("spell_resolved")

            # Stack cleared on your main phase - opponent ability/spell resolved
            # leaving you with clean priority for sorcery-speed plays.
            # This covers upkeep/draw triggers, opponent ETBs, etc.
            if (
                len(curr_stack) == 0
                and "spell_resolved" not in triggers
                and "Main" in curr_phase
            ):
                logger.info(
                    "Stack cleared trigger: opponent spell/ability resolved on your main phase"
                )
                triggers.append("spell_resolved")

        # THREAT DETECTION - warn about dangerous opponent cards
        opp_seat = curr_opp.get("seat_id") if curr_opp else None
        if opp_seat:
            curr_battlefield = curr_state.get("battlefield", [])
            for card in curr_battlefield:
                # Only check opponent's permanents
                controller = card.get("controller_seat_id") or card.get("owner_seat_id")
                if controller != opp_seat:
                    continue

                instance_id = card.get("instance_id")
                card_name = card.get("name", "")

                # Check if this is a threat card we haven't warned about
                if (
                    card_name in self.THREAT_CARDS
                    and instance_id not in self._seen_threats
                ):
                    self._seen_threats.add(instance_id)
                    # Store threat info for the standalone coach to retrieve
                    self._last_threat = {
                        "name": card_name,
                        "warning": self.THREAT_CARDS[card_name],
                    }
                    logger.info(
                        f"Threat detected: {card_name} - {self.THREAT_CARDS[card_name]}"
                    )
                    triggers.append("threat_detected")

                # Generic planeswalker detection fallback
                elif (
                    card_name not in self.THREAT_CARDS
                    and "planeswalker" in card.get("type_line", "").lower()
                    and instance_id not in self._seen_threats
                ):
                    self._seen_threats.add(instance_id)
                    self._last_threat = {
                        "name": card_name,
                        "warning": f"Opponent played planeswalker {card_name} — generates value every turn, consider attacking it.",
                    }
                    logger.info(f"Threat detected (planeswalker): {card_name}")
                    triggers.append("threat_detected")

        return triggers
