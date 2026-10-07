"""Game plan x line search (multi-turn planning spec WP9).

The background plan sees the line search's CANDIDATE LINES; validate_plan
replays the plan through the search (per-turn mana from the plan's own land
drops, the plan's line against the best line); the decision block marks this
turn's progress and carries the opponent's instant-speed interaction verdict;
the log carries the line and trick telemetry. Boards are the real recorded
states in tests/strategic_states.py; nothing reads ~/.arenamcp.

Evidence: standalone.log 2026-10-06 15:44:49 (G1 T14 plan "Play Island;
attack Arbiter" with Archive Arbiter cast that turn; the game was lost on
Arbiter's destroy mode instead of gain 4 life) and 17:53:20 (card draw
scheduled while the opponent had lethal on board).
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests import strategic_states as S

from arenamcp import board_assessment as ba
from arenamcp import game_plan as gp
from arenamcp import opponent_tricks as ot
from arenamcp.game_plan import GamePlan, GamePlanManager, compose_strategy_block, validate_plan

_TRICK_DIR = Path(tempfile.mkdtemp(prefix="plan-lines-tricks-"))


def _offline_service() -> ot.TrickTableService:
    """A trick table service that never reads the disk cache, the card database or 17Lands."""
    return ot.TrickTableService(
        primer_fn=lambda code: None,
        ratings_fn=lambda code: [],
        color_ratings_fn=lambda code: None,
        card_lookup=lambda grp: None,
        cache_dir=_TRICK_DIR,
    )


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Deck lookups from the fixture catalog; no real trick tables; fresh caches."""
    monkeypatch.setattr(
        "arenamcp.match_context._local_card",
        lambda grp_id, epoch: dict(S.DECK_CATALOG.get(grp_id) or {"name": f"Unknown({grp_id})"}),
    )
    monkeypatch.setattr(ot.TrickTableService, "_shared", _offline_service())
    monkeypatch.delenv("ARENAMCP_LINE_SEARCH", raising=False)
    ba._CACHE.clear()
    gp._TRICK_CACHE.clear()


def _with_catalog(state: dict) -> dict:
    result = deepcopy(state)
    result["deck_catalog"] = deepcopy(S.DECK_CATALOG)
    return result


def _parsed(turns: list[dict], turn: int, **fields) -> GamePlan:
    data = {
        "role": "control/stabilize",
        "role_reason": "",
        "win_conditions": ["Stabilize, then win"],
        **fields,
    }
    plan = GamePlanManager._parse(json.dumps({**data, "turns": turns}), turn)
    assert plan is not None
    return plan


def _validated(state: dict, turns: list[dict], turn: int, **fields) -> GamePlan:
    return validate_plan(_parsed(turns, turn, **fields), ba.assess(state), state)


def _line_issues(plan: GamePlan) -> list[str]:
    return [issue for issue in plan.issues if issue.startswith("plan line")]


class _Backend:
    def __init__(self, reply: dict):
        self.reply = json.dumps(reply)
        self.calls: list[tuple[str, str]] = []

    def complete(self, system, user, *args, **kwargs):
        self.calls.append((system, user))
        return self.reply


PLAN_REPLY = {
    "role": "control/stabilize",
    "turns": [{"turn": "T", "land": "Island", "cast": ["Archive Arbiter"], "attack": "none"}],
    "win_conditions": ["Stabilize behind Archive Arbiter"],
}


# --- the plan prompt -------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture, first_line",
    [
        ("G1_T12", "  1. T12: Island + Undulating Witness -> life 7 | T14: Murmuring Volume -> life 5"),
        ("G1_T14", "  1. T14: Island + Archive Arbiter (gain 4 life) -> life 2 | T16: Island -> life -3"),
    ],
)
def test_plan_prompt_lists_candidate_lines_to_build_from(fixture, first_line):
    backend = _Backend(PLAN_REPLY)
    GamePlanManager(backend).maybe_reform(_with_catalog(getattr(S, fixture)))
    system, user = backend.calls[0]
    assert (
        'When BOARD FACTS lists CANDIDATE LINES (a deterministic 2-turn search), build "turns" from one of '
        "them and keep its attack/hold posture unless you name a concrete card or combat reason." in system
    )
    block = user[user.index("CANDIDATE LINES (2-turn search") :]
    assert block.splitlines()[1].startswith(first_line)
    assert "Prefer one of these lines; a deviation needs a concrete card or combat reason." in block


def test_plan_prompt_without_the_search_only_mentions_candidate_lines_conditionally(monkeypatch):
    # Review 2026-10-07: with ARENAMCP_LINE_SEARCH=0 the system prompt pointed at a section
    # that wasn't there. It stays static (prefix caching) but is phrased as a condition.
    monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "0")
    backend = _Backend(PLAN_REPLY)
    GamePlanManager(backend).maybe_reform(_with_catalog(S.G1_T12))
    system, user = backend.calls[0]
    assert "CANDIDATE LINES" not in user
    mentions = [line for line in system.splitlines() if "CANDIDATE LINES" in line]
    assert mentions and all(line.startswith("- When BOARD FACTS lists CANDIDATE LINES") for line in mentions)
    assert system == gp.GAME_PLAN_PROMPT


