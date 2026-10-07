"""Concede when dead no matter what: loss estimate, countdown, pipe, bridge, desktop.

The board is bug_20261006_180436 (FRA, T22, the opponent's Main1): we are at 3
life facing a 3/3 flying Thopter and 17 damage through our best blocks, with an
Island in hand and four untapped lands.
"""

from __future__ import annotations

from typing import Any

import pytest

from arenamcp.concede import ConcedeController, estimate_loss, normalize_phases
from arenamcp.gre_bridge import GREBridge, GREBridgeError
from arenamcp.mac_bridge_adapter import MacBridgeAdapter
from arenamcp.pipe_adapter import PipeAdapter
from test_all_in import BOARD
from test_draft_autoplay import Obj, World

# Our lands tapped during our turn 21, as in the report.
TAPPED = frozenset({255, 264, 279, 294, 305, 329})
PROFT_TEXT = (
    "Threshold — You can't cast this spell unless there are seven or more cards in your graveyard.\n"
    "Menace\n"
    "{oB}, Discard this card: Target creature gets -3/-1 until end of turn.\n"
    "{oB}, Discard this card: Target creature gets <nobr>-3/-1</nobr> until end of turn."
)
ISLAND = {
    "instance_id": 447,
    "name": "Island",
    "type_line": "Basic Land — Island",
    "oracle_text": "({T}: Add {U}.)",
    "mana_cost": "",
    "card_types": ["Land"],
    "owner_seat_id": 1,
    "controller_seat_id": 1,
}
DARKNESS = {
    "instance_id": 900,
    "name": "Darkness",
    "type_line": "Instant",
    "oracle_text": "Prevent all combat damage that would be dealt this turn.",
    "mana_cost": "{oB}",
    "card_types": ["Instant"],
    "owner_seat_id": 1,
    "controller_seat_id": 1,
}


def bug_state(
    life: int = 3,
    *,
    phase: str = "Phase_Main1",
    step: str = "",
    active: int = 2,
    hand: list[dict] | None = None,
    tapped: frozenset[int] = TAPPED,
    opp_life: int = 18,
    turn: int = 22,
) -> dict[str, Any]:
    battlefield = []
    for iid, name, ctrl, power, toughness, type_line, text in BOARD:
        battlefield.append(
            {
                "instance_id": iid,
                "name": name,
                "controller_seat_id": ctrl,
                "owner_seat_id": ctrl,
                "power": power,
                "toughness": toughness,
                "type_line": type_line,
                "oracle_text": PROFT_TEXT if name.startswith("Proft") else text,
                "keywords": ["flying"] if name == "Thopter" else [],
                "is_tapped": iid in tapped,
                "turn_entered_battlefield": 1,
            }
        )
    return {
        "match_id": "fra-3da54de9",
        "local_seat_id": 1,
        "opponent_seat_id": 2,
        "turn": {
            "turn_number": turn,
            "active_player": active,
            "priority_player": active,
            "phase": phase,
            "step": step,
        },
        "players": [
            {"seat_id": 1, "life_total": life, "is_local": True, "status": "InGame", "mana_pool": {}},
            {"seat_id": 2, "life_total": opp_life, "is_local": False, "status": "InGame", "mana_pool": {}},
        ],
        "battlefield": battlefield,
        "hand": [dict(ISLAND)] if hand is None else hand,
        "graveyard": [],
        "exile": [],
        "stack": [],
        "action_history": [],
        "pending_decision": None,
        "controlled_turn": {"opponent_controlled_by_you": False, "you_controlled_by_opponent": False},
        "zones": {
            "library_count": 12,
            "library_count_source": "log_zone_membership",
            "opponent_hand_count": 1,
        },
    }


def swamps_tapped() -> frozenset[int]:
    return TAPPED | {355, 464}


# --- the loss estimate ------------------------------------------------------------


@pytest.mark.parametrize("phase, step", [("Phase_Main1", ""), ("Main1", "None")])
def test_the_bug_board_on_their_turn_is_a_concede(phase, step):
    estimate = estimate_loss(bug_state(phase=phase, step=step))
    assert estimate.confidence >= 0.95, estimate.blockers
    assert estimate.facts["through"] == 17 and estimate.facts["evasive_power"] == 3
    assert estimate.facts["outs"] == []  # Proft's "Discard this card" works only from hand
    assert estimate.reason.startswith("Concede: their 3/3 flying Thopter and 14 more damage")
    assert "your 3 life" in estimate.reason


def test_an_untapped_instant_that_could_save_us_is_an_out():
    saved = estimate_loss(bug_state(hand=[dict(ISLAND), dict(DARKNESS)]))
    assert saved.confidence < 0.95
    assert any("Darkness" in why for why in saved.blockers)
    # Without black mana it can't be cast: dead again.
    tapped_out = estimate_loss(bug_state(hand=[dict(ISLAND), dict(DARKNESS)], tapped=swamps_tapped()))
    assert tapped_out.confidence >= 0.95


def test_proft_in_hand_is_an_out_but_not_on_the_battlefield():
    proft = next(dict(c) for c in bug_state()["battlefield"] if c["name"].startswith("Proft"))
    proft.update(instance_id=901, is_tapped=False)
    estimate = estimate_loss(bug_state(hand=[proft]))
    assert estimate.confidence < 0.95 and any("from hand" in out for out in estimate.facts["outs"])


def test_card_draw_and_sorcery_speed_cards_are_not_outs_on_their_turn():
    draw = dict(
        DARKNESS, instance_id=902, name="Quick Study", oracle_text="Draw two cards.", mana_cost="{oU}"
    )
    sorcery = dict(
        DARKNESS,
        instance_id=903,
        name="Doom",
        type_line="Sorcery",
        card_types=["Sorcery"],
        oracle_text="Destroy target creature.",
    )
    # One untapped Island: the draw spell leaves nothing to cast what it finds.
    estimate = estimate_loss(bug_state(hand=[draw, sorcery], tapped=swamps_tapped() | {403}))
    assert estimate.confidence >= 0.95, estimate.blockers
    # With an Island to spare it could dig for an answer.
    assert estimate_loss(bug_state(hand=[draw, sorcery], tapped=swamps_tapped())).confidence < 0.95


def test_a_castable_flash_blocker_is_an_out():
    flier = dict(
        DARKNESS,
        instance_id=904,
        name="Spectral Sailor",
        type_line="Creature — Spirit Pirate",
        card_types=["Creature"],
        oracle_text="Flash\nFlying",
        mana_cost="{oU}",
        power=1,
        toughness=1,
    )
    assert estimate_loss(bug_state(hand=[flier])).confidence < 0.95


def test_a_sorcery_speed_graveyard_ability_counts_only_in_our_main_phase():
    adept = {
        "instance_id": 905,
        "name": "Cryotheory Adept",
        "type_line": "Creature — Human Wizard",
        "oracle_text": "Prowess\n{o3oU}, Exile this card from your graveyard: Tap target creature and put a "
        "stun counter on it. Activate only as a sorcery.",
        "owner_seat_id": 1,
        "controller_seat_id": 1,
    }
    theirs = bug_state()
    theirs["graveyard"] = [adept]
    assert estimate_loss(theirs).confidence >= 0.95
    ours = bug_state(active=1, phase="Phase_Main2", tapped=frozenset())
    ours["graveyard"] = [adept]
    assert estimate_loss(ours).confidence < 0.95


def test_lethal_for_us_means_never_concede():
    state = bug_state(active=1, phase="Phase_Main1", opp_life=4, tapped=frozenset())
    for card in state["battlefield"]:
        if card["controller_seat_id"] == 2:
            card["is_tapped"] = True
    estimate = estimate_loss(state)
    assert estimate.confidence == 0.0 and estimate.reason == "we have lethal on board"


