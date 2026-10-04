"""Deck learning, provenance and lifecycle, independently of any named commander."""

import json
import threading
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.coach import CoachEngine
from arenamcp.deck_strategy import DECK_ANALYSIS_PROMPT, DeckPlaybook, commander_ids, deck_identity
from arenamcp.game_plan import GamePlanManager
from arenamcp.standalone_deck import _DeckAnalysisMixin


def deck_case(kind="replay"):
    """Small decks with opposing policies; fixture cards aren't real card claims."""
    card = {
        "name": "Fixture Returning Captain",
        "mana_cost": "{3}{G}{G}",
        "type_line": "Legendary Creature — Scout",
        "oracle_text": "When this creature enters, if it isn't a token, create two tokens that are copies of it.",
    }
    preference = "Trade the recoverable original, then recast it to rebuild token copies."
    condition = "Equal combat outcomes and a profitable recast payable from surviving sources after tax."
    if kind == "preserve":
        card["name"] = "Fixture Growing Captain"
        card["oracle_text"] = "Whenever you cast a creature spell, put a +1/+1 counter on this creature."
        preference = "Preserve the commander and its accumulated counters; spend a replaceable token."
        condition = "The commander retains counters and a token supplies the same safe block."
    elif kind == "spells":
        card["name"] = "Fixture Spell Engine"
        card["type_line"] = "Enchantment"
        card["oracle_text"] = "Whenever you cast your second spell each turn, draw a card."
        preference = "Sequence two affordable spells together to draw, preserving necessary interaction."
        condition = "The engine is present and both spells have useful legal effects and combined payment."
    catalog = {
        1001: card,
        1002: {
            "name": "Forest",
            "type_line": "Basic Land — Forest",
            "mana_cost": "",
            "oracle_text": "({T}: Add {G}.)",
        },
    }
    commanders = [] if kind == "spells" else [1001]
    state = {
        "match_id": "deck-test",
        "local_seat_id": 1,
        "deck_cards": [1002] * 20 + ([] if commanders else [1001]),
        "commander_grp_ids": commanders,
        "players": [{"seat_id": 1, "is_local": True, "commander_ids": [50] if commanders else []}],
        "turn": {"turn_number": 5, "active_player": 2, "phase": "Combat"},
        "battlefield": [
            {**card, "grp_id": 1001, "instance_id": 50, "owner_seat_id": 1, "controller_seat_id": 1}
        ],
        "decision_context": {"type": "declare_blockers"},
    }
    response = {
        "archetype": f"Fixture {kind} engine",
        "primary_plan": preference,
        "backup_plan": "Develop remaining resources after the engine is disrupted.",
        "card_roles": {
            "1001": "Conditional engine; preserve or replay according to its rules.",
            "1002": "Green mana and land drops.",
        },
        "commanders": [
            {
                "card": 1001,
                "deployment": "Deploy when it advances the engine.",
                "ongoing_value": "Compare remaining abilities with available substitutes.",
                "replay_value": "Re-entry can rebuild only what the cited trigger supplies; counters are lost.",
                "resource_comparison": {
                    "starting_resources": "Commander and a replaceable body.",
                    "preserve_original": "Spend substitute; retain commander and invested resources.",
                    "spend_and_recover": "Spend commander; pay recovery and add supported trigger value.",
                    "decision_test": condition,
                },
                "preserve_or_reuse": preference,
                "constraints": "Respect tax, timing, colored mana and lost counters.",
            }
        ]
        if commanders
        else [],
        "mechanisms": [
            {
                "id": "engine",
                "cards": [1001],
                "evidence": [{"card": 1001, "rule": 1}],
                "effect": preference,
                "requires": [condition],
                "avoid": ["Spending mana needed to survive."],
            }
        ],
        "decision_rules": [
            {
                "id": "resource_trade",
                "decisions": ["combat", "commander_zone"] if commanders else ["development"],
                "mechanisms": ["engine"],
                "baseline": "Preserve material and spend replaceable resources first.",
                "when": condition,
                "prefer": preference,
                "unless": "The alternative prevents lethal or recovery is unaffordable or too slow.",
            }
        ],
        "phases": {
            phase: f"{phase}: develop the supported engine." for phase in ("early", "mid", "late", "recovery")
        },
        "spoken_summary": preference,
    }
    return state, catalog, response


