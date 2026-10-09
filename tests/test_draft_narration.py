"""Spoken draft commentary: grounded reasons per pick, story beats, and the on/off toggle."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from arenamcp.draft_autopick import Lane, PickScore
from arenamcp.draft_narration import DraftNarrator


def card(grp_id, name, colors, gih, *, types="Creature — Wizard", cmc=2.0, oracle="Flying"):
    return SimpleNamespace(
        grp_id=grp_id, name=name, colors=colors, gih_wr=gih, types=types, cmc=cmc, oracle=oracle
    )


class Primer:
    def __init__(self, cards):
        self.cards = {c.grp_id: c for c in cards}
        self.archetypes = [
            {"colors": "WU", "name": "Azorius Scry & Surveil Tempo", "key_commons": ["Tide Scholar"]},
            {"colors": "UB", "name": "Dimir Control"},
        ]
        self.pair_stats = {"WU": {"win_rate": 0.56}, "UB": {"win_rate": 0.567}, "RG": {"win_rate": 0.51}}

    def card(self, grp_id):
        return self.cards.get(grp_id)

    def card_roles(self, name):
        return [
            (a["colors"], role)
            for a in self.archetypes
            for role in ("key_commons",)
            if name in (a.get(role) or [])
        ]


CARDS = [
    card(1, "Sky Ace", "U", 0.62),
    card(2, "Tide Scholar", "U", 0.55),
    card(3, "Bog Snare", "B", 0.58, types="Instant", oracle="Destroy target creature."),
    card(4, "Meadow Squire", "W", 0.53),
    card(5, "Forge Pup", "R", 0.54),
]
PRIMER = Primer(CARDS)


def lane(colors, commitment=0.6):
    return Lane(colors=colors, weights={}, commitment=commitment)


def line(
    narrator, pick, ranking, *, pool=(1, 2), before="U", after="U", pack=1, number=5, size=14, reason=""
):
    return narrator.pick_line(
        names=[PRIMER.card(pick.grp_id).name],
        chosen=[pick],
        ranking=ranking,
        pool=list(pool),
        lane_before=lane(before),
        lane_after=lane(after),
        primer=PRIMER,
        pack_number=pack,
        pick_number=number,
        pack_size=size,
        model_reason=reason,
    )


def score(grp_id, *reasons):
    return PickScore(grp_id=grp_id, name=PRIMER.card(grp_id).name, score=0.0, reasons=list(reasons))


def test_best_card_in_pack_is_said_from_17lands_data():
    narrator = DraftNarrator()
    narrator._lane = "UW"  # no lane story this pick
    text = line(narrator, score(1), [score(1), score(2)], after="WU", before="WU")
    assert text.startswith(
        "Taking Sky Ace: the best card in this pack on 17Lands data, in our white-blue colors."
    )


def test_removal_need_and_archetype_role_are_grounded():
    narrator = DraftNarrator()
    narrator._lane = "UB"
    text = line(
        narrator, score(3, "pool needs removal (0 so far)"), [score(1), score(3)], before="UB", after="UB"
    )
    assert text.startswith("Taking Bog Snare: removal our blue-black deck needs.")
    narrator._lane = "WU"
    text = line(narrator, score(2, "in lane WU"), [score(1), score(2)], before="WU", after="WU")
    assert "a key common for Azorius Scry & Surveil Tempo" in text


def test_unverified_model_synergy_is_never_spoken():
    narrator = DraftNarrator()
    narrator._lane = "UB"
    reason = "Pairs with Sky Ace to empower Jace (synergy unverified)"
    text = line(narrator, score(4), [score(1), score(4)], before="UB", after="UB", reason=reason)
    assert "Sky Ace" not in text.split(":", 1)[1] and "empower" not in text


def test_lane_change_tells_the_story_once():
    narrator = DraftNarrator()
    first = line(narrator, score(3), [score(3)], before="U", after="UB")
    assert "We're leaning blue-black, one of the strongest pairs in this set on 17Lands" in first
    assert "the deck is Dimir Control" in first
    again = line(narrator, score(3), [score(3)], before="UB", after="UB")
    assert "leaning" not in again


def test_end_of_pack_summary_names_best_cards_and_needs():
    narrator = DraftNarrator()
    narrator._lane = "UB"
    text = line(narrator, score(2), [score(2)], pool=(1, 2), before="UB", after="UB", number=14, size=14)
    assert "End of pack 1: we're blue-black with 2 on-color cards, led by Sky Ace and Tide Scholar" in text
    assert "still looking for removal" in text


@pytest.fixture
def primer():
    from tests.test_draft_autoplay import PAIRS, RATINGS

    from arenamcp.set_primer import data_primer

    return data_primer("TST", RATINGS, PAIRS, lambda grp, name: {"oracle_text": f"{name} text", "cmc": 3.0})


def test_driver_commentary_toggle(primer):
    from tests.test_draft_autoplay import PICK_STATE, FakeBridge, driver_for

    for enabled in (False, True):
        bridge = FakeBridge(
            {
                "get_screen": {"ok": True, "draft": True},
                "get_draft_state": dict(PICK_STATE),
                "submit_draft_pick": {"ok": True},
            }
        )
        spoken: list[str] = []
        driver = driver_for(bridge, primer, speak_fn=spoken.append, commentary_fn=lambda e=enabled: e)
        driver._step()
        assert len(spoken) == 1
        if enabled:
            assert spoken[0].startswith("Taking ") and ":" in spoken[0]
        else:
            assert spoken[0] == "Taking Blue Ace."


# ---------------------------------------------------------------------------
# Pick-Two: each card its own reason, grammar for two, honesty about our lane
# (2026-10-07 live PickTwoDraft_FRA: "Taking Twinned Vision and Extended
# Absence: the best card in this pack on 17Lands data." for two cards, and
# Haunted Ridge, a B/R dual, under "a solid card for our blue-black deck").
# ---------------------------------------------------------------------------

LANDS = [
    card(40, "Ash Ridge", "", 0.63, types="Land", cmc=None, oracle="{oT}: Add {oB} or {oR}."),
    card(41, "Tidal Fen", "", 0.54, types="Land", cmc=None, oracle="{oT}: Add {oU} or {oB}."),
]
LAND_PRIMER = Primer(CARDS + LANDS)


def pair_line(narrator, picks, ranking, *, colors="UB", commitment=0.6, reasons=None, primer=PRIMER):
    return narrator.pick_line(
        names=[primer.card(p.grp_id).name for p in picks],
        chosen=picks,
        ranking=ranking,
        pool=[1, 2],
        lane_before=lane(colors, commitment),
        lane_after=lane(colors, commitment),
        primer=primer,
        pack_number=1,
        pick_number=5,
        pack_size=7,
        model_reasons=reasons,
        verb="Choosing",
    )


def test_two_best_cards_are_spoken_in_the_plural():
    narrator = DraftNarrator()
    narrator._lane = "U"
    text = pair_line(
        narrator, [score(1), score(2)], [score(1), score(2), score(4)], colors="U", commitment=0.1
    )
    assert text == "Choosing Sky Ace and Tide Scholar: the two best cards in this pack on 17Lands data."
    assert "the best card in this pack" not in text


def test_pick_two_gives_each_card_its_own_deck_reason():
    narrator = DraftNarrator()
    narrator._lane = "UB"
    picks = [score(1), score(3, "pool needs removal (1 so far)")]
    text = pair_line(narrator, picks, [score(1), score(3), score(4)])
    assert text == (
        "Choosing Sky Ace — the best card in this pack on 17Lands data, in our blue-black colors; "
        "and Bog Snare — removal our blue-black deck needs."
    )


def test_off_lane_cards_are_never_called_a_fit_even_when_the_model_says_so():
    narrator = DraftNarrator()
    narrator._lane = "UB"
    reasons = ["", "Fits our deck perfectly and curves out nicely."]
    text = pair_line(narrator, [score(1), score(5, "in lane UB")], [score(1), score(5)], reasons=reasons)
    assert "Forge Pup — the strongest option left, though it's outside our blue-black colors" in text
    assert "fits our deck" not in text.lower()


def test_lands_are_described_by_the_mana_they_make_for_our_lane():
    narrator = DraftNarrator()
    narrator._lane = "UB"
    picks = [score_in(LAND_PRIMER, 40), score_in(LAND_PRIMER, 41)]
    text = pair_line(narrator, picks, picks + [score(1)], primer=LAND_PRIMER)
    assert "Ash Ridge — only a black source for our blue-black deck" in text
    assert "Tidal Fen — fixes our blue-black mana" in text
    # Lands' inflated GIH never makes them "the best card in this pack".
    assert "17Lands" not in text


def test_model_reason_prefers_the_clause_about_our_deck_and_says_colors():
    narrator = DraftNarrator()
    narrator._lane = "UB"
    reasons = [
        "",
        "Strongest body in the pack: a 1/5 flier. Fits our UB spells lane and adds a needed creature.",
    ]
    text = pair_line(narrator, [score(1), score(2)], [score(1), score(4), score(2)], reasons=reasons)
    assert "Tide Scholar — fits our blue-black spells lane and adds a needed creature" in text


def score_in(primer, grp_id, *reasons):
    return PickScore(grp_id=grp_id, name=primer.card(grp_id).name, score=0.0, reasons=list(reasons))


def test_plan_colors_reads_guilds_archetypes_words_and_codes():
    from arenamcp.draft_narration import plan_colors

    assert plan_colors("Izzet spells built around Jace", PRIMER) == "UR"
    assert plan_colors("Settle into the Dimir Control deck with mill", PRIMER) == "UB"
    assert plan_colors("A blue-red tempo deck with cheap spells", PRIMER) == "UR"
    assert plan_colors("red and green stompy", PRIMER) == "RG"
    assert plan_colors("UB tempo with evasive threats", PRIMER) == "UB"
    assert plan_colors("Aggressive tempo deck with cheap creatures", PRIMER) == ""
    assert plan_colors("blue, black and red goodstuff", PRIMER) == ""
    assert plan_colors("", PRIMER) == ""


def test_model_plan_holds_the_lane_when_the_pool_estimate_flips():
    narrator = DraftNarrator()
    first = line(narrator, score(1), [score(1)], before="U", after="UR", reason="Izzet spells with Jace")
    assert "We're leaning blue-red" in first
    # Black cards pile up and the pool estimate tips to blue-black; the model's plan is still Izzet.
    narrator_line = narrator.pick_line(
        names=["Bog Snare"],
        chosen=[score(3)],
        ranking=[score(3), score(5)],
        pool=[1, 2, 3],
        lane_before=lane("UB", 0.8),
        lane_after=lane("UB", 0.8),
        primer=PRIMER,
        pack_number=3,
        pick_number=2,
        pack_size=14,
        model_plan="Izzet spells with Jace",
    )
    assert "blue-black" not in narrator_line and "Dimir" not in narrator_line
    assert "outside our blue-red colors" in narrator_line
    again = narrator.pick_line(
        names=["Forge Pup"],
        chosen=[score(5)],
        ranking=[score(5), score(3)],
        pool=[1, 2, 3, 5],
        lane_before=lane("UB", 0.8),
        lane_after=lane("UB", 0.8),
        primer=PRIMER,
        pack_number=3,
        pick_number=3,
        pack_size=14,
        model_plan="",  # a ranking pick: the last stated plan still holds
    )
    assert "our blue-red deck" in again and "leaning" not in again


def test_model_plan_without_colors_falls_back_to_the_pool_estimate():
    narrator = DraftNarrator()
    text = narrator.pick_line(
        names=["Bog Snare"],
        chosen=[score(3)],
        ranking=[score(3)],
        pool=[1, 2, 3],
        lane_before=lane("U"),
        lane_after=lane("UB"),
        primer=PRIMER,
        pack_number=1,
        pick_number=5,
        pack_size=14,
        model_plan="Grind them out with removal and card advantage",
    )
    assert "We're leaning blue-black" in text


def test_driver_passes_the_models_plan_to_the_narrator(primer):
    from tests.test_draft_autoplay import PICK_STATE, FakeBridge, driver_for

    driver = driver_for(FakeBridge({"get_draft_state": PICK_STATE}), primer)
    seen = {}

    def pick_line(**kwargs):
        seen.update(kwargs)
        return "spoken"

    driver._narrator.pick_line = pick_line
    driver.run.plan = "Izzet spells with Jace"
    assert (
        driver._pick_commentary(
            chosen=[12],
            names=["x"],
            pack=[12, 1, 3],
            pool=[],
            primer=primer,
            pack_number=1,
            pick_number=2,
            model_reasons={},
        )
        == "spoken"
    )
    assert seen.get("model_plan") == "Izzet spells with Jace"


def test_plan_colors_ignores_a_splash():
    from arenamcp.draft_narration import plan_colors

    assert plan_colors("Blue-red spells splashing black for Garruk", PRIMER) == "UR"
    assert plan_colors("blue-red tempo with a black splash", PRIMER) == "UR"


# ---------------------------------------------------------------------------
# The narrator's lane follows the model's stated plan, not the pool estimate
# (2026-10-08 QuickDraft_FRA: the plan said "Izzet spells/Jace-empowerment ...
# light black splash" from P1p2 on while the pool estimate tipped to UB at P1p9
# and P3 onward, so the coach said "the deck is Dimir Threshold Mill", "a
# creature our blue-black deck needs" and "outside our blue-black colors").
# ---------------------------------------------------------------------------

FRA_ARCHETYPES = [
    {"colors": "WU", "name": "Azorius Scry-Surveil Tempo"},
    {"colors": "UB", "name": "Dimir Threshold Mill"},
    {"colors": "UR", "name": "Izzet Spells & Jace Empowerment"},
    {"colors": "UG", "name": "Simic Ramp-and-Empower"},
]

# Plan sentences from the live logs (2026-10-06..08) and what they name.
REAL_PLANS = [
    # P1p2: two guilds joined by "or" share only blue; the second color is open.
    (
        "Blue-based tempo/spells deck leaning toward Azorius scry-surveil or Izzet Jace empowerment, "
        "starting from Mindseeker Oculus plus premium interaction.",
        "U",
        "",
    ),
    (
        "Blue-based tempo/spells deck (Izzet or Azorius) using cheap interaction and Jace empowerment, "
        "still flexible on second color.",
        "U",
        "",
    ),
    (
        "Continue the blue-based Izzet/Azorius spells-tempo lane: cheap instants and flash creatures.",
        "UR",
        "",
    ),
    ("Blue-red spells tempo: cheap instants and Jace-empowering creatures build an early engine.", "UR", ""),
    # P1p9: Izzet now; Dimir is only kept open.
    (
        "Stay blue-centered (Izzet spells/Jace now, keeping Dimir threshold open), adding cheap evasive "
        "bodies and interaction while waiting on a Jace planeswalker and real removal.",
        "UR",
        "",
    ),
    # P1p10: red named as a "secondary splash" is already a main color.
    (
        "Blue-centered Izzet spells/Jace deck with red as a secondary splash for bodies and burn, "
        "staying open to Dimir threshold.",
        "UR",
        "",
    ),
    (
        "Stay on the Tier-1 Izzet Spells & Jace Empowerment lane; this pick is a flexible utility body.",
        "UR",
        "",
    ),
    (
        "Stay on the Tier-1 Izzet Spells/Jace Empowerment lane while staying open to adding black for "
        "Garruk as a late-game bomb and extra removal.",
        "UR",
        "",
    ),
    # P3p2-p6: a light black splash is our splash, not a lane color.
    (
        "Continue Izzet Spells/Jace Empowerment as the core, splashing black for Garruk, Veiled Butcher and "
        "building toward Dimir Threshold payoffs like Void Extrapolator.",
        "UR",
        "B",
    ),
    (
        "Izzet spells and Jace-empowerment tempo: cheap interaction and fliers, with a light black splash "
        "for Garruk and Void Extrapolator.",
        "UR",
        "B",
    ),
    (
        "Continue the Izzet spells/Jace-empowerment core, with black as a light secondary for Recursive "
        "Recruitment and Screeching Soulbreaker if fixing allows.",
        "UR",
        "B",
    ),
    (
        "Stay on the Izzet spells/Jace-empowerment core while keeping white open for an Azorius pivot; "
        "add cheap evasive bodies now.",
        "UR",
        "",
    ),
    # 2026-10-06 draft: codes, archetype names and an open plan.
    (
        "Blue-led Jace-empowerment/tempo core in UG, adding a light black splash for Overwrite the "
        "Multiverse as a late-game reset.",
        "UG",
        "B",
    ),
    ("Continue Dimir Threshold Mill: cheap mill enablers feed Void Extrapolator.", "UB", ""),
    (
        "Blue-led tempo/threshold deck leaning on cheap creatures, with green as a possible secondary via "
        "this fixing land.",
        "U",
        "G",
    ),
    (
        "Stay flexible between Dimir Threshold Mill and Izzet/Dimir spells-tempo, anchored by Dark Matter "
        "Manipulator.",
        "",
        "",
    ),
    ("Open toward Dimir Threshold Mill, starting with a cheap self-milling threat.", "", ""),
]


@pytest.mark.parametrize(("plan", "main", "splash"), REAL_PLANS)
def test_real_plan_sentences_name_their_colors_and_splash(plan, main, splash):
    from arenamcp.draft_plan import plan_colors as read_plan

    colors = read_plan(plan, FRA_ARCHETYPES)
    assert (colors.main, colors.splash) == (main, splash)


def test_plan_naming_an_archetype_reports_it():
    from arenamcp.draft_plan import plan_colors as read_plan

    colors = read_plan("Continue Tier-1 Izzet Spells & Jace Empowerment: cheap instants.", FRA_ARCHETYPES)
    assert colors.main == "UR" and colors.archetype == "Izzet Spells & Jace Empowerment"


class FraPrimer(Primer):
    def __init__(self, cards):
        super().__init__(cards)
        self.archetypes = FRA_ARCHETYPES
        self.pair_stats = {"UB": {"win_rate": 0.567}, "UR": {"win_rate": 0.56}, "WU": {"win_rate": 0.55}}


FRA_PRIMER = FraPrimer(CARDS)
P1P9_PLAN = (
    "Stay blue-centered (Izzet spells/Jace now, keeping Dimir threshold open), adding cheap evasive "
    "bodies and interaction while waiting on a Jace planeswalker and real removal."
)
P1P10_PLAN = (
    "Blue-centered Izzet spells/Jace deck with red as a secondary splash for bodies and burn, "
    "staying open to Dimir threshold."
)
P3P5_PLAN = (
    "Izzet spells and Jace-empowerment tempo: cheap interaction and fliers, with a light black splash "
    "for Garruk and Void Extrapolator."
)


def fra_line(narrator, pick, ranking, *, plan, estimate="UB", pack=1, number=9, size=14, pool=(1, 2, 3)):
    return narrator.pick_line(
        names=[FRA_PRIMER.card(pick.grp_id).name],
        chosen=[pick],
        ranking=ranking,
        pool=list(pool),
        lane_before=lane(estimate, 0.6),
        lane_after=lane(estimate, 0.6),
        primer=FRA_PRIMER,
        pack_number=pack,
        pick_number=number,
        pack_size=size,
        model_plan=plan,
        verb="Choosing",
    )


def test_p1p9_dimir_is_not_announced_over_an_izzet_plan():
    # Live: "We're leaning blue-black ...; the deck is Dimir Threshold Mill."
    narrator = DraftNarrator()
    text = fra_line(narrator, score(3, "pool needs more creatures"), [score(3), score(1)], plan=P1P9_PLAN)
    assert "blue-black" not in text and "Dimir" not in text
    assert "We're leaning blue-red" in text and "the deck is Izzet Spells & Jace Empowerment" in text


def test_p1p10_a_red_card_is_in_our_colors_under_the_izzet_plan():
    # Live: "Choosing Tether Technician: the best card here on 17Lands data, though it's outside our blue-black colors."
    narrator = DraftNarrator()
    narrator._lane = "UR"
    text = fra_line(narrator, score(5), [score(5), score(4)], plan=P1P10_PLAN, number=10)
    assert "outside" not in text and "blue-black" not in text
    assert text.startswith(
        "Choosing Forge Pup: the best card in this pack on 17Lands data, in our blue-red colors."
    )


def test_p3p5_needs_framing_uses_the_plans_colors_and_its_splash(caplog):
    # Live: "Choosing Geist of Saint Thalia: a creature our blue-black deck needs."
    narrator = DraftNarrator()
    narrator._lane = "UR"
    with caplog.at_level(logging.INFO, logger="arenamcp.draft_narration"):
        text = fra_line(
            narrator,
            score(1, "pool needs more creatures"),
            [score(1), score(3)],
            plan=P3P5_PLAN,
            pack=3,
            number=5,
        )
        assert text.startswith("Choosing Sky Ace: a creature our blue-red deck needs")
        # Black is the plan's splash: a black card is ours, never "outside our colors".
        text = fra_line(narrator, score(3), [score(3), score(1)], plan=P3P5_PLAN, pack=3, number=6)
        assert text.startswith("Choosing Bog Snare: removal for our black splash")
        # The needs beat names the plan's archetype, not the estimate's.
        narrator._since_story = 4
        text = fra_line(narrator, score(2), [score(2)], plan="", pack=3, number=7)  # a ranking pick
        assert "For our Izzet Spells & Jace Empowerment deck we still want" in text
        assert "Dimir" not in text
    disagreements = [m for m in caplog.messages if m.startswith("Draft lane: the plan says UR")]
    assert disagreements == [
        "Draft lane: the plan says UR (Izzet Spells & Jace Empowerment) but the pool estimate says UB; "
        "following the plan"
    ]


def test_end_of_pack_summary_names_the_splash():
    narrator = DraftNarrator()
    narrator._lane = "UR"
    text = fra_line(
        narrator, score(1), [score(1)], plan=P3P5_PLAN, pack=2, number=14, size=14, pool=(1, 2, 3)
    )
    assert "End of pack 2: we're blue-red splashing black with 2 on-color cards" in text


def test_one_color_plan_keeps_the_second_color_open():
    # P1p2-p4: "Blue-based ... Izzet or Azorius" is blue; no pair is announced yet.
    narrator = DraftNarrator()
    plan = "Blue-based tempo/spells deck (Izzet or Azorius) using cheap interaction, still flexible on second color."
    text = fra_line(narrator, score(1), [score(1), score(3)], plan=plan, estimate="UB", number=3)
    assert "leaning" not in text and "blue-black" not in text and "Dimir" not in text
    assert text == "Choosing Sky Ace: the best card in this pack on 17Lands data."


def test_driver_logs_the_plans_lane_beside_the_pool_estimate(primer, caplog):
    from tests.test_draft_autoplay import PICK_STATE, FakeBridge, driver_for

    def recommend(details, fallback):
        return {
            "reasoning_source": "card_rules",
            "recommendations": [{"grp_id": 3, "reason": "Burn for the Izzet deck."}],
            "plan": "Izzet spells tempo with a light black splash for removal.",
            "lane": "UR",
            "splash": "B",
            "needs": ["Removal"],
        }

    bridge = FakeBridge(
        {
            "get_screen": {"ok": True, "draft": True},
            "get_draft_state": {**PICK_STATE, "pick_number": 6, "pick_seconds_remaining": 60},
            "submit_draft_pick": {"ok": True},
        }
    )
    spoken: list[str] = []
    driver = driver_for(
        bridge,
        primer,
        picked_fn=lambda: [6, 6, 6, 1, 2],  # black-heavy pool: the estimate says UB
        pick_advisor_fn=lambda: SimpleNamespace(recommend=recommend),
        pack_fn=lambda: {
            "pack_number": 1,
            "pick_number": 6,
            "cards": [{"grp_id": g} for g in PICK_STATE["pack_cards"]],
        },
        speak_fn=spoken.append,
    )
    with caplog.at_level(logging.INFO, logger="arenamcp.draft_event"):
        driver._step()
    strategy = next(m for m in caplog.messages if m.startswith("Draft strategy P1p6"))
    assert strategy.startswith("Draft strategy P1p6: lane=UR; pool=UB; plan=Izzet spells tempo")
    assert driver.run.plan_lane == "UR"
    assert spoken and "blue-black" not in spoken[0] and "Dimir" not in spoken[0]