@pytest.mark.parametrize("life", [12, 20])
def test_a_healthy_life_total_is_never_a_concede(life):
    assert estimate_loss(bug_state(life)).confidence == 0.0


def test_their_main2_waits_for_our_untap_and_draw():
    estimate = estimate_loss(bug_state(phase="Phase_Main2"))
    assert estimate.confidence < 0.95 and "draw" in estimate.reason


def test_a_spell_on_the_stack_holds_off():
    state = bug_state()
    state["stack"] = [{"instance_id": 999, "name": "Something", "controller_seat_id": 2}]
    assert estimate_loss(state).confidence < 0.95


def test_our_turn_only_after_our_attack_could_not_win():
    # Before combat our 23 power could kill them unblocked (18 life): play on.
    assert estimate_loss(bug_state(active=1, phase="Phase_Main1", tapped=frozenset())).confidence == 0.0
    # After combat (Main2) their crackback still kills us: concede.
    after = estimate_loss(bug_state(active=1, phase="Phase_Main2", tapped=frozenset()))
    assert after.confidence >= 0.95 and "their next attack" in after.reason


@pytest.mark.parametrize(
    "change",
    [
        {"match_id": None},
        {"pending_decision": "Intermission"},
        {"_bridge_in_intermission": True},
        {"turn": {"turn_number": 22, "active_player": 2, "phase": "Main1", "stage": "GameOver"}},
    ],
)
def test_not_a_live_game_is_never_a_concede(change):
    state = bug_state()
    state.update(change)
    assert estimate_loss(state).confidence == 0.0


def test_normalize_phases_maps_bridge_names():
    state = bug_state(phase="Main1", step="None")
    assert normalize_phases(state)["turn"]["phase"] == "Phase_Main1"
    assert normalize_phases(state)["turn"]["step"] == ""
    combat = bug_state(phase="Combat", step="DeclareAttack")
    assert normalize_phases(combat)["turn"]["step"] == "Step_DeclareAttack"
    logged = bug_state()
    assert normalize_phases(logged) is logged


# --- review regressions: the best case for us -------------------------------------
#
# Small boards: a creature row, our hand, and what we did this turn. Each case
# was a 0.97 "concede" before the fix.


def creature(iid, name, ctrl, power, toughness, *, keywords=(), text="", tapped=False, entered=1, **extra):
    card = {
        "instance_id": iid,
        "name": name,
        "controller_seat_id": ctrl,
        "owner_seat_id": extra.pop("owner", ctrl),
        "power": power,
        "toughness": toughness,
        "type_line": extra.pop("type_line", "Creature — Beast"),
        "oracle_text": text,
        "keywords": list(keywords),
        "is_tapped": tapped,
        "turn_entered_battlefield": entered,
    }
    card.update(extra)
    return card


def permanent(iid, name, ctrl, type_line, text, **extra):
    card = {
        "instance_id": iid,
        "name": name,
        "controller_seat_id": ctrl,
        "owner_seat_id": ctrl,
        "type_line": type_line,
        "oracle_text": text,
        "is_tapped": False,
        "turn_entered_battlefield": 1,
    }
    card.update(extra)
    return card


def basic(iid, ctrl=1, kind="Island", tapped=False):
    symbol = {"Island": "U", "Mountain": "R", "Swamp": "B", "Plains": "W", "Forest": "G"}[kind]
    return permanent(iid, kind, ctrl, f"Basic Land — {kind}", f"({{T}}: Add {{{symbol}}}.)", is_tapped=tapped)


def spell(iid, name, type_line, cost, text, **extra):
    card = {
        "instance_id": iid,
        "name": name,
        "type_line": type_line,
        "mana_cost": cost,
        "oracle_text": text,
        "owner_seat_id": 1,
        "controller_seat_id": 1,
    }
    card.update(extra)
    return card


def board(
    battlefield,
    *,
    life=3,
    opp_life=20,
    active=2,
    phase="Phase_Main1",
    step="",
    turn=10,
    hand=(),
    graveyard=(),
    command=(),
    history=(),
):
    return {
        "match_id": "m-1",
        "local_seat_id": 1,
        "opponent_seat_id": 2,
        "turn": {
            "turn_number": turn,
            "active_player": active,
            "priority_player": active,
            "phase": phase,
            "step": step,
        },
        "players": [
            {"seat_id": 1, "life_total": life, "status": "InGame", "mana_pool": {}},
            {"seat_id": 2, "life_total": opp_life, "status": "InGame", "mana_pool": {}},
        ],
        "battlefield": list(battlefield),
        "hand": list(hand),
        "graveyard": list(graveyard),
        "exile": [],
        "command": list(command),
        "stack": [],
        "action_history": list(history),
        "pending_decision": None,
        "zones": {"library_count": 20, "opponent_hand_count": 2},
    }


FLYING = ("flying",)
HOLY_DAY = spell(50, "Holy Day", "Instant", "{W}", "Prevent all combat damage that would be dealt this turn.")
FROST_BREATH = spell(
    51,
    "Frost Breath",
    "Instant",
    "{2}{U}",
    "Tap up to two target creatures. Those creatures don't untap during their controller's next untap step.",
)


def cast(name, turn=10, seat=1, action="Cast"):
    return {"turn": turn, "phase": "Phase_Main1", "seat": seat, "action": action, "card": name}


def test_a_fog_that_already_resolved_this_turn_holds_off():
    their_board = [creature(10, "Drake", 2, 3, 3, keywords=FLYING), creature(11, "Bear", 2, 4, 4)]
    fogged = board(their_board, graveyard=[HOLY_DAY], history=[cast("Holy Day")])
    estimate = estimate_loss(fogged)
    assert estimate.confidence < 0.95 and "Holy Day" in estimate.reason
    # The same card cast on an earlier turn protects nothing now.
    old = board(their_board, graveyard=[HOLY_DAY], history=[cast("Holy Day", turn=8)])
    assert estimate_loss(old).confidence >= 0.95
    # The opponent's casts are theirs, not ours.
    theirs = board(their_board, graveyard=[HOLY_DAY], history=[cast("Holy Day", seat=2)])
    assert estimate_loss(theirs).confidence >= 0.95


def test_the_bug_board_after_darkness_resolved_never_concedes():
    darkness = dict(DARKNESS)
    state = bug_state(hand=[dict(ISLAND)])
    state["graveyard"] = [darkness]
    state["action_history"] = [cast("Darkness", turn=22)]
    for phase, step in [
        ("Phase_Main1", ""),
        ("Phase_Combat", "Step_BeginCombat"),
        ("Phase_Combat", "Step_DeclareAttack"),
    ]:
        state["turn"].update(phase=phase, step=step)
        assert estimate_loss(state).confidence < 0.95
    harness = Harness(state)
    harness.observe()
    assert harness.threads == [] and harness.concedes == []


