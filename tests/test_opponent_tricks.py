"""Opponent trick model (multi-turn planning spec WP5, phase 1: advisory only).

The table is built from an embedded slice of FRA: real 17Lands game counts
(FRA_PremierDraft card ratings, 2026-10-06), real mana costs from MTGA's card
database, oracle text from tests/strategic_states.CARDS plus the few cards it
lacks (below, collapsed the same way), and FRA's real colour ratings. Board
states are the real recorded ones in tests/strategic_states.py. Nothing reads
~/.arenamcp. Tests pin decision-relevant invariants and the formulas, not tuned
numbers (critique C3).
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
import time
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from tests import strategic_states as S

from arenamcp import opponent_tricks as ot
from arenamcp.set_primer import SetCard, SetPrimer

REPO = Path(__file__).resolve().parents[1]

# Cards the slice needs that tests/strategic_states.CARDS lacks (MTGA card
# database text, Arena's formatting variants collapsed).
EXTRA = {
    "Compel Brutality": {
        "type_line": "Instant",
        "mana_cost": "{1}{G}",
        "oracle_text": "Choose one —\n•Target creature you control deals damage equal to its power to target creature "
        "or planeswalker an opponent controls.\n•Target planeswalker you control deals damage equal to its loyalty to "
        "target creature or planeswalker an opponent controls.",
    },
    "Wrath of the Bloodmane": {
        "type_line": "Instant",
        "mana_cost": "{2}{R}",
        "oracle_text": "This spell costs {o1} less to cast if you control a legendary creature.\nWrath of the "
        "Bloodmane deals 4 damage to target creature or planeswalker.",
    },
    "Konstrari Charm": {
        "type_line": "Instant",
        "mana_cost": "{R}{G}",
        "oracle_text": "Choose one —\n•Konstrari Charm deals 6 damage to target creature with flying.\n•Put two "
        "+1/+1 counters on target creature. It gains trample until end of turn.\n•Add {oCoCoC}.",
    },
    "Ferocity of the Hunt": {
        "type_line": "Enchantment — Aura",
        "mana_cost": "{1}{B/G}",
        "oracle_text": "Flash\nEnchant creature\nEnchanted creature gets +1/+0 and has deathtouch.\nWhen enchanted "
        "creature dies, return that card to the battlefield tapped under its owner's control.",
    },
    "Charge the Sanctum": {
        "type_line": "Instant",
        "mana_cost": "{2}{R/W}",
        "oracle_text": "Choose one —\n•Creatures you control get +2/+0 until end of turn.\n•Target creature gets "
        "+2/+0 and gains first strike until end of turn. Put a +1/+1 counter on it.",
    },
    "Last Gasp": {
        "type_line": "Instant",
        "mana_cost": "{1}{B}",
        "oracle_text": "Target creature gets -3/-3 until end of turn.",
    },
    "Icy Reception": {
        "type_line": "Instant",
        "mana_cost": "{1}{U}",
        "oracle_text": "Choose one —\n•Counter target creature or legendary spell unless its controller pays {o3}.\n"
        "•Target creature gets -5/-0 until end of turn.",
    },
    "Edgar, Moonlit Sovereign": {
        "type_line": "Legendary Creature — Werewolf Noble",
        "mana_cost": "{3}{G}{G}",
        "oracle_text": "Flash\nAt the beginning of your end step, if you didn't cast a spell this turn, put two "
        "+1/+1 counters on Edgar.\n{o4oG}: Put a +1/+1 counter on each creature you control with a +1/+1 counter "
        "on it.",
    },
}

# (grp id, name, 17Lands colour, 17Lands game_count) — FRA_PremierDraft, 2026-10-06.
SLICE = [
    (106352, "Tethermage's Advantage", "G", 902), (106335, "Compel Brutality", "G", 10712),
    (106329, "Wrath of the Bloodmane", "R", 10736), (106375, "Konstrari Charm", "RG", 2761),
    (106314, "Fulminous Forte", "R", 5399), (106305, "Blazing Crescendo", "R", 1582),
    (106226, "Academic Ascent", "W", 1511), (106371, "Ferocity of the Hunt", "BG", 4260),
    (106360, "Charge the Sanctum", "WR", 1286), (106273, "Unsummon", "U", 12246),
    (106399, "Twinned Vision", "UR", 12292), (106350, "Sureshot Sower", "G", 6903),
    (106480, "Proft, Sinister Mastermind", "B", 4137), (106255, "Divining Duelist", "U", 3861),
    (106471, "Yuriko, Hope from the Shadows", "U", 2428), (106396, "Theorix Charm", "UB", 4954),
    (106279, "Break Under Pressure", "B", 6793), (106285, "Last Gasp", "B", 12307),
    (106256, "Icy Reception", "U", 13061), (106250, "Countersculpt", "U", 7703),
    (106283, "Extended Absence", "B", 14488), (106502, "Edgar, Moonlit Sovereign", "G", 2201),
    (106495, "Pia, Determined Rebuilder", "R", 4532), (106306, "Chandra's Emberling", "R", 3182),
    (106508, "Marwyn, the Preserver", "G", 4296), (106344, "Inspired Tethermage", "G", 4761),
    (106388, "Solarium Sentry", "WG", 1036), (106248, "Unflinching Hortimancer", "W", 6054),
    (106381, "Paradox Shaper", "UB", 4420), (106397, "Theorix Metamage", "UB", 5832),
    (106299, "Void Extrapolator", "B", 6556), (106264, "Seasoned Cryomancer", "U", 1050),
    (106265, "Semester Foreseer", "U", 6817), (106325, "Skilled Battlecarver", "R", 6342),
    (106317, "Heartstring Puller", "R", 7112), (106369, "Fatehold Chronologist", "WU", 12418),
    (106459, "Geist of Saint Thalia", "U", 4644), (106252, "Cryotheory Adept", "U", 4480),
    (106281, "Dark Matter Manipulator", "B", 1623), (106506, "Jiang Yanggu, Never Alone", "G", 4310),
    (106259, "Mindseeker Oculus", "U", 17502), (106416, "Keeper of the Quiet Hour", "", 8187),
    (106272, "Undulating Witness", "U", 11528), (106419, "Murmuring Volume", "", 7194),
    (106412, "Archive Arbiter", "", 3603), (106241, "Predictive Preparations", "W", 1831),
    (106387, "Recursive Recruitment", "UB", 7874), (106263, "Protege's Awakening", "U", 11398),
    (106268, "Sphinx's Approach", "U", 2726), (106394, "Tam's Resistance", "UG", 6533),
    (106298, "Theoretical Necromancer", "B", 8269), (106269, "Surveillance Phantasm", "U", 14657),
    (106466, "Tetsuko Umezawa, Fugitive", "U", 3380), (106433, "Room of Refuge", "", 14013),
    (106420, "Dedicated Commons", "", 3234), (106510, "Ruric Thar, Magecrusher", "G", 3813),
    (106574, "Splinter Twin", "R", 139), (106415, "Eye of Jace", "", 1148),
]  # fmt: skip

# FRA 17Lands colour ratings, 2026-10-06 (games): summaries, pairs, pairs + splash, three colours.
COLOR_RATINGS = (
    [{"short_name": key, "is_summary": True, "games": games} for key, games in ((2, 125000), ("2+", 88257), (3, 35380))]
    + [
        {"short_name": key, "is_summary": False, "games": games}
        for key, games in (
            ("WU", 17972), ("UB", 19411), ("BR", 14011), ("RG", 18172), ("WG", 15387),
            ("WB", 6952), ("BG", 7914), ("UG", 4212), ("UR", 12519), ("WR", 8450),
            ("WU+", 12501), ("UB+", 14886), ("BR+", 4843), ("RG+", 10952), ("WG+", 10117),
            ("WB+", 4703), ("BG+", 10583), ("UG+", 6898), ("UR+", 8991), ("WR+", 3783),
            ("WUR", 3230), ("UBG", 5053), ("WBR", 1034), ("URG", 3590), ("WBG", 2605),
            ("WUB", 4795), ("UBR", 3734), ("BRG", 4903), ("WRG", 3900), ("WUG", 2536),
        )
    ]
)  # fmt: skip
PAIR_SHARE = {
    "WU": 0.1438, "WB": 0.0556, "WR": 0.0676, "WG": 0.1231, "UB": 0.1553,
    "UR": 0.1002, "UG": 0.0337, "BR": 0.1121, "BG": 0.0633, "RG": 0.1454,
}  # fmt: skip

# The primer stores Arena's text with its three formatting variants (FRA.json).
ACADEMIC_ASCENT_PRIMER = (
    "Target creature gets +2/+2 and gains flying until end of turn.\nTarget creature gets <nobr>+2/+2</nobr> and "
    "gains flying until end of turn.\nTarget creature gets +2/+2 and gains flying until end of turn.\nEmpower Jace 2."
)
DIVINING_DUELIST_PRIMER = (
    "Flash\nWhen this creature enters, choose one — \n•Tap target creature. \n•Untap target creature. \n•Draw a "
    "card, then discard a card.\nWhen this creature enters, choose one<nobr> —</nobr> \n•<indent=4%>Tap target "
    "creature. </indent>\n•<indent=4%>Untap target creature. </indent>\n•<indent=4%>Draw a card, then discard a "
    "card.</indent>\nWhen this creature enters, choose one — \n•Tap target creature. \n•Untap target creature. "
    "\n•Draw a card, then discard a card."
)


def _info(name: str) -> dict:
    return EXTRA.get(name) or S.CARDS[name]


LOOKUP = {
    grp: {"mana_cost": _info(name).get("mana_cost", ""), "type_line": _info(name)["type_line"]}
    for grp, name, _, _ in SLICE
}
RATINGS = [{"mtga_id": grp, "name": name, "game_count": games} for grp, name, _, games in SLICE]
GRP = {name: grp for grp, name, _, _ in SLICE}


def make_primer(built_at: float | None = None) -> SetPrimer:
    oracles = {"Academic Ascent": ACADEMIC_ASCENT_PRIMER, "Divining Duelist": DIVINING_DUELIST_PRIMER}
    cards = {
        grp: SetCard(
            grp_id=grp,
            name=name,
            colors=colors,
            rarity="C",
            types=_info(name)["type_line"],
            cmc=None,
            oracle=oracles.get(name, _info(name).get("oracle_text", "")),
        )
        for grp, name, colors, _ in SLICE
    }
    return SetPrimer(
        set_code="FRA",
        built_at=time.time() - 60 if built_at is None else built_at,
        pair_stats={pair: {"share": share} for pair, share in PAIR_SHARE.items()},
        cards=cards,
    )


@pytest.fixture(scope="module")
def table() -> ot.TrickTable:
    return ot.build_trick_table("FRA", make_primer(), RATINGS, LOOKUP.get, color_ratings=COLOR_RATINGS)


def opp(iid: int, name: str, *, tapped: bool = False, entered: int = 1, **extra) -> dict:
    return S.card(iid, name, 2, is_tapped=tapped, turn_entered_battlefield=entered, **extra)


def mini(
    *,
    battlefield=(),
    graveyard=(),
    hand: int | None = 2,
    active: int = 1,
    phase: str = "Phase_Main1",
    step: str = "",
    turn: int = 8,
    library: int | None = None,
    pool: dict | None = None,
    revealed: list | None = None,
    deck: int = 40,
    event: str = "PremierDraft_FRA_20260929",
) -> dict:
    """A planner-shape snapshot: we are seat 1, the opponent seat 2."""
    zones: dict = {} if hand is None else {"opponent_hand_count": hand}
    if library is not None:
        zones["opponent_library_count"] = library
    state = {
        "match_id": "test",
        "event_id": event,
        "local_seat_id": 1,
        "opponent_seat_id": 2,
        "deck_cards": [106273] * deck,
        "turn": {
            "turn_number": turn,
            "active_player": active,
            "priority_player": active,
            "phase": phase,
            "step": step,
        },
        "players": [
            {"seat_id": 1, "life_total": 20, "is_local": True, "mana_pool": {}},
            {"seat_id": 2, "life_total": 20, "is_local": False, "mana_pool": dict(pool or {})},
        ],
        "battlefield": list(battlefield),
        "graveyard": list(graveyard),
        "hand": [],
        "zones": zones,
    }
    if revealed is not None:
        state["revealed_cards"] = revealed
    return state


def single_pair_table(cards: list[ot.TrickCard], copies: dict[int, float], key: str = "RG") -> ot.TrickTable:
    return ot.TrickTable(
        set_code="FRA", prior={key: 1.0}, cards={c.grp_id: c for c in cards}, copies={key: dict(copies)}
    )


PUMP = ot.TrickCard(grp_id=1, name="Test Pump", mana_cost="{1}{G}", mv=2, pips=("G",), kinds=("pump",))
GROWTH = ot.TrickCard(grp_id=2, name="Test Growth", mana_cost="{G}", mv=1, pips=("G",), kinds=("pump",))
RG_LANDS = [opp(10, "Forest"), opp(11, "Mountain")]


# --- the table -------------------------------------------------------------------


def test_clean_oracle_collapses_arena_variants_and_expands_costs():
    assert ot.clean_oracle(ACADEMIC_ASCENT_PRIMER) == (
        "Target creature gets +2/+2 and gains flying until end of turn.\nEmpower Jace 2."
    )
    assert ot.split_modes(DIVINING_DUELIST_PRIMER) == [
        "Tap target creature.",
        "Untap target creature.",
        "Draw a card, then discard a card.",
    ]
    assert (
        ot.expand_costs("Flashback—{o1o(U/R)o(U/R)}, Discard a card.")
        == "Flashback—{1}{U/R}{U/R}, Discard a card."
    )
    assert ot.expand_costs("{oT}: Add {oCoCoC}.") == "{T}: Add {C}{C}{C}."


@pytest.mark.parametrize(
    ("name", "kinds", "source"),
    [
        ("Tethermage's Advantage", ("pump", "keyword"), "instant"),
        ("Academic Ascent", ("pump", "keyword"), "instant"),
        ("Compel Brutality", ("removal",), "instant"),  # "bite"
        ("Break Under Pressure", ("removal",), "instant"),  # edict
        ("Konstrari Charm", ("removal", "pump", "keyword"), "instant"),
        ("Theorix Charm", ("removal", "counter"), "instant"),
        ("Icy Reception", ("shrink", "counter"), "instant"),
        ("Unsummon", ("bounce",), "instant"),
        ("Ferocity of the Hunt", ("pump", "keyword"), "flash"),  # flash Aura
        ("Yuriko, Hope from the Shadows", ("shrink", "flash_body"), "flash"),
        ("Divining Duelist", ("tap", "flash_body"), "flash"),
        ("Edgar, Moonlit Sovereign", ("flash_body",), "flash"),  # its own ability is not a hand trick
        ("Proft, Sinister Mastermind", ("removal",), "hand"),  # {B}, Discard this card: -3/-1
        ("Sureshot Sower", ("removal",), "hand"),
    ],
)
def test_instant_speed_cards_and_their_kinds(table, name, kinds, source):
    card = table.cards[GRP[name]]
    assert card.kinds == kinds
    assert card.source == source


def test_hand_abilities_cost_the_ability_not_the_card(table):
    assert (
        table.cards[GRP["Proft, Sinister Mastermind"]].mana_cost,
        table.cards[GRP["Proft, Sinister Mastermind"]].mv,
    ) == ("{B}", 1)
    assert table.cards[GRP["Sureshot Sower"]].mv == 4


def test_draw_spells_sorceries_and_creatures_are_left_out(table):
    for name in (
        "Twinned Vision",
        "Sphinx's Approach",
        "Predictive Preparations",
        "Pia, Determined Rebuilder",
    ):
        assert GRP[name] not in table.cards


def test_hybrid_cards_fit_every_pair_with_either_colour(table):
    # Critique B2: "colours ⊆ pair" gave Twinned Vision-style hybrids one pair only.
    ferocity, charge = GRP["Ferocity of the Hunt"], GRP["Charge the Sanctum"]
    for pair in ("WB", "UB", "BR", "BG", "WG", "UG", "RG"):
        assert table.copies[pair][ferocity] > 0
    for pair in ("WU", "WR", "UR"):
        assert ferocity not in table.copies[pair]
    for pair in ("WU", "WB", "WG", "WR", "UR", "BR", "RG"):
        assert table.copies[pair][charge] > 0
    assert charge not in table.copies["UB"]
    # Without the card database the gold colours stand in for the cost: BG only.
    blind = ot.build_trick_table("FRA", make_primer(), RATINGS, None, color_ratings=COLOR_RATINGS)
    assert [pair for pair in ot.PAIRS if blind.copies[pair].get(ferocity)] == ["BG"]


def test_copies_are_normalised_to_23_nonland_cards_per_deck():
    names = [
        "Unsummon",
        "Compel Brutality",
        "Wrath of the Bloodmane",
        "Last Gasp",
        "Academic Ascent",
        "Konstrari Charm",
    ]
    primer = make_primer()
    primer.cards = {grp: card for grp, card in primer.cards.items() if card.name in names}
    built = ot.build_trick_table("FRA", primer, RATINGS, LOOKUP.get, color_ratings=COLOR_RATINGS)
    for key, row in built.copies.items():
        assert sum(row.values()) == pytest.approx(23.0, abs=1e-3), key


def test_priors_follow_the_colour_ratings():
    priors = ot.hypothesis_priors(PAIR_SHARE, COLOR_RATINGS)
    assert sum(priors.values()) == pytest.approx(1.0)
    pure = sum(v for k, v in priors.items() if len(k) == 2)
    splash = sum(v for k, v in priors.items() if "+" in k)
    three = sum(v for k, v in priors.items() if len(k) == 3)
    total = 125000 + 88257 + 35380
    assert (pure, splash, three) == pytest.approx((125000 / total, 88257 / total, 35380 / total))
    assert priors["UB"] / priors["UG"] == pytest.approx(19411 / 4212)
    default = ot.hypothesis_priors(PAIR_SHARE)
    assert sum(v for k, v in default.items() if len(k) == 2) == pytest.approx(0.48 / 0.96)


def test_table_json_round_trip(table):
    again = ot.TrickTable.from_json(table.to_json())
    assert again == table
    state = deepcopy(S.G3_T10_BLOCKS)
    assert ot.trick_risk(state, again).as_payload() == ot.trick_risk(state, table).as_payload()


# --- the estimate: formulas -------------------------------------------------------


def test_copies_in_hand_use_deck_density_not_the_unseen_pool():
    """Critique A1: lambda = hand * K / 40, not (hand / unseen) * (K - copies seen)."""
    tables = single_pair_table([PUMP], {1: 2.0})
    expected = 1 - math.exp(-3 * 2.0 / 40)
    for library in (30, 20, 10):  # D = library + 3 + 2 public lands <= 42: no shrink
        risk = ot.trick_risk(mini(battlefield=RG_LANDS, hand=3, library=library), tables)
        assert risk.p_hand == pytest.approx(expected, abs=1e-12)
        unseen = library + 3
        assert abs(risk.p_hand - (1 - math.exp(-3 / unseen * 2.0))) > 0.01
    assumed = ot.trick_risk(mini(battlefield=RG_LANDS, hand=3), tables)
    assert assumed.deck_size_assumed and assumed.deck_size == 40
    assert assumed.p_hand == pytest.approx(expected, abs=1e-12)


def test_poisson_combination_is_exact_per_card():
    tables = single_pair_table([PUMP, GROWTH], {1: 0.5, 2: 0.3})
    risk = ot.trick_risk(mini(battlefield=RG_LANDS, hand=4), tables)
    assert risk.p_hand == pytest.approx(1 - math.exp(-4 * 0.5 / 40) * math.exp(-4 * 0.3 / 40), abs=1e-12)
    assert [name for name, _, _ in risk.top] == ["Test Pump", "Test Growth"]
    assert risk.top[0][2] == pytest.approx(4 * 0.5 / 40)


def test_big_decks_shrink_toward_the_generic_density():
    tables = single_pair_table([PUMP, GROWTH], {1: 0.2, 2: 0.1})  # 0.3 pump per 40 cards
    generic = ot.GENERIC_COPIES_PER_40["pump"]

    def p(deck_size: int) -> float:
        return ot.trick_risk(mini(battlefield=RG_LANDS, hand=3), tables, deck_size=deck_size).p_hand

    assert p(40) == pytest.approx(1 - math.exp(-3 * 0.3 / 40))
    assert p(42) == pytest.approx(p(40))
    assert p(68) == pytest.approx(1 - math.exp(-3 * generic / 40))  # w = 1
    mixed = 0.5 * 0.3 + 0.5 * generic  # w = (51 - 42) / 18
    assert p(51) == pytest.approx(1 - math.exp(-3 * mixed / 40))
    from_zone = ot.trick_risk(mini(battlefield=RG_LANDS, hand=3, library=63), tables)
    assert (from_zone.deck_size, from_zone.deck_size_assumed) == (68, False)
    assert from_zone.p_hand == pytest.approx(p(68))


def test_hybrid_public_cards_are_on_colour_for_both_halves(table):
    """G1's opponent showed Paradox Shaper {1}{U/B} and Fatehold Chronologist {1}{W/U} with Islands and
    Mountains: both fit UR, so UR stays the likely pair (critique B2)."""
    posterior = ot.deck_posterior(S.G1_T12, table)
    assert max(posterior, key=posterior.get) == "UR"
    assert posterior["UR"] > 0.5


def test_a_splash_or_third_colour_absorbs_an_off_pair_colour(table):
    """The G3 opponent showed Forests, a Mountain, a Plains and a white sorcery (critique B3)."""
    posterior = ot.deck_posterior(S.G3_T10_BLOCKS, table)
    with_white = sum(p for key, p in posterior.items() if "W" in key)
    assert with_white > 0.4
    assert sum(p for key, p in posterior.items() if {"R", "G"} <= set(key)) > 0.8


# --- the estimate: real states ----------------------------------------------------


def _castable(mana_cost: str, open_mana: list[str]) -> bool:
    """An independent check: mana value fits and each coloured pip finds its own open source."""
    from arenamcp.mulligan_policy import _mana_value, _pips

    pips = sorted(_pips(mana_cost), key=len)
    left = list(open_mana)
    for pip in pips:
        source = next((color for color in left if color in pip), None)
        if source is None:
            return False
        left.remove(source)
    return _mana_value(mana_cost) <= len(open_mana)


def test_worked_example_g3_block(table):
    """Match 37bdbe2d T10 DeclareBlock (Player.log 40409): they hold 3 cards with Forest, Forest and
    Mountain open; Tethermage's Advantage followed. The estimate is the exact formula."""
    state = deepcopy(S.G3_T10_BLOCKS)
    risk = ot.trick_risk(state, table)
    assert risk.known and risk.deck_size_assumed
    assert (risk.open_mana, risk.open_colors, risk.hand) == (3, ("R", "G", "G"), 3)
    # Their turn, blocks declared: no taps, flash blockers, counters or fogs.
    assert risk.relevant == ("removal", "bounce", "pump", "protect", "keyword", "shrink")
    posterior = ot.deck_posterior(state, table)
    expected = 0.0
    for key, post in posterior.items():
        rate = sum(
            3 * k / 40
            for grp, k in table.copies.get(key, {}).items()
            if _castable(table.cards[grp].mana_cost, ["G", "G", "R"])
            and set(table.cards[grp].kinds) & set(risk.relevant)
        )
        expected += post * (1 - math.exp(-rate))
    assert risk.p_hand == pytest.approx(expected, abs=1e-12)
    assert risk.p_any == risk.p_hand
    named = {name for name, _, _ in risk.top}
    assert {"Compel Brutality", "Wrath of the Bloodmane"} <= named
    assert not named & {"Academic Ascent", "Sureshot Sower", "Edgar, Moonlit Sovereign"}  # W shut, 4+ mana
    assert 0.0 < risk.q_by_class["pump"] < risk.p_hand < 0.5
    verdict = risk.verdict_line()
    assert verdict.startswith("Opponent interaction risk") and len(verdict) <= 160
    # Their 68-card deck (library 56 at the block) once WP6 publishes the count.
    assert ot.trick_risk(state, table, deck_size=68).deck_size_assumed is False


