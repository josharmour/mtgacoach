"""Combat guards and the combat model on the 2026-10-19 ladder boards (macOS bridge, Player.log).

- #12 cd41ad5a T21 (07:40, Brawl): Shark Shredder's first-strike hit reanimated
  a Hare Apparent from our graveyard into the attack; the block guard counted
  12 into 14 life and Leonardo blocked the Hare. 14 was exactly lethal.
- #14 f9e0ec3b T10 (09:19): Emberheart Challenger 3/3 attacked into an untapped
  Lyra Dawnbringer next to a 1/2 Hired Claw; Lyra ate it and gained 5.
  a74bbc90 08:09:19: Slickshot Show-Off was held back from a trade with Moseo.
  a51dea64 10:25 T15: Hired Claw's ping exempted it from the guard.
- #18 c36db3a3 T10 (07:51): a free Slickshot attack held back to "chump" Elenda
  at 18 life, a chump the chump guard refuses.
- #22 a74bbc90 T9 (08:10): no block with Slickshot on an attacking 1/1 Pest.
- #25 92c09c7a T10 (09:34) and 1e4ccb0c T13 (10:14:31): Fleeting Effigy (returns
  to hand at our end step) counted as next turn's blocker; the bridge's attack
  override also counted a blocker that dies in the scored block and a tapped
  "doesn't untap" Slumbering Cerberus in the crackback.
- #34 6175f002 T9 (10:03:59): the opponent withdrew Guiding Hydra from the
  attack (gsid 269); our state kept it attacking.
- #35 6175f002 T9: the block prompt ignored Momo's granted double strike and
  lifelink ("take 9" was 10) and said "Preserve blockers!".
"""

from __future__ import annotations

import copy
import logging

from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction
from arenamcp.combat_solver import (
    _resolve_attacker,
    can_attack_next_turn,
    evaluate_attack,
    leaves_at_our_end_step,
)
from arenamcp.combat_strategy import (
    combat_choice,
    free_attackers,
    free_blocks,
    guarded_block_plan,
    hit_bonus,
    losing_attackers,
    phantom_attackers,
    safe_default_blocks,
    wasteful_chump_blocks,
)

PLAYER = {"type": "DamageRecType_Player"}


def creature(iid, name, power, toughness, seat, text="", *, type_line="Creature", **extra):
    return {
        "instance_id": iid,
        "name": name,
        "type_line": type_line,
        "card_types": ["CardType_Creature"],
        "power": power,
        "toughness": toughness,
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "oracle_text": text,
        "is_tapped": False,
        **extra,
    }


def land(iid, seat, tapped=True):
    return {
        "instance_id": iid,
        "name": "Mountain",
        "type_line": "Basic Land — Mountain",
        "card_types": ["CardType_Land"],
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "oracle_text": "({T}: Add {R}.)",
        "is_tapped": tapped,
    }


def players(local, local_life, opponent, opponent_life):
    return [
        {"seat_id": opponent, "life_total": opponent_life, "is_local": False, "mana_pool": {}},
        {"seat_id": local, "life_total": local_life, "is_local": True, "mana_pool": {}},
    ]


def attack_window(attacker_ids, opponent_seat):
    return {
        "type": "declare_attackers",
        "legal_attacker_ids": list(attacker_ids),
        "raw_attackers": [
            {
                "attackerInstanceId": identity,
                "legalDamageRecipients": [{**PLAYER, "playerSystemSeatId": opponent_seat}],
            }
            for identity in attacker_ids
        ],
    }


def block_window(raw_blockers, attacker_ids):
    return {
        "type": "declare_blockers",
        "legal_blocker_ids": [entry["blockerInstanceId"] for entry in raw_blockers],
        "raw_blockers": raw_blockers,
        "attacker_ids": list(attacker_ids),
    }


# --- #12: Shark Shredder's reanimation counts in block math -------------------------

SHARK_TEXT = (
    "Sneak {3}{B}{B}\nFirst strike\nWhenever Shark Shredder deals combat damage to a player, put up to "
    "one target creature card from that player's graveyard onto the battlefield under your control. It "
    "enters tapped and attacking that player."
)
HARE_TEXT = (
    "When this creature enters, create a number of 1/1 white Rabbit creature tokens equal to the number "
    "of other creatures you control named Hare Apparent.\nA deck can have any number of cards named Hare "
    "Apparent."
)
LEONARDO_TEXT = "Sneak {W}\nLeonardo gets +1/+0 for each other creature you control."
US_BRAWL, THEM_BRAWL = 2, 1


