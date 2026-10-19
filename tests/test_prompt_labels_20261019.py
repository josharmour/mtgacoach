"""Prompt and label bugs from the 2026-10-19 ladder run (ledger #40, #41, #44, #53, #69, #73).

Shapes come from that day's Player.log copies: match 1e4ccb0c (Perforating
Artist's raid trigger, msgId 455, gameStateId 351) and match 115944ce (Iroh's
Demonstration's CastingTimeOptionsReq, msgId 262, gameStateId 191).
"""

from __future__ import annotations

import logging
import sqlite3
from types import SimpleNamespace

import pytest

import arenamcp.decisions as decisions
import arenamcp.server as server_module
from arenamcp.action_planner import ACTION_SCHEMA, ActionPlanner, ActionType
from arenamcp.coach import CoachEngine
from arenamcp.decisions import build_pending_decision, prompt_choice_ids, select_n_discard_note
from arenamcp.gamestate import GameState
from arenamcp.gamestate_decisions import _handle_decision_message
from arenamcp.gamestate_models import GameObject, Player
from arenamcp.mtgadb import MTGADatabase
from arenamcp.rules_engine import RulesEngine

ARTIST = (
    "<i>Raid</i><nobr> —</nobr> At the beginning of your end step, if you attacked this turn, each "
    "opponent loses 3 life unless that player sacrifices a nonland permanent of their choice or discards a card."
)
PROMPTS = {1216: "Sacrifice a nonland permanent", 1217: "Discard a card", 1218: "Lose 3 life"}

# Player.log, match 1e4ccb0c, 10:14:56 (T14, opponent's end step).
LOG_SELECT_N_REQ = {
    "type": "GREMessageType_SelectNReq",
    "systemSeatIds": [1],
    "msgId": 455,
    "gameStateId": 351,
    "prompt": {
        "promptId": 112,
        "parameters": [
            {"parameterName": "CardId", "type": "ParameterType_Number", "numberValue": 351},
            {"parameterName": "CardId", "type": "ParameterType_Number", "numberValue": 1216},
            {"parameterName": "CardId", "type": "ParameterType_Number", "numberValue": 1217},
            {"parameterName": "CardId", "type": "ParameterType_Number", "numberValue": 1218},
        ],
    },
    "selectNReq": {
        "minSel": 1,
        "maxSel": 1,
        "context": "SelectionContext_Resolution",
        "optionContext": "OptionContext_Resolution",
        "listType": "SelectionListType_Dynamic",
        "ids": [9, 11],
        "prompt": {
            "parameters": [
                {"parameterName": "Parameter", "type": "ParameterType_PromptId", "promptId": 1216},
                {"parameterName": "Parameter", "type": "ParameterType_PromptId", "promptId": 1217},
                {"parameterName": "Parameter", "type": "ParameterType_PromptId", "promptId": 1218},
            ]
        },
        "idType": "IdType_PromptParameterIndex",
        "unfilteredIds": [8, 9, 11],
        "sourceId": 351,
        "validationType": "SelectionValidationType_NonRepeatable",
        "minWeight": -2147483648,
        "maxWeight": 2147483647,
    },
    "allowCancel": "AllowCancel_No",
}


def _mac_poll(*, with_payload: bool) -> dict:
    """The IL2CPP bridge's poll for that request (protobuf oneof fields reflected as value/valueCase)."""
    poll = {
        "has_pending": True,
        "request_type": "SelectN",
        "request_class": "SelectNRequest",
        "game_state_id": 351,
        "msg_id": 455,
        "select_n_ids": [9, 11],
        "select_n_id_type": "PromptParameterIndex",
        "select_n_list_type": "Dynamic",
        "select_n_context": "Resolution",
        "select_n_min": 1,
        "select_n_max": 1,
    }
    if with_payload:
        poll["request_payload"] = {
            "requestType": "SelectN",
            "requestClass": "SelectNRequest",
            "ids": [9, 11],
            "unfilteredIds": [8, 9, 11],
            "reqPrompt": {
                "parameters": [
                    {"parameterName": "Parameter", "type": "PromptId", "value": pid, "valueCase": "PromptId"}
                    for pid in (1216, 1217, 1218)
                ]
            },
            "sourceId": 351,
        }
    return poll


