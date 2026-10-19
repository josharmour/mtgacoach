"""A hand card we must give up is a LOSS, never a cast (ledger #79, match 772233af, 2026-10-19).

T3, 11:35:23 (Player.log line 196282): Unscrupulous Agent ("When this creature
enters, target opponent exiles a card from their hand") resolved. Arena sent
seat 1 a SelectNReq over our six hand cards, ids [280 Tectonic Hazard,
281 Lightning Strike, 283 Tectonic Hazard, 285 Mountain, 344 Shock,
349 Fleeting Effigy], with the message's prompt 1117 ("Exile a card." in the
card DB's Prompts table, parameters CardId 353, CardId 1) and sourceId 353, the
Agent. We had one Mountain in play. The prompt id was dropped and the decision
had no source (packet: source_label "", source_oracle_text ""), so the model
answered "Killing the flying Faerie Dreamthief ... Shock is the cheapest
efficient answer" and exiled Shock for good. The second Hazard was the card
to lose.
"""

from __future__ import annotations

import sqlite3

import pytest
from tests.fakes import planner_with

from arenamcp import arena_prompts, decisions
from arenamcp.decisions import (
    DecisionOption,
    PendingDecision,
    build_pending_decision,
    decision_from_dict,
    decision_to_dict,
    hand_loss,
    hand_loss_choice,
    select_n_discard_note,
    select_n_note,
)
from arenamcp.gamestate import GameObject, GameState
from arenamcp.gamestate_decisions import _auto_clear_stale_decision, _handle_decision_message
from arenamcp.gamestate_models import Zone, ZoneType

AGENT_TEXT = "When Unscrupulous Agent enters, target opponent exiles a card from their hand."
PROMPTS = {
    1117: "Exile a card.",
    1024: "Discard a card.",
    153: "Discard {2, plural, =1 {a card. (If you discard a nonland card, put a <nobr>+1/+1</nobr> counter on "
    "the conniving creature)} other {{2, Number} cards.}}",
    14: "{1, plural, =1 {Discard a card.} other {Discard {1, Number} cards.}}",
    1: "#Deprecated PromptMessage_GameStateMessage",
}


def _card(iid, name, type_line, cost="", text="", seat=1, **extra):
    return {
        "instance_id": iid,
        "name": name,
        "type_line": type_line,
        "mana_cost": cost,
        "oracle_text": text,
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        **extra,
    }


HAZARD = (
    "Tectonic Hazard",
    "Sorcery",
    "{R}",
    "Tectonic Hazard deals 1 damage to each opponent and each creature they control.",
)
STRIKE = ("Lightning Strike", "Instant", "{1}{R}", "Lightning Strike deals 3 damage to any target.")
SHOCK = ("Shock", "Instant", "{R}", "Shock deals 2 damage to any target.")
EFFIGY = (
    "Fleeting Effigy", "Creature — Elemental", "{R}",
    "At the beginning of your end step, return Fleeting Effigy to its owner's hand.\n"
    "{2}{R}: Fleeting Effigy gets +2/+0 until end of turn.",
)  # fmt: skip
MOUNTAIN = ("Mountain", "Basic Land — Mountain", "", "({T}: Add {R}.)")

LIVE_HAND = [
    _card(280, *HAZARD),
    _card(281, *STRIKE),
    _card(283, *HAZARD),
    _card(285, *MOUNTAIN),
    _card(344, *SHOCK),
    _card(349, *EFFIGY),
]
LIVE_IDS = [280, 281, 283, 285, 344, 349]


def live_state(hand=None, lands_in_play=1):
    """Seat 1 at T3, 11:35:23: one Mountain in play; the Agent and Faerie Dreamthief on their side."""
    ours = [_card(345 + i * 1000, *MOUNTAIN) for i in range(lands_in_play)]
    theirs = [
        _card(341, "Faerie Dreamthief", "Creature — Faerie Rogue", "{B}", "Flying", seat=2, power=1, toughness=1),
        _card(353, "Unscrupulous Agent", "Creature — Elf Detective", "{1}{B}", AGENT_TEXT, seat=2, power=1, toughness=1),
    ]  # fmt: skip
    return {
        "turn": {"turn_number": 3, "active_player": 2, "phase": "Phase_Main1"},
        "players": [{"seat_id": 1, "is_local": True, "life_total": 20}, {"seat_id": 2, "life_total": 18}],
        "local_seat_id": 1,
        "hand": list(LIVE_HAND if hand is None else hand),
        "battlefield": ours + theirs,
        "stack": [],
        "graveyard": [],
        "_bridge_request_payload": {"requestType": "SelectN", "sourceId": 353},
    }