def test_plan_prompt_keeps_the_deck_and_playbook_ahead_of_the_board():
    # Prefix caching: system prompt + deck reference + playbook are the same on every plan call.
    playbook = "UG tempo: flyers and counters; Archive Arbiter is the top end."
    users = []
    for fixture in (S.G1_T12, S.G1_T14):
        backend = _Backend(PLAN_REPLY)
        manager = GamePlanManager(backend)
        manager.seed(playbook)
        manager.maybe_reform(_with_catalog(fixture))
        users.append(backend.calls[0][1])
    for user in users:
        assert user.startswith("DECK REFERENCE")
        head = user.index("DECK PLAYBOOK / STRATEGY:\n" + playbook)
        assert head < user.index("Heartstring Puller") < user.index("BOARD FACTS (deterministic")
    stable = users[0][: users[0].index(playbook) + len(playbook)]
    assert users[1].startswith(stable)


# --- telemetry --------------------------------------------------------------------------


def test_board_facts_log_line_names_the_best_line_and_the_search(caplog):
    with caplog.at_level(logging.INFO, logger="arenamcp.game_plan"):
        GamePlanManager(_Backend(PLAN_REPLY)).maybe_reform(_with_catalog(S.G1_T14))
        GamePlanManager(_Backend(PLAN_REPLY)).maybe_reform(_with_catalog(S.G1_T12))
    facts = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Board facts")]
    t14, t12 = facts
    assert re.search(
        r"\| line: Island \+ Archive Arbiter \(gain 4 life\) — dead on T17 \(nodes=\d+, \d+\.\d ms\) "
        r"\| greedy dead_in=1$",
        t14,
    )
    assert re.search(r"\| line: Island \+ Undulating Witness; then Murmuring Volume — .*\(nodes=\d+, ", t12)
    assert "greedy dead_in" not in t12  # same as the line's


def test_line_telemetry_flags_bounded_and_truncated_searches():
    best = SimpleNamespace(summary=lambda: "Island — survives (life 4)")
    result = SimpleNamespace(best=best, stats=lambda: {})
    assessment = SimpleNamespace(
        line_search=result,
        search_stats={"nodes": 812, "ms": 131.04, "bounded": True, "truncated": True},
        dead_in=None,
        dead_in_greedy=2,
    )
    assert gp._line_telemetry(assessment) == (
        " | line: Island — survives (life 4) (nodes=812, 131.0 ms, bounded, truncated) | greedy dead_in=2"
    )
    assert gp._line_telemetry(SimpleNamespace(line_search=None)) == ""


def test_board_facts_log_has_no_line_with_the_search_switched_off(caplog, monkeypatch):
    monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "0")
    with caplog.at_level(logging.INFO, logger="arenamcp.game_plan"):
        GamePlanManager(_Backend(PLAN_REPLY)).maybe_reform(_with_catalog(S.G1_T14))
    facts = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("Board facts"))
    assert "| line:" not in facts and "greedy dead_in" not in facts


# --- validate_plan: the plan's line against the best line -----------------------------------


# Synthetic boards (review 2026-10-07): a card the line search values, one it can't value.
_SYNTHETIC = {
    "Hill Giant": {
        "type_line": "Creature — Giant",
        "mana_cost": "{3}{R}",
        "oracle_text": "",
        "power": 3,
        "toughness": 3,
        "card_types": ["CardType_Creature"],
    },
    "Grizzly Bears": {
        "type_line": "Creature — Bear",
        "mana_cost": "{1}{R}",
        "oracle_text": "",
        "power": 2,
        "toughness": 2,
        "card_types": ["CardType_Creature"],
    },
    "Reckless Study": {
        "type_line": "Sorcery",
        "mana_cost": "{2}{R}",
        "oracle_text": "Draw two cards.",
        "card_types": ["CardType_Sorcery"],
    },
    "Elemental Uprising": {
        "type_line": "Sorcery",
        "mana_cost": "{2}{R}",
        "oracle_text": "Create two 3/1 red Elemental creature tokens with haste.",
        "card_types": ["CardType_Sorcery"],
    },
}


@pytest.fixture
def synthetic_cards(monkeypatch):
    for name, info in _SYNTHETIC.items():
        monkeypatch.setitem(S.CARDS, name, info)


def _bears_board(hand: str, *, life: int = 3, their_life: int = 20, giant_tapped: bool = False) -> dict:
    """Our T10 Main1, three untapped Mountains; their Hill Giant; ``hand`` and Grizzly Bears in hand."""
    return S.state(
        turn=10,
        active=1,
        phase="Phase_Main1",
        step="",
        life={1: life, 2: their_life},
        lands_played={1: 1, 2: 0},
        library=20,
        opponent_hand=0,
        battlefield=[
            (401, "Mountain", 1, False, 2),
            (402, "Mountain", 1, False, 4),
            (403, "Mountain", 1, False, 6),
            (410, "Hill Giant", 2, giant_tapped, 5),
            (411, "Mountain", 2, False, 1),
        ],
        hand=[(501, hand), (502, "Grizzly Bears")],
        graveyard=[],
    )


_STUDY_TURNS = [
    {"turn": "T", "land": "", "cast": ["Reckless Study"], "attack": "none", "hold": "R for a trick"},
    {"turn": "T+1", "land": "", "cast": ["Grizzly Bears"], "attack": "none"},
]


def _this_turn(state: dict, plan: GamePlan) -> str:
    block = compose_strategy_block(ba.assess(state), plan)
    return next(line.strip() for line in block.splitlines() if "THIS TURN" in line)