def playbook_for(kind="replay"):
    state, catalog, response = deck_case(kind)
    return DeckPlaybook.parse(json.dumps(response), catalog, commander_ids(state), deck_identity(state))


@pytest.fixture
def deck(monkeypatch):
    state, catalog, response = deck_case()
    monkeypatch.setattr("arenamcp.match_context._local_card", lambda gid, epoch: catalog[gid])
    return state, catalog, response


@pytest.mark.parametrize("kind", ["replay", "preserve", "spells"])
def test_stored_rules_support_opposing_policies_and_noncommander_decks(kind):
    state, catalog, response = deck_case(kind)
    book = DeckPlaybook.parse(json.dumps(response), catalog, commander_ids(state), deck_identity(state))
    rendered = book.render()
    assert response["decision_rules"][0]["prefer"] in rendered
    assert response["decision_rules"][0]["unless"] in rendered
    assert catalog[1001]["oracle_text"] in rendered
    assert book.data["card_roles"]["1002"] in rendered
    if kind == "spells":
        assert "COMMANDER " not in rendered
    assert "Notary" not in DECK_ANALYSIS_PROMPT


@pytest.mark.parametrize(
    "bad",
    [
        "missing_role",
        "wrong_commander",
        "missing_policy",
        "wrong_card",
        "invented_quote",
        "missing_evidence",
        "missing_limits",
        "unlinked_rule",
        "unknown_scope",
    ],
)
def test_invalid_or_ungrounded_analysis_is_not_stored(deck, bad):
    state, catalog, response = deck
    if bad == "missing_role":
        del response["card_roles"]["1001"]
    elif bad == "wrong_commander":
        response["commanders"][0]["card"] = 1002
    elif bad == "missing_policy":
        response["commanders"] = []
    elif bad == "wrong_card":
        response["mechanisms"][0]["cards"] = [9999]
    elif bad == "invented_quote":
        response["mechanisms"][0]["evidence"][0]["rule"] = 999
    elif bad == "missing_evidence":
        response["mechanisms"][0]["evidence"] = []
    elif bad == "missing_limits":
        response["mechanisms"][0]["avoid"] = []
    elif bad == "unlinked_rule":
        response["decision_rules"][0]["mechanisms"] = ["made_up"]
    else:
        response["decision_rules"][0]["decisions"] = ["made_up"]
    with pytest.raises(ValueError):
        DeckPlaybook.parse(json.dumps(response), catalog, commander_ids(state))


def test_full_analysis_receives_costs_types_rules_and_explicit_commander_after_zone_change(deck):
    state, catalog, response = deck
    del state["commander_grp_ids"]
    backend = Mock()
    backend.complete.return_value = json.dumps(response)
    coach = CoachEngine(backend)
    assert coach.analyze_deck(state)
    prompt = backend.complete.call_args.args[1]
    assert "DESIGNATED COMMANDER CARD IDS: [1001]" in prompt
    assert "{3}{G}{G}" in prompt and "Scout" in prompt
    assert catalog[1001]["oracle_text"] in prompt
    assert coach._deck_playbook.data["decision_rules"] == response["decision_rules"]
    assert not coach._deck_strategy_pending
    assert backend.complete.call_count == 2
    assert "AUDIT THE PROPOSED PLAYBOOK" in backend.complete.call_args.args[1]


def test_one_schema_repair_then_publish_or_report_failure(deck):
    state, _, response = deck
    backend = Mock()
    backend.complete.side_effect = ["Discovery notes", '{"archetype":"too brief"}', json.dumps(response)]
    coach = CoachEngine(backend)
    assert coach.analyze_deck(state)
    assert "failed validation" in backend.complete.call_args_list[2].args[1]
    assert backend.complete.call_count == 3
    backend.complete.side_effect = ["Discovery notes"] + ['{"archetype":"too brief"}'] * 2
    assert coach.analyze_deck(state) is None
    assert coach._deck_playbook is None and coach._deck_strategy is None
    assert coach._deck_analysis_error and not coach._deck_strategy_pending


