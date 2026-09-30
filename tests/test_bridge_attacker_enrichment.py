import pytest

from arenamcp.gre_bridge import enrich_snapshot_from_pending_response


@pytest.mark.parametrize("payload_only", [False, True])
def test_live_attackers_survive_cleared_log_context(payload_only):
    snapshot = {
        "decision_context": None,
        "battlefield": [
            {"instance_id": 208, "name": "Heartwood Crafter"},
            {"instance_id": 238, "name": "Pia, Aether Ascetic"},
            {"instance_id": 288, "name": "Beast"},
            {"instance_id": 300, "name": "Greenhouse Propagator"},
        ],
    }
    attackers = [{"attackerInstanceId": instance_id} for instance_id in (208, 238, 288)]
    poll = {
        "has_pending": True,
        "request_type": "DeclareAttackers",
        "request_class": "DeclareAttackerRequest",
        "request_payload": {"qualifiedAttackers": attackers},
    }
    if not payload_only:
        poll["attackers"] = attackers
    enrich_snapshot_from_pending_response(snapshot, poll, bridge_connected=True)
    context = snapshot["decision_context"]
    assert context["legal_attacker_ids"] == [208, 238, 288]
    assert context["legal_attackers"] == ["Heartwood Crafter", "Pia, Aether Ascetic", "Beast"]
    assert context["raw_attackers"] == attackers


def test_empty_current_attack_list_clears_old_candidates():
    snapshot = {"decision_context": {"type": "declare_attackers", "legal_attackers": ["Old attacker"]}}
    poll = {"has_pending": True, "request_type": "DeclareAttackers", "attackers": []}
    enrich_snapshot_from_pending_response(snapshot, poll, bridge_connected=True)
    assert snapshot["decision_context"]["legal_attackers"] == []
    assert snapshot["decision_context"]["legal_attacker_ids"] == []
