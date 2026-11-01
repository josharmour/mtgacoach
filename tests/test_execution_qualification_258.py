"""Accepted block narration must explain guard overrides without dropping useful blocks."""

import pytest
from tests.test_block_declaration_20261019 import (
    LOCAL,
    OPPONENT,
    SHARK,
    ArenaBlockers,
    blockers_request,
    creature,
    engine_for,
    incident_board,
    with_context,
)

from arenamcp.action_planner import ActionPlan, ActionType, GameAction


@pytest.mark.parametrize("life,kept", [(25, False), (4, True)])
def test_chump_override_matches_submission_and_explains_why(life, kept, monkeypatch):
    monkeypatch.setattr("arenamcp.autopilot_bridge.time.sleep", lambda _: None)
    request = blockers_request()
    request["blockers"] = [
        {
            "blockerInstanceId": 484,
            "attackerInstanceIds": [SHARK],
            "selectedAttackerInstanceIds": [],
            "maxAttackers": 1,
        }
    ]
    state = incident_board(life=life)
    state["battlefield"] = [
        creature(SHARK, "Trampler", 4, 4, OPPONENT, keywords=["trample"], attacking=True),
        creature(484, "Rabbit", 1, 1, LOCAL, token=True),
    ]
    state = with_context(state, request)
    state["decision_context"]["attacker_ids"] = [SHARK]
    bridge = ArenaBlockers(request, state)
    original = GameAction(
        ActionType.DECLARE_BLOCKERS,
        blocker_assignments={"Rabbit [id:484]": "Trampler [id:468]"},
        blocker_instance_assignments={484: SHARK},
        reasoning="Chump with Rabbit to reduce trample damage.",
    )
    result = engine_for(bridge, state)._try_gre_bridge_blockers(original)
    assert result is not None and result.success
    assert bridge.committed == ({484: {SHARK}} if kept else {})
    accepted = result.submitted_action or original
    spoken = ActionPlan(actions=[accepted]).spoken_actions()
    if kept:
        assert accepted.blocker_instance_assignments == {484: SHARK}
        assert "Rabbit" in spoken and "Don't block" not in spoken
    else:
        assert accepted.blocker_instance_assignments == {}
        assert accepted.model_reasoning == original.reasoning
        assert accepted.override_note
        assert "Don't block" in spoken
        assert accepted.override_note.rstrip(".") in spoken