def test_only_audited_policy_is_published_and_failed_audit_leaves_no_stale_plan(deck):
    state, _, response = deck
    draft = deepcopy(response)
    draft["commanders"][0]["preserve_or_reuse"] = "An unreviewed policy."
    backend = Mock()
    backend.complete.side_effect = [json.dumps(draft), json.dumps(response)]
    coach = CoachEngine(backend)
    assert coach.analyze_deck(state)
    assert "An unreviewed policy." not in coach._deck_strategy
    audit_prompt = backend.complete.call_args.args[1]
    assert "An unreviewed policy." in audit_prompt
    assert "A spent ETB is not a continuing benefit" in audit_prompt
    backend.complete.side_effect = [json.dumps(draft), "[BACKEND ERROR] timed out"]
    assert coach.analyze_deck(state) is None
    assert coach._deck_playbook is None and coach._deck_strategy is None


def test_combat_sees_resource_recovery_rules_and_live_tax_even_if_rule_scope_is_sacrifice(deck):
    state, catalog, response = deck
    response["decision_rules"][0]["decisions"] = ["sacrifice", "commander_zone"]
    book = DeckPlaybook.parse(json.dumps(response), catalog, commander_ids(state))
    state["commander_casts"] = {"1001": 2}
    prompt = book.decision_context(state)
    assert "RULE resource_trade" in prompt
    assert "printed {3}{G}{G} plus commander tax {4}" in prompt
    state["commander_casts"] = {}
    assert "UNKNOWN; do not assume zero" in book.decision_context(state)


def test_late_old_deck_analysis_cannot_publish_or_clear_new_pending_work(deck):
    state, _, response = deck
    entered, release = threading.Event(), threading.Event()

    def complete(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return json.dumps(response)

    coach = CoachEngine(Mock(complete=complete))
    worker = threading.Thread(target=coach.analyze_deck, args=(state,))
    worker.start()
    assert entered.wait(3)
    coach.clear_deck_strategy()
    coach.begin_deck_analysis("different-deck")
    release.set()
    worker.join(3)
    assert not worker.is_alive()
    assert coach._deck_playbook is None and coach._deck_strategy is None
    assert coach._deck_strategy_pending
    assert coach._deck_analysis_identity == "different-deck"


def test_identity_is_stable_across_draws_and_commander_zone_changes_but_not_deck_swaps(deck):
    state, _, _ = deck
    identity = deck_identity(state)
    changed = deepcopy(state)
    changed["command"] = changed.pop("battlefield")
    changed["hand"] = [{"grp_id": 1002}]
    changed["commander_casts"] = {1001: 4}
    assert deck_identity(changed) == identity
    changed["commander_grp_ids"] = [1002]
    assert deck_identity(changed) != identity
    changed["commander_grp_ids"] = [1001]
    changed["deck_cards"].append(1002)
    assert deck_identity(changed) != identity


def test_relevant_exception_reaches_every_decision_without_reanalysis(deck):
    from arenamcp.decisions import build_pending_decision

    state, _, response = deck
    book = playbook_for()
    backend = Mock()
    backend.complete.return_value = '{"option_ids":["pass"]}'
    planner = ActionPlanner(backend, deck_strategy_fn=book.render, deck_playbook_fn=lambda: book)
    prompt = planner._build_action_prompt(state, "combat_blockers", ["Pass"])
    assert "DECK DECISION RULES FOR THIS WINDOW" in prompt
    assert response["decision_rules"][0]["prefer"] in prompt
    assert response["decision_rules"][0]["unless"] in prompt
    decision = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "ActionsAvailable",
            "can_pass": True,
            "actions": [{"actionType": "ActionType_Pass"}],
        }
    )
    for _ in range(2):
        planner._llm_decision_options(decision, state)
        assert response["decision_rules"][0]["prefer"] in backend.complete.call_args.args[1]
    assert backend.complete.call_count == 2  # Only the requested tactical decisions.


def test_learned_sequencing_rule_bypasses_generic_land_first_shortcut():
    book = playbook_for("spells")
    planner = ActionPlanner(Mock(), deck_playbook_fn=lambda: book)
    state = {"turn": {"active_player": 1, "phase": "Main1"}, "local_seat_id": 1}
    assert planner._should_force_land_drop(state, ["Play Land: Forest"], {}) is None


