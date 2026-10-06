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


def test_two_lander_on_the_draw_with_a_cheap_play_keeps():
    assert verdict(hand_with(2, [902, 901, 901, 903, 903]), mulligans=0, on_play=False)[0] == "keep"


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