@pytest.mark.parametrize("setting", [None, "shadow", "off"])
def test_the_t_step_override_is_shadow_unless_the_line_guard_is_on(synthetic_cards, monkeypatch, setting):
    # Review 2026-10-07: the line check rewrote the plan's T step (and so every
    # prompt's THIS TURN line) under the default settings, before the user's
    # review of the shadow logs. Card draw at 3 life dies to the Giant on T11;
    # the Bears block it.
    if setting:
        monkeypatch.setenv("ARENAMCP_LINE_GUARD", setting)
    state = _bears_board("Reckless Study")
    plan = _validated(state, deepcopy(_STUDY_TURNS), 10)
    assert [step["cast"] for step in plan.turn_plan] == [["Reckless Study"], ["Grizzly Bears"]]
    assert plan.turn_plan[0]["hold"] == "R for a trick"
    (issue,) = _line_issues(plan)
    assert issue.startswith("plan line dies T11; best line Grizzly Bears; then Reckless Study — dead on T13")
    if setting == "off":
        assert "would replace" not in issue and "replaced" not in issue
    else:
        assert issue.endswith("— Line guard (shadow): would replace T with Grizzly Bears")
    assert _this_turn(state, plan).startswith("THIS TURN (T10, now): cast Reckless Study")


def test_with_the_line_guard_on_the_t_step_takes_the_best_line(synthetic_cards, monkeypatch):
    monkeypatch.setenv("ARENAMCP_LINE_GUARD", "on")
    state = _bears_board("Reckless Study")
    plan = _validated(state, deepcopy(_STUDY_TURNS), 10)
    t, t1 = plan.turn_plan
    # The old hold planned around the old casts; the displaced card draw moves to T+1,
    # where the best line casts it.
    assert t == {
        "turn": 10,
        "label": "T",
        "land": "",
        "cast": ["Grizzly Bears"],
        "attack": "none",
        "hold": "",
        "mana": 3,
    }
    assert t1["cast"] == ["Reckless Study"]
    assert _line_issues(plan) == [
        "plan line dies T11; best line Grizzly Bears; then Reckless Study — dead on T13 "
        "— T replaced with Grizzly Bears"
    ]
    assert "T+1: dropped Grizzly Bears (the line check casts it on T10)" in plan.issues
    assert "T+1: casts Reckless Study (the best line plays it then)" in plan.issues
    assert _this_turn(state, plan) == "THIS TURN (T10, now): cast Grizzly Bears; attack: none [game plan T10]"


@pytest.mark.parametrize("setting", [None, "on"])
def test_a_modal_best_step_with_an_unmodelled_mode_never_replaces_the_plan(monkeypatch, setting):
    # G1 T14 at 4 life: the best line is Arbiter choosing gain 4 life, but its destroy
    # mode can't be valued (Splinter Twin) — the mode guard never applies such a
    # choice, so the plan doesn't force it either.
    if setting:
        monkeypatch.setenv("ARENAMCP_LINE_GUARD", setting)
    state = _with_catalog(S.G1_T14)
    plan = _validated(
        state,
        [
            {"turn": "T", "land": "Island", "cast": [], "attack": "none", "hold": "UU for Countersculpt"},
            {"turn": "T+1", "land": "Island", "cast": ["Archive Arbiter"], "attack": "none"},
        ],
        14,
    )
    t, t1 = plan.turn_plan
    assert (t["land"], t["cast"], t["hold"]) == ("Island", [], "UU for Countersculpt")
    assert t1["cast"] == ["Archive Arbiter"]
    assert _line_issues(plan) == [
        "plan line dies T15; best line Island + Archive Arbiter (gain 4 life) — dead on T17 "
        "— T kept (Archive Arbiter's other mode is not modelled)"
    ]


@pytest.mark.parametrize("setting", [None, "on"])
def test_a_plan_casting_an_unmodelled_card_is_not_judged_by_the_search(monkeypatch, setting):
    # Review 2026-10-07: the search gave a token maker no body, scored a plan casting two
    # hasty 3/1s that were exactly lethal 'dies T11' and put the Bears in THIS TURN. It reads
    # those tokens now; tokens exiled at end of turn it still can't value.
    if setting:
        monkeypatch.setenv("ARENAMCP_LINE_GUARD", setting)
    state = S.HASTE_TOKENS_UNREAD
    plan = _validated(
        state, [{"turn": "T", "land": "", "cast": ["Elemental Surge"], "attack": "all"}], 10, role="aggressor"
    )
    assert plan.turn_plan[0]["cast"] == ["Elemental Surge"]
    assert _line_issues(plan) == []
    assert "line check skipped, the search can't value: T: Elemental Surge (its tokens)" in plan.issues
    assert _this_turn(state, plan).startswith("THIS TURN (T10, now): cast Elemental Surge")


def test_a_token_maker_plan_is_judged_now():
    # The same board with tokens the search reads: the plan is the lethal line itself, and
    # an aggressor plan agrees with the role the LETHAL LINE sets (no 'rejected' issue).
    state = S.HASTE_TOKENS_LETHAL
    assessment = ba.assess(state)
    assert assessment.role == "aggressor" and assessment.opp_lethal_on_board
    plan = _validated(
        state,
        [{"turn": "T", "land": "", "cast": ["Elemental Uprising"], "attack": "all"}],
        10,
        role="aggressor",
    )
    assert plan.role == "aggressor" and plan.issues == []
    # Holding the tokens back wins two turns later: noted against the lethal line.
    plan = _validated(
        state, [{"turn": "T", "land": "", "cast": ["Grizzly Bears"], "attack": "none"}], 10, role="aggressor"
    )
    (issue,) = _line_issues(plan)
    assert issue.startswith(
        "plan line lethal on T12; best line Elemental Uprising, attack with Elemental token, Elemental token "
        "— lethal on T10"
    )


