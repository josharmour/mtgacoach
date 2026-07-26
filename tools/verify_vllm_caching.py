"""Verify vLLM prefix caching is on and measure the speedup.

Why this exists: vLLM's automatic prefix caching only helps when the
prompt's prefix is byte-identical across requests. The coach naturally
sends a stable system prompt and a slowly-evolving game state, which
caches well — but only if the server has caching enabled and the
prompt structure isn't forcing a fresh prefill every turn.

What this does:
  1. Pulls the current prefix-cache hit/query totals from /metrics.
  2. Sends a long-ish prompt with a fixed prefix twice and measures
     time-to-first-token + total wall-clock for each call.
  3. Pulls /metrics again, computes the per-call delta, and prints
     hit_rate, the prefill speedup factor, and a verdict.

Usage:
    python -m tools.verify_vllm_caching
    python -m tools.verify_vllm_caching --base-url http://host:port/v1 --model gemma4:e2b
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request

_METRICS_KEYS = (
    "vllm:prefix_cache_queries_total",
    "vllm:prefix_cache_hits_total",
)


def _read_metrics(base_url: str) -> dict[str, float]:
    """Fetch Prometheus /metrics and pull just the counters we care about."""
    metrics_url = base_url.rstrip("/").removesuffix("/v1") + "/metrics"
    try:
        with urllib.request.urlopen(metrics_url, timeout=5) as resp:
            text = resp.read().decode("utf-8", "ignore")
    except urllib.error.URLError as e:
        print(f"WARN: could not fetch /metrics at {metrics_url}: {e}", file=sys.stderr)
        return {}
    out: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        for key in _METRICS_KEYS:
            if line.startswith(key + "{") or line.startswith(key + " "):
                # last whitespace-separated token is the value
                try:
                    out[key] = float(line.rsplit(" ", 1)[-1])
                except ValueError:
                    pass
    return out


def _prompt_with_stable_prefix(seed: int) -> tuple[str, str]:
    """Two-message prompt where the system prompt + most of the user message
    is identical across calls. Only the trailing seed varies, so vLLM
    should hit the cache for the prefix on the second call."""
    system = (
        "You are an expert MTG Arena coach. Reply with a single short line. "
        + ("RULES: be brief, never hallucinate cards, stay under 20 words. " * 30)
    )
    # Long stable middle so the prefix dominates.
    user = (
        "Game state for analysis:\n"
        + ("- Standard board: turn 5, life 17 vs 14, you have Lightning Bolt + 5 lands.\n" * 20)
        + f"\nSeed={seed}: what is the obvious play?"
    )
    return system, user


def _one_shot(client, model: str, system: str, user: str) -> tuple[float, float, int]:
    """Run a non-streaming chat completion. Return (ttft_ms, total_ms, prompt_tokens)."""
    start = time.perf_counter()
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_completion_tokens=8,
        temperature=0.0,
    )
    total_ms = (time.perf_counter() - start) * 1000
    # We don't get a true TTFT without streaming; for prefill-cost comparison,
    # total_ms with max_tokens=8 is dominated by prefill (~100-200ms decode).
    prompt_tokens = (resp.usage.prompt_tokens if resp.usage else 0)
    return total_ms, total_ms, prompt_tokens


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default="http://localhost:8000/v1",
                   help="vLLM OpenAI-compatible base URL")
    p.add_argument("--model", default="gemma4:e2b")
    p.add_argument("--api-key", default="vllm")
    args = p.parse_args()

    try:
        from openai import OpenAI
    except ImportError:
        print("ERROR: pip install openai", file=sys.stderr)
        return 1

    client = OpenAI(base_url=args.base_url, api_key=args.api_key, timeout=60)

    # 1. Lifetime metrics
    before = _read_metrics(args.base_url)
    if before:
        q = before.get("vllm:prefix_cache_queries_total", 0)
        h = before.get("vllm:prefix_cache_hits_total", 0)
        rate = (h / q * 100) if q else 0.0
        print(f"Lifetime prefix-cache: {h:.0f} hits / {q:.0f} queries = {rate:.1f}%")
    else:
        print("WARN: could not read /metrics — proceeding with timing only")

    # 2. First call (cold prefix)
    sys_prompt, user_prompt = _prompt_with_stable_prefix(seed=1)
    print(f"\n>>> call 1 (cold prefix) — prompt ~{len(sys_prompt) + len(user_prompt)} chars")
    cold_ms, _, cold_tokens = _one_shot(client, args.model, sys_prompt, user_prompt)
    print(f"    {cold_ms:.0f} ms total, prompt_tokens={cold_tokens}")

    # 3. Second call — same prefix, different seed in the trailing line
    sys_prompt2, user_prompt2 = _prompt_with_stable_prefix(seed=2)
    print(">>> call 2 (warm prefix, seed flipped at the tail)")
    warm_ms, _, warm_tokens = _one_shot(client, args.model, sys_prompt2, user_prompt2)
    print(f"    {warm_ms:.0f} ms total, prompt_tokens={warm_tokens}")

    speedup = cold_ms / warm_ms if warm_ms > 0 else float("inf")
    saved_ms = cold_ms - warm_ms
    print(f"\n>>> warm-vs-cold: speedup x{speedup:.2f}, saved {saved_ms:.0f} ms")

    # 4. Metrics delta
    after = _read_metrics(args.base_url)
    if before and after:
        dq = after.get("vllm:prefix_cache_queries_total", 0) - before.get("vllm:prefix_cache_queries_total", 0)
        dh = after.get("vllm:prefix_cache_hits_total", 0) - before.get("vllm:prefix_cache_hits_total", 0)
        delta_rate = (dh / dq * 100) if dq else 0.0
        print(f">>> /metrics delta over the 2 calls: {dh:.0f} hits / {dq:.0f} queries = {delta_rate:.1f}% hit rate")

    # 5. Verdict
    print()
    if speedup >= 1.5:
        print(f"OK prefix caching is working — {speedup:.2f}x prefill speedup on repeat prefix.")
        return 0
    if speedup >= 1.1:
        print(f"WARN modest speedup ({speedup:.2f}x). Caching is on but the test prompt may be too short to amortize warmup.")
        return 0
    print(f"FAIL no real speedup ({speedup:.2f}x). Check that vLLM was started without --no-enable-prefix-caching.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
