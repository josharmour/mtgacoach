"""Planeswalker target options carry their loyalty and the damage that kills them (#130).

2026-10-19 13:01:20 (match 3e545401, T15): Shock's target request offered
Ajani Resolute at loyalty 1 (Player.log selectTargetsReq sourceId 374:
players 1 and 2, Ajani 341, Slickshot 347, Hortimancer 353, Cindermaw 360,
the Pridemate token 372). Creatures came with "pt", "status" and
"dies_to_damage"; Ajani came as a bare name, though Shock's 2 kills it. The
retarget code read no loyalty either: ``_prefer_lethal_damage_target``
counted every planeswalker as killed by any damage, and
``_aim_any_target_damage`` offered any planeswalker as "dies instead".
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from tests.test_walker_attack_guard_20261019 import (
    AJANI_ID,
    CINDERMAW_ID,
    HORTIMANCER_ID,
    SLICKSHOT_ID,
    THEM,
    US,
    creature,
    turn_15_board,
)

from arenamcp.action_planner import DECLINE_DECISION, ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision, TargetSlot
from arenamcp.play_safety import planeswalker_state_facts

SHOCK_ID, PRIDEMATE_ID = 374, 372
SHOCK_TEXT = "Shock deals 2 damage to any target."
PRIDEMATE_TEXT = "Whenever you gain life, put a +1/+1 counter on this token."
SHOCK_SAID = (
    "Shock kills Ajani's Pridemate (2/2, dies to 2), removing a growing lifegain/blocker engine exactly as "
    "intended; Hortimancer needs 6 damage and ward, and my own creatures shouldn't be targeted."
)
TARGETS = (1, 2, AJANI_ID, SLICKSHOT_ID, HORTIMANCER_ID, CINDERMAW_ID, PRIDEMATE_ID)


class _Backend:
    def __init__(self, reply):
        self.reply = reply
        self.prompts: list[str] = []

    def available(self):
        return True

    def complete(self, system, user, *args, **kwargs):
        if "PENDING DECISION" in user:
            self.prompts.append(user)
        return self.reply


def _targets(ids, source_label, source_id):
    return PendingDecision(
        request_id=(341, 400),
        request_type="SelectTargets",
        options=tuple(
            DecisionOption(f"tgt:{iid}", f"Target #{iid}", meta={"instanceId": iid}) for iid in ids
        ),
        min_select=1,
        max_select=1,
        can_cancel=True,
        source_label=source_label,
        slots=(TargetSlot(1, 1, 1, 0, tuple(ids)),),
    )


def shock_window(*, ajani_loyalty=1) -> dict:
    """13:01:20: Shock cast from hand (Mountain 281 tapped for it); its target request is open."""
    state = turn_15_board(ajani_loyalty=ajani_loyalty)
    state["turn"].update(phase="Phase_Main1", step="")
    for card in state["battlefield"]:
        if card["instance_id"] == SLICKSHOT_ID:
            card["power"] = 1  # Shock's +2/+0 trigger has not resolved yet
    state["battlefield"].append(
        creature(
            PRIDEMATE_ID,
            "Ajani's Pridemate",
            2,
            2,
            THEM,
            PRIDEMATE_TEXT,
            type_line="Token Creature — Cat Soldier",
            is_token=True,
            summoning_sickness=True,
        )
    )
    state["stack"] = [
        {
            "instance_id": SHOCK_ID,
            "grp_id": 75525,
            "name": "Shock",
            "type_line": "Instant",
            "mana_cost": "{R}",
            "oracle_text": SHOCK_TEXT,
            "controller_seat_id": US,
            "owner_seat_id": US,
        }
    ]
    state["decision_context"] = {"type": "target_selection", "source_id": SHOCK_ID}
    state["_bridge_request_payload"] = {"sourceId": SHOCK_ID}
    return state


def option_facts(prompt: str, option_id: str) -> dict:
    line = next(line for line in prompt.splitlines() if line.startswith(f"- {option_id}:"))
    return json.loads(line[line.index("{") :])


def test_the_shock_prompt_gives_ajani_its_loyalty_and_the_damage_that_kills_it():
    backend = _Backend(json.dumps({"option_ids": [f"tgt:{PRIDEMATE_ID}"], "reasoning": SHOCK_SAID}))
    planner = ActionPlanner(backend, timeout=5, land_drop_first=False)
    chosen = planner.plan_decision_options(_targets(TARGETS, "Shock", SHOCK_ID), shock_window())
    assert chosen == [f"tgt:{PRIDEMATE_ID}"]
    prompt = backend.prompts[0]
    ajani = option_facts(prompt, f"tgt:{AJANI_ID}")
    assert ajani["loyalty"] == 1 and ajani["dies_to_damage"] == 1
    assert ajani["controller_seat_id"] == THEM
    # Creatures keep their own facts; no loyalty on them.
    pridemate = option_facts(prompt, f"tgt:{PRIDEMATE_ID}")
    assert pridemate["pt"] == "2/2" and pridemate["dies_to_damage"] == 2 and "loyalty" not in pridemate
    trace = {entry["option_id"]: entry for entry in planner.get_last_decision_trace()["targets"]}
    assert trace[f"tgt:{AJANI_ID}"]["loyalty"] == 1


def test_walker_facts_read_loyalty_counters_first():
    walker = {"type_line": "Legendary Planeswalker — Ajani", "loyalty": 0, "counters": {"Loyalty": 3}}
    assert planeswalker_state_facts(walker) == {"loyalty": 3, "dies_to_damage": 3}
    assert planeswalker_state_facts({"type_line": "Legendary Planeswalker — Ajani"}) == {}
    # A creature that is also a planeswalker dies to whichever runs out first.
    both = {
        "type_line": "Legendary Creature Planeswalker — Gideon",
        "loyalty": 4,
        "toughness": 2,
        "power": 2,
    }
    assert planeswalker_state_facts(both) == {"loyalty": 4, "dies_to_damage": 2}


# --- the retarget code reads the same loyalty -------------------------------------------------------

WRATH = "Wrath of the Bloodmane deals 4 damage to target creature or planeswalker."


def removal_board(walker_loyalty: int) -> dict:
    state = turn_15_board(ajani_loyalty=walker_loyalty)
    # Hortimancer 7/6 survives 4 (and 2); Ajani is the only other opposing target. Slickshot is
    # left out: its cast trigger is a separate reason to keep a no-kill pick.
    state["turn"].update(phase="Phase_Main1", step="")
    state["battlefield"] = [card for card in state["battlefield"] if card["instance_id"] != SLICKSHOT_ID]
    return state


def test_fixed_damage_removal_retargets_only_to_a_walker_it_kills():
    planner = ActionPlanner(_Backend(""), timeout=5, land_drop_first=False)
    planner._decision_source_oracle = lambda decision, state: WRATH.lower()
    planner._last_decision_reasoning = ""
    decision = SimpleNamespace(
        options=[SimpleNamespace(option_id=f"tgt:{iid}") for iid in (HORTIMANCER_ID, AJANI_ID)]
    )
    # Loyalty 4: the 4 damage kills Ajani, not Hortimancer.
    assert planner._prefer_lethal_damage_target(decision, removal_board(4), [f"tgt:{HORTIMANCER_ID}"]) == [
        f"tgt:{AJANI_ID}"
    ]
    # Loyalty 5: nothing dies, Ajani is no "kill instead"; the spell is kept.
    assert planner._prefer_lethal_damage_target(decision, removal_board(5), [f"tgt:{HORTIMANCER_ID}"]) == [
        DECLINE_DECISION
    ]
    # The model's own pick of a walker it only damages stands.
    assert planner._prefer_lethal_damage_target(decision, removal_board(5), [f"tgt:{AJANI_ID}"]) == [
        f"tgt:{AJANI_ID}"
    ]


def test_any_target_burn_calls_a_walker_a_kill_only_at_its_loyalty():
    targets = (1, 2, AJANI_ID, HORTIMANCER_ID)
    for loyalty, expected in ((2, f"tgt:{AJANI_ID}"), (3, "tgt:1")):
        planner = ActionPlanner(_Backend(""), timeout=5, land_drop_first=False)
        planner._last_decision_reasoning = "Shock the Hortimancer."
        state = shock_window(ajani_loyalty=loyalty)
        chosen = planner._prefer_lethal_damage_target(
            _targets(targets, "Shock", SHOCK_ID), state, [f"tgt:{HORTIMANCER_ID}"]
        )
        assert chosen == [expected], loyalty
        reasoning = planner.get_decision_reasoning(chosen)
        if expected == "tgt:1":
            assert "to the opponent's face" in reasoning
        else:
            assert "Ajani Resolute dies instead" in reasoning
