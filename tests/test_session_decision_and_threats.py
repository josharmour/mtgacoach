from types import SimpleNamespace

from arenamcp.attack_potential import _Side, annotate, attack_bodies, counter_growth_pump
from arenamcp.decision_consistency import consistent_action

BILL = "Landfall — Whenever a land you control enters, put a +1/+1 counter on target creature. {3}{G}{G}: Double the number of +1/+1 counters on each creature you control."
MIKE = "If one or more +1/+1 counters would be put on a creature you control, that many plus one +1/+1 counters are put on it instead."


def test_visible_bill_counter_growth_forecasts_seventeen_not_nine():
    bill = {
        "instance_id": 10,
        "name": "Bill",
        "type_line": "Creature",
        "controller_seat_id": 2,
        "oracle_text": BILL,
        "counters": {"P1P1": 2},
        "power": 4,
        "toughness": 4,
    }
    mike = {
        "instance_id": 11,
        "name": "Michelangelo",
        "type_line": "Creature",
        "controller_seat_id": 2,
        "oracle_text": MIKE,
        "counters": {"P1P1": 4},
        "power": 5,
        "toughness": 5,
    }
    forests = [
        {"instance_id": i, "type_line": "Basic Land — Forest", "controller_seat_id": 2} for i in range(20, 25)
    ]
    board = [bill, mike, *forests]
    bodies = [dict(bill), dict(mike)]
    annotate(bodies, board, 2, untapped_only=True, declared=False)
    assert [b["power"] for b in attack_bodies(bodies)] == [7, 10]
    for c in forests:
        c["is_tapped"] = True
    bodies = [dict(bill), dict(mike)]
    annotate(bodies, board, 2, untapped_only=True, declared=False)
    assert [b["power"] for b in attack_bodies(bodies)] == [4, 5]
    for c in forests:
        c["is_tapped"] = False
        c["type_line"] = "Basic Land — Mountain"
    assert not counter_growth_pump(bill, _Side(board, 2, mana=5, untapped_only=True))


def test_ready_hand_size_ultimate_projects_known_draw_not_future_token():
    walker = {
        "instance_id": 30,
        "name": "The Theorist",
        "type_line": "Planeswalker",
        "controller_seat_id": 2,
        "loyalty": 6,
        "oracle_text": "−6: Draw three cards. Then put X +1/+1 counters on each creature you control, where X is the number of cards in your hand.",
    }
    token = {
        "instance_id": 10,
        "name": "Illusion",
        "controller_seat_id": 2,
        "type_line": "Creature",
        "power": 1,
        "toughness": 1,
    }
    side = _Side([walker, token], 2, mana=0, untapped_only=False, hand_size=7)
    assert counter_growth_pump(token, side)[0].power == 11
    walker["loyalty"] = 5
    assert not counter_growth_pump(token, side)


def decision():
    return SimpleNamespace(
        options=[
            SimpleNamespace(option_id="idx:0", meta={"actionType": "Cast", "instanceId": 10}),
            SimpleNamespace(option_id="pass", meta={}),
        ]
    )


def test_explicit_hold_pass_conclusion_cannot_commit_opposite_cast():
    reason = "Iroh's 1-damage sweeper kills nothing here (Overlord is 5/5), so hold it as the 4-damage removal for their blocker and pass; opponent at 3 with no profitable attack."
    assert consistent_action(decision(), {}, ["idx:0"], reason)[0] == "pass"
    assert consistent_action(decision(), {}, ["idx:0"], "Do not hold it and pass; cast now.") is None
    assert (
        consistent_action(decision(), {}, ["idx:0"], "Cast to trigger prowess, then hold priority.") is None
    )


def test_pending_prowess_waits_only_without_an_urgent_response():
    body = {
        "instance_id": 11,
        "name": "Emberheart Challenger",
        "controller_seat_id": 1,
        "type_line": "Creature",
        "oracle_text": "Haste\nProwess",
    }
    state = {
        "local_seat_id": 1,
        "turn": {"active_player": 1, "phase": "Main1"},
        "stack": [body],
        "hand": [{"instance_id": 10, "type_line": "Instant"}],
    }
    assert (
        consistent_action(
            decision(), state, ["idx:0"], "Burn the blocker and pump the resolving Challenger."
        )[0]
        == "pass"
    )
    state["stack"].append({"instance_id": 12, "controller_seat_id": 2, "type_line": "Instant"})
    assert consistent_action(decision(), state, ["idx:0"], "Pump Challenger to protect it.") is None
    state["stack"] = []
    assert consistent_action(decision(), state, ["idx:0"], "Pump the resolved Challenger.") is None
