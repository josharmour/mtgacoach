"""Real Pick-Two picks from the 2026-10-07 live draft (PickTwoDraft_FRA, a blue-black pool).

Packs, picks and the model's reasons come from Player.log (Draft.Notify,
EventPlayerDraftMakePick) and standalone.log; card data is the cached FRA set
primer trimmed to these cards (tests/fixtures/fra_pick_two_draft_20261007.json).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from arenamcp.draft_autopick import choose_picks, land_colors, lane_fit, rank_pack
from arenamcp.draft_event import DraftEventDriver
from arenamcp.set_primer import SetCard, SetPrimer

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "fra_pick_two_draft_20261007.json").read_text())
PRIMER = SetPrimer.from_json(json.dumps(FIXTURE["primer"]))
COSTS = {int(k): v for k, v in FIXTURE["mana_costs"].items()}
NAMES = {int(k): v for k, v in FIXTURE["names"].items()}
PICKS = {(pack, pick): grp_ids for pack, pick, grp_ids in FIXTURE["picks"]}
ID = {name: grp_id for grp_id, name in NAMES.items()}

# The model's reasons as logged ("Draft pick P1p2 [...]: [...]").
MODEL_REASONS = {
    "Traxos, Academy Guardian": "Strongest body in the pack: a 1/5 flying vigilant prowess wall for "
    "effectively 2U after casting a noncreature spell like Twinned Vision or Extended Absence. Fits our "
    "blue-based spells lane and adds a needed early-ish creature.",
    "Shipwreck Marsh": "Smooth two-land-check dual that fixes U/B without costing a spell slot; supports "
    "both our existing Extended Absence and the blue core, keeping Dimir/Azorius options open.",
    "Fblthp, Impossibly Lost": "Highest-rated card here and a strong blue body: after any combat damage to "
    "an opponent it draws two cards, refueling for Traxos discounts and prowess-style turns.",
    "Recursive Recruitment": "Tier-1 Dimir Threshold payoff that makes two 2/2 bodies immediately — fixes "
    "our creature deficit — and its flashback plus graveyard bonus fits the UB mill/recursion plan with "
    "Extended Absence.",
    "Cast Away Doubt": "Three-mana draw-two smooths the Dimir Threshold plan and pings each opponent.",
    "Theorix Annex": "On-color U/B dual that fixes mana for our Dimir Threshold core.",
}


def pool_before(key: tuple[int, int]) -> list[int]:
    return [g for k, grp_ids in sorted(PICKS.items()) if k < key for g in grp_ids]


def pack(label: str) -> list[int]:
    return list(FIXTURE["packs"][label])


def rank(label: str, key: tuple[int, int]):
    ids = pack(label)
    return rank_pack(
        ids, pool_before(key), PRIMER, pack_number=key[0], pick_number=key[1], names=NAMES, mana_costs=COSTS
    )


def choose(label: str, key: tuple[int, int]):
    return choose_picks(
        pack(label),
        pool_before(key),
        PRIMER,
        picks_required=2,
        players=4,
        pack_number=key[0],
        pick_number=key[1],
        names=NAMES,
        mana_costs=COSTS,
    )


def driver(recommend=None, label="P2p1", key=(2, 1)) -> DraftEventDriver:
    db = SimpleNamespace(
        get_card=lambda g: SimpleNamespace(
            name=NAMES.get(g, f"Card {g}"), mana_cost=COSTS.get(g, ""), expansion_code="FRA"
        )
    )
    service = SimpleNamespace(get=lambda code: PRIMER, ensure=lambda code: None, quick=lambda code: PRIMER)
    built = DraftEventDriver(
        bridge_fn=lambda: None,
        tracker_fn=lambda: None,
        primer_service=service,
        card_db=db,
        picked_fn=lambda: pool_before(key),
        pick_advisor_fn=(lambda: SimpleNamespace(recommend=recommend)) if recommend else None,
        pack_fn=lambda: {
            "pack_number": key[0],
            "pick_number": key[1],
            "cards": [{"grp_id": g} for g in pack(label)],
        },
    )
    built.run.set_code = "FRA"
    built.run.event_name = "PickTwoDraft_FRA_20260929"
    return built


def model_answer(*names: str):
    def recommend(details, fallback):
        return {
            "reasoning_source": "card_rules",
            "recommendations": [{"grp_id": ID[name], "reason": MODEL_REASONS[name]} for name in names],
            "plan": "Dimir Threshold Mill.",
            "needs": [],
        }

    return recommend


# ---------------------------------------------------------------------------
# Lands are judged by the mana they make for our lane
# ---------------------------------------------------------------------------


def test_land_colors_come_from_rules_text_and_land_types():
    assert land_colors(PRIMER.card(ID["Haunted Ridge"])) == "BR"
    assert land_colors(PRIMER.card(ID["Shipwreck Marsh"])) == "UB"
    room = SetCard(
        106433,
        "Room of Refuge",
        "",
        "C",
        "Land",
        oracle="This land enters tapped. As it enters, choose a color.\n{oT}: Add one mana of the chosen color.",
    )
    assert land_colors(room) == "WUBRG"
    typed = SetCard(1, "Typed Dual", "", "C", "Land - Island Swamp", oracle="")
    assert land_colors(typed) == "UB"
    assert lane_fit(PRIMER.card(ID["Haunted Ridge"]), "UB") == "partial"
    assert lane_fit(PRIMER.card(ID["Theorix Annex"]), "UB") == "fixing"
    assert lane_fit(PRIMER.card(ID["Tam's Resistance"]), "UB", COSTS) == "in"  # {1}{G/U} hybrid


def test_p2p1_off_lane_dual_and_third_pump_spell_are_discounted():
    # Live: "took Tam's Resistance and Haunted Ridge (ranking)" for a UB pool.
    ranking = {pick.name: pick for pick in rank("P2p1", (2, 1))}
    ridge = ranking["Haunted Ridge"]
    assert "taps for only B of lane UB" in ridge.reasons and "land; GIH discounted" in ridge.reasons
    assert not any("in lane" in reason for reason in ridge.reasons)
    assert "fixes lane UB" in ranking["Theorix Annex"].reasons
    assert ranking["Theorix Annex"].score > ridge.score
    assert "2 already in pool" in ranking["Tam's Resistance"].reasons
    picks = [pick.name for pick in choose("P2p1", (2, 1))]
    assert "Haunted Ridge" not in picks


def test_p1p3_still_takes_the_two_best_blue_black_cards():
    assert [pick.name for pick in choose("P1p3", (1, 3))] == [
        "Fblthp, Impossibly Lost",
        "Recursive Recruitment",
    ]


# ---------------------------------------------------------------------------
# Pick-Two: the model's cards are judged one by one
# ---------------------------------------------------------------------------


def test_p2p1_keeps_the_models_acceptable_card_and_replaces_only_the_far_off_one():
    # Live: "Model pick Cast Away Doubt ranks #6 (-0.77) vs ranking floor 0.19;
    # keeping the ranking's pick" threw away the model's whole pair.
    d = driver(model_answer("Cast Away Doubt", "Theorix Annex"))
    ranked = choose("P2p1", (2, 1))
    picks, reasons, source, from_model = d._refine_pick(pack("P2p1"), PRIMER, pool_before((2, 1)), ranked, 2)
    assert ID["Theorix Annex"] in picks and ID["Cast Away Doubt"] not in picks
    assert len(set(picks)) == 2 and from_model == {ID["Theorix Annex"]}
    assert source == "model+ranking"
    replacement = next(g for g in picks if g != ID["Theorix Annex"])
    assert replacement == ranked[0].grp_id  # the ranking's own first choice fills the slot
    assert reasons[ID["Theorix Annex"]].startswith("On-color U/B dual")


def test_p1p3_model_pair_inside_the_tolerance_is_kept_whole():
    d = driver(model_answer("Fblthp, Impossibly Lost", "Recursive Recruitment"), "P1p3", (1, 3))
    picks, _reasons, source, from_model = d._refine_pick(
        pack("P1p3"), PRIMER, pool_before((1, 3)), choose("P1p3", (1, 3)), 2
    )
    assert source == "model" and set(picks) == from_model == {
        ID["Fblthp, Impossibly Lost"],
        ID["Recursive Recruitment"],
    }


def test_single_pick_drafts_still_reject_the_whole_answer():
    d = driver(model_answer("Cast Away Doubt"))
    ranked = choose_picks(pack("P2p1"), pool_before((2, 1)), PRIMER, names=NAMES, mana_costs=COSTS)
    assert d._refine_pick(pack("P2p1"), PRIMER, pool_before((2, 1)), ranked, 1) is None


# ---------------------------------------------------------------------------
# What is said: each card's role in our deck
# ---------------------------------------------------------------------------


def commentary(d, label, key, names, *, model=True):
    chosen = [ID[name] for name in names]
    return d._pick_commentary(
        chosen=chosen,
        names=names,
        pack=pack(label),
        pool=pool_before(key),
        primer=PRIMER,
        pack_number=key[0],
        pick_number=key[1],
        model_reasons={ID[n]: MODEL_REASONS[n] for n in names} if model else {},
        required=2,
        verb="Choosing",
    )


def test_real_picks_are_explained_card_by_card():
    d = driver()
    p1p2 = commentary(d, "P1p2", (1, 2), ["Traxos, Academy Guardian", "Shipwreck Marsh"])
    # Live: "Taking Traxos, Academy Guardian and Shipwreck Marsh: strongest body in the pack."
    assert p1p2 == (
        "Choosing Traxos — fits our blue-based spells lane and adds a needed early-ish creature; "
        "and Shipwreck Marsh — a blue-black dual that keeps our options open."
    )
    p1p3 = commentary(d, "P1p3", (1, 3), ["Fblthp, Impossibly Lost", "Recursive Recruitment"])
    # Live: "Taking Fblthp, Impossibly Lost and Recursive Recruitment: the best card in this pack ..."
    assert p1p3.startswith(
        "Choosing Fblthp — highest-rated card here and a strong blue body; "
        "and Recursive Recruitment — a payoff for Dimir Threshold Mill."
    )
    assert "the deck is Dimir Threshold Mill" in p1p3
    p2p1 = commentary(d, "P2p1", (2, 1), ["Tam's Resistance", "Haunted Ridge"], model=False)
    # Live: "Taking Tam's Resistance and Haunted Ridge: a solid card for our blue-black deck."
    assert "Haunted Ridge — only a black source for our blue-black deck" in p2p1
    assert "Tam's Resistance — a solid card for our blue-black deck" in p2p1


@pytest.mark.parametrize("pick_number, summary", [(6, False), (7, True)])
def test_end_of_pack_summary_fires_on_the_last_pick_two_pick(pick_number, summary):
    # Pack size counted one card per pick, so Pick-Two never reached "last pick".
    d = driver()
    d._narrator._since_story = 0
    d._narrator._lane = "UB"
    last = PICKS[(1, pick_number)]
    text = d._pick_commentary(
        chosen=last,
        names=[NAMES[g] for g in last],
        pack=list(last) + ([] if pick_number == 7 else [ID["Cast Away Doubt"], ID["Tam's Resistance"]]),
        pool=pool_before((1, pick_number)),
        primer=PRIMER,
        pack_number=1,
        pick_number=pick_number,
        model_reasons={},
        required=2,
    )
    assert ("End of pack 1" in text) == summary
