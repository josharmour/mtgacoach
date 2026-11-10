"""London-mulligan policy: logged FRA hands and generic land-count cases.

2026-10-06, match 3da54de9 (Player.log MulliganReq/MulliganResp): both games
started on five cards. G1 was on the draw, G2 on the play. The hands below
are the exact seven cards shown at each mulligan decision.
"""

from __future__ import annotations

import logging

import pytest

from arenamcp import mulligan_policy as mp
from arenamcp.action_planner import ActionPlanner
from arenamcp.coach import CoachEngine
from arenamcp.coach_prompts import DECISION_PROMPTS
from arenamcp.decisions import DecisionOption, PendingDecision

ISLAND, MOUNTAIN, FOREST, PLAINS, SWAMP = 106529, 106533, 74121, 70001, 74159
CARDS = {
    ISLAND: ("Island", "Basic Land — Island", "", "({T}: Add {U}.)", "common"),
    MOUNTAIN: ("Mountain", "Basic Land — Mountain", "", "({T}: Add {R}.)", "common"),
    FOREST: ("Forest", "Basic Land — Forest", "", "({T}: Add {G}.)", "common"),
    PLAINS: ("Plains", "Basic Land — Plains", "", "({T}: Add {W}.)", "common"),
    SWAMP: ("Swamp", "Basic Land — Swamp", "", "({T}: Add {B}.)", "common"),
    106256: ("Icy Reception", "Instant", "{1}{U}", "Choose one —\n•Target creature gets -5/-0.", "common"),
    106259: (
        "Mindseeker Oculus",
        "Creature — Homunculus",
        "{2}{U}",
        "When this enters, empower Jace 4.",
        "common",
    ),
    106265: (
        "Semester Foreseer",
        "Creature — Human Wizard",
        "{3}{U}",
        "When this enters, surveil 1.",
        "common",
    ),
    106267: (
        "Sphinx of False Conclusions",
        "Creature — Sphinx Illusion",
        "{2}{U}{U}",
        "Flash\nFlying\nWhenever this creature attacks, draw a card, then discard a card.",
        "rare",
    ),
    106268: ("Sphinx's Approach", "Instant", "{1}{U}{U}", "Draw two cards.", "common"),
    106272: (
        "Undulating Witness",
        "Creature — Serpent",
        "{4}{U}",
        "Flying\n{o2}: This creature gets +1/-1 until end of turn.\nBasic landcycling {o2}",
        "common",
    ),
    106273: ("Unsummon", "Instant", "{U}", "Return target creature to its owner's hand.", "common"),
    106394: (
        "Tam's Resistance",
        "Sorcery",
        "{1}{G/U}",
        "Put a +1/+1 counter on up to one target creature.",
        "common",
    ),
    106399: (
        "Twinned Vision",
        "Instant",
        "{1}{U/R}",
        "Draw a card.\nFlashback—{o1o(U/R)o(U/R)}, Discard a card.",
        "common",
    ),
    106416: (
        "Keeper of the Quiet Hour",
        "Artifact Creature — Chimera",
        "{3}",
        "When this enters, empower Jace 2.",
        "common",
    ),
    106417: (
        "Living Library",
        "Artifact Creature — Book Illusion",
        "{2}",
        "{o6}, Sacrifice this creature: ...",
        "common",
    ),
    106458: (
        "Fblthp, Impossibly Lost",
        "Legendary Creature — Homunculus",
        "{1}{U}",
        "Draw two cards.",
        "uncommon",
    ),
    900: ("Grizzly Bears", "Creature — Bear", "{1}{G}", "", "common"),
    901: ("Hill Giant", "Creature — Giant", "{3}{R}", "", "common"),
    902: ("Shock", "Instant", "{R}", "Shock deals 2 damage to any target.", "common"),
    903: ("Colossus", "Creature — Giant", "{6}{R}{R}", "", "common"),
    904: ("Llanowar Elves", "Creature — Elf Druid", "{G}", "{oT}: Add {oG}.", "common"),
}

