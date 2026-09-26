"""Phase C: per-request submission FSM (fable-improvements.md items 2+3).

Identity is content-addressed (request type + option set), so the GRE's
habit of re-issuing the same logical decision under fresh msgId /
gameStateId values can't disguise a rejection as a new window — and a
fresh window can't inherit a stale window's failure count.
"""

from dataclasses import replace

from arenamcp.decisions import build_pending_decision
from arenamcp.request_tracker import RequestTracker, decision_fingerprint


def _decision(ids=(2, 161)):
    return build_pending_decision(
        {
            "has_pending": True,
            "request_type": "SelectTargets",
            "target_candidates": [{"targetInstanceId": i, "grpId": 0} for i in ids],
            "target_selections": [{"minTargets": 1, "maxTargets": 1}],
        }
    )


def test_one_in_flight_submission_per_request():
    t = RequestTracker()
    fp = decision_fingerprint(_decision())
    assert t.may_submit(fp)
    t.note_submitted(fp)
    # Second submit for the same request cannot fire until settled.
    assert not t.may_submit(fp)


def test_different_decision_settles_as_advanced():
    t = RequestTracker()
    fp1 = decision_fingerprint(_decision((2, 161)))
    fp2 = decision_fingerprint(_decision((300,)))
    t.note_submitted(fp1)
    t.observe(fp2)  # a different window appeared → ADVANCED
    assert t.rejections(fp1) == 0
    assert t.may_submit(fp1)


def test_represent_within_grace_is_not_rejection():
    t = RequestTracker()
    fp = decision_fingerprint(_decision())
    t.note_submitted(fp)
    t.observe(fp)  # immediately re-seen — processing lag, not a rejection
    assert t.rejections(fp) == 0
    assert not t.may_submit(fp)  # still in flight


def test_represent_after_grace_counts_rejection(monkeypatch):
    t = RequestTracker()
    monkeypatch.setattr(t, "REJECT_GRACE_S", 0.0)
    fp = decision_fingerprint(_decision())
    t.note_submitted(fp)
    t.observe(fp)
    assert t.rejections(fp) == 1
    assert t.may_submit(fp)  # settled — may try again (until the cap)


def test_submission_cap_exhausts_request(monkeypatch):
    t = RequestTracker()
    monkeypatch.setattr(t, "REJECT_GRACE_S", 0.0)
    fp = decision_fingerprint(_decision())
    for _ in range(t.MAX_SUBMISSIONS_PER_REQUEST):
        assert t.may_submit(fp)
        t.note_submitted(fp)
        t.observe(fp)  # rejected each time
    assert not t.may_submit(fp)
    assert t.exhausted(fp)


def test_escape_requires_real_rejections(monkeypatch):
    t = RequestTracker()
    monkeypatch.setattr(t, "REJECT_GRACE_S", 0.0)
    fp = decision_fingerprint(_decision())
    assert not t.may_escape(fp)  # untouched request: no escape, ever
    t.note_submitted(fp)
    t.observe(fp)
    assert not t.may_escape(fp)  # one rejection: still no
    t.note_submitted(fp)
    t.observe(fp)
    assert t.may_escape(fp)  # two real rejections: escape allowed


def test_rollback_settles_in_flight():
    t = RequestTracker()
    fp = decision_fingerprint(_decision())
    t.note_submitted(fp)
    t.note_rolled_back(fp)
    assert t.may_submit(fp)


def test_reset_clears_history():
    t = RequestTracker()
    fp = decision_fingerprint(_decision())
    for _ in range(t.MAX_SUBMISSIONS_PER_REQUEST):
        t.note_submitted(fp)
        t.note_rolled_back(fp)
    assert not t.may_submit(fp)
    t.reset()
    assert t.may_submit(fp)


def test_mulligan_fingerprint_distinguishes_rounds_when_ids_available():
    """Mulligan option sets are identical across rounds; with the plugin
    surfacing request identity, round 2 must NOT inherit round 1's
    rejection/submission history (fable Phase E)."""
    round1 = build_pending_decision(
        {"has_pending": True, "request_type": "Mulligan", "game_state_id": 10, "msg_id": 5}
    )
    round2 = build_pending_decision(
        {"has_pending": True, "request_type": "Mulligan", "game_state_id": 22, "msg_id": 9}
    )
    assert decision_fingerprint(round1) != decision_fingerprint(round2)

    # Old plugin (no ids) keeps the legacy collision behavior — harmless.
    legacy = build_pending_decision({"has_pending": True, "request_type": "Mulligan"})
    legacy2 = build_pending_decision({"has_pending": True, "request_type": "Mulligan"})
    assert decision_fingerprint(legacy) == decision_fingerprint(legacy2)


def test_accepted_answers_do_not_count_toward_the_cap():
    # 2026-09-24: five auras targeted the same creature (#283), each accepted;
    # the sixth "target a creature you control" was refused as exhausted.
    t = RequestTracker()
    aura_target = decision_fingerprint(_decision((283,)))
    priority = decision_fingerprint(_decision((1, 2, 3)))
    for _ in range(6):
        assert t.may_submit(aura_target)
        t.note_submitted(aura_target)
        t.observe(priority)  # the aura resolved: a different window → ADVANCED
        t.observe(aura_target)  # next aura asks for the same target again
    assert not t.exhausted(aura_target)
    assert t.rejections(aura_target) == 0


def _priority_decision(instance_id=10, ability_id=20, payable=True, state_id=1):
    return build_pending_decision(
        {
            "has_pending": True,
            "request_type": "ActionsAvailable",
            "game_state_id": state_id,
            "can_pass": True,
            "actions": [
                {
                    "actionType": "Cast",
                    "instanceId": instance_id,
                    "abilityGrpId": ability_id,
                    "hasAutoTap": payable,
                }
            ],
        }
    )


def test_priority_menu_identity_tracks_cards_abilities_and_payability():
    original = decision_fingerprint(_priority_decision())
    assert original != decision_fingerprint(_priority_decision(instance_id=11))
    assert original != decision_fingerprint(_priority_decision(ability_id=21))
    assert original != decision_fingerprint(_priority_decision(payable=False))
    assert original == decision_fingerprint(_priority_decision(state_id=2))


def test_new_spell_at_same_index_settles_previous_submission():
    tracker = RequestTracker()
    previous = decision_fingerprint(_priority_decision(instance_id=10))
    current = decision_fingerprint(_priority_decision(instance_id=11))
    tracker.note_submitted(previous)
    tracker.observe(current)
    assert tracker.may_submit(current)
    assert tracker.may_submit(previous)
    assert tracker.rejections(previous) == 0


def test_target_slot_changes_are_distinct_requests():
    decision = _decision()
    original = decision_fingerprint(decision)
    changed_slot = replace(decision.slots[0], candidate_ids=(161,))
    assert original != decision_fingerprint(replace(decision, slots=(changed_slot,)))
    assert original != decision_fingerprint(replace(decision, can_cancel=True))
    assert original != decision_fingerprint(replace(decision, source_label="Another spell"))


def test_option_display_order_does_not_reset_rejection_history():
    decision = _decision()
    assert decision_fingerprint(decision) == decision_fingerprint(
        replace(decision, options=tuple(reversed(decision.options)))
    )
