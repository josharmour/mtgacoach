"""The line search in the typed decision path (``ActionPlanner``): guards, tags, LINES, fallback.

Field report (2026-10-06 FRA game 1): at T14, 4 life, the autopilot chose
Archive Arbiter's "Destroy target noncreature, nonland permanent" over "You
gain 4 life" (match packet 154509 decision 18; standalone.log 15:44:38
"typed-decision choice ['idx:0']", submitted as CastingTimeOption grpId=208283)
and died to the T15 attack. That CastingTimeOptions answer returned before any
guard. These tests run ``plan_decision_options`` on the recorded states
(tests/strategic_states.py) and pin:

- the mode guard sees that menu: in shadow (the default) it logs the
  gain-4 override and submits the model's pick; 'on' applies a
  non-contingent verdict, but never the real T14 one (Splinter Twin's
  copies are not modelled, so destroying it is a contingent gain), nor one
  for or against a mode the search can't value (review 2026-10-07);
- the line guard runs after the role guard (which keeps precedence),
  shadow by default, and never touches lethal, a pass, their turn, or a
  cast whose effect the search drops (tokens, an enters trigger, an aura);
- ActionsAvailable options on our turn and modal modes carry '[LINE ...]'
  tags, with the LINES (or MODES) line right above OPTIONS:;
- with the model down, the best searched line's play is the fallback,
  keeping the board-math land-first order (tests/test_llm_down_fallback.py);
- ARENAMCP_LINE_SEARCH=0 removes all of it, the modal path included.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from copy import deepcopy
from unittest import mock

import pytest
from tests.strategic_states import (
    BUG_174855,
    CARDS,
    DECK_CATALOG,
    G1_T12,
    G1_T12_MENU,
    G1_T14_MODE_MENU,
    G1_T14_MODE_REQUEST_ID,
    G1_T14_MODE_STATE,
    G1_T15_FROM_OPPONENT,
    actions_decision,
    after_land_drop,
    card,
    cast,
    mac_phase,
    modal_decision,
    play,
    state,
)

from arenamcp import board_assessment as ba
from arenamcp import line_guard as lg
from arenamcp.action_planner import (
    DECLINE_DECISION,
    ActionPlanner,
    board_math_option_pick,
    line_fallback_option_pick,
)
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.line_search import compare_modes

LOGGER = "arenamcp.action_planner"
DESTROY, GAIN = "idx:0", "idx:1"
ROCK, ISLAND, ROOM = "idx:1", "idx:5", "idx:6"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Default guard modes, the line search on, empty caches, no card DB."""
    for name in ("ARENAMCP_LINE_GUARD", "ARENAMCP_MODE_GUARD", "ARENAMCP_LINE_SEARCH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        "arenamcp.match_context._local_card",
        lambda grp_id, epoch: dict(DECK_CATALOG.get(grp_id) or {"name": f"Unknown({grp_id})"}),
    )
    ba._CACHE.clear()
    lg._MODE_CACHE.clear()
    yield
    ba._CACHE.clear()
    lg._MODE_CACHE.clear()


class _Backend:
    """Answers every decision with ``reply``; records the prompts."""

    def __init__(self, reply: dict | None = None, *, available: bool = True):
        self.reply = json.dumps(reply or {})
        self._available = available
        self.prompts: list[str] = []

    def available(self) -> bool:
        return self._available

    def complete(self, system, user, *args, **kwargs):
        self.prompts.append(user)
        return self.reply


def _planner(option_ids: list[str] | None = None, reasoning: str = "") -> tuple[ActionPlanner, _Backend]:
    backend = _Backend({"option_ids": option_ids or [], "reasoning": reasoning})
    return ActionPlanner(backend), backend


def _down() -> ActionPlanner:
    return ActionPlanner(_Backend(available=False))


def _header(prompt: str) -> str:
    """The decision header: PENDING DECISION through the options, before the game state."""
    return prompt[prompt.index("PENDING DECISION") : prompt.index("GAME STATE:")]


def _option_line(prompt: str, option_id: str) -> str:
    return next(line for line in _header(prompt).splitlines() if line.startswith(f"- {option_id}:"))


def _with_life(source: dict, life: int) -> dict:
    result = deepcopy(source)
    for player in result["players"]:
        if player["is_local"]:
            player["life_total"] = life
    return result


def _mode_decision():
    return modal_decision(
        [(oid, label, dict(meta)) for oid, label, meta in G1_T14_MODE_MENU], G1_T14_MODE_REQUEST_ID
    )


def _without_twin(source: dict) -> dict:
    """The T14 menu with Splinter Twin gone: the destroy mode has nothing unmodelled to answer."""
    result = deepcopy(source)
    result["battlefield"] = [c for c in result["battlefield"] if c["instance_id"] != 324]
    return result


def _room_of_refuge(life: int = 7) -> tuple[dict, object]:
    """G1_T12 with Room of Refuge (enters tapped) in hand: at 7 life only the Island line survives T13."""
    source = _with_life(G1_T12, life)
    source["hand"].append(card(990, "Room of Refuge", 1))
    menu = [
        *G1_T12_MENU[:3],
        (ROOM, "Play land: Room of Refuge", play(990), None),
        ("pass", "Pass", None, None),
    ]
    return source, actions_decision(menu)