G1_SEVEN = [ISLAND, MOUNTAIN, MOUNTAIN, ISLAND, 106416, 106273, MOUNTAIN]
G1_SIX = [106272, 106394, 106265, ISLAND, 106273, MOUNTAIN, MOUNTAIN]
G1_FIVE = [106458, 106259, 106417, 106394, MOUNTAIN, 106272, 106268]
G2_SEVEN = [MOUNTAIN, MOUNTAIN, 106256, ISLAND, 106272, 106272, MOUNTAIN]
G2_SIX = [MOUNTAIN, MOUNTAIN, 106265, ISLAND, 106417, 106267, 106272]
G2_FIVE = [106268, 106399, ISLAND, MOUNTAIN, 106272, ISLAND, 106416]


def card(instance_id, grp_id):
    name, type_line, cost, text, rarity = CARDS[grp_id]
    return {
        "instance_id": instance_id,
        "grp_id": grp_id,
        "name": name,
        "type_line": type_line,
        "mana_cost": cost,
        "oracle_text": text,
        "rarity": rarity,
        "owner_seat_id": 2,
        "controller_seat_id": 2,
    }


def state(grp_ids, mulligans=0, on_play=False, match_id="3da54de9"):
    """Seat 2 is local; during mulligans the GRE's active player is the starting player."""
    players = [{"seat_id": 2, "is_local": True, "life_total": 20}, {"seat_id": 1, "life_total": 20}]
    if mulligans is not None:
        players[0]["mulligan_count"] = mulligans
    return {
        "match_id": match_id,
        "local_seat_id": 2,
        "pending_decision": "Mulligan",
        "decision_context": {"type": "mulligan"},
        "players": players,
        "turn": {"turn_number": 0, "active_player": 2 if on_play else 1},
        "hand": [card(200 + index, grp_id) for index, grp_id in enumerate(grp_ids)],
        "battlefield": [],
    }


def verdict(grp_ids, mulligans=0, on_play=False):
    return mp.mulligan_verdict(mp.situation(state(grp_ids, mulligans, on_play)))


# --- the logged hands --------------------------------------------------------------


def test_g1_six_card_three_lander_with_unsummon_is_kept():
    choice, reason = verdict(G1_SIX, mulligans=1, on_play=False)
    assert choice == "keep"
    assert "Unsummon" in reason


def test_g2_six_card_three_lander_with_sphinx_is_kept_and_keeps_the_bomb():
    choice, reason = verdict(G2_SIX, mulligans=1, on_play=True)
    assert choice == "keep"
    kept, bottom = mp.best_keep(mp.situation(state(G2_SIX, 1, True)).cards, 6)
    assert "Sphinx of False Conclusions" in {c.name for c in kept}
    assert sum(c.is_land for c in kept) == 3


def test_g2_seven_card_four_lander_with_icy_reception_is_kept():
    choice, reason = verdict(G2_SEVEN, mulligans=0, on_play=True)
    assert choice == "keep"
    assert "Icy Reception" in reason


def test_g1_seven_card_five_lander_goes_to_the_model():
    assert verdict(G1_SEVEN, mulligans=0, on_play=False) is None


@pytest.mark.parametrize("hand, on_play", [(G1_FIVE, False), (G2_FIVE, True)])
def test_the_accepted_five_card_hands_are_kept(hand, on_play):
    assert verdict(hand, mulligans=2, on_play=on_play)[0] == "keep"


def test_witness_is_described_as_a_five_drop_landcycler():
    lines = "\n".join(mp.describe(state(G2_SEVEN, 0, True)))
    assert "Undulating Witness {4}{U}" in lines and "mana value 5" in lines
    assert "landcycling {2}" in lines
    assert "KEEP → you keep 7 of these 7" in lines
    assert "MULLIGAN → you will see a new 7, keep 6 and bottom 1" in lines
    assert "ON THE PLAY" in lines


# --- generic land counts --------------------------------------------------------------


def hand_with(lands, spells):
    return [MOUNTAIN] * lands + spells