def test_frost_breath_this_turn_or_last_turn_holds_off():
    drakes = [
        creature(10, "Drake", 2, 4, 4, keywords=FLYING, tapped=True),
        creature(11, "Drake", 2, 4, 4, keywords=FLYING, tapped=True),
    ]
    ours = board(
        drakes,
        life=5,
        active=1,
        phase="Phase_Main2",
        graveyard=[FROST_BREATH],
        history=[cast("Frost Breath")],
    )
    assert estimate_loss(ours).confidence < 0.95
    # Cast in their end step (turn 9): on our turn 10 they still won't untap.
    late = board(
        drakes,
        life=5,
        active=1,
        phase="Phase_Main2",
        graveyard=[FROST_BREATH],
        history=[cast("Frost Breath", turn=9)],
    )
    assert estimate_loss(late).confidence < 0.95
    # A card we can't find (no text): unknown effect this turn.
    unknown = board(drakes, life=5, active=1, phase="Phase_Main2", history=[cast("Mystery Spell")])
    assert estimate_loss(unknown).confidence < 0.95
    # Mana abilities and land drops are not effects.
    mana = board(
        drakes,
        life=5,
        active=1,
        phase="Phase_Main2",
        history=[cast("Island", action="Activate_Mana"), cast("Island", action="Play")],
    )
    assert estimate_loss(mana).confidence >= 0.95


BLOSSOMBIND = (
    "Enchant creature\nWhen this Aura enters, tap enchanted creature.\n"
    "Enchanted creature can't become untapped and can't have counters put on it."
)


def test_auras_that_keep_a_creature_tapped_or_home():
    drakes = [
        creature(10, "Drake", 2, 4, 4, keywords=FLYING, tapped=True),
        creature(11, "Drake", 2, 4, 4, keywords=FLYING, tapped=True),
    ]
    auras = [
        permanent(20, "Blossombind", 1, "Enchantment — Aura", BLOSSOMBIND, attached_to_id=10),
        permanent(21, "Blossombind", 1, "Enchantment — Aura", BLOSSOMBIND, attached_to_id=11),
    ]
    assert estimate_loss(board(drakes + auras, life=5, active=1, phase="Phase_Main2")).confidence < 0.95
    exerted = [
        creature(
            12,
            "Tah-Crop Elite",
            2,
            8,
            8,
            keywords=FLYING,
            tapped=True,
            text="Flying\nYou may exert ~ as it attacks.",
        )
    ]
    assert estimate_loss(board(exerted, life=5, active=1, phase="Phase_Main2")).confidence < 0.95
    # Log mode before attachments were parsed: an unlinked Pacifism goes on
    # their most dangerous creature, so the 6/6 flyer stays home.
    angel = creature(30, "Serra Angel", 2, 6, 6, keywords=FLYING)
    pacifism = permanent(
        31, "Pacifism", 1, "Enchantment — Aura", "Enchant creature\nEnchanted creature can't attack or block."
    )
    estimate = estimate_loss(board([angel, pacifism], life=4))
    assert estimate.confidence < 0.95
    claustrophobia = permanent(
        32,
        "Claustrophobia",
        1,
        "Enchantment — Aura",
        "Enchant creature\nWhen Claustrophobia enters, tap enchanted creature.\n"
        "Enchanted creature doesn't untap during its controller's untap step.",
    )
    tapped_angel = dict(angel, is_tapped=True)
    assert (
        estimate_loss(board([tapped_angel, claustrophobia], life=4, active=1, phase="Phase_Main2")).confidence
        < 0.95
    )


LILIANA = permanent(40, "Liliana", 1, "Legendary Planeswalker — Liliana", "+1: Each player discards a card.")


def test_attackers_aimed_elsewhere_do_not_hit_us():
    attack = dict(phase="Phase_Combat", step="Step_DeclareBlock")
    at_walker = creature(10, "Big", 2, 6, 6, is_attacking=True, attack_target_id=40, tapped=True)
    at_us = creature(11, "Small", 2, 2, 2, is_attacking=True, attack_target_id=1, tapped=True)
    estimate = estimate_loss(board([at_walker, at_us, LILIANA], **attack))
    assert estimate.confidence == 0.0  # 2 damage reaches us at 3 life
    battle = permanent(41, "Invasion of Somewhere", 2, "Battle — Siege", "When this enters, ...")
    at_battle = dict(at_walker, attack_target_id=41)
    assert estimate_loss(board([at_battle, at_us, battle], **attack)).confidence == 0.0
    # Target unknown while a planeswalker is out: maybe not us.
    unknown = [dict(at_walker, attack_target_id=None), dict(at_us, attack_target_id=None), LILIANA]
    assert estimate_loss(board(unknown, **attack)).confidence < 0.95
    # Everything at us: dead.
    all_at_us = [dict(at_walker, attack_target_id=1), at_us, LILIANA]
    assert estimate_loss(board(all_at_us, **attack)).confidence >= 0.95


def test_the_cheap_face_of_an_adventure_card_is_an_out():
    drake = creature(10, "Drake", 2, 4, 4, keywords=FLYING)
    bonecrusher = spell(
        60,
        "Bonecrusher Giant",
        "Creature — Giant // Instant — Adventure",
        "{2}{R} // {1}{R}",
        "Whenever this creature becomes the target of a spell, this creature deals 2 damage to that "
        "spell's controller.\n---\nDamage can't be prevented this turn. Stomp deals 2 damage to any target.",
        power=4,
        toughness=3,
    )
    mountains = [basic(1, kind="Mountain"), basic(2, kind="Mountain")]
    estimate = estimate_loss(board([drake, *mountains], opp_life=2, hand=[bonecrusher]))
    assert estimate.confidence < 0.95 and "Bonecrusher Giant" in estimate.facts["outs"][0]
    borrower = spell(
        61,
        "Brazen Borrower",
        "Creature — Faerie Rogue // Instant — Adventure",
        "{1}{U}{U} // {1}{U}",
        "Flash\nFlying\nThis creature can block only creatures with flying.\n---\n"
        "Return target nonland permanent an opponent controls to its owner's hand.",
        power=3,
        toughness=1,
    )
    assert estimate_loss(board([drake, basic(1), basic(2)], hand=[borrower])).confidence < 0.95
    # One land: neither face is castable.
    assert (
        estimate_loss(board([drake, basic(1, kind="Mountain")], opp_life=2, hand=[bonecrusher])).confidence
        >= 0.95
    )


HULLBREAKER = (
    "Flash\nThis spell can't be countered.\nWhenever you cast a spell, choose up to one —\n"
    "• Return target spell you don't control to its owner's hand.\n• Return target nonland permanent to its owner's hand."
)


def test_a_flash_commander_in_the_command_zone_is_an_out():
    attackers = [
        creature(10, "Huge", 2, 8, 8, is_attacking=True, tapped=True, attack_target_id=1),
        creature(11, "Small", 2, 2, 2, is_attacking=True, tapped=True, attack_target_id=1),
    ]
    islands = [basic(100 + i) for i in range(7)]
    horror = spell(
        70, "Hullbreaker Horror", "Creature — Kraken Horror", "{5}{U}{U}", HULLBREAKER, power=7, toughness=8
    )
    state = board(
        attackers + islands, life=5, phase="Phase_Combat", step="Step_DeclareAttack", command=[horror]
    )
    estimate = estimate_loss(state)
    assert estimate.confidence < 0.95 and any("command zone" in out for out in estimate.facts["outs"])
    # Six Islands can't pay for it.
    state["battlefield"] = attackers + islands[:6]
    assert estimate_loss(state).confidence >= 0.95


SHARK_TYPHOON = (
    "Whenever you cast a noncreature spell, create an X/X blue Shark creature token with flying, where X is "
    "that spell's mana value.\nCycling {X}{1}{U} ({X}{1}{U}, Discard this card: Draw a card.)\n"
    "When you cycle this card, create an X/X blue Shark creature token with flying."
)


def test_cycling_that_makes_a_blocker_is_an_out():
    drake = creature(10, "Drake", 2, 4, 4, keywords=FLYING)
    shark = spell(60, "Shark Typhoon", "Enchantment", "{5}{U}", SHARK_TYPHOON)
    islands = [basic(100 + i) for i in range(3)]
    estimate = estimate_loss(board([drake, *islands], hand=[shark]))
    assert estimate.confidence < 0.95 and "from hand" in estimate.facts["outs"][0]