def test_learned_zone_policy_reaches_the_model_instead_of_forcing_command_zone(deck):
    from arenamcp.decisions import DecisionOption, PendingDecision

    state, _, _ = deck
    state["decision_context"] = {
        "type": "optional_action",
        "commander_return": True,
        "raw": {"gameStateId": 20, "msgId": 30},
        "recipient_ids": [50],
    }
    decision = PendingDecision(
        (20, 30),
        "OptionalAction",
        (
            DecisionOption("optional:accept", "Command zone", meta={"recipients": [50]}),
            DecisionOption("optional:decline", "Leave in graveyard"),
        ),
    )
    book = playbook_for()
    backend = Mock()
    backend.complete.return_value = (
        '{"option_ids":["optional:decline"],"reasoning":"Use a supported graveyard recovery line instead."}'
    )
    planner = ActionPlanner(backend, deck_strategy_fn=book.render, deck_playbook_fn=lambda: book)
    assert planner.plan_decision_options(decision, state) == ["optional:decline"]
    backend.complete.assert_called_once()
    assert "DECK DECISION RULES FOR THIS WINDOW" in backend.complete.call_args.args[1]


def test_report_block_replay_keeps_real_identity_tax_and_legal_alternatives(monkeypatch):
    from pathlib import Path

    state = json.loads((Path(__file__).parent / "fixtures/commander_block_20261004_103338.json").read_text())
    # No network or model action: verify the evidence and the action boundary.
    monkeypatch.setattr("arenamcp.match_context._local_card", lambda gid, epoch: {})
    planner = ActionPlanner(Mock())
    prompt = planner._build_action_prompt(state, "combat_blockers", state["legal_actions"])
    assert "printed {3}{G}{G} plus {4} commander tax" in prompt
    assert "The Notary Hobbits [id:1034] is YOUR COMMANDER" in prompt
    assert "*The Notary Hobbits [id:1042]" in prompt
    plan = planner._parse_response(
        json.dumps(
            {
                "action_type": "declare_blockers",
                "blocker_assignments": {
                    "The Notary Hobbits [id:1034]": "*Samurai [id:1046]",
                    "*Bird [id:1057]": "Atraxa, Praetors' Voice [id:849]",
                },
            }
        ),
        state["legal_actions"],
        state["decision_context"],
        game_state=state,
    )
    assert plan.actions[0].blocker_instance_assignments == {1034: 1046, 1057: 849}
    # No deterministic substitution: the generic planner preserves the model's
    # validated choice. The learned playbook is what must inform that choice.
    token_plan = planner._parse_response(
        '{"action_type":"declare_blockers","blocker_assignments":{"*The Notary Hobbits [id:1042]":"*Samurai [id:1046]"}}',
        state["legal_actions"],
        state["decision_context"],
        game_state=state,
    )
    assert token_plan.actions[0].blocker_instance_assignments == {1042: 1046}


def test_spoken_summary_cannot_replace_internal_strategy():
    runtime = _DeckAnalysisMixin()
    runtime._coach = CoachEngine(Mock())
    runtime._coach._deck_playbook = playbook_for()
    runtime._coach._deck_strategy = runtime._coach._deck_playbook.render()
    stored = runtime._coach._deck_strategy
    runtime._mcp = Mock()
    runtime.ui = Mock()
    runtime.speak_advice = Mock()
    runtime._generate_deck_strategy_brief()
    assert runtime._coach._deck_strategy == stored
    runtime._coach._backend.complete.assert_not_called()
    runtime.speak_advice.assert_called_once_with(runtime._coach._deck_playbook.data["spoken_summary"])


def test_deck_worker_starts_once_then_restarts_for_a_changed_commander(deck, monkeypatch):
    state, _, _ = deck
    workers = []
    monkeypatch.setattr(
        "arenamcp.standalone_deck.threading.Thread",
        lambda **kw: SimpleNamespace(start=lambda: workers.append(kw["target"])),
    )
    runtime = _DeckAnalysisMixin()
    runtime._coach = CoachEngine(Mock())
    runtime._auto_deck_strategy = True
    runtime._is_mulligan_pending = lambda state: False
    assert runtime._maybe_analyze_deck(state)
    assert not runtime._maybe_analyze_deck(state)
    old_generation = runtime._coach._deck_analysis_generation
    changed = deepcopy(state)
    changed["commander_grp_ids"] = [1002]
    assert runtime._maybe_analyze_deck(changed)
    assert len(workers) == 2
    assert runtime._coach._deck_analysis_generation > old_generation


