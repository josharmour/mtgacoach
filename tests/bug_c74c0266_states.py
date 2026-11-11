"""State module rebuilt from match c74c0266 (2026-11-09 23:33-23:55).

Evidence (READ ONLY copies from the Mac):
- packet_20261109_235553_c74c0266-39d0-4db4-a447-e3d477749169.json
  (decision records quoted below),
- standalone.log around 23:33-23:55.

Logged trajectory (standalone.log):

* 23:42:50 - their turn-27 DeclareAttack vs our 5 life: estimate **0.97**
  ("this attack ... Herald of Eternal Dawn ... vs your 5 life"); countdown armed.
* The attack dealt only 3 damage (5 -> 2 life); at 23:42:53 the estimate
  collapsed to **0.00** ("not dead next attack") and the countdown aborted.
* The game dragged ~13 minutes and was lost at 23:55:53.

Packet decision records (facts quoted from trace.plan_block):

* decision_index=36, 2026-11-09 23:44:06, our turn 28 - "Board: us 0
  creatures/0 power vs them 4/27 | hand 3 vs 12 | lands 11 + land drop vs
  13"; "their next attack: 27 -> lethal"; "life 2 vs 23"; "Opponent has 4
  visibly untapped creatures now". Threats: "Herald of Eternal Dawn (8/8,
  flying, we have no flyer/reach); Nissa, Leyline Tamer (7/7, deathtouch,
  card draw); Chandra, Flameshaper (planeswalker (loyalty 7), adds attackers
  each turn (token/copy engine))"; other planeswalkers Narset, Parter of
  Veils (loyalty 1), Ugin, the Spirit Dragon (loyalty 12), Jace, Multiverse
  Architect (loyalty 10). Commander: "Laelia, the Blade Reforged {12}{R}
  now ({8} tax for 4 previous casts)". The turn's only plays were three
  Mountains and a pass - "Cast Laelia" was never offered ({12}{R} needs
  thirteen mana; eleven untapped Mountains plus the land drop make twelve).
* decision_index=40, 2026-11-09 23:49:15, our turn 30 - "Board: us 0
  creatures/0 power vs them 11/55 | hand 0 vs 16 | lands 13 vs 17" (the
  turn's Mountain was already played, so no land drop remains); "their next
  attack: 55 -> lethal"; "life 2 vs 26". Here "Cast Laelia" WAS offered at
  {12}{R} - thirteen untapped Mountains pay it exactly - and passed.
  Threats: Herald of Eternal Dawn (8/8, flying), Elspeth, Sun's Champion
  (planeswalker (loyalty 8)), Chandra, Flameshaper (planeswalker (loyalty
  11)); other planeswalkers Jace, Multiverse Architect (6), Ugin, the Spirit
  Dragon (6), Teferi, Hero of Dominaria (8), Vraska, Betrayal's Sting (8),
  Teferi, Time Raveler (8), Nicol Bolas, God-Pharaoh (13), Narset, Parter of
  Veils (5).

The printed commander cost is {12}{R} in both decisions; the packet's own
parenthetical "({8} tax for 4 previous casts)" contradicts it ({8} + {2}{R}
= {10}{R}), so the fixture reproduces the printed {12}{R}: five prior casts
-> tax {10}. Only Herald of Eternal Dawn is named among their creatures in
both records; the remaining bodies are placeholders carrying the packet's
power totals.
"""

from typing import Any

MATCH_ID = "c74c0266-39d0-4db4-a447-e3d477749169"

# Printed {12}{R} = base {2}{R} + tax {10} -> five prior commander casts.
LIELIA_CASTS = 5
OUR_LANDS_ON_OUR_TURN_28 = 11