@pytest.mark.parametrize("lands", [0, 1, 6, 7])
def test_seven_card_extremes_mulligan(lands):
    spells = [902, 901, 900, 903, 902, 901, 900][: 7 - lands]
    assert verdict(hand_with(lands, spells), mulligans=0)[0] == "mulligan"


def test_two_lander_on_the_draw_needs_development_to_autokeep():
    # Q1 2026-11-08/09 (matches b55e160b, a4ca1d46): reactive burn alone no longer
    # auto-keeps a two-lander on the draw; a mana maker still does.
    assert verdict(hand_with(2, [902, 901, 901, 903, 903]), mulligans=0, on_play=False) is None
    assert verdict([FOREST, MOUNTAIN, 904, 902, 901, 903, 903], mulligans=0, on_play=False)[0] == "keep"


def test_two_lander_on_the_play_goes_to_the_model():
    assert verdict(hand_with(2, [902, 901, 901, 903, 903]), mulligans=0, on_play=True) is None


def test_second_mulligan_is_allowed_for_a_zero_land_six():
    assert verdict([902, 901, 900, 903, 902, 901, 900], mulligans=1)[0] == "mulligan"


def test_six_card_three_lander_is_kept():
    assert verdict(hand_with(3, [902, 901, 903, 903]), mulligans=1)[0] == "keep"


def test_five_card_two_lander_is_kept():
    assert verdict(hand_with(2, [902, 901, 903, 903, 900]), mulligans=2)[0] == "keep"


def test_five_cards_mulligan_only_when_unplayable():
    assert verdict([902, 901, 900, 903, 902, 901, 900], mulligans=2)[0] == "mulligan"
    assert verdict(hand_with(6, [903]), mulligans=2)[0] == "mulligan"
    # One Mountain and nothing castable for two or less (Hill Giant, Colossus).
    assert verdict(hand_with(1, [901, 901, 903, 903, 901, 903]), mulligans=2)[0] == "mulligan"
    assert verdict(hand_with(1, [902, 901, 903, 903, 901, 903]), mulligans=2)[0] == "keep"


def test_never_mulligan_to_three():
    assert verdict([902, 901, 900, 903, 902, 901, 900], mulligans=3)[0] == "keep"


def test_unknown_count_never_forces_a_mulligan():
    assert verdict([902, 901, 900, 903, 902, 901, 900], mulligans=None) is None
    assert verdict(G2_SEVEN, mulligans=None, on_play=True)[0] == "keep"


# Colour screw: lands that cannot make the spells' colours are not "lands" for a keep.
BLUE_SPELLS = [106273, 106256, 106259, 106268]  # Unsummon, Icy Reception, Oculus, Sphinx's Approach


def hand_cards(*grp_ids):
    return [mp.hand_card(card(300 + index, grp_id)) for index, grp_id in enumerate(grp_ids)]


def test_castability_needs_the_right_colours_and_enough_sources():
    mountains, islands = hand_cards(MOUNTAIN, MOUNTAIN, MOUNTAIN), hand_cards(ISLAND, ISLAND, ISLAND)
    unsummon, approach = hand_cards(106273, 106268)
    assert not mp.colors_reachable(unsummon, mountains) and not mp.castable_with(unsummon, mountains)
    assert mp.colors_reachable(unsummon, islands) and mp.castable_with(unsummon, islands)
    # {1}{U}{U} with one Island: the colour is reachable but needs two blue sources.
    one_island = hand_cards(ISLAND, MOUNTAIN, MOUNTAIN)
    assert mp.colors_reachable(approach, one_island) and not mp.castable_with(approach, one_island)
    assert mp.castable_with(approach, islands)


def test_three_off_colour_lands_are_not_an_automatic_keep():
    assert verdict([ISLAND] * 3 + BLUE_SPELLS, mulligans=0)[0] == "keep"
    assert verdict([MOUNTAIN] * 3 + BLUE_SPELLS, mulligans=0) is None  # left to the model
    lines = mp.describe(state([MOUNTAIN] * 3 + BLUE_SPELLS, mulligans=0))
    assert any(
        line.startswith("  - Unsummon") and "needs U mana this hand cannot make" in line for line in lines
    )