def test_the_first_strike_damage_step_is_never_enough():
    knight = creature(
        10, "Knight", 2, 5, 5, keywords=("first strike",), is_attacking=True, tapped=True, attack_target_id=1
    )
    estimate = estimate_loss(board([knight], phase="Phase_Combat", step="Step_FirstStrikeDamage"))
    assert estimate.confidence < 0.95 and "first-strike" in estimate.reason


def test_creatures_that_may_be_summoning_sick_stay_home():
    stolen = creature(10, "Dragon", 2, 5, 5, keywords=FLYING, owner=1, entered=3, summoning_sickness=True)
    assert estimate_loss(board([stolen], life=4)).confidence == 0.0
    owner_differs = creature(10, "Dragon", 2, 5, 5, keywords=FLYING, owner=1, entered=3)
    assert estimate_loss(board([owner_differs], life=4)).confidence == 0.0
    just_cast = creature(10, "Dragon", 2, 5, 5, keywords=FLYING, entered=-1, summoning_sickness=True)
    assert estimate_loss(board([just_cast], life=4)).confidence == 0.0
    hasty = dict(just_cast, keywords=["flying", "haste"])
    assert estimate_loss(board([hasty], life=4)).confidence >= 0.95
    settled = creature(10, "Dragon", 2, 5, 5, keywords=FLYING, entered=3)
    assert estimate_loss(board([settled], life=4)).confidence >= 0.95


def test_our_attack_can_deal_more_than_its_power():
    drake = creature(12, "Drake", 2, 6, 6, keywords=FLYING)
    wall = creature(11, "Wall", 2, 0, 1, keywords=("defender",))
    striker = creature(10, "Striker", 1, 5, 5, keywords=("double strike",))
    estimate = estimate_loss(board([striker, wall, drake], opp_life=8, active=1))
    assert estimate.confidence == 0.0 and estimate.facts["our_unblocked_power"] == 10
    pinger = creature(
        10, "Pinger", 1, 1, 1, text="Whenever this creature attacks, it deals 3 damage to each opponent."
    )
    assert estimate_loss(board([pinger, drake], opp_life=3, active=1)).confidence < 0.95
    hound = creature(
        10,
        "Hound",
        1,
        1,
        1,
        entered=10,
        text="Landfall — Whenever a land you control enters, this creature deals 1 damage to each opponent.",
    )
    mountain = basic(80, kind="Mountain")
    landfall = estimate_loss(board([hound, drake], opp_life=1, active=1, hand=[mountain]))
    assert landfall.confidence < 0.95 and any("landfall" in out for out in landfall.facts["outs"])


def test_a_pump_on_our_turn_wears_off_before_their_attack():
    faerie = creature(10, "Faerie", 2, 5, 5, keywords=FLYING, printed_power=2, printed_toughness=2)
    tapped_out = [basic(1, tapped=True)]
    assert (
        estimate_loss(board([faerie, *tapped_out], life=4, active=1, phase="Phase_Main2")).confidence == 0.0
    )
    # +1/+1 counters are permanent.
    countered = dict(faerie, counters={"CounterType_P1P1": 3})
    assert (
        estimate_loss(board([countered, *tapped_out], life=4, active=1, phase="Phase_Main2")).confidence
        >= 0.95
    )
    # Printed stats unknown but the snapshot says it was modified: maybe temporary.
    unknown = creature(10, "Faerie", 2, 5, 5, keywords=FLYING, modified_power=5)
    assert (
        estimate_loss(board([unknown, *tapped_out], life=4, active=1, phase="Phase_Main2")).confidence < 0.95
    )
    # On their turn a pump lasts through their combat: still dead.
    assert estimate_loss(board([faerie], life=4)).confidence >= 0.95


def test_mana_rocks_lands_that_tap_for_more_and_phyrexian_mana():
    drake = creature(10, "Drake", 2, 5, 5, keywords=FLYING)
    lotus = permanent(
        1,
        "Lotus Field",
        1,
        "Land",
        "Hexproof\nThis land enters tapped.\nWhen this land enters, sacrifice two lands.\n{T}: Add three mana of any one color.",
    )
    murder = spell(60, "Murder", "Instant", "{1}{B}{B}", "Destroy target creature.")
    assert estimate_loss(board([drake, lotus], life=4, hand=[murder])).confidence < 0.95
    sol_ring = permanent(2, "Sol Ring", 1, "Artifact", "{T}: Add {C}{C}.")
    divination_cost = spell(61, "Cancel Attack", "Instant", "{2}", "Tap target creature.")
    assert estimate_loss(board([drake, sol_ring], life=4, hand=[divination_cost])).confidence < 0.95
    gut_shot = spell(
        62,
        "Gut Shot",
        "Instant",
        "{R/P}",
        "({R/P} can be paid with either {R} or 2 life.)\nGut Shot deals 1 damage to any target.",
    )
    glass_drake = creature(10, "Drake", 2, 4, 1, keywords=FLYING)
    assert estimate_loss(board([glass_drake], life=4, hand=[gut_shot])).confidence < 0.95
    # At 2 life the 2 life would kill us: not an out.
    assert estimate_loss(board([glass_drake], life=2, hand=[gut_shot])).confidence >= 0.95


def test_a_blocker_that_blocks_many_attackers_holds_off():
    bears = [creature(10 + i, f"Bear {i}", 2, 2, 2) for i in range(4)]
    guardian = creature(30, "Guardian", 1, 1, 4, text="This creature can block any number of creatures.")
    assert estimate_loss(board([*bears, guardian], life=4)).confidence < 0.95


def test_a_dig_with_mana_to_spare_holds_off():
    drake = creature(10, "Drake", 2, 4, 4, keywords=FLYING)
    opt = spell(60, "Opt", "Instant", "{U}", "Scry 1.\nDraw a card.")
    spare = estimate_loss(board([drake, *[basic(100 + i) for i in range(4)]], hand=[opt]))
    assert spare.confidence < 0.95 and "dig" in spare.reason
    # One Island: Opt leaves no mana for what it finds.
    assert estimate_loss(board([drake, basic(100)], hand=[opt])).confidence >= 0.95


def test_the_reason_reads_naturally():
    drake = creature(10, "Drake", 2, 4, 4, keywords=FLYING)
    assert "their 4/4 flying Drake gets through" in estimate_loss(board([drake])).reason


# --- log mode: what Player.log says about attachments, attacks and sickness --------


def _gre_object(iid, grp, zone, seat, **extra):
    data = {
        "instanceId": iid,
        "grpId": grp,
        "type": "GameObjectType_Card",
        "zoneId": zone,
        "visibility": "Visibility_Public",
        "ownerSeatId": seat,
        "controllerSeatId": seat,
        "cardTypes": ["CardType_Creature"],
        "power": {"value": 3},
        "toughness": {"value": 3},
    }
    data.update(extra)
    return data