def shark_board(*, graveyard=True):
    """T21 of match cd41ad5a at 07:40:28: Shark 4/4, Super Shredder 8/7 menace and our stolen Hare attack."""
    return {
        "turn": {"turn_number": 21, "active_player": THEM_BRAWL, "phase": "Phase_Combat"},
        "players": players(US_BRAWL, 14, THEM_BRAWL, 6),
        "battlefield": [
            creature(
                468,
                "Shark Shredder, Killer Clone",
                4,
                4,
                THEM_BRAWL,
                SHARK_TEXT,
                type_line="Legendary Creature — Shark Octopus Ninja",
                keywords=["first strike"],
                is_attacking=True,
            ),
            creature(
                605,
                "Super Shredder",
                8,
                7,
                THEM_BRAWL,
                "Menace\nWhenever another permanent leaves the battlefield, put a +1/+1 counter on Super Shredder.",
                keywords=["menace"],
                is_attacking=True,
            ),
            {
                **creature(613, "Hare Apparent", 2, 2, THEM_BRAWL, HARE_TEXT, is_attacking=True),
                "owner_seat_id": US_BRAWL,
            },
            creature(575, "Leonardo, Big Brother", 1, 3, US_BRAWL, LEONARDO_TEXT),
        ],
        "graveyard": (
            [creature(616, "Hare Apparent", 2, 2, US_BRAWL, HARE_TEXT, type_line="Creature — Rabbit Noble")]
            if graveyard
            else []
        ),
        "hand": [],
        "decision_context": block_window(
            [{"blockerInstanceId": 575, "attackerInstanceIds": [468, 613], "maxAttackers": 1}],
            [468, 605, 613],
        ),
    }


def test_shark_shredders_first_strike_reanimation_adds_the_best_creature_in_our_graveyard():
    state = shark_board()
    shark = state["battlefield"][0]
    assert hit_bonus(shark, state, US_BRAWL) == 2
    assert hit_bonus(shark, shark_board(graveyard=False), US_BRAWL) == 0
    # Without first strike the creature enters after combat damage: nothing more this combat.
    assert (
        hit_bonus(
            {**shark, "keywords": [], "oracle_text": SHARK_TEXT.replace("First strike\n", "")}, state, 2
        )
        == 0
    )
    marked = {**shark, "combat_hit_bonus": 2}
    assert _resolve_attacker(marked, []).damage_through == 6
    leonardo = state["battlefield"][3]
    assert _resolve_attacker(marked, [leonardo]).damage_through == 0  # blocked: no trigger


def test_block_guard_counts_the_reanimated_hare_and_chumps_shark(caplog):
    state = shark_board()
    plan = ActionPlan(
        actions=[
            GameAction(
                ActionType.DECLARE_BLOCKERS,
                blocker_assignments={"Leonardo, Big Brother": "Hare Apparent"},
                blocker_instance_assignments={575: 613},
            )
        ]
    )
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        ActionPlanner.__new__(ActionPlanner)._check_block_survival(plan, state, state["decision_context"])
    assert "let 14 damage through at 14 life" in caplog.text
    assert plan.actions[0].blocker_instance_assignments == {575: 468}
    assert plan.fallback_reason == "planner_lethal_block"


def test_safe_default_and_chump_guard_keep_leonardo_on_shark():
    state = shark_board()
    blocks, reason = safe_default_blocks(state)
    assert blocks == {575: 468}
    assert "10 damage at 14 life" in reason
    assert wasteful_chump_blocks(state, {575: 468}) == {}


# --- #14: the losing-attack guard judges each attacker ------------------------------

US, THEM = 2, 1
CHALLENGER_TEXT = (
    "Haste\nProwess (Whenever you cast a noncreature spell, this creature gets +1/+1 until end of turn.)\n"
    "Valiant — Whenever this creature becomes the target of a spell or ability you control for the first "
    "time each turn, exile the top card of your library. Until end of turn, you may play that card."
)
CLAW_TEXT = (
    "Whenever you attack with one or more Lizards, this creature deals 1 damage to target opponent.\n"
    "{1}{R}: Put a +1/+1 counter on this creature. Activate only if an opponent lost life this turn and "
    "only once each turn."
)
LYRA_TEXT = (
    "Flying\nFirst strike (This creature deals combat damage before creatures without first strike.)\n"
    "Lifelink (Damage dealt by this creature also causes you to gain that much life.)\nOther Angels you "
    "control get +1/+1 and have lifelink."
)
SLICKSHOT_TEXT = (
    "Flying, haste\nWhenever you cast a noncreature spell, this creature gets +2/+0 until end of turn.\n"
    "Plot {1}{R} (You may pay {1}{R} and exile this card from your hand. Cast it as a sorcery on a later "
    "turn without paying its mana cost. Plot only as a sorcery.)"
)