def mac_poll(ids=LIVE_IDS, prompt_id=1117, min_sel=1, max_sel=1):
    """The native Mac bridge's SelectN shape (request_payload carries the prompt and sourceId)."""
    return {
        "has_pending": True,
        "request_type": "SelectN",
        "request_class": "SelectNRequest",
        "game_state_id": 68,
        "msg_id": 102,
        "select_n_ids": list(ids),
        "select_n_id_type": "InstanceId",
        "select_n_list_type": "Dynamic",
        "select_n_context": "Resolution",
        "select_n_min": min_sel,
        "select_n_max": max_sel,
        "request_payload": {
            "requestType": "SelectN",
            "requestClass": "SelectNRequest",
            "prompt": {
                "promptId": prompt_id,
                "parameters": [
                    {"parameterName": "CardId", "type": "Number", "value": 353, "valueCase": "NumberValue"},
                    {"parameterName": "CardId", "type": "Number", "value": 1, "valueCase": "NumberValue"},
                ],
            },
            "sourceId": 353,
        },
    }


@pytest.fixture(autouse=True)
def prompt_db(monkeypatch):
    monkeypatch.setattr(arena_prompts, "_lookup_template", lambda identifier: PROMPTS.get(identifier, ""))


def _decision(state=None, **poll):
    state = state or live_state()
    names = {int(c["instance_id"]): c["name"] for zone in ("hand", "battlefield") for c in state[zone]}
    return build_pending_decision(mac_poll(**poll), resolve_instance=lambda iid: names.get(iid, ""))


# --- the prompt text ------------------------------------------------------------


