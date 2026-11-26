"""System-prompt rules ride only when their subject is in the game (2026-10-18 prompt audit).

A typed decision's system prompt carried the crew, animation, Equipment,
commander and library-search special cases on every call: ~800 of its
~1,950 tokens in a Limited game with none of those cards.
"""

from arenamcp.action_planner import AUTOPILOT_SYSTEM_PROMPT, ActionPlanner, autopilot_system_prompt
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.match_context import STRATEGIC_POLICY, strategic_policy
from arenamcp.situational_rules import SUBJECTS, deck_subjects, dynamic_subjects, subjects_in_play

LOCAL, OPP = 1, 2
CREW = "Crew activates a Vehicle"
ANIMATION = "Animating an artifact or land"
EQUIPMENT = "For haste-granting equipment"
COMMANDER = "A commander in the command zone"
LAND_SEARCH = "Searching lands into hand"
SEARCH_CHOICE = "For library searches"
TUTORS = "For tutors"
FETCH = "A fetch land that sacrifices itself"
CORE = (
    "payable individually now",
    "Payable does not mean free.",
    "Never buff an opposing creature merely to draw.",
    "does not trigger 'when you cast'",
    "SEQUENCING",
    "Never discard a castable card",
    "A plan is conditional",
    "Do not tutor a habitual favorite or spend removal",
    "Neither commanders nor tokens are automatically expendable.",
)


def _card(name, type_line, oracle="", owner=LOCAL, **extra):
    return {
        "name": name,
        "type_line": type_line,
        "oracle_text": oracle,
        "owner_seat_id": owner,
        "controller_seat_id": owner,
        **extra,
    }


BEAR = _card("Grizzly Bears", "Creature — Bear")
FOREST = _card("Forest", "Basic Land — Forest", "({T}: Add {G}.)")


def _state(**zones):
    return {
        "local_seat_id": LOCAL,
        "players": [{"seat_id": LOCAL, "is_local": True}, {"seat_id": OPP, "is_local": False}],
        "turn": {"turn_number": 5, "active_player": LOCAL, "phase": "Phase_Main1"},
        "hand": [BEAR],
        "battlefield": [FOREST],
        **zones,
    }


def _decision(request_type="ActionsAvailable", labels=("Pass",), min_weight=None):
    return PendingDecision(
        (1, 1),
        request_type,
        tuple(DecisionOption(f"idx:{i}", label) for i, label in enumerate(labels)),
        min_weight=min_weight,
    )


def test_with_every_subject_in_play_the_prompts_are_the_full_rules():
    # E2 prefix order: the shared strategic policy leads both system prompts;
    # the class-specific rules follow (see tests/test_prompt_cache_order_e2.py).
    assert ActionPlanner._decision_system_prompt(SUBJECTS) == (
        STRATEGIC_POLICY + "\n" + ActionPlanner._DECISION_SYSTEM_PROMPT
    )
    assert autopilot_system_prompt(SUBJECTS) == STRATEGIC_POLICY + "\n" + AUTOPILOT_SYSTEM_PROMPT
    for rule in (CREW, ANIMATION, EQUIPMENT, COMMANDER, LAND_SEARCH, SEARCH_CHOICE, TUTORS, FETCH):
        assert rule in ActionPlanner._DECISION_SYSTEM_PROMPT + STRATEGIC_POLICY, rule


def test_a_board_without_the_subjects_gets_the_core_rules_only():
    assert subjects_in_play(_state(), _decision()) == frozenset()
    rules = ActionPlanner._decision_system_prompt(frozenset())
    for absent in (CREW, ANIMATION, EQUIPMENT, COMMANDER, LAND_SEARCH, SEARCH_CHOICE, TUTORS, FETCH):
        assert absent not in rules, absent
    for kept in CORE:
        assert kept in rules, kept
    full = ActionPlanner._decision_system_prompt(SUBJECTS)
    assert len(rules) < 0.65 * len(full)  # 7184 -> 4433 characters
    plan = autopilot_system_prompt(frozenset())
    for absent in ("- ANIMATION:", "- EQUIPMENT:", "- TUTORS & SEARCH LIBRARY:", TUTORS):
        assert absent not in plan, absent
    for kept in (
        "- ZERO-POWER ATTACKERS:",
        "- BLOCKERS:",
        "- COMBAT SOLVER:",
        "SCHEMA:",
        "A plan is conditional",
    ):
        assert kept in plan, kept
    assert strategic_policy(frozenset()).startswith("Follow the deck's win conditions")