def challenger():
    return creature(
        368, "Emberheart Challenger", 3, 3, US, CHALLENGER_TEXT, type_line="Creature — Mouse Warrior"
    )


def claw(iid=387, power=1, toughness=2):
    return creature(
        iid, "Hired Claw", power, toughness, US, CLAW_TEXT, type_line="Creature — Lizard Mercenary"
    )


def lyra(iid=392, power=5, toughness=5, text=LYRA_TEXT):
    return creature(
        iid,
        "Lyra Dawnbringer",
        power,
        toughness,
        THEM,
        text,
        type_line="Legendary Creature — Angel",
        keywords=["flying", "first strike", "lifelink"] if "Lifelink" in text else ["flying", "first strike"],
    )


def lyra_board(ours=None, theirs=None):
    """T10 of match f9e0ec3b at 09:19:36: both our lands tapped for Lightning Strike."""
    ours = [challenger(), claw()] if ours is None else ours
    theirs = [lyra()] if theirs is None else theirs
    return {
        "turn": {"turn_number": 10, "active_player": US, "phase": "Phase_Combat"},
        "players": players(US, 16, THEM, 18),
        "battlefield": [
            land(281, US),
            land(282, US),
            *ours,
            *theirs,
            {
                "instance_id": 380,
                "name": "Authority of the Consuls",
                "type_line": "Enchantment",
                "card_types": ["CardType_Enchantment"],
                "owner_seat_id": THEM,
                "controller_seat_id": THEM,
                "oracle_text": "Creatures your opponents control enter tapped.\nWhenever a creature an "
                "opponent controls enters, you gain 1 life.",
            },
        ],
        "hand": [
            claw(399),
            {
                "instance_id": 400,
                "name": "Burst Lightning",
                "type_line": "Instant",
                "mana_cost": "{R}",
                "owner_seat_id": US,
                "oracle_text": "Kicker {4}\nBurst Lightning deals 2 damage to any target. If this spell was "
                "kicked, it deals 4 damage instead.",
            },
        ],
        "graveyard": [],
        "decision_context": attack_window([card["instance_id"] for card in ours], THEM),
    }


STRATEGY = "Race the evasive angel clock with every point of ground damage plus burn reach."


def test_a_second_attacker_no_longer_shields_the_doomed_challenger():
    losing = losing_attackers(lyra_board(), [368, 387], strategy_text=STRATEGY)
    assert 368 in losing
    assert "Lyra Dawnbringer 5/5 can block Emberheart Challenger 3/3, kill it and survive" in losing[368]
    assert "lifelink gains them 5 life" in losing[368]


def test_hired_claws_one_damage_ping_does_not_pay_for_feeding_it_to_a_lifelinker():
    # 2026-10-19 10:25 T15 (match a51dea64): Claw 2/3 into two untapped Lyras.
    board = lyra_board([claw(power=2, toughness=3)], [lyra(392, 6, 6), lyra(393, 6, 6)])
    losing = losing_attackers(board, [387])
    assert set(losing) == {387}
    assert "its attack trigger is worth only 1" in losing[387]
    # The f9e0ec3b board: once the Challenger is held, Lyra would eat the Claw too.
    assert set(losing_attackers(lyra_board(), [368, 387], strategy_text=STRATEGY)) == {368, 387}


def test_a_ping_with_nobody_to_eat_it_still_attacks():
    board = lyra_board([claw()], [creature(392, "Wall", 0, 4, THEM, "Defender", keywords=["defender"])])
    assert losing_attackers(board, [387]) == {}


def test_a_lizard_only_payoff_on_a_home_claw_does_not_cover_the_mouse():
    assert set(losing_attackers(lyra_board(), [368], strategy_text=STRATEGY)) == {368}
    # A payoff for any attacker still keeps every attack.
    board = lyra_board()
    board["battlefield"].append(
        creature(
            450,
            "Captain",
            2,
            2,
            US,
            "Whenever a creature you control attacks, it gets +1/+0.",
            is_tapped=True,
        )
    )
    assert losing_attackers(board, [368], strategy_text=STRATEGY) == {}