def test_a_one_lander_at_five_mulligans_when_its_land_casts_nothing():
    spells = BLUE_SPELLS + [106273, 106256]
    assert verdict([MOUNTAIN] + spells, mulligans=2)[0] == "mulligan"
    assert verdict([ISLAND] + spells, mulligans=2)[0] == "keep"


# --- planner integration -----------------------------------------------------------------


class Backend:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def complete(self, system, user, *args, **kwargs):
        self.prompts.append(user)
        return self.answers.pop(0)


MULLIGAN = PendingDecision(
    request_id=(4, 15),
    request_type="Mulligan",
    options=(DecisionOption("mull:keep", "Keep this hand"), DecisionOption("mull:mull", "Mulligan")),
)


def test_planner_keeps_the_logged_six_without_asking_the_model(caplog):
    backend = Backend('{"option_ids": ["mull:mull"], "reasoning": "no early plays"}')
    planner = ActionPlanner(backend, timeout=5, land_drop_first=False)
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        assert planner.plan_decision_options(MULLIGAN, state(G1_SIX, 1, False)) == ["mull:keep"]
    assert backend.prompts == []
    assert "Mulligan guard: KEEP" in caplog.text
    assert "Unsummon" in planner.get_decision_reasoning(["mull:keep"])


def test_deferred_hand_reaches_the_model_with_policy_and_hand_facts():
    backend = Backend('{"option_ids": ["mull:keep"], "reasoning": "five lands on the draw"}')
    planner = ActionPlanner(backend, timeout=5, land_drop_first=False)
    assert planner.plan_decision_options(MULLIGAN, state(G1_SEVEN, 0, False)) == ["mull:keep"]
    prompt = backend.prompts[0]
    assert "MULLIGAN POLICY (London)" in prompt
    assert "5 lands with castable spells is usually a keep" in prompt
    assert "mulligans taken this game: 0" in prompt and "ON THE DRAW" in prompt
    assert "Unsummon {U}: Instant, mana value 1, castable turn 1" in prompt


def test_planner_counts_its_own_mulligans_when_the_state_has_no_count():
    backend = Backend('{"option_ids": ["mull:mull"], "reasoning": "five lands"}')
    planner = ActionPlanner(backend, timeout=5, land_drop_first=False)
    assert planner.plan_decision_options(MULLIGAN, state(G1_SEVEN, None, False)) == ["mull:mull"]
    # The next look is a six-card decision: the logged keep no longer needs the model.
    assert planner.plan_decision_options(MULLIGAN, state(G1_SIX, None, False)) == ["mull:keep"]
    assert len(backend.prompts) == 1
    # Keeping resets the count for the next game of the match.
    assert planner._mulligans_taken(state(G1_SEVEN, None)) == 0


def test_the_gre_count_beats_the_planner_track():
    planner = ActionPlanner(Backend(), timeout=5, land_drop_first=False)
    planner._mulligan_track = ("3da54de9", 2)
    assert planner._mulligans_taken(state(G1_SIX, 1)) == 1


# --- London bottoming --------------------------------------------------------------------


def bottom_decision(game_state, count):
    return PendingDecision(
        request_id=(6, 21),
        request_type="Group",
        options=tuple(
            DecisionOption(
                f"grp:{c['instance_id']}", f"Bottom {c['name']}", meta={"instance_id": c["instance_id"]}
            )
            for c in game_state["hand"]
        ),
        min_select=count,
        max_select=count,
        source_label="LondonMulligan",
    )


def ids_of(game_state, *names):
    return [f"grp:{c['instance_id']}" for c in game_state["hand"] if c["name"] in names]


def test_bottoming_keeps_lands_and_cheap_plays():
    game_state = state(G1_SIX, 1)
    assert mp.bottom_choice(game_state, [c["instance_id"] for c in game_state["hand"]], 1) == [
        c["instance_id"] for c in game_state["hand"] if c["name"] == "Undulating Witness"
    ]