def test_plan_preserves_learned_resource_tradeoff_and_refreshes_after_commander_tax_change(deck):
    state, _, _ = deck
    payload = {
        "win_conditions": ["Engine advantage"],
        "path": "Rebuild then attack",
        "develop_next": "Recover engine",
        "active_mechanisms": ["engine: affordable recovery"],
        "resource_priorities": ["Preserve surviving mana, spend recoverable body"],
        "assumptions": ["Recast must remain affordable after tax"],
    }
    backend = Mock()
    backend.complete.return_value = json.dumps(payload)
    manager = GamePlanManager(backend)
    manager.seed(playbook_for().render())
    plan = manager.maybe_reform(state)
    assert payload["resource_priorities"][0] in plan.as_planner_block()
    assert payload["assumptions"][0] in plan.as_planner_block()
    changed = deepcopy(state)
    changed["commander_casts"] = {1001: 2}
    manager.maybe_reform(changed)
    assert backend.complete.call_count == 2


def test_reload_round_trip_revalidates_identity_version_and_evidence(deck):
    state, _, _ = deck
    book = playbook_for()
    saved = json.loads(json.dumps(book.export()))
    assert DeckPlaybook.restore(saved, state).render() == book.render()
    saved["data"]["mechanisms"][0]["evidence"][0]["rule"] = 999
    with pytest.raises(ValueError):
        DeckPlaybook.restore(saved, state)
    saved = book.export()
    saved["version"] = -1
    with pytest.raises(ValueError):
        DeckPlaybook.restore(saved, state)


def test_background_learning_starts_during_opening_hand_without_changing_tactical_backend(deck, monkeypatch):
    state, _, response = deck
    state["turn"]["turn_number"] = 0
    state["pending_decision"] = "Mulligan"
    workers = []
    monkeypatch.setattr(
        "arenamcp.standalone_deck.threading.Thread",
        lambda **kw: SimpleNamespace(start=lambda: workers.append(kw["target"])),
    )
    background = Mock(enable_thinking=False)
    background.complete.return_value = json.dumps(response)
    monkeypatch.setattr("arenamcp.coach.create_backend", lambda *args, **kw: background)
    tactical = Mock(enable_thinking=False)
    runtime = _DeckAnalysisMixin()
    runtime._coach = CoachEngine(tactical)
    runtime._auto_deck_strategy = True
    runtime._backend_name = "online"
    runtime.model_name = "test-model"
    runtime.ui = Mock()
    runtime.speak_advice = Mock()
    assert runtime._maybe_analyze_deck(state)
    workers[0]()
    assert background.enable_thinking is False
    assert background.complete.call_args_list[0].kwargs["enable_thinking"] is True
    assert background.complete.call_args_list[-1].kwargs["enable_thinking"] is False
    assert tactical.enable_thinking is False
    tactical.complete.assert_not_called()
    assert runtime._coach._deck_playbook
    assert not runtime._maybe_analyze_deck(state)
    assert not runtime._maybe_analyze_deck({**state, "game_over": True})


def test_source_rules_are_attached_from_original_cards_including_vanilla_characteristics(deck):
    state, catalog, response = deck
    del response["mechanisms"][0]["evidence"]
    catalog[1001]["oracle_text"] = ""
    book = DeckPlaybook.parse(json.dumps(response), catalog, commander_ids(state))
    assert book.data["mechanisms"][0]["evidence"] == [{"card": 1001, "rule": 1}]
    assert "Printed characteristics: {3}{G}{G} Legendary Creature" in book.render()


def test_half_a_resource_comparison_cannot_be_published(deck):
    state, catalog, response = deck
    del response["commanders"][0]["resource_comparison"]["spend_and_recover"]
    with pytest.raises(ValueError):
        DeckPlaybook.parse(json.dumps(response), catalog, commander_ids(state))


def test_tactical_prompt_reuses_relevant_rules_without_repeating_full_playbook():
    state, _, _ = deck_case()
    book = playbook_for()
    full = Mock(return_value=book.render())
    planner = ActionPlanner(Mock(), deck_strategy_fn=full, deck_playbook_fn=lambda: book)
    prompt = planner._strategy_context(state)
    full.assert_not_called()
    assert prompt.count("RULE resource_trade") == 1
    assert book.data["primary_plan"] in prompt
    assert book.data["mechanisms"][0]["effect"] in prompt
    assert "KEY CARD ROLES" not in prompt
