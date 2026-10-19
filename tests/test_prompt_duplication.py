"""Each card fact reaches a prompt once (2026-10-18 prompt-size audit).

Recorded boards (bug reports of 2026-10-10..18) sent decision prompts that
quoted the same rules several times: Arena's card data repeats abilities in
markup variants, so Yuriko's and Divining Duelist's triggers appeared three
times per zone and the cached Brawl playbooks quoted every "+1/+1 counter"
rule three times. The facts stay; only the repeats go.
"""

import json
from copy import deepcopy

from tests.deck_fakes import deck_case

from arenamcp.coach import CoachEngine
from arenamcp.deck_strategy import DeckPlaybook, commander_ids, deck_identity
from arenamcp.match_context import oracle_lines, prepare_match_context

# Divining Duelist as Arena's card data stores it (set primer FRA.json): the
# trigger in three renderings, the second with <nobr>/<indent> markup.
DUELIST_TEXT = (
    "Flash\nWhen this creature enters, choose one — \n•Tap target creature. \n•Untap target creature. \n•Draw a "
    "card, then discard a card.\nWhen this creature enters, choose one<nobr> —</nobr> \n•<indent=4%>Tap target "
    "creature. </indent>\n•<indent=4%>Untap target creature. </indent>\n•<indent=4%>Draw a card, then discard a "
    "card.</indent>\nWhen this creature enters, choose one — \n•Tap target creature. \n•Untap target creature. "
    "\n•Draw a card, then discard a card."
)
DUELIST_RULES = [
    "Flash",
    "When this creature enters, choose one —",
    "•Tap target creature.",
    "•Untap target creature.",
    "•Draw a card, then discard a card.",
]


def _duelist(instance_id, owner, **extra):
    return {
        "instance_id": instance_id,
        "grp_id": 106252,
        "name": "Divining Duelist",
        "type_line": "Creature — Merfolk Wizard",
        "mana_cost": "{1}{U}",
        "power": 1,
        "toughness": 3,
        "oracle_text": DUELIST_TEXT,
        "owner_seat_id": owner,
        "controller_seat_id": owner,
        "turn_entered_battlefield": 2,
        **extra,
    }


def _state(**zones):
    return {
        "match_id": "duplication-test",
        "local_seat_id": 1,
        "turn": {"turn_number": 6, "active_player": 1, "priority_player": 1, "phase": "Phase_Main1"},
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20},
            {"seat_id": 2, "is_local": False, "life_total": 20},
        ],
        "battlefield": [],
        "hand": [],
        "graveyard": [],
        "exile": [],
        "stack": [],
        "legal_actions": ["Pass"],
        **zones,
    }


def test_arena_renderings_collapse_while_distinct_abilities_keep_their_modes():
    assert oracle_lines(DUELIST_TEXT) == DUELIST_RULES
    two_abilities = (
        "When this creature enters, choose one —\n•Draw a card.\n•Scry 2.\n"
        "When this creature dies, choose one —\n•Draw a card.\n•You gain 3 life."
    )
    assert oracle_lines(two_abilities) == two_abilities.split("\n")


def test_board_and_graveyard_quote_a_repeated_trigger_once_each():
    # No deck list: the zones are the only place these rules are quoted.
    state = _state(battlefield=[_duelist(30, 1)], graveyard=[_duelist(41, 2)])
    context = CoachEngine.__new__(CoachEngine)._format_game_context(state, for_planner=True)
    # Before: three copies in each zone.
    for rule in DUELIST_RULES[1:]:
        assert context.count(rule) == 2, rule
    assert "<" not in context.replace("<-", "")


def test_deck_reference_quotes_rules_without_markup_or_repeats():
    state = _state(hand=[_duelist(30, 1)], deck_cards=[106252] * 4)
    card = {key: _duelist(0, 1)[key] for key in ("name", "type_line", "mana_cost", "power", "toughness")}
    prepared = prepare_match_context(state, card_lookup=lambda gid: {**card, "oracle_text": DUELIST_TEXT})
    quoted = [line for line in prepared["deck_reference"].splitlines() if "| Flash" in line]
    assert len(quoted) == 1
    assert quoted[0].endswith(" | " + " ".join(DUELIST_RULES))