def test_each_subject_is_recognised_wherever_our_cards_show_it():
    vehicle = _card("Smuggler's Copter", "Artifact — Vehicle", "Flying\nCrew 1")
    cases = {
        "vehicle in hand": (_state(hand=[vehicle]), None, {"vehicle", "animation"}),
        "crew weight selection": (_state(), _decision("SelectN", min_weight=3), {"vehicle", "animation"}),
        "manland": (
            _state(battlefield=[_card("Mishra's Land", "Land", "{1}: This land becomes a 2/2 creature.")]),
            None,
            {"animation"},
        ),
        "equipment in our graveyard": (
            _state(graveyard=[_card("Bonesplitter", "Artifact — Equipment", "Equip {1}")]),
            None,
            {"equipment"},
        ),
        "equip option": (_state(), _decision(labels=("Activate: Sword [Equip {2}]",)), {"equipment"}),
        "our command zone": (
            _state(command=[_card("Fixture Captain", "Legendary Creature")]),
            None,
            {"commander"},
        ),
        "designated commander": (_state(commander_grp_ids=[81845]), None, {"commander"}),
        "tutor in hand": (
            _state(hand=[_card("Tutor", "Sorcery", "Search your library for a card.")]),
            None,
            {"library"},
        ),
        "search decision": (_state(), _decision("Search"), {"library"}),
    }
    for why, (state, decision, expected) in cases.items():
        assert subjects_in_play(state, decision) == frozenset(expected), why
    theirs = _state(battlefield=[FOREST, {**vehicle, "owner_seat_id": OPP, "controller_seat_id": OPP}])
    assert subjects_in_play(theirs) == frozenset()


def test_the_typed_and_plan_calls_send_the_rules_for_their_board():
    class Capture:
        def __init__(self, reply):
            self.reply, self.calls = reply, []

        def complete(self, system, user, *args, **kwargs):
            self.calls.append((system, user))
            return self.reply

    backend = Capture('{"option_ids": ["pass"]}')
    decision = PendingDecision((1, 1), "ActionsAvailable", (DecisionOption("pass", "Pass"),), can_pass=True)
    ActionPlanner(backend)._llm_decision_options(decision, _state())
    sys_1, user_1 = backend.calls[-1]
    combined_1 = sys_1 + "\n" + user_1
    assert CREW not in combined_1 and "payable individually now" in combined_1

    ActionPlanner(backend)._llm_decision_options(
        decision, _state(hand=[BEAR, _card("Copter", "Artifact — Vehicle")])
    )
    sys_2, user_2 = backend.calls[-1]
    combined_2 = sys_2 + "\n" + user_2
    assert CREW in combined_2 and EQUIPMENT not in combined_2
    assert sys_2 == sys_1

    plan_backend = Capture('{"actions": [{"pick": 1}]}')
    planner = ActionPlanner(plan_backend, timeout=10.0, land_drop_first=False)
    planner.plan_actions(_state(), "new_turn", ["Cast Grizzly Bears [OK]", "Pass"])
    plan_sys_1, plan_user_1 = plan_backend.calls[-1]
    plan_combined_1 = plan_sys_1 + "\n" + plan_user_1
    assert "- EQUIPMENT:" not in plan_combined_1 and "- BLOCKERS:" in plan_combined_1
    assert plan_sys_1 == sys_1

    planner.plan_actions(
        _state(battlefield=[FOREST, _card("Sword", "Artifact — Equipment", "Equip {2}")]),
        "new_turn",
        ["Cast Grizzly Bears [OK]", "Activate Ability: Sword [OK]", "Pass"],
    )
    plan_sys_2, plan_user_2 = plan_backend.calls[-1]
    plan_combined_2 = plan_sys_2 + "\n" + plan_user_2
    assert "- EQUIPMENT:" in plan_combined_2
    assert plan_sys_2 == plan_sys_1