def _mode_logs(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER and "Mode guard" in r.getMessage()]


def _line_logs(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER and "Line guard" in r.getMessage()]


# --- the mode guard on the real T14 menu ---------------------------------------------------


def test_t14_mode_menu_shadow_logs_the_gain_4_override_and_keeps_the_pick(caplog):
    planner, backend = _planner([DESTROY], "Destroying Splinter Twin removes the engine.")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        chosen = planner.plan_decision_options(_mode_decision(), deepcopy(G1_T14_MODE_STATE))
    assert chosen == [DESTROY]  # shadow: today's choice is submitted
    (record,) = _mode_logs(caplog)
    message = record.getMessage()
    assert record.levelno == logging.INFO
    assert message.startswith(
        "Mode guard (shadow): would replace Mode 1: Destroy target noncreature, nonland permanent "
        "with Mode 2: You gain 4 life — Archive Arbiter: gain 4 life (dead on T17) instead of Mode 1"
    )
    assert "(dead on T15)" in message and "contingent" in message and "Splinter Twin" in message
    trace = planner.get_last_decision_trace()
    assert trace["mode_guard"]["replaced"] == DESTROY and trace["mode_guard"]["with"] == GAIN
    assert trace["mode_guard"]["applied"] is False and trace["mode_guard"]["mode"] == "shadow"
    json.dumps(trace)
    assert planner.get_decision_reasoning([DESTROY]) == "Destroying Splinter Twin removes the engine."
    # The model saw each mode's line next to it.
    # No mode is called 'best' while the destroy mode can't be valued.
    prompt = backend.prompts[-1]
    assert "[LINE: dead T15; effect unmodelled]" in _option_line(prompt, DESTROY)
    assert "[LINE: survives T15 at 2, dead T17]" in _option_line(prompt, GAIN)
    assert trace["lines"]["tags"] == {
        DESTROY: "[LINE: dead T15; effect unmodelled]",
        GAIN: "[LINE: survives T15 at 2, dead T17]",
    }


def test_t14_mode_guard_on_still_never_applies_the_contingent_verdict(monkeypatch, caplog):
    monkeypatch.setenv("ARENAMCP_MODE_GUARD", "on")
    planner, _ = _planner([DESTROY], "Destroying Splinter Twin removes the engine.")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        chosen = planner.plan_decision_options(_mode_decision(), deepcopy(G1_T14_MODE_STATE))
    # Splinter Twin's copies are not modelled: destroying it might be the better play.
    assert chosen == [DESTROY]
    (record,) = _mode_logs(caplog)
    assert record.getMessage().startswith("Mode guard (on, contingent: not applied): would replace Mode 1")
    trace = planner.get_last_decision_trace()["mode_guard"]
    assert trace["applied"] is False and trace["mode"] == "on" and trace["contingent"]


@pytest.mark.parametrize("setting", [None, "shadow", "on"])
def test_a_verdict_against_an_unmodelled_chosen_mode_never_applies(monkeypatch, caplog, setting):
    # Without Splinter Twin compare_modes has no contingent note, but the chosen
    # destroy mode is still unmodelled (worth 0 to the search): review 2026-10-07.
    if setting is not None:
        monkeypatch.setenv("ARENAMCP_MODE_GUARD", setting)
    planner, _ = _planner([DESTROY], "Destroy their permanent.")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        chosen = planner.plan_decision_options(_mode_decision(), _without_twin(G1_T14_MODE_STATE))
    assert chosen == [DESTROY]
    (record,) = _mode_logs(caplog)
    assert record.levelno == logging.INFO
    expected = "Mode guard (on, contingent: not applied):" if setting == "on" else "Mode guard (shadow):"
    assert record.getMessage().startswith(expected)
    assert (
        "'Mode 1: Destroy target noncreature, nonland permanent' (chosen) not modelled" in record.getMessage()
    )
    trace = planner.get_last_decision_trace()["mode_guard"]
    assert trace["with"] == GAIN and trace["applied"] is False
    assert trace["contingent"] == [
        "'Mode 1: Destroy target noncreature, nonland permanent' (chosen) not modelled"
    ]
    assert planner.get_decision_reasoning([DESTROY]) == "Destroy their permanent."


# A sorcery on the stack whose modes the search values both ways (review 2026-10-07 boards).
_MODAL_CARDS = {
    "Plains": {
        "type_line": "Basic Land — Plains",
        "oracle_text": "({T}: Add {W}.)",
        "card_types": ["CardType_Land"],
    },
    "Hill Giant": {
        "type_line": "Creature — Giant",
        "mana_cost": "{3}{R}",
        "oracle_text": "",
        "power": 3,
        "toughness": 3,
        "card_types": ["CardType_Creature"],
    },
    "Sunlit Verdict": {
        "type_line": "Sorcery",
        "mana_cost": "{2}{W}{W}",
        "oracle_text": "Choose one —\n•Destroy target creature an opponent controls.\n•You gain 3 life.",
        "card_types": ["CardType_Sorcery"],
    },
    "Beast Charm": {
        "type_line": "Sorcery",
        "mana_cost": "{2}{W}{W}",
        "oracle_text": "Choose one —\n•Create a 4/4 green Beast creature token.\n•You gain 3 life.",
        "card_types": ["CardType_Sorcery"],
    },
}


