"""BUG-278: a defender-role label must not veto a supported surviving attack.

Symptom (queue row): "Defender-role plans repeatedly hold despite a supported
surviving attack in a losing race."  The historical packet for #278 is missing
(docs/BACKLOG_STAGE_2026-11-01.md:136), so these boards are reconstructed from
the production modules themselves rather than replayed from a match:

* ``assess`` labels the board (``role``) and runs the line search;
* ``line_search.posture`` ranks hold against attack with net lifelink and
  crackback already in the numbers;
* ``attack_line_note`` turns that verdict into prompt text;
* ``attack_push`` turns an attacking posture into a declaration when the
  declared line ranks clearly below the best one;
* ``ActionPlanner._check_attack_crackback`` trims or rejects an attack that
  loses to their crackback.

Contrast pair pinned here:

* push board   - role 'defender', posture 'attack': the empty declaration is
                 pushed to the searched attackers and the crackback guard keeps
                 them, so the label does not veto the better supported line.
* reject board - posture 'hold' ("attack ... dead on T9; hold: dead on T11"):
                 nothing is pushed and the crackback guard empties the
                 declaration, so a genuinely losing attack stays rejected.
"""

from __future__ import annotations

import pytest

from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction
from arenamcp.board_assessment import ROLE_DEFENDER, assess
from arenamcp.line_guard import attack_line_note, attack_push

pytestmark = pytest.mark.usefixtures("unlimited_search_deadline")

LOCAL_SEAT = 1
OPPONENT_SEAT = 2


def _creature(instance_id: int, name: str, controller: int, power: int, toughness: int) -> dict:
    return {
        "instance_id": instance_id,
        "name": name,
        "owner_seat_id": controller,
        "controller_seat_id": controller,
        "grp_id": instance_id,
        "type_line": f"Creature — {name}",
        "card_types": ["CardType_Creature"],
        "power": power,
        "toughness": toughness,
        "oracle_text": "",
        "is_tapped": False,
    }


def _land(instance_id: int, controller: int) -> dict:
    return {
        "instance_id": instance_id,
        "name": "Forest",
        "owner_seat_id": controller,
        "controller_seat_id": controller,
        "grp_id": instance_id,
        "type_line": "Basic Land — Forest",
        "card_types": ["CardType_Land"],
        "oracle_text": "({T}: Add {G}.)",
    }


def _board(
    *,
    life: int,
    opponent_life: int,
    ours: list[tuple[int, int]],
    theirs: list[tuple[int, int]],
) -> dict:
    """Our turn, declare attackers; every creature untapped and able to attack."""
    battlefield = [
        _creature(100 + index, f"Ours{index}", LOCAL_SEAT, power, toughness)
        for index, (power, toughness) in enumerate(ours)
    ]
    battlefield += [_land(200 + index, LOCAL_SEAT) for index in range(4)]
    battlefield += [
        _creature(300 + index, f"Theirs{index}", OPPONENT_SEAT, power, toughness)
        for index, (power, toughness) in enumerate(theirs)
    ]
    battlefield += [_land(400 + index, OPPONENT_SEAT) for index in range(5)]
    return {
        "match_id": "bug-278-reconstruction",
        "local_seat_id": LOCAL_SEAT,
        "opponent_seat_id": OPPONENT_SEAT,
        "turn": {
            "turn_number": 8,
            "active_player": LOCAL_SEAT,
            "priority_player": LOCAL_SEAT,
            "phase": "Phase_Combat",
            "step": "Step_DeclareAttack",
        },
        "players": [
            {"seat_id": LOCAL_SEAT, "life_total": life, "is_local": True},
            {"seat_id": OPPONENT_SEAT, "life_total": opponent_life, "is_local": False},
        ],
        "battlefield": battlefield,
    }


def _attacker_ids(state: dict) -> list[int]:
    return [card["instance_id"] for card in state["battlefield"] if card["name"].startswith("Ours")]


def _declare_everything(state: dict) -> tuple[ActionPlan, GameAction]:
    ids = _attacker_ids(state)
    names = [f"Ours{index}" for index in range(len(ids))]
    action = GameAction(
        ActionType.DECLARE_ATTACKERS,
        attacker_names=list(names),
        attacker_instance_ids=list(ids),
        reasoning="model: attack with everything",
    )
    plan = ActionPlan(actions=[action], overall_strategy="Attack with everything.")
    return plan, action


def _context(state: dict) -> dict:
    ids = _attacker_ids(state)
    return {
        "type": "declare_attackers",
        "legal_attacker_ids": ids,
        "raw_attackers": [{"attackerInstanceId": instance_id} for instance_id in ids],
    }


@pytest.fixture(autouse=True)
def _push_on_by_default(monkeypatch):
    """The push guard's default setting is 'on'; an inherited env var must not change it."""
    monkeypatch.delenv("ARENAMCP_ATTACK_PUSH", raising=False)


@pytest.mark.parametrize("life", [11, 12])
def test_a_defender_label_does_not_veto_the_searched_attack(life):
    """2x 4/4 against 2x 3/3 at ten life: the search attacks even though we are 'defender'."""
    state = _board(life=life, opponent_life=10, ours=[(4, 4), (4, 4)], theirs=[(3, 3), (3, 3)])
    facts = assess(state)

    assert facts.role == ROLE_DEFENDER
    assert facts.posture == "attack"
    assert facts.all_in is False
    assert facts.lethal_now is False

    note = attack_line_note(facts.line_search, state)
    assert note.startswith("ATTACK vs HOLD")
    assert note.endswith("Verdict: ATTACK.")

    verdict = attack_push(
        facts.line_search,
        [],
        state,
        eligible=_attacker_ids(state),
        lethal_now=facts.lethal_now,
        all_in=facts.all_in,
    )
    assert verdict is not None
    assert verdict.applies is True
    assert verdict.setting == "on"
    assert verdict.attack_ids == [100, 101]
    assert verdict.attack_names == ["Ours0", "Ours1"]

    plan, action = _declare_everything(state)
    ActionPlanner(None, timeout=5.0)._check_attack_crackback(plan, state, _context(state))
    assert action.attacker_names == ["Ours0", "Ours1"]
    assert getattr(action, "guard_hold", None) is False
    assert plan.fallback_reason == ""
    assert plan.voice_advice == ""


def test_a_genuinely_losing_attack_is_still_rejected():
    """2x 1/1 against 2x 2/2 at six life: attacking is dead on T9, holding dead on T11."""
    state = _board(life=6, opponent_life=20, ours=[(1, 1), (1, 1)], theirs=[(2, 2)] * 4)
    facts = assess(state)

    assert facts.posture == "hold"
    assert facts.all_in is False

    note = attack_line_note(facts.line_search, state)
    assert note.startswith("ATTACK vs HOLD")
    assert note.endswith(
        ". Not lethal this turn. Their creatures being tapped now does not make an attack free: an "
        "attacker without vigilance stays tapped through their next turn and cannot block. Keep the "
        "attackers the search keeps home unless you can name what it is missing."
    )
    assert ". Verdict: HOLD." in note

    verdict = attack_push(
        facts.line_search,
        [],
        state,
        eligible=_attacker_ids(state),
        lethal_now=facts.lethal_now,
        all_in=facts.all_in,
    )
    assert verdict is None

    plan, action = _declare_everything(state)
    ActionPlanner(None, timeout=5.0)._check_attack_crackback(plan, state, _context(state))
    assert action.attacker_names == []
    assert getattr(action, "guard_hold", None) is True
    assert plan.fallback_reason == "planner_crackback_guard"
    assert plan.voice_advice.startswith("Holding Ours0 and Ours1 back")
