from copy import deepcopy

from arenamcp.action_planner import ActionPlanner
from arenamcp.channel_zones import battlefield_channel_note, channel_land_note
from arenamcp.decisions import build_pending_decision

CARD = {
    "instance_id": 10,
    "grp_id": 100,
    "name": "Eiganjo, Seat of the Empire",
    "type_line": "Legendary Land",
    "controller_seat_id": 1,
    "owner_seat_id": 1,
    "oracle_text": "{T}: Add {W}.\nChannel — {2}{W}, Discard Eiganjo: It deals 4 damage to target attacking or blocking creature.",
}


def state():
    return {
        "local_seat_id": 1,
        "turn": {"turn_number": 5, "active_player": 1, "phase": "Phase_Main1"},
        "players": [{"seat_id": 1, "is_local": True, "life_total": 20}, {"seat_id": 2, "life_total": 20}],
        "hand": [dict(CARD)],
        "battlefield": [],
    }


def test_channel_land_warning_uses_rules_not_card_name():
    assert "unavailable on the battlefield" in channel_land_note(CARD)
    assert channel_land_note({"name": CARD["name"], "oracle_text": "{T}: Add {W}."}) == ""
    assert "out of hand" in channel_land_note({**CARD, "name": "Other channel land"})


def test_battlefield_copy_does_not_disable_separate_hand_copy():
    s = state()
    s["battlefield"] = [{**CARD, "instance_id": 20}]
    before = deepcopy(s)
    note = battlefield_channel_note(s)
    assert "Those battlefield copies cannot" in note
    assert "separate copy in hand may still" in note
    assert s == before
    s["battlefield"][0]["controller_seat_id"] = 2
    assert battlefield_channel_note(s) == ""


def test_typed_land_prompt_explains_channel_cost_without_banning_play():
    s = state()
    prompts = []

    class Backend:
        def complete(self, system, user, **kwargs):
            prompts.append(user)
            return '{"option_ids":["idx:0"],"reasoning":"Need the white source."}'

    decision = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "ActionsAvailable",
            "can_pass": True,
            "actions": [
                {"actionType": "ActionType_Play", "instanceId": 10, "grpId": 100},
                {"actionType": "ActionType_Pass"},
            ],
        },
        resolve_name=lambda _: CARD["name"],
    )
    planner = ActionPlanner(Backend(), timeout=1)
    assert planner.plan_decision_options(decision, s) == ["idx:0"]
    assert "Channel ability requires discarding it from hand" in prompts[0]
    assert planner._last_decision_reasoning == "Need the white source."


def test_legacy_prompt_does_not_reserve_battlefield_channel():
    s = state()
    s["battlefield"] = [dict(CARD)]
    s["hand"] = []
    prompt = ActionPlanner(object(), land_drop_first=False)._build_action_prompt(
        s, "decision_required", ["Pass"], {"type": "actions_available"}
    )
    assert "CHANNEL ZONES: Eiganjo, Seat of the Empire" in prompt
    assert "Those battlefield copies cannot" in prompt
