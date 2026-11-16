from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.entry_damage import entry_damage_note, entry_kill_concerns


def test_budget_is_shared_across_claimed_kills_and_preserves_separate_lines():
    spell = {
        "name": "Ash Caller",
        "instance_id": 1,
        "type_line": "Creature",
        "oracle_text": "When this creature enters, it deals 4 damage divided as you choose among any number of target creatures and/or planeswalkers.",
    }

    def body(name, iid, toughness):
        return {
            "name": name,
            "instance_id": iid,
            "controller_seat_id": 2,
            "type_line": "Creature",
            "oracle_text": "Vigilance",
            "power": 3,
            "toughness": toughness,
        }

    state = {
        "hand": [spell],
        "local_seat_id": 1,
        "battlefield": [body("Alpha Sentinel", 2, 3), body("Beta Sentinel", 3, 3)],
    }
    decision = PendingDecision(
        request_id=(1, 2),
        request_type="ActionsAvailable",
        options=(
            DecisionOption(
                "idx:0",
                "Cast Ash Caller",
                meta={
                    "actionType": "ActionType_Cast",
                    "instanceId": 1,
                    "alternativeCostText": "Evoke—Exile a red card from your hand.",
                },
            ),
        ),
    )

    def recheck(reason):
        return entry_kill_concerns(decision, state, ["idx:0"], reason)

    assert "4 total" in entry_damage_note(spell, state)
    assert recheck("Ash Caller kills Alpha Sentinel. Ash Caller also kills Beta Sentinel.")
    assert not recheck("Ash Caller kills Alpha Sentinel. Next turn Terminate kills Beta Sentinel.")
    assert not recheck("Ash Caller kills Alpha Sentinel or Beta Sentinel.")
    assert not recheck("Ash Caller kills Alpha Sentinel and Terminate kills Beta Sentinel.")
    state["battlefield"][1]["name"] = "Alpha Sentinel"
    assert not recheck("Ash Caller kills Alpha Sentinel.")
    state["battlefield"][1]["name"] = "Beta Sentinel"
    state["battlefield"][0]["toughness"] = 5
    assert recheck("Evoking Ash Caller deals four to kill Alpha Sentinel.")
    assert not recheck("Blink Ash Caller for two entries. Ash Caller kills Alpha Sentinel.")
    state["battlefield"][0].update(damage=1, damaged_this_turn=True)
    assert not recheck("Ash Caller kills Alpha Sentinel.")