# Laelia, the Blade Reforged as printed (Scryfall): a plain aggressive body -
# haste plus exile-draw triggers; no removal, lifegain or blocker creation.
LAELIA = {
    "instance_id": 700,
    "name": "Laelia, the Blade Reforged",
    "mana_cost": "{2}{R}",
    "type_line": "Legendary Creature - Spirit Warrior",
    "oracle_text": (
        "Haste\n"
        "Whenever Laelia attacks, exile the top card of your library. "
        "You may play that card this turn.\n"
        "Whenever one or more cards are put into exile from your library "
        "and/or your graveyard, put a +1/+1 counter on Laelia."
    ),
    "power": 2,
    "toughness": 2,
    "owner_seat_id": 1,
    "controller_seat_id": 1,
}


def _card(
    instance_id: int,
    name: str,
    controller_seat_id: int,
    *,
    types: str = "Creature - Knight",
    text: str = "",
    power: int | None = None,
    toughness: int | None = None,
    keywords: list[str] | None = None,
    tapped: bool = False,
    attacking: bool = False,
    entered_turn: int = 20,
) -> dict[str, Any]:
    card = {
        "instance_id": instance_id,
        "name": name,
        "type_line": types,
        "oracle_text": text,
        "owner_seat_id": controller_seat_id,
        "controller_seat_id": controller_seat_id,
        "is_tapped": tapped,
        "is_attacking": attacking,
        "turn_entered_battlefield": entered_turn,
    }
    if power is not None:
        card["power"] = power
    if toughness is not None:
        card["toughness"] = toughness
    if keywords:
        card["keywords"] = keywords
    return card


def _mountains(count: int, first_iid: int = 500) -> list[dict[str, Any]]:
    return [
        _card(first_iid + i, "Mountain", 1, types="Basic Land - Mountain", text="({T}: Add {R}.)")
        for i in range(count)
    ]


def _shell(
    life: int,
    *,
    phase: str,
    step: str = "",
    active: int,
    turn: int,
    our_lands: int,
    their_life: int,
    library_count: int,
    opponent_hand_count: int,
    hand_cards: list[dict[str, Any]] | None = None,
    lands_played: int | None = None,
) -> dict[str, Any]:
    ours = {"seat_id": 1, "life_total": life, "is_local": True, "status": "InGame", "mana_pool": {}}
    theirs = {"seat_id": 2, "life_total": their_life, "is_local": False, "status": "InGame", "mana_pool": {}}
    if lands_played is not None:
        ours["lands_played"] = lands_played
    return {
        "match_id": MATCH_ID,
        "local_seat_id": 1,
        "opponent_seat_id": 2,
        "turn": {
            "turn_number": turn,
            "active_player": active,
            "priority_player": active,
            "phase": phase,
            "step": step,
        },
        # Life totals and zone counts are packet plan-block facts; zone
        # membership is log-derived. No floating mana in either state.
        # Their planeswalkers carry packet loyalties only; loyalty abilities
        # are not modelled here.
        "players": [ours, theirs],
        "battlefield": _mountains(our_lands),
        "hand": list(hand_cards or []),
        "graveyard": [],
        "exile": [],
        "stack": [],
        "action_history": [],
        "pending_decision": None,
        # Five prior casts -> tax {10} on the printed {2}{R} base.
        "command": [dict(LAELIA, commander_casts=LIELIA_CASTS)],
        "commander_casts": {},
        "zones": {
            "library_count": library_count,
            "library_count_source": "packet_plan_block",
            "opponent_hand_count": opponent_hand_count,
        },
    }