def test_tapped_out_and_nothing_castable(table):
    for state in (S.G1_T8, S.G1_T10):
        risk = ot.trick_risk(state, table)
        assert (risk.known, risk.p_any, risk.open_mana) == (True, 0.0, 0)
        assert risk.verdict_line() == "Opponent tapped out: no instant-speed interaction"
    # T12: one Mountain open; FRA has no one-mana red instant.
    risk = ot.trick_risk(S.G1_T12, table)
    assert (risk.open_colors, risk.p_any) == (("R",), 0.0)
    assert (
        risk.verdict_line() == "Opponent has no instant-speed interaction castable with 1 open: play normally"
    )


def test_g1_t14_one_island_open(table):
    risk = ot.trick_risk(S.G1_T14, table)
    assert risk.open_colors == ("U",)
    assert risk.top[0][:2] == ("Unsummon", "bounce")
    assert 0.0 < risk.p_any < 0.1
    assert "low" in risk.verdict_line()


def test_bridge_states(table):
    commons = ot.trick_risk(S.BUG_174855, table)  # Dedicated Commons makes W or R
    assert commons.known and "*" in commons.open_colors
    crowded = ot.trick_risk(S.BUG_180436, table)
    assert crowded.known and crowded.open_mana == 8 and crowded.hand == 1


