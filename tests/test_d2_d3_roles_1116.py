"""Focused regression and contrary-control tests for Batch 2 (D2 + D3)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from arenamcp.card_facts.limited_roles import archetype_role, set_record, with_role
from arenamcp.draft_advisor import DraftAdvisor
from arenamcp.draft_autopick import rank_pack
from arenamcp.draft_eval import evaluate_pack as eval_pack_weighted
from arenamcp.draft_guidance import CardData, FormatContext, evaluate_pack, normalize_card
from arenamcp.limited_deck import (
    _build_candidate,
    _fill_role_slots,
    deck_quality,
    fallback_deck,
)
from arenamcp.set_primer import SetCard, SetPrimer

FOOD = {
    "grp_id": 91698,
    "name": "Bakersbane Duo",
    "mana_cost": "{1}{G}",
    "type_line": "Creature — Squirrel Raccoon",
    "oracle_text": "When this creature enters, create a Food token.\nWhenever you expend 4, this creature gets +1/+1 until end of turn.",
}
DAGGER = {
    "grp_id": 91625,
    "name": "Daggerfang Duo",
    "mana_cost": "{2}{B}",
    "type_line": "Creature — Rat Squirrel",
    "oracle_text": "When this creature enters, mill two cards. You may forage. If you do, create a Food token.",
}
PAYOFF = {
    "grp_id": 91737,
    "name": "Treetop Sentries",
    "mana_cost": "{3}{G}",
    "type_line": "Creature — Squirrel Archer",
    "oracle_text": "Reach\nWhen this creature enters, you may forage. If you do, draw a card.",
}
BOUNCE = {
    "grp_id": 106643,
    "name": "Interstellar Effigy",
    "mana_cost": "{4}{U}",
    "type_line": "Artifact Creature — Construct",
    "oracle_text": "When this creature enters, return target creature an opponent controls to its owner's hand.",
}
GENESIS = {
    "grp_id": 106728,
    "name": "Genesis Effect",
    "mana_cost": "{4}{R}",
    "type_line": "Instant",
    "oracle_text": "Genesis Effect deals 6 damage to target creature. Destroy target land you control.",
}


def _filler(i: int, mana: str = "{1}{B}", *, gih: float = 0.55) -> dict:
    return {
        "grp_id": i,
        "name": f"Filler {i}",
        "mana_cost": mana,
        "type_line": "Creature — Squirrel",
        "oracle_text": "",
        "gih_wr": gih,
    }


def _removal(i: int, mana: str = "{1}{B}", *, gih: float = 0.58) -> dict:
    return {
        "grp_id": i,
        "name": f"Removal {i}",
        "mana_cost": mana,
        "type_line": "Instant",
        "oracle_text": "Destroy target creature.",
        "gih_wr": gih,
    }


def test_limited_roles_set_record_and_archetype_role_controls():
    blb = set_record("blb")
    assert blb is not None
    assert blb["set_code"] == "blb"
    assert blb["mechanics"] == ["Forage"]
    assert blb["archetypes"] == {"BG": "Forage"}
    blb["archetypes"]["BG"] = "mutated"
    assert set_record("BLB")["archetypes"]["BG"] == "Forage"

    trk = set_record("TRK")
    assert trk is not None and len(trk["archetypes"]) == 10 and "Station" in trk["mechanics"]
    assert set_record("FRA") is None
    assert set_record("") is None

    payoff = with_role(PAYOFF, "BLB")["limited_role"]
    assert archetype_role(payoff, "GB")["theme"] == "Forage"
    assert archetype_role(payoff, "UR") is None
    assert archetype_role(payoff, "G") is None


def test_d2_normalize_card_folds_reviewed_removal_and_tempo_with_controls():
    # Conditional tempo bounce must NOT acquire the "removal" tag.
    bounce_card = normalize_card(with_role(BOUNCE, "TRK"))
    assert "removal" not in bounce_card.tags

    # Unreviewed card keeps regex-derived baseline tag.
    unreviewed_kill = normalize_card(_removal(901))
    assert "removal" in unreviewed_kill.tags

    # Real reviewed TRK conditional removal gains the "removal" tag even if text is opaque.
    opaque_genesis = {**GENESIS, "oracle_text": "Opaque custom effect."}
    # Rebind requires original oracle text, so real print keeps "removal" and unreviewed opaque loses it.
    assert "removal" in normalize_card(with_role(GENESIS, "TRK")).tags
    assert "removal" not in normalize_card(with_role(opaque_genesis, "TRK")).tags


def test_d2_draft_guidance_surfaces_set_archetypes_roles_removal_and_filler():
    food = with_role(FOOD, "BLB")
    filler_bg = lambda i, mana="{1}{B}": _filler(i, mana, gih=0.55)  # noqa: E731
    bg_pool = [
        normalize_card(filler_bg(1, "{1}{B}")),
        normalize_card(filler_bg(2, "{2}{B}")),
        normalize_card(filler_bg(3, "{1}{G}")),
        normalize_card(filler_bg(4, "{2}{G}")),
        normalize_card({**food, "gih_wr": 0.56}),
    ]
    fmt = FormatContext(mean_gih_wr_pct=54.0, std_gih_wr_pct=4.0)

    # Zero-role control vs BLB enabler + set theme.
    no_role_pack = [normalize_card({**FOOD, "gih_wr": 0.55})]
    with_role_pack = [normalize_card({**food, "gih_wr": 0.55})]
    res_off = evaluate_pack(no_role_pack, [normalize_card(filler_bg(i)) for i in range(1, 5)], 5, 2, fmt=fmt)[
        0
    ]
    res_on = evaluate_pack(with_role_pack, bg_pool, 5, 2, fmt=fmt)[0]
    assert "Forage" in "; ".join(res_on.reasons)
    assert "BG Forage enabler" in res_on.reasons
    assert "Forage" not in "; ".join(res_off.reasons)

    # Payoff with >=2 non-partner reviewed enablers gets boosted vs payoff with 0 enablers in pack 2.
    enabler_role = {
        "set_code": "blb",
        "roles": ["enabler"],
        "removal": "unknown",
        "archetypes": [{"colors": "BG", "theme": "Forage", "role": "enabler", "partners": []}],
    }
    payoff_role = {
        "set_code": "blb",
        "roles": ["payoff"],
        "removal": "unknown",
        "archetypes": [{"colors": "BG", "theme": "Forage", "role": "payoff", "partners": []}],
    }
    rem_role = {"set_code": "blb", "roles": [], "removal": "unconditional", "archetypes": []}
    filler_role = {"set_code": "blb", "roles": ["filler"], "removal": "none", "archetypes": []}
    fin_role = {"set_code": "blb", "roles": ["finisher"], "removal": "none", "archetypes": []}

    p_card = CardData(
        grp_id=50,
        name="Test Payoff",
        colors=("G",),
        mana_cost="{3}{G}",
        cmc=4.0,
        types=("Creature",),
        gih_wr_pct=55.0,
        limited_role=payoff_role,
    )
    r_card = CardData(
        grp_id=51,
        name="Test Uncond",
        colors=("B",),
        mana_cost="{1}{B}",
        cmc=2.0,
        types=("Instant",),
        gih_wr_pct=55.0,
        limited_role=rem_role,
    )
    f_card = CardData(
        grp_id=52,
        name="Test Filler",
        colors=("B",),
        mana_cost="{2}{B}",
        cmc=3.0,
        types=("Creature",),
        gih_wr_pct=55.0,
        limited_role=filler_role,
    )
    fin_card = CardData(
        grp_id=53,
        name="Test Finisher",
        colors=("G",),
        mana_cost="{5}{G}",
        cmc=6.0,
        types=("Creature",),
        gih_wr_pct=55.0,
        limited_role=fin_role,
    )

    pool_0 = [
        CardData(
            grp_id=i,
            name=f"C{i}",
            colors=("B",) if i % 2 else ("G",),
            mana_cost="{2}",
            cmc=2.0,
            types=("Creature",),
            gih_wr_pct=55.0,
        )
        for i in range(1, 5)
    ]
    pool_2 = [
        *pool_0,
        CardData(
            grp_id=10,
            name="En1",
            colors=("B",),
            mana_cost="{1}{B}",
            cmc=2.0,
            types=("Creature",),
            limited_role=enabler_role,
        ),
        CardData(
            grp_id=11,
            name="En2",
            colors=("G",),
            mana_cost="{1}{G}",
            cmc=2.0,
            types=("Creature",),
            limited_role=enabler_role,
        ),
    ]

    g_0 = evaluate_pack([p_card, r_card, f_card, fin_card], pool_0, 3, 2, fmt=fmt)
    g_2 = evaluate_pack([p_card], pool_2, 3, 2, fmt=fmt)
    by_id_0 = {g.grp_id: g for g in g_0}
    assert by_id_0[50].score < g_2[0].score
    assert any("no reviewed enablers yet" in r for r in by_id_0[50].reasons)
    assert any("2 enablers in pool" in r for r in g_2[0].reasons)
    assert "Unconditional removal" in by_id_0[51].reasons
    assert "Reviewed curve filler" in by_id_0[52].reasons
    assert "Reviewed finisher — pool wants a closer" in by_id_0[53].reasons


def test_d2_draft_eval_and_autopick_surface_set_archetypes_and_removal():
    rows = {
        FOOD["grp_id"]: {**FOOD, "colors": ["G"], "rarity": "common"},
        PAYOFF["grp_id"]: {**PAYOFF, "colors": ["G"], "rarity": "common"},
        401: {**_filler(401, "{1}{B}"), "colors": ["B"], "rarity": "common"},
        402: {**_filler(402, "{2}{B}"), "colors": ["B"], "rarity": "common"},
        403: {**_filler(403, "{1}{G}"), "colors": ["G"], "rarity": "common"},
        404: {**_filler(404, "{2}{B}"), "colors": ["B"], "rarity": "common"},
    }
    scry = SimpleNamespace(get_card_by_arena_id=lambda gid: SimpleNamespace(**rows[gid]))
    stats = SimpleNamespace(
        get_draft_rating=lambda n, s: SimpleNamespace(gih_wr=0.56, alsa=4.0, iwd=0.01),
        get_color_pair_stats=lambda s: {"BG": SimpleNamespace(win_rate=0.58, games=200)},
    )
    pool_ids = [401, 402, 403, 404]
    ev_on = eval_pack_weighted(
        [PAYOFF["grp_id"]], pool_ids, "BLB", scry, draft_stats=stats, event_name="PremierDraft_BLB"
    )
    ev_off = eval_pack_weighted(
        [PAYOFF["grp_id"]], pool_ids, "BLB", scry, draft_stats=stats, event_name="PremierDraft_FRA"
    )
    assert "fits BG (Forage)" in ev_on[0].all_reasons
    assert "fits BG" in ev_off[0].all_reasons
    assert "fits BG (Forage)" not in ev_off[0].all_reasons

    # draft_autopick rank_pack checks enabler/payoff/unconditional removal vs missing roles.
    primer = SetPrimer(
        set_code="BLB",
        cards={
            1: SetCard(1, "B1", "B", "common", "Creature", 2.0, gih_wr=0.55),
            2: SetCard(2, "B2", "B", "common", "Creature", 2.0, gih_wr=0.55),
            3: SetCard(3, "G1", "G", "common", "Creature", 2.0, gih_wr=0.55),
            4: SetCard(4, "G2", "G", "common", "Creature", 2.0, gih_wr=0.55),
            5: SetCard(5, "Payoff", "G", "common", "Creature", 4.0, gih_wr=0.56),
            6: SetCard(6, "Removal", "B", "common", "Instant", 2.0, gih_wr=0.56),
        },
        pair_stats={"BG": {"win_rate": 0.57, "games": 200}},
    )
    payoff_role = {
        "set_code": "blb",
        "name": "Payoff",
        "roles": ["payoff"],
        "removal": "unknown",
        "archetypes": [{"colors": "BG", "theme": "Forage", "role": "payoff", "partners": []}],
    }
    en_role = {
        "set_code": "blb",
        "name": "En",
        "roles": ["enabler"],
        "removal": "unknown",
        "archetypes": [{"colors": "BG", "theme": "Forage", "role": "enabler", "partners": []}],
    }
    uncond_role = {
        "set_code": "blb",
        "name": "Removal",
        "roles": [],
        "removal": "unconditional",
        "archetypes": [],
    }
    s_off = {r.grp_id: r for r in rank_pack([5, 6], [1, 2, 3, 4], primer, pack_number=2, pick_number=2)}
    s_no_en = {
        r.grp_id: r
        for r in rank_pack(
            [5, 6],
            [1, 2, 3, 4],
            primer,
            pack_number=2,
            pick_number=2,
            limited_roles={5: payoff_role, 6: uncond_role},
        )
    }
    s_with_en = {
        r.grp_id: r
        for r in rank_pack(
            [5, 6],
            [1, 2, 3, 4],
            primer,
            pack_number=2,
            pick_number=2,
            limited_roles={1: en_role, 3: en_role, 5: payoff_role, 6: uncond_role},
        )
    }
    assert s_no_en[5].score < s_off[5].score
    assert s_with_en[5].score > s_off[5].score
    assert s_no_en[6].score > s_off[6].score
    assert "unconditional removal" in s_no_en[6].reasons


def test_d3_orphan_payoff_swapped_out_with_cut_reason_and_supported_control():
    payoff = {**PAYOFF, "gih_wr": 0.56}  # strong enough to initially make top 23, < BOMB_GIH (61.0)
    core = [_removal(2000 + i, "{1}{B}", gih=0.58) for i in range(4)] + [
        _filler(3000 + i, "{1}{B}" if i % 2 == 0 else "{1}{G}", gih=0.55) for i in range(21)
    ]
    pool_orphan = [payoff, *core]

    blb_orphan = fallback_deck([with_role(c, "BLB") for c in pool_orphan])
    kept_ids = {entry["grp_id"] for entry in blb_orphan["main_deck"]}
    assert PAYOFF["grp_id"] not in kept_ids
    cut_map = {entry["grp_id"]: entry["reason"] for entry in blb_orphan["cuts"]}
    assert cut_map[PAYOFF["grp_id"]] == "payoff lacks enablers in this pool"
    assert blb_orphan["limited_checks"]["orphan_payoffs"] == []
    assert blb_orphan["limited_checks"]["set_archetype"] == "Forage"
    assert blb_orphan["limited_checks"]["set_mechanics"] == ["Forage"]
    assert blb_orphan["quality"]["archetype"] == "Forage"
    assert deck_quality([with_role(FOOD, "BLB"), *core[:22]], ("B", "G"))["archetype"] == "Forage"

    # Contrary control A: same payoff WITH Bakersbane Duo enabler in pool stays in main deck.
    food = {**FOOD, "gih_wr": 0.57}
    pool_supported = [payoff, food, *core[1:]]
    blb_supported = fallback_deck([with_role(c, "BLB") for c in pool_supported])
    supported_ids = {entry["grp_id"] for entry in blb_supported["main_deck"]}
    assert PAYOFF["grp_id"] in supported_ids
    assert FOOD["grp_id"] in supported_ids
    assert blb_supported["limited_checks"]["orphan_payoffs"] == []
    assert any("Forage payoff with Bakersbane Duo" in p for p in blb_supported["limited_checks"]["payoffs"])

    # Contrary control B: roles absent (FRA) leaves PAYOFF inside the 40.
    fra_control = fallback_deck([with_role(c, "FRA") for c in pool_orphan])
    assert PAYOFF["grp_id"] in {entry["grp_id"] for entry in fra_control["main_deck"]}
    assert not fra_control.get("limited_checks")

    # _build_candidate standalone check still reports orphan cut reason directly.
    remaining = [_filler(4001, "{2}{G}", gih=0.55)]
    cand = _build_candidate(
        [with_role(c, "BLB") for c in pool_orphan],
        {
            "colors": ("B", "G"),
            "chosen": [with_role(c, "BLB") for c in core[:23]],
            "quality": blb_orphan["quality"],
        },
    )
    assert any(
        c["grp_id"] == PAYOFF["grp_id"] and c["reason"] == "payoff lacks enablers in this pool"
        for c in cand["cuts"]
    )
    slot_chosen = [with_role(payoff, "BLB"), *core[:22]]
    slot_remaining = list(remaining)
    _fill_role_slots(slot_chosen, slot_remaining, ("B", "G"), ())
    assert PAYOFF["grp_id"] not in {c["grp_id"] for c in slot_chosen}


def test_d2_off_archetype_and_colorless_land_enablers_do_not_count_in_wrong_lane():
    # Regression for Reviewer Finding #1: TRK Earth, Sector 001 (UG) and Unexplored Territory (RG)
    # are colorless lands with enabler role, and Support Mission (WG) is mono-green; none may count
    # as an enabler for BR Vengeance Mission or BG Vash.
    earth = with_role(
        {
            "grp_id": 106858,
            "name": "Earth, Sector 001",
            "mana_cost": "",
            "type_line": "Land — Planet",
            "oracle_text": "{T}: Add {C}. Tap an untapped creature you control: Put a charge counter on Earth, Sector 001.",
        },
        "TRK",
    )
    unexplored = with_role(
        {
            "grp_id": 106880,
            "name": "Unexplored Territory",
            "mana_cost": "",
            "type_line": "Land",
            "oracle_text": "When Unexplored Territory enters, if you control a Mount or landfall creature, search your library for a basic land card, put it onto the battlefield tapped, then shuffle.",
        },
        "TRK",
    )
    vengeance = with_role(
        {
            "grp_id": 106713,
            "name": "Vengeance Mission",
            "mana_cost": "{2}{B}",
            "type_line": "Enchantment",
            "oracle_text": "Whenever a Klingon you control dies, each opponent loses 2 life and you gain 2 life.",
        },
        "TRK",
    )
    support = with_role(
        {
            "grp_id": 106793,
            "name": "Support Mission",
            "mana_cost": "{2}{G}",
            "type_line": "Enchantment",
            "oracle_text": "At the beginning of combat on your turn, put a +1/+1 counter on target creature you control.",
        },
        "TRK",
    )
    vash = with_role(
        {
            "grp_id": 106832,
            "name": "Vash, Freelance Archaeologist",
            "mana_cost": "{1}{B}{G}",
            "type_line": "Legendary Creature — Human Rogue Scientist",
            "oracle_text": "Whenever one or more cards leave your graveyard, draw a card and lose 1 life.",
        },
        "TRK",
    )
    fmt = FormatContext(mean_gih_wr_pct=54.0, std_gih_wr_pct=4.0)

    # BR pool containing colorless UG + RG enabler lands and NO BR enablers.
    br_pool = [
        normalize_card({**_filler(1, "{1}{B}"), "gih_wr": 0.55}),
        normalize_card({**_filler(2, "{1}{R}"), "gih_wr": 0.55}),
        normalize_card({**_filler(3, "{2}{B}"), "gih_wr": 0.55}),
        normalize_card({**_filler(4, "{2}{R}"), "gih_wr": 0.55}),
        normalize_card(earth),
        normalize_card(unexplored),
    ]
    br_payoff_card = CardData(
        grp_id=106713,
        name="Vengeance Mission",
        colors=("B",),
        mana_cost="{2}{B}",
        cmc=3.0,
        types=("Enchantment",),
        gih_wr_pct=55.0,
        limited_role=vengeance.get("limited_role")
        or {
            "set_code": "trk",
            "roles": ["payoff"],
            "removal": "unknown",
            "archetypes": [
                {"colors": "BR", "theme": "Sacrificing Klingons", "role": "payoff", "partners": []}
            ],
        },
    )
    # Seed explicit colorless enabler roles directly so oracle-text mismatch cannot mask the test.
    ug_land_role = {
        "set_code": "trk",
        "roles": ["enabler"],
        "removal": "unknown",
        "archetypes": [{"colors": "UG", "theme": "Different land names", "role": "enabler", "partners": []}],
    }
    rg_land_role = {
        "set_code": "trk",
        "roles": ["enabler"],
        "removal": "unknown",
        "archetypes": [{"colors": "RG", "theme": "Landfall", "role": "enabler", "partners": []}],
    }
    br_pool_explicit = [
        *br_pool[:4],
        CardData(
            grp_id=106858,
            name="Earth, Sector 001",
            colors=(),
            mana_cost="",
            cmc=0.0,
            types=("Land",),
            limited_role=ug_land_role,
        ),
        CardData(
            grp_id=106880,
            name="Unexplored Territory",
            colors=(),
            mana_cost="",
            cmc=0.0,
            types=("Land",),
            limited_role=rg_land_role,
        ),
    ]
    br_res = evaluate_pack([br_payoff_card], br_pool_explicit, 3, 2, fmt=fmt)[0]
    assert any("no reviewed enablers yet" in r for r in br_res.reasons)
    assert not any("enablers in pool" in r for r in br_res.reasons)

    # BG pool containing 2 WG enablers (Support Mission) in evaluate_pack AND draft_autopick.rank_pack.
    wg_role = {
        "set_code": "trk",
        "name": "Support Mission",
        "roles": ["enabler"],
        "removal": "unknown",
        "archetypes": [{"colors": "WG", "theme": "Counters on creatures", "role": "enabler", "partners": []}],
    }
    bg_payoff_role = {
        "set_code": "trk",
        "name": "Vash, Freelance Archaeologist",
        "roles": ["payoff"],
        "removal": "unknown",
        "archetypes": [
            {
                "colors": "BG",
                "theme": "Cards entering and leaving the graveyard",
                "role": "payoff",
                "partners": [],
            }
        ],
    }
    bg_pool_explicit = [
        normalize_card({**_filler(10, "{1}{B}"), "gih_wr": 0.55}),
        normalize_card({**_filler(11, "{2}{B}"), "gih_wr": 0.55}),
        CardData(
            grp_id=106793,
            name="Support Mission",
            colors=("G",),
            mana_cost="{2}{G}",
            cmc=3.0,
            types=("Enchantment",),
            gih_wr_pct=55.0,
            limited_role=wg_role,
        ),
        CardData(
            grp_id=106793,
            name="Support Mission",
            colors=("G",),
            mana_cost="{2}{G}",
            cmc=3.0,
            types=("Enchantment",),
            gih_wr_pct=55.0,
            limited_role=wg_role,
        ),
    ]
    vash_card = CardData(
        grp_id=106832,
        name="Vash, Freelance Archaeologist",
        colors=("B", "G"),
        mana_cost="{1}{B}{G}",
        cmc=3.0,
        types=("Creature",),
        gih_wr_pct=55.0,
        limited_role=vash.get("limited_role") or bg_payoff_role,
    )
    bg_res = evaluate_pack([vash_card], bg_pool_explicit, 3, 2, fmt=fmt)[0]
    assert any("no reviewed enablers yet" in r for r in bg_res.reasons)

    primer = SetPrimer(
        set_code="TRK",
        cards={
            10: SetCard(10, "B1", "B", "common", "Creature", 2.0, gih_wr=0.55),
            11: SetCard(11, "B2", "B", "common", "Creature", 2.0, gih_wr=0.55),
            106793: SetCard(106793, "Support Mission", "G", "uncommon", "Enchantment", 3.0, gih_wr=0.55),
            106832: SetCard(
                106832, "Vash, Freelance Archaeologist", "BG", "uncommon", "Creature", 3.0, gih_wr=0.56
            ),
        },
        pair_stats={"BG": {"win_rate": 0.57, "games": 200}},
    )
    ap_res = rank_pack(
        [106832],
        [10, 11, 106793, 106793],
        primer,
        pack_number=2,
        pick_number=2,
        limited_roles={106793: wg_role, 106832: bg_payoff_role},
    )[0]
    assert any("without reviewed enablers" in r for r in ap_res.reasons)
    assert support["name"] == "Support Mission"


def test_d3_duplicate_copies_do_not_self_enable_and_match_limited_checks():
    from arenamcp.limited_deck import _limited_checks

    # Two distinct dict copies of a self-synergizing artifact creature payoff must still both be flagged
    # and swapped out consistently between _fill_role_slots and _limited_checks.
    self_role = {
        "set_code": "blb",
        "roles": ["payoff"],
        "removal": "unknown",
        "archetypes": [{"colors": "BG", "theme": "Forage", "role": "payoff", "partners": [91698]}],
    }

    def make_copy():
        return {
            "grp_id": 99001,
            "name": "Clockwork Squirrel",
            "mana_cost": "{3}{G}",
            "type_line": "Artifact Creature — Squirrel",
            "oracle_text": "Whenever another artifact enters under your control, put a +1/+1 counter on this creature.",
            "gih_wr": 0.56,
            "limited_role": self_role,
        }

    core = [_removal(5000 + i, "{1}{B}", gih=0.58) for i in range(4)] + [
        _filler(5100 + i, "{1}{B}" if i % 2 == 0 else "{1}{G}", gih=0.55) for i in range(21)
    ]
    copy_a, copy_b = make_copy(), make_copy()
    chosen_distinct = [copy_a, copy_b, *core[:21]]
    remaining = [*core[21:]]
    _fill_role_slots(chosen_distinct, remaining, ("B", "G"), ())
    assert all(c["grp_id"] != 99001 for c in chosen_distinct)

    checks_shared = _limited_checks([copy_a, *core], {c["grp_id"]: 1 for c in [copy_a, *core[:22]]})
    checks_distinct = _limited_checks(
        [copy_a, copy_b, *core[:22]], {99001: 2, **{c["grp_id"]: 1 for c in core[:21]}}
    )
    assert "Clockwork Squirrel" in checks_shared["orphan_payoffs"]
    assert "Clockwork Squirrel" in checks_distinct["orphan_payoffs"]


def test_d3_recommend_deck_sends_set_record_and_rejects_orphan_payoff_regression():
    payoff = {**PAYOFF, "gih_wr": 0.56}
    core = [_removal(2100 + i, "{1}{B}", gih=0.58) for i in range(4)] + [
        _filler(3100 + i, "{1}{B}" if i % 2 == 0 else "{1}{G}", gih=0.55) for i in range(21)
    ]
    pool_cards = [with_role(c, "BLB") for c in [payoff, *core]]
    baseline = fallback_deck(pool_cards)
    baseline["pool_cards"] = pool_cards
    baseline["candidates"] = []

    prompts: list[dict] = []
    # Swap one kept 2-drop for PAYOFF so early/removal targets stay met while introducing 1 orphan payoff.
    kept_list = list(baseline["main_deck"])
    bad_main = [*kept_list[:-1], {"grp_id": PAYOFF["grp_id"], "count": 1}]
    bad_reply = json.dumps(
        {
            "main_deck": bad_main,
            "basic_lands": baseline["basic_lands"],
            "cuts": [],
            "plan": "Play black-green creatures and removal.",
        }
    )

    class _FakeBackend:
        def complete(self, system_prompt, message, *args, **kwargs):
            prompts.append(json.loads(message))
            return bad_reply

    advisor = DraftAdvisor(_FakeBackend())
    result = advisor.recommend_deck(baseline)
    assert len(prompts) == 1
    assert prompts[0]["reviewed_set_record"]["archetypes"] == {"BG": "Forage"}
    assert result["reasoning_source"] == "heuristic"  # rejected orphan-payoff worsening!

    # Contrary control: model respects clean non-orphan build -> accepted.
    good_reply = json.dumps(
        {
            "main_deck": baseline["main_deck"],
            "basic_lands": baseline["basic_lands"],
            "cuts": baseline["cuts"],
            "plan": "Play black-green creatures and removal.",
        }
    )

    class _GoodBackend:
        def complete(self, system_prompt, message, *args, **kwargs):
            return good_reply

    accepted = DraftAdvisor(_GoodBackend()).recommend_deck(baseline)
    assert accepted["reasoning_source"] == "card_rules"