def _modal_board(monkeypatch, name: str) -> tuple[dict, object]:
    """Our T10 Main1 at 2 life, four tapped Plains, ``name`` on the stack; their untapped Hill Giant."""
    for card_name, info in _MODAL_CARDS.items():
        monkeypatch.setitem(CARDS, card_name, info)
    source = state(
        turn=10, active=1, phase="Phase_Main1", step="",
        life={1: 2, 2: 20}, lands_played={1: 1, 2: 0}, library=20, opponent_hand=0,
        battlefield=[(401, "Plains", 1, True, 2), (402, "Plains", 1, True, 4), (403, "Plains", 1, True, 6),
                     (404, "Plains", 1, True, 8), (410, "Hill Giant", 2, False, 5), (411, "Mountain", 2, False, 1),
                     (412, "Mountain", 2, False, 3)],
        hand=[], graveyard=[],
    )  # fmt: skip
    source["stack"] = [card(500, name, 1)]
    labels = [line.lstrip("•") for line in _MODAL_CARDS[name]["oracle_text"].split("\n")[1:]]

    def meta(index: int) -> dict:
        return {
            "actionType": "CastingTimeOption", "choiceKind": "modal",
            "requestClass": "CastingTimeOption_ModalRequest", "childIndex": 0, "label": f"Mode {index + 1}",
            "optionIndex": index, "grpId": 9000 + index, "sourceId": 500, "min": 1, "max": 1,
        }  # fmt: skip

    menu = [(f"idx:{i}", f"Mode {i + 1}: {label}", meta(i)) for i, label in enumerate(labels)]
    return source, modal_decision(menu, (500, 600))


@pytest.mark.parametrize(
    ("setting", "expected"), [(None, ["idx:1"]), ("shadow", ["idx:1"]), ("on", ["idx:0"])]
)
def test_a_fully_modelled_mode_verdict_applies_only_when_on(monkeypatch, caplog, setting, expected):
    # Gaining 3 at 2 life dies to the Giant on T13; destroying the Giant survives.
    if setting is not None:
        monkeypatch.setenv("ARENAMCP_MODE_GUARD", setting)
    source, decision = _modal_board(monkeypatch, "Sunlit Verdict")
    planner, backend = _planner(["idx:1"], "Gain 3 life.")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        chosen = planner.plan_decision_options(decision, source)
    assert chosen == expected
    (record,) = _mode_logs(caplog)
    trace = planner.get_last_decision_trace()["mode_guard"]
    assert trace["with"] == "idx:0" and trace["contingent"] == []
    if setting == "on":
        assert record.levelno == logging.WARNING and record.getMessage().startswith(
            "Mode guard: Sunlit Verdict"
        )
        assert trace["applied"] is True
        assert planner.get_decision_reasoning(["idx:0"]).startswith("Sunlit Verdict: choose destroy target")
    else:
        assert record.levelno == logging.INFO and record.getMessage().startswith("Mode guard (shadow):")
        assert trace["applied"] is False
    # Both modes are valued: the surviving one is 'best'.
    prompt = backend.prompts[-1]
    assert "[LINE best: survives T11 at 2, 2 after T13, 2 after T15]" in _option_line(prompt, "idx:0")
    assert "[LINE: survives T11 at 2, dead T13]" in _option_line(prompt, "idx:1")


@pytest.mark.parametrize("setting", [None, "on"])
def test_a_chosen_token_mode_is_never_replaced(monkeypatch, caplog, setting):
    # Review 2026-10-07 (m1_mode_token): the 4/4 Beast blocks the Giant every turn, but
    # the search values the token mode at 0; 'on' replaced it with 'gain 3 life'.
    if setting is not None:
        monkeypatch.setenv("ARENAMCP_MODE_GUARD", setting)
    source, decision = _modal_board(monkeypatch, "Beast Charm")
    planner, backend = _planner(["idx:0"], "Make a 4/4 Beast to block their Hill Giant.")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert planner.plan_decision_options(decision, source) == ["idx:0"]
    (record,) = _mode_logs(caplog)
    assert record.levelno == logging.INFO
    assert "(chosen) not modelled" in record.getMessage()
    assert planner.get_last_decision_trace()["mode_guard"]["applied"] is False
    assert planner.get_decision_reasoning(["idx:0"]) == "Make a 4/4 Beast to block their Hill Giant."
    header = _header(backend.prompts[-1])
    assert "[LINE best:" not in header
    assert "[LINE: dead T11; effect unmodelled]" in _option_line(backend.prompts[-1], "idx:0")
    assert "[LINE: survives T11 at 2, dead T13]" in _option_line(backend.prompts[-1], "idx:1")