def test_blocker_lifelink_is_a_cost_of_the_pressure():
    ogre = creature(369, "Ogre", 3, 3, US)
    plain_angel = lyra(text="Flying\nFirst strike")
    # Two 3/3s against one 5/5: whichever it eats, 3 gets through. That pressure pays for the attack...
    assert losing_attackers(lyra_board([challenger(), ogre], [plain_angel]), [368, 369]) == {}
    # ...until the blocker's lifelink gains back more than the pressure deals.
    assert set(losing_attackers(lyra_board([challenger(), ogre], [lyra()]), [368, 369])) == {368, 369}


def test_planner_holds_back_the_challenger_and_the_claw(caplog):
    state = lyra_board()
    plan = ActionPlan(
        actions=[
            GameAction(
                ActionType.DECLARE_ATTACKERS,
                attacker_names=["Emberheart Challenger", "Hired Claw"],
                attacker_instance_ids=[368, 387],
                attacker_targets={"Emberheart Challenger": "Opponent", "Hired Claw": "Opponent"},
            )
        ],
        overall_strategy=STRATEGY,
    )
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        ActionPlanner.__new__(ActionPlanner)._check_losing_attacks(plan, state, state["decision_context"])
    assert plan.actions[0].attacker_instance_ids == []
    assert plan.fallback_reason == "planner_losing_attack"
    assert "Emberheart Challenger [368]" in caplog.text and "Hired Claw [387]" in caplog.text


MOSEO_TEXT = (
    'Flying\nWhen Moseo enters, create a 1/1 black and green Pest creature token with "Whenever this token '
    'attacks, you gain 1 life."\nInfusion — At the beginning of your end step, if you gained life this '
    "turn, return up to one target creature card with mana value X or less from your graveyard to the "
    "battlefield, where X is the amount of life you gained this turn."
)
PLAN_TEXT = (
    "attack: Slickshot Show-Off (flying; their only blocker Moseo has shadow and cannot block. "
    "Swing Slickshot every turn."
)


def moseo_board(slickshot_power=1):
    """08:09:17 (match a74bbc90 T8): Slickshot against Moseo and three ground creatures."""
    slickshot = creature(
        303, "Slickshot Show-Off", slickshot_power, 2, US, SLICKSHOT_TEXT, keywords=["flying", "haste"]
    )
    return {
        "turn": {"turn_number": 8, "active_player": US, "phase": "Phase_Combat"},
        "players": players(US, 17, THEM, 22),
        "battlefield": [
            land(281, US),
            slickshot,
            creature(347, "Moseo, Vein's New Dean", 2, 1, THEM, MOSEO_TEXT, keywords=["flying"]),
            creature(358, "Virulent Emissary", 1, 1, THEM, "Deathtouch", keywords=["deathtouch"]),
            creature(
                320,
                "Hinterland Sanctifier",
                1,
                2,
                THEM,
                "Whenever another creature you control enters, you gain 1 life.",
            ),
            creature(
                354,
                "Pest",
                1,
                1,
                THEM,
                "Whenever this token attacks, you gain 1 life.",
                type_line="Token Creature — Pest",
            ),
        ],
        "hand": [],
        "graveyard": [],
        "decision_context": attack_window([303], THEM),
    }


def test_a_flier_trading_with_a_flier_is_a_trade_not_a_loss(caplog):
    with caplog.at_level(logging.INFO, logger="arenamcp.combat_strategy"):
        assert losing_attackers(moseo_board(), [303], strategy_text=PLAN_TEXT) == {}
    assert "trade" in caplog.text and "Moseo" in caplog.text


def test_a_losing_trade_on_a_false_evasion_claim_is_still_held_and_says_trade():
    # Slickshot pumped to 3/2 (7 material) would trade with Moseo (5 without its recursion read as such).
    board = moseo_board(slickshot_power=3)
    board["battlefield"][2] = creature(
        347, "Momo, Playful Pet", 2, 2, THEM, "Flying\nVigilance", keywords=["flying", "vigilance"]
    )
    losing = losing_attackers(board, [303], strategy_text="Slickshot Show-Off swings in the air, unblocked.")
    assert set(losing) == {303} and "trade with it at a loss" in losing[303]
    # Without a claim that the block refutes, the same trade is kept.
    assert losing_attackers(board, [303], strategy_text="Pressure their life total.") == {}