def test_log_attachments_link_auras_like_the_bridge_does():
    from arenamcp.gamestate import GameState

    state = GameState()
    state.update_from_message(
        {
            "type": "GameStateType_Full",
            "turnInfo": {"turnNumber": 5, "activePlayer": 2, "phase": "Phase_Main1"},
            "gameObjects": [
                _gre_object(238, 1001, 28, 2),
                _gre_object(324, 1002, 28, 1, cardTypes=["CardType_Enchantment"], subtypes=["SubType_Aura"]),
            ],
            "zones": [{"zoneId": 28, "type": "ZoneType_Battlefield", "objectInstanceIds": [238, 324]}],
            "persistentAnnotations": [
                {"id": 811, "affectorId": 324, "affectedIds": [238], "type": ["AnnotationType_Attachment"]}
            ],
        }
    )
    battlefield = {c["instance_id"]: c for c in state.get_published_snapshot()["zones"]["battlefield"]}
    assert battlefield[324]["attached_to_id"] == 238 and "attached_to_id" not in battlefield[238]
    state.update_from_message({"type": "GameStateType_Diff", "diffDeletedPersistentAnnotationIds": [811]})
    battlefield = {c["instance_id"]: c for c in state.get_published_snapshot()["zones"]["battlefield"]}
    assert "attached_to_id" not in battlefield[324]


def test_log_attack_targets_and_summoning_sickness():
    from arenamcp.gamestate import GameState

    state = GameState()
    state.update_from_message(
        {
            "type": "GameStateType_Full",
            "turnInfo": {"turnNumber": 5, "activePlayer": 2, "phase": "Phase_Combat"},
            "gameObjects": [
                _gre_object(
                    10, 1001, 28, 2, attackState="AttackState_Attacking", attackInfo={"targetId": 40}
                ),
                _gre_object(11, 1002, 28, 2, hasSummoningSickness=True),
            ],
            "zones": [{"zoneId": 28, "type": "ZoneType_Battlefield", "objectInstanceIds": [10, 11]}],
        }
    )
    cards = {c["instance_id"]: c for c in state.get_published_snapshot()["zones"]["battlefield"]}
    assert cards[10]["attack_target_id"] == 40 and cards[11]["summoning_sickness"] is True
    # A full object without the flag: no longer sick.
    state.update_from_message({"type": "GameStateType_Diff", "gameObjects": [_gre_object(11, 1002, 28, 2)]})
    cards = {c["instance_id"]: c for c in state.get_published_snapshot()["zones"]["battlefield"]}
    assert "summoning_sickness" not in cards[11]
    # Sickness also ends when its controller's turn begins.
    state.update_from_message(
        {
            "type": "GameStateType_Diff",
            "gameObjects": [_gre_object(11, 1002, 28, 2, hasSummoningSickness=True)],
        }
    )
    state.update_from_message(
        {"type": "GameStateType_Diff", "turnInfo": {"turnNumber": 7, "activePlayer": 2}}
    )
    cards = {c["instance_id"]: c for c in state.get_published_snapshot()["zones"]["battlefield"]}
    assert "summoning_sickness" not in cards[11]


# --- the countdown ----------------------------------------------------------------


class Harness:
    def __init__(
        self,
        state: dict,
        *,
        autopilot: bool = True,
        bridge: bool = True,
        result: Any = None,
        ends_on_concede: bool = True,
    ):
        self.state = state
        self.autopilot = autopilot
        self.bridge = bridge
        self.game_over = False
        self.ends_on_concede = ends_on_concede
        self.settings: dict[str, Any] = {}
        self.result = {"ok": True} if result is None else result
        self.advice: list[str] = []
        self.announced: list[str] = []
        self.events: list[dict] = []
        self.concedes: list[dict] = []
        self.threads: list = []
        self.waits: list[float] = []
        self.on_concede = None
        self.on_announce = None
        self.now = 0.0
        self.controller = ConcedeController(
            get_state=lambda: self.state,
            concede_fn=self._concede,
            advise=lambda text, state=None: self.advice.append(text),
            emit=self._emit,
            settings_get=lambda key, default=None: self.settings.get(key, default),
            autopilot_on=lambda: self.autopilot,
            bridge_ready=lambda: self.bridge,
            game_over=lambda: self.game_over,
            announce=self._announce,
            start_thread=lambda target, armed: self.threads.append((target, armed)),
            wait=self._wait,
            clock=lambda: self.now,
        )
        self.on_emit = None

    def _emit(self, payload: dict) -> None:
        self.events.append(payload)
        if self.on_emit:
            self.on_emit(payload)

    def _announce(self, text: str, state: Any, cancelled: Any) -> bool:
        # The countdown must not have started while the offer is being heard.
        assert "armed" not in self.states
        self.announced.append(text)
        if self.on_announce:
            self.on_announce()
        return not cancelled()

    def _wait(self, event: Any, seconds: float) -> bool:
        self.waits.append(seconds)
        return event.is_set()

    def _concede(self, state: dict) -> Any:
        self.concedes.append(state)
        if self.on_concede:
            self.on_concede()
        if self.ends_on_concede and self.result.get("ok"):
            self.game_over = True
        return self.result

    def observe(self, key: Any = ("fra", 1), times: int = 2) -> None:
        """Observe like the coaching loop: the verdict must hold on two polls."""
        for _ in range(times):
            self.now += 1.0
            self.controller.observe(self.state, key)

    def run_countdown(self) -> None:
        target, armed = self.threads.pop(0)
        target(armed)

    @property
    def states(self) -> list[str]:
        return [event["state"] for event in self.events]


def test_one_snapshot_is_not_enough():
    harness = Harness(bug_state(), autopilot=False)
    harness.observe(times=1)
    assert harness.advice == []
    harness.now += 0.1  # too soon: the same poll burst
    harness.controller.observe(harness.state, ("fra", 1))
    assert harness.advice == []
    harness.observe(times=1)
    assert len(harness.advice) == 1


def test_autopilot_off_recommends_once_and_never_arms():
    harness = Harness(bug_state(), autopilot=False)
    harness.observe()
    harness.observe()
    assert len(harness.advice) == 1 and harness.advice[0].startswith("Concede:")
    assert "Auto-concede" not in harness.advice[0]
    assert harness.events == [] and harness.threads == [] and harness.concedes == []


def test_autopilot_on_concedes_after_the_offer_is_heard_and_the_countdown():
    harness = Harness(bug_state())
    harness.observe()
    assert harness.states == ["offering"] and harness.events[0]["seconds"] == 10
    assert harness.advice == [] and harness.announced == []  # spoken on the countdown thread
    harness.run_countdown()
    text = harness.announced[0]
    assert text.startswith("Concede: their 3/3 flying Thopter") and text.endswith(
        "Auto-concede in 10 seconds — say cancel or press Cancel to keep playing."
    )
    assert harness.waits[0] == 10 and len(harness.concedes) == 1
    assert harness.states == ["offering", "armed", "conceding", "sent", "conceded"]


def test_cancel_while_the_offer_is_spoken_stops_everything():
    harness = Harness(bug_state())
    harness.on_announce = lambda: harness.controller.cancel("stop_speech")
    harness.observe()
    harness.run_countdown()
    assert harness.states == ["offering", "cancelled"] and harness.waits == [] and harness.concedes == []


def test_cancel_stops_the_countdown_for_the_rest_of_the_game():
    harness = Harness(bug_state())
    harness.observe()
    assert harness.controller.cancel("ui") is True
    harness.run_countdown()
    assert harness.concedes == [] and harness.states == ["offering", "cancelled"]
    harness.observe()  # same game: no new recommendation or countdown
    assert harness.advice == [] and harness.threads == []
    assert harness.controller.cancel("ui") is False


def test_a_confidence_drop_aborts():
    harness = Harness(bug_state())
    harness.observe()
    harness.state = bug_state(20)
    harness.run_countdown()
    assert harness.concedes == []
    assert harness.states[-1] == "aborted" and "dropped" in harness.events[-1]["reason"]


