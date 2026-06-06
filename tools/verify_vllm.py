"""Round-trip verification for the local LLM endpoint.

Usage:
    python -m tools.verify_vllm
    python -m tools.verify_vllm --url http://localhost:8000/v1 --model gemma4:e2b

Hits /v1/models, then runs a tiny streamed chat completion. Prints the
chosen model, the raw streamed text, and a non-streamed response object so
you can confirm the `model` field round-trips. Used to validate the vLLM
migration; also works against Ollama / LM Studio / any OpenAI-compatible
server when the URL is overridden.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def list_models(base_url: str, timeout: float = 5.0) -> list[str]:
    req = urllib.request.Request(f"{base_url.rstrip('/')}/models", method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    return [m.get("id") for m in data.get("data", []) if m.get("id")]


def chat_round_trip(base_url: str, model: str, api_key: str = "vllm") -> None:
    from openai import OpenAI

    client = OpenAI(base_url=base_url, api_key=api_key, timeout=30.0)

    print(f"\n>>> streamed chat completion (model={model})")
    start = time.perf_counter()
    chunks: list[str] = []
    reasoning_chunks: list[str] = []
    stream = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You are a curt MTG coach."},
            {"role": "user", "content": "Reply with the single word: ready"},
        ],
        max_completion_tokens=150,
        temperature=0.0,
        stream=True,
    )
    last_model_seen: str | None = None
    for chunk in stream:
        if getattr(chunk, "model", None):
            last_model_seen = chunk.model
        if chunk.choices and chunk.choices[0].delta:
            delta = chunk.choices[0].delta
            
            # Extract reasoning
            r = None
            if getattr(delta, "reasoning_content", None):
                r = delta.reasoning_content
            elif getattr(delta, "model_extra", None) and delta.model_extra.get("reasoning"):
                r = delta.model_extra.get("reasoning")
            elif getattr(delta, "reasoning", None):
                r = delta.reasoning
                
            if r:
                reasoning_chunks.append(r)
            if delta.content:
                chunks.append(delta.content)
                
    elapsed = (time.perf_counter() - start) * 1000
    print(f"    latency: {elapsed:.0f}ms")
    print(f"    server-reported model: {last_model_seen}")
    if reasoning_chunks:
        print(f"    thinking process: {''.join(reasoning_chunks)!r}")
    print(f"    streamed text: {''.join(chunks)!r}")

    print("\n>>> non-streamed chat completion")
    start = time.perf_counter()
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Reply with: ok"}],
        max_completion_tokens=150,
        temperature=0.0,
    )
    elapsed = (time.perf_counter() - start) * 1000
    print(f"    latency: {elapsed:.0f}ms")
    print(f"    server-reported model: {resp.model}")
    
    message = resp.choices[0].message
    reasoning = None
    if getattr(message, "reasoning_content", None):
        reasoning = message.reasoning_content
    elif getattr(message, "model_extra", None) and message.model_extra.get("reasoning"):
        reasoning = message.model_extra.get("reasoning")
    elif getattr(message, "reasoning", None):
        reasoning = message.reasoning
        
    if reasoning:
        print(f"    thinking process: {reasoning!r}")
    print(f"    content: {message.content!r}")
    if resp.usage:
        print(f"    tokens: in={resp.usage.prompt_tokens} out={resp.usage.completion_tokens}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://localhost:8000/v1",
                   help="OpenAI-compatible base URL")
    p.add_argument("--model", default=None,
                   help="Model id (default: first reported by the endpoint)")
    p.add_argument("--api-key", default="vllm",
                   help="Bearer token (vLLM/Ollama ignore this; LM Studio needs 'lm-studio')")
    args = p.parse_args()

    print(f">>> GET {args.url}/models")
    try:
        models = list_models(args.url)
    except urllib.error.URLError as e:
        print(f"ERROR: cannot reach {args.url}: {e}", file=sys.stderr)
        return 2
    if not models:
        print("ERROR: endpoint reports no models", file=sys.stderr)
        return 3
    print(f"    found {len(models)} model(s): {models}")

    model = args.model or models[0]
    try:
        chat_round_trip(args.url, model, api_key=args.api_key)
    except Exception as e:
        print(f"ERROR: chat round-trip failed: {e}", file=sys.stderr)
        return 4

    print("\nOK end-to-end verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