def test_the_mode_menu_prompt_says_the_board_facts_precede_the_choice():
    # Review 2026-10-07: the G1 T14 tags said gain 4 survives while the strategy block
    # (computed without the pending trigger) said no line survives: the MODES line
    # names each mode's outcome and which facts already count the choice.
    planner, backend = _planner([GAIN], "Gain 4.")
    planner.plan_decision_options(_mode_decision(), deepcopy(G1_T14_MODE_STATE))
    header = _header(backend.prompts[-1])
    lines = header.splitlines()
    modes = lines[lines.index("OPTIONS:") - 1]
    assert modes == (
        "MODES (each searched with this choice resolved; the role and board facts below were computed "
        "before it resolves): Destroy target noncreature, nonland permanent (not modelled) — dead T15 | "
        "gain 4 life — survives T15 at 2, dead T17"
    )
    assert len(modes) <= 320
    assert planner.get_last_decision_trace()["lines"]["summary"] == modes


def test_mode_guard_off_is_silent(monkeypatch, caplog):
    monkeypatch.setenv("ARENAMCP_MODE_GUARD", "off")
    planner, _ = _planner([DESTROY], "Destroy.")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert planner.plan_decision_options(_mode_decision(), _without_twin(G1_T14_MODE_STATE)) == [DESTROY]
    assert _mode_logs(caplog) == [] and "mode_guard" not in planner.get_last_decision_trace()


@pytest.mark.parametrize("setting", ["shadow", "on"])
def test_the_kill_switch_covers_the_modal_path(monkeypatch, caplog, setting):
    monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "0")
    monkeypatch.setenv("ARENAMCP_MODE_GUARD", setting)
    planner, backend = _planner([DESTROY], "Destroy.")
    with mock.patch.object(lg, "compare_modes", wraps=compare_modes) as spy, caplog.at_level(logging.INFO):
        chosen = planner.plan_decision_options(_mode_decision(), _without_twin(G1_T14_MODE_STATE))
    assert chosen == [DESTROY] and spy.call_count == 0
    assert _mode_logs(caplog) == []
    assert "[LINE" not in _header(backend.prompts[-1])
    trace = planner.get_last_decision_trace()
    assert "mode_guard" not in trace and "lines" not in trace


@pytest.mark.parametrize("case", ["lethal_now", "unassessed"])
def test_mode_guard_never_steers_with_lethal_on_board_or_an_unknown_board(monkeypatch, caplog, case):
    monkeypatch.setenv("ARENAMCP_MODE_GUARD", "on")
    real = ba.assess

    def doctored(source):
        found = real(source) if case == "lethal_now" else None
        return dataclasses.replace(found, lethal_now=True) if found is not None else None

    monkeypatch.setattr(ba, "assess", doctored)
    planner, _ = _planner([DESTROY], "Destroy.")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert planner.plan_decision_options(_mode_decision(), _without_twin(G1_T14_MODE_STATE)) == [DESTROY]
    assert _mode_logs(caplog) == []


def test_the_mode_comparison_runs_once_for_tags_and_guard():
    planner, _ = _planner([DESTROY], "Destroy.")
    with mock.patch.object(lg, "compare_modes", wraps=compare_modes) as spy:
        planner.plan_decision_options(_mode_decision(), deepcopy(G1_T14_MODE_STATE))
    assert spy.call_count == 1


def test_search_choices_are_never_mode_guarded():
    decision = PendingDecision(
        (5, 5),
        "Search",
        (DecisionOption("idx:0", "Island", meta={"grpId": 106529}),),
        can_cancel=True,
    )
    planner, _ = _planner(["idx:0"], "Fetch the Island.")
    with mock.patch.object(lg, "mode_guard", side_effect=AssertionError("Search reached the mode guard")):
        assert planner.plan_decision_options(decision, deepcopy(G1_T12)) == ["idx:0"]


def test_an_invalid_modal_answer_still_declines():
    planner, _ = _planner([DESTROY, GAIN], "Both.")  # choose one: two modes is not a valid answer
    assert planner.plan_decision_options(_mode_decision(), deepcopy(G1_T14_MODE_STATE)) == [DECLINE_DECISION]


# --- the line guard after the role guard -----------------------------------------------------


@pytest.mark.parametrize("phase_names", ["log", "bridge"])
def test_line_guard_shadow_logs_a_dying_land_pick(caplog, phase_names):
    source, decision = _room_of_refuge()
    if phase_names == "bridge":
        source = mac_phase(source)  # 'Main1'/'None' from the Mac bridge
    planner, _ = _planner([ROOM], "Play Room of Refuge.")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        chosen = planner.plan_decision_options(decision, source)
    assert chosen == [ROOM]
    (record,) = _line_logs(caplog)
    assert record.levelno == logging.INFO
    assert record.getMessage().startswith(
        "Line guard (shadow): would replace Play land: Room of Refuge with Play land: Island — Island + "
        "Undulating Witness"
    )
    assert "dead on T13" in record.getMessage()
    trace = planner.get_last_decision_trace()
    assert "role_guard" not in trace  # the role guard never replaces a land
    assert {k: trace["line_guard"][k] for k in ("replaced", "with", "applied", "mode")} == {
        "replaced": ROOM,
        "with": ISLAND,
        "applied": False,
        "mode": "shadow",
    }
    assert trace["line_guard"]["reason"].startswith("Line guard: Island + Undulating Witness")
    json.dumps(trace)
    assert planner.get_decision_reasoning([ROOM]) == "Play Room of Refuge."


