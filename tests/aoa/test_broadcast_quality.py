"""Conversation continuity, whole-position analysis and expressive delivery."""

import copy
import json
import sys
import threading
import time
from dataclasses import replace

import pytest
from test_headless_booth import observation, response, source  # noqa: F401
from test_strategy_content import card

from arenamcp.commentary.client import CommentaryClient
from arenamcp.commentary.config import PERSONAS, CommentaryConfig
from arenamcp.commentary.context import compact_context, generation_format, request_size
from arenamcp.commentary.contracts import Observation
from arenamcp.commentary.headless import SYSTEM, HeadlessBooth, deterministic
from arenamcp.commentary.service import CommentaryService
from arenamcp.commentary.strategy import strategic_observation


@pytest.mark.parametrize(
    "old,new",
    [
        ("classic", "technical"),
        ("esports", "hype"),
        ("test_match", "technical"),
        ("pro_tour", "technical"),
        ("hype", "hype"),
        ("technical", "technical"),
    ],
)
def test_two_styles_preserve_legacy_preferences(old, new):
    assert set(PERSONAS) == {"hype", "technical"}
    config = CommentaryConfig.from_settings({"commentary_persona": old})
    assert config.persona == new
    assert CommentaryConfig.from_wire(config.to_wire()) == config


def test_analysis_memory_tracks_delivery_independently_of_action_calls():
    service = CommentaryService(settings=lambda: {}, backend=lambda: None, emit=lambda _: None)
    first = deterministic(observation(), CommentaryConfig())[0]
    for i in range(30):
        role = "color_analyst" if i == 0 else "play_by_play"
        item = replace(first, utterance_id=str(i), text=f"Delivered point {i}.", role=role)
        service.proposals[item.utterance_id] = item
        service.delivery(item.utterance_id, "finished")
    assert "Delivered point 0." not in service.heard
    assert list(service.heard_analysis) == ["Delivered point 0."]
    for state in ("started", "failed", "superseded", "stopped", "muted"):
        item = replace(first, utterance_id=state, role="color_analyst", text="Never finished.")
        service.proposals[state] = item
        service.delivery(state, state)
    assert list(service.heard_analysis) == ["Delivered point 0."]
    obs = replace(observation(), recent_analysis=tuple(service.heard_analysis))
    assert Observation.from_wire(obs.to_wire()).recent_analysis == obs.recent_analysis


def test_dense_card_rules_cannot_displace_all_conversational_memory():
    history = [
        "The attacking side has to keep pressure on before the resource engine takes over.",
        "Trading creatures could favor the player with a repeatable source of cards.",
        "That removal changes which player can afford to keep developing their board.",
        "Holding back blockers could buy a turn, at the cost of slowing the race.",
    ]
    context = {
        "facts": {
            "event:new": {"kind": "spell_cast", "data": {"grp_id": 1}},
            "state:game_read": {"shape": "race", "players": []},
            "state:battlefield": {"objects": [], "complete": False},
            **{f"card:{i}": {"name": f"Card {i}", "rules": "Long rules. " * 200} for i in range(20)},
        },
        "new_events": ["event:new"],
        "recent_analysis": history,
    }
    selected = compact_context(SYSTEM, context)
    assert selected["recent_analysis"] == history
    assert "event:new" in selected["facts"] and "state:game_read" in selected["facts"]
    assert (
        request_size(SYSTEM, selected) + len(json.dumps(generation_format(), separators=(",", ":")).encode())
        <= 5900
    )


@pytest.mark.usefixtures("source")
@pytest.mark.parametrize("fresh", [True, False, None])
def test_editorial_audit_checks_meaning_against_heard_analysis(fresh):
    prior = "Keeping creatures back could slow the race but protect against the return attack."
    obs = replace(observation(), recent_analysis=(prior,))
    requests = []

    def rpc(system, context, audit):
        requests.append(audit)
        assert context["recent_analysis"] == [prior]
        if audit:
            assert [line["role"] for line in context["lines"]] == ["play_by_play", "color_analyst"]
            result = response(context, True)
            if fresh is None:
                result["checks"][1].pop("adds_insight")
            else:
                result["checks"][1]["adds_insight"] = fresh
            return result
        return response(context, False)

    booth = HeadlessBooth(CommentaryConfig(mode="commentary", generative=True), rpc, time.monotonic)
    lines, status = booth.generate(obs)
    assert requests == [False, True]
    assert bool(lines) is (fresh is True), status
    assert not booth.booth.heard  # Neither a proposal nor an audit counts as spoken.


def test_fallback_develops_different_position_topics_then_stays_quiet():
    obs = observation()
    board = [card(20, "First threat", ""), {**card(21, "Second threat", ""), "controller_seat_id": 2}]
    board += [{**card(30 + i, "Land", ""), "type_line": "Land"} for i in range(4)]
    board += [{**card(40, "Land", ""), "type_line": "Land", "controller_seat_id": 2}]
    state = copy.deepcopy(obs.public_state)
    state["zones"]["battlefield"] = board
    obs = replace(obs, public_state=state)
    insights = []
    for _ in range(5):
        insight = strategic_observation(obs, obs.events[0])
        if insight is None:
            break
        insights.append(insight)
        obs = replace(obs, recent_analysis=tuple(text for text, scope in insights))
    assert len(insights) == 2  # Combat exposure and resource capacity, not paraphrases.
    assert len({text for text, _ in insights}) == 2
    changed = copy.deepcopy(state)
    changed["zones"]["battlefield"] = [c for c in board if c["instance_id"] != 21]
    next_point = strategic_observation(replace(obs, public_state=changed), obs.events[0])
    assert next_point and next_point not in insights  # Losing a board opens a new strategic question.


def test_private_hand_cannot_supply_fallback_without_opt_in():
    obs = replace(observation(), private_state={"hand": [card(30, "Private card", "")]})
    assert strategic_observation(obs, obs.events[0]) is None
    assert strategic_observation(obs, obs.events[0], private=True) is not None


@pytest.mark.parametrize("style,expected", [("hype", "electric"), ("technical", "normal")])
def test_fallback_game_end_preserves_style_delivery_in_wire(style, expected):
    obs = observation()
    obs = replace(obs, events=(replace(obs.events[0], kind="game_end"),))
    line = deterministic(obs, CommentaryConfig(mode="commentary", persona=style))[0]
    assert line.to_wire()["excitement"] == expected


@pytest.mark.parametrize("style,expected", [("hype", "electric"), ("technical", "normal")])
def test_generated_excitement_survives_real_worker_transport(request, style, expected):
    obs = observation()
    obs = replace(obs, events=(replace(obs.events[0], kind="game_end", payload={}),))

    def rpc(system, context, audit):
        if audit:
            return response(context, True)
        return {
            "turns": [{"role": "play_by_play", "text": "The game has ended.", "refs": context["new_events"]}]
        }

    client = CommentaryClient()
    config = CommentaryConfig(
        mode="commentary",
        generative=True,
        persona=style,
        python=sys.executable,
        aoa_source=request.getfixturevalue("source"),
    )
    try:
        result = client.generate(obs, config, rpc, threading.Event())
        assert len(result["utterances"]) == 1, result["status"]
        assert result["utterances"][0]["excitement"] == expected
    finally:
        client.close()
