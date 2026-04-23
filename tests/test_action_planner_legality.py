"""Regression tests for ActionPlanner._is_action_legal combat declarations.

Without these, the planner would reject valid LLM block/attack plans and
fall back to "Done (confirm blockers)" — passing the entire attack through
without blocking. See issue #67-class reports for the original failure.
"""

from arenamcp.action_planner import ActionPlanner, ActionType, GameAction


def _planner() -> ActionPlanner:
    return ActionPlanner.__new__(ActionPlanner)


def test_declare_blockers_plan_accepts_single_ooze_blocking():
    plan = GameAction(
        action_type=ActionType.DECLARE_BLOCKERS,
        blocker_assignments={"Ooze #1": "Goblin Shaman"},
    )
    legal = [
        "Block with: Ooze #1",
        "Block with: Ooze #2",
        "Done (confirm blockers)",
    ]
    assert _planner()._is_action_legal(plan, legal) is True


def test_declare_blockers_plan_accepts_empty_blockers_as_legal():
    plan = GameAction(action_type=ActionType.DECLARE_BLOCKERS, blocker_assignments={})
    legal = ["Block with: Ooze #1", "Done (confirm blockers)"]
    assert _planner()._is_action_legal(plan, legal) is True


def test_declare_blockers_plan_rejected_when_blocker_not_in_legal_set():
    plan = GameAction(
        action_type=ActionType.DECLARE_BLOCKERS,
        blocker_assignments={"Phantom": "Goblin Shaman"},
    )
    legal = ["Block with: Ooze #1", "Done (confirm blockers)"]
    assert _planner()._is_action_legal(plan, legal) is False


def test_declare_blockers_plan_rejected_outside_combat_context():
    plan = GameAction(
        action_type=ActionType.DECLARE_BLOCKERS,
        blocker_assignments={"Ooze #1": "Goblin"},
    )
    legal = ["Cast Slime Against Humanity [OK]", "Pass"]
    assert _planner()._is_action_legal(plan, legal) is False


def test_declare_attackers_plan_accepts_subset_of_legal_attackers():
    plan = GameAction(
        action_type=ActionType.DECLARE_ATTACKERS,
        attacker_names=["Ooze #1", "Ooze #3"],
    )
    legal = [
        "Attack with: Ooze #1",
        "Attack with: Ooze #2",
        "Attack with: Ooze #3",
        "Done (confirm attackers)",
    ]
    assert _planner()._is_action_legal(plan, legal) is True


def test_declare_attackers_plan_rejected_when_attacker_missing_from_legal():
    plan = GameAction(
        action_type=ActionType.DECLARE_ATTACKERS,
        attacker_names=["Ooze #1", "Phantom"],
    )
    legal = ["Attack with: Ooze #1", "Done (confirm attackers)"]
    assert _planner()._is_action_legal(plan, legal) is False


def test_cast_spell_validation_still_matches_on_card_name():
    plan = GameAction(action_type=ActionType.CAST_SPELL, card_name="Lightning Bolt")
    legal = ["Cast Lightning Bolt [OK]", "Pass"]
    assert _planner()._is_action_legal(plan, legal) is True


def test_cast_spell_rejected_when_name_not_in_legal():
    plan = GameAction(action_type=ActionType.CAST_SPELL, card_name="Counterspell")
    legal = ["Cast Lightning Bolt [OK]", "Pass"]
    assert _planner()._is_action_legal(plan, legal) is False


def test_accept_legal_action_converts_to_click_button_accept():
    legal = _planner()._legal_action_to_action("Accept (yes)")
    assert legal is not None
    assert legal.action_type == ActionType.CLICK_BUTTON
    assert legal.card_name == "accept"


def test_decline_legal_action_converts_to_click_button_decline():
    legal = _planner()._legal_action_to_action("Decline (no)")
    assert legal is not None
    assert legal.action_type == ActionType.CLICK_BUTTON
    assert legal.card_name == "decline"


