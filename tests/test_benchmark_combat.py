import copy
import gzip
import json

import pytest

from arenamcp.benchmark_combat import aggregate, compare


def declaration(block=False):
    kind = "DeclareBlockers" if block else "DeclareAttackers"
    cls = "DeclareBlockersRequest" if block else "DeclareAttackerRequest"
    context = {"requestType": kind, "requestClass": cls, "_bridge_source": True}
    target_kind = "attacker" if block else "player"
    target = 20 if block else 2
    key = f"{'block' if block else 'attack'}:10:{target_kind}:{target}"
    if block:
        context.update(
            type="declare_blockers",
            attacker_ids=[20],
            raw_blockers=[
                {"blockerInstanceId": 10, "attackerInstanceIds": [20], "minAttackers": 0, "maxAttackers": 1}
            ],
        )
    else:
        context.update(
            type="declare_attackers",
            raw_attackers=[
                {
                    "attackerInstanceId": 10,
                    "legalDamageRecipients": [{"type": "Player", "playerSystemSeatId": 2}],
                }
            ],
        )

    def creature(i, seat, power):
        return dict(
            instance_id=i,
            name=f"Test body {i}",
            type_line="Creature",
            owner_seat_id=seat,
            controller_seat_id=seat,
            power=power,
            toughness=3,
            is_attacking=block and seat == 2,
        )

    state = dict(
        match_id="synthetic-match",
        game_number=1,
        local_seat_id=1,
        opponent_seat_id=2,
        _bridge_request_type=kind,
        _bridge_request_class=cls,
        decision_context=context,
        players=[
            {"seat_id": 1, "is_local": True, "life_total": 2},
            {"seat_id": 2, "is_local": False, "life_total": 2},
        ],
        battlefield=[creature(10, 1, 3)] + ([creature(20, 2, 3)] if block else []),
    )
    opt = {"actor_instance_id": 10, "recipient_type": target_kind, "recipient_id": target, "option_id": key}
    return {
        "state_snapshot": {"version": 1, "state": state},
        "outcome": "executed",
        "pending_decision": {
            "request_type": kind,
            "request_id": [17, 45],
            "option_family": "combat_assignment_v1",
            "options": [opt],
        },
        "chosen_options": [key],
        "submitted_request_id": [18, 46],
    }


@pytest.mark.parametrize("block", [False, True])
def test_replay_compares_actual_native_selections_without_export(block):
    row = declaration(block)
    assert compare(row) == "same_submission"
    row["chosen_options"] = []
    row["submitted_request_id"] = [17, 45]
    assert compare(row) == "different_offered_selection"


def test_rejects_mismatched_temporal_and_menu_provenance():
    row = declaration()
    row["submitted_request_id"] = [19, 48]
    with pytest.raises(ValueError):
        compare(row)
    row = declaration()
    row["pending_decision"]["options"][0]["recipient_id"] = True
    with pytest.raises(ValueError):
        compare(row)
    row = declaration()
    row["state_snapshot"]["state"]["decision_context"]["raw_attackers"][0]["legalDamageRecipients"][0][
        "battleInstanceId"
    ] = 42
    with pytest.raises(ValueError):
        compare(row)


def test_counts_conflicting_duplicate_once(tmp_path):
    row = declaration()
    changed = copy.deepcopy(row)
    changed.update(chosen_options=[], submitted_request_id=[17, 45])

    def write(i, rows):
        with gzip.open(tmp_path / f"{i}.json.gz", "wt") as f:
            json.dump({"match_id": "synthetic-match", "packet": {"decisions": rows}}, f)

    write(1, [row])
    write(2, [row])
    write(3, [changed])
    assert aggregate(tmp_path.glob("*.gz"))["DeclareAttackers"] == {"conflicting_receipts": 1}


def test_observable_initial_selections_and_invalid_seat_are_rejected():
    row = declaration(True)
    row["state_snapshot"]["state"]["decision_context"]["raw_blockers"][0]["selectedAttackerInstanceIds"] = [
        20
    ]
    with pytest.raises(ValueError):
        compare(row)
    row = declaration()
    target = row["state_snapshot"]["state"]["decision_context"]["raw_attackers"][0]["legalDamageRecipients"][
        0
    ]
    target["playerSystemSeatId"] = 3
    with pytest.raises(ValueError):
        compare(row)


def test_recorded_blocker_can_cover_multiple_distinct_attackers():
    row = declaration(True)
    state = row["state_snapshot"]["state"]
    other = dict(state["battlefield"][-1], instance_id=21)
    state["battlefield"].append(other)
    ctx = state["decision_context"]
    ctx["attacker_ids"].append(21)
    ctx["raw_blockers"][0].update(maxAttackers=2, attackerInstanceIds=[20, 21])
    key = "block:10:attacker:21"
    row["pending_decision"]["options"].append(
        dict(row["pending_decision"]["options"][0], recipient_id=21, option_id=key)
    )
    row["chosen_options"].append(key)
    assert compare(row) == "different_offered_selection"
    row["chosen_options"].append(key)
    with pytest.raises(ValueError):
        compare(row)