def test_playbook_quotes_a_card_cited_by_two_mechanisms_once():
    state, catalog, response = deck_case("preserve")
    counter_rule = "Whenever you cast a creature spell, put a +1/+1 counter on this creature."
    catalog = deepcopy(catalog)
    catalog[1001]["oracle_text"] = (
        f"{counter_rule}\nWhenever you cast a creature spell, put a <nobr>+1/+1</nobr> counter on this creature."
    )
    second = {**response["mechanisms"][0], "id": "growth", "evidence": [{"card": 1001, "rule": 2}]}
    response["mechanisms"].append(second)
    response["decision_rules"][0]["mechanisms"].append("growth")
    book = DeckPlaybook.parse(json.dumps(response), catalog, commander_ids(state), deck_identity(state))
    rendered = book.render()
    # Before: quoted twice under the first mechanism and once more under the second.
    assert rendered.count(counter_rule) == 1
    assert "<nobr>" not in rendered
    assert "Fixture Growing Captain [grp:1001], rule 2: quoted above" in rendered


# --- the zones point to the deck reference's rules -----------------------------------

CATALOG = {
    100: {
        "name": "Fixture Mana Elf",
        "type_line": "Creature — Elf Druid",
        "mana_cost": "{G}",
        "cmc": 1,
        "power": 1,
        "toughness": 1,
        "oracle_text": "{T}: Add {G}. Spend this mana only to cast creature spells.",
    },
    101: {
        "name": "Fixture Growth",
        "type_line": "Sorcery",
        "mana_cost": "{1}{G}",
        "cmc": 2,
        "oracle_text": "Put a +1/+1 counter on each creature you control. Draw a card.",
    },
    102: {
        "name": "Forest",
        "type_line": "Basic Land — Forest",
        "mana_cost": "",
        "cmc": 0,
        "oracle_text": "({T}: Add {G}.)",
    },
    200: {
        "name": "Fixture Matriarch",
        "type_line": "Legendary Creature — Elf Noble",
        "mana_cost": "{2}{G}",
        "cmc": 3,
        "power": 2,
        "toughness": 3,
        "oracle_text": "Whenever another creature you control enters, it gains haste until end of turn.",
    },
}
ELF_RULE = CATALOG[100]["oracle_text"]
GROWTH_RULE = CATALOG[101]["oracle_text"]
MATRIARCH_RULE = CATALOG[200]["oracle_text"]


def _card(instance_id, grp_id, **extra):
    return {
        "instance_id": instance_id,
        "grp_id": grp_id,
        "owner_seat_id": 1,
        "controller_seat_id": 1,
        **CATALOG[grp_id],
        **extra,
    }


def _deck_state():
    return _state(
        match_id="zones-point-to-reference",
        deck_cards=[100] * 2 + [101] * 2 + [102] * 16,
        commander_grp_ids=[200],
        hand=[_card(11, 100), _card(12, 100), _card(13, 101)],
        battlefield=[_card(21, 102, turn_entered_battlefield=1)],
        command=[_card(31, 200)],
        zones={"library_count": 16, "opponent_hand_count": 5},
    )


class _Capture:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def complete(self, system, user, *args, **kwargs):
        self.calls.append(user)
        return self.reply


def _typed_prompt(state):
    from arenamcp.action_planner import ActionPlanner
    from arenamcp.decisions import build_pending_decision

    decision = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "ActionsAvailable",
            "can_pass": True,
            "actions": [
                {"actionType": "ActionType_Cast", "grpId": 101, "instanceId": 13, "hasAutoTap": True},
                {"actionType": "ActionType_Pass"},
            ],
        },
        resolve_name=lambda gid: CATALOG.get(gid, {}).get("name", ""),
        resolve_zone=lambda iid: "hand",
        activation_unaffordable=lambda action: False,
    )
    backend = _Capture('{"option_ids":["pass"]}')
    ActionPlanner(backend)._llm_decision_options(decision, deepcopy(state))
    return backend.calls[-1]


def _coach_prompt(state):
    from types import SimpleNamespace

    backend = _Capture("Pass.")
    coach = CoachEngine(backend=backend)
    coach._ensure_game_plan_mgr = lambda: None
    coach._rules_db = SimpleNamespace(get_rules_for_situation=lambda *args, **kwargs: [])
    coach.get_advice(deepcopy(state), trigger="new_turn")
    return backend.calls[-1]


def _plan_prompt(state):
    from arenamcp.action_planner import ActionPlanner

    return ActionPlanner(_Capture("{}"))._build_action_prompt(
        deepcopy(state), "new_turn", legal_actions=["Cast Fixture Growth [OK]", "Pass"]
    )