def test_verdicts_never_name_cards(table):
    names = {name for _, name, _, _ in SLICE} | set(S.CARDS)
    states = [S.G1_T8, S.G1_T12, S.G1_T14, S.G3_T10_BLOCKS, S.BUG_135027, S.BUG_174855, S.BUG_180436]
    for state in states:
        risk = ot.trick_risk(state, table)
        assert risk.known
        verdict = risk.verdict_line()
        assert len(verdict) <= 160
        assert not [name for name in names if name in verdict]
        assert len(risk.ui_line()) <= 220
    assert "Compel Brutality" in ot.trick_risk(S.G3_T10_BLOCKS, table).ui_line()


# --- open mana -------------------------------------------------------------------------


def _elf(iid: int, entered: int, **extra) -> dict:
    card = {
        "instance_id": iid,
        "name": "Test Elf",
        "type_line": "Creature — Elf Druid",
        "oracle_text": "{oT}: Add {oG}.",
        "power": 1,
        "toughness": 1,
        "owner_seat_id": 2,
        "controller_seat_id": 2,
        "is_tapped": False,
        "turn_entered_battlefield": entered,
    }
    card.update(extra)
    return card


def test_open_mana_rules():
    tables = single_pair_table([GROWTH], {2: 1.0})
    tapped = ot.trick_risk(mini(battlefield=[opp(10, "Forest", tapped=True)], hand=2), tables)
    assert (tapped.open_mana, tapped.p_any) == (0, 0.0)
    # Our turn 8: their latest turn began at 7, so an elf from turn 7 is still sick.
    assert ot.trick_risk(mini(battlefield=[_elf(20, 7)]), tables).open_mana == 0
    assert ot.trick_risk(mini(battlefield=[_elf(20, 6)]), tables).open_colors == ("G",)
    # Their turn 8: one from turn 8 is sick, one from turn 6 is not.
    assert ot.trick_risk(mini(battlefield=[_elf(20, 8)], active=2), tables).open_mana == 0
    assert ot.trick_risk(mini(battlefield=[_elf(20, 6)], active=2), tables).open_mana == 1
    assert ot.trick_risk(mini(battlefield=[_elf(20, 7, keywords=["haste"])]), tables).open_mana == 1
    # color_production: bridge colour names, or the log's ManaColor digits.
    digits = {"instance_id": 30, "name": "Test Land", "type_line": "Land", "color_production": ["5"],
              "owner_seat_id": 2, "controller_seat_id": 2, "is_tapped": False}  # fmt: skip
    assert ot.trick_risk(mini(battlefield=[digits]), tables).open_colors == ("G",)
    commons = S.bridge_card(31, 106420, "Dedicated Commons", 2, "battlefield")
    assert ot.trick_risk(mini(battlefield=[commons]), tables).open_colors == ("*",)
    volume = opp(32, "Murmuring Volume")
    assert ot.trick_risk(mini(battlefield=[volume]), tables).open_colors == ("*",)
    pooled = ot.trick_risk(mini(pool={"ManaColor_Green": 1}), tables)
    assert pooled.open_colors == ("G",) and pooled.p_hand > 0