# --- #18: crackback that our chump guard would never block -----------------------------

ELENDA_TEXT = (
    "Lifelink, hexproof from instants\nAs long as your life total is greater than your starting life "
    "total, Elenda gets +1/+1 and has menace. Elenda gets an additional +5/+5 as long as your life total "
    "is at least 10 greater than your starting life total."
)


def elenda_board(our_life=18):
    """T10 of match c36db3a3 at 07:51:28: Slickshot 1/2 against a summoning-sick Elenda 4/4."""
    return {
        "turn": {"turn_number": 10, "active_player": US, "phase": "Phase_Combat"},
        "players": players(US, our_life, THEM, 9),
        "battlefield": [
            land(281, US),
            creature(333, "Slickshot Show-Off", 1, 2, US, SLICKSHOT_TEXT, keywords=["flying", "haste"]),
            creature(
                337,
                "Elenda, Saint of Dusk",
                4,
                4,
                THEM,
                ELENDA_TEXT,
                keywords=["lifelink"],
                turn_entered_battlefield=9,
            ),
        ],
        "hand": [],
        "graveyard": [],
        "decision_context": attack_window([333], THEM),
    }


def test_slickshots_attack_is_free_when_holding_it_would_only_chump_at_eighteen():
    free = free_attackers(elenda_board())
    assert free is not None and free.attacker_ids == [333]
    assert free.hold_crackback == free.crackback == 4


def test_the_crackback_forecast_keeps_chumps_that_matter():
    slickshot, elenda = elenda_board()["battlefield"][1:]
    assert guarded_block_plan([elenda], [slickshot], 18).assignments == {}
    assert guarded_block_plan([elenda], [slickshot], 6).assignments == {333: 337}


# --- #25: creatures that leave at our end step do not block next turn -------------------

EFFIGY_TEXT = (
    "Haste\nAt the beginning of your end step, return this creature to its owner's hand. (Return it only "
    "if it's on the battlefield.)\n{2}{R}: This creature gets +2/+0 until end of turn."
)


def test_leaving_at_our_end_step_is_read_from_unconditional_text_only():
    assert leaves_at_our_end_step(creature(1, "Fleeting Effigy", 2, 2, US, EFFIGY_TEXT))
    assert leaves_at_our_end_step(
        creature(
            2,
            "Ball Lightning",
            6,
            1,
            US,
            "Trample, haste\nAt the beginning of the end step, sacrifice Ball Lightning.",
        )
    )
    unearth = "Unearth {R} (Return this card from your graveyard to the battlefield. It gains haste. Exile it at the beginning of the next end step or if it would leave the battlefield. Unearth only as a sorcery.)"
    assert not leaves_at_our_end_step(creature(3, "Ghoul", 2, 2, US, unearth))
    assert not leaves_at_our_end_step(
        creature(
            4,
            "Dweller",
            2,
            2,
            US,
            "At the beginning of your end step, if you control no other creatures, sacrifice this creature.",
        )
    )


def chocobo_board():
    """T10 of match 92c09c7a at 09:34:24 (the model was down; the solver chose the attack)."""
    return {
        "turn": {"turn_number": 10, "active_player": US, "phase": "Phase_Combat"},
        "players": players(US, 10, THEM, 11),
        "battlefield": [
            creature(491, "Slickshot Show-Off", 3, 2, US, SLICKSHOT_TEXT, keywords=["flying", "haste"]),
            creature(577, "Fleeting Effigy", 2, 2, US, EFFIGY_TEXT, keywords=["haste"]),
            creature(
                402,
                "Sazh's Chocobo",
                7,
                8,
                THEM,
                "Landfall — Whenever a land you control enters, put a +1/+1 counter on this creature.",
                is_tapped=True,
            ),
            creature(501, "Llanowar Elves", 1, 1, THEM, "{T}: Add {G}."),
        ],
        "hand": [],
        "graveyard": [],
        "decision_context": attack_window([491, 577], THEM),
    }


def test_the_solver_attacks_with_effigy_and_keeps_slickshot_home():
    choice = combat_choice(chocobo_board())
    assert set(choice.assignments) == {577}
    # Effigy home blocks nothing: holding everything still takes the Chocobo's 7 unless Slickshot chumps.
    assert choice.crackback <= 1