def test_unmodelled_effects_are_named_and_modelled_cards_are_not(synthetic_cards):
    # One check for the search, the guards, the board facts and the plan (line_search_moves).
    from arenamcp.line_search_moves import unmodelled_effect as canonical

    unmodelled = gp.unmodelled_effect
    assert unmodelled(S.card(1, "Elemental Uprising", 1)) == ""  # its tokens have bodies now
    assert unmodelled(S.card(2, "Elemental Surge", 1)) == "its tokens"  # exiled at end of turn
    assert unmodelled(S.card(3, "Reckless Study", 1)) == ""  # card draw changes nothing it values
    assert unmodelled(S.card(4, "Grizzly Bears", 1)) == ""
    assert unmodelled(S.card(5, "Archive Arbiter", 1)) == ""  # its gain-4 mode is modelled
    assert unmodelled(S.card(6, "Heartstring Puller", 1)) == ""  # its enters token is modelled
    assert unmodelled(S.card(7, "Unsummon", 1)) == ""  # bounce
    assert unmodelled(S.card(8, "Splinter Twin", 1))  # an aura granting a copy ability
    assert unmodelled(S.card(9, "Chupacabra", 1)) == ""  # enters removal
    assert unmodelled(None) == "" and unmodelled({}) == ""
    for name in ("Elemental Surge", "Pacifism", "Seismic Jolt", "Beast Summons", "Archive Arbiter"):
        assert unmodelled(S.card(10, name, 1)) == canonical(S.card(10, name, 1))


# --- validate_plan: cast names as the prompt writes them ------------------------------------------


def test_candidate_line_and_line_check_notes_name_the_card_in_hand():
    # Review 2026-10-07: "Archive Arbiter (gain 4 life)" copied from CANDIDATE LINES was
    # dropped as 'not in hand', and so was the line check's own "(choose: …)" form.
    state = _with_catalog(S.G1_T12)
    plan = _validated(
        state,
        [
            {"turn": "T", "land": "Island", "cast": ["Undulating Witness"], "attack": "none"},
            {"turn": "T+1", "cast": ["Murmuring Volume"], "attack": "none"},
            {"turn": "T+2", "cast": ["Archive Arbiter (gain 4 life)"], "attack": "none"},
        ],
        12,
        role="defender",
    )
    assert plan.turn_plan[2]["cast"] == ["Archive Arbiter (gain 4 life)"]
    assert not [issue for issue in plan.issues if "dropped" in issue]
    state = _with_catalog(S.G1_T14)
    plan = _validated(
        state,
        [
            {
                "turn": "T",
                "land": "Island",
                "cast": ["Archive Arbiter (choose: gain 4 life)"],
                "attack": "none",
            }
        ],
        14,
    )
    assert plan.turn_plan[0]["cast"] == ["Archive Arbiter (choose: gain 4 life)"]
    assert plan.issues == []  # the best line itself: no false 'plan line dies'


def test_a_noted_mode_the_search_does_not_model_skips_the_line_check():
    # The replay casts the noted mode (it used to cast the best one, so this plan was judged
    # as if it gained 4 life). Destroying Splinter Twin is a mode the search can't value.
    state = _with_catalog(S.G1_T14)
    plan = _validated(
        state,
        [
            {
                "turn": "T",
                "land": "Island",
                "cast": ["Archive Arbiter (choose: destroy target noncreature, nonland permanent)"],
                "attack": "none",
            }
        ],
        14,
    )
    assert plan.turn_plan[0]["cast"] == [
        "Archive Arbiter (choose: destroy target noncreature, nonland permanent)"
    ]
    assert plan.issues == [
        "line check skipped, the search can't value: T: Archive Arbiter (choose: destroy target noncreature, "
        "nonland permanent): mode not modelled"
    ]


_SUNLIT_VERDICT = {
    "type_line": "Sorcery",
    "mana_cost": "{2}{W}{W}",
    "oracle_text": "Choose one —\n•Destroy target creature an opponent controls.\n•You gain 3 life.",
    "card_types": ["CardType_Sorcery"],
}


@pytest.mark.parametrize(
    ("cast", "dies"),
    [
        ("Sunlit Verdict", False),  # no note: the mode worth most
        ("Sunlit Verdict (choose: destroy target creature an opponent controls)", False),
        ("Sunlit Verdict (on Hill Giant)", False),
        ("Sunlit Verdict (choose: gain 3 life)", True),
        ("Sunlit Verdict (gain 3 life)", True),  # the CANDIDATE LINES form
        # Other words for the same mode (review 2026-10-07: all judged as the destroy mode, issues=[]).
        ("Sunlit Verdict (gain life)", True),
        ("Sunlit Verdict (lifegain)", True),
        ("Sunlit Verdict (+3 life)", True),
        ("Sunlit Verdict (choose: mode 2)", True),
        ("Sunlit Verdict (choose: Mode 2: You gain 3 life.)", True),
    ],
)
def test_a_noted_mode_is_judged_as_the_plan_wrote_it(monkeypatch, cast, dies):
    # Both modes are modelled: at 2 life gaining 3 dies to the Giant on T13, destroying it
    # survives. Before the replay honoured notes, the gain-3 plan was skipped ('the replay
    # chose destroy …'); without a note the replay casts the mode worth most.
    monkeypatch.setitem(S.CARDS, "Sunlit Verdict", _SUNLIT_VERDICT)
    state = S.state(
        turn=10, active=1, phase="Phase_Main1", step="", life={1: 2, 2: 20}, lands_played={1: 1, 2: 0},
        library=20, opponent_hand=0,
        battlefield=[(401, "Plains", 1, False, 2), (402, "Plains", 1, False, 4), (403, "Plains", 1, False, 6),
                     (404, "Plains", 1, False, 8), (410, "Hill Giant", 2, False, 5), (411, "Mountain", 2, False, 1),
                     (412, "Mountain", 2, False, 3)],
        hand=[(500, "Sunlit Verdict")], graveyard=[],
    )  # fmt: skip
    plan = _validated(state, [{"turn": "T", "land": "", "cast": [cast], "attack": "none"}], 10)
    assert plan.turn_plan[0]["cast"] == [cast]
    assert not [issue for issue in plan.issues if issue.startswith("line check skipped")]
    if dies:
        (issue,) = _line_issues(plan)
        assert issue.startswith("plan line dies T13; best line Sunlit Verdict (on Hill Giant) — survives")
    else:
        assert _line_issues(plan) == []