def test_the_card_db_prompts_table_gives_arenas_words(tmp_path):
    from arenamcp.mtgadb import MTGADatabase

    path = tmp_path / "Raw_CardDatabase_test.mtga"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE Cards(GrpId INT, TitleId INT, ExpansionCode TEXT);
        CREATE TABLE Prompts(Id INT UNIQUE PRIMARY KEY NOT NULL, LocId INT NOT NULL);
        CREATE TABLE Localizations_enUS(LocId INT NOT NULL, Formatted INT NOT NULL, Loc TEXT,
                                        PRIMARY KEY (LocId, Formatted));
        INSERT INTO Prompts VALUES (1117, 258553), (153, 900);
        INSERT INTO Localizations_enUS VALUES (258553, 0, 'Exile a card.'), (258553, 1, 'Exile a card.'),
            (900, 1, 'Discard {2, plural, =1 {a card. (put a <nobr>+1/+1</nobr> counter)} other {x}}');
        """
    )
    con.commit()
    con.close()
    db = MTGADatabase(path)
    assert db.get_prompt_text(1117) == "Exile a card."
    assert "<nobr>" not in db.get_prompt_text(153)
    assert db.get_prompt_text(4242) is None
    db.close()
    # An older DB build without the table answers None and stays usable.
    old = tmp_path / "Raw_CardDatabase_old.mtga"
    con = sqlite3.connect(old)
    con.executescript("CREATE TABLE Cards(GrpId INT, TitleId INT, ExpansionCode TEXT);")
    con.close()
    db = MTGADatabase(old)
    assert db.get_prompt_text(1117) is None and db.available
    db.close()


def test_prompt_templates_fill_counts_and_card_names():
    connive = arena_prompts.prompt_text({"promptId": 153, "parameters": [
        {"parameterName": "CardId", "numberValue": 7}, {"parameterName": "Number", "numberValue": 1}]})  # fmt: skip
    assert connive.startswith("Discard a card. (If you discard a nonland card, put a +1/+1 counter")
    two = arena_prompts.prompt_text(
        {"promptId": 14, "parameters": [{"parameterName": "Number", "numberValue": 2}]}
    )
    assert two == "Discard 2 cards."
    assert arena_prompts.prompt_text({"promptId": 1}) == ""  # deprecated text says nothing
    assert arena_prompts.prompt_text({"parameters": []}) == ""


def test_the_bridge_decision_carries_the_prompt_text():
    decision = _decision()
    assert decision.request_type == "SelectN" and decision.prompt_text == "Exile a card."
    # The fingerprint's source_label stays resolver-independent; the note names the card.
    assert decision.source_label == ""
    # Match packets keep Arena's words for the next review.
    assert decision_to_dict(decision)["prompt_text"] == "Exile a card."
    assert decision_from_dict(decision_to_dict(decision)) == decision


# --- the framing ----------------------------------------------------------------


def test_the_agents_exile_is_a_loss_not_a_cast():
    state = live_state()
    loss = hand_loss(_decision(state), state)
    assert loss is not None and (loss.kind, loss.source, loss.unusual) == ("exile", "Unscrupulous Agent", "")
    note = select_n_note(_decision(state), state)
    assert note.startswith("THIS IS A LOSS, NOT A CAST: the card you pick is EXILED from your hand for good")
    assert "Nothing is cast or targeted" in note
    assert 'Arena asks: "Exile a card."' in note
    assert "asked by Unscrupulous Agent (When Unscrupulous Agent enters, target opponent exiles" in note


def test_without_the_prompt_the_agents_rules_text_still_says_exile(monkeypatch):
    monkeypatch.setattr(arena_prompts, "_lookup_template", lambda identifier: "")
    state = live_state()
    loss = hand_loss(_decision(state), state)
    assert loss is not None and loss.kind == "exile"
    assert AGENT_TEXT in loss.evidence


def test_an_unknown_hand_choice_still_says_it_casts_nothing(monkeypatch):
    monkeypatch.setattr(arena_prompts, "_lookup_template", lambda identifier: "")
    state = live_state()
    state["battlefield"] = [c for c in state["battlefield"] if c["instance_id"] != 353]
    state["_bridge_request_payload"] = {}
    decision = _decision(state)
    assert hand_loss(decision, state) is None
    assert "choosing one does NOT cast or play it" in select_n_note(decision, state)


def test_named_alternatives_and_hand_losses_each_get_their_own_note():
    """#40's prompt-parameter choices keep "CHOICE FROM <card>" alone; a hand loss keeps the loss framing."""
    state = live_state()
    state["battlefield"].append(
        _card(
            351,
            "Perforating Artist",
            "Creature — Devil",
            "{1}{B}{R}",
            "Raid — ... loses 3 life unless ...",
            seat=2,
        )
    )
    choice = PendingDecision(
        (351, 455),
        "SelectN",
        tuple(
            DecisionOption(f"sel:{oid}", text, meta={"choice": "prompt", "promptId": pid, "sourceId": 351})
            for oid, pid, text in ((9, 1217, "Discard a card"), (11, 1218, "Lose 3 life"))
        ),
        source_label="Perforating Artist",
    )
    for note in (select_n_note(choice, state), select_n_discard_note(choice, state)):
        assert note.startswith("CHOICE FROM Perforating Artist (OPPONENT's card")
        assert "LOSS" not in note and "SELECTION:" not in note and "DISCARD" not in note
    assert hand_loss(choice, state) is None and hand_loss_choice(choice, state) == ([], "")
    assert select_n_discard_note(_decision(state), state).startswith("THIS IS A LOSS, NOT A CAST")


def test_a_single_prompt_id_reads_as_plain_text_not_an_icu_template(monkeypatch):
    from types import SimpleNamespace

    from arenamcp import card_db

    raw = SimpleNamespace(get_prompt_text=lambda pid: PROMPTS.get(pid))
    monkeypatch.setattr(card_db, "get_card_database", lambda: SimpleNamespace(get_raw_mtgadb=lambda: raw))
    assert decisions.prompt_text(14) == "Discard a card."
    assert decisions.prompt_text(1117) == "Exile a card."
    assert decisions.prompt_text(1) == ""  # "#Deprecated ..." says nothing
    assert decisions.prompt_text(4242) == ""


def test_their_hand_is_not_our_loss():
    # A Duress-style choice over the opponent's hand: the options are not our cards.
    state = live_state()
    decision = _decision(state, ids=[901, 902])
    assert hand_loss(decision, state) is None
    assert hand_loss_choice(decision, state) == ([], "")


# --- the pick -------------------------------------------------------------------


