"""Dense-board regression, private context, useful booth turns, and opening overview."""

import copy
import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_headless_booth import source  # noqa: F401

from arenamcp.commentary.config import CommentaryConfig
from arenamcp.commentary.context import compact_context, deck_overview, request_size
from arenamcp.commentary.events import ObservationAdapter
from arenamcp.commentary.headless import AUDIT, SYSTEM, HeadlessBooth, deterministic
from arenamcp.commentary.service import CommentaryService


def snapshot():
    return {
        "match_id": "opening",
        "game_number": 1,
        "local_seat_id": 1,
        "turn_info": {"turn_number": 1},
        "players": [{"seat_id": 1, "life_total": 20}, {"seat_id": 2, "life_total": 20}],
        "zones": {
            "my_hand": [
                {
                    "instance_id": 10,
                    "grp_id": 100,
                    "name": "Known spell",
                    "visibility": "private",
                    "owner_seat_id": 1,
                    "oracle_text": "Draw a card.",
                    "mana_cost": "{U}",
                }
            ],
            "opponent_hand": [{"instance_id": 20, "name": "SECRET"}],
        },
        "commentary_deck_overview": {
            "identity": "deck-one",
            "summary": "This deck develops mana for large threats. Protection keeps those threats on the battlefield.",
        },
    }


def test_private_opening_context_is_scoped_and_not_repeated():
    state = snapshot()
    a = ObservationAdapter()
    first = a.observe(state, True)
    assert {e.kind for e in first.events} >= {"hand_changed", "deck_overview"}
    assert "SECRET" not in json.dumps(first.to_wire())
    assert not a.observe(state, True).events
    disabled = ObservationAdapter().observe(state, False)
    assert not disabled.private_state
    assert not any(e.visibility == "private" for e in disabled.events)
    state["zones"]["my_hand"].append(
        {"instance_id": 30, "grp_id": 101, "name": "Other player's hidden card", "owner_seat_id": 2}
    )
    assert "Other player's" not in json.dumps(a.observe(state, True).to_wire())
    state["zones"]["my_hand"][0]["name"] = "Changed hand"
    assert any(e.kind == "hand_changed" for e in a.observe(state, True).events)
    state["game_number"] = 2
    assert any(e.kind == "deck_overview" for e in a.observe(state, True).events)


def test_existing_deck_overview_requires_current_match_and_deck():
    book = SimpleNamespace(identity="deck-one", data={"spoken_summary": "A supported deck overview."})
    coach = SimpleNamespace(
        _coach=SimpleNamespace(_deck_playbook=book), _deck_analysis_key=("opening", "deck-one")
    )
    assert deck_overview(coach, snapshot())["summary"] == book.data["spoken_summary"]
    assert deck_overview(coach, dict(snapshot(), match_id="next")) is None
    book.identity = "old-deck"
    assert deck_overview(coach, snapshot()) is None


def test_opening_summary_uses_both_voices_without_another_model_call():
    state = snapshot()
    o = ObservationAdapter().observe(state, True)
    config = CommentaryConfig(mode="commentary", private_information=True)
    lines = deterministic(o, config)
    assert len(lines) == 2 and lines[1].anchor_id == lines[0].utterance_id
    assert lines[0].text.startswith("Our deck plan:")
    assert lines[1].text == "Protection keeps those threats on the battlefield."
    assert all(line.event_bound for line in lines)
    assert all(
        "deck plan" not in line.text for line in deterministic(o, replace(config, private_information=False))
    )


def test_dense_context_retains_whole_facts_without_silent_budget_failure():
    facts = {
        "state:board": {"objects": [{"name": "Long creature name", "rules": "x" * 300} for _ in range(60)]},
        "private:hand:1": {"seat": 1, "cards": [{"name": "Useful card", "grp_id": 100}]},
        "card:100": {"name": "Useful card", "rules": "Conditional rule. " * 30},
        "event:new": {"kind": "cast", "data": {"grp_id": 100}},
    }
    context = {
        "facts": facts,
        "new_events": ["event:new"],
        "allowed_roles": ["play_by_play", "color_analyst"],
        "booth": {},
        "style": "Technical analysis",
        "heard": [],
        "earlier_points": [],
        "in_flight": [],
    }
    original = copy.deepcopy(context)
    selected = compact_context(SYSTEM, context)
    assert request_size(SYSTEM, context) > 6000 and request_size(SYSTEM, selected) < 6000
    assert "private:hand:1" in selected["facts"] and "card:100" in selected["facts"]
    assert selected["facts"]["card:100"] == facts["card:100"]
    assert context == original and "state:board" not in selected["facts"]