def test_a_landcycle_entry_is_a_landcycling_cast():
    # "landcycle Undulating Witness" (the line check's form) pays the landcycling cost.
    state = _with_catalog(S.G1_T12)
    plan = _validated(
        state,
        [{"turn": "T", "land": "Island", "cast": ["landcycle Undulating Witness"], "attack": "none"}],
        12,
        role="defender",
    )
    assert plan.turn_plan[0]["cast"] == ["landcycle Undulating Witness"]
    assert not [issue for issue in plan.issues if "dropped" in issue]


def test_outside_survival_a_worse_plan_line_is_only_recorded():
    # We have lethal now (log and bridge phase names): a plan that holds back is noted, not rewritten.
    for state in (_with_catalog(S.G1_T15_FROM_OPPONENT), S.mac_phase(_with_catalog(S.G1_T15_FROM_OPPONENT))):
        assessment = ba.assess(state)
        assert assessment.lethal_now and not assessment.survival_mode
        plan = validate_plan(
            _parsed([{"turn": "T", "land": "", "cast": [], "attack": "none"}], 15, role="aggressor"),
            assessment,
            state,
        )
        assert plan.turn_plan[0]["attack"] == "none"
        (issue,) = _line_issues(plan)
        assert issue.startswith("plan line lethal on T17; best line attack with Heartstring Puller")
        assert issue.endswith("— lethal on T15") and "replaced" not in issue


def test_a_large_value_gap_in_the_same_outcome_is_noted_without_a_rewrite():
    # G1 T12's real mistake shape: the rock first. Both lines survive, but at 4/4/3 life instead of 7/5/8.
    state = _with_catalog(S.G1_T12)
    plan = _validated(
        state,
        [
            {"turn": "T", "land": "Island", "cast": ["Murmuring Volume"], "attack": "none"},
            {"turn": "T+1", "cast": ["Archive Arbiter"]},
            {"turn": "T+2", "cast": ["Undulating Witness"]},
        ],
        12,
        role="defender",
    )
    assert plan.turn_plan[0]["cast"] == ["Murmuring Volume"]
    (issue,) = _line_issues(plan)
    gap = float(re.search(r"trails the best line by (\d+\.\d) in value", issue).group(1))
    assert gap >= gp.PLAN_LINE_GAP
    assert issue.startswith("plan line survives (life 4, 4, 3)")


def _noted(step) -> list[str]:
    """A line step's casts as the line check writes them: "X (choose: mode, on target)"."""
    modes, targets = dict(step.modes), dict(step.targets)
    casts = []
    for name in step.casts:
        notes = [f"choose: {modes[name]}"] if modes.get(name) else []
        notes += [f"on {targets[name]}"] if targets.get(name) else []
        casts.append(f"{name} ({', '.join(notes)})" if notes else name)
    return casts


@pytest.mark.parametrize("noted", [False, True])
@pytest.mark.parametrize(
    "fixture", ["G1_T8", "G1_T10", "G1_T12", "G1_T14", "ENTERS_REMOVAL", "HASTE_TOKENS_LETHAL"]
)
def test_the_best_line_as_a_plan_passes_the_line_check(fixture, noted):
    state = _with_catalog(getattr(S, fixture))
    best = ba.assess(state).line_search.best
    casts = [_noted(s) if noted else list(s.casts) for s in best.steps]
    turns = [
        {"turn": s.label, "land": s.land, "cast": cast, "attack": ", ".join(s.attack) or "none"}
        for s, cast in zip(best.steps, casts, strict=True)
    ]
    plan = _validated(state, turns, state["turn"]["turn_number"])
    assert _line_issues(plan) == []
    assert not [issue for issue in plan.issues if issue.startswith("line check skipped")]
    assert [step["cast"] for step in plan.turn_plan] == casts


def test_the_line_check_is_off_without_a_usable_search(monkeypatch):
    turns = [{"turn": "T", "land": "Island", "cast": [], "attack": "none"}]
    monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "0")
    plan = _validated(_with_catalog(S.G1_T14), turns, 14)
    assert _line_issues(plan) == [] and plan.turn_plan[0]["cast"] == []
    monkeypatch.delenv("ARENAMCP_LINE_SEARCH")

    def broken(*args, **kwargs):
        raise RuntimeError("search bug")

    monkeypatch.setattr("arenamcp.line_search.evaluate_plan", broken)
    plan = _validated(_with_catalog(S.G1_T14), turns, 14)
    assert _line_issues(plan) == [] and plan.turn_plan[0]["cast"] == []