def test_line_guard_on_replaces_the_pick_like_the_role_guard(monkeypatch, caplog):
    monkeypatch.setenv("ARENAMCP_LINE_GUARD", "on")
    source, decision = _room_of_refuge()
    planner, _ = _planner([ROOM], "Play Room of Refuge.")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        chosen = planner.plan_decision_options(decision, source)
    assert chosen == [ISLAND]
    (record,) = _line_logs(caplog)
    assert record.levelno == logging.WARNING
    assert record.getMessage().startswith("Line guard: Island + Undulating Witness")
    assert record.getMessage().endswith("[model chose Play land: Room of Refuge: Play Room of Refuge.]")
    trace = planner.get_last_decision_trace()["line_guard"]
    assert trace["applied"] is True and trace["with"] == ISLAND and trace["mode"] == "on"
    reasoning = planner.get_decision_reasoning([ISLAND])
    assert reasoning.startswith("Island + Undulating Witness instead of Play land: Room of Refuge:")


@pytest.mark.parametrize(
    ("env", "life"), [({"ARENAMCP_LINE_GUARD": "off"}, 7), ({"ARENAMCP_LINE_SEARCH": "0", "ARENAMCP_LINE_GUARD": "on"}, 7),
                      ({"ARENAMCP_LINE_GUARD": "on"}, 11)],
    ids=["off", "kill-switch", "value-only-land-gain"],
)  # fmt: skip
def test_line_guard_stays_silent(monkeypatch, caplog, env, life):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    source, decision = _room_of_refuge(life)
    planner, _ = _planner([ROOM], "Play Room of Refuge.")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert planner.plan_decision_options(decision, source) == [ROOM]
    assert _line_logs(caplog) == [] and "line_guard" not in planner.get_last_decision_trace()


@pytest.mark.parametrize("case", ["their_turn", "stack", "unknown_body"])
def test_line_guard_on_never_fires_on_their_turn_a_stack_or_an_unknown_body(monkeypatch, caplog, case):
    monkeypatch.setenv("ARENAMCP_LINE_GUARD", "on")
    source, decision = _room_of_refuge()
    if case == "their_turn":
        source["turn"]["active_player"] = 2
    elif case == "stack":
        source["stack"] = [
            {"instance_id": 999, "name": "Ability", "type_line": "Ability", "controller_seat_id": 2}
        ]
    else:  # the planner passes the assessment's unknown-P/T notes on
        for entry in source["battlefield"]:
            if entry["instance_id"] == 280:
                entry.update(power=None, toughness=None)
    planner, _ = _planner([ROOM], "Play Room of Refuge.")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert planner.plan_decision_options(decision, source) == [ROOM]
    assert _line_logs(caplog) == [] and "line_guard" not in planner.get_last_decision_trace()


def test_role_guard_keeps_precedence(monkeypatch, caplog):
    monkeypatch.setenv("ARENAMCP_LINE_GUARD", "on")
    planner, _ = _planner([ROCK], "Murmuring Volume ramps toward Arbiter.")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert planner.plan_decision_options(actions_decision(G1_T12_MENU), deepcopy(G1_T12)) == [ISLAND]
    trace = planner.get_last_decision_trace()
    assert trace["role_guard"]["replaced"] == ROCK and "line_guard" not in trace
    assert _line_logs(caplog) == []


def _burn_board() -> tuple[dict, object]:
    """Our Main1 at 3 life, they are at 2: Shock wins now; Stone Wall would only block."""
    source = state(
        turn=10, active=1, phase="Phase_Main1", step="", life={1: 3, 2: 2}, lands_played={1: 1, 2: 0},
        library=20, opponent_hand=1,
        battlefield=[(10, "Mountain", 1, False, 2), (11, "Mountain", 1, False, 4), (12, "Mountain", 1, False, 6),
                     (20, "Heartstring Puller", 2, False, 7), (21, "Cadet", 2, False, 7)],
        hand=[], graveyard=[],
    )  # fmt: skip
    shock = {"name": "Shock", "type_line": "Instant", "mana_cost": "{R}", "card_types": ["CardType_Instant"],
             "oracle_text": "Shock deals 2 damage to any target."}  # fmt: skip
    wall = {"name": "Stone Wall", "type_line": "Creature — Wall", "mana_cost": "{2}{R}", "oracle_text": "Defender",
            "power": 0, "toughness": 6, "keywords": ["defender"], "card_types": ["CardType_Creature"]}  # fmt: skip
    source["hand"] = [{**card(30, "Unsummon", 1), **shock}, {**card(31, "Unsummon", 1), **wall}]
    menu = [("idx:1", "Cast Shock", cast(30), True), ("idx:2", "Cast Stone Wall", cast(31), True),
            ("pass", "Pass", None, None)]  # fmt: skip
    return source, actions_decision(menu)