@pytest.mark.usefixtures("source")
def test_native_hand_observation_supports_audited_possibilities():
    state = snapshot()
    state.pop("commentary_deck_overview")
    o = ObservationAdapter().observe(state, True)
    # Hand changes provide work even without a new public play.
    o = replace(o, events=tuple(e for e in o.events if e.kind == "hand_changed"))
    requests = []

    def rpc(system, context, audit):
        requests.append(audit)
        assert request_size(system, context) <= 6000
        if audit:
            assert "Clearly labeled possibilities" in system
            return {
                "checks": [
                    {"index": i, "supported": True, "unsupported_claim": ""}
                    for i in range(len(context["lines"]))
                ]
            }
        assert "private:hand:1" in context["facts"] and "card:100" in context["facts"]
        return {
            "turns": [
                {
                    "role": "play_by_play",
                    "text": "Known spell is in your hand.",
                    "refs": [context["new_events"][0], "private:hand:1"],
                },
                {
                    "role": "color_analyst",
                    "text": "If it resolves, it could draw a card; an opposing response remains possible.",
                    "refs": [context["new_events"][0], "private:hand:1", "card:100"],
                },
            ]
        }

    booth = HeadlessBooth(
        CommentaryConfig(mode="commentary", generative=True, private_information=True), rpc, time.monotonic
    )
    lines, status = booth.generate(o)
    assert len(lines) == 2, status
    assert requests == [False, True] and lines[1].anchor_uid == lines[0].uid
    assert "never imply access to hidden cards" in SYSTEM
    assert "fabricated probabilities" in AUDIT


def test_service_keeps_opening_while_speech_is_busy_and_emits_once():
    state = snapshot()
    events = []
    ready = threading.Event()

    class Client:
        def close(self):
            pass

        def delivered(self, *args):
            pass

        def start(self, *args):
            raise AssertionError("Deck overview must not start inference")

    def emit(event):
        events.append(event)
        if event["type"] == "commentary_proposal":
            ready.set()

    s = CommentaryService(
        settings=lambda: {"autoplay_speech": "commentary", "commentary_private_information": True},
        backend=lambda: None,
        emit=emit,
        client_factory=Client,
    )
    s.inflight["existing"] = time.monotonic() + 5
    try:
        s.offer(state)
        assert not ready.wait(0.2)
        s.delivery("existing", "finished")
        assert ready.wait(2)
        deadline = time.monotonic() + 1
        while (
            len([e for e in events if e["type"] == "commentary_proposal"]) < 2 and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        proposals = [e["utterance"] for e in events if e["type"] == "commentary_proposal"]
        assert len(proposals) == 2
        for p in proposals:
            s.delivery(p["utterance_id"], "finished")
        s.offer(state)
        time.sleep(0.2)
        assert len([e for e in events if e["type"] == "commentary_proposal"]) == 2
    finally:
        s.close()
        s.thread.join(2)


def test_shared_audit_facts_are_not_duplicated_or_truncated():
    from arenamcp.commentary.context import compact_audit

    fact = {"rules": "A complete conditional rule. " * 80}
    original = {
        "lines": [
            {"index": i, "text": "A conditional possibility.", "evidence": {"card:1": fact}} for i in range(2)
        ]
    }
    audit = compact_audit(original)
    assert audit["facts"]["card:1"] == fact
    assert all(line["refs"] == ["card:1"] for line in audit["lines"])
    assert request_size(AUDIT, audit) < request_size(AUDIT, original)


def test_hand_opportunity_survives_unrelated_board_change_but_not_empty_hand():
    state = snapshot()
    state.pop("commentary_deck_overview")
    s = CommentaryService(
        settings=lambda: {"autoplay_speech": "commentary", "commentary_private_information": True},
        backend=lambda: None,
        emit=lambda e: None,
    )
    s.inflight["existing"] = time.monotonic() + 30

    def wait(predicate):
        end = time.monotonic() + 2
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(0.01)
        assert predicate()

    try:
        s.offer(state)
        wait(lambda: any(e.kind == "hand_changed" for e in s.pending_events.values()))
        original = next(e.event_id for e in s.pending_events.values() if e.kind == "hand_changed")
        changed = copy.deepcopy(state)
        changed["players"][1]["life_total"] = 19
        s.offer(changed)
        wait(lambda: s.adapter.previous.public_state["players"][1]["life_total"] == 19)
        assert original in s.pending_events
        empty = copy.deepcopy(changed)
        empty["zones"]["my_hand"] = []
        s.offer(empty)
        wait(lambda: original not in s.pending_events)
    finally:
        s.close()
        s.thread.join(2)


def test_hypothetical_damage_math_uses_only_recorded_life_and_printed_player_damage():
    from arenamcp.commentary.context import add_damage_arithmetic

    facts = {
        "state:player:2": {"seat": 2, "life": 8},
        "card:1": {"rules": "Bolt deals 3 damage to any target."},
        "card:2": {"rules": "This deals X damage to any target."},
        "card:3": {"rules": "This deals 3 damage to target creature."},
        "card:4": {"rules": None},
    }
    add_damage_arithmetic(facts)
    calculation = facts["card:1"]["hypothetical_arithmetic"]
    assert calculation["players"] == [{"seat": 2, "life_before": 8, "damage": 3, "life_after": 5}]
    assert "No target, resolution" in calculation["condition"]
    assert "hypothetical_arithmetic" not in facts["card:2"]
    assert "hypothetical_arithmetic" not in facts["card:3"]
    assert "hypothetical_arithmetic" not in facts["card:4"]