def test_the_coaching_loop_aborts_when_confidence_drops():
    harness = Harness(bug_state())
    harness.observe()
    harness.state = bug_state(hand=[dict(ISLAND), dict(DARKNESS)])
    harness.observe(times=1)
    assert harness.states == ["offering", "aborted"]
    harness.run_countdown()  # the stale worker does nothing
    assert harness.concedes == [] and harness.states == ["offering", "aborted"]


@pytest.mark.parametrize("ending", ["event", "life"])
def test_game_over_aborts(ending):
    harness = Harness(bug_state())
    harness.observe()
    if ending == "event":
        harness.game_over = True
    else:
        harness.state = bug_state(0)
    harness.run_countdown()
    assert harness.concedes == [] and harness.states[-1] == "aborted"


def test_our_new_turn_aborts():
    harness = Harness(bug_state())
    harness.observe()
    harness.state = bug_state(active=1, phase="Phase_Main2", turn=23, tapped=frozenset())
    harness.run_countdown()
    assert harness.concedes == [] and "new turn" in harness.events[-1]["reason"]


def test_never_concedes_twice():
    harness = Harness(bug_state(), ends_on_concede=False)
    harness.observe()
    harness.run_countdown()
    harness.observe()
    harness.observe()
    assert len(harness.concedes) == 1 and harness.threads == []


def test_cancel_after_the_claim_is_too_late():
    harness = Harness(bug_state())
    late: list[bool] = []
    harness.on_concede = lambda: late.append(harness.controller.cancel("ui"))
    harness.observe()
    harness.run_countdown()
    assert late == [False] and len(harness.concedes) == 1


def test_bridge_down_or_setting_off_only_recommends():
    down = Harness(bug_state(), bridge=False)
    down.observe()
    off = Harness(bug_state())
    off.settings["auto_concede"] = False
    off.observe()
    for harness in (down, off):
        assert len(harness.advice) == 1 and "Auto-concede" not in harness.advice[0]
        assert harness.threads == []


def test_a_new_game_gets_a_new_recommendation():
    harness = Harness(bug_state(), autopilot=False)
    harness.observe(("fra", 1))
    harness.observe(("fra", 2))
    assert len(harness.advice) == 2


def test_a_failed_concede_is_reported_once_and_not_retried():
    harness = Harness(bug_state(), result={"ok": False, "error": "Main thread busy"})
    harness.observe()
    harness.run_countdown()
    assert len(harness.concedes) == 1 and harness.states[-1] == "failed"
    assert "Arena's menu" in harness.advice[-1]
    harness.observe()
    assert harness.threads == []


def test_an_unsupported_bridge_stops_offering_until_it_reconnects():
    harness = Harness(
        bug_state(), result={"ok": False, "unsupported": True, "error": "Unknown action: concede"}
    )
    harness.observe(("fra", 1))
    harness.run_countdown()
    assert harness.states[-1] == "failed" and "plugin is updated" in harness.advice[-1]
    # Next game: recommend only, no countdown.
    harness.observe(("fra", 2))
    assert harness.threads == [] and harness.advice[-1].startswith("Concede:")
    assert harness.controller.snapshot()["bridge_unsupported"] is True
    # The bridge goes away and comes back (an updated plugin): offers again.
    harness.bridge = False
    harness.observe(("fra", 3))
    harness.bridge = True
    harness.observe(("fra", 4))
    assert len(harness.threads) == 1


def test_a_sent_concede_is_confirmed_only_when_the_game_ends():
    harness = Harness(bug_state(), ends_on_concede=False)
    harness.observe()
    harness.run_countdown()
    assert harness.states[-2:] == ["sent", "unconfirmed"]
    assert "hasn't ended the game" in harness.advice[-1]
    ended = Harness(bug_state(), ends_on_concede=False)
    ended.on_concede = lambda: setattr(ended, "state", bug_state(0))
    ended.observe()
    ended.run_countdown()
    assert ended.states[-2:] == ["sent", "conceded"] and ended.advice == []


def test_a_cancel_on_another_thread_waits_for_the_armed_event():
    import threading

    harness = Harness(bug_state())
    blocked: list[bool] = []

    def on_emit(payload: dict) -> None:
        if payload["state"] != "armed":
            return
        canceller = threading.Thread(target=harness.controller.cancel, args=("ui",))
        canceller.start()
        canceller.join(0.2)
        blocked.append(canceller.is_alive())  # waiting on the emit lock
        harness._joins = canceller  # type: ignore[attr-defined]

    def wait(event: Any, seconds: float) -> bool:
        harness._joins.join(2)  # type: ignore[attr-defined]  # the cancel lands during the countdown
        return event.is_set()

    harness.on_emit = on_emit
    harness.controller._wait = wait
    harness.observe()
    harness.run_countdown()
    assert blocked == [True]
    assert harness.states[:3] == ["offering", "armed", "cancelled"] and harness.concedes == []


def test_a_reload_keeps_the_decline_and_never_concedes_twice():
    first = Harness(bug_state())
    first.observe()
    first.controller.cancel("ui")
    saved = first.controller.export_state()
    assert saved == {
        "match_id": "fra-3da54de9",
        "turn": 22,
        "recommended": True,
        "declined": True,
        "conceded": False,
        "arms": 1,
    }
    second = Harness(bug_state())
    second.controller.resume_from(saved)
    second.observe(("fra-3da54de9", 0))  # the new process numbers matches from 0
    second.observe(("fra-3da54de9", 0))
    assert second.advice == [] and second.announced == [] and second.threads == []
    # A record from a different game is ignored.
    third = Harness(bug_state(turn=3))
    third.controller.resume_from(saved)
    third.observe()
    assert third.states == ["offering"]


def test_a_reload_mid_countdown_aborts_without_declining():
    first = Harness(bug_state())
    first.observe()
    assert first.controller.cancel("the engine is reloading", decline=False) is True
    assert first.states == ["offering", "aborted"]
    saved = first.controller.export_state()
    assert saved["declined"] is False and saved["recommended"] is True
    second = Harness(bug_state())
    second.controller.resume_from(saved)
    second.observe()
    second.run_countdown()
    # Recommended already: only the offer is repeated, then the countdown runs.
    assert second.announced == ["Auto-concede in 10 seconds — say cancel or press Cancel to keep playing."]
    assert len(second.concedes) == 1


def test_settings_are_clamped():
    harness = Harness(bug_state())
    harness.settings.update(concede_threshold=0.5, concede_countdown_s=999)
    assert harness.controller.threshold() == 0.90 and harness.controller.countdown_s() == 60


# --- pipe commands ----------------------------------------------------------------


class PipeCoach:
    def __init__(self, armed: bool = False) -> None:
        self.cancels: list[str] = []
        self.aborts: list[str] = []
        self.auto_concede: list[bool] = []
        self.armed = armed
        self._autopilot = None
        self._autopilot_enabled = False
        self.conversation = None
        self.voice_session = None
        self._voice_output = None

    def cancel_concede(self, source: str = "ui") -> bool:
        self.cancels.append(source)
        was, self.armed = self.armed, False
        return was

    def abort_concede(self, reason: str) -> bool:
        self.aborts.append(reason)
        was, self.armed = self.armed, False
        return was

    def concede_armed(self) -> bool:
        return self.armed

    def set_auto_concede(self, enabled: bool) -> bool:
        self.auto_concede.append(enabled)
        return enabled

    def set_autopilot(self, enabled: bool) -> bool:
        return False


def pipe(coach: PipeCoach) -> tuple[PipeAdapter, list[dict]]:
    adapter = PipeAdapter()
    events: list[dict] = []
    adapter._emit = events.append  # type: ignore[method-assign]
    adapter._coach = coach
    return adapter, events


