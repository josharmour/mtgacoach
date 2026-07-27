"""Invariants of the strategic-cast corpus.

These are the properties that distinguish it from the land-drop-dominated
corpus it replaces. If one of them regresses the dataset silently turns back
into the thing that trained a land-drop reflex, so each is pinned here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
for _p in (str(_REPO), str(_REPO / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.training.build_strategic_casts import (  # noqa: E402
    CardFacts,
    CastPosition,
    Step,
    build_cast_position,
    make_records,
    steps_for_turn,
)
from tools.training.synthesize_menu import Card  # noqa: E402

FOREST = 1
BEAR = 2  # {1}{G} creature
GIANT = 3  # {4}{G} creature
BOLT = 4  # {R} instant
TAPLAND = 5

CARDS = {
    FOREST: Card(FOREST, "Forest", "Basic Land — Forest", "", produced_mana=("G",)),
    BEAR: Card(BEAR, "Grizzly Bears", "Creature — Bear", "{1}{G}"),
    GIANT: Card(GIANT, "Craw Giant", "Creature — Giant", "{4}{G}"),
    BOLT: Card(BOLT, "Lightning Bolt", "Instant", "{R}"),
    TAPLAND: Card(TAPLAND, "Slow Land", "Land", "", produced_mana=("G", "R")),
}


class FakeResolver:
    _scry_raw: dict = {}
    _scry_by_name: dict = {}

    def get(self, grp_id: int) -> Card:
        return CARDS[int(grp_id)]

    def save(self) -> None:  # pragma: no cover
        pass


class FakeDetail:
    def raw(self, card):
        return {}

    def pt(self, card):
        return ""

    def oracle(self, card, limit):
        return "enters tapped" if card.grp_id == TAPLAND else ""


@pytest.fixture
def facts() -> CardFacts:
    return CardFacts(FakeResolver(), FakeDetail())


class FakeRow:
    """Minimal RowView/row stand-in driven by a {column: value} dict."""

    def __init__(self, data: dict[str, str]):
        self.data = data

    def get(self, row, col, default=""):
        return self.data.get(col, default)

    def ids(self, row, col):
        val = self.data.get(col, "")
        return [int(x) for x in val.split("|") if x]


def _records(facts: CardFacts, pos: CastPosition, **kw):
    kw.setdefault("policy", "chain")
    kw.setdefault("min_menu_casts", 2)
    kw.setdefault("include_pass", False)
    kw.setdefault("oracle_chars", 80)
    kw.setdefault("menu_order", "shuffled")
    kw.setdefault("game_key", "g")
    kw.setdefault("rank", "diamond")
    kw.setdefault("expansion", "TST")
    kw.setdefault("won", "True")
    kw.setdefault("wr_bucket", "0.5")
    return make_records(facts, pos, **kw)


def test_land_played_leaves_hand_and_joins_mana(facts):
    rv = FakeRow(
        {
            "user_turn_3_lands_played": str(FOREST),
            "user_turn_3_creatures_cast": str(BEAR),
            "user_turn_3_cards_drawn": str(FOREST),
            "oppo_turn_2_eot_user_cards_in_hand": f"{BEAR}|{GIANT}",
            "oppo_turn_2_eot_user_lands_in_play": f"{FOREST}|{FOREST}",
        }
    )
    pos = build_cast_position(facts, rv, [], 3, on_play=True, basis="oppo")
    assert pos is not None
    # exactly one copy of the played land is consumed by the land drop
    assert pos.hand.count(FOREST) == 0
    assert pos.land_played == FOREST
    assert len(pos.lands) == 3  # two in play + the one played this turn
    assert sorted(pos.hand) == [BEAR, GIANT]


def test_menu_never_offers_a_land_action(facts):
    pos = CastPosition(
        turn=4,
        on_play=True,
        hand=[BEAR, GIANT, FOREST, TAPLAND],
        lands=[FOREST, FOREST, FOREST],
        land_played=FOREST,
        creature_casts=[BEAR],
    )
    recs, drops = _records(facts, pos)
    assert drops == {} or sum(drops.values()) == 0
    assert len(recs) == 1
    assert "Play Land" not in recs[0]["user"]
    assert recs[0]["meta"]["pick_kind"] == "cast"
    assert recs[0]["meta"]["menu_excludes_land_actions"] is True
    # lands in hand are still shown as cards, they are just not actionable
    assert "Forest" in recs[0]["user"]


def test_pass_is_not_a_menu_entry_by_default(facts):
    pos = CastPosition(turn=4, on_play=True, hand=[BEAR, GIANT], lands=[FOREST] * 3, creature_casts=[BEAR])
    recs, _ = _records(facts, pos)
    assert recs
    assert "Pass" not in recs[0]["user"]
    recs_p, _ = _records(facts, pos, include_pass=True)
    assert "Pass" in recs_p[0]["user"]


def test_poisoned_pick_is_dropped(facts):
    """A cast the reconstructed hand cannot account for must never be emitted."""
    pos = CastPosition(turn=6, on_play=True, hand=[BEAR, GIANT], lands=[FOREST] * 5, noncreature_casts=[BOLT])
    recs, drops = _records(facts, pos)
    assert recs == []
    assert drops["pick_not_in_hand"] == 1


def test_menu_below_two_candidates_is_dropped(facts):
    pos = CastPosition(
        turn=2, on_play=True, hand=[BEAR, FOREST], lands=[FOREST, FOREST], creature_casts=[BEAR]
    )
    recs, drops = _records(facts, pos)
    assert recs == []
    assert drops["menu_below_min_casts"] == 1


def test_chain_sequences_state_and_never_repeats_a_prompt(facts):
    pos = CastPosition(
        turn=6,
        on_play=True,
        hand=[BEAR, GIANT, BOLT],
        lands=[FOREST] * 6,
        creature_casts=[BEAR, GIANT],
    )
    steps = steps_for_turn(facts, pos, "chain")
    assert [s.pick for s in steps] == [GIANT, BEAR]  # most expensive first
    assert steps[0].mana_spent == 0 and steps[1].mana_spent == 5
    assert GIANT in steps[0].hand and GIANT not in steps[1].hand
    assert GIANT in steps[1].creatures  # the first cast is on the board now

    recs, _ = _records(facts, pos)
    assert len(recs) == 2
    assert recs[0]["user"] != recs[1]["user"]
    assert "Already cast this turn" in recs[1]["user"]


def test_all_policy_repeats_the_same_position(facts):
    """The 'all' policy is the one that produces contradictory duplicates —
    documented, and the reason 'chain' is the default."""
    pos = CastPosition(
        turn=6, on_play=True, hand=[BEAR, GIANT, BOLT], lands=[FOREST] * 6, creature_casts=[BEAR, GIANT]
    )
    steps = steps_for_turn(facts, pos, "all")
    assert len(steps) == 2
    assert steps[0].hand == steps[1].hand  # identical prompt, different answer
    assert steps_for_turn(facts, pos, "drop") == []
    assert len(steps_for_turn(facts, pos, "first")) == 1


def test_record_carries_attribution_and_pick_envelope(facts):
    pos = CastPosition(turn=4, on_play=False, hand=[BEAR, GIANT], lands=[FOREST] * 3, creature_casts=[BEAR])
    recs, _ = _records(facts, pos)
    rec = recs[0]
    assert rec["attribution"] == "17Lands (CC BY 4.0)"
    payload = json.loads(rec["response"])
    assert list(payload) == ["actions"]
    idx = payload["actions"][0]["pick"]
    assert isinstance(idx, int) and 1 <= idx <= rec["meta"]["menu_size"]
    # the numbered menu line at that index is the card the player really cast
    menu_lines = [ln for ln in rec["user"].splitlines() if ln.strip().startswith(f"{idx}. ")]
    assert menu_lines and "Grizzly Bears" in menu_lines[0]


def test_tapped_land_is_not_counted_as_available_mana(facts):
    pos = CastPosition(
        turn=4,
        on_play=True,
        hand=[BEAR, GIANT],
        lands=[FOREST, FOREST, TAPLAND],
        land_played=TAPLAND,
        land_entered_tapped=True,
        creature_casts=[BEAR],
    )
    recs, _ = _records(facts, pos)
    assert "Mana: 2 (lands: 3" in recs[0]["user"]
    assert "entered TAPPED" in recs[0]["user"]


def test_step_dataclass_is_stable():
    s = Step(
        seq=0, n_casts=1, pick=BEAR, hand=[BEAR], creatures=[], noncreatures=[], mana_spent=0, already_cast=[]
    )
    assert s.seq == 0 and s.pick == BEAR
