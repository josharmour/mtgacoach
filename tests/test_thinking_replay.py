"""Offline replay transport, deadline, and scoring regressions."""

import asyncio
import importlib.util
from pathlib import Path

import httpx
import pytest

spec = importlib.util.spec_from_file_location(
    "replay_thinking", Path(__file__).parents[1] / "scripts" / "replay_thinking.py"
)
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


def case(budget=1):
    return {
        "id": "test",
        "messages": [{"role": "user", "content": "Choose"}],
        "budget_s": budget,
        "decision": {
            "request_type": "ActionsAvailable",
            "options": [{"option_id": "idx:0", "label": "Forest"}],
        },
        "accepted_selections": [["idx:0"]],
    }


@pytest.mark.parametrize("effort", ["none", "low", 1, 10])
def test_actual_outbound_effort_and_usage(effort):
    def serve(request):
        import json

        body = json.loads(request.content)
        assert body["chat_template_kwargs"] == {"thinking": effort != "none", "reasoning_effort": effort}
        assert body["max_tokens"] == 2048
        return httpx.Response(
            200,
            text='data: {"model":"test","choices":[{"delta":{"content":"{\\"option_ids\\":[\\"idx:0\\"]}"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
            return await replay.run_one(
                client, "https://example.test/v1/chat/completions", case(), effort, "current"
            )

    result = asyncio.run(run())
    assert result["usable_within_deadline"]
    assert result["assessment"]["expected_selection"]


@pytest.mark.parametrize("budget", [0, 0.01])
def test_deadline_cancels_hanging_transport_without_retry(budget):
    calls = []

    async def serve(request):
        calls.append(request)
        await asyncio.sleep(60)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
            return await replay.run_one(
                client, "https://example.test/v1/chat/completions", case(budget), 1, "current"
            )

    result = asyncio.run(run())
    assert result["status"] == "deadline"
    assert not result["usable_within_deadline"]
    assert len(calls) == (1 if budget else 0)


def test_legal_selection_and_expected_selection_are_separate():
    c = case()
    c["accepted_selections"] = [["different"]]
    result = replay.assess(c, '{"option_ids":[0],"reasoning":"test"}')
    assert result["selection_valid"]
    assert not result["expected_selection"]
    assert not replay.assess(c, '{"option_ids":["idx:99"]}')["selection_valid"]
    assert not replay.assess(c, "not json")["parsed"]


def test_thinking_budget_is_separate_from_total_output_budget():
    c = dict(case(), thinking_token_budget=128)
    assert replay.request_body(c, 1, "current")["thinking_token_budget"] == 128
    assert replay.request_body(c, 1, "current")["max_tokens"] == 2048
    assert "thinking_token_budget" not in replay.request_body(c, "none", "current")