def test_validated_steps_keep_their_seven_keys():
    plan = _validated(
        _with_catalog(S.G1_T14),
        [{"turn": "T", "land": "Island", "cast": [], "attack": "none"}, {"turn": "T+1", "cast": []}],
        14,
    )
    assert plan.turn_plan and all(
        set(step) == {"turn", "label", "land", "cast", "attack", "hold", "mana"} for step in plan.turn_plan
    )
    json.dumps(plan.as_payload())


# --- validate_plan: mana from the plan's own land drops ------------------------------------


def test_a_land_that_enters_tapped_pays_only_from_the_next_turn(monkeypatch):
    # G3 T10 (their turn; T = our turn 11): five lands, Island and Room of Refuge (enters tapped) in hand.
    state = deepcopy(S.G3_T10_BLOCKS)
    state["hand"].append(S.card(901, "Archive Arbiter", 1))
    turns = [
        {"turn": "T", "land": "Room of Refuge", "cast": ["Archive Arbiter"], "attack": "none"},
        {"turn": "T+1", "land": "Island", "cast": []},
    ]
    plan = _validated(state, turns, 10, role="defender")
    t, t1 = plan.turn_plan
    assert (t["land"], t["cast"], t["mana"]) == ("Room of Refuge", [], 5)
    assert "T: dropped Archive Arbiter — not mana-legal (plan needs 6 mana, budget 5 BU)" in plan.issues
    assert t1["mana"] == 7  # both new lands untapped on T+1
    # The untapped Island pays for it the same turn.
    plan = _validated(state, [{**turns[0], "land": "Island"}], 10, role="defender")
    assert (plan.turn_plan[0]["cast"], plan.turn_plan[0]["mana"]) == (["Archive Arbiter"], 6)
    # Before the line search, the board math's budget (it plays the Island) let the plan through.
    monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "0")
    plan = _validated(state, turns, 10, role="defender")
    assert plan.turn_plan[0]["cast"] == ["Archive Arbiter"]


def test_a_land_the_plan_cannot_play_is_dropped():
    # Island already played this turn; the second Island is in hand but there is no land drop left.
    state = _with_catalog(S.after_land_drop(S.G1_T14, 336))
    plan = _validated(state, [{"turn": "T", "land": "Island", "cast": ["Archive Arbiter"]}], 14)
    assert (plan.turn_plan[0]["land"], plan.turn_plan[0]["cast"]) == ("", ["Archive Arbiter"])
    assert "T: land Island can't be played then (no land drop left, or played earlier)" in plan.issues


def test_an_omitted_land_drop_still_pays():
    # The plan forgot to name the Island: its land drop is not a decision to skip it.
    state = _with_catalog(S.G1_T12)
    plan = _validated(state, [{"turn": "T", "land": "", "cast": ["Undulating Witness"]}], 12, role="defender")
    assert (plan.turn_plan[0]["cast"], plan.turn_plan[0]["mana"]) == (["Undulating Witness"], 5)
    assert not [issue for issue in plan.issues if "not mana-legal" in issue]
    assert _line_issues(plan) == []


def test_an_omitted_land_drop_never_takes_the_land_a_later_turn_names():
    # Review 2026-10-07 (G3 T10: Island and Room of Refuge in hand): the filler drop for
    # an omitted T land took the Island that T+1 names, and validation then deleted the
    # T+1 drop as unplayable, leaving the Island unplayed.
    state = deepcopy(S.G3_T10_BLOCKS)
    plan = _validated(
        state,
        [
            {"turn": "T", "land": "", "cast": [], "attack": "none"},
            {"turn": "T+1", "land": "Island", "cast": [], "attack": "none"},
            {"turn": "T+2", "land": "Room of Refuge", "cast": [], "attack": "none"},
        ],
        10,
        role="defender",
    )
    assert [step["land"] for step in plan.turn_plan] == ["", "Island", "Room of Refuge"]
    assert not [issue for issue in plan.issues if "can't be played then" in issue]


# --- validate_plan: attacks ---------------------------------------------------------------


def test_a_creature_cast_this_turn_is_not_an_attacker():
    # standalone.log 15:44:49: "Play Island; attack Arbiter (opp tapped)" with Archive Arbiter cast that turn.
    state = _with_catalog(S.G1_T14)
    plan = _validated(
        state,
        [
            {"turn": "T", "land": "Island", "cast": ["Archive Arbiter"], "attack": "Arbiter (opp tapped)"},
            {"turn": "T+1", "land": "Island", "cast": [], "attack": "Archive Arbiter"},
        ],
        14,
    )
    t, t1 = plan.turn_plan
    assert t["attack"] == "" and t1["attack"] == "Archive Arbiter"
    assert "T: Archive Arbiter can't attack the turn it is cast" in plan.issues
    # "Everything" attacks with what can; a creature with haste can attack at once.
    plan = _validated(
        state, [{"turn": "T", "land": "Island", "cast": ["Archive Arbiter"], "attack": "all"}], 14
    )
    assert plan.turn_plan[0]["attack"] == "all"
    hasty = deepcopy(state)
    next(c for c in hasty["hand"] if c["name"] == "Archive Arbiter")["keywords"] = ["flying", "haste"]
    plan = _validated(
        hasty, [{"turn": "T", "land": "Island", "cast": ["Archive Arbiter"], "attack": "Arbiter"}], 14
    )
    assert plan.turn_plan[0]["attack"] == "Arbiter"
    assert not [issue for issue in plan.issues if "can't attack" in issue]


