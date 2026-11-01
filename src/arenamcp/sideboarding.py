"""Recognize the between-games deck request without treating it as gameplay."""

SIDEBOARD_REQUESTS = frozenset({"SubmitDeck", "SubmitDeckReq", "SubmitDeckRequest"})
SIDEBOARD_WAIT_MESSAGE = "Sideboarding: waiting for a reviewed deck submission before the next game."


def is_sideboarding(state: dict) -> bool:
    """Prefer live bridge identity over a previous log-derived decision."""
    request = state.get("_bridge_request_type") or state.get("request_type")
    request_class = state.get("_bridge_request_class") or state.get("request_class")
    if request or request_class:
        return request in SIDEBOARD_REQUESTS or request_class in SIDEBOARD_REQUESTS
    if state.get("_bridge_connected") and state.get("_bridge_has_pending") is False:
        return False
    return (state.get("decision_context") or {}).get("type") == "sideboard" or state.get(
        "pending_decision"
    ) == "Sideboard"