def test_deck_subjects_reads_only_deck_and_format_metadata(monkeypatch):
    # Empty state with no deck or brawl/commander metadata -> frozenset()
    assert deck_subjects(None) == frozenset()
    assert deck_subjects(_state()) == frozenset()

    # Live zones and decision options are never inspected by deck_subjects
    vehicle = _card("Smuggler's Copter", "Artifact — Vehicle", "Flying\nCrew 1")
    equipment = _card("Short Sword", "Artifact — Equipment", "Equip {1}")
    tutor = _card("Tutor", "Sorcery", "Search your library for a card.")
    strix = _card("Hallway Heckler", "Creature — Sorcerer", "This creature enters prepared.")
    manland = _card("Mishra's Land", "Land", "{1}: This land becomes a 2/2 creature.")
    live_only = _state(
        hand=[vehicle, tutor],
        battlefield=[equipment, strix, manland],
        stack=[tutor],
        graveyard=[equipment],
        exile=[vehicle],
    )
    assert deck_subjects(live_only) == frozenset()

    # deck_catalog detects all deck-level mechanics
    catalog = {101: BEAR, 102: vehicle, 103: equipment, 104: tutor, 105: strix}
    assert deck_subjects({"deck_catalog": catalog}) == frozenset(
        {"vehicle", "animation", "equipment", "library", "set_mechanics"}
    )

    # Lazily resolves deck_cards via _local_card only when deck_catalog is absent
    monkeypatch.setattr("arenamcp.match_context._local_card", lambda gid, _epoch: catalog[gid])
    assert deck_subjects({"deck_cards": [101, 102]}) == frozenset({"vehicle", "animation"})
    assert deck_subjects({"deck_cards": [101], "deck_catalog": {999: equipment}}) == frozenset({"equipment"})

    # Commander metadata and format_name / event_id detect "commander"
    assert deck_subjects({"commander_grp_ids": [81845]}) == frozenset({"commander"})
    assert deck_subjects({"format_name": "Historic Brawl"}) == frozenset({"commander"})
    assert deck_subjects({"event_id": "Play_Commander_100"}) == frozenset({"commander"})

    # Broken populated deck data never drops rules -> returns SUBJECTS
    assert deck_subjects({"deck_catalog": 7}) == SUBJECTS
    assert deck_subjects({"deck_cards": 7}) == SUBJECTS

    # dynamic_subjects returns only subjects not already covered by match_subjects
    match_sub = frozenset({"vehicle", "animation"})
    assert dynamic_subjects(
        _state(hand=[vehicle, equipment]),
        _decision("Search"),
        match_subjects=match_sub,
    ) == frozenset({"equipment", "library"})

    # Kill switch restores all SUBJECTS
    monkeypatch.setenv("ARENAMCP_SITUATIONAL_RULES", "0")
    assert deck_subjects(_state()) == SUBJECTS


def test_the_coach_prompt_drops_the_strixhaven_primer_and_equipment_rule_without_their_cards():
    from arenamcp.coach_prompts import DEFAULT_SYSTEM_PROMPT, situational_coach_prompt

    assert situational_coach_prompt(DEFAULT_SYSTEM_PROMPT, SUBJECTS) == DEFAULT_SYSTEM_PROMPT
    plain = situational_coach_prompt(DEFAULT_SYSTEM_PROMPT, frozenset())
    for absent in (
        "SECRETS OF STRIXHAVEN",
        "- PREPARE",
        "- PARADIGM",
        "- CONVERGE",
        "- EQUIPMENT & TARGETING",
    ):
        assert absent not in plain, absent
    for kept in ("- HEURISTIC HINTS:", "- Bounce/removal spells", "- ORACLE TEXT:", "CRITICAL MANA RULES:"):
        assert kept in plain, kept
    assert "\n\n\n" not in plain
    assert len(plain) < len(DEFAULT_SYSTEM_PROMPT) - 2000
    # An opponent's prepared creature is enough for the primer.
    prepared = _card(
        "Hallway Heckler", "Creature — Elemental Sorcerer", "This creature enters prepared.", owner=OPP
    )
    assert subjects_in_play(_state(battlefield=[FOREST, prepared])) == {"set_mechanics"}


def test_coach_advice_sends_the_rules_for_its_board():
    from types import SimpleNamespace

    from arenamcp.coach import CoachEngine

    class Capture:
        def __init__(self):
            self.systems = []
            self.prompts = []

        def complete(self, system, user, *args, **kwargs):
            self.systems.append(system)
            self.prompts.append(system + "\n\n" + user)
            return "Cast Grizzly Bears."

    backend = Capture()
    coach = CoachEngine(backend=backend)
    coach._ensure_game_plan_mgr = lambda: None
    coach._rules_db = SimpleNamespace(get_rules_for_situation=lambda *args, **kwargs: [])
    coach.get_advice(_state(legal_actions=["Cast Grizzly Bears [OK]", "Pass"]), trigger="new_turn")
    assert "SECRETS OF STRIXHAVEN" not in backend.prompts[-1] and TUTORS not in backend.prompts[-1]
    assert "FORGET all previous" not in backend.systems[-1]  # calls are stateless: no earlier game to forget
    heckler = _card("Hallway Heckler", "Creature — Elemental Sorcerer", "This creature enters prepared.")
    coach.get_advice(_state(hand=[BEAR, heckler], legal_actions=["Pass"]), trigger="new_turn")
    assert "SECRETS OF STRIXHAVEN" in backend.prompts[-1]
    assert backend.systems[0] == backend.systems[1]


def test_an_unreadable_snapshot_keeps_every_rule_and_the_library():
    from arenamcp.match_context import library_in_play

    odd = _state(decision_context="Select Items", hand=["Grizzly Bears"], players=[None])
    assert library_in_play(odd) is False  # unreadable entries are skipped, not fatal
    broken = _state(battlefield=7)
    assert subjects_in_play(broken) == SUBJECTS
    assert library_in_play(broken) is True


def test_the_kill_switch_sends_every_rule(monkeypatch):
    monkeypatch.setenv("ARENAMCP_SITUATIONAL_RULES", "0")
    assert subjects_in_play(_state(), _decision()) == SUBJECTS