def test_hand_and_command_zone_rules_are_quoted_once_with_the_deck_reference(monkeypatch):
    from arenamcp.match_context import reset_seen_rules

    monkeypatch.setattr("arenamcp.match_context._local_card", lambda gid, epoch: CATALOG[gid])
    for build in (_typed_prompt, _coach_prompt, _plan_prompt):
        reset_seen_rules()
        prompt = build(_deck_state())
        # Before: the deck reference, then the hand (once per copy) and the command zone.
        assert prompt.count(ELF_RULE) == 1, build.__name__
        assert prompt.count(GROWTH_RULE) == 1, build.__name__
        assert prompt.count(MATRIARCH_RULE) == 1, build.__name__
        assert ELF_RULE in prompt.split("\n\n", 1)[0]  # the deck reference, the prompt's first block
        hand = prompt.split("\nHAND:", 1)[1]
        assert "Fixture Mana Elf #1" in hand and "Fixture Mana Elf #2" in hand and "Fixture Growth" in hand


def test_a_typed_prompt_lists_its_options_once(monkeypatch):
    from arenamcp.match_context import reset_seen_rules

    monkeypatch.setattr("arenamcp.match_context._local_card", lambda gid, epoch: CATALOG[gid])
    reset_seen_rules()
    prompt = _typed_prompt(_deck_state())
    assert "- idx:0: Cast Fixture Growth" in prompt and "- pass: Pass" in prompt
    # Before: "Legal: Cast Fixture Growth [OK], Pass", "GRE_Request: ActionsAvailable" and
    # "GRE_RequestClass: ActionsAvailableRequest" restated them in the game view.
    for restated in ("Legal:", "GRE_Request", "legal actions available"):
        assert restated not in prompt, restated
    assert "\nHAND:" in prompt and "Life: You=20 Opp=20" in prompt
    reset_seen_rules()
    assert "Legal:" in _plan_prompt(_deck_state())  # the plan prompt's numbered menu


def test_without_a_deck_list_the_hand_quotes_each_card_once():
    state = _deck_state()
    state["deck_cards"] = []
    context = CoachEngine.__new__(CoachEngine)._format_game_context(state, for_planner=True)
    assert context.count(ELF_RULE) == 1  # before: once per copy
    assert context.count(GROWTH_RULE) == 1
    assert context.count(MATRIARCH_RULE) == 1


def test_repeated_board_copies_quote_their_rules_once():
    token = {
        "name": "Cadet",
        "type_line": "Token Creature — Human Soldier",
        "object_kind": "TOKEN",
        "power": 1,
        "toughness": 1,
        "oracle_text": "When this token dies, you gain 1 life.",
        "owner_seat_id": 2,
        "controller_seat_id": 2,
        "turn_entered_battlefield": 3,
    }
    state = _state(battlefield=[{**token, "instance_id": iid, "is_tapped": iid == 52} for iid in (51, 52, 53)])
    context = CoachEngine.__new__(CoachEngine)._format_game_context(state, for_planner=True)
    assert context.count("When this token dies, you gain 1 life.") == 1  # before: 3
    for number in (1, 2, 3):
        assert f"*Cadet #{number} 1/1" in context
    assert "*Cadet #2 1/1 [T]" in context


# --- the library inventory is listed when the library is in play -------------------

FETCH = "{T}, Pay 1 life, Sacrifice this land: Search your library for a basic land card, put it onto the battlefield."


def _library_state(**changes):
    state = _deck_state()
    for key, value in changes.items():
        state[key] = value
    return state


def _context(state, **prepare):
    prepared = prepare_match_context(state, card_lookup=CATALOG.get, **prepare)
    return CoachEngine.__new__(CoachEngine)._format_game_context(prepared, for_planner=True)


def test_tactical_prompts_point_to_the_deck_reference_without_a_library_effect():
    state = _library_state()
    tactical, strategic = _context(state), _context(state, library=True)
    assert "MY LIBRARY (" not in tactical
    assert "MY LIBRARY: the DECK REFERENCE minus the cards seen this game" in tactical
    assert "15x Forest [grp:102]" in strategic  # 16 in the deck, 1 on the battlefield
    assert len(tactical) < len(strategic)
    assert "Your library: 16 card(s)" in tactical  # the count stays