@pytest.mark.parametrize(
    "command, source",
    [
        ({"cmd": "cancel_concede"}, "ui"),
        ({"cmd": "force_stop"}, "force_stop"),
        ({"cmd": "stop_speech"}, "stop_speech"),
        ({"cmd": "autopilot_abort"}, "autopilot_abort"),
    ],
)
def test_pipe_commands_cancel_the_countdown(command, source):
    coach = PipeCoach(armed=True)
    adapter, _ = pipe(coach)
    adapter._dispatch(command)
    assert coach.cancels[:1] == [source]


def test_set_auto_concede_reaches_the_engine():
    coach = PipeCoach()
    adapter, _ = pipe(coach)
    adapter._dispatch({"cmd": "set_auto_concede", "enabled": False})
    adapter._dispatch({"cmd": "set_auto_concede", "enabled": True})
    assert coach.auto_concede == [False, True]


def test_chat_cancel_only_intercepts_while_a_countdown_runs():
    coach = PipeCoach(armed=True)
    adapter, _ = pipe(coach)
    assert adapter._try_concede_cancel_reply("Cancel!") and coach.cancels == ["chat"]
    assert not adapter._try_concede_cancel_reply("cancel")  # nothing running any more
    assert not PipeAdapter._try_concede_cancel_reply(pipe(PipeCoach(armed=True))[0], "what now?")


def test_autopilot_cancel_falls_back_to_escape():
    class Engine:
        escaped = 0

        def on_escape(self) -> None:
            Engine.escaped += 1

    coach = PipeCoach()
    coach._autopilot = Engine()
    adapter, events = pipe(coach)
    adapter._dispatch({"cmd": "autopilot_cancel"})
    assert Engine.escaped == 1 and not [e for e in events if e.get("type") == "error"]


def test_concede_countdown_event_shape():
    adapter, events = pipe(PipeCoach())
    adapter.concede_countdown({"state": "armed", "seconds": 10, "id": 1})
    assert events == [{"type": "concede_countdown", "data": {"state": "armed", "seconds": 10, "id": 1}}]


# --- bridge -----------------------------------------------------------------------


def concede_world(
    match_state="GameInProgress", scene="DuelScene", stage="Play", manager=True
) -> tuple[World, int]:
    world = World()
    gre = world.add(40, "GreInterface", _disposed=False)
    info = world.add(50, "GameInfo", MatchID="fra-3da54de9", GameNumber=1)
    game = world.add(60, "MtgGameState", Stage=stage, GameWideTurn=22, GameInfo=Obj(info))
    scenes = world.add(70, "MatchSceneManager", Current=scene)
    match = world.add(20, "MatchManager", MatchState=match_state, GreInterface=Obj(gre))
    if manager:
        world.finds["GameManager"] = world.add(
            10,
            "GameManager",
            MatchManager=Obj(match),
            MatchSceneManager=Obj(scenes),
            CurrentGameState=Obj(game),
        )
    return world, gre


def test_mac_concede_calls_the_clients_own_concede_game():
    world, gre = concede_world()
    result = MacBridgeAdapter(world.send).handle(
        {"action": "concede", "scope": "game", "expected_match_id": "fra-3da54de9", "expected_turn": 22}
    )
    assert result["ok"] and result["scope"] == "Game" and result["turn"] == 22
    assert [(h, m) for h, m, _a in world.calls] == [(gre, "ConcedeGame")]


@pytest.mark.parametrize(
    "world_args, command",
    [
        ({"match_state": "GameComplete"}, {}),
        ({"scene": "MatchEnd"}, {}),
        ({"stage": "GameOver"}, {}),
        ({"manager": False}, {}),
        ({}, {"expected_turn": 23}),
        ({}, {"expected_match_id": "another-match"}),
        ({}, {"scope": "match"}),
    ],
)
def test_mac_concede_refuses_anything_but_this_game_in_play(world_args, command):
    world, _ = concede_world(**world_args)
    result = MacBridgeAdapter(world.send).handle({"action": "concede", **command})
    assert not result["ok"]
    assert not any(method.startswith("Concede") for _h, method, _a in world.calls)


def test_mac_concede_timeout_is_outcome_unknown():
    world, _ = concede_world()
    batches = []

    def send(command: dict, timeout: float | None) -> dict:
        batches.append(command)
        if len(batches) == 2:
            return {"ok": False, "error": "main thread did not answer in time (ticks=5); outcome unknown"}
        return world.send(command, timeout)

    result = MacBridgeAdapter(send).handle({"action": "concede"})
    assert result == {
        "ok": False,
        "outcome_unknown": True,
        "error": "main thread did not answer in time (ticks=5); outcome unknown",
    }


def test_bridge_concede_sends_once_and_never_retries(monkeypatch):
    bridge = GREBridge()
    bridge._connected = True
    sent: list[dict] = []
    reconnects: list[bool] = []

    def failing(cmd: dict, timeout: float | None = None) -> dict:
        sent.append(cmd)
        raise GREBridgeError("Pipe read timeout (5.0s) for action=concede")

    monkeypatch.setattr(bridge, "_send_command", failing)
    monkeypatch.setattr(bridge, "connect", lambda: reconnects.append(True) or True)
    result = bridge.concede(expected_match_id="fra", expected_turn=22)
    assert result["outcome_unknown"] and len(sent) == 1 and reconnects == []
    assert sent[0] == {
        "action": "concede",
        "scope": "game",
        "expires_in_ms": 4000,  # the plugin refuses a concede that waited longer for the main thread
        "expected_match_id": "fra",
        "expected_turn": 22,
    }

    monkeypatch.setattr(
        bridge, "_send_command", lambda cmd, timeout=None: {"ok": False, "error": "Unknown action: concede"}
    )
    assert bridge.concede()["unsupported"] is True
    assert bridge.concede_supported is False  # remembered until the client reconnects


def test_shutdown_paths_abort_the_countdown(monkeypatch):
    import io
    import sys

    coach = PipeCoach(armed=True)
    adapter, _ = pipe(coach)
    adapter._running = True
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    adapter._stdin_loop()  # the desktop closed our stdin
    assert coach.aborts == ["the desktop closed"] and coach._running is False

    class Broken:
        def write(self, data: bytes) -> None:
            raise BrokenPipeError

        def flush(self) -> None:
            pass

    coach = PipeCoach(armed=True)
    adapter, _ = pipe(coach)
    adapter._running = True
    adapter._write_queue.put("{}\n")
    monkeypatch.setattr(sys, "stdout", type("Out", (), {"buffer": Broken()})())
    adapter._stdout_loop()  # the desktop stopped reading
    assert coach.aborts == ["the desktop went away"] and coach._running is False

    coach = PipeCoach(armed=True)
    adapter, _ = pipe(coach)
    adapter._dispatch({"cmd": "restart"})
    assert coach.aborts == ["the engine is restarting"] and coach._running is False


def test_bridge_concede_capability_follows_the_client():
    bridge = GREBridge()
    assert bridge.concede_supported is False  # not connected
    bridge._connected = True
    for version, supported in [
        (None, False),
        ("0.6.3", False),
        ("0.6.4", True),
        ("0.7.0", True),
        ("v1.0", True),
    ]:
        bridge.plugin_version = version
        assert bridge.concede_supported is supported, version
    bridge.plugin_version = "0.6.3"
    bridge._mac_adapter = object()  # the native library's adapter always can
    assert bridge.concede_supported is True
    bridge._concede_unsupported = True  # it said "Unknown action: concede"
    assert bridge.concede_supported is False