def _names(iid: int) -> str:
    return {351: "Perforating Artist"}.get(iid, "")


# --- #40: prompt-parameter choices are named ----------------------------------------


def test_prompt_choice_ids_pair_unfiltered_ids_with_prompt_parameters():
    assert prompt_choice_ids(LOG_SELECT_N_REQ["selectNReq"]) == {8: 1216, 9: 1217, 11: 1218}
    assert prompt_choice_ids(_mac_poll(with_payload=True)["request_payload"]) == {8: 1216, 9: 1217, 11: 1218}
    # A CardId-only prompt is not a prompt-choice request.
    assert prompt_choice_ids({"unfilteredIds": [351], "prompt": LOG_SELECT_N_REQ["prompt"]}) == {}


def test_mtga_database_resolves_prompt_text(tmp_path):
    path = tmp_path / "Raw_CardDatabase_test.mtga"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE Cards (GrpId INTEGER PRIMARY KEY, TitleId INTEGER);
        CREATE TABLE Prompts (Id INTEGER PRIMARY KEY, LocId INTEGER);
        CREATE TABLE Localizations_enUS (LocId INTEGER, Formatted INTEGER, Loc TEXT);
        INSERT INTO Prompts VALUES (1218, 128893);
        INSERT INTO Localizations_enUS VALUES (128893, 1, 'Lose 3 life');
        """
    )
    conn.commit()
    conn.close()
    db = MTGADatabase(path)
    assert db.get_prompt_text(1218) == "Lose 3 life"
    assert db.get_prompt_text(9999) is None


def test_bridge_poll_labels_each_choice_with_its_prompt_text(monkeypatch):
    monkeypatch.setattr(decisions, "_live_game_state", lambda: None)
    decision = build_pending_decision(
        _mac_poll(with_payload=True), resolve_instance=_names, resolve_prompt=PROMPTS.get
    )
    assert decision is not None and decision.request_type == "SelectN"
    assert [(o.option_id, o.label) for o in decision.options] == [
        ("sel:9", "Discard a card"),
        ("sel:11", "Lose 3 life"),
    ]
    assert decision.source_label == "Perforating Artist"
    assert all(o.meta["identity_known"] and o.meta["sourceId"] == 351 for o in decision.options)


def _log_state(monkeypatch) -> GameState:
    monkeypatch.setattr(decisions, "prompt_text", lambda pid: PROMPTS.get(pid, ""))
    monkeypatch.setattr(
        server_module,
        "enrich_with_oracle_text",
        lambda grp_id: {"name": "Perforating Artist", "type_line": "Creature — Devil", "oracle_text": ARTIST},
    )
    state = GameState()
    state.set_local_seat_id(1, source=2)
    state.game_objects[351] = GameObject(
        instance_id=351, grp_id=93837, zone_id=28, owner_seat_id=2, controller_seat_id=2, power=3, toughness=2
    )
    _handle_decision_message(state, "GREMessageType_SelectNReq", LOG_SELECT_N_REQ)
    return state


def test_log_parser_names_the_choices_and_the_asking_card(monkeypatch):
    state = _log_state(monkeypatch)
    context = state.decision_context
    assert context["option_cards"] == ["Discard a card", "Lose 3 life"]
    assert context["prompt_choices"] == [
        {"id": 9, "prompt_id": 1217, "text": "Discard a card"},
        {"id": 11, "prompt_id": 1218, "text": "Lose 3 life"},
    ]
    assert context["source_card"] == "Perforating Artist"
    assert "loses 3 life unless" in context["source_oracle_text"]
    # The asking permanent sits on the battlefield: no stack-based auto-clear key.
    assert "source_id" not in context and context["prompt_source_id"] == 351


def test_bridge_poll_without_pairing_uses_the_log(monkeypatch):
    state = _log_state(monkeypatch)
    monkeypatch.setattr(decisions, "_live_game_state", lambda: state)
    decision = build_pending_decision(_mac_poll(with_payload=False), resolve_instance=lambda _iid: "")
    assert [o.label for o in decision.options] == ["Discard a card", "Lose 3 life"]
    assert decision.source_label == "Perforating Artist"


def test_unresolved_prompt_text_is_not_dressed_up_as_a_card(monkeypatch):
    monkeypatch.setattr(decisions, "_live_game_state", lambda: None)
    decision = build_pending_decision(
        _mac_poll(with_payload=True), resolve_instance=_names, resolve_prompt=lambda _pid: ""
    )
    assert [o.label for o in decision.options] == [
        "Unlabeled choice 1 of 2 (prompt 1217)",
        "Unlabeled choice 2 of 2 (prompt 1218)",
    ]
    assert not any(o.meta["identity_known"] for o in decision.options)


def test_choice_note_quotes_the_opponents_rules_text(monkeypatch):
    monkeypatch.setattr(decisions, "_live_game_state", lambda: None)
    decision = build_pending_decision(
        _mac_poll(with_payload=True), resolve_instance=_names, resolve_prompt=PROMPTS.get
    )
    state = {
        "local_seat_id": 1,
        "battlefield": [
            {"instance_id": 351, "name": "Perforating Artist", "controller_seat_id": 2, "oracle_text": ARTIST}
        ],
        "hand": [{"instance_id": 362, "name": "Fleeting Effigy"}],
    }
    note = select_n_discard_note(decision, state)
    assert note.startswith("CHOICE FROM Perforating Artist (OPPONENT's card")
    assert "each opponent loses 3 life unless that player sacrifices a nonland permanent" in note
    assert "<" not in note


class _Backend:
    def __init__(self, reply):
        self.reply = reply
        self.prompts: list[str] = []

    def available(self):
        return True

    def complete(self, system, user, *args, **kwargs):
        if "PENDING DECISION" in user:
            self.prompts.append(user)
        return self.reply


def test_typed_decision_prompt_shows_the_choices_and_their_source(monkeypatch):
    monkeypatch.setattr(decisions, "_live_game_state", lambda: None)
    decision = build_pending_decision(
        _mac_poll(with_payload=True), resolve_instance=_names, resolve_prompt=PROMPTS.get
    )
    state = {
        "match_id": "1e4ccb0c",
        "local_seat_id": 1,
        "turn": {"turn_number": 14, "active_player": 2, "priority_player": 1, "phase": "Phase_Ending"},
        "players": [
            {"seat_id": 1, "life_total": 8, "is_local": True},
            {"seat_id": 2, "life_total": 12, "is_local": False},
        ],
        "battlefield": [
            {
                "instance_id": 351,
                "name": "Perforating Artist",
                "controller_seat_id": 2,
                "owner_seat_id": 2,
                "power": 3,
                "toughness": 2,
                "type_line": "Creature — Devil",
                "card_types": ["CardType_Creature"],
                "oracle_text": ARTIST,
            }
        ],
        "hand": [{"instance_id": 362, "name": "Fleeting Effigy", "type_line": "Creature — Elemental"}],
        "graveyard": [],
        "stack": [],
    }
    backend = _Backend('{"option_ids": ["sel:11"], "reasoning": "Keep the Effigy; 3 life is affordable."}')
    planner = ActionPlanner(backend, timeout=5, land_drop_first=False)
    assert planner.plan_decision_options(decision, state) == ["sel:11"]
    prompt = backend.prompts[0]
    assert "PENDING DECISION: SelectN (source: Perforating Artist)" in prompt
    assert "CHOICE FROM Perforating Artist (OPPONENT's card" in prompt
    assert "- sel:9: Discard a card" in prompt and "- sel:11: Lose 3 life" in prompt
    assert "Option 11" not in prompt


def test_coach_prompt_lists_the_named_choices(monkeypatch):
    state = _log_state(monkeypatch)
    lines = CoachEngine.__new__(CoachEngine)._format_decision_lines(
        {"pending_decision": state.pending_decision, "decision_context": state.decision_context}
    )
    assert lines[0] == "!!! DECISION: CHOOSE ONE for Perforating Artist !!!"
    assert any("loses 3 life unless" in line for line in lines)
    assert "Options: Discard a card, Lose 3 life" in lines


# --- #53: player targets say whose face they are ------------------------------------


def test_player_targets_are_labelled_with_owner_and_life(monkeypatch):
    live = SimpleNamespace(
        players={1: Player(seat_id=1, life_total=13), 2: Player(seat_id=2, life_total=12)},
        local_seat_id=1,
        opponent_name="Theseus",
        decision_context={},
    )
    monkeypatch.setattr(decisions, "_live_game_state", lambda: live)
    poll = {
        "has_pending": True,
        "request_type": "SelectTargets",
        "target_candidates": [
            {"targetInstanceId": 1, "targetIdx": 1, "grpId": 0},
            {"targetInstanceId": 2, "targetIdx": 1, "grpId": 0},
            {"targetInstanceId": 351, "targetIdx": 1, "grpId": 0},
        ],
        "target_selections": [{"targetIdx": 1, "minTargets": 1, "maxTargets": 1}],
    }
    decision = build_pending_decision(poll, resolve_instance=_names)
    assert decision.find("tgt:1").label == "You (seat 1, 13 life)"
    assert decision.find("tgt:2").label == "Opponent Theseus (12 life)"
    assert decision.find("tgt:2").meta["player_seat"] == 2
    assert decision.find("tgt:351").label == "Perforating Artist"


# --- #41 / #69: deterministic board facts -------------------------------------------


def _creature(iid, name, ctrl, power, toughness, *, tapped=False, text="", type_line="Creature"):
    return {
        "instance_id": iid,
        "name": name,
        "controller_seat_id": ctrl,
        "owner_seat_id": ctrl,
        "power": power,
        "toughness": toughness,
        "type_line": type_line,
        "card_types": ["CardType_Creature"] if "Creature" in type_line else ["CardType_Enchantment"],
        "oracle_text": text,
        "is_tapped": tapped,
        "turn_entered_battlefield": 5,
    }


def _main1(battlefield: list[dict]) -> dict:
    return {
        "local_seat_id": 1,
        "turn": {"turn_number": 13, "active_player": 1, "priority_player": 1, "phase": "Phase_Main1"},
        "players": [
            {"seat_id": 1, "life_total": 13, "is_local": True, "lands_played": 1},
            {"seat_id": 2, "life_total": 12, "is_local": False, "lands_played": 1},
        ],
        "battlefield": battlefield,
        "hand": [],
        "graveyard": [],
    }


EFFIGY = "Haste\n{o2oR}: CARDNAME gets +2/+0 until end of turn."


def test_attack_prompt_names_their_untapped_blockers():
    # 1e4ccb0c T13: "All opposing creatures are tapped" while the Artist stood untapped.
    board = [
        _creature(362, "Fleeting Effigy", 1, 2, 2, text=EFFIGY),
        _creature(301, "Diregraf Ghoul", 2, 2, 2, tapped=True),
        _creature(351, "Perforating Artist", 2, 3, 2, text=ARTIST),
        _creature(330, "Slumbering Cerberus", 2, 4, 2, tapped=True),
    ]
    context = CoachEngine.__new__(CoachEngine)._format_game_context(_main1(board), for_planner=True)
    assert (
        "Their untapped potential blockers: Perforating Artist 3/2 "
        "(tapped, can't block: Diregraf Ghoul, Slumbering Cerberus)"
    ) in context

    board[2] = {**board[2], "is_tapped": True}
    context = CoachEngine.__new__(CoachEngine)._format_game_context(_main1(board), for_planner=True)
    assert "Their untapped potential blockers: NONE — all of their creatures are tapped" in context


def test_static_buffs_say_they_are_already_in_the_shown_pt():
    # 115944ce 11:11:35: "the anthemed 6-toughness Trostani" — a 5/5 that already counted the anthem.
    anthem = _creature(
        410, "Anthem of Champions", 2, None, None, text="Creatures you control get +1/+1.", type_line="Enchantment"
    )
    board = [
        _creature(362, "Fleeting Effigy", 1, 2, 2, text=EFFIGY),
        _creature(400, "Trostani", 2, 5, 5),
        anthem,
    ]
    context = CoachEngine.__new__(CoachEngine)._format_game_context(_main1(board), for_planner=True)
    anthem_line = next(line for line in context.split("\n") if line.startswith("  Anthem of Champions"))
    assert anthem_line.endswith("(static P/T buff: already included in the P/T shown)")
    effigy_line = next(line for line in context.split("\n") if line.startswith("  Fleeting Effigy"))
    assert "already included" not in effigy_line

    buff = CoachEngine._static_pt_buff
    assert buff("Enchant creature\nEnchanted creature gets +2/+2.")
    assert buff("Equipped creature gets +1/+0 and has haste.\nEquip {o2}")
    assert buff("Other Elves you control get +1/+1.")
    assert not buff("Whenever you cast a spell, this creature gets +1/+1 until end of turn.")
    assert not buff("When this creature enters, target creature an opponent controls gets -3/-3.")
    assert not buff("{oT}: Target creature gets +1/+1.")
    assert not buff("-3: Creatures you control get +2/+2.")


# --- #44: the schema's pick placeholder ----------------------------------------------


class _NoBackend:
    def complete(self, *a, **k):
        raise AssertionError("parser tests must not call the LLM")


def _planner(menu: list[str]) -> ActionPlanner:
    planner = ActionPlanner(_NoBackend())
    planner._last_menu = list(menu)
    return planner


ATTACK_MENU = ["Attack with: Fleeting Effigy (2/2)", "Attack with: Hired Claw (1/2)", "Done (confirm attackers)"]


def test_schema_pick_is_one_based_and_optional():
    assert '"pick": 0' not in ACTION_SCHEMA
    assert "1-based" in ACTION_SCHEMA and "OMIT for structured actions" in ACTION_SCHEMA


def test_echoed_pick_never_replaces_a_structured_attack(caplog):
    planner = _planner(ATTACK_MENU)
    response = (
        '{"actions": [{"pick": 1, "action_type": "declare_attackers", "attacker_names": ["Hired Claw"],'
        ' "attacker_targets": {"Hired Claw": "Opponent"}}]}'
    )
    plan = planner._parse_response(response, ATTACK_MENU)
    assert len(plan.actions) == 1
    assert plan.actions[0].action_type == ActionType.DECLARE_ATTACKERS
    assert plan.actions[0].attacker_names == ["Hired Claw"]

    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        plan = planner._parse_response(
            '{"actions": [{"pick": 0, "action_type": "declare_attackers", "attacker_names": ["Fleeting Effigy"]}]}',
            ATTACK_MENU,
        )
    assert "out of menu range" not in caplog.text
    assert plan.actions and plan.actions[0].attacker_names == ["Fleeting Effigy"]


def test_echoed_done_pick_never_replaces_structured_blocks():
    menu = ["Block with: Soldier (1/1)", "Done (confirm blockers)"]
    planner = _planner(menu)
    plan = planner._parse_response(
        '{"actions": [{"pick": 2, "action_type": "declare_blockers", "blocker_assignments": {"Soldier": "Giant"}}]}',
        menu,
        decision_context={"type": "declare_blockers"},
        bridge_request="DeclareBlockers",
    )
    assert len(plan.actions) == 1
    assert plan.actions[0].action_type == ActionType.DECLARE_BLOCKERS
    assert plan.actions[0].blocker_assignments == {"Soldier": "Giant"}


def test_a_bare_attack_pick_still_reads_the_menu():
    planner = _planner(ATTACK_MENU)
    plan = planner._parse_response('{"actions": [{"pick": 2}]}', ATTACK_MENU)
    assert plan.actions[0].action_type == ActionType.DECLARE_ATTACKERS
    assert plan.actions[0].attacker_names == ["Hired Claw"]


# --- assign damage menu entry ---------------------------------------------------------


def test_assign_damage_pick_carries_the_structured_assignment(caplog):
    # 11:23:58: "Planner pick 1 ('Assign damage (order targets by priority)') has no executable mapping".
    menu = ["Assign damage (order targets by priority)", "Done"]
    planner = _planner(menu)
    response = '{"actions": [{"pick": 1, "distribution": {"Trostani": 5, "Opponent": 2}, "reasoning": "trample"}]}'
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        plan = planner._parse_response(
            response, menu, decision_context={"type": "assign_damage"}, bridge_request="AssignDamage"
        )
    assert "no executable mapping" not in caplog.text
    assert len(plan.actions) == 1
    assert plan.actions[0].action_type == ActionType.ASSIGN_DAMAGE
    assert plan.actions[0].distribution == {"Trostani": 5, "Opponent": 2}


# --- #73: casting-time menus name the real modes --------------------------------------

IROH_REQ = {
    "type": "GREMessageType_CastingTimeOptionsReq",
    "systemSeatIds": [2],
    "msgId": 262,
    "gameStateId": 191,
    "prompt": {"promptId": 23},
    "castingTimeOptionsReq": {
        "castingTimeOptionReq": [
            {
                "ctoId": 2,
                "castingTimeOptionType": "CastingTimeOptionType_Modal",
                "affectedId": 327,
                "affectorId": 327,
                "grpId": 97417,
                "playerIdToPrompt": 2,
                "isRequired": True,
                "modalReq": {
                    "modalOptions": [{"grpId": 87912}, {"grpId": 21773}],
                    "abilityGrpId": 192719,
                    "minSel": 1,
                    "maxSel": 1,
                },
            }
        ]
    },
    "allowCancel": "AllowCancel_Abort",
    "allowUndo": True,
}
MODES = {
    87912: "CARDNAME deals 1 damage to each creature your opponents control.",
    21773: "CARDNAME deals 4 damage to target creature.",
}


@pytest.fixture
def mode_texts(monkeypatch):
    import arenamcp.card_db as card_db

    monkeypatch.setattr(
        card_db, "get_card_database", lambda: SimpleNamespace(get_ability_text=lambda grp_id: MODES.get(grp_id))
    )
    monkeypatch.setattr(
        server_module,
        "enrich_with_oracle_text",
        lambda grp_id: {"name": "Iroh's Demonstration", "type_line": "Sorcery", "oracle_text": ""},
    )


def _iroh_state() -> GameState:
    state = GameState()
    state.set_local_seat_id(2, source=2)
    state.game_objects[327] = GameObject(instance_id=327, grp_id=97417, zone_id=27, owner_seat_id=2)
    _handle_decision_message(state, "GREMessageType_CastingTimeOptionsReq", IROH_REQ)
    return state


def test_casting_menu_lists_the_spells_modes(mode_texts):
    state = _iroh_state()
    context = state.decision_context
    # The spell being cast is the source (it is on the stack), so the modes outlive the 5 s auto-clear.
    assert context["source_id"] == 327 and context["source_card"] == "Iroh's Demonstration"
    assert RulesEngine.get_legal_actions({"decision_context": context}) == [
        "Mode 0: Iroh's Demonstration deals 1 damage to each creature your opponents control.",
        "Mode 1: Iroh's Demonstration deals 4 damage to target creature.",
    ]
    lines = CoachEngine.__new__(CoachEngine)._format_casting_time_options({}, context)
    assert lines[0] == "!!! DECISION: CHOOSE MODE (2 options) !!!"
    assert "  modal_index=1: Iroh's Demonstration deals 4 damage to target creature." in lines


def test_a_mode_pick_maps_to_its_modal_index(mode_texts):
    menu = RulesEngine.get_legal_actions({"decision_context": _iroh_state().decision_context})
    planner = _planner(menu)
    plan = planner._parse_response(
        '{"actions": [{"pick": 2, "reasoning": "4 damage to Trostani"}]}',
        menu,
        decision_context={"type": "casting_time_options"},
        bridge_request="CastingTimeOptions",
    )
    assert len(plan.actions) == 1
    assert plan.actions[0].action_type == ActionType.MODAL_CHOICE
    assert plan.actions[0].modal_index == 1


def test_unknown_casting_options_are_not_invented():
    menu = RulesEngine.get_legal_actions({"decision_context": {"type": "casting_time_options"}})
    assert menu == ["Cast normally"]


def test_kicker_menu_offers_the_kicker_and_its_cost():
    # Burst Lightning's live request (115944ce): Kicker {4} plus Done.
    raw = {
        "castingTimeOptionReq": [
            {
                "ctoId": 2,
                "castingTimeOptionType": "CastingTimeOptionType_Kicker",
                "affectedId": 293,
                "grpId": 2852,
                "manaCost": [{"color": ["ManaColor_Red"], "count": 1}, {"color": ["ManaColor_Generic"], "count": 4}],
            },
            {"castingTimeOptionType": "CastingTimeOptionType_Done", "isRequired": True},
        ]
    }
    menu = RulesEngine.get_legal_actions({"decision_context": {"type": "casting_time_options", "raw": raw}})
    assert menu == ["Cast normally", "Pay kicker (total cost {4}{R})"]