@pytest.mark.parametrize("pick", ["idx:1", "idx:2", "pass"])
def test_never_steers_away_from_or_toward_lethal(monkeypatch, caplog, pick):
    monkeypatch.setenv("ARENAMCP_LINE_GUARD", "on")
    source, decision = _burn_board()
    planner, backend = _planner([pick], "Go.")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert planner.plan_decision_options(decision, source) == [pick]
    assert _line_logs(caplog) == [] and "line_guard" not in planner.get_last_decision_trace()
    assert "[LINE" not in _header(backend.prompts[-1])  # a winning line on the table: no tags
    assert "lethal on T10" in _header(backend.prompts[-1])  # the LINES line says so


def test_lethal_on_board_is_left_alone():
    state_ = deepcopy(G1_T15_FROM_OPPONENT)
    state_["hand"] = [card(801, "Twinned Vision", 2), card(802, "Theorix Metamage", 2)]
    decision = actions_decision(
        [
            ("idx:0", "Cast Twinned Vision", cast(801), True),
            ("idx:1", "Cast Theorix Metamage", cast(802), True),
        ]
    )
    with mock.patch.dict("os.environ", {"ARENAMCP_LINE_GUARD": "on"}):
        planner, _ = _planner(["idx:0"], "Draw first.")
        assert planner.plan_decision_options(decision, state_) == ["idx:0"]
    assert "line_guard" not in planner.get_last_decision_trace()


# Boards from the 2026-10-07 safety review: the chosen cast's effect is one the search drops.
_UNMODELLED_CARDS = {
    "Hill Giant": {
        "type_line": "Creature — Giant", "mana_cost": "{3}{R}", "oracle_text": "",
        "power": 3, "toughness": 3, "card_types": ["CardType_Creature"],
    },
    "Gray Ogre": {
        "type_line": "Creature — Ogre", "mana_cost": "{2}{R}", "oracle_text": "",
        "power": 2, "toughness": 2, "card_types": ["CardType_Creature"],
    },
    "Grizzly Bears": {
        "type_line": "Creature — Bear", "mana_cost": "{1}{R}", "oracle_text": "",
        "power": 2, "toughness": 2, "card_types": ["CardType_Creature"],
    },
    "Elemental Uprising": {
        "type_line": "Sorcery", "mana_cost": "{2}{R}",
        "oracle_text": "Create two 3/1 red Elemental creature tokens with haste.", "card_types": ["CardType_Sorcery"],
    },
    "Beast Summons": {
        "type_line": "Sorcery", "mana_cost": "{2}{R}",
        "oracle_text": "Create a 4/4 green Beast creature token.", "card_types": ["CardType_Sorcery"],
    },
    "Sky Knight": {
        "type_line": "Creature — Angel", "mana_cost": "{3}{W}", "oracle_text": "Flying",
        "power": 4, "toughness": 4, "keywords": ["flying"], "card_types": ["CardType_Creature"],
    },
    "Chupacabra": {
        "type_line": "Creature — Horror", "mana_cost": "{2}{R}{R}",
        "oracle_text": "When this creature enters, destroy target creature an opponent controls.",
        "power": 2, "toughness": 2, "card_types": ["CardType_Creature"],
    },
    "Giant Spider": {
        "type_line": "Creature — Spider", "mana_cost": "{3}{R}", "oracle_text": "Reach",
        "power": 2, "toughness": 4, "keywords": ["reach"], "card_types": ["CardType_Creature"],
    },
}  # fmt: skip

_UNMODELLED_BOARDS = {
    # Two hasty 3/1s are exactly lethal: they are at 6 and their only creature is tapped.
    "haste_tokens_lethal": (3, 6, [(410, "Hill Giant", 2, True, 5)], "Elemental Uprising", "Grizzly Bears"),
    # The 4/4 token walls the Giant and the Ogre; the Bears die on T13.
    "token_blocker": (3, 20, [(410, "Hill Giant", 2, False, 5), (413, "Gray Ogre", 2, False, 7)],
                      "Beast Summons", "Grizzly Bears"),
    # The enters trigger destroys their 4/4 flier; the reach blocker only trades turns.
    "enters_removal": (4, 20, [(410, "Sky Knight", 2, False, 5), (413, "Gray Ogre", 2, False, 7)],
                       "Chupacabra", "Giant Spider"),
}  # fmt: skip


