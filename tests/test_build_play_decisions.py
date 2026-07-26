"""Contract tests for tools/training/build_play_decisions.py.

These encode the five NON-NEGOTIABLE conditions from the menu-reconstruction
fidelity spike (docs/rl-training-status.md §19.1). Violating any of them poisons
the dataset in a way that is invisible until a training run fails the gate, so
each one gets a test:

1. no affordability filter          -> test_condition1_*
2. drop picks absent from the hand  -> test_condition2_*
3. menu labelled CANDIDATE          -> test_condition3_*
4. scope: own precombat main, T<=5  -> test_condition4_*
5. no ability activations           -> test_condition5_*

Plus the prompt-staleness guard (§17.2's "one test that is the whole defence"):
if production edits AUTOPILOT_SYSTEM_PROMPT, this dataset's system prompt must
change with it or the suite fails.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from tools.training import build_play_decisions as bpd
from tools.training.synthesize_menu import Card

from arenamcp.action_planner import AUTOPILOT_SYSTEM_PROMPT

# ---------------------------------------------------------------------------
# Fakes -- the real CardResolver parses a 620 MB Scryfall bulk file.
# ---------------------------------------------------------------------------

PLAINS, SWAMP, FOREST = 82390, 82392, 82394
BOLT, BIG_GUY, TWO_DROP = 96000, 96001, 96002

_CARDS = {
    PLAINS: Card(PLAINS, "Plains", "Basic Land — Plains", "", "normal", ("W",)),
    SWAMP: Card(SWAMP, "Swamp", "Basic Land — Swamp", "", "normal", ("B",)),
    FOREST: Card(FOREST, "Forest", "Basic Land — Forest", "", "normal", ("G",)),
    BOLT: Card(BOLT, "Plasma Bolt", "Instant", "{R}", "normal", ()),
    BIG_GUY: Card(BIG_GUY, "Voidforged Titan", "Creature — Giant", "{5}{G}", "normal", ()),
    TWO_DROP: Card(TWO_DROP, "Virus Beetle", "Creature — Insect", "{1}{B}", "normal", ()),
}


class FakeResolver:
    def get(self, grp_id: int) -> Card:
        return _CARDS.get(int(grp_id), Card(int(grp_id), "", "", "", "", ()))


@pytest.fixture
def resolver() -> FakeResolver:
    return FakeResolver()


@pytest.fixture
def detail(resolver: FakeResolver) -> bpd.CardDetail:
    return bpd.CardDetail(resolver)


def _position(**kw) -> bpd.Position:
    base = {"turn": 3, "on_play": True, "hand": [], "lands": [], "lands_played": [], "casts": []}
    base.update(kw)
    return bpd.Position(**base)


# ---------------------------------------------------------------------------
# Prompt identity / staleness guard
# ---------------------------------------------------------------------------


def test_system_prompt_is_production_prompt_plus_documented_addendum():
    assert bpd.SYSTEM_PROMPT.startswith(AUTOPILOT_SYSTEM_PROMPT), (
        "The dataset's system prompt must BE production's, imported — not retyped. "
        "If this fails, production edited AUTOPILOT_SYSTEM_PROMPT and the dataset is stale."
    )
    assert bpd.CANDIDATE_MENU_ADDENDUM in bpd.SYSTEM_PROMPT


def test_addendum_states_the_menu_is_incomplete_and_untagged():
    text = bpd.CANDIDATE_MENU_ADDENDUM.lower()
    assert "incomplete" in text
    assert "not mtga's legal-action list" in text
    assert "[ok]" in text  # explicitly says the affordability tags are absent


# ---------------------------------------------------------------------------
# Condition 1 -- NO affordability filter
# ---------------------------------------------------------------------------


def test_condition1_unaffordable_spells_stay_in_the_menu(resolver, detail):
    """MTGA's menu is not mana-gated; 47% of real Cast rows are unaffordable.
    A 6-drop with one untapped land must still be offered."""
    pos = _position(hand=[BIG_GUY, FOREST], lands=[FOREST])
    menu = bpd.order_menu(
        resolver,
        bpd.synth_from_zones(resolver, pos.hand, pos.lands, policy="nofilter", allow_play=True),
        seed=1,
        mode="canonical",
    )
    assert ("Cast", BIG_GUY) in menu


def test_condition1_zero_lands_still_lists_casts(resolver):
    menu = bpd.synth_from_zones(resolver, [BOLT, BIG_GUY], [], policy="nofilter", allow_play=True)
    assert ("Cast", BOLT) in menu and ("Cast", BIG_GUY) in menu


# ---------------------------------------------------------------------------
# Condition 2 -- drop rows whose recorded pick is not in the reconstructed hand
# ---------------------------------------------------------------------------


def _make(resolver, detail, pos, **kw):
    st = kw.pop("st", None) or bpd.BuildStats()
    rec = bpd._make_record(
        resolver,
        detail,
        pos,
        st,
        game_key="g",
        rank="mythic",
        expansion="EOE",
        won="True",
        wr_bucket="0.6",
        multi_cast_policy=kw.get("multi_cast_policy", "drop"),
        include_pass=kw.get("include_pass", False),
        menu_order=kw.get("menu_order", "canonical"),
        oracle_chars=0,
    )
    return rec, st


def test_condition2_pick_absent_from_hand_is_dropped(resolver, detail):
    # The player is recorded as casting Virus Beetle, but the reconstructed
    # hand never contained it (the 5.4% poison term).
    pos = _position(hand=[FOREST, BOLT], lands=[FOREST], casts=[TWO_DROP])
    rec, st = _make(resolver, detail, pos)
    assert rec is None
    assert st.dropped["pick_not_in_hand"] == 1
    assert st.dropped_by_kind["pick_not_in_hand:cast"] == 1


def test_condition2_pick_present_in_hand_is_kept(resolver, detail):
    pos = _position(hand=[FOREST, TWO_DROP], lands=[FOREST, SWAMP], casts=[TWO_DROP])
    rec, st = _make(resolver, detail, pos)
    assert rec is not None
    assert st.dropped["pick_not_in_hand"] == 0
    assert rec["meta"]["pick_action"] == ["Cast", TWO_DROP]


def test_condition2_land_drop_absent_from_hand_is_dropped(resolver, detail):
    pos = _position(hand=[BOLT], lands=[], lands_played=[FOREST])
    rec, st = _make(resolver, detail, pos)
    assert rec is None
    assert st.dropped["pick_not_in_hand"] == 1


# ---------------------------------------------------------------------------
# Condition 3 -- CANDIDATE menu, never a complete legal-action list
# ---------------------------------------------------------------------------


def test_condition3_user_message_says_candidate_not_legal(resolver, detail):
    pos = _position(hand=[FOREST, TWO_DROP], lands=[SWAMP], lands_played=[FOREST])
    rec, _ = _make(resolver, detail, pos)
    user = rec["user"]
    assert "Candidate: (pick by number" in user
    assert "RECONSTRUCTED" in user
    assert "Legal:" not in user, "a reconstructed menu must never be presented as the legal list"


def test_condition3_divergence_is_recorded_in_metadata(resolver, detail):
    pos = _position(hand=[FOREST, TWO_DROP], lands=[SWAMP], lands_played=[FOREST])
    rec, st = _make(resolver, detail, pos)
    assert rec["meta"]["menu_is_candidate_not_legal"] is True

    args = _Args()
    stats = bpd.summarize(st, args, 0.0)
    div = stats["divergence_from_production"]
    assert div["menu_is_complete_legal_action_list"] is False
    assert div["affordability_tags_present"] is False
    assert div["measured_menu_recall_vs_real_mtga"] == 0.622
    assert stats["attribution"] == "17Lands (CC BY 4.0)"


class _Args:
    replay_csv: list = []
    min_rank = "diamond"
    min_turn = 1
    max_turn = 5
    include_pass = False
    multi_cast_policy = "drop"
    menu_order = "canonical"
    state_basis = "oppo"
    emit = "all"


# ---------------------------------------------------------------------------
# Condition 4 -- scope
# ---------------------------------------------------------------------------


def test_condition4_cli_defaults_encode_the_scope():
    ns = bpd.build_arg_parser().parse_args(["--replay-csv", "x.csv", "--out", "y.jsonl"])
    assert ns.max_turn == 5, "condition 4: pick recall degrades badly after player-turn 5"
    assert ns.min_rank == "diamond", "diamond+mythic only"
    assert ns.multi_cast_policy == "drop"
    assert ns.include_pass is False
    assert ns.state_basis == "oppo"
    assert ns.menu_order == "shuffled", "a canonical order hands the model a positional shortcut"


def test_condition4_predecessor_turn_ordering():
    # on the play:  user_1, oppo_1, user_2, oppo_2 ...
    assert bpd.predecessor_prefix(1, on_play=True, basis="oppo") is None
    assert bpd.predecessor_prefix(3, on_play=True, basis="oppo") == "oppo_turn_2_"
    # on the draw:  oppo_1, user_1, oppo_2, user_2 ...
    assert bpd.predecessor_prefix(1, on_play=False, basis="oppo") == "oppo_turn_1_"
    assert bpd.predecessor_prefix(3, on_play=False, basis="oppo") == "oppo_turn_3_"
    # legacy basis = previous own turn
    assert bpd.predecessor_prefix(3, on_play=True, basis="user") == "user_turn_2_"


def test_condition4_prompt_is_own_precombat_main(resolver, detail):
    pos = _position(hand=[FOREST, TWO_DROP], lands=[SWAMP], lands_played=[FOREST])
    rec, _ = _make(resolver, detail, pos)
    assert "T3 YOUR | Main1 | Pri:You" in rec["user"]
    assert rec["user"].startswith(bpd.TRIGGER_LINE)


# ---------------------------------------------------------------------------
# Condition 5 -- no ability activations
# ---------------------------------------------------------------------------


def test_condition5_no_ability_entries_are_emitted(resolver, detail):
    pos = _position(
        hand=[FOREST, TWO_DROP],
        lands=[SWAMP],
        creatures=[TWO_DROP],
        lands_played=[FOREST],
    )
    rec, _ = _make(resolver, detail, pos)
    assert "Activate" not in rec["user"]
    menu_block = rec["user"].split("T3 YOUR")[0]
    assert all(
        line.strip().split(". ", 1)[1].startswith(("Play Land:", "Cast ", "Pass"))
        for line in menu_block.splitlines()
        if line.strip()[:1].isdigit()
    )


# ---------------------------------------------------------------------------
# Response shape / index integrity
# ---------------------------------------------------------------------------


def test_response_is_production_shaped_and_index_matches_the_menu(resolver, detail):
    pos = _position(hand=[FOREST, TWO_DROP, BOLT], lands=[SWAMP], lands_played=[FOREST])
    rec, _ = _make(resolver, detail, pos)
    payload = json.loads(rec["response"])
    assert list(payload) == ["actions"]
    assert len(payload["actions"]) == 1
    pick = payload["actions"][0]["pick"]
    assert isinstance(pick, int) and pick >= 1
    numbered = [ln.strip() for ln in rec["user"].splitlines() if ln.strip()[:1].isdigit()]
    assert numbered[pick - 1] == f"{pick}. Play Land: Forest"
    assert rec["meta"]["pick_index"] == pick


def test_menu_order_is_deterministic_for_a_given_seed(resolver):
    menu_set = bpd.synth_from_zones(resolver, [FOREST, TWO_DROP, BOLT], [SWAMP], "nofilter", True)
    a = bpd.order_menu(resolver, menu_set, 12345, "shuffled")
    b = bpd.order_menu(resolver, menu_set, 12345, "shuffled")
    c = bpd.order_menu(resolver, menu_set, 999, "shuffled")
    assert a == b
    assert set(a) == set(c)


def test_pass_rows_are_excluded_by_default(resolver, detail):
    pos = _position(hand=[TWO_DROP], lands=[SWAMP])
    rec, st = _make(resolver, detail, pos)
    assert rec is None
    assert st.dropped["no_action_recorded"] == 1


def test_ambiguous_multi_cast_turn_is_dropped(resolver, detail):
    pos = _position(hand=[TWO_DROP, BOLT], lands=[SWAMP], casts=[TWO_DROP, BOLT])
    rec, st = _make(resolver, detail, pos)
    assert rec is None
    assert st.dropped["ambiguous_multi_cast"] == 1


def test_land_drop_is_labelled_before_a_same_turn_cast(resolver, detail):
    """17lands records no ordering; the documented heuristic is land-first."""
    pos = _position(hand=[FOREST, TWO_DROP], lands=[SWAMP], lands_played=[FOREST], casts=[TWO_DROP])
    rec, _ = _make(resolver, detail, pos)
    assert rec["meta"]["pick_kind"] == "land_drop"
    assert rec["meta"]["casts_recorded_this_turn"] == 1


def test_trivial_land_drop_is_not_counted_as_strategic():
    only_land = [("Play", FOREST), ("Cast", BOLT), ("Pass", None)]
    two_lands = [("Play", FOREST), ("Play", SWAMP), ("Pass", None)]
    assert bpd.is_nontrivial("land_drop", only_land) is False
    assert bpd.is_nontrivial("land_drop", two_lands) is True
    assert bpd.is_nontrivial("cast", only_land) is True


def test_every_record_carries_17lands_attribution(resolver, detail):
    pos = _position(hand=[FOREST, TWO_DROP], lands=[SWAMP], lands_played=[FOREST])
    rec, _ = _make(resolver, detail, pos)
    assert rec["attribution"] == "17Lands (CC BY 4.0)"
    assert rec["source"] == "17lands-replay_data"


# ---------------------------------------------------------------------------
# End-to-end over a synthetic 17lands-shaped CSV
# ---------------------------------------------------------------------------

_TURN_COLS = [
    "cards_drawn",
    "cards_tutored",
    "lands_played",
    "creatures_cast",
    "non_creatures_cast",
    "user_abilities",
    "eot_user_cards_in_hand",
    "eot_oppo_cards_in_hand",
    "eot_user_lands_in_play",
    "eot_oppo_lands_in_play",
    "eot_user_creatures_in_play",
    "eot_oppo_creatures_in_play",
    "eot_user_non_creatures_in_play",
    "eot_oppo_non_creatures_in_play",
    "eot_user_life",
    "eot_oppo_life",
]


def _write_fixture_csv(path: Path, *, max_turn: int = 6) -> None:
    header = [
        "expansion",
        "rank",
        "on_play",
        "draft_id",
        "match_number",
        "game_number",
        "won",
        "user_game_win_rate_bucket",
        "opening_hand",
        "num_turns",
    ]
    for t in range(1, max_turn + 1):
        for who in ("user", "oppo"):
            header += [f"{who}_turn_{t}_{c}" for c in _TURN_COLS]
    idx = {name: i for i, name in enumerate(header)}

    def blank() -> list[str]:
        return [""] * len(header)

    def row_for(rank: str) -> list[str]:
        row = blank()
        row[idx["expansion"]] = "EOE"
        row[idx["rank"]] = rank
        row[idx["on_play"]] = "True"
        row[idx["draft_id"]] = f"draft-{rank}"
        row[idx["match_number"]] = "1"
        row[idx["game_number"]] = "1"
        row[idx["won"]] = "True"
        row[idx["opening_hand"]] = "|".join(str(g) for g in (FOREST, SWAMP, TWO_DROP, BOLT))
        row[idx["num_turns"]] = "8"
        # T1: play Forest.
        row[idx["user_turn_1_lands_played"]] = str(FOREST)
        row[idx["user_turn_1_eot_user_lands_in_play"]] = str(FOREST)
        row[idx["oppo_turn_1_eot_user_cards_in_hand"]] = f"{SWAMP}|{TWO_DROP}|{BOLT}"
        row[idx["oppo_turn_1_eot_user_lands_in_play"]] = str(FOREST)
        row[idx["oppo_turn_1_eot_user_life"]] = "20.0"
        row[idx["oppo_turn_1_eot_oppo_life"]] = "20.0"
        # T2: play Swamp, cast the two-drop.
        row[idx["user_turn_2_lands_played"]] = str(SWAMP)
        row[idx["user_turn_2_creatures_cast"]] = str(TWO_DROP)
        row[idx["oppo_turn_2_eot_user_cards_in_hand"]] = str(BOLT)
        row[idx["oppo_turn_2_eot_user_lands_in_play"]] = f"{FOREST}|{SWAMP}"
        row[idx["oppo_turn_2_eot_user_creatures_in_play"]] = str(TWO_DROP)
        row[idx["oppo_turn_2_eot_user_life"]] = "20.0"
        row[idx["oppo_turn_2_eot_oppo_life"]] = "17.0"
        # T3: no land, single cast -> a genuine spell decision.
        row[idx["user_turn_3_non_creatures_cast"]] = str(BOLT)
        row[idx["oppo_turn_3_eot_user_cards_in_hand"]] = ""
        row[idx["oppo_turn_3_eot_user_lands_in_play"]] = f"{FOREST}|{SWAMP}"
        # T6 exists in the file but is out of scope (condition 4).
        row[idx["user_turn_6_lands_played"]] = str(FOREST)
        row[idx["oppo_turn_5_eot_user_cards_in_hand"]] = str(FOREST)
        return row

    lines = [",".join(header)]
    lines += [",".join(row_for("mythic")), ",".join(row_for("diamond")), ",".join(row_for("bronze"))]
    path.write_text("\n".join(lines) + "\n")


def test_end_to_end_build(tmp_path, resolver, detail):
    csv_path = tmp_path / "replay_fixture.csv"
    _write_fixture_csv(csv_path)
    out = tmp_path / "play_decisions.jsonl"
    nt = tmp_path / "nontrivial.jsonl"
    cast = tmp_path / "cast.jsonl"

    st = bpd.build(
        resolver,
        detail,
        [csv_path],
        min_rank="diamond",
        max_rows_per_file=100,
        min_turn=1,
        max_turn=5,
        multi_cast_policy="drop",
        include_pass=False,
        menu_order="shuffled",
        oracle_chars=0,
        state_basis="oppo",
        out=out,
        nontrivial_out=nt,
        cast_out=cast,
        max_records=0,
    )

    recs = [json.loads(line) for line in out.read_text().splitlines()]
    # bronze is filtered out; 2 qualifying games x 3 in-scope decisions each.
    assert st.games_read == 2
    assert {r["meta"]["rank"] for r in recs} == {"mythic", "diamond"}
    assert st.emitted == len(recs) == 6
    assert max(r["meta"]["turn"] for r in recs) <= 5, "condition 4: turn cap"
    assert {r["meta"]["pick_kind"] for r in recs} == {"land_drop", "cast"}

    casts = [json.loads(line) for line in cast.read_text().splitlines()]
    assert len(casts) == 2 and all(c["meta"]["pick_kind"] == "cast" for c in casts)
    assert len(nt.read_text().splitlines()) == st.nontrivial

    for r in recs:
        assert r["system"].startswith(AUTOPILOT_SYSTEM_PROMPT)
        pick = json.loads(r["response"])["actions"][0]["pick"]
        numbered = [ln.strip() for ln in r["user"].splitlines() if ln.strip()[:1].isdigit()]
        assert 1 <= pick <= len(numbered)
        assert numbered[pick - 1].startswith(f"{pick}. ")


def test_end_to_end_build_reads_gzip(tmp_path, resolver, detail):
    plain = tmp_path / "fixture.csv"
    _write_fixture_csv(plain)
    gz = tmp_path / "fixture.csv.gz"
    gz.write_bytes(gzip.compress(plain.read_bytes()))
    out = tmp_path / "out.jsonl"
    st = bpd.build(
        resolver,
        detail,
        [gz],
        min_rank="diamond",
        max_rows_per_file=100,
        min_turn=1,
        max_turn=5,
        multi_cast_policy="drop",
        include_pass=False,
        menu_order="shuffled",
        oracle_chars=0,
        state_basis="oppo",
        out=out,
        nontrivial_out=None,
        cast_out=None,
        max_records=0,
    )
    assert st.emitted == 6
