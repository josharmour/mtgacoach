"""Opening resources from Armour vs Raph & Mikey, before the next draw."""

from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.land_sequence import choose_land_sequence, landfall_color


def card(i, name, kind, text, cost=""):
    return dict(
        instance_id=i,
        name=name,
        type_line=kind,
        oracle_text=text,
        mana_cost=cost,
        controller_seat_id=2,
        owner_seat_id=2,
    )


def position():
    town = card(1, "Starting Town", "Land", "{T}: Add {C}.\n{T}, Pay 1 life: Add one mana of any color.")
    passage = card(
        2,
        "Multiversal Passage",
        "Land",
        "As this land enters, choose a basic land type. "
        "Then you may pay 2 life. If you don't, it enters tapped. This land is the chosen type.",
    )
    arena = card(
        3, "Arena of Glory", "Land", "This land enters tapped unless you control a Mountain.\n{T}: Add {R}."
    )
    cobra = card(
        4,
        "Lotus Cobra",
        "Creature",
        "Whenever a land you control enters, add one mana of any color.",
        "{1}{G}",
    )
    cmd = card(5, "Commander", "Legendary Planeswalker", "Rules.", "{2}{R}{G}")
    state = dict(
        match_id="game",
        turn=dict(turn_number=3, active_player=2, phase="Phase_Main1"),
        hand=[passage, arena, cobra],
        battlefield=[town],
        local_seat_id=2,
        command=[cmd],
        players=[dict(seat_id=2, is_local=True, life_total=25), dict(seat_id=1, life_total=25)],
    )

    def play(c):
        return DecisionOption(
            "idx:" + str(c["instance_id"]),
            c["name"],
            meta={"actionType": "ActionType_Play", "instanceId": c["instance_id"]},
        )

    decision = PendingDecision((42, 65), "ActionsAvailable", (play(arena), play(passage)))
    return state, decision


def test_two_known_lands_make_a_turn_three_commander():
    state, decision = position()
    pick, pending = choose_land_sequence(decision, state, None)
    assert pick[0] == "idx:2" and pending[2].kind == "Mountain" and pending[2].life == 2
    assert "Arena of Glory" in pick[1] and pending[2].target_id == 4
    state["players"][0]["life_total"] = 2
    assert choose_land_sequence(decision, state, None) == (None, None)


def test_landfall_funds_commander_even_with_a_tapped_land_and_preserves_colored_pips():
    state, decision = position()
    passage, arena, cobra = state["hand"]
    verge = card(6, "Verge", "Land", "{T}: Add {R}.")
    state.update(hand=[passage], battlefield=[*state["battlefield"], arena, verge, cobra])
    pick, pending = choose_land_sequence(decision, state, None)
    assert pick[0] == "idx:2" and pending[2].life == 0 and pending[2].now
    assert pending[2].target_id == 5
    state["hand"] = [card(7, "Curator", "Creature", "", "{G}{G}")]
    state["battlefield"] = [dict(passage, subtypes=["Forest"]), cobra]
    colors = PendingDecision(
        (134, 182),
        "SelectN",
        tuple(DecisionOption("sel:" + k, k, meta={"choice": "color", "color": k}) for k in "RG"),
        source_label="Lotus Cobra",
    )
    assert landfall_color(colors, state).option_id == "sel:G"


def test_first_land_pays_for_a_forest_aura_before_red_land():
    s, _ = position()
    red, forest, aura = (
        card(6, "Ring", "Land", "{T}: Add {R}."),
        card(7, "Forest", "Basic Land — Forest", ""),
        card(
            8,
            "Sprawl",
            "Enchantment — Aura",
            "Enchant Forest\nWhenever enchanted Forest is tapped for mana, its controller adds an additional one mana of the chosen color.",
            "{G}",
        ),
    )
    s.update(battlefield=[], hand=[red, forest, aura])
    play = PendingDecision(
        (6, 19),
        "ActionsAvailable",
        tuple(
            DecisionOption(
                str(c["instance_id"]),
                c["name"],
                meta={"actionType": "ActionType_Play", "instanceId": c["instance_id"]},
            )
            for c in (red, forest)
        ),
    )
    pick, pending = choose_land_sequence(play, s, None)
    assert pick[0] == "7" and pending[2].target_id == 8