def _unmodelled_board(monkeypatch, name: str) -> tuple[dict, object, str]:
    for card_name, info in _UNMODELLED_CARDS.items():
        monkeypatch.setitem(CARDS, card_name, info)
    life, their_life, theirs, unmodelled, other = _UNMODELLED_BOARDS[name]
    lands = [(401 + i, "Mountain", 1, False, 2 + 2 * i) for i in range(4 if name == "enters_removal" else 3)]
    source = state(
        turn=10, active=1, phase="Phase_Main1", step="",
        life={1: life, 2: their_life}, lands_played={1: 1, 2: 0}, library=20, opponent_hand=0,
        battlefield=[*lands, *theirs, (411, "Mountain", 2, False, 1)],
        hand=[(501, unmodelled), (502, other)], graveyard=[],
    )  # fmt: skip
    decision = actions_decision(
        [
            ("idx:0", f"Cast {unmodelled}", cast(501), True),
            ("idx:1", f"Cast {other}", cast(502), True),
            ("pass", "Pass", None, None),
        ]
    )
    return source, decision, unmodelled


@pytest.mark.parametrize("board", sorted(_UNMODELLED_BOARDS))
@pytest.mark.parametrize("setting", [None, "on"])
def test_the_line_guard_never_overrides_a_cast_the_search_cannot_value(monkeypatch, caplog, board, setting):
    # Review 2026-10-07: with ARENAMCP_LINE_GUARD=on each of these picks was replaced by
    # the other creature, because the search scored the pick's line as doing nothing.
    if setting is not None:
        monkeypatch.setenv("ARENAMCP_LINE_GUARD", setting)
    source, decision, unmodelled = _unmodelled_board(monkeypatch, board)
    result = ba.assess(source).line_search
    assert not result.truncated and ("cast", 501, None) not in result.best.first_actions  # the blind spot
    planner, backend = _planner(["idx:0"], "The model's reason.")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert planner.plan_decision_options(decision, deepcopy(source)) == ["idx:0"]
    assert planner.get_decision_reasoning(["idx:0"]) == "The model's reason."
    (record,) = _line_logs(caplog)
    assert record.levelno == logging.INFO
    assert record.getMessage().startswith(f"Line guard (not checked: {unmodelled} (")
    trace = planner.get_last_decision_trace()
    assert trace["line_guard"]["applied"] is False and trace["line_guard"]["unmodelled"].startswith(
        unmodelled
    )
    # The prompt says the search can't judge that card instead of calling it worse.
    prompt = backend.prompts[-1]
    assert _option_line(prompt, "idx:0").endswith("[LINE: effect not modelled]")
    assert "[LINE best:" not in _header(prompt)
    lines = next(line for line in _header(prompt).splitlines() if line.startswith("LINES ("))
    assert f"| not modelled, judge these yourself: {unmodelled} (" in lines and len(lines) <= 320
    assert trace["lines"]["unmodelled"] == {"idx:0": lines.split("judge these yourself: ", 1)[1]}


# --- tags and the LINES line in the decision prompt -------------------------------------------


def test_t12_prompt_tags_each_option_and_puts_lines_above_the_menu():
    planner, backend = _planner([ISLAND], "Island first.")
    assert planner.plan_decision_options(actions_decision(G1_T12_MENU), deepcopy(G1_T12)) == [ISLAND]
    prompt = backend.prompts[-1]
    header = _header(prompt).splitlines()
    options_at = header.index("OPTIONS:")
    assert header[options_at - 1].startswith(
        "LINES (2-turn search, greedy 3rd; their new cards/tricks not modelled)"
    )
    assert "best Island + Undulating Witness" in header[options_at - 1] and len(header[options_at - 1]) <= 320
    assert "[LINE best: 7 after T13, 5 after T15" in _option_line(prompt, ISLAND)
    assert "[LINE: -" in _option_line(prompt, ROCK) and "vs best; 4 after T13" in _option_line(prompt, ROCK)
    assert "[LINE: -" in _option_line(prompt, "idx:4")
    assert "[LINE" not in _option_line(prompt, "pass")  # Main1: passing goes to combat
    tags = planner.get_last_decision_trace()["lines"]["tags"]
    assert set(tags) == {ROCK, "idx:4", ISLAND} and all(len(tag) <= 60 for tag in tags.values())
    their_cards = {c["name"] for c in G1_T12["battlefield"] if c["controller_seat_id"] != 1}
    assert not any(name in tag for tag in tags.values() for name in their_cards)


def test_prompt_growth_stays_within_budget_and_the_kill_switch_removes_it(monkeypatch):
    planner, backend = _planner([ISLAND], "Island first.")
    planner.plan_decision_options(actions_decision(G1_T12_MENU), deepcopy(G1_T12))
    with_lines = _header(backend.prompts[-1])
    monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "0")
    planner, backend = _planner([ISLAND], "Island first.")
    planner.plan_decision_options(actions_decision(G1_T12_MENU), deepcopy(G1_T12))
    without = _header(backend.prompts[-1])
    assert "LINES (" not in without and "[LINE" not in without
    assert "lines" not in planner.get_last_decision_trace()
    assert 0 < len(with_lines) - len(without) <= 700


def test_no_tags_or_lines_on_the_opponents_turn():
    source = deepcopy(BUG_174855)
    menu = [
        (f"idx:{i}", f"Cast {c['name']}", cast(c["instance_id"]), True) for i, c in enumerate(source["hand"])
    ]
    planner, backend = _planner(["pass"], "Hold.")
    planner.plan_decision_options(actions_decision([*menu, ("pass", "Pass", None, None)]), source)
    header = _header(backend.prompts[-1])
    assert "LINES (" not in header and "[LINE" not in header