def test_the_concede_mixin_needs_a_concede_capable_bridge(monkeypatch):
    from unittest.mock import Mock

    import arenamcp.gre_bridge as gre_bridge
    from arenamcp.standalone_concede import _ConcedeMixin

    class Runtime(_ConcedeMixin):
        _bridge_poller = Mock(connected=True)

    fake = Mock(concede_supported=False)
    monkeypatch.setattr(gre_bridge, "get_bridge", lambda: fake)
    assert Runtime()._concede_bridge_ready() is False
    fake.concede_supported = True
    assert Runtime()._concede_bridge_ready() is True


# --- desktop ------------------------------------------------------------------------


def test_desktop_banner_offers_counts_down_and_cancels(qapp):
    pytest.importorskip("PySide6")
    from arenamcp.desktop.compact_coach import CompactCoachPanel

    panel = CompactCoachPanel()
    sent: list[dict] = []
    panel.session._process.send_payload = sent.append  # type: ignore[method-assign]
    panel.show()

    def event(data: dict) -> None:
        panel.session._handle_process_event({"type": "concede_countdown", "data": data})

    try:
        event({"state": "offering", "id": 1, "seconds": 10, "reason": "Concede: x"})
        assert panel.concede_banner.isVisible() and "Auto-concede offered" in panel.concede_label.text()
        assert panel.concede_cancel_btn.isVisible()
        event({"state": "armed", "id": 1, "seconds": 10, "reason": "Concede: x"})
        assert "Auto-concede in 10s" in panel.concede_label.text()
        panel.concede_cancel_btn.click()
        assert sent == [{"cmd": "cancel_concede"}] and not panel.concede_cancel_btn.isEnabled()
        event({"state": "cancelled", "id": 1})
        assert "cancelled" in panel.concede_label.text() and not panel.concede_cancel_btn.isVisible()
        # Sent is not conceded: the banner waits for the game to end.
        event({"state": "offering", "id": 2, "seconds": 5})
        event({"state": "armed", "id": 2, "seconds": 5})
        event({"state": "conceding", "id": 2})
        event({"state": "sent", "id": 2})
        assert "waiting for Arena" in panel.concede_label.text()
        event({"state": "unconfirmed", "id": 2, "message": "Arena hasn't ended the game"})
        assert "not confirmed" in panel.concede_label.text()
        # A coach that dies mid-countdown takes the banner's countdown down too.
        event({"state": "offering", "id": 3, "seconds": 5})
        panel.session._handle_exited(1)
        assert "stopped" in panel.concede_label.text()
    finally:
        panel.close()


def test_desktop_drops_an_armed_banner_nobody_follows_up(qapp):
    pytest.importorskip("PySide6")
    import time

    from arenamcp.desktop.compact_coach import CompactCoachPanel

    panel = CompactCoachPanel()
    panel.show()
    try:
        panel.session._handle_process_event(
            {"type": "concede_countdown", "data": {"state": "armed", "id": 7, "seconds": 3}}
        )
        assert panel.concede_banner.isVisible()
        panel._concede_deadline = time.monotonic() - 20  # deadline long gone, no follow-up
        panel._render_concede()
        assert not panel.concede_banner.isVisible()
    finally:
        panel.close()


# --- coach wiring -------------------------------------------------------------------


def test_toggling_autoplay_off_cancels_the_countdown():
    from unittest.mock import Mock

    from arenamcp.standalone import StandaloneCoach

    coach = StandaloneCoach.__new__(StandaloneCoach)
    coach._concede = Mock()
    coach._autopilot_enabled = True
    coach._autopilot = Mock(requires_desktop_poll=False)
    coach._autopilot_backend = None
    coach._coach = None
    coach.settings = Mock()
    assert coach.toggle_autopilot() is False
    coach._concede.cancel.assert_called_once_with("autopilot toggled", decline=True)


def test_stop_aborts_the_countdown_even_when_already_stopping():
    from unittest.mock import Mock

    from arenamcp.standalone import StandaloneCoach

    coach = StandaloneCoach.__new__(StandaloneCoach)
    coach._concede = Mock()
    coach._running = False  # stdin EOF / restart cleared it first
    coach.stop()
    coach._concede.cancel.assert_called_once_with("the coach stopped", decline=False)


def test_a_stopping_coach_never_concedes():
    from unittest.mock import Mock

    from arenamcp.standalone_concede import _ConcedeMixin

    class Runtime(_ConcedeMixin):
        def __init__(self) -> None:
            self.settings = Mock()
            self._concede = Mock()
            self._autopilot_dry_run = False
            self._running = True

        def _autopilot_control_status(self) -> str:
            return "AP:ON"

    runtime = Runtime()
    assert runtime._concede_autopilot_on() is True
    runtime._running = False
    assert runtime._concede_autopilot_on() is False


def test_turning_auto_concede_off_saves_and_cancels():
    from unittest.mock import Mock

    from arenamcp.standalone_concede import _ConcedeMixin

    class Runtime(_ConcedeMixin):
        def __init__(self) -> None:
            self.settings = Mock()
            self._concede = Mock()
            self._autopilot_dry_run = True
            self._running = True

        def _autopilot_control_status(self) -> str:
            return "AP:ON"

    runtime = Runtime()
    runtime.set_auto_concede(False)
    runtime.settings.set.assert_called_once_with("auto_concede", False)
    runtime._concede.cancel.assert_called_once_with("auto-concede turned off", decline=True)
    assert runtime._concede_autopilot_on() is False  # a dry run never concedes


def test_the_engine_reload_record_reaches_the_new_engine(monkeypatch, tmp_path):
    import json
    import time
    from unittest.mock import Mock

    import arenamcp.standalone_startup as startup
    from arenamcp.standalone_concede import _ConcedeMixin

    record = {
        "match_id": "fra-3da54de9",
        "turn": 22,
        "recommended": True,
        "declined": True,
        "conceded": False,
    }
    path = tmp_path / "engine_resume.json"
    path.write_text(json.dumps({"saved_at": time.time(), "match_id": "fra-3da54de9", "concede": record}))
    monkeypatch.setattr(startup, "ENGINE_RESUME_PATH", path)
    monkeypatch.setenv("ARENAMCP_ENGINE_RELOAD", "1")

    class Runtime(_ConcedeMixin, startup._StartupMixin):
        def __init__(self) -> None:
            self.settings = Mock()
            self.ui = Mock()
            self._concede = Mock()

    runtime = Runtime()
    runtime._load_engine_resume()
    runtime._concede.resume_from.assert_called_once_with(record)


def test_printed_stats_are_added_on_our_turn_only(monkeypatch):
    from unittest.mock import Mock

    from arenamcp.standalone_concede import _ConcedeMixin

    class Runtime(_ConcedeMixin):
        settings = Mock()

    lookups: list[int] = []
    monkeypatch.setattr(
        _ConcedeMixin, "_lookup_printed", staticmethod(lambda grp: lookups.append(grp) or (2, 2))
    )
    runtime = Runtime()
    faerie = {"instance_id": 10, "grp_id": 555, "type_line": "Creature — Faerie", "power": 5, "toughness": 5}
    ours = {"local_seat_id": 1, "turn": {"active_player": 1}, "battlefield": [faerie]}
    enriched = runtime._with_printed_stats(ours)
    assert enriched["battlefield"][0]["printed_power"] == 2 and "printed_power" not in faerie
    runtime._with_printed_stats(ours)
    assert lookups == [555]  # cached
    theirs = {"local_seat_id": 1, "turn": {"active_player": 2}, "battlefield": [faerie]}
    assert runtime._with_printed_stats(theirs) is theirs
