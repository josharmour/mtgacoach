"""#84/#262 ae86ca8d T10: Headliner sacrifices from three bodies, deals two.

Minimal factual state from monitoring/ae86ca8d-headliner-{candidate,raw}.json:
activation567, ability622/parent567, target560 at6/6 with2 damage, two other
creatures552/553. Raw annotation720 deals2 and leaves560 at4 marked damage.
"""

from unittest.mock import Mock

from tests.test_tactical_timing_backlog import card, menu

from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import DecisionOption
from arenamcp.play_safety import claim_kill_reach, claimed_kills, damage_effect

COST = "{1}{R}, Sacrifice this"
EFFECT = "It deals damage equal to the number of creatures you control to target creature."
ORACLE = "Mobilize 1\n" + COST + ": " + EFFECT
ACTIVATION_REASON = "Board changed: Hexhaven now has 2 damage marked, so Stadium Headliner's sacrifice damage (4 creatures = 4) kills the 6/6 threat."
TARGET_REASON = "Stadium Headliner's 4 damage (4 creatures) kills the 6/6 Hexhaven, removing their biggest threat and damage-trigger engine."


def factual_board():
    source = card(
        567,
        "Stadium Headliner",
        ORACLE,
        "Creature — Goblin Warrior",
        "{R}",
        power=2,
        toughness=2,
        card_types=["CardType_Creature"],
    )
    state = {
        "local_seat_id": 1,
        "turn": {"turn_number": 10, "active_player": 1, "phase": "Main2"},
        "players": [{"seat_id": 1, "life_total": 16}, {"seat_id": 2, "life_total": 6}],
        "battlefield": [
            card(
                552,
                "Ninja Turtle Spirit",
                "",
                "Creature — Ninja Turtle Spirit",
                power=2,
                toughness=2,
                card_types=["CardType_Creature"],
            ),
            card(
                553,
                "Ninja Turtle Spirit",
                "",
                "Creature — Ninja Turtle Spirit",
                power=2,
                toughness=2,
                card_types=["CardType_Creature"],
            ),
            source,
            card(
                560,
                "Hexhaven Invigorator",
                "",
                "Creature — Chimera Horror",
                "{5}{G}",
                seat=2,
                power=6,
                toughness=6,
                damage=2,
                damaged_this_turn=True,
                card_types=["CardType_Creature"],
            ),
        ],
        "hand": [],
        "stack": [],
    }
    return state, source


def test_actual_post_sacrifice_count_is_two_not_three_or_four():
    state, source = factual_board()
    assert damage_effect(source, state, activation=True) == (2, "target")
    assert claim_kill_reach(source, state, activation=True) == ("damage", 2)
    assert damage_effect(source, activation=True) is None
    # The calculation uses rules, not the Headliner name.
    source["name"] = "Another self-sacrificing creature"
    assert damage_effect(source, state, activation=True) == (2, "target")


def test_pending_parent_identity_never_double_subtracts_paid_source():
    state, source = factual_board()
    ability = {
        "instance_id": 622,
        "parent_instance_id": 567,
        "controller_seat_id": 1,
        "object_kind": "ABILITY",
        "oracle_text": "{o1or}, sacrifice cardname: it deals damage equal to the number of creatures you control to target creature.",
    }
    state["stack"] = [ability]
    state["decision_context"] = {"source_id": 622, "source_parent_instance_id": 567}
    assert damage_effect(ability, state, activation=True) == (2, "target")
    state["battlefield"].remove(source)
    state["graveyard"] = [{**source, "instance_id": 625}]
    assert damage_effect(ability, state, activation=True) == (2, "target")
    state["battlefield"].append(card(700, "New token", "", "Creature", power=1, toughness=1))
    assert damage_effect(ability, state, activation=True) == (3, "target")