def test_model_bottoming_two_lands_into_a_one_land_five_is_replaced(caplog):
    game_state = state(G2_FIVE, 2, True)
    bad = ids_of(game_state, "Island")  # both Islands
    backend = Backend(f'{{"option_ids": {bad!r}, "reasoning": "keep spells"}}'.replace("'", '"'))
    planner = ActionPlanner(backend, timeout=5, land_drop_first=False)
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        chosen = planner.plan_decision_options(bottom_decision(game_state, 2), game_state)
    assert set(chosen).isdisjoint(bad)
    assert "Mulligan bottom guard" in caplog.text
    assert "LONDON MULLIGAN BOTTOM" in backend.prompts[0]


def test_a_reasonable_model_bottoming_is_kept():
    game_state = state(G2_FIVE, 2, True)
    good = ids_of(game_state, "Undulating Witness", "Sphinx's Approach")
    backend = Backend(f'{{"option_ids": {good!r}}}'.replace("'", '"'))
    planner = ActionPlanner(backend, timeout=5, land_drop_first=False)
    assert sorted(planner.plan_decision_options(bottom_decision(game_state, 2), game_state)) == sorted(good)


# --- coaching path --------------------------------------------------------------------


def test_coaching_prompt_no_longer_mulligans_five_landers():
    text = DECISION_PROMPTS["mulligan"]
    assert "5+ lands" not in text
    assert "5 lands is NOT an automatic mulligan; 6+ is" in text


def test_coach_mulligan_section_shows_hand_size_and_card_costs():
    lines = "\n".join(CoachEngine.__new__(CoachEngine)._format_mulligan_hand(state(G1_SIX, 1)))
    assert "KEEP → you keep 6 of these 7 and bottom 1" in lines
    assert "Tam's Resistance {1}{G/U}: Sorcery, mana value 2, castable turn 2" in lines


def test_coach_bottom_section_uses_the_real_count():
    game_state = state(G2_FIVE, 2, True)
    context = {
        "type": "mulligan_bottom",
        "raw": {
            "groupSpecs": [
                {"zoneType": "ZoneType_Library", "subZoneType": "SubZoneType_Bottom", "upperBound": 2}
            ]
        },
    }
    lines = CoachEngine.__new__(CoachEngine)._format_mulligan_bottom(context, game_state)
    assert lines[0] == "!!! DECISION: MULLIGAN - PUT 2 CARD(S) ON BOTTOM !!!"
    assert lines[-1].startswith("Land/curve suggestion: bottom ")


@pytest.mark.parametrize("land_count", [2, 3])
def test_aura_payoffs_need_an_early_carrier_for_an_automatic_keep(land_count):
    def hc(name, types, cost, text=""):
        return mp.hand_card({"name": name, "type_line": types, "mana_cost": cost, "oracle_text": text})

    land = hc("Plains", "Basic Land — Plains", "")
    aura = hc(
        "Unknown Blessing", "Enchantment — Aura", "{W}", "Enchant creature\nEnchanted creature gets +2/+2."
    )
    removal = hc("Answer", "Instant", "{W}", "Exile target tapped creature.")
    cards = [land] * land_count + [aura] * (6 - land_count) + [removal]
    found = mp.MulliganSituation(cards=cards, mulligans=0, on_play=False)
    assert mp.mulligan_verdict(found) is None
    cards[-2] = hc("Token Spell", "Sorcery", "{1}{W}", "Create two 1/1 white Soldier creature tokens.")
    assert mp.mulligan_verdict(found)[0] == "keep"