@pytest.mark.parametrize(
    ("active", "phase", "step", "absent"),
    [
        (2, "Phase_Combat", "Step_DeclareBlock", {"tap", "flash_body", "counter", "fog"}),
        (2, "Combat", "DeclareBlock", {"tap", "flash_body", "counter", "fog"}),  # bridge names
        (2, "Main1", "None", {"flash_body", "counter", "fog"}),
        (1, "Phase_Main1", "", set()),
        (1, "Combat", "DeclareAttack", {"tap"}),
        (1, "Phase_Combat", "Step_DeclareBlock", {"tap", "flash_body"}),
    ],
)
def test_relevant_kinds_follow_the_timing(active, phase, step, absent):
    kinds = ot.relevant_kinds(mini(active=active, phase=phase, step=step))
    assert not kinds & absent
    assert {"removal", "bounce", "pump", "protect", "keyword", "shrink"} <= kinds
    if active == 1:
        assert kinds == ot.SCORED_KINDS - absent


# --- visible options -----------------------------------------------------------------------


def test_visible_options_are_certain(table):
    lands = [opp(10, "Mountain"), opp(11, "Forest")]
    carver = opp(12, "Skilled Battlecarver", tapped=True)  # no {T} in the cost
    risk = ot.trick_risk(mini(battlefield=[*lands, carver]), table)
    assert risk.p_any == 1.0 and risk.q_by_class["pump"] == 1.0
    assert risk.certain[0]["name"] == "Skilled Battlecarver" and risk.certain[0]["where"] == "on board"
    verdict = risk.verdict_line()
    assert verdict.startswith("Opponent has a visible instant-speed pump (on board, 2 open)")
    assert "Battlecarver" not in verdict

    # Not certain: Pia's {5}{R} with 3 open, Room of Refuge (sorcery speed), a draw flashback,
    # Cryotheory Adept's graveyard ability (sorcery speed).
    three = [opp(10, "Mountain"), opp(11, "Forest"), opp(13, "Island")]
    quiet = mini(
        battlefield=[*three, opp(14, "Pia, Determined Rebuilder"), opp(15, "Room of Refuge")],
        graveyard=[S.card(40, "Twinned Vision", 2), S.card(41, "Cryotheory Adept", 2)],
    )
    assert ot.trick_risk(quiet, table).certain == []

    flashback = {"instance_id": 42, "name": "Test Flashback Pump", "type_line": "Instant", "mana_cost": "{1}{G}",
                 "oracle_text": "Target creature gets +2/+2 until end of turn.\nFlashback {o2oG}",
                 "owner_seat_id": 2, "controller_seat_id": 2}  # fmt: skip
    found = ot.trick_risk(mini(battlefield=three, graveyard=[flashback]), table)
    assert [(c["name"], c["where"], c["cost"]) for c in found.certain] == [
        ("Test Flashback Pump", "graveyard", "{2}{G}")
    ]