def cerberus_board():
    """T13 of match 1e4ccb0c at 10:14:29: Effigy cast precombat; Ghoul entered tapped, Cerberus stays tapped."""
    return {
        "turn": {"turn_number": 13, "active_player": 1, "phase": "Phase_Combat"},
        "players": players(1, 13, 2, 12),
        "battlefield": [
            *(land(270 + i, 1, tapped=i < 1) for i in range(6)),
            creature(
                359, "Fleeting Effigy", 2, 2, 1, EFFIGY_TEXT, keywords=["haste"], turn_entered_battlefield=13
            ),
            creature(355, "Diregraf Ghoul", 2, 2, 2, "This creature enters tapped.", is_tapped=True),
            creature(
                351,
                "Perforating Artist",
                3,
                2,
                2,
                "Deathtouch\nRaid — At the beginning of your end step, if you attacked this turn, each opponent "
                "loses 3 life unless that player sacrifices a nonland permanent of their choice or discards a card.",
                keywords=["deathtouch"],
                turn_entered_battlefield=12,
            ),
            creature(
                344,
                "Slumbering Cerberus",
                4,
                2,
                2,
                "This creature doesn't untap during your untap step.\nMorbid — At the beginning of each end "
                "step, if a creature died this turn, untap this creature.",
                is_tapped=True,
            ),
        ],
        "hand": [],
        "graveyard": [],
        "decision_context": {**attack_window([359], 2), "legal_attackers": ["Fleeting Effigy"]},
    }


def test_crackback_drops_blockers_killed_in_the_attack_and_creatures_that_stay_tapped():
    board = cerberus_board()
    effigy = board["battlefield"][6]
    theirs = board["battlefield"][7:]
    untapped = [card for card in theirs if not card.get("is_tapped")]
    attack = evaluate_attack([effigy], [], untapped, 12, 13, theirs)
    # Artist dies blocking; Cerberus untaps at our end step (morbid: a creature died).
    assert attack.worst_case_crackback == 6
    hold = evaluate_attack([], [effigy], untapped, 12, 13, theirs, creature_died_this_turn=False)
    assert hold.worst_case_crackback == 5  # Effigy blocks nothing; Cerberus stays tapped
    unknown = evaluate_attack([], [effigy], untapped, 12, 13, theirs)
    assert unknown.worst_case_crackback == 9  # nobody known to have died: assume morbid may untap it
    cerberus = theirs[2]
    assert not can_attack_next_turn(cerberus, creature_died=False)
    assert can_attack_next_turn(cerberus, creature_died=True)
    assert not can_attack_next_turn(
        {**cerberus, "oracle_text": "This creature doesn't untap during your untap step."}
    )


def test_the_bridge_attack_override_keeps_the_effigy_attack():
    from arenamcp.autopilot_bridge import _BridgeSubmitMixin

    board = cerberus_board()
    pilot = _BridgeSubmitMixin()
    pilot._find_instance_id = lambda name, battlefield, local: next(
        (card["instance_id"] for card in battlefield if card.get("name") == name), None
    )
    assert pilot._attack_override(["Fleeting Effigy"], copy.deepcopy(board)) is None


# --- #22: free blocks are always declared ------------------------------------------------

PEST_TEXT = "Whenever this token attacks, you gain 1 life."


def pest_board():
    """T9 of match a74bbc90 at 08:10:02: a 1/1 Pest and a 1/1 deathtouch Emissary attack."""
    return {
        "turn": {"turn_number": 9, "active_player": THEM, "phase": "Phase_Combat"},
        "players": players(US, 17, THEM, 26),
        "battlefield": [
            creature(303, "Slickshot Show-Off", 1, 2, US, SLICKSHOT_TEXT, keywords=["flying", "haste"]),
            creature(
                354, "Pest", 1, 1, THEM, PEST_TEXT, type_line="Token Creature — Pest", is_attacking=True
            ),
            creature(
                358, "Virulent Emissary", 1, 1, THEM, "Deathtouch", keywords=["deathtouch"], is_attacking=True
            ),
            creature(
                366,
                "Essence Channeler",
                9,
                8,
                THEM,
                "Whenever you gain life, put a +1/+1 counter on this creature.",
            ),
        ],
        "hand": [],
        "graveyard": [],
        "decision_context": block_window(
            [{"blockerInstanceId": 303, "attackerInstanceIds": [354, 358], "maxAttackers": 1}], [354, 358]
        ),
    }


def test_slickshot_blocks_the_pest_for_free():
    blocks = free_blocks(pest_board())
    assert {blocker: attacker for blocker, (attacker, _why) in blocks.items()} == {303: 354}


