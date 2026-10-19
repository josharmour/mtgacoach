"""A non-lethal Sneak the line search rates well below its best line is doubted (ledger #187, 2026-10-19).

Match 9a6ea9be T8 (15:59:13.991-15:59:20.480; Player.log gsid 181, our declare-blockers step):
we are at 25, they are at 27. Hare Apparent 477 attacked and is unblocked; Hare 498 and Rabbit
502 entered this turn, Rabbit 481 stayed home; one Plains open, five Hare Apparents in hand and
Leonardo, Big Brother in the command zone (no earlier cast). Theirs: Badgermole Cub untapped,
Skull Prophet and an animated Forest tapped, The Great Henge. Arena offered Leonardo through
Sneak for {W}; the option carried "[LINE: -13 vs best; 25 after T9, 25 after T11, 25 after T13]"
(the best line: no Sneak now, Hare Apparent + Leonardo on T10). The low-effort answer took it
("puts a 4-power Leonardo on the battlefield attacking now for only {W}"): 4 damage of their 27,
returning the Hare. Leonardo then died to removal and every later cast cost {2} more. The Sneak
guard forced only lethal Sneaks and had no doubt for a bad one.

Now a non-lethal Sneak tagged ``LINE_GUARD_MARGIN`` or more below the best line is a second-look
concern ("sneak_gap"): the low answer waits for high effort (or is asked again at high effort),
and when no high-effort answer comes the fallback passes instead of Sneaking.
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from typing import Any

import pytest
from tests import leonardo_boards_20261019 as lb

from arenamcp import sneak
from arenamcp.action_planner import ActionPlanner
from arenamcp.board_assessment import assess
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.line_guard import option_note

# The lines these tests pin must not depend on a loaded machine (the hard cap is a clock).
pytestmark = pytest.mark.usefixtures("unlimited_search_deadline")


LOCAL, OPPONENT = 1, 2
CARDS: dict[str, dict[str, Any]] = {
    "Plains": deepcopy(lb.CARDS["Plains"]),
    "Leonardo, Big Brother": deepcopy(lb.CARDS["Leonardo, Big Brother"]),
    "Hare Apparent": deepcopy(lb.CARDS["Hare Apparent"]),
    "Rabbit": deepcopy(lb.CARDS["Rabbit"]),
    "Badgermole Cub": {
        "type_line": "Creature — Badger Mole", "mana_cost": "{1}{G}", "power": 2, "toughness": 2,
        "card_types": ["CardType_Creature"],
        "oracle_text": "When this creature enters, earthbend 1.\nWhenever you tap a creature for mana, add an "
        "additional {oG}.",
    },
    "Skull Prophet": {
        "type_line": "Creature — Human Druid", "mana_cost": "{B}{G}", "power": 3, "toughness": 1,
        "card_types": ["CardType_Creature"], "oracle_text": "{oT}: Add {oB} or {oG}.\n{oT}: Mill two cards.",
    },
    "Forest": {
        "type_line": "Basic Land — Forest", "power": 1, "toughness": 1, "oracle_text": "",
        "card_types": ["CardType_Creature", "CardType_Land"],
    },
    "The Great Henge": {
        "type_line": "Legendary Artifact", "mana_cost": "{7}{G}{G}", "card_types": ["CardType_Artifact"],
        "oracle_text": "This spell costs {oX} less to cast, where X is the greatest power among creatures you "
        "control.\n{oT}: Add {oGoG}. You gain 2 life.\nWhenever a nontoken creature you control enters, put a "
        "+1/+1 counter on it and draw a card.",
    },
    "Drowned Catacomb": {
        "type_line": "Land", "card_types": ["CardType_Land"],
        "oracle_text": "This land enters tapped unless you control an Island or a Swamp.\n{oT}: Add {oU} or {oB}.",
    },
    "Island": {"type_line": "Basic Land — Island", "card_types": ["CardType_Land"], "oracle_text": ""},
    "Command Tower": {
        "type_line": "Land", "card_types": ["CardType_Land"],
        "oracle_text": "{oT}: Add one mana of any color in your commander's color identity.",
    },
}  # fmt: skip
SNEAK_META = {
    "actionType": "ActionType_Cast", "grpId": 100471, "instanceId": 238, "abilityGrpId": 115,
    "manaCost": [{"color": '[ "White" ]', "count": 1}], "alternativeGrpId": 203047, "alternative": "Sneak",
}  # fmt: skip
SNEAK_LABEL = (
    "Cast Leonardo, Big Brother [via Sneak {W}: return an unblocked attacker you control to its owner's hand; "
    "Leonardo, Big Brother enters tapped and attacking]"
)
MODEL_REASON = (
    "Sneak returns the blocked-or-unblocked Hare to hand and puts a 4-power Leonardo on the battlefield "
    "attacking now for only {W}, advancing the aggressive clock while keeping the Hare for recasting."
)


def _card(iid: int, name: str, seat: int, **extra: Any) -> dict[str, Any]:
    return {
        "instance_id": iid, "name": name, "owner_seat_id": seat, "controller_seat_id": seat,
        **deepcopy(CARDS[name]), **extra,
    }  # fmt: skip


def t8_declare_blockers() -> dict[str, Any]:
    """Match 9a6ea9be T8 at gsid 181: our declare-blockers priority, the Sneak offered."""
    ours = [
        _card(477, "Hare Apparent", LOCAL, is_tapped=True, turn_entered_battlefield=6, is_attacking=True,
              is_blocked=False),
        _card(498, "Hare Apparent", LOCAL, is_tapped=False, turn_entered_battlefield=8),
        _card(481, "Rabbit", LOCAL, is_tapped=False, turn_entered_battlefield=6, object_kind="TOKEN"),
        _card(502, "Rabbit", LOCAL, is_tapped=False, turn_entered_battlefield=8, object_kind="TOKEN"),
        _card(476, "Plains", LOCAL, is_tapped=False, turn_entered_battlefield=6),
        _card(456, "Plains", LOCAL, is_tapped=True, turn_entered_battlefield=4),
        _card(448, "Plains", LOCAL, is_tapped=True, turn_entered_battlefield=2),
    ]  # fmt: skip
    theirs = [
        _card(451, "Badgermole Cub", OPPONENT, is_tapped=False, turn_entered_battlefield=3),
        _card(463, "Skull Prophet", OPPONENT, is_tapped=True, turn_entered_battlefield=5),
        _card(450, "Forest", OPPONENT, is_tapped=True, turn_entered_battlefield=3),
        _card(484, "The Great Henge", OPPONENT, is_tapped=True, turn_entered_battlefield=7),
        _card(483, "Drowned Catacomb", OPPONENT, is_tapped=True, turn_entered_battlefield=7),
        _card(462, "Island", OPPONENT, is_tapped=True, turn_entered_battlefield=5),
        _card(438, "Command Tower", OPPONENT, is_tapped=True, turn_entered_battlefield=1),
    ]
    return {
        "match_id": "9a6ea9be-a791-45ee-a084-8c505d66ccb7",
        "local_seat_id": LOCAL,
        "opponent_seat_id": OPPONENT,
        "turn": {"turn_number": 8, "active_player": LOCAL, "priority_player": LOCAL, "phase": "Phase_Combat",
                 "step": "Step_DeclareBlock"},
        "players": [
            {"seat_id": LOCAL, "life_total": 25, "is_local": True, "lands_played": 1},
            {"seat_id": OPPONENT, "life_total": 27, "is_local": False, "lands_played": 0},
        ],
        "battlefield": ours + theirs,
        "hand": [_card(i, "Hare Apparent", LOCAL) for i in (497, 475, 455, 246, 245)],
        "command": [_card(238, "Leonardo, Big Brother", LOCAL)],
        "commander_grp_ids": [100471],
        "commander_casts": {"100471": 0},
        "graveyard": [_card(495, "Hare Apparent", LOCAL)],
        "stack": [],
        "zones": {"library_count": 80, "library_count_source": "log_zone_membership", "opponent_hand_count": 2},
        "deck_cards": [],
        "legal_actions_raw": [dict(SNEAK_META, actionType="ActionType_Cast")],
    }  # fmt: skip


def _menu() -> PendingDecision:
    return PendingDecision(
        (181, 242), "ActionsAvailable",
        (DecisionOption("idx:0", SNEAK_LABEL, True, dict(SNEAK_META)), DecisionOption("pass", "Pass")),
        can_pass=True,
    )  # fmt: skip


def test_the_t8_sneak_is_four_of_twenty_seven_and_tagged_well_below_the_best_line():
    state = t8_declare_blockers()
    found = sneak.sneak_now(state)
    assert (found.through, found.power, found.total, found.life) == (2, 4, 4, 27)
    assert not found.lethal and not found.beats_hard_cast
    assert sneak.sneak_cast_pick(_menu(), state) == ([], "")  # left to the model, as before
    result = assess(state).line_search
    tag = option_note(result, _menu().find("idx:0"), state)
    assert tag.startswith("[LINE: -") and " vs best" in tag, tag
    assert int(tag.split("-")[1].split(" ")[0]) >= 5


# --- the doubt --------------------------------------------------------------------------------


class _Model:
    """A scripted model: the low leg answers ``low``; the high leg answers ``high`` (None: fails)."""

    def __init__(self, low: str, high: str | None = None):
        self.low, self.high = low, high
        self.efforts: list = []

    def complete(self, system, user, *args, **kwargs):
        if "PENDING DECISION" not in user:
            return "{}"
        effort = kwargs.get("reasoning_effort")
        self.efforts.append(effort)
        if effort != "high":
            return self.low
        if self.high is None:
            raise TimeoutError("LLM streaming time budget exhausted")
        return self.high


def _reply(option_id: str, reasoning: str) -> str:
    return json.dumps({"option_ids": [option_id], "reasoning": reasoning})


def _tags(state: dict) -> tuple[Any, dict[str, str]]:
    menu = _menu()
    source, _text, unmodelled = ActionPlanner._line_prompt_context(menu, state)
    return source, {o.option_id: ActionPlanner._line_tag(source, o, state, unmodelled) for o in menu.options}


def test_the_low_answer_sneak_is_doubted():
    state = t8_declare_blockers()
    source, tags = _tags(state)
    gap = int(tags["idx:0"].split("-")[1].split(" ")[0])
    planner = ActionPlanner(_Model("{}"), timeout=5, land_drop_first=False)
    planner._last_decision_trace = {}
    concerns = planner._second_look_concerns(_menu(), state, ["idx:0"], MODEL_REASON, source, tags)
    assert [kind for kind, _ in concerns] == ["sneak_gap"]
    assert concerns[0][1].startswith(
        "Sneaking Leonardo, Big Brother now is not lethal (Sneak Leonardo, Big Brother ({W}) returning Hare "
        f"Apparent: 2 unblocked - 2 + 4 = 4 against their 27 life), and the line search rates it {gap} below "
        "its best line ("
    )
    assert planner._last_decision_trace["sneak_gap"]["gap"] == gap
    # Passing raises nothing.
    assert planner._second_look_concerns(_menu(), state, ["pass"], "", source, tags) == []


def test_no_doubt_for_a_small_gap_or_a_lethal_sneak():
    state = t8_declare_blockers()
    source, tags = _tags(state)
    planner = ActionPlanner(_Model("{}"), timeout=5, land_drop_first=False)
    planner._last_decision_trace = {}
    small = dict(tags, **{"idx:0": "[LINE: -4 vs best; 25 after T9]"})
    assert planner._second_look_concerns(_menu(), state, ["idx:0"], MODEL_REASON, source, small) == []
    lethal = t8_declare_blockers()
    lethal["players"][1]["life_total"] = 4
    assert sneak.sneak_gap(_menu(), lethal, "idx:0", tags["idx:0"], 5) == (0, "")


def test_without_a_high_answer_the_doubted_sneak_gives_way_to_a_pass(caplog):
    model = _Model(_reply("idx:0", MODEL_REASON), high=None)
    planner = ActionPlanner(model, timeout=5, land_drop_first=False)
    with caplog.at_level(logging.INFO, logger="arenamcp.action_planner"):
        assert planner.plan_decision_options(_menu(), t8_declare_blockers()) == ["pass"]
    assert "high" in model.efforts
    assert "sneak_gap: Sneaking Leonardo, Big Brother now is not lethal" in caplog.text
    trace = planner.get_last_decision_trace()
    assert trace["fallback"] == "sneak_gap" and trace["validated_ids"] == ["pass"]


def test_a_high_answer_still_decides():
    model = _Model(
        _reply("idx:0", MODEL_REASON),
        high=_reply("idx:0", "Leonardo's 4 now and next turn's width beat holding him back."),
    )
    planner = ActionPlanner(model, timeout=5, land_drop_first=False)
    assert planner.plan_decision_options(_menu(), t8_declare_blockers()) == ["idx:0"]