def test_a_failing_line_search_leaves_the_prompt_as_today(monkeypatch):
    monkeypatch.setattr(lg, "option_note", mock.Mock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(lg, "lines_summary", mock.Mock(side_effect=RuntimeError("boom")))
    planner, backend = _planner([ISLAND], "Island first.")
    assert planner.plan_decision_options(actions_decision(G1_T12_MENU), deepcopy(G1_T12)) == [ISLAND]
    assert "[LINE" not in _header(backend.prompts[-1])


# --- the fallback when the model is down -----------------------------------------------------


def _removal_board() -> tuple[dict, object]:
    """G1_T12 with two more Islands, Unsummon in hand and the Island already played."""
    source = deepcopy(G1_T12)
    source["battlefield"] += [
        card(960 + i, "Island", 1, is_tapped=False, turn_entered_battlefield=3) for i in (0, 1)
    ]
    source["hand"].append(card(991, "Unsummon", 1))
    source = after_land_drop(source, 284)
    menu = [
        ("idx:1", "Cast Murmuring Volume", cast(217), True),
        ("idx:2", "Cast Undulating Witness", cast(229), True),
        ("idx:3", "Cast Unsummon", cast(991), True),
        ("pass", "Pass", None, None),
    ]
    return source, actions_decision(menu)


def test_fallback_casts_the_lines_removal_on_an_attacker_first():
    source, decision = _removal_board()
    board_pick, _ = board_math_option_pick(decision, source)
    assert board_pick == ["idx:2"]  # board math: the most expensive cast of the same line
    planner = _down()
    assert planner.plan_decision_options(decision, source) == ["idx:3"]
    assert planner.last_llm_failure == "unavailable"
    trace = planner.get_last_decision_trace()
    assert trace["fallback"] == "line" and trace["board_math_pick"] == ["idx:2"]
    reasoning = planner.get_decision_reasoning(["idx:3"])
    assert reasoning.startswith("Model unavailable; the best searched line is Undulating Witness + Unsummon")


def test_fallback_keeps_the_board_math_order_within_the_same_line():
    # Land first: the line's own Island (G1_T12), the board-math pick and the line's agree.
    planner = _down()
    assert planner.plan_decision_options(actions_decision(G1_T12_MENU), deepcopy(G1_T12)) == [ISLAND]
    assert planner.get_last_decision_trace()["fallback"] == "board_math"
    # With the Island down, the line's land drop goes before its aimed removal too.
    source, decision = _removal_board()
    source["hand"].append(card(992, "Island", 1))
    for player in source["players"]:
        if player["is_local"]:
            player["lands_played"] = 0
    with_land = actions_decision(
        [(o.option_id, o.label, o.meta, o.payable) for o in decision.options[:3]]
        + [("idx:4", "Play land: Island", play(992), None), ("pass", "Pass", None, None)]
    )
    assert board_math_option_pick(with_land, source)[0] == ["idx:4"]
    assert line_fallback_option_pick(with_land, source, ["idx:4"]) is None
    assert _down().plan_decision_options(with_land, source) == ["idx:4"]


def test_fallback_line_pick_needs_main_phase_empty_stack_confirmed_payment_and_the_search(monkeypatch):
    source, decision = _removal_board()
    assert line_fallback_option_pick(decision, source, ["idx:2"]) == (["idx:3"], mock.ANY)
    stacked = {
        **deepcopy(source),
        "stack": [{"instance_id": 999, "name": "Ability", "controller_seat_id": 2}],
    }
    assert line_fallback_option_pick(decision, stacked, ["pass"]) is None
    combat = deepcopy(source)
    combat["turn"].update(phase="Phase_Combat", step="Step_BeginCombat")
    assert line_fallback_option_pick(decision, combat, ["pass"]) is None
    unconfirmed = actions_decision(
        [
            (o.option_id, o.label, o.meta, None if o.option_id == "idx:3" else o.payable)
            for o in decision.options
        ]
    )
    assert line_fallback_option_pick(unconfirmed, source, ["idx:2"]) is None
    targets = PendingDecision((1, 1), "SelectTargets", (DecisionOption("tgt:238", "Heartstring Puller"),))
    assert line_fallback_option_pick(targets, source, ["tgt:238"]) is None
    monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "0")
    assert line_fallback_option_pick(decision, source, ["idx:2"]) is None
    planner = _down()
    assert planner.plan_decision_options(decision, source) == ["idx:2"]  # today's board-math pick
    assert planner.get_last_decision_trace()["fallback"] == "board_math"


def test_fallback_line_pick_errors_fall_back_to_board_math(monkeypatch):
    source, decision = _removal_board()
    monkeypatch.setattr(lg, "line_fallback_pick", mock.Mock(side_effect=RuntimeError("boom")))
    planner = _down()
    assert planner.plan_decision_options(decision, source) == ["idx:2"]
    assert planner.get_last_decision_trace()["fallback"] == "board_math"