def test_no_free_block_against_deathtouch_or_a_visible_pump():
    board = pest_board()
    board["battlefield"][1]["keywords"] = ["deathtouch"]
    assert free_blocks(board) == {}
    board = pest_board()
    board["battlefield"].append(
        creature(370, "Pump Lord", 1, 1, THEM, "{1}: Target creature gets +1/+1 until end of turn.")
    )
    board["battlefield"].append(land(371, THEM, tapped=False))
    assert free_blocks(board) == {}


def test_planner_declares_the_free_block_the_model_declined(caplog):
    state = pest_board()
    plan = ActionPlan(
        actions=[GameAction(ActionType.CLICK_BUTTON, card_name="done")],
        overall_strategy="Preserve Slickshot as the sole wincon; absorb minor damage.",
    )
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        ActionPlanner.__new__(ActionPlanner)._ensure_free_blocks(plan, state, state["decision_context"])
    action = plan.actions[0]
    assert action.action_type == ActionType.DECLARE_BLOCKERS
    assert action.blocker_instance_assignments == {303: 354}
    assert plan.fallback_reason == "planner_free_block"
    assert "Free block" in caplog.text


# --- #34: a withdrawn attacker is not attacking --------------------------------------------

MOMO = {
    "instanceId": 370,
    "grpId": 97304,
    "type": "GameObjectType_Card",
    "zoneId": 28,
    "visibility": "Visibility_Public",
    "ownerSeatId": 1,
    "controllerSeatId": 1,
    "superTypes": ["SuperType_Legendary"],
    "cardTypes": ["CardType_Creature"],
    "subtypes": ["SubType_Lemur", "SubType_Bat", "SubType_Ally"],
    "power": {"value": 5},
    "toughness": {"value": 5},
    "attackInfo": {"targetId": 2},
    "uniqueAbilities": [
        {"id": 257, "grpId": 8},
        {"id": 258, "grpId": 15},
        {"id": 259, "grpId": 192576},
        {"id": 292, "grpId": 3},
        {"id": 297, "grpId": 12},
    ],
}
HYDRA = {
    "instanceId": 385,
    "grpId": 106236,
    "type": "GameObjectType_Card",
    "zoneId": 28,
    "visibility": "Visibility_Public",
    "ownerSeatId": 1,
    "controllerSeatId": 1,
    "cardTypes": ["CardType_Creature"],
    "subtypes": ["SubType_Hydra", "SubType_Horror"],
    "power": {"value": 4},
    "toughness": {"value": 3},
    "uniqueAbilities": [{"id": 272, "grpId": 76885}, {"id": 273, "grpId": 208094}],
}


def test_an_attacker_resent_without_attack_state_is_withdrawn():
    from arenamcp.gamestate import GameState

    state = GameState()
    declared = {"attackState": "AttackState_Declared", "attackInfo": {"targetId": 2}}
    # Player.log gsid 268, 269 and 270 (match 6175f002, T9 at 10:03:56-59).
    state.update_from_message(
        {
            "type": "GameStateType_Diff",
            "gameStateId": 268,
            "gameObjects": [{**MOMO, **declared}, {**HYDRA, **declared}],
        }
    )
    assert state.game_objects[385].is_attacking
    state.update_from_message({"type": "GameStateType_Diff", "gameStateId": 269, "gameObjects": [HYDRA]})
    state.update_from_message(
        {
            "type": "GameStateType_Diff",
            "gameStateId": 270,
            "gameObjects": [{**MOMO, "attackState": "AttackState_Attacking"}],
        }
    )
    assert not state.game_objects[385].is_attacking
    assert state.game_objects[370].is_attacking
    assert set(state.game_objects[370].keywords) >= {"double strike", "lifelink", "flying"}


MOMO_TEXT = (
    "Flying, vigilance\nWhen Momo leaves the battlefield, choose one —\n• Create a Food token.\n• Put a "
    "+1/+1 counter on target creature you control.\n• Scry 2."
)


