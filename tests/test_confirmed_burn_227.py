"""Confirmed damage facts survive submission/cancellation without inventing resolution."""

from copy import deepcopy

from tests.test_hold_recompute_227 import _strike_target_state, _t14_main

from arenamcp.confirmed_burn import ConfirmedBurn


def damage(state):
    return next(c for c in state["battlefield"] if c["instance_id"] == 338).get("damage", 0)


def event(amount=3, **kw):
    return dict(type="damage_dealt", turn=14, source_id=400, target_id=338, amount=amount, **kw)


def test_selected_submitted_cancelled_and_countered_states_are_not_resolution():
    ledger = ConfirmedBurn()
    source = _strike_target_state()
    assert damage(ledger.reconcile(source)) == 0
    # A submitted source with target IDs is still pending, not dealt damage.
    source["stack"][0]["targeting"] = [338]
    assert damage(ledger.reconcile(source)) == 0
    gone = _t14_main()
    gone["stack"] = []
    assert damage(ledger.reconcile(gone)) == 0
    gone["graveyard"] = [{**source["stack"][0], "is_countered": True}]
    assert damage(ledger.reconcile(gone)) == 0


def test_confirmed_event_is_idempotent_and_public_damage_never_doubles():
    ledger = ConfirmedBurn()
    ledger.reconcile(_strike_target_state())
    state = _t14_main()
    state["recent_events"] = [event(), event()]
    original = deepcopy(state)
    assert damage(ledger.reconcile(state)) == 3
    assert damage(ledger.reconcile(state)) == 3
    assert state == original
    target = next(c for c in state["battlefield"] if c["instance_id"] == 338)
    for current in (0, 1, 3, 4):
        target["damage"] = current
        assert damage(ledger.reconcile(state)) == current


def test_turn_cleanup_new_match_and_target_identity_expire_event_fills():
    for change in ("turn", "cleanup", "match", "target"):
        ledger = ConfirmedBurn()
        ledger.reconcile(_strike_target_state())
        state = _t14_main()
        state["recent_events"] = [event()]
        if change == "turn":
            state["turn"]["turn_number"] = 15
        elif change == "cleanup":
            state["turn"]["step"] = "Step_Cleanup"
        elif change == "match":
            state["match_id"] = "different-game"
        else:
            state["recent_events"][0]["target_id"] = 999
        assert damage(ledger.reconcile(state)) == 0


def test_no_lingering_derived_damage_after_event_leaves_snapshot():
    ledger = ConfirmedBurn()
    ledger.reconcile(_strike_target_state())
    state = _t14_main()
    state["recent_events"] = [event()]
    assert damage(ledger.reconcile(state)) == 3
    state["recent_events"] = []
    assert damage(ledger.reconcile(state)) == 0


def test_unknown_source_infect_and_damage_reset_rules_are_not_plain_burn():
    for change in ("unknown", "infect", "reset"):
        ledger = ConfirmedBurn()
        source = _strike_target_state()
        if change == "infect":
            source["stack"][0]["oracle_text"] += "\nInfect"
        if change != "unknown":
            ledger.reconcile(source)
        state = _t14_main()
        state["recent_events"] = [event()]
        if change == "reset":
            state["battlefield"][0]["oracle_text"] = "{T}: Remove all damage from target creature."
        assert damage(ledger.reconcile(state)) == 0