def test_optional_action_rules_engine_emits_accept_decline():
    """Regression for bug #67: MTGA's commander-to-command-zone prompt
    arrives as an OptionalActionMessage. Before this fix rules_engine had
    no branch for "optional_action" and fell through to the priority check,
    returning ["Wait (Opponent has priority)"] — so the autopilot just
    passed priority on a request that doesn't accept a pass.
    """
    from arenamcp.rules_engine import RulesEngine

    game_state = {
        "players": [
            {"seat_id": 1, "is_local": True, "life": 20},
            {"seat_id": 2, "is_local": False, "life": 20},
        ],
        "turn": {
            "turn_number": 5,
            "phase": "Phase_Main1",
            "step": "",
            "active_player": 2,
            "priority_player": 2,
        },
        "decision_context": {"type": "optional_action", "prompt": "Move to command zone?"},
        "hand": [],
        "battlefield": [],
        "stack": [],
    }
    actions = RulesEngine.get_legal_actions(game_state)
    assert any("accept" in a.lower() for a in actions)
    assert any("decline" in a.lower() for a in actions)
    assert not any("wait" in a.lower() for a in actions)


# ---------------------------------------------------------------------------
# Regression tests for issue #117 / #118: the v2.1.4 rules-engine change
# started annotating legal "Attack with: ..." lines with a "(P/T)" suffix.
# When the LLM call fails (proxy 402, parse error, transient API blip) the
# planner falls back to parsing the legal-action string back into a
# GameAction. Without stripping the decoration, the bridge submitter sees
# attacker_names=["Veteran Survivor (4/3)"] which doesn't match anything on
# the battlefield, the action fails, and the autopilot loops forever.
# ---------------------------------------------------------------------------


def test_legal_action_to_action_strips_pt_suffix_from_attackers():
    action = _planner()._legal_action_to_action("Attack with: Veteran Survivor (4/3)")
    assert action is not None
    assert action.action_type == ActionType.DECLARE_ATTACKERS
    assert action.attacker_names == ["Veteran Survivor"]


def test_legal_action_to_action_strips_zero_power_warning_tag():
    action = _planner()._legal_action_to_action(
        "Attack with: Tin Rebel (0/2) [0 POWER — attacking deals 0 damage]"
    )
    assert action is not None
    assert action.attacker_names == ["Tin Rebel"]


def test_legal_action_to_action_strips_pt_from_multiple_attackers():
    action = _planner()._legal_action_to_action(
        "Attack with: Veteran Survivor (4/3), Page, Loose Leaf (2/1)"
    )
    assert action is not None
    # Comma split happens before strip. "Page, Loose Leaf" trips the naive
    # split (the card name itself contains a comma) — but at minimum we must
    # NOT preserve the "(P/T)" decoration on whatever we DID extract.
    for name in action.attacker_names:
        assert "(" not in name, f"P/T decoration leaked into {name!r}"


def test_legal_action_to_action_strips_decoration_from_select_target():
    action = _planner()._legal_action_to_action("Select target: Escape Tunnel [OK]")
    assert action is not None
    assert action.action_type == ActionType.SELECT_TARGET
    assert action.target_names == ["Escape Tunnel"]


def test_legal_action_to_action_strips_decoration_from_block_with():
    action = _planner()._legal_action_to_action("Block with: Ooze #1 (2/2)")
    assert action is not None
    assert action.action_type == ActionType.DECLARE_BLOCKERS
    # Disambiguation suffix "#1" must be preserved (it identifies which copy
    # of the card to block with), but the (P/T) must be stripped.
    assert action.blocker_assignments == {"Ooze #1": ""}


def test_legal_action_to_action_leaves_undecorated_names_alone():
    action = _planner()._legal_action_to_action("Cast Veteran Survivor")
    assert action is not None
    assert action.action_type == ActionType.CAST_SPELL
    assert action.card_name == "Veteran Survivor"