def test_the_second_hazard_goes_not_shock():
    state = live_state()
    picked, why = hand_loss_choice(_decision(state), state)
    assert picked == ["sel:283"], why
    assert why.startswith("Unscrupulous Agent takes a card from our hand.")
    assert "Tectonic Hazard, a second copy" in why


def test_the_planner_exiles_without_asking_the_model(monkeypatch):
    planner = planner_with("{}")

    def no_model(*_a, **_k):
        raise AssertionError("a hand loss is a count, not a model call")

    monkeypatch.setattr(planner, "_llm_decision_options", no_model)
    state = live_state()
    assert planner.plan_decision_options(_decision(state), state) == ["sel:283"]
    assert planner._last_decision_trace["policy"] == "hand_loss"


def test_an_extra_land_goes_first():
    hand = [_card(10, *MOUNTAIN), _card(11, *SHOCK), _card(12, *STRIKE)]
    state = live_state(hand=hand, lands_in_play=5)
    decision = _decision(state, ids=[10, 11, 12], prompt_id=1024)
    picked, why = hand_loss_choice(decision, state)
    assert (
        picked == ["sel:10"] and "a land we can spare" in why and why.split(". ")[1].startswith("Discarding")
    )


def test_a_land_we_still_need_stays():
    hand = [_card(10, *MOUNTAIN), _card(11, *SHOCK), _card(12, *STRIKE), _card(13, *HAZARD)]
    state = live_state(hand=hand, lands_in_play=1)
    picked, _why = hand_loss_choice(_decision(state, ids=[10, 11, 12, 13]), state)
    assert picked == ["sel:13"]  # burn that only reaches each opponent and their small creatures


def test_two_cards_lost_at_once():
    state = live_state()
    picked, _why = hand_loss_choice(_decision(state, min_sel=2, max_sel=2), state)
    assert sorted(picked) == ["sel:280", "sel:283"]


def test_connive_is_left_to_the_model_with_the_discard_framing():
    state = live_state()
    decision = _decision(state, prompt_id=153)
    loss = hand_loss(decision, state)
    assert loss is not None and loss.kind == "discard"
    assert loss.unusual.startswith("the effect reads which card goes")
    assert hand_loss_choice(decision, state) == ([], "")
    assert select_n_note(decision, state).startswith("THIS IS A DISCARD")


def test_a_graveyard_deck_decides_its_own_discards(monkeypatch):
    state = live_state()
    state["hand"] = state["hand"] + [
        _card(400, "Bloodghast", "Creature — Vampire Spirit", "{B}{B}", "Landfall — Whenever a land you control "
              "enters, you may return this card from your graveyard to the battlefield.")
    ]  # fmt: skip
    decision = _decision(state, prompt_id=1024)
    loss = hand_loss(decision, state)
    assert loss is not None and loss.kind == "discard" and "graveyard" in loss.unusual
    assert hand_loss_choice(decision, state) == ([], "")


def test_a_variable_count_is_left_to_the_model():
    state = live_state()
    assert hand_loss_choice(_decision(state, min_sel=0, max_sel=1), state) == ([], "")


def test_the_model_sees_the_loss_when_it_is_asked(monkeypatch):
    state = live_state()
    decision = _decision(state, prompt_id=153)
    planner = planner_with("{}")
    seen = []

    def model(decision_arg, state_arg):
        seen.append(select_n_note(decision_arg, state_arg))
        return ["sel:285"]

    monkeypatch.setattr(planner, "_llm_decision_options", model)
    assert planner.plan_decision_options(decision, state) == ["sel:285"]
    assert seen and seen[0].startswith("THIS IS A DISCARD")


# --- the log path ---------------------------------------------------------------

