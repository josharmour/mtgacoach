"""Minimal factual regressions for audit H02, match1abe10ef, GRE175/177.

Historical evidence is preserved in gameplay-ultra-audit.json. These are
purpose-built current planner snapshots, not full historical replay packets.
"""

from tests.test_tactical_timing_backlog import board, card, cast, menu

from arenamcp.blink_intent import blink_intent_issue

BLINK = "Exile target creature you control, then return that card to the battlefield under your control."
FLICKER = "Exile target creature or enchantment, then return it to the battlefield under its owner's control."
REASON = "Cloudshift on the returning Nessian Wanderer adds another constellation trigger to dig for a land."


def fixture():
    state = board(5)
    state["hand"] = [card(583, "Cloudshift", BLINK, "Instant", "{W}")]
    state["battlefield"].append(
        card(
            569,
            "Nessian Wanderer",
            "Constellation — Whenever an enchantment enters the battlefield under your control, look at the top three cards of your library.",
            "Creature — Satyr Scout",
            "{1}{G}",
        )
    )
    state["stack"] = [card(580, "Flicker of Fate", FLICKER, "Instant", "{1}{W}", targeting=[569])]
    return state, menu(cast(583))


def test_same_iid_blink_overlap_is_refuted_before_second_cast():
    state, decision = fixture()
    issue = blink_intent_issue(decision, state, ["idx:583"], REASON)
    assert issue.resolve_first
    assert "#569" in issue.reason and "new object" in issue.reason


def test_matching_name_is_not_matching_object_identity():
    state, decision = fixture()
    state["stack"][0]["targeting"] = [999]
    issue = blink_intent_issue(decision, state, ["idx:583"], "Cloudshift on Nessian Wanderer untaps it.")
    assert issue is None


def test_new_returned_object_is_not_old_pending_target():
    state, decision = fixture()
    state["battlefield"][-1]["instance_id"] = 586
    assert blink_intent_issue(decision, state, ["idx:583"], "Blink Nessian Wanderer to untap it.") is None


def test_non_enchantment_constellation_observer_does_not_trigger_on_itself():
    state, decision = fixture()
    state["stack"] = []
    issue = blink_intent_issue(decision, state, ["idx:583"], REASON)
    assert issue and not issue.resolve_first
    assert "not an enchantment" in issue.reason


def test_other_valid_blink_purposes_are_not_refuted():
    state, decision = fixture()
    state["stack"] = []
    for reason in [
        "Blink Nessian Wanderer to untap our blocker.",
        "Do not blink Nessian Wanderer for a constellation trigger.",
    ]:
        assert blink_intent_issue(decision, state, ["idx:583"], reason) is None
    state["battlefield"][-1]["type_line"] = "Enchantment Creature — Satyr"
    assert blink_intent_issue(decision, state, ["idx:583"], REASON) is None


def test_real_hei_bai_etb_engine_is_preserved():
    state, decision = fixture()
    state["stack"] = []
    state["battlefield"][-1] = card(
        569,
        "Hei Bai, Forest Guardian",
        "When Hei Bai, Forest Guardian enters, you may put an enchantment card from your hand onto the battlefield.",
        "Legendary Creature — Spirit",
        "{3}{G}",
    )
    assert (
        blink_intent_issue(
            decision,
            state,
            ["idx:583"],
            "Blink Hei Bai, Forest Guardian to put a Shrine onto the battlefield and trigger constellation.",
        )
        is None
    )


def test_opposing_response_and_uncertain_target_preserve_normal_tactics():
    state, decision = fixture()
    state["stack"].insert(
        0, card(900, "Removal", "Destroy target creature.", "Instant", seat=2, targeting=[569])
    )
    assert blink_intent_issue(decision, state, ["idx:583"], REASON) is None
    state, decision = fixture()
    assert (
        blink_intent_issue(decision, state, ["idx:583"], "Cast Cloudshift now; choose the best target next.")
        is None
    )
    assert blink_intent_issue(decision, state, ["pass"], REASON) is None


def test_indirect_creature_entry_engine_is_not_declared_no_payoff():
    state, decision = fixture()
    state["stack"] = []
    state["battlefield"].append(
        card(
            800,
            "Engine",
            "Whenever a creature enters under your control, create an enchantment token.",
            "Enchantment",
        )
    )
    assert blink_intent_issue(decision, state, ["idx:583"], REASON) is None


def test_own_intervening_effect_can_create_real_urgency():
    state, decision = fixture()
    state["stack"].insert(0, card(900, "Our wrath", "Destroy all creatures.", "Sorcery"))
    assert (
        blink_intent_issue(
            decision, state, ["idx:583"], "Blink Nessian Wanderer to preserve it from the pending wrath."
        )
        is None
    )


def test_planner_recheck_resolves_identity_before_second_blink():
    import json
    from unittest.mock import Mock

    # Use the same minimal factual board as the helper regression below.
    from tests.test_tactical_timing_backlog import board, card, cast, menu

    from arenamcp.action_planner import ActionPlanner

    state = board(2)
    text = "Exile target creature you control, then return that card to the battlefield under your control."
    state["hand"] = [card(30, "Second blink", text, "Instant", "{W}")]
    state["battlefield"] += [card(569, "Wanderer", "", "Creature")]
    state["stack"] = [card(600, "First blink", text, "Instant", targeting=[569])]
    decision = menu(cast(30))
    reason = "Cast Second blink targeting Wanderer for another entry."
    planner = ActionPlanner(backend=Mock())
    planner._last_decision_trace = {}

    def parse(raw):
        data = json.loads(raw)
        return data, data["option_ids"]

    ask = Mock(return_value=json.dumps({"option_ids": ["idx:30"], "reasoning": reason}))
    data, picks = planner._repair_blink_claim(
        decision, state, {"reasoning": reason}, ["idx:30"], ask, parse, ""
    )
    assert picks == ["pass"] and "new object" in data["reasoning"]
    assert ask.call_count == 1
    state["battlefield"].append(card(570, "Other creature", "", "Creature"))
    ask = Mock(
        return_value=json.dumps(
            {"option_ids": ["idx:30"], "reasoning": "Blink Other creature for its useful entry."}
        )
    )
    _, picks = planner._repair_blink_claim(decision, state, {"reasoning": reason}, ["idx:30"], ask, parse, "")
    assert picks == ["idx:30"]


def test_opponent_own_turn_lock_does_not_block_our_turn_casts():
    state, decision = fixture()
    state["stack"] = []
    state["hand"][0]["oracle_text"] = FLICKER
    state["battlefield"].append(
        card(
            600,
            "Grand Abolisher",
            "During your turn, your opponents can't cast spells or activate abilities of artifacts, creatures, or enchantments.",
            "Creature",
            seat=2,
        )
    )
    reason = "Blink Grand Abolisher to unlock our spells this turn."
    issue = blink_intent_issue(decision, state, ["idx:583"], reason)
    assert issue and "controller's turn" in issue.reason
    assert (
        blink_intent_issue(decision, state, ["idx:583"], "Blink Grand Abolisher to remove its attached Aura.")
        is None
    )
    state["turn"]["active_player"] = 2
    assert blink_intent_issue(decision, state, ["idx:583"], reason) is None
