"""Offline, serial paired replay. No Arena connection or production policy changes.

Inputs and responses are private JSON artifacts. The runner uses the production
JSON extractor and selection validator, but does NOT run full gameplay guards.
Run with PYTHONPATH=src; credentials come only from an environment variable.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import subprocess
import time
from pathlib import Path

import httpx

from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision


def request_body(case, effort, model):
    if effort != "none" and effort != "low" and not (type(effort) is int and 1 <= effort <= 100):
        raise ValueError("effort must be none, low, or an integer 1..100")
    body = {
        "model": model,
        "messages": case["messages"],
        "temperature": 0.0,
        "max_tokens": case.get("max_tokens", 2048),
        "stream": True,
        "stream_options": {"include_usage": True},
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": {"thinking": effort != "none", "reasoning_effort": effort},
    }
    cap = case.get("thinking_token_budget")
    if cap is not None:
        if type(cap) is not int or cap < 0:
            raise ValueError("thinking_token_budget must be a nonnegative integer")
        if effort != "none":
            body["thinking_token_budget"] = cap
    return body


def assess(case, content):
    result = {"parsed": False, "selection_valid": False, "expected_selection": None}
    try:
        data = json.loads(ActionPlanner._extract_first_json(content) or "")
        ids = data["option_ids"]
        if not isinstance(ids, list) or any(type(x) not in (str, int) for x in ids):
            return result
        result["parsed"] = True
        d = case["decision"]
        decision = PendingDecision(
            request_id=tuple(d.get("request_id", [0, 0])),
            request_type=d["request_type"],
            options=tuple(DecisionOption(**x) for x in d["options"]),
            min_select=d.get("min_select", 1),
            max_select=d.get("max_select", 1),
        )
        ids = [str(x) for x in ids]
        if decision.request_type in {"ActionsAvailable", "CastingTimeOptions"}:
            ids = [
                f"idx:{x}"
                if x not in decision.option_ids() and x.isdecimal() and f"idx:{x}" in decision.option_ids()
                else x
                for x in ids
            ]
        result.update(
            selection_valid=decision.selection_is_valid(ids),
            option_ids=ids,
            explanation=data.get("reasoning", ""),
        )
        if "accepted_selections" in case:
            result["expected_selection"] = ids in case["accepted_selections"]
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    return result


async def run_one(client, url, case, effort, model):
    body = request_body(case, effort, model)
    started = time.perf_counter()
    result = {
        "case_id": case["id"],
        "effort": effort,
        "request": body,
        "prompt_sha256": hashlib.sha256(json.dumps(case["messages"], sort_keys=True).encode()).hexdigest(),
        "budget_s": case["budget_s"],
        "started_unix": time.time(),
        "content": "",
        "reasoning_chars": 0,
        "first_reasoning_ms": None,
        "first_content_ms": None,
        "finish_reason": None,
        "status": "error",
        "bridge_submission": "disabled",
        "fallback_action": None,
    }

    async def consume():
        async with client.stream("POST", url, json=body) as response:
            result["http_status"] = response.status_code
            response.raise_for_status()
            result["route_headers"] = {
                k: v
                for k, v in response.headers.items()
                if k in {"x-litellm-model-id", "x-litellm-model-api-base", "x-request-id"}
            }
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                if line[6:] == "[DONE]":
                    result["status"] = "ok"
                    return
                chunk = json.loads(line[6:])
                result["served_model"] = chunk.get("model", result.get("served_model"))
                if chunk.get("usage"):
                    result["usage"] = chunk["usage"]
                for choice in chunk.get("choices", []):
                    delta = choice.get("delta", {})
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                    content = delta.get("content") or ""
                    elapsed = (time.perf_counter() - started) * 1000
                    if reasoning and result["first_reasoning_ms"] is None:
                        result["first_reasoning_ms"] = elapsed
                    if content and result["first_content_ms"] is None:
                        result["first_content_ms"] = elapsed
                    result["reasoning_chars"] += len(reasoning)
                    result["content"] += content
                    if choice.get("finish_reason"):
                        result["finish_reason"] = choice["finish_reason"]
            result["status"] = "incomplete_stream"

    try:
        if case["budget_s"] <= 0:
            raise TimeoutError
        await asyncio.wait_for(consume(), timeout=case["budget_s"])
    except TimeoutError:
        result["status"] = "deadline"
        result["cancelled"] = True
    except (httpx.HTTPError, ValueError) as exc:
        # Do not serialize headers or exception strings containing credentials.
        result["error_type"] = type(exc).__name__
    result["total_ms"] = (time.perf_counter() - started) * 1000
    result["assessment"] = assess(case, result["content"])
    result["usable_within_deadline"] = (
        result["status"] == "ok"
        and result["finish_reason"] == "stop"
        and result["assessment"]["selection_valid"]
        and result["total_ms"] <= case["budget_s"] * 1000
    )
    return result


async def main(args):
    cases = json.loads(args.cases.read_text())["cases"]
    if args.thinking_token_budget is not None:
        cases = [dict(c, thinking_token_budget=args.thinking_token_budget) for c in cases]
    conditions = [int(x) if x.isdecimal() else x for x in args.efforts.split(",")]
    for case in cases:
        for effort in conditions:
            request_body(case, effort, args.model)
    key = os.environ[args.key_env]
    schedule = [(c, attempt) for attempt in range(args.attempts) for c in cases]
    rng = random.Random(args.seed)
    rng.shuffle(schedule)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {key}"}, timeout=None, trust_env=False
    ) as client:
        with os.fdopen(fd, "w") as out:
            for case, attempt in schedule:
                order = list(conditions)
                rng.shuffle(order)
                for effort in order:
                    result = await run_one(
                        client, args.base_url.rstrip("/") + "/chat/completions", case, effort, args.model
                    )
                    result.update(
                        attempt=attempt,
                        source_commit=commit,
                        schedule_seed=args.seed,
                        provenance=case.get("provenance"),
                        missing=case.get("missing"),
                    )
                    out.write(json.dumps(result) + "\n")
                    out.flush()
                    print(
                        case["id"],
                        attempt,
                        effort,
                        result["status"],
                        round(result["total_ms"]),
                        result["assessment"].get("option_ids"),
                        flush=True,
                    )
                    if result.get("http_status") in {401, 403, 429}:
                        raise SystemExit("Stopped after authentication/admission failure")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-url", default="https://api.mtgacoach.com/v1")
    parser.add_argument("--model", default="current")
    parser.add_argument("--key-env", default="ARENAMCP_REPLAY_KEY")
    parser.add_argument("--efforts", default="none,1,10,low")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--thinking-token-budget", type=int)
    parser.add_argument("--seed", type=int, default=1103)
    asyncio.run(main(parser.parse_args()))