def test_revealed_cards_in_hand_are_certain(table):
    """WP6 shape: revealed_cards = [{instance_id, grp_id, name, zone}] still in their hand."""
    revealed = [{"instance_id": 500, "grp_id": GRP["Tethermage's Advantage"], "name": "Tethermage's Advantage",
                 "zone": "hand"}]  # fmt: skip
    risk = ot.trick_risk(mini(battlefield=[opp(10, "Forest")], hand=2, revealed=revealed), table)
    assert risk.hand_unknown == 1
    assert [(c["name"], c["where"]) for c in risk.certain] == [("Tethermage's Advantage", "revealed in hand")]
    assert risk.p_any == 1.0


# --- scope and failure ------------------------------------------------------------------------


def test_unknown_outside_scope(table):
    state = deepcopy(S.G3_T10_BLOCKS)
    assert not ot.trick_risk(state, None).known
    constructed = mini(battlefield=RG_LANDS, deck=60, event="Ladder")
    assert not ot.trick_risk(constructed, table).known
    assert not ot.trick_risk(mini(battlefield=RG_LANDS, hand=None), table).known  # hand count missing
    for broken in (None, {}, {"players": "x"}, {"turn": 3, "zones": []}):
        risk = ot.trick_risk(broken, table)
        assert not risk.known and risk.verdict_line() == "" and risk.ui_line() == ""
    bad = ot.TrickTable(set_code="FRA", prior={"RG": 1.0}, cards={1: PUMP}, copies={"RG": {1: "x"}})
    assert not ot.trick_risk(mini(battlefield=RG_LANDS), bad).known  # an exception never escapes


