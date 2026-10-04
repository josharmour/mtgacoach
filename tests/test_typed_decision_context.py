"""Typed decisions see the full board, and fights read as harmful.

2026-09-24 (real match, lost):
- Stuck on three lands, the autopilot passed turns 7 and 9 with Archdruid's
  Charm castable. The priority-window prompt listed hand cards by name only,
  so the model could not see that the charm fetches a land.
- Kogla's "it fights up to one target creature you don't control" read as a
  beneficial effect; the model's correct pick (an opponent's creature) was
  overridden and the choice went to the player.
"""

from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import build_pending_decision

LOCAL, OPP = 2, 1


class _CapturingBackend:
    def __init__(self, reply: str):
        self.reply = reply
        self.prompts: list[str] = []

    def complete(self, system, user, *args, **kwargs):
        self.prompts.append(user)
        return self.reply


def _state(hand=(), battlefield=(), stack=()) -> dict:
    return {
        "turn": {"turn_number": 7, "phase": "Phase_Main1", "active_player": LOCAL, "priority_player": LOCAL},
        "local_seat_id": LOCAL,
        "players": [
            {"seat_id": LOCAL, "is_local": True, "life_total": 12},
            {"seat_id": OPP, "is_local": False, "life_total": 25},
        ],
        "hand": list(hand),
        "battlefield": list(battlefield),
        "stack": list(stack),
        "graveyard": [],
        "legal_actions": [],
    }


def test_priority_window_prompt_carries_card_text():
    charm = {
        "instance_id": 345,
        "grp_id": 1,
        "name": "Archdruid's Charm",
        "type_line": "Instant",
        "mana_cost": "{G}{G}{G}",
        "oracle_text": "Choose one — Search your library for a creature or land card and reveal it. "
        "If it's a land card, you may put it onto the battlefield tapped.",
        "owner_seat_id": LOCAL,
        "controller_seat_id": LOCAL,
    }
    decision = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "ActionsAvailable",
            "can_pass": True,
            "actions": [
                {"actionType": "ActionType_Cast", "grpId": 1, "instanceId": 345, "hasAutoTap": True},
                {"actionType": "ActionType_Pass"},
            ],
        },
        resolve_name=lambda grp: "Archdruid's Charm",
    )
    backend = _CapturingBackend('{"option_ids": ["idx:0"]}')
    ActionPlanner(backend=backend)._llm_decision_options(decision, _state(hand=[charm]))
    assert "put it onto the battlefield tapped" in backend.prompts[0]


def test_fight_trigger_is_harmful_so_an_enemy_pick_stands():
    kogla_trigger = {
        "instance_id": 900,
        "name": "Kogla, the Titan Ape",
        "object_kind": "ABILITY",
        "oracle_text": "When Kogla, the Titan Ape enters the battlefield, it fights up to one target "
        "creature you don't control.",
        "controller_seat_id": LOCAL,
    }
    squirrel = {
        "instance_id": 803,
        "name": "Squirrel",
        "type_line": "Creature — Squirrel",
        "power": 1,
        "toughness": 1,
        "controller_seat_id": OPP,
        "owner_seat_id": OPP,
    }
    state = _state(battlefield=[squirrel], stack=[kogla_trigger])
    state["decision_context"] = {"source_id": 900}
    decision = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "SelectTargets",
            "source_card": "Kogla, the Titan Ape",
            "target_candidates": [{"targetInstanceId": 803, "grpId": 0}],
            "target_selections": [{"minTargets": 0, "maxTargets": 1}],
        }
    )
    planner = ActionPlanner(backend=_CapturingBackend('{"option_ids": ["tgt:803"]}'))
    assert planner._decision_source_is_harmful(decision, state) is True
    assert planner.plan_decision_options(decision, state) == ["tgt:803"]


def test_commander_choice_receives_current_deck_strategy_and_engine_text():
    hobbits = {
        "instance_id": 243,
        "grp_id": 103511,
        "name": "The Notary Hobbits",
        "type_line": "Legendary Creature — Halfling Advisor",
        "mana_cost": "{3}{G}{G}",
        "oracle_text": "When The Notary Hobbits enter, if they're not a token, create two tokens "
        "that are copies of them, except the tokens aren't legendary.\n"
        "{oT}: Add {oC} for each Halfling you control.",
        "owner_seat_id": LOCAL,
        "controller_seat_id": LOCAL,
    }
    state = _state()
    state["command"] = [hobbits]
    decision = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "ActionsAvailable",
            "can_pass": True,
            "actions": [
                {"actionType": "ActionType_Cast", "instanceId": 243, "hasAutoTap": True},
                {"actionType": "ActionType_Pass"},
            ],
        },
        resolve_instance=lambda iid: "The Notary Hobbits",
    )
    strategy = None
    backend = _CapturingBackend('{"option_ids": ["idx:0"]}')
    planner = ActionPlanner(backend, deck_strategy_fn=lambda: strategy)
    # The analysis finishes after autopilot creation; read it at decision time.
    strategy = "Deploy The Notary Hobbits to fund the expensive creatures with their token copies."
    planner.set_game_plan("GAME PLAN: Establish the commander mana engine.")
    assert planner.plan_decision_options(decision, state) == ["idx:0"]
    prompt = backend.prompts[-1]
    option = next(line for line in prompt.splitlines() if line.startswith("- idx:0:"))
    assert "YOUR COMMANDER" in option
    assert "Arena confirms payable now" in option
    assert "create two tokens" in prompt
    assert "for each Halfling you control" in prompt
    assert strategy in prompt
    assert "GAME PLAN: Establish the commander mana engine." in prompt

    # Clearing the coach's analysis at the next match must clear the prompt too.
    strategy = None
    planner.clear_game_plan()
    planner._llm_decision_options(decision, state)
    assert "DECK STRATEGY:" not in backend.prompts[-1]
    assert "GAME PLAN:" not in backend.prompts[-1]


def test_legacy_action_prompt_also_receives_deck_strategy():
    strategy = "Build the Halfling mana engine before deploying the top end."
    planner = ActionPlanner(_CapturingBackend("{}"), deck_strategy_fn=lambda: strategy)
    assert strategy in planner._build_action_prompt(_state(), "new_turn", ["Pass"])


def test_planner_board_uses_controller_and_normalized_creature_types():
    stolen = {
        "instance_id": 982,
        "name": "Prosperous Innkeeper",
        "owner_seat_id": LOCAL,
        "controller_seat_id": OPP,
        "type_line": "Creature — Halfling Citizen",
        "card_types": ["Creature"],
        "power": 1,
        "toughness": 1,
    }
    borrowed = {
        **stolen,
        "instance_id": 983,
        "name": "Borrowed creature",
        "owner_seat_id": OPP,
        "controller_seat_id": LOCAL,
    }
    planner = ActionPlanner(_CapturingBackend("{}"))
    prompt = planner._decision_game_context(_state(battlefield=[stolen, borrowed]))
    own, opposing = prompt.split("YOUR BOARD:", 1)[1].split("OPP BOARD:", 1)
    assert "Borrowed creature 1/1" in own
    assert "Prosperous Innkeeper" not in own
    assert "Prosperous Innkeeper 1/1" in opposing
    assert "NOT A CREATURE" not in prompt