def _t27_their_board(*, attacking: bool, herald_tapped: bool) -> list[dict[str, Any]]:
    """Their board at the logged turn-27 attack.

    Three ready creatures totalled nineteen attacking power ("19 through our
    best blocks vs 5 life"); Herald of Eternal Dawn is the logged name and is
    flying. A fourth body joined post-combat ("0 vs 4 creatures, 0 vs 27
    power"), summoning sick. Planeswalker loyalties per packet decision
    record: Chandra, Flameshaper entered this turn at loyalty seven; Narset,
    Parter of Veils at one; Ugin, the Spirit Dragon at twelve; Jace,
    Multiverse Architect at ten since turn twenty.
    """
    return [
        _card(
            600,
            "Herald of Eternal Dawn",
            2,
            types="Creature - Angel",
            power=8,
            toughness=8,
            text="Flying",
            keywords=["flying"],
            tapped=herald_tapped,
            attacking=attacking,
        ),
        _card(601, "Opponent Attacker", 2, power=6, toughness=6, tapped=herald_tapped, attacking=attacking),
        _card(602, "Opponent Attacker", 2, power=5, toughness=5, tapped=herald_tapped, attacking=attacking),
        _card(603, "Opponent Attacker", 2, power=8, toughness=8, entered_turn=27),
        _card(610, "Chandra, Flameshaper", 2, types="Planeswalker - Chandra", entered_turn=27),
        _card(611, "Narset, Parter of Veils", 2, types="Planeswalker - Narset"),
        _card(612, "Ugin, the Spirit Dragon", 2, types="Planeswalker - Ugin"),
        _card(613, "Jace, Multiverse Architect", 2, types="Planeswalker - Jace"),
    ]


def _t28_their_board() -> list[dict[str, Any]]:
    """Their board at packet decision index 36 (our turn 28).

    Four visibly untapped creatures totalling twenty-seven power. Named
    threats: Herald of Eternal Dawn (8/8 flying - the printed card is a 6/6
    angel; the packet reports it buffed) and Nissa, Leyline Tamer (7/7
    deathtouch with card draw). The two unnamed bodies carry the remaining
    twelve power. Planeswalkers with packet loyalties: Chandra, Flameshaper
    (7), Narset, Parter of Veils (1), Ugin, the Spirit Dragon (12), Jace,
    Multiverse Architect (10).
    """
    return [
        _card(
            600,
            "Herald of Eternal Dawn",
            2,
            types="Creature - Angel",
            power=8,
            toughness=8,
            text="Flying",
            keywords=["flying"],
        ),
        _card(
            601,
            "Nissa, Leyline Tamer",
            2,
            types="Creature - Nissa",
            power=7,
            toughness=7,
            keywords=["deathtouch"],
        ),
        _card(602, "Opponent Attacker", 2, power=6, toughness=6),
        _card(603, "Opponent Attacker", 2, power=6, toughness=6),
        _card(610, "Chandra, Flameshaper", 2, types="Planeswalker - Chandra", entered_turn=27),
        _card(611, "Narset, Parter of Veils", 2, types="Planeswalker - Narset"),
        _card(612, "Ugin, the Spirit Dragon", 2, types="Planeswalker - Ugin"),
        _card(613, "Jace, Multiverse Architect", 2, types="Planeswalker - Jace"),
    ]


def _t30_their_board() -> list[dict[str, Any]]:
    """Their board at packet decision index 40 (our turn 30).

    Eleven visibly untapped creatures totalling fifty-five power. Named
    threats: Herald of Eternal Dawn (8/8 flying), Elspeth, Sun's Champion and
    Chandra, Flameshaper as token engines; ten unnamed bodies carry the
    remaining forty-seven power. Planeswalkers with packet loyalties: Jace,
    Multiverse Architect (6), Ugin, the Spirit Dragon (6), Chandra,
    Flameshaper (11), Teferi, Hero of Dominaria (8), Elspeth, Sun's Champion
    (8), Vraska, Betrayal's Sting (8), Teferi, Time Raveler (8), Nicol Bolas,
    God-Pharaoh (13), Narset, Parter of Veils (5).
    """
    return [
        _card(
            600,
            "Herald of Eternal Dawn",
            2,
            types="Creature - Angel",
            power=8,
            toughness=8,
            text="Flying",
            keywords=["flying"],
        ),
        # Seven bodies at five power and three at four: 7*5 + 3*4 = 47.
        *[
            _card(601 + i, "Opponent Attacker", 2, power=5 if i < 7 else 4, toughness=5 if i < 7 else 4)
            for i in range(10)
        ],
        _card(610, "Jace, Multiverse Architect", 2, types="Planeswalker - Jace"),
        _card(611, "Ugin, the Spirit Dragon", 2, types="Planeswalker - Ugin"),
        _card(612, "Chandra, Flameshaper", 2, types="Planeswalker - Chandra"),
        _card(613, "Teferi, Hero of Dominaria", 2, types="Planeswalker - Teferi"),
        _card(614, "Elspeth, Sun's Champion", 2, types="Planeswalker - Elspeth"),
        _card(615, "Vraska, Betrayal's Sting", 2, types="Planeswalker - Vraska"),
        _card(616, "Teferi, Time Raveler", 2, types="Planeswalker - Teferi"),
        _card(617, "Nicol Bolas, God-Pharaoh", 2, types="Planeswalker - Nicol Bolas"),
        _card(618, "Narset, Parter of Veils", 2, types="Planeswalker - Narset"),
    ]