@pytest.mark.parametrize("visible_count", [0, 1, 6])
def test_mulligan_waits_for_complete_hand_without_model_or_count_change(visible_count):
    backend = Backend('{"option_ids": ["mull:mull"], "reasoning": "guess from deck"}')
    planner = ActionPlanner(backend, timeout=5, land_drop_first=False)
    game = state([MOUNTAIN] * 7, 0)
    game["hand"] = game["hand"][:visible_count]
    decision = PendingDecision(
        request_id=(3, 12),
        request_type="Mulligan",
        options=(DecisionOption("mull:keep", "Keep"), DecisionOption("mull:mull", "Mulligan")),
    )
    assert planner.plan_decision_options(decision, game) == []
    assert not backend.prompts
    assert getattr(planner, "_mulligan_track", None) is None
    # The request is still actionable as soon as the same hand is hydrated.
    hydrated = state(G1_SIX, 0)
    assert planner.plan_decision_options(decision, hydrated) == ["mull:keep"]
    assert not backend.prompts


# --- Q1 2026-11-08/09: two-land keeps on the draw need development ------------------------
# Two logged keeps under the old "2 lands on the draw with cheap plays" rule won
# 38%, vs 60% for comparable three-landers. Sources (evidence dir
# /home/joshu/fable-nov-evidence/arenamcp): standalone.log 2026-11-08 21:25 and
# 2026-11-09 21:18; match packets packet_20261108_213005_b55e160b*.json and
# packet_20261109_212045_a4ca1d46*.json; bug report bug_20261109_212434.json.
# Card text below is verbatim from those packets; Gemstone Caverns and Chrome
# Mox come from the repo fixtures (attack_tax_210_finale_public.json,
# nov6_damage_windows.json) because the packets reference them by name only.

GEMSTONE_CAVERNS = (
    "Gemstone Caverns",
    "Legendary Land",
    "",
    "If this card is in your opening hand and you're not the starting player, you may begin the game "
    "with Gemstone Caverns on the battlefield with a luck counter on it. If you do, exile a card from "
    "your hand.\n{oT}: Add {oC}. If Gemstone Caverns has a luck counter on it, instead add one mana of any color.",
    "rare",
)
WOODED_FOOTHSILLS = (
    "Wooded Foothills",
    "Land",
    "",
    "{oT}, Pay 1 life, Sacrifice this land: Search your library for a Mountain or Forest card, put it "
    "onto the battlefield, then shuffle.",
    "rare",
)
FLAME_SLASH = ("Flame Slash", "Sorcery", "{R}", "Flame Slash deals 4 damage to target creature.", "common")
SPARKS_ABLAZE = (
    "Sparks Ablaze",
    "Instant",
    "{R}",
    "Sparks Ablaze deals 2 damage to target creature or planeswalker. If that creature or planeswalker "
    "would die this turn, exile it instead.",
    "uncommon",
)
EXPLORATION = ("Exploration", "Enchantment", "{G}", "You may play an additional land on each of your turns.", "rare")
CHROME_MOX = (
    "Chrome Mox",
    "Artifact",
    "",
    "<i>Imprint</i> — When this artifact enters, you may exile a nonartifact, nonland card from your hand.\n"
    "{oT}: Add one mana of any of the exiled card's colors.",
    "rare",
)
GUT_TRUE_SOUL_ZEALOT = (
    "Gut True Soul Zealot",
    "Legendary Creature — Goblin Shaman",
    "{2}{R}",
    "Whenever you attack, you may sacrifice another creature or an artifact. If you do, create a 4/1 "
    "black Skeleton creature token with menace that's tapped and attacking.",
    "rare",
)
JARSYL_DARK_AGE_SCION = (
    "Jarsyl Dark Age Scion",
    "Legendary Creature — Human Wizard",
    "{1}{R}{G}",
    "Starting intensity 1\nAt the beginning of combat on your turn, you may cast a spell with mana value "
    "equal to Jarsyl Dark Age Scion's intensity from your graveyard without paying its mana cost.",
    "rare",
)
BLOOD_MOON = ("Blood Moon", "Enchantment", "{2}{R}", "Nonbasic lands are Mountains.", "rare")
FABLE_OF_THE_MIRROR_BREAKER = (
    "Fable of the Mirror-Breaker",
    "Enchantment — Saga",
    "{2}{R}",
    "(As this Saga enters and after each of your chapters, resolve its chapter.)\n"
    "I — Create two 1/1 red Goblin creature tokens.\nII, III — Create a Treasure token.",
    "rare",
)

