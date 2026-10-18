"""Mulligan-pending detection.

Right after a match boundary the coach loop suppresses stale triggers for two
seconds, except bridge triggers and a pending mulligan (standalone.py): the
mulligan prompt of a new game is real. ``_is_mulligan_pending`` decides that
exemption, so it must recognise every shape a mulligan prompt arrives in.
"""

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