def test_unknown_body_type_or_source_or_extra_sacrifice_stays_unknown():
    state, source = factual_board()
    state["battlefield"][1] = {"instance_id": 553, "controller_seat_id": 1, "name": "Unknown"}
    assert damage_effect(source, state, activation=True) is None
    state["battlefield"][1]["type_line"] = "Unknown"
    assert damage_effect(source, state, activation=True) is None
    state["battlefield"][1]["card_types"] = ["CardType_Unknown"]
    assert damage_effect(source, state, activation=True) is None
    state, source = factual_board()
    state["battlefield"].remove(source)
    assert damage_effect(source, state, activation=True) is None
    state, source = factual_board()
    for cost in ("{1}{R}, Sacrifice another creature", "{1}{R}, Sacrifice this and another creature"):
        assert (
            damage_effect(source, state, activation=True, metadata={"ability_text": cost + ": " + EFFECT})
            is None
        )


def test_current_types_phasing_and_opponent_bodies_control_count():
    state, source = factual_board()
    state["battlefield"][0]["card_types"] = ["CardType_Enchantment"]
    assert damage_effect(source, state, activation=True) == (1, "target")
    state["battlefield"][1]["is_phased_out"] = True
    assert damage_effect(source, state, activation=True) == (0, "target")


def test_visible_count_changing_death_trigger_is_not_silently_ignored():
    state, source = factual_board()
    state["battlefield"][0]["oracle_text"] = (
        "Whenever another creature you control dies, create a 1/1 Spirit creature token."
    )
    assert damage_effect(source, state, activation=True) is None


def test_exact_recorded_claims_bind_unique_prefix_and_size():
    state, _ = factual_board()
    target = state["battlefield"][-1]
    assert claimed_kills(ACTIVATION_REASON, [target]) == [target]
    assert claimed_kills(TARGET_REASON, [target]) == [target]
    for reason in (
        ACTIVATION_REASON.replace("kills", "cannot kill"),
        TARGET_REASON.replace("kills", "does not kill"),
        "Hexhaven attacks. Headliner kills the 6/6 threat.",
    ):
        assert claimed_kills(reason, [target]) == []
    same_size = {**target, "instance_id": 900, "name": "Other Creature"}
    same_prefix = {**target, "instance_id": 901, "name": "Hexhaven Other", "power": 4, "toughness": 4}
    assert claimed_kills(ACTIVATION_REASON, [target, same_size]) == []
    assert claimed_kills(ACTIVATION_REASON, [target, same_prefix]) == []


def test_actual_activation_guard_rejects_false_kill_and_preserves_true_kill(monkeypatch):
    state, source = factual_board()
    decision = menu(
        DecisionOption(
            "idx:1",
            "Activate: Stadium Headliner",
            True,
            {"actionType": "ActionType_Activate", "instanceId": 567, "abilityGrpId": 188824},
        )
    )
    planner = ActionPlanner(backend=Mock())
    planner._last_decision_reasoning = ACTIVATION_REASON
    planner._last_decision_trace = {}
    monkeypatch.setattr("arenamcp.action_planner.line_casts_option", lambda *args: "")
    assert planner._reject_false_kill_claim(decision, state, ["idx:1"]) == []
    assert planner._last_decision_trace["false_kill_claim"] == ["Hexhaven Invigorator"]
    # Two additional live bodies make four damage actually sufficient.
    for iid in (701, 702):
        state["battlefield"].append(card(iid, "Token", "", "Creature", power=1, toughness=1))
    assert planner._reject_false_kill_claim(decision, state, ["idx:1"]) == ["idx:1"]
    # A deliberate non-killing use is preserved; this is not a blanket ban.
    state, source = factual_board()
    planner._last_decision_reasoning = (
        "Use Headliner's damage to set up removal after combat; Hexhaven survives this ability."
    )
    assert planner._reject_false_kill_claim(decision, state, ["idx:1"]) == ["idx:1"]


def test_effect_only_metadata_and_other_pending_effects_are_not_exact():
    state, source = factual_board()
    assert damage_effect(source, state, activation=True, metadata={"ability_text": EFFECT}) is None
    state["stack"] = [
        {
            "instance_id": 800,
            "object_kind": "ABILITY",
            "oracle_text": "Create two creature tokens.",
            "controller_seat_id": 1,
        }
    ]
    assert damage_effect(source, state, activation=True) is None


def test_visible_damage_multiplier_keeps_alternative_line_available():
    state, source = factual_board()
    state["battlefield"].append(
        card(
            800,
            "Damage amplifier",
            "If a source you control would deal damage to an opponent's creature, it deals that much damage plus 2 instead.",
            "Enchantment",
        )
    )
    assert damage_effect(source, state, activation=True) is None