def attack_state() -> dict[str, Any]:
    """Their turn-27 DeclareAttack vs our **5** life (logged estimate **0.97**)."""
    state = _shell(
        life=5,
        phase="Phase_Combat",
        step="Step_DeclareAttack",
        active=2,
        turn=27,
        our_lands=OUR_LANDS_ON_OUR_TURN_28,
        their_life=23,
        library_count=77,
        opponent_hand_count=12,
    )
    state["battlefield"] = _mountains(OUR_LANDS_ON_OUR_TURN_28) + _t27_their_board(
        attacking=True, herald_tapped=False
    )
    return state


def post_combat_state() -> dict[str, Any]:
    """Their turn-27 post-combat main phase (logged estimate **0.00**, "not
    dead next attack"): the smaller-than-lethal attack landed for 3."""
    state = _shell(
        life=2,
        phase="Phase_Main2",
        active=2,
        turn=27,
        our_lands=OUR_LANDS_ON_OUR_TURN_28,
        their_life=23,
        library_count=77,
        opponent_hand_count=12,
    )
    state["battlefield"] = _mountains(OUR_LANDS_ON_OUR_TURN_28) + _t27_their_board(
        attacking=False, herald_tapped=True
    )
    return state


def our_turn_28_state() -> dict[str, Any]:
    """Packet decision index 36 (2026-11-09 23:44:06): our turn 28 Main1.

    Packet-faithful board: eleven untapped Mountains plus the land drop
    (twelve mana) cannot pay Laelia's printed {12}{R}, and the hand holds
    three Mountains - exactly the three land plays the turn actually made.
    """
    state = _shell(
        life=2,
        phase="Phase_Main1",
        active=1,
        turn=28,
        our_lands=OUR_LANDS_ON_OUR_TURN_28,
        their_life=23,
        library_count=77,
        opponent_hand_count=12,
        hand_cards=_mountains(3, first_iid=900),
    )
    state["battlefield"] = _mountains(OUR_LANDS_ON_OUR_TURN_28) + _t28_their_board()
    return state


def our_turn_30_state() -> dict[str, Any]:
    """Packet decision index 40 (2026-11-09 23:49:15): our turn 30 Main1.

    Thirteen untapped Mountains pay Laelia's printed {12}{R} exactly - the
    packet offered "Cast Laelia" here - and the hand is empty because the
    turn's Mountain was already played (lands_played=1).
    """
    state = _shell(
        life=2,
        phase="Phase_Main1",
        active=1,
        turn=30,
        our_lands=13,
        their_life=26,
        library_count=55,
        opponent_hand_count=16,
        lands_played=1,
    )
    state["battlefield"] = _mountains(13) + _t30_their_board()
    return state


def saved_state() -> dict[str, Any]:
    """Contrast case on the packet-decision-40 board shape: a single ground
    attacker a fresh body can chump-block - casting Laelia *could* leave us
    alive here, so she stays an out."""
    state = our_turn_30_state()
    state["players"][0]["life_total"] = 3
    state["battlefield"] = [
        card
        for card in state["battlefield"]
        if card["controller_seat_id"] != 2 or "Planeswalker" in card["type_line"]
    ] + [_card(650, "Opponent Attacker", 2, power=5, toughness=5)]
    return state
