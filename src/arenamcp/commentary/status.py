"""User-facing local scheduling state; no implied server capacity diagnosis."""


def status_label(status):
    state, reason = status.get("state"), status.get("reason")
    if state == "voice":
        return str(status.get("message") or "Check Speech & voices")
    labels = {
        "ready": "Analysis ready",
        "deterministic": "Grounded event calls",
        "grounded": "Narrating observed events",
        "waiting_events": "Waiting for a new observed game event",
        "waiting_delivery": "Narration queued or playing",
        "waiting": "Waiting for a game",
        "silent": "Waiting for a fresh moment",
        "dropped": "Skipped a line whose facts changed",
        "unavailable": "Announcers unavailable — check Speech & voices",
    }
    reasons = {
        "grounded_call_spacing": "Brief pause between event calls",
        "analysis_waits_for_gameplay": "Gameplay decision has priority; using event calls",
        "superseded": "Analysis interrupted by a gameplay decision",
        "exchange_rate_or_busy": "Commentary request limit reached; using event calls",
        "request_time_budget": "Commentary time budget reached; using event calls",
        "analysis_retry_spacing": "Brief pause before retrying analysis",
        "opening_deck_overview": "Introducing the deck plan",
        "confirmed_action_explanation": "Discussing the plan behind an observed play",
        "unknown_reference": "Skipped analysis with an unverified reference",
        "unsupported_rule": "Skipped analysis with an unverified card rule",
        "facts_changed": "Skipped a line whose facts changed",
        "facts_or_mode_changed": "Skipped analysis after the game or speech mode changed",
        "expired_or_changed": "Skipped expired or changed analysis",
    }
    return "Arena On Air: " + reasons.get(reason, labels.get(state, "Waiting for a grounded, fresh moment"))