# match a4ca1d46 (2026-11-09 21:20:45), on the draw: Gemstone Caverns, Wooded
# Foothills, Flame Slash, Sparks Ablaze, Exploration, Gut True Soul Zealot,
# Jarsyl Dark Age Scion.
FURY_GAME_HAND = [
    GEMSTONE_CAVERNS,
    WOODED_FOOTHSILLS,
    FLAME_SLASH,
    SPARKS_ABLAZE,
    EXPLORATION,
    GUT_TRUE_SOUL_ZEALOT,
    JARSYL_DARK_AGE_SCION,
]
# match b55e160b (2026-11-08 21:25), on the draw: Mountain, Gemstone Caverns,
# Flame Slash, Chrome Mox, Sparks Ablaze plus two non-cheap cards. The guard
# message names the cheap trio; Blood Moon is named in the packet's turn-1
# explanation and Fable of the Mirror-Breaker is from the same deck — both are
# mana value 3+, so the verdict below is the same for any such filler.
MOX_GAME_HAND = [
    ("Mountain", "Basic Land — Mountain", "", "({T}: Add {R}.)", "common"),
    GEMSTONE_CAVERNS,
    FLAME_SLASH,
    CHROME_MOX,
    SPARKS_ABLAZE,
    BLOOD_MOON,
    FABLE_OF_THE_MIRROR_BREAKER,
]


def logged_card(index, spec):
    name, type_line, cost, text, rarity = spec
    return {
        "instance_id": 400 + index,
        "grp_id": 0,
        "name": name,
        "type_line": type_line,
        "mana_cost": cost,
        "oracle_text": text,
        "rarity": rarity,
        "owner_seat_id": 2,
        "controller_seat_id": 2,
    }


def logged_state(specs, on_play=False):
    """Same shape as state(): seat 2 local, mulligan decision pending."""
    players = [{"seat_id": 2, "is_local": True, "life_total": 20}, {"seat_id": 1, "life_total": 20}]
    players[0]["mulligan_count"] = 0
    return {
        "match_id": "b55e160b" if specs is MOX_GAME_HAND else "a4ca1d46",
        "local_seat_id": 2,
        "pending_decision": "Mulligan",
        "decision_context": {"type": "mulligan"},
        "players": players,
        "turn": {"turn_number": 0, "active_player": 2 if on_play else 1},
        "hand": [logged_card(index, spec) for index, spec in enumerate(specs)],
        "battlefield": [],
    }


@pytest.mark.parametrize("specs", [FURY_GAME_HAND, MOX_GAME_HAND], ids=["a4ca1d46", "b55e160b"])
def test_logged_two_lander_burn_keeps_on_the_draw_go_to_the_model(specs):
    # Both were auto-kept by the old rule ("2 lands on the draw with cheap plays
    # (Flame Slash, ...)") and lost far more often than comparable three-landers.
    # Neither hand can develop its mana: Flame Slash / Sparks Ablaze are reactive,
    # Exploration needs green these lands cannot make (a4ca1d46), and Chrome Mox
    # eats a hand card (b55e160b). Q1 leaves these to the model.
    assert mp.mulligan_verdict(mp.situation(logged_state(specs))) is None


def test_logged_shape_with_a_mana_maker_still_autokeeps():
    # Same two-land-on-the-draw shape as a4ca1d46/b55e160b, but Llanowar Elves
    # taps for mana off the Forest: development, so the keep stands.
    choice, reason = verdict([FOREST, MOUNTAIN, 904, 902, 901, 903, 903], mulligans=0, on_play=False)
    assert choice == "keep"


def test_logged_shape_with_two_cheap_permanents_still_autokeeps():
    # Development clause two: two castable permanents of mana value 2 or less.
    choice, reason = verdict([FOREST, FOREST, 900, 900, 902, 901, 903], mulligans=0, on_play=False)
    assert choice == "keep"
