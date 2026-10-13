"""A planned landfall permanent is cast before the land drop that would trigger it.

Live 2026-10-12 22:32:39-49 (Mac, turn 11, our Main 1, empty stack): the GRE
offered Cast Evolution Sage [OK] (grpId 69610, iid 496) and Play Land: Ba Sing
Se (grpId 97547, iid 513). The model answered idx:4 — "Ba Sing Se is a free
land drop ... keeping my 5 mana intact to cast Evolution Sage or Michelangelo,
Improviser afterward" — and cast Sage four seconds later, so "Landfall —
Whenever a land you control enters, proliferate" (and the Terrasymbiosis draw
it feeds) missed the land drop. The game plan's T11 step was "cast Evolution
Sage + Michelangelo, Improviser".
"""

from __future__ import annotations

from arenamcp.action_planner import ActionPlanner, ActionType, board_math_option_pick
from arenamcp.decisions import build_pending_decision
from arenamcp.landfall_order import landfall_cast_pending, landfall_first, landfall_trigger

ME, THEM = 2, 1

SAGE = {
    "instance_id": 496,
    "grp_id": 69610,
    "name": "Evolution Sage",
    "owner_seat_id": ME,
    "type_line": "Creature — Elf Druid",
    "card_types": ["Creature"],
    "mana_cost": "{2}{G}",
    "power": 3,
    "toughness": 2,
    "oracle_text": "Landfall — Whenever a land you control enters, proliferate.",
}
IMPROVISER = {
    "instance_id": 505,
    "grp_id": 70001,
    "name": "Michelangelo, Improviser",
    "owner_seat_id": ME,
    "type_line": "Legendary Creature — Mutant Ninja Turtle",
    "card_types": ["Creature"],
    "mana_cost": "{3}{G}",
    "power": 3,
    "toughness": 3,
    "oracle_text": "Whenever Michelangelo deals combat damage to a player, draw a card.",
}
BA_SING_SE = {
    "instance_id": 513,
    "grp_id": 97547,
    "name": "Ba Sing Se",
    "owner_seat_id": ME,
    "type_line": "Land",
    "card_types": ["Land"],
    "oracle_text": "This land enters tapped unless you control a basic land.\n{T}: Add {G}.",
}

THIS_TURN = (
    "  THIS TURN (T11, now): play Ba Sing Se; cast Evolution Sage + Michelangelo, Improviser; "
    "attack: Michelangelo, Weirdness to 11; hold: ~2 mana spare [game plan T10]"
)
LIVE_REASONING = (
    "Ba Sing Se is a free land drop that develops mana without spending castable resources, "
    "keeping my 5 mana intact to cast Evolution Sage or Michelangelo, Improviser afterward."
)


def _state(*, hand=None, active=ME, phase="Phase_Main1", stack=None) -> dict:
    return {
        "match_id": "landfall-t11",
        "local_seat_id": ME,
        "turn": {"turn_number": 11, "active_player": active, "priority_player": ME, "phase": phase},
        "players": [
            {"seat_id": THEM, "life_total": 20, "is_local": False, "lands_played": 0},
            {"seat_id": ME, "life_total": 20, "is_local": True, "lands_played": 0},
        ],
        "battlefield": [],
        "hand": list(hand if hand is not None else [SAGE, IMPROVISER, BA_SING_SE]),
        "stack": list(stack or []),
        "graveyard": [],
        "exile": [],
        "command": [],
        "decision_context": {},
    }


def _decision(*, sage_payable: bool = True):
    """Cast Sage (idx:0), Cast Improviser (idx:1), Play Ba Sing Se (idx:2), Pass."""
    actions = [
        {"actionType": "ActionType_Cast", "grpId": 69610, "instanceId": 496, "hasAutoTap": sage_payable},
        {"actionType": "ActionType_Cast", "grpId": 70001, "instanceId": 505, "hasAutoTap": True},
        {"actionType": "ActionType_Play", "grpId": 97547, "instanceId": 513},
        {"actionType": "ActionType_Pass"},
    ]
    names = {69610: "Evolution Sage", 70001: "Michelangelo, Improviser", 97547: "Ba Sing Se"}
    return build_pending_decision(
        {"has_pending": True, "request_type": "ActionsAvailable", "actions": actions, "can_pass": True},
        resolve_name=lambda grp: names.get(grp, f"Card {grp}"),
    )


class _Backend:
    def __init__(self, answer: str):
        self.answer = answer

    def complete(self, system, user, *args, **kwargs):
        return self.answer


def _land_answer(reasoning: str = LIVE_REASONING) -> str:
    return '{"option_ids": ["idx:2"], "reasoning": "%s"}' % reasoning


# --- recognition ---------------------------------------------------------------


def test_landfall_wordings_are_recognised():
    assert landfall_trigger(SAGE)
    cobra = {
        "type_line": "Creature — Snake",
        "oracle_text": "Landfall — Whenever a land enters the battlefield under your control, add one "
        "mana of any color.",
    }
    assert landfall_trigger(cobra)
    plural = {
        "type_line": "Enchantment",
        "oracle_text": "Whenever one or more lands you control enter, create a 1/1 Insect token.",
    }
    assert landfall_trigger(plural)