def test_a_life_total_of_25_does_not_end_limited(table):
    # detect_format_profile calls any game with a life total >= 25 Brawl.
    state = deepcopy(S.G3_T10_BLOCKS)
    state["players"][0]["life_total"] = 26
    assert ot.trick_risk(state, table).known


def test_payload_is_json_safe_and_deterministic(table):
    first = ot.trick_risk(deepcopy(S.G3_T10_BLOCKS), table).as_payload()
    second = ot.trick_risk(deepcopy(S.G3_T10_BLOCKS), table).as_payload()
    assert first == second
    assert json.loads(json.dumps(first)) == first
    assert first["ui_line"] and first["verdict"]


def test_latency(table):
    states = [deepcopy(S.G3_T10_BLOCKS), deepcopy(S.BUG_180436)]
    for state in states:
        ot.trick_risk(state, table)
    started = time.perf_counter()
    for _ in range(20):
        for state in states:
            ot.trick_risk(state, table)
    assert (time.perf_counter() - started) / 40 < 0.010


# --- set code, telemetry, service --------------------------------------------------------------


def test_resolve_set_code(table):
    assert ot.resolve_set_code({"event_id": "PremierDraft_FRA_20260929"}) == "FRA"
    assert ot.resolve_set_code({"format_name": "Sealed FRA 20260929"}) == "FRA"
    # bug_20261006_185403: event_id empty after a coach restart (critique B6); our deck still says FRA.
    restarted = deepcopy(S.G3_T10_BLOCKS)
    restarted["event_id"] = restarted["format_name"] = ""
    assert ot.resolve_set_code(restarted, tables=[table]) == "FRA"
    assert ot.resolve_set_code(restarted) == ""  # memory only, no table: unknown
    lookup = {106273: {"expansion_code": "FRA", "type_line": "Instant"}, 96191: {"expansion_code": "FIN",
              "type_line": "Basic Land — Forest"}}  # fmt: skip
    basics = {"deck_cards": [96191] * 17 + [106273] * 2}
    assert ot.resolve_set_code(basics, card_lookup=lookup.get) == "FRA"  # FRA's basics carry FIN