LOG_SELECT_N_REQ = {
    "type": "GREMessageType_SelectNReq",
    "systemSeatIds": [1],
    "msgId": 102,
    "gameStateId": 68,
    "prompt": {
        "promptId": 1117,
        "parameters": [
            {"parameterName": "CardId", "type": "ParameterType_Number", "numberValue": 353},
            {"parameterName": "CardId", "type": "ParameterType_Number", "numberValue": 1},
        ],
    },
    "selectNReq": {
        "minSel": 1,
        "maxSel": 1,
        "context": "SelectionContext_Resolution",
        "optionContext": "OptionContext_Resolution",
        "listType": "SelectionListType_Dynamic",
        "ids": LIVE_IDS,
        "prompt": {
            "parameters": [{"parameterName": "Parameter", "type": "ParameterType_PromptId", "promptId": 1}]
        },
        "idType": "IdType_InstanceId",
        "sourceId": 353,
        "validationType": "SelectionValidationType_NonRepeatable",
        "minWeight": -2147483648,
        "maxWeight": 2147483647,
    },
    "allowCancel": "AllowCancel_No",
}
GRP = {87323: HAZARD, 66263: STRIKE, 65385: MOUNTAIN, 75525: SHOCK, 95628: EFFIGY}
AGENT_GRP = 89024


def _log_state(monkeypatch):
    from arenamcp import server

    def info(grp_id):
        if grp_id == AGENT_GRP:
            return {
                "name": "Unscrupulous Agent",
                "type_line": "Creature — Elf Detective",
                "oracle_text": AGENT_TEXT,
            }
        name, type_line, cost, text = GRP.get(grp_id, ("", "", "", ""))
        return {"name": name, "type_line": type_line, "mana_cost": cost, "oracle_text": text}

    monkeypatch.setattr(server, "enrich_with_oracle_text", info)
    monkeypatch.setattr(server, "get_card_info", info)
    monkeypatch.setattr(GameState, "_resolve_card_name", lambda self, grp_id: info(grp_id)["name"])
    state = GameState()
    state.local_seat_id = 1
    state.zones[31] = Zone(
        zone_id=31, zone_type=ZoneType.HAND, owner_seat_id=1, object_instance_ids=list(LIVE_IDS)
    )
    state.zones[28] = Zone(zone_id=28, zone_type=ZoneType.BATTLEFIELD, object_instance_ids=[353])
    for iid, grp_id in zip(LIVE_IDS, (87323, 66263, 87323, 65385, 75525, 95628), strict=True):
        state.game_objects[iid] = GameObject(instance_id=iid, grp_id=grp_id, zone_id=31, owner_seat_id=1)
    state.game_objects[353] = GameObject(
        instance_id=353, grp_id=AGENT_GRP, zone_id=28, owner_seat_id=2, controller_seat_id=2
    )
    return state


def test_the_log_keeps_the_prompt_the_source_and_calls_it_an_exile(monkeypatch):
    state = _log_state(monkeypatch)
    _handle_decision_message(state, "GREMessageType_SelectNReq", LOG_SELECT_N_REQ)
    context = state.decision_context
    assert state.pending_decision == "Exile"
    assert context["type"] == "exile" and context["hand_loss"] == "exile"
    assert (context["prompt_id"], context["prompt_text"]) == (1117, "Exile a card.")
    assert (context["prompt_source_id"], context["source_card"]) == (353, "Unscrupulous Agent")
    assert context["source_oracle_text"] == AGENT_TEXT
    assert context["request_id"] == [68, 102]
    assert context["option_cards"][4] == "Shock"
    # The Agent is on the battlefield, not the stack: "source_id" would arm the
    # stack-based auto-clear and drop the decision after its 5 s hold.
    assert "source_id" not in context
    state.decision_timestamp -= 6
    assert _auto_clear_stale_decision(state) is False and state.pending_decision == "Exile"


def test_the_log_context_alone_frames_and_decides_the_loss(monkeypatch):
    """No bridge prompt (a plugin that sends none): the log's prompt text for the same request is used."""
    monkeypatch.setattr(arena_prompts, "_lookup_template", lambda identifier: "")
    log = _log_state(monkeypatch)
    monkeypatch.setattr(arena_prompts, "_lookup_template", lambda identifier: PROMPTS.get(identifier, ""))
    _handle_decision_message(log, "GREMessageType_SelectNReq", LOG_SELECT_N_REQ)
    monkeypatch.setattr(arena_prompts, "_lookup_template", lambda identifier: "")
    state = live_state()
    state["decision_context"] = dict(log.decision_context)
    state["_bridge_request_payload"] = {}
    decision = _decision(state)
    assert decision.prompt_text == ""
    loss = hand_loss(decision, state)
    assert loss is not None and loss.kind == "exile" and loss.source == "Unscrupulous Agent"
    assert hand_loss_choice(decision, state)[0] == ["sel:283"]