def test_a_second_copy_cast_this_turn_leaves_the_ready_copy_an_attacker():
    # Review 2026-10-07: the 'cast this turn' rule worked on names, so a Witness that
    # entered on T10 lost its attack when the plan cast the second Witness on T12.
    state = _with_catalog(S.G1_T12)
    state["battlefield"].append(
        S.card(777, "Undulating Witness", 1, is_tapped=False, turn_entered_battlefield=10)
    )
    turns = [{"turn": "T", "land": "Island", "cast": ["Undulating Witness"], "attack": "Undulating Witness"}]
    plan = _validated(state, deepcopy(turns), 12, role="defender")
    assert plan.turn_plan[0]["attack"] == "Undulating Witness"
    assert not [issue for issue in plan.issues if "can't attack" in issue]
    # A copy that is tapped (or entered this turn) can't attack at T: the rule still applies.
    for extra in ({"is_tapped": True}, {"turn_entered_battlefield": 12}):
        board = deepcopy(state)
        board["battlefield"][-1].update(extra)
        plan = _validated(board, deepcopy(turns), 12, role="defender")
        assert plan.turn_plan[0]["attack"] == ""
        assert "T: Undulating Witness can't attack the turn it is cast" in plan.issues


# --- the decision block: progress and the opponent's interaction -------------------------------


def _played_island_and_witness() -> dict:
    """G1 T12 after Island and Undulating Witness: both entered this turn."""
    state = S.after_land_drop(S.G1_T12, 284)
    witness = next(c for c in state["hand"] if c["instance_id"] == 229)
    state["hand"] = [c for c in state["hand"] if c["instance_id"] != 229]
    state["battlefield"].insert(0, dict(witness, is_tapped=False, turn_entered_battlefield=12))
    return state


def _t12_plan() -> GamePlan:
    return GamePlan(
        role="defender",
        turn_formed=12,
        win_conditions=["Stabilize behind Undulating Witness"],
        turn_plan=[
            {
                "turn": 12,
                "label": "T",
                "land": "Island",
                "cast": ["Undulating Witness", "Murmuring Volume"],
                "attack": "none",
                "hold": "",
                "mana": 5,
            }
        ],
    )


def test_this_turn_marks_the_land_played_and_the_permanents_cast():
    manager = GamePlanManager(None)
    manager._plan = _t12_plan()
    this_turn = manager.strategy_block(_played_island_and_witness()).splitlines()[1]
    assert this_turn == (
        "  THIS TURN (T12, now): play Island ✓; cast Undulating Witness ✓ + Murmuring Volume; "
        "attack: none [game plan T12]"
    )
    fresh = manager.strategy_block(deepcopy(S.G1_T12)).splitlines()[1]
    assert "✓" not in fresh and fresh.startswith(
        "  THIS TURN (T12, now): play Island; cast Undulating Witness"
    )
    # Without the snapshot (the old call shape) nothing is marked.
    assert "✓" not in compose_strategy_block(ba.assess(_played_island_and_witness()), _t12_plan())


def test_the_strategy_block_leaves_out_its_lines_only_when_asked():
    # The coach prompt keeps the LINES line; a typed decision shows it above its options and
    # asks the block (as wired by the autopilot) to leave its copy out (review 2026-10-07, I4).
    manager = GamePlanManager(None)
    state = deepcopy(S.G1_T12)
    full = manager.strategy_block(state).splitlines()
    (lines,) = [line for line in full if line.startswith("  LINES (2-turn search")]
    assert manager.strategy_block(state, with_lines=False).splitlines() == [x for x in full if x != lines]
    facts = gp.grounded_facts_block(state).splitlines()
    assert lines in facts
    assert gp.grounded_facts_block(state, with_lines=False).splitlines() == [x for x in facts if x != lines]


PUMP = ot.TrickCard(grp_id=1, name="Test Pump", mana_cost="{1}{G}", mv=2, pips=("G",), kinds=("pump",))


def _service_with_table() -> ot.TrickTableService:
    service = _offline_service()
    table = ot.TrickTable(
        set_code="FRA", prior={"RG": 1.0}, cards={1: PUMP}, copies={"RG": {1: 2.0}}, built_at=time.time()
    )
    service.register(table)
    return service


def test_the_opponents_interaction_is_a_fact_without_card_names():
    manager = GamePlanManager(None)
    manager.trick_service = _service_with_table()
    state = deepcopy(S.G3_T10_BLOCKS)  # Forest, Forest, Mountain open; three cards in hand
    lines = manager.strategy_block(state).splitlines()
    (fact,) = [line for line in lines if "OPP INTERACTION" in line]
    assert fact.startswith("  OPP INTERACTION: Opponent interaction risk moderate (~14%; 3 open, 3 cards")
    assert len(fact) <= len("  OPP INTERACTION: ") + 160 and "Test Pump" not in fact
    assert lines.index(fact) == len(lines) - 2 and lines[-1].startswith("  Priority:")
    payload = manager.ui_payload(state)
    assert payload["opp_interaction"]["known"] is True
    assert "Test Pump" in payload["opp_interaction"]["ui_line"]
    json.dumps(payload)
    # No table for the set: no fact and no payload key, the rest unchanged.
    bare = GamePlanManager(None)
    bare.trick_service = _offline_service()
    without = bare.strategy_block(state)
    assert "OPP INTERACTION" not in without and "opp_interaction" not in bare.ui_payload(state)
    assert without.splitlines() == [line for line in lines if line != fact]


