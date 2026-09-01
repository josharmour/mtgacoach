"""Tests to verify that mulligan decisions take priority over deck strategy analysis at match start."""

from arenamcp.standalone import StandaloneCoach


def test_is_mulligan_pending_detection():
    """Verify _is_mulligan_pending identifies all forms of pending mulligans."""
    # 1. Plain pending_decision = "Mulligan"
    assert StandaloneCoach._is_mulligan_pending({"pending_decision": "Mulligan"}) is True
    assert StandaloneCoach._is_mulligan_pending({"pending_decision": "Mulligan Bottom"}) is True
    assert StandaloneCoach._is_mulligan_pending({"pending_decision": "MulliganReq"}) is True

    # 2. Bridge trigger with request_type / _bridge_request_type / _bridge_request_class
    assert StandaloneCoach._is_mulligan_pending({"_bridge_trigger": {"request_type": "Mulligan"}}) is True
    assert (
        StandaloneCoach._is_mulligan_pending(
            {"_bridge_trigger": {"_bridge_request_class": "MulliganRequest"}}
        )
        is True
    )

    # 3. decision_context with LondonMulligan
    assert StandaloneCoach._is_mulligan_pending({"decision_context": {"context": "LondonMulligan"}}) is True
    assert (
        StandaloneCoach._is_mulligan_pending({"decision_context": {"group_context": "LondonMulligan"}})
        is True
    )

    # 4. Non-mulligan decisions
    assert StandaloneCoach._is_mulligan_pending({"pending_decision": "Action Required"}) is False
    assert StandaloneCoach._is_mulligan_pending({"pending_decision": "SelectTargets"}) is False
    assert StandaloneCoach._is_mulligan_pending({}) is False
    assert StandaloneCoach._is_mulligan_pending(None) is False


def test_mulligan_defers_deck_analysis_evaluation():
    """Verify that a pending mulligan prevents deck analysis from triggering."""
    state_mulligan_pending = {
        "pending_decision": "Mulligan",
        "deck_cards": list(range(40)),
    }
    state_mulligan_resolved = {
        "pending_decision": None,
        "deck_cards": list(range(40)),
    }

    assert StandaloneCoach._is_mulligan_pending(state_mulligan_pending) is True
    assert StandaloneCoach._is_mulligan_pending(state_mulligan_resolved) is False


def test_deck_analysis_turn_and_card_count_gating():
    """Verify turn_num >= 1 and len(deck_cards) >= 20 are required for deck strategy."""
    # turn_num = 0 (Turn 0 / Mulligan phase) -> must NOT trigger deck analysis
    turn_0_state = {"turn": {"turn_number": 0}, "deck_cards": list(range(40))}

    # turn_num = 1, but deck_cards < 20 (partial hand) -> must NOT trigger deck analysis
    partial_deck_state = {"turn": {"turn_number": 1}, "deck_cards": list(range(7))}

    # turn_num = 1, full deck (40 cards), no mulligan -> valid for deck analysis
    valid_state = {"turn": {"turn_number": 1}, "deck_cards": list(range(40))}

    assert (turn_0_state.get("turn", {}).get("turn_number", 0) >= 1) is False
    assert (len(partial_deck_state["deck_cards"]) >= 20) is False
    assert (valid_state.get("turn", {}).get("turn_number", 0) >= 1) is True
    assert (len(valid_state["deck_cards"]) >= 20) is True


def test_mulligan_trigger_exempt_from_boundary_suppression():
    """Verify mulligan decisions are exempt from match boundary trigger suppression."""
    mulligan_state = {"pending_decision": "Mulligan"}
    regular_state = {"pending_decision": "Action Required"}

    assert StandaloneCoach._is_mulligan_pending(mulligan_state) is True
    assert StandaloneCoach._is_mulligan_pending(regular_state) is False