def test_observed_tricks():
    before = deepcopy(S.G3_T10_BLOCKS)
    cast = deepcopy(before)
    trick = S.card(320, "Tethermage's Advantage", 2)
    cast["stack"] = [trick]
    assert ot.observed_tricks(before, cast) == ["Tethermage's Advantage"]
    resolved = deepcopy(cast)
    resolved["stack"] = []
    resolved["graveyard"] = [*resolved["graveyard"], S.card(321, "Tethermage's Advantage", 2)]
    assert ot.observed_tricks(cast, resolved) == []
    assert ot.observed_tricks(before, before) == []


def test_service_builds_off_thread_and_caches(tmp_path):
    release = threading.Event()
    calls: list[str] = []

    def ratings(code: str) -> list[dict]:
        calls.append(code)
        release.wait(5)
        return RATINGS

    primer = make_primer()
    service = ot.TrickTableService(
        primer_fn=lambda code: primer,
        ratings_fn=ratings,
        color_ratings_fn=lambda code: COLOR_RATINGS,
        card_lookup=LOOKUP.get,
        cache_dir=tmp_path,
    )
    started = time.perf_counter()
    service.ensure("fra")
    service.ensure("FRA")  # already building: no second thread
    assert time.perf_counter() - started < 0.5
    assert service.get("FRA") is None
    release.set()
    built = service.wait("FRA", 10)
    assert built is not None and built.cards and calls == ["FRA"]
    assert (tmp_path / "FRA.json").exists()
    assert service.get_for_state(S.G3_T10_BLOCKS) is built

    def refuse(code: str) -> list[dict]:
        raise AssertionError("must load from disk")

    quiet = {"color_ratings_fn": lambda code: None, "card_lookup": LOOKUP.get}
    cached = ot.TrickTableService(
        primer_fn=lambda code: primer, ratings_fn=refuse, cache_dir=tmp_path, **quiet
    )
    assert cached.build_now("FRA") == built
    # A newer primer makes the cached table stale.
    newer = replace(primer, built_at=primer.built_at + 3600)
    rebuilt: list[str] = []
    fresh = ot.TrickTableService(
        primer_fn=lambda code: newer,
        ratings_fn=lambda code: rebuilt.append(code) or RATINGS,
        cache_dir=tmp_path,
        **quiet,
    )
    assert fresh.build_now("FRA") is not None and rebuilt == ["FRA"]
    # Missing data: no table, no exception, and get() stays memory-only.
    empty = ot.TrickTableService(
        primer_fn=lambda code: None, ratings_fn=refuse, cache_dir=tmp_path / "none", **quiet
    )
    assert empty.build_now("XYZ") is None
    other = ot.TrickTableService(cache_dir=tmp_path / "other", **quiet)
    other.register(built)
    assert other.get("fra") is built


def test_no_llm_or_network_on_the_decision_path(tmp_path, table):
    """arenamcp/__init__ imports the coach (and so arenamcp.backends), so the check loads the package
    without it: importing the module and estimating a real state must not pull either in."""
    path = tmp_path / "table.json"
    path.write_text(table.to_json(), encoding="utf-8")
    code = f"""
import sys, types
pkg = types.ModuleType("arenamcp"); pkg.__path__ = [{str(REPO / "src" / "arenamcp")!r}]
sys.modules["arenamcp"] = pkg
sys.path.insert(0, {str(REPO)!r})
import arenamcp.opponent_tricks as ot
from tests import strategic_states as S
table = ot.TrickTable.from_json(open({str(path)!r}, encoding="utf-8").read())
assert ot.trick_risk(S.G3_T10_BLOCKS, table).known
assert ot.TrickTableService(cache_dir=None).get("FRA") is None
bad = sorted(m for m in sys.modules if m.startswith("arenamcp.backends") or m in ("requests", "arenamcp.coach"))
print(bad)
sys.exit(1 if bad else 0)
"""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO, env=env, timeout=120
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --- 2026-10-07 review regressions ------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "kinds"),
    [
        ("Multiply by Zero", ("removal",)),  # base power and toughness 0/0
        ("Clash of Elements", ("removal",)),  # tucks any nonland permanent
        ("Rise of the Deathbringer", ("removal",)),  # "All creatures get -3/-3"
        ("Perfected Theory", ("pump", "shrink")),  # base 1/1 or base 4/5
        ("Flourishing Grapple", ("removal",)),  # a bite worded "to that permanent"
    ],
)
def test_more_fra_instants_and_their_kinds(name, kinds):
    info = S.CARDS[name]
    entry = ot._trick_entry(1, name, info["type_line"], info["oracle_text"], info["mana_cost"])
    assert entry is not None and entry.kinds == kinds and entry.source == "instant"