def test_the_interaction_estimate_is_cached_per_board(monkeypatch):
    calls = []
    real = ot.trick_risk

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(ot, "trick_risk", counted)
    manager = GamePlanManager(None)
    manager.trick_service = _service_with_table()
    state = deepcopy(S.G3_T10_BLOCKS)
    manager.strategy_block(state)
    manager.strategy_block(deepcopy(state))
    manager.ui_payload(state)
    assert len(calls) == 1
    pooled = deepcopy(state)
    next(p for p in pooled["players"] if p["seat_id"] == 2)["mana_pool"] = {"ManaColor_Green": 1}
    manager.strategy_block(pooled)
    assert len(calls) == 2


def test_a_failing_trick_service_never_breaks_the_block():
    class Broken:
        def get_for_state(self, state):
            raise RuntimeError("table bug")

        def ensure_for_state(self, state):
            raise RuntimeError("loader bug")

    manager = GamePlanManager(None)
    manager.trick_service = Broken()
    state = deepcopy(S.G3_T10_BLOCKS)
    manager.observe(state)
    block = manager.strategy_block(state)
    assert block.startswith("STRATEGIC ROLE") and "OPP INTERACTION" not in block


def test_trick_telemetry_once_per_combat_step_and_observed_tricks(caplog):
    service = _service_with_table()
    ensured = []
    real_ensure = service.ensure_for_state
    service.ensure_for_state = lambda state: (ensured.append(state.get("match_id")), real_ensure(state))
    manager = GamePlanManager(None)
    manager.trick_service = service
    blocks = deepcopy(S.G3_T10_BLOCKS)
    with caplog.at_level(logging.INFO, logger="arenamcp.game_plan"):
        manager.observe(blocks)
        manager.observe(deepcopy(blocks))
        manager.observe(S.mac_phase(blocks))  # the bridge's names: the same step
        cast = deepcopy(blocks)
        cast["stack"] = [S.card(320, "Tethermage's Advantage", 2)]
        manager.observe(cast)
    messages = [r.getMessage() for r in caplog.records]
    risk = [m for m in messages if m.startswith("Trick risk")]
    assert len(risk) == 1
    assert risk[0].startswith(
        "Trick risk (turn 10, DeclareBlock): p_any=0.139, verdict=Opponent interaction risk moderate"
    )
    assert [m for m in messages if m.startswith("Trick observed")] == [
        "Trick observed: Tethermage's Advantage (predicted p_any=0.139)"
    ]
    assert len(ensured) == 4


def test_no_trick_lines_outside_combat(caplog):
    manager = GamePlanManager(None)
    manager.trick_service = _service_with_table()
    main = deepcopy(S.G3_T10_BLOCKS)
    main["turn"].update(phase="Phase_Main2", step="")
    with caplog.at_level(logging.INFO, logger="arenamcp.game_plan"):
        manager.observe(main)
        cast = deepcopy(main)
        cast["stack"] = [S.card(320, "Tethermage's Advantage", 2)]
        manager.observe(cast)
    assert not [r for r in caplog.records if r.getMessage().startswith("Trick")]


def test_a_mode_note_naming_no_mode_skips_the_line_check(monkeypatch):
    # A note the replay can't map to a mode used to be judged as the mode worth most, silently.
    monkeypatch.setitem(S.CARDS, "Sunlit Verdict", _SUNLIT_VERDICT)
    monkeypatch.setenv("ARENAMCP_LINE_GUARD", "on")
    state = S.state(
        turn=10, active=1, phase="Phase_Main1", step="", life={1: 2, 2: 20}, lands_played={1: 1, 2: 0},
        library=20, opponent_hand=0,
        battlefield=[(401, "Plains", 1, False, 2), (402, "Plains", 1, False, 4), (403, "Plains", 1, False, 6),
                     (404, "Plains", 1, False, 8), (410, "Hill Giant", 2, False, 5), (411, "Mountain", 2, False, 1),
                     (412, "Mountain", 2, False, 3)],
        hand=[(500, "Sunlit Verdict")], graveyard=[],
    )  # fmt: skip
    cast = "Sunlit Verdict (choose: the good one)"
    plan = _validated(state, [{"turn": "T", "land": "", "cast": [cast], "attack": "none"}], 10)
    (skipped,) = [issue for issue in plan.issues if issue.startswith("line check skipped")]
    assert skipped.endswith("T: Sunlit Verdict (choose: the good one): mode not recognised")
    assert plan.turn_plan[0]["cast"] == [cast] and _line_issues(plan) == []


def test_a_planned_x_spell_skips_the_line_check(monkeypatch):
    # Volcanic Spray (X damage to a creature) kills the Giant; the replay can't cast X spells,
    # so it judged the plan as if nothing were cast, and with the line guard on rewrote T to
    # "cast Grizzly Bears", a chump block.
    monkeypatch.setenv("ARENAMCP_LINE_GUARD", "on")
    state = S.mountain_board(
        life=3, their_life=20, mountains=4, theirs=[(410, "Hill Giant", 2, False, 5)],
        hand=[(501, "Volcanic Spray"), (502, "Grizzly Bears")],
    )  # fmt: skip
    plan = _validated(state, [{"turn": "T", "land": "", "cast": ["Volcanic Spray"], "attack": "none"}], 10)
    (skipped,) = [issue for issue in plan.issues if issue.startswith("line check skipped")]
    assert "T: Volcanic Spray (its X cost)" in skipped
    assert _line_issues(plan) == [] and plan.turn_plan[0]["cast"] == ["Volcanic Spray"]