def test_corrected_contextual_claim_is_removed_when_other_value_keeps_play():
    from arenamcp.play_safety import strip_kill_claims

    state, _ = factual_board()
    cleaned = strip_kill_claims(ACTIVATION_REASON, [state["battlefield"][-1]])
    assert "kills the 6/6 threat" not in cleaned


def test_target_source_resolves_effect_only_text_through_exact_live_parent():
    from arenamcp.activation_damage import target_activation_damage

    state, source = factual_board()
    ability = {
        "instance_id": 622,
        "parent_instance_id": 567,
        "controller_seat_id": 1,
        "object_kind": "ABILITY",
        "oracle_text": EFFECT,
        "name": "Stadium Headliner ability",
    }
    state["stack"] = [ability]
    state["decision_context"] = {"source_id": 622, "source_parent_instance_id": 567}
    assert target_activation_damage(state, 622) == (2, "target")
    assert target_activation_damage(state, 999) is None
    # Do not infer the omitted sacrifice cost after the parent leaves.
    state["battlefield"].remove(source)
    assert target_activation_damage(state, 622) is None
    ability["oracle_text"] = COST + ": " + EFFECT
    assert target_activation_damage(state, 622) == (2, "target")


def test_target_source_ambiguous_parent_activations_and_unknown_body_abstain():
    from arenamcp.activation_damage import target_activation_damage

    state, source = factual_board()
    state["stack"] = [
        {
            "instance_id": 622,
            "parent_instance_id": 567,
            "controller_seat_id": 1,
            "object_kind": "ABILITY",
            "oracle_text": EFFECT,
        }
    ]
    state["decision_context"] = {"source_id": 622}
    source["oracle_text"] += "\n{R}: " + EFFECT
    assert target_activation_damage(state, 622) is None
    source["oracle_text"] = ORACLE
    state["battlefield"][1] = {"instance_id": 553, "name": "Unknown", "controller_seat_id": 1}
    assert target_activation_damage(state, 622) is None


def test_opposing_ability_does_not_count_our_creatures():
    state, source = factual_board()
    source["controller_seat_id"] = 2
    assert damage_effect(source, state, activation=True) is None


def test_recorded_phase_main2_priority_pipeline_does_not_execute_false_kill():
    import json

    from tests.test_burn_targeting_20261019 import _planner

    from arenamcp.decisions import PendingDecision

    state, _ = factual_board()
    # Exact phase/request from ae86ca8d req252/350 (target request uses Main2).
    state["turn"]["phase"] = "Phase_Main2"
    state["turn"]["step"] = ""
    state["players"][0]["lands_played"] = 1
    state["hand"] = [
        card(
            404,
            "Seam Rip",
            "When this enchantment enters, exile target nonland permanent an opponent controls with mana value 2 or less until this enchantment leaves the battlefield.",
            "Enchantment",
            "{W}",
        )
    ]
    state["battlefield"].extend(
        card(iid, "Mountain", "{T}: Add {R}.", "Basic Land — Mountain") for iid in (566, 405, 415, 416, 417)
    )
    decision = PendingDecision(
        (252, 350),
        "ActionsAvailable",
        (
            DecisionOption(
                "idx:0",
                "Cast Seam Rip",
                True,
                {"actionType": "ActionType_Cast", "instanceId": 404, "hasAutoTap": True},
            ),
            DecisionOption(
                "idx:1",
                "Activate: Stadium Headliner [" + COST + ": " + EFFECT + "]",
                True,
                {
                    "actionType": "ActionType_Activate",
                    "instanceId": 567,
                    "abilityGrpId": 188824,
                    "hasAutoTap": True,
                    "manaCost": [{"color": ["Generic"], "count": 1}, {"color": ["Red"], "count": 1}],
                },
            ),
            DecisionOption("pass", "Pass"),
        ),
        can_pass=True,
    )
    planner = _planner(json.dumps({"option_ids": ["idx:1"], "reasoning": ACTIVATION_REASON}))
    assert planner.plan_decision_options(decision, state) != ["idx:1"]
    assert planner.get_last_decision_trace()["false_kill_claim"] == ["Hexhaven Invigorator"]
