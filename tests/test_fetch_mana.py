from types import SimpleNamespace

import pytest

from arenamcp import mulligan_policy as policy
from arenamcp import opening_fetches
from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import build_pending_decision
from arenamcp.optional_prompts import prompt_note

MIRE = "{T}, Pay 1 life, Sacrifice Test Land: Search your library for a Swamp or Mountain card, put it onto the battlefield, then shuffle."
SHOCK = "As this land enters, you may pay 2 life. If you don't, it enters tapped."
MOUNTAIN = {"name": "Mountain", "type_line": "Basic Land — Mountain", "oracle_text": ""}
DUAL = {"name": "Steam Vents", "type_line": "Land — Island Mountain", "oracle_text": SHOCK}
FETCH = {"name": "Test Land", "type_line": "Land", "oracle_text": MIRE, "grp_id": 10}
BLUE = {"name": "Spell", "type_line": "Sorcery", "mana_cost": "{U}{U}", "grp_id": 20}
SPLASH = {**BLUE, "mana_cost": "{1}{U}"}


def opening(monkeypatch, targets, *, held=None, life=20):
    hand = [FETCH, FETCH, BLUE, SPLASH, BLUE, BLUE, held or BLUE]
    s = {
        "hand": [dict(c, instance_id=i + 1) for i, c in enumerate(hand)],
        "local_seat_id": 1,
        "players": [{"seat_id": 1, "life_total": life}],
        "turn": {"turn_number": 0},
        "game_number": 1,
        "deck_cards": [c["grp_id"] for c in hand] + list(targets),
    }
    monkeypatch.setattr("arenamcp.deck_profile._resolve", lambda gid: {30: DUAL, 40: MOUNTAIN}.get(gid, {}))
    return s


def test_same_dual_cannot_fund_two_fetches(monkeypatch):
    s = opening(monkeypatch, [30, 40])
    cards = policy.situation(s).cards
    assert policy.castable_with(cards[3], cards[:2])
    assert not policy.castable_with(cards[2], cards[:2])
    s["deck_cards"].append(30)
    cards = policy.situation(s).cards
    assert policy.castable_with(cards[2], cards[:2])
    s["players"][0]["life_total"] = 6
    cards = policy.situation(s).cards
    assert policy.castable_with(cards[2], cards[:2])  # One shock untaps before turn two.
    s["players"][0]["life_total"] = 4
    cards = policy.situation(s).cards
    assert not policy.castable_with(cards[2], cards[:2])


def test_bottoming_the_only_typed_dual_returns_it_to_the_library(monkeypatch):
    s = opening(monkeypatch, [40], held={**DUAL, "grp_id": 30})
    cards = policy.situation(s).cards
    assert not policy.castable_with(cards[3], cards[:2])
    kept = opening_fetches.after_bottom(cards[:6], {7})
    assert policy.castable_with(kept[3], kept[:2])
    assert policy.kept_score(s, list(range(1, 8)), [7]) == policy.hand_score(
        kept, policy._keep_land_values(6)
    )
    assert not policy.castable_with(cards[3], cards[:2])


@pytest.mark.parametrize(
    "unsafe",
    [
        {**DUAL, "type_line": "Land Creature — Mountain Dryad"},
        {**DUAL, "oracle_text": ""},
        {**DUAL, "layout": "modal_dfc"},
        {**DUAL, "oracle_text": "As this land enters, sacrifice a creature."},
    ],
)
def test_unverified_immediate_sources(monkeypatch, unsafe):
    s = opening(monkeypatch, [30, 40])
    monkeypatch.setattr("arenamcp.deck_profile._resolve", lambda gid: {30: unsafe, 40: MOUNTAIN}.get(gid, {}))
    assert not policy.castable_with(policy.hand_card(SPLASH), policy.situation(s).cards[:2])
    s["game_number"] = 2
    assert all(c.fetch_targets is None for c in policy.situation(s).cards)


def test_stack_note_only_for_own_pending_search(monkeypatch):
    class Backend:
        def complete(self, system, user, **kw):
            self.prompt = user
            return '{"option_ids":["pass"], "reasoning":"Consider a second main phase spell."}'

    hand = dict(BLUE, name="Flash creature", type_line="Creature", oracle_text="Flash", instance_id=11)
    s = {
        "local_seat_id": 1,
        "hand": [hand],
        "turn": {"turn_number": 4, "active_player": 1, "phase": "Main1"},
        "stack": [
            {
                "instance_id": 22,
                "controller_seat_id": 1,
                "object_kind": "ABILITY",
                "name": "Land ability",
                "oracle_text": MIRE,
            }
        ],
    }
    d = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "ActionsAvailable",
            "can_pass": True,
            "actions": [
                {"actionType": "ActionType_Cast", "instanceId": 11, "grpId": 20, "hasAutoTap": True},
                {"actionType": "ActionType_Pass"},
            ],
        },
        resolve_name=lambda _: "Flash creature",
    )
    b = Backend()
    ActionPlanner(b, timeout=1)._llm_decision_options(d, s)
    assert "Reassess payable spells" in b.prompt
    s["stack"][0]["controller_seat_id"] = 2
    ActionPlanner(b, timeout=1)._llm_decision_options(d, s)
    assert "Reassess payable spells" not in b.prompt


def test_shock_payment_uses_actual_tapped_lands_and_bound_request(monkeypatch):
    d = SimpleNamespace(request_type="OptionalAction", prompt_text="Pay 2 life?", find=lambda _: None)
    monkeypatch.setattr(
        "arenamcp.optional_prompts.prompt_cards",
        lambda *_: [{**DUAL, "instance_id": 44, "controller_seat_id": 1}],
    )
    s = {
        "local_seat_id": 1,
        "battlefield": [{**MOUNTAIN, "instance_id": 11, "is_tapped": True, "controller_seat_id": 1}],
    }
    assert "1 of 1 other visible lands are tapped" in prompt_note(d, s)
    d.prompt_text = "Pay 3 life?"
    assert "other visible lands" not in prompt_note(d, s)


def test_fetch_a_tapped_dual_on_turn_one_for_a_turn_two_spell(monkeypatch):
    s = opening(monkeypatch, [30, 40])
    tapped = {
        "name": "Ketria Triome",
        "type_line": "Land — Forest Island Mountain",
        "oracle_text": "({T}: Add {G}, {U}, or {R}.)\nThis land enters tapped.\nCycling {3}",
    }
    monkeypatch.setattr("arenamcp.deck_profile._resolve", lambda g: {30: tapped, 40: MOUNTAIN}.get(g, {}))
    cards = policy.situation(s).cards
    one = policy.hand_card({**BLUE, "mana_cost": "{U}"})
    assert not policy.castable_with(one, cards[:1])
    assert policy.castable_with(cards[3], cards[:2])
    assert not policy.castable_with(cards[2], cards[:2])