def test_non_triggers_and_land_first_payoffs_are_not_landfall():
    blaze = {
        "type_line": "Instant",
        "oracle_text": "Landfall — If you had a land enter the battlefield under your control this turn, "
        "Searing Blaze deals 3 damage instead.",
    }
    assert not landfall_trigger(blaze)
    punisher = {
        "type_line": "Enchantment",
        "oracle_text": "Whenever a land an opponent controls enters, that player loses 1 life.",
    }
    assert not landfall_trigger(punisher)
    bloodghast = {
        "type_line": "Creature — Vampire Spirit",
        "oracle_text": "Bloodghast can't block.\nLandfall — Whenever a land you control enters, you may "
        "return Bloodghast from your graveyard to the battlefield.",
    }
    assert not landfall_trigger(bloodghast)
    assert not landfall_trigger(IMPROVISER)


# --- the live turn ----------------------------------------------------------------


def test_live_turn_11_casts_sage_before_ba_sing_se():
    planner = ActionPlanner(backend=_Backend(_land_answer()))
    chosen = planner.plan_decision_options(_decision(), _state())
    assert chosen == ["idx:0"]
    assert "Evolution Sage" in planner._last_decision_reasoning
    assert "Ba Sing Se" in planner._last_decision_reasoning
    assert planner._last_decision_trace["landfall_order"]["replaced"] == "idx:2"


def test_this_turn_plan_line_is_enough_intent():
    decision = _decision()
    first = landfall_first(decision, _state(), "idx:2", reasoning="Free land drop.", plan_block=THIS_TURN)
    assert first is not None and first.option_id == "idx:0"


def test_board_math_line_casts_count_as_intent():
    first = landfall_first(_decision(), _state(), "idx:2", planned_names={"Evolution Sage"})
    assert first is not None and first.card_name == "Evolution Sage"


# --- when the land stays first ------------------------------------------------------


def test_no_plan_to_cast_it_keeps_the_land():
    planner = ActionPlanner(backend=_Backend(_land_answer("Free land drop, then Michelangelo, Improviser.")))
    assert planner.plan_decision_options(_decision(), _state()) == ["idx:2"]


def test_holding_the_landfall_card_keeps_the_land():
    first = landfall_first(
        _decision(), _state(), "idx:2", reasoning="Play the land and hold Evolution Sage for next turn."
    )
    assert first is None


def test_unpayable_landfall_card_keeps_the_land():
    # It needs the land's mana: land first is the only way to cast it.
    first = landfall_first(_decision(sage_payable=False), _state(), "idx:2", planned_names={"Evolution Sage"})
    assert first is None


def test_x_cost_landfall_card_is_left_to_the_model():
    hydra = dict(SAGE, mana_cost="{X}{G}{G}")
    first = landfall_first(
        _decision(), _state(hand=[hydra, IMPROVISER, BA_SING_SE]), "idx:2", planned_names={"Evolution Sage"}
    )
    assert first is None


def test_only_on_our_main_phase_with_an_empty_stack():
    planned = {"Evolution Sage"}
    assert landfall_first(_decision(), _state(active=THEM), "idx:2", planned_names=planned) is None
    assert landfall_first(_decision(), _state(stack=[IMPROVISER]), "idx:2", planned_names=planned) is None


def test_a_cast_pick_is_never_rewritten():
    assert landfall_first(_decision(), _state(), "idx:1", planned_names={"Evolution Sage"}) is None


# --- the land-first defaults ----------------------------------------------------------


def test_legacy_preflight_defers_to_the_model_with_a_landfall_cast_pending():
    legal = ["Cast Evolution Sage [OK]", "Play Land: Ba Sing Se", "Pass"]
    assert landfall_cast_pending(_state(), legal) == "Evolution Sage"
    state = dict(
        _state(), _bridge_request_type="ActionsAvailable", _bridge_request_class="ActionsAvailableRequest"
    )
    planner = ActionPlanner(backend=_Backend(""), land_drop_first=True)
    assert planner._should_force_land_drop(state, legal, {"type": "actions_available"}) is None
    no_landfall = ["Cast Michelangelo, Improviser [OK]", "Play Land: Ba Sing Se", "Pass"]
    assert planner._should_force_land_drop(state, no_landfall, {"type": "actions_available"}) == (
        "Play Land: Ba Sing Se"
    )


def test_board_math_fallback_casts_a_scheduled_landfall_card_first(monkeypatch):
    from types import SimpleNamespace

    import arenamcp.board_assessment as board_assessment

    step = SimpleNamespace(land="Ba Sing Se", casts=("Evolution Sage", "Michelangelo, Improviser"))
    assessment = SimpleNamespace(our_turn=True, lookahead=[step], line_search=None)
    monkeypatch.setattr(board_assessment, "assess", lambda state: assessment)
    picked, why = board_math_option_pick(_decision(), _state())
    assert picked == ["idx:0"]
    assert "landfall" in why


def test_preflight_plan_type_is_still_land_without_landfall():
    legal = ["Cast Michelangelo, Improviser [OK]", "Play Land: Ba Sing Se", "Pass"]
    state = dict(
        _state(hand=[IMPROVISER, BA_SING_SE]),
        _bridge_request_type="ActionsAvailable",
        _bridge_request_class="ActionsAvailableRequest",
    )
    planner = ActionPlanner(backend=_Backend(""), land_drop_first=True)
    plan = planner.plan_actions(state, "decision_required", legal, {"type": "actions_available"})
    assert plan.actions and plan.actions[0].action_type == ActionType.PLAY_LAND