@pytest.mark.parametrize("name", ["Way of the Warlord", "Avatar of Burgeoning Echoes", "Loot, the Anomaly"])
def test_granted_loyalty_and_self_or_conditional_abilities_are_not_certain(table, name):
    board = [S.card(50, name, 2, is_tapped=False, turn_entered_battlefield=1), opp(10, "Plains", tapped=True)]
    risk = ot.trick_risk(mini(battlefield=board, hand=0), table)
    assert risk.certain == [] and risk.p_any == 0.0


def test_loot_is_still_no_trick_once_its_threshold_is_met(table):
    # Seven cards in their graveyard and another creature: Loot only shrinks itself.
    board = [S.card(50, "Loot, the Anomaly", 2, turn_entered_battlefield=1), opp(51, "Cadet")]
    graveyard = [S.card(60 + i, "Unsummon", 2) for i in range(7)]
    risk = ot.trick_risk(mini(battlefield=board, graveyard=graveyard, hand=0), table)
    assert risk.certain == []


def test_restricted_mana_does_not_cast_spells_from_hand(table):
    blocks = {"active": 2, "phase": "Phase_Combat", "step": "Step_DeclareBlock"}
    forest = ot.trick_risk(mini(battlefield=[opp(10, "Forest")], hand=3, **blocks), table)
    crafter = S.card(20, "Heartwood Crafter", 2, is_tapped=False, turn_entered_battlefield=1)
    both = ot.trick_risk(mini(battlefield=[opp(10, "Forest"), crafter], hand=3, **blocks), table)
    assert forest.open_mana == both.open_mana == 1
    assert "Compel Brutality" not in [name for name, _kind, _copies in both.top]
    memorial = S.card(21, "Gideon's Memorial", 2, is_tapped=False, turn_entered_battlefield=1)
    assert (
        ot.trick_risk(mini(battlefield=[opp(10, "Forest"), memorial], hand=3, **blocks), table).open_mana == 1
    )


def test_a_modes_target_restriction_needs_a_matching_creature_of_ours(table):
    entry = table.cards[GRP["Sureshot Sower"]]
    assert [mode.target for mode in entry.modes] == ["flying"]
    assert [mode.target for mode in table.cards[GRP["Konstrari Charm"]].modes][0] == "flying"
    lands = [opp(10, "Forest"), opp(11, "Forest"), opp(12, "Mountain"), opp(13, "Mountain")]
    attack = {"phase": "Phase_Combat", "step": "Step_DeclareAttack", "hand": 3}
    grounded = [S.card(70, "Theorix Metamage", 1, is_tapped=False, turn_entered_battlefield=1)]
    flier = [*grounded, S.card(71, "Fatehold Chronologist", 1, is_tapped=False, turn_entered_battlefield=1)]
    low = ot.trick_risk(mini(battlefield=lands + grounded, **attack), table)
    high = ot.trick_risk(mini(battlefield=lands + flier, **attack), table)
    assert "Sureshot Sower" not in [name for name, _kind, _copies in low.top]
    assert "Sureshot Sower" in [name for name, _kind, _copies in high.top]
    assert high.p_hand > low.p_hand
    burn = "Essence Burn deals 5 damage to target black or green creature or planeswalker."
    assert ot.mode_target(burn) == "colors:BG"
    assert ot.mode_target("Destroy target creature or planeswalker that's blue or red.") == "colors:RU"
    assert ot.mode_target("It deals 4 damage to target attacking or blocking creature.") == "combat"


def test_set_code_resolution_runs_once_per_match_and_never_outside_limited():
    lookups: list[int] = []

    def lookup(grp: int) -> dict:
        lookups.append(grp)
        return {"expansion_code": "", "type_line": "Instant"}

    service = ot.TrickTableService(
        primer_fn=lambda code: None,
        ratings_fn=lambda code: [],
        color_ratings_fn=lambda code: None,
        card_lookup=lookup,
    )
    lost = {"event_id": "", "match_id": "m1", "deck_cards": list(range(1000, 1040)), "players": [],
            "local_seat_id": 1, "opponent_seat_id": 2}  # fmt: skip
    for _ in range(50):
        service.ensure_for_state(lost)
    if service._resolver is not None:
        service._resolver.join(5)
    for _ in range(10):
        service.ensure_for_state(lost)  # failed recently: no new resolver
    assert len(lookups) == 40  # one vote over the 40 deck cards
    lookups.clear()
    ladder = {"event_id": "Ladder", "match_id": "m2", "deck_cards": list(range(2000, 2060))}
    cube = {"event_id": "Cube_Draft_20261001", "match_id": "m3", "deck_cards": list(range(3000, 3040))}
    for _ in range(5):
        service.ensure_for_state(ladder)
        service.ensure_for_state(cube)
    assert lookups == [] and service._building == {}