def momo_board():
    """T9 of match 6175f002 at 10:03:59 as our state had it: Hydra still flagged attacking."""
    return {
        "turn": {"turn_number": 9, "active_player": THEM, "phase": "Phase_Combat"},
        "players": players(US, 12, THEM, 15),
        "battlefield": [
            creature(375, "Slickshot Show-Off", 1, 2, US, SLICKSHOT_TEXT, keywords=["flying", "haste"]),
            creature(
                370,
                "Momo, Playful Pet",
                5,
                5,
                THEM,
                MOMO_TEXT,
                keywords=["flying", "vigilance", "double strike", "lifelink"],
                is_attacking=True,
            ),
            creature(
                385,
                "Guiding Hydra",
                4,
                3,
                THEM,
                "Guiding Hydra enters with X +1/+1 counters on it.\nAt the beginning of combat on your turn, you "
                "may remove a +1/+1 counter from Guiding Hydra. If you do, put a +1/+1 counter on each other "
                "creature you control.",
                is_attacking=True,
            ),
        ],
        "hand": [],
        "graveyard": [],
        "decision_context": block_window(
            [{"blockerInstanceId": 375, "attackerInstanceIds": [370], "maxAttackers": 1}], [370, 385]
        ),
    }


def test_the_blockers_request_shows_the_hydra_is_not_attacking():
    state = momo_board()
    assert phantom_attackers(state, [370, 385]) == {385}
    # A flyer our ground blocker cannot block is never listed and stays an attacker.
    grounded = copy.deepcopy(state)
    grounded["battlefield"][0]["keywords"] = ["haste"]
    assert phantom_attackers(grounded, [370]) == set()


def test_the_10_04_05_guard_still_chumps_momo_without_the_phantom(caplog):
    state = momo_board()
    plan = ActionPlan(actions=[GameAction(ActionType.DECLARE_BLOCKERS)])
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        ActionPlanner.__new__(ActionPlanner)._check_block_survival(plan, state, state["decision_context"])
    # 10 from Momo's double strike (not 14 with the withdrawn Hydra) leaves us at 2: chump it.
    assert "let 10 damage through at 12 life" in caplog.text
    assert plan.actions[0].blocker_instance_assignments == {375: 370}


# --- #35: the block prompt counts double strike and granted keywords ------------------------


def test_block_prompt_counts_momos_double_strike_and_drops_preserve_blockers():
    from arenamcp.coach import CoachEngine

    state = momo_board()
    ours = [card for card in state["battlefield"] if card["controller_seat_id"] == US]
    theirs = [card for card in state["battlefield"] if card["controller_seat_id"] == THEM]
    local = next(player for player in state["players"] if player["is_local"])
    lines = CoachEngine.__new__(CoachEngine)._format_block_combat(
        ours, theirs, local, 9, "Combat", set(), state["decision_context"], state
    )
    prompt = "\n".join(lines)
    assert "No blocks → take 10 dmg → 2 life remaining" in prompt
    attackers = next(line for line in lines if line.startswith("Attackers:"))
    assert attackers == "Attackers: Momo, Playful Pet 5/5 [FLYING,DOUBLE-STRIKE,LIFELINK,VIGILANCE]"
    # Its first-strike hit kills Slickshot; the regular hit has no blocker left to damage.
    assert (
        "If Slickshot Show-Off 1/2 blocks Momo, Playful Pet 5/5: Slickshot Show-Off dies, Momo, Playful "
        "Pet lives (4 left); Momo, Playful Pet's lifelink gains 5 life"
    ) in prompt
    assert "Preserve blockers!" not in prompt
    assert "Not blocking is worse: follow the computed blocks." in prompt
    assert "Computed optimal blocks: Slickshot Show-Off blocks Momo, Playful Pet" in prompt


def test_board_line_flags_show_granted_double_strike_and_lifelink():
    from arenamcp.coach import CoachEngine

    momo = momo_board()["battlefield"][1]
    lines = CoachEngine.__new__(CoachEngine)._format_board_card(
        momo,
        attachments={},
        name_counts={"Momo, Playful Pet": 1},
        name_seen={},
        turn_num=9,
        local_seat=US,
        is_local=False,
    )
    assert "DS" in lines[0].split("[")[1] and "LL" in lines[0].split("[")[1]


def test_shark_prompt_names_the_reanimation_damage():
    from arenamcp.coach import CoachEngine

    state = shark_board()
    ours = [card for card in state["battlefield"] if card["controller_seat_id"] == US_BRAWL]
    theirs = [card for card in state["battlefield"] if card["controller_seat_id"] == THEM_BRAWL]
    local = next(player for player in state["players"] if player["is_local"])
    prompt = "\n".join(
        CoachEngine.__new__(CoachEngine)._format_block_combat(
            ours, theirs, local, 21, "Combat", set(), state["decision_context"], state
        )
    )
    assert "No blocks → 16 dmg → DEAD" in prompt
    assert "+2 more if it hits you" in prompt
