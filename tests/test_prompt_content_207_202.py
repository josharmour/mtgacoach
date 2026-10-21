from arenamcp.action_planner import ActionPlanner, ActionType, GameAction
from arenamcp.decisions import build_pending_decision
from arenamcp.gamestate import GameObject, GameState
from arenamcp.gamestate_decisions import _handle_decision_message
from arenamcp.optional_prompts import prompt_cards, prompt_note


def test_optional_action_prompt_renders_parameterized_prompt_and_source_oracle():
    gs = GameState()
    gs.local_seat_id = 1
    gs.game_objects[101] = GameObject(
        instance_id=101,
        grp_id=999001,
        zone_id=1,
        owner_seat_id=1,
        controller_seat_id=1,
    )
    gs._resolve_card_name = lambda grp_id: "Midnight Reaper" if grp_id == 999001 else ""

    msg = {
        "type": "GREMessageType_OptionalActionMessage",
        "gameStateId": 42,
        "msgId": 7,
        "optionalActionMessage": {
            "sourceId": 101,
            "optionalActionTypes": ["CardMechanicType_DrawCard"],
            "recipientIds": [101],
            "prompt": {
                "promptId": 1,
                "parameters": [
                    {"parameterName": "CardId", "value": 101},
                ],
            },
        },
    }
    _handle_decision_message(gs, "GREMessageType_OptionalActionMessage", msg)
    assert gs.pending_decision == "Optional Action"
    assert gs.decision_context["prompt_cards"]
    assert gs.decision_context["prompt_cards"][0]["name"] == "Midnight Reaper"


def test_optional_prompt_note_resolves_parent_source_card_and_zone_transfer():
    poll = {
        "has_pending": True,
        "game_state_id": 50,
        "msg_id": 9,
        "request_type": "OptionalActionMessageRequest",
        "source_instance_id": 302,
        "optional_mechanics": ["CardMechanicType_ZoneTransfer"],
        "optional_recipients": [201],
        "prompt": {
            "promptId": 144,
            "parameters": [{"parameterName": "CardId", "Value": 201}],
        },
    }
    names = {201: "rankle, master of pranks", 302: ""}
    dec = build_pending_decision(poll, resolve_instance=lambda iid: names.get(iid, ""))
    assert dec is not None
    assert "command zone" in dec.prompt_text.lower()
    assert "rankle, master of pranks" in dec.source_label.lower()

    state = {
        "local_seat_id": 1,
        "stack": [
            {
                "instance_id": 302,
                "name": "Ability (ID:99)",
                "oracle_text": "When this creature dies, return it to its owner's hand.",
                "source_card": {
                    "instance_id": 201,
                    "name": "Rankle, Master of Pranks",
                    "oracle_text": "Flying, haste",
                    "controller_seat_id": 1,
                },
            }
        ],
    }
    cards = prompt_cards(dec, state)
    assert cards
    assert "rankle" in cards[0]["name"].lower()
    note = prompt_note(dec, state)
    assert "Rankle, Master of Pranks" in note
    assert "The card asking is not identified" not in note


def test_select_mode_legal_action_preserves_index_and_description():
    v = ActionPlanner(backend=None)
    act = v._legal_action_to_action("Select Mode 2: Target player sacrifices a creature")
    assert act is not None
    assert act.action_type == ActionType.MODAL_CHOICE
    assert act.modal_index == 2
    assert "Target player sacrifices a creature" in act.card_name
    assert "mode 2" in str(act).lower()

    # Matching by modal_index succeeds even if card_name phrasing differs
    candidate = GameAction(action_type=ActionType.MODAL_CHOICE, card_name="Mode 2", modal_index=2)
    assert v._is_legal_default(candidate, ["Select Mode 2: Target player sacrifices a creature"])