def test_library_effects_in_play_list_the_inventory():
    land_search = "Search your library for a basic land card, reveal it, put it into your hand, then shuffle."
    listed = {
        "a search in hand": _library_state(hand=[_card(13, 101, oracle_text=land_search)]),
        "typecycling in hand": _library_state(hand=[_card(13, 101, oracle_text="Basic landcycling {2}")]),
        "a fetch land's ability": _library_state(
            battlefield=[_card(21, 102, name="Fixture Fetch", oracle_text=FETCH, turn_entered_battlefield=1)]
        ),
        "a recurring scry": _library_state(
            battlefield=[_card(21, 101, oracle_text="Whenever you cast an instant spell, scry 1.")]
        ),
        "our spell on the stack": _library_state(stack=[_card(40, 101, oracle_text="Surveil 2. Draw a card.")]),
        "a search decision": _library_state(decision_context={"type": "search"}),
        "a scry decision": _library_state(decision_context={"type": "scry"}),
    }
    for why, state in listed.items():
        assert "MY LIBRARY (" in _context(state), why
    not_listed = {
        "a resolved enters trigger": "When this land enters, scry 1.",
        # Yuriko, Hope from the Shadows (FRA): the surveil is a mode of its enters trigger.
        "a resolved modal enters trigger": "Flash\nWhen this creature enters, choose one —\n"
        "•Target creature gets -X/-0 until end of turn.\n•Surveil 2.",
        # Diviner of Victory (FRA): a payoff, not a library effect.
        "a scry payoff": "Whenever you scry or surveil, this creature gets +1/+1 until end of turn.",
    }
    for why, text in not_listed.items():
        entered = _library_state(battlefield=[_card(21, 100, oracle_text=text)])
        assert "MY LIBRARY (" not in _context(entered), why
    theirs = _library_state(stack=[_card(40, 101, oracle_text="Scry 2.", controller_seat_id=2, owner_seat_id=2)])
    assert "MY LIBRARY (" not in _context(theirs)


def test_the_game_plan_and_questions_keep_the_inventory(monkeypatch):
    from arenamcp.game_plan import GamePlanManager

    monkeypatch.setattr("arenamcp.match_context._local_card", lambda gid, epoch: CATALOG[gid])
    backend = _Capture('{"win_conditions":["Engine"],"path":"Develop","threat":"x","develop_next":"y"}')
    GamePlanManager(backend).maybe_reform(_library_state(), force=True)
    assert "MY LIBRARY (16 cards left" in backend.calls[-1]
    from types import SimpleNamespace

    coach_backend = _Capture("Two Forests and the second Growth remain.")
    coach = CoachEngine(backend=coach_backend)
    coach._ensure_game_plan_mgr = lambda: None
    coach._rules_db = SimpleNamespace(get_rules_for_situation=lambda *args, **kwargs: [])
    coach.get_advice(_library_state(), question="What can I still draw?")
    assert "MY LIBRARY (16 cards left" in coach_backend.calls[-1]


# --- the playbook's rules for the window, not all of them -------------------------


def _scoped_playbook():
    state, catalog, response = deck_case("spells")
    base = response["decision_rules"][0]
    response["decision_rules"] = [
        {**base, "id": f"rule_{kinds[0]}", "decisions": kinds, "prefer": f"Prefer for {kinds[0]}."}
        for kinds in (["development"], ["combat"], ["targeting"], ["tutor"], ["all"])
    ]
    return state, DeckPlaybook.parse(json.dumps(response), catalog, commander_ids(state), deck_identity(state))


def _rules_shown(text):
    return sorted(line.split()[1] for line in text.splitlines() if line.startswith("RULE "))


def test_playbook_rules_follow_the_window_including_log_decision_types():
    state, book = _scoped_playbook()
    # An upkeep priority window, as Player.log names it ("actions_available"):
    # before, no hint matched the underscore form and all five rules were sent.
    upkeep = {**state, "turn": {"phase": "Phase_Beginning"}, "decision_context": {"type": "actions_available"}}
    assert _rules_shown(book.decision_context(upkeep)) == ["rule_all", "rule_development"]
    # A typed decision names its request.
    plain = {**state, "turn": {"phase": "Phase_Ending"}, "decision_context": None}
    assert _rules_shown(book.decision_context(plain, request_type="SelectTargets")) == ["rule_all", "rule_targeting"]
    assert _rules_shown(book.decision_context(plain, request_type="Search")) == ["rule_all", "rule_tutor"]
    damage = {**plain, "decision_context": {"type": "assign_damage"}}
    assert "rule_combat" in _rules_shown(book.decision_context(damage))
    # A window no hint classifies still gets every rule.
    unknown = {**plain, "decision_context": {"type": "numeric_input"}}
    assert len(_rules_shown(book.decision_context(unknown))) == 5
