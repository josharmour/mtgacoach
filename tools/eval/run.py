"""Replay captured coach prompts through one or more backends.

Reads a JSONL file produced by ``MTGACOACH_PROMPT_DUMP_PATH`` (or a hand-
seeded corpus). For every prompt × backend combination, calls
``ProxyBackend.complete`` and writes one row per response to a JSONL file.
The judge step (``judge.py``) reads the responses and scores them.

Usage:
    python -m tools.eval.run \\
        --prompts tools/eval/data/prompts.jsonl \\
        --responses tools/eval/data/responses.jsonl \\
        --backend online:deepseek-v4-flash \\
        --backend ollama:llama3.1:8b \\
        --backend ollama:qwen2.5:14b \\
        --license-key $MTGACOACH_LICENSE_KEY

Each ``--backend`` is one of:
    online:<model>          → routes through api.mtgacoach.com
    ollama:<model>          → http://localhost:11434/v1
    openai-compatible:<base_url>:<model>  → arbitrary OpenAI-compatible

Existing rows in --responses are preserved; only missing (prompt_id, backend)
pairs are added. Re-running with the same args is therefore idempotent and
safe to interrupt.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import traceback
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Make the in-repo src/ importable when running directly.
REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from arenamcp.backend_health import BACKEND_ERROR_PREFIX  # noqa: E402
from arenamcp.backends.proxy import ProxyBackend  # noqa: E402

logger = logging.getLogger("eval.run")


@dataclass
class BackendSpec:
    label: str  # human label used in CSV: "online:deepseek-v4-flash"
    model: str
    base_url: Optional[str]
    api_key: str

    @classmethod
    def parse(cls, spec: str, license_key: str = "") -> BackendSpec:
        if spec.startswith("online:"):
            model = spec.split(":", 1)[1]
            return cls(
                label=f"online:{model}",
                model=model,
                base_url=None,  # ProxyBackend.create_online uses ONLINE_BASE_URL
                api_key=license_key,
            )
        if spec.startswith("ollama:"):
            model = spec.split(":", 1)[1]
            return cls(
                label=f"ollama:{model}",
                model=model,
                base_url="http://localhost:11434/v1",
                api_key="ollama",
            )
        if spec.startswith("openai-compatible|"):
            # URL contains colons (http://host:port/v1) and model tags too
            # (llama3.1:8b), so use | as the field separator here.
            body = spec[len("openai-compatible|") :]
            try:
                base_url, model = body.split("|", 1)
            except ValueError:
                raise ValueError(
                    f"openai-compatible spec must be openai-compatible|<base_url>|<model>: {spec!r}"
                )
            return cls(
                label=f"openai-compat:{model}",
                model=model,
                base_url=base_url,
                api_key="any",
            )
        raise ValueError(f"unknown backend spec: {spec!r}")

    def build(self) -> ProxyBackend:
        if self.base_url is None:
            return ProxyBackend.create_online(model=self.model, license_key=self.api_key)
        return ProxyBackend(model=self.model, base_url=self.base_url, api_key=self.api_key)


def _read_jsonl(path: Path) -> Iterable[dict]:
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning(f"skipping malformed line in {path.name}: {e}")


import threading
from concurrent.futures import ThreadPoolExecutor

_append_lock = threading.Lock()


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _append_lock, open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _stable_prompt_id(prompt: dict, fallback_index: int) -> str:
    """Use the explicit id if present, otherwise a hash-based one."""
    pid = prompt.get("id")
    if pid:
        return str(pid)
    import hashlib

    h = hashlib.sha1(
        ((prompt.get("system") or "") + "|" + (prompt.get("user") or "")).encode("utf-8")
    ).hexdigest()[:10]
    return f"auto-{fallback_index:03d}-{h}"


def _existing_keys(responses_path: Path) -> set[tuple[str, str]]:
    """Return (prompt_id, backend) pairs already in the responses file."""
    keys: set[tuple[str, str]] = set()
    for r in _read_jsonl(responses_path):
        pid = r.get("prompt_id")
        be = r.get("backend")
        if pid and be:
            keys.add((str(pid), str(be)))
    return keys


def run(
    prompts_path: Path,
    responses_path: Path,
    backends: list[BackendSpec],
    limit: Optional[int] = None,
    timeout_s: float = 60.0,
    max_tokens: Optional[int] = None,
    concurrency: int = 16,
) -> None:
    prompts = list(_read_jsonl(prompts_path))
    if limit:
        prompts = prompts[:limit]
    if not prompts:
        logger.error(f"no prompts found in {prompts_path}")
        sys.exit(1)

    done = _existing_keys(responses_path)
    logger.info(
        f"prompts={len(prompts)} backends={[b.label for b in backends]} "
        f"already-recorded={len(done)} concurrency={concurrency}"
    )

    for be in backends:
        client = be.build()
        client._get_client()  # thread-safe pre-initialization

        # `be`/`client` are bound as default arguments (not captured by
        # closure) so each loop iteration gets its own binding — otherwise the
        # threads started for backend N would see backend N+1's client as soon
        # as the loop advanced (ruff B023).
        def _process_one(item: tuple[int, dict], be: BackendSpec = be, client=client) -> None:
            idx, prompt = item
            pid = _stable_prompt_id(prompt, idx)
            system = prompt.get("system") or ""
            user = prompt.get("user") or ""
            if not user:
                logger.warning(f"prompt {pid} has empty user message — skipping")
                return

            key = (pid, be.label)
            if key in done:
                return

            t0 = time.perf_counter()
            try:
                content = client.complete(
                    system,
                    user,
                    max_tokens=int(max_tokens or prompt.get("max_tokens") or 400),
                    temperature=float(prompt["temperature"])
                    if prompt.get("temperature") is not None
                    else 0.0,
                    request_timeout_s=timeout_s,
                )
                err = None
                # ProxyBackend.complete() defaults to raise_on_error=False, so
                # an API failure comes back as the prose sentinel
                # "[BACKEND ERROR] ..." rather than an exception. Left
                # unchecked it is recorded with error=None and the gate scores
                # a server outage as a WRONG ANSWER — silently, in every
                # response set. Hit for real on 2026-07-26 when one 32.4k-token
                # prompt exceeded max_model_len and vLLM returned HTTP 400.
                if isinstance(content, str) and content.startswith(BACKEND_ERROR_PREFIX):
                    err = content.strip()
                    content = ""
                    logger.warning(f"{pid} {be.label} backend error: {err}")
            except Exception as e:
                content = ""
                err = f"{type(e).__name__}: {e}"
                logger.warning(f"{pid} {be.label} failed: {err}")
                logger.debug(traceback.format_exc())
            latency_ms = (time.perf_counter() - t0) * 1000

            temp_val = float(prompt["temperature"]) if prompt.get("temperature") is not None else 0.0
            record = {
                "prompt_id": pid,
                "backend": be.label,
                "model": be.model,
                "response": content or "",
                "error": err,
                "latency_ms": round(latency_ms, 1),
                "response_chars": len(content or ""),
                "temperature": temp_val,
                "decoding_params": {"temperature": temp_val},
                "ts": time.time(),
            }
            _append_jsonl(responses_path, record)
            logger.info(f"{pid} {be.label}: {latency_ms:.0f}ms, {len(content or '')} chars")

        items_to_process = [
            (idx, prompt)
            for idx, prompt in enumerate(prompts)
            if (_stable_prompt_id(prompt, idx), be.label) not in done
        ]

        if concurrency > 1:
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                list(executor.map(_process_one, items_to_process))
        else:
            for item in items_to_process:
                _process_one(item)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument(
        "--prompts", required=True, type=Path, help="JSONL of {system, user, ...} records to replay"
    )
    parser.add_argument(
        "--responses", required=True, type=Path, help="JSONL output (appended; safe to re-run)"
    )
    parser.add_argument(
        "--backend",
        action="append",
        required=True,
        help="Backend spec (repeatable): online:<model> | ollama:<model> | openai-compatible:<base_url>:<model>",
    )
    parser.add_argument("--license-key", default=os.environ.get("MTGACOACH_LICENSE_KEY", ""))
    parser.add_argument("--limit", type=int, help="Only run the first N prompts")
    parser.add_argument("--timeout-s", type=float, default=60.0)
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="Override per-prompt max_tokens.",
    )
    parser.add_argument("--concurrency", type=int, default=16, help="Max concurrent requests (default: 16)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    backends = [BackendSpec.parse(b, license_key=args.license_key) for b in args.backend]
    run(
        args.prompts,
        args.responses,
        backends,
        limit=args.limit,
        timeout_s=args.timeout_s,
        max_tokens=args.max_tokens,
        concurrency=args.concurrency,
    )


if __name__ == "__main__":
    main()
