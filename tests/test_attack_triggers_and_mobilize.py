"""Regression tests for Mobilize N, tapped-and-attacking tokens, attack-trigger damage/drain,
and unmodeled opponent attack-trigger safety / E4 escalation (ssh2n T7 Arabella + Packbeasts)."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from arenamcp import board_assessment as ba
from arenamcp import combat_solver as cs_solver
from arenamcp import combat_strategy as cs_strat
from arenamcp.attack_potential import (
    attack_damage_of,
    attack_tokens_of,
    attack_trigger_drain,
)
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.selective_thinking import pivotal_evidence

ME = 1
OPP = 2


def _card(
    iid: int,
    name: str,
    seat: int,
    *,
    power: int | None = None,
    toughness: int | None = None,
    oracle: str = "",
    keywords: list[str] | None = None,
    type_line: str = "Creature",
    tapped: bool = False,
    attacking: bool = False,
    token: bool = False,
) -> dict:
    return {
        "instance_id": iid,
        "name": name,
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "power": power,
        "toughness": toughness,
        "oracle_text": oracle,
        "keywords": list(keywords or ()),
        "type_line": type_line,
        "card_types": ["CardType_Land"] if "Land" in type_line else ["CardType_Creature"],
        "subtypes": [],
        "is_tapped": tapped,
        "is_attacking": attacking,
        "is_token": token,
        "counters": {},
    }


def _ssh2n_t7_board(
    *, mobilize: bool = True, step: str = "Step_DeclareAttack", our_turn: bool = True
) -> dict:
    arabella = _card(
        293,
        "Arabella, Abandoned Doll",
        OPP,
        power=1,
        toughness=3,
        oracle=(
            "Whenever Arabella attacks, it deals X damage to each opponent and you gain X life, "
            "where X is the number of creatures you control with power 2 or less."
        ),
        type_line="Legendary Artifact Creature — Toy",
        tapped=our_turn,
    )
    packbeasts = _card(
        304,
        "Dalkovan Packbeasts",
        OPP,
        power=0,
        toughness=4,
        oracle="Vigilance\nMobilize 3" if mobilize else "Vigilance",
        keywords=["vigilance", "mobilize 3"] if mobilize else ["vigilance"],
        type_line="Creature — Ox",
    )
    bf = [
        _card(281, "Plains", ME, type_line="Basic Land — Plains", tapped=True),
        _card(287, "Plains", ME, type_line="Basic Land — Plains", tapped=True),
        _card(282, "Optimistic Scavenger", ME, power=2, toughness=2),
        _card(288, "Skyward Spider", ME, power=6, toughness=6, keywords=["flying", "first strike"]),
        _card(300, "Optimistic Scavenger", ME, power=1, toughness=1),
        _card(316, "Spider", ME, power=2, toughness=1, keywords=["reach"], token=True),
        _card(285, "Godless Shrine", OPP, type_line="Land", tapped=True),
        _card(292, "Mountain", OPP, type_line="Basic Land — Mountain", tapped=True),
        _card(303, "Sacred Foundry", OPP, type_line="Land", tapped=True),
        arabella,
        packbeasts,
    ]
    return {
        "match_id": "test-ssh2n",
        "local_seat_id": ME,
        "opponent_hand_count": 5,
        "turn": {
            "turn_number": 7 if our_turn else 8,
            "active_player": ME if our_turn else OPP,
            "phase": "Phase_Combat",
            "step": step,
        },
        "players": [
            {"seat_id": ME, "life_total": 17, "is_local": True, "lands_played": 0},
            {"seat_id": OPP, "life_total": 15, "is_local": False, "lands_played": 0},
        ],
        "battlefield": bf,
        "hand": [],
        "graveyard": [],
        "exile": [],
        "stack": [],
    }


def test_mobilize_dedup_and_arabella_parsing():
    board = _ssh2n_t7_board()
    arabella = next(c for c in board["battlefield"] if c["instance_id"] == 293)
    packbeasts = next(c for c in board["battlefield"] if c["instance_id"] == 304)

    toks = attack_tokens_of(packbeasts)
    assert len(toks) == 1
    assert (toks[0].count, toks[0].power, toks[0].toughness, toks[0].ephemeral) == (3, 1, 1, True)

    dmgs = attack_damage_of(arabella)
    assert len(dmgs) == 1
    assert (dmgs[0].kind, dmgs[0].amount, dmgs[0].gains_x) == ("count_power_le", 2, True)


def test_ssh2n_t7_board_assessment_sees_mobilize_and_arabella():
    ba._CACHE.clear()
    facts = ba.assess(_ssh2n_t7_board())
    assert facts is not None
    # Arabella (1/3) + Packbeasts (0/4) + three 1/1 Warriors attack:
    # 5 Arabella drain lands pre-combat (17 -> 12), then untapped blockers stop the rest -> (5, 12).
    assert facts.their_next_attack == (5, 12)
    assert facts.clock_unmodelled == []
    assert facts.opponent_clock_unmodelled == []
    threat_map = {t.name: t.why for t in facts.threats}
    assert "attack trigger" in threat_map["Arabella, Abandoned Doll"]
    assert "attack trigger" in threat_map["Dalkovan Packbeasts"]

    # Control: without Mobilize 3, Arabella only sees Arabella + Ox = 2 (17 -> 15).
    ba._CACHE.clear()
    no_mob_facts = ba.assess(_ssh2n_t7_board(mobilize=False))
    assert no_mob_facts is not None
    assert no_mob_facts.their_next_attack == (2, 15)


def test_ssh2n_t7_combat_solver_crackback_with_and_without_all_in():
    board = _ssh2n_t7_board()
    ours = [c for c in board["battlefield"] if c["controller_seat_id"] == ME and c.get("power") is not None]
    theirs = [
        c for c in board["battlefield"] if c["controller_seat_id"] == OPP and c.get("power") is not None
    ]
    spider = [c for c in ours if c["instance_id"] == 316]
    attackers = [c for c in ours if c["instance_id"] in (282, 288, 300)]

    # Attacking with Spider + Scavengers leaving 2/1 Spider token home -> crackback 8 (3 combat + 5 Arabella).
    plan_hold = cs_solver.evaluate_attack(
        attackers,
        spider,
        [c for c in theirs if not c.get("is_tapped")],
        15,
        17,
        theirs,
        state=board,
    )
    assert plan_hold.worst_case_crackback == 8

    # All-in attack leaving 0 defenders -> crackback 9 (4 combat + 5 Arabella).
    plan_all = cs_solver.evaluate_attack(
        ours,
        [],
        [c for c in theirs if not c.get("is_tapped")],
        15,
        17,
        theirs,
        state=board,
    )
    assert plan_all.worst_case_crackback == 9

    # Control: without Mobilize 3, Arabella only sees Arabella + Ox (2) + 1 combat damage = 3.
    no_mob = _ssh2n_t7_board(mobilize=False)
    theirs_no_mob = [
        c for c in no_mob["battlefield"] if c["controller_seat_id"] == OPP and c.get("power") is not None
    ]
    plan_no_mob = cs_solver.evaluate_attack(
        ours,
        [],
        [c for c in theirs_no_mob if not c.get("is_tapped")],
        15,
        17,
        theirs_no_mob,
        state=no_mob,
    )
    assert plan_no_mob.worst_case_crackback == 3


def test_their_next_attack_includes_mobilize_tokens_and_respects_declared_control():
    board = _ssh2n_t7_board()
    theirs = [
        c for c in board["battlefield"] if c["controller_seat_id"] == OPP and c.get("power") is not None
    ]
    threats, combats = cs_strat._their_next_attack(board, theirs, ME)
    assert combats == 1
    assert len(threats) == 5
    assert sum(c["power"] for c in threats) == 4

    # Declared attack control: once attackers are declared, Mobilize/Arabella triggers already resolved.
    assert attack_trigger_drain(threats, side_creatures=theirs, declared=True) == (0, 0)


def test_general_tapped_and_attacking_tokens_and_hellrider_distinction():
    adeline = _card(
        10,
        "Vanguard Summoner",
        OPP,
        power=2,
        toughness=4,
        oracle=(
            "Whenever Vanguard Summoner attacks, create two 1/1 white Soldier creature tokens "
            "that are tapped and attacking."
        ),
    )
    hellrider = _card(
        11,
        "Flame Rider",
        OPP,
        power=3,
        toughness=3,
        oracle="Whenever a creature you control attacks, Flame Rider deals 1 damage to each opponent.",
    )
    arabella = _card(
        12,
        "Arabella, Abandoned Doll",
        OPP,
        power=1,
        toughness=3,
        oracle=(
            "Whenever Arabella attacks, it deals X damage to each opponent and you gain X life, "
            "where X is the number of creatures you control with power 2 or less."
        ),
    )
    # 3 declared attackers (adeline, hellrider, arabella) + 2 created Soldier tokens = 5 creatures, 4 with power<=2.
    # Hellrider triggers 3 times (declared only, NOT created tokens) -> 3; Arabella triggers for 4 -> total 7 dmg, 4 gain.
    from arenamcp.attack_potential import projected_attack

    expanded, dmg, gain = projected_attack(
        [adeline, hellrider, arabella], side_creatures=[adeline, hellrider, arabella]
    )
    assert len(expanded) == 5
    assert dmg == 7
    assert gain == 4


def test_unmodelled_attack_keyword_flags_lower_bound_and_escalates_combat_only():
    weird_opp = _card(
        90,
        "Legions of Mystery",
        OPP,
        power=3,
        toughness=3,
        oracle="Myriad\nWhenever Legions of Mystery attacks, exile the top card of your library.",
        keywords=["Myriad"],
    )
    weird_mine = _card(
        91,
        "Friendly Battler",
        ME,
        power=3,
        toughness=3,
        oracle="Exalted",
        keywords=["Exalted"],
    )
    board = _ssh2n_t7_board()
    board["battlefield"] = [c for c in board["battlefield"] if c["instance_id"] not in (293, 304)] + [
        weird_opp
    ]
    ba._CACHE.clear()
    facts = ba.assess(board)
    assert facts.opponent_clock_unmodelled == ["Legions of Mystery (attack trigger)"]
    assert "(opponent crackback/clock is a lower bound)" in facts.facts_line()

    combat_dec = PendingDecision(
        request_type="DeclareAttackers",
        request_id=1,
        options=[
            DecisionOption(option_id="attack:288:player:2", label="Attack with Skyward Spider", payable=True)
        ],
    )
    reasons_combat = pivotal_evidence(combat_dec, board, assessment=facts)
    assert "opponent has unmodeled attack trigger" in reasons_combat

    # Non-combat ActionsAvailable does NOT escalate for opponent_clock_unmodelled.
    main_dec = PendingDecision(
        request_type="ActionsAvailable",
        request_id=2,
        options=[DecisionOption(option_id="pass", label="Pass", payable=True)],
    )
    reasons_main = pivotal_evidence(main_dec, board, assessment=facts)
    assert "opponent has unmodeled attack trigger" not in reasons_main

    # Control: unmodeled attack keyword on OUR own creature does NOT set opponent_clock_unmodelled.
    mine_board = deepcopy(board)
    mine_board["battlefield"] = [c for c in mine_board["battlefield"] if c["instance_id"] != 90] + [
        weird_mine
    ]
    ba._CACHE.clear()
    facts_mine = ba.assess(mine_board)
    assert facts_mine.opponent_clock_unmodelled == []
    assert "opponent has unmodeled attack trigger" not in pivotal_evidence(
        SimpleNamespace(request_type="DeclareAttackers", options=combat_dec.options, source_label=""),
        mine_board,
        assessment=facts_mine,
    )


def test_precombat_lethal_attack_trigger_beats_lifelink_blocker():
    board = _ssh2n_t7_board()
    board["players"] = [
        {"seat_id": ME, "life_total": 5, "is_local": True, "lands_played": 0},
        {"seat_id": OPP, "life_total": 15, "is_local": False, "lands_played": 0},
    ]
    # Single 5/5 lifelink blocker: Arabella + Packbeasts + 3 Warriors = 5 creatures with power <= 2,
    # so Arabella's attack trigger deals 5 BEFORE combat damage and lifelink healing cannot save us.
    lifelinker = _card(
        401,
        "Baneslayer Standin",
        ME,
        power=5,
        toughness=5,
        keywords=["lifelink"],
        oracle="Lifelink",
    )
    board["battlefield"] = [
        c for c in board["battlefield"] if c.get("controller_seat_id") == OPP or c.get("power") is None
    ] + [lifelinker]
    ba._CACHE.clear()
    facts = ba.assess(board)
    assert facts.their_next_attack == (5, 0)
    assert facts.their_clock == 1
    assert facts.opp_lethal_on_board is True


def test_post_trigger_life_guides_block_solver():
    board = _ssh2n_t7_board()
    board["players"] = [
        {"seat_id": ME, "life_total": 8, "is_local": True, "lands_played": 0},
        {"seat_id": OPP, "life_total": 15, "is_local": False, "lands_played": 0},
    ]
    arabella = _card(
        293,
        "Arabella, Abandoned Doll",
        OPP,
        power=1,
        toughness=3,
        oracle=(
            "Whenever Arabella attacks, it deals X damage to each opponent and you gain X life, "
            "where X is the number of creatures you control with power 2 or less."
        ),
        type_line="Legendary Artifact Creature — Toy",
    )
    walls = [
        _card(
            310 + i, f"Defender Wall {i}", OPP, power=0, toughness=4, keywords=["defender"], oracle="Defender"
        )
        for i in range(4)
    ]
    crusher = _card(305, "Big Beast", OPP, power=4, toughness=4)
    chump = _card(402, "Chump Soldier", ME, power=1, toughness=1, oracle="")
    board["battlefield"] = [arabella, *walls, crusher, chump]

    theirs = [arabella, *walls, crusher]
    plan = cs_solver.evaluate_attack(
        [],
        [chump],
        [arabella, *walls, crusher],
        15,
        8,
        theirs,
        state=board,
    )
    # 5 pre-combat Arabella damage drops defender from 8 -> 3, so Chump Soldier MUST chump
    # Big Beast (4/4): remaining combat damage is 1 (Arabella) -> total crackback = 6 (survives at 2 life!).
    # Without post-trigger life reduction (seeing life=8), chump guard could drop the chump and let 5 through -> lethal 10.
    assert plan.worst_case_crackback == 6


def test_disposable_token_only_removal_allows_planeswalkers_and_activated_permanents():
    from arenamcp.action_planner import _disposable_token_only_removal

    opp_bf = [
        {
            "instance_id": 10,
            "name": "Warrior Token",
            "controller_seat_id": OPP,
            "type_line": "Creature Token — Warrior",
            "power": 1,
            "toughness": 1,
            "is_token": True,
        }
    ]
    game_state = {
        "local_seat_id": ME,
        "turn": {"active_player": ME, "phase": "Phase_Main1"},
        "stack": [],
        "players": [{"seat_id": ME, "life_total": 20, "is_local": True}],
        "battlefield": list(opp_bf),
    }

    walker = {
        "instance_id": 501,
        "name": "Removal Walker",
        "type_line": "Legendary Planeswalker — Tester",
        "oracle_text": "-3: Exile target nonland permanent.",
    }
    seal = {
        "instance_id": 502,
        "name": "Artifact Seal",
        "type_line": "Artifact",
        "oracle_text": "{1}, Sacrifice Artifact Seal: Destroy target creature.",
    }
    seam_rip = {
        "instance_id": 503,
        "name": "Seam Rip",
        "type_line": "Enchantment",
        "oracle_text": (
            "When this enchantment enters, exile target nonland permanent an opponent controls "
            "with mana value 2 or less until this enchantment leaves the battlefield."
        ),
    }

    assert _disposable_token_only_removal(walker, game_state, opp_bf, ME) == (False, [])
    assert _disposable_token_only_removal(seal, game_state, opp_bf, ME) == (False, [])
    suppress, reasons = _disposable_token_only_removal(seam_rip, game_state, opp_bf, ME)
    assert suppress is True and reasons


def test_mobilize_x_flagged_unmodelled_and_granted_mobilize_not_parsed_as_innate():
    avenger = _card(
        92,
        "Avenger of the Fallen",
        OPP,
        power=2,
        toughness=4,
        oracle="Deathtouch\nMobilize X",
        keywords=["deathtouch", "mobilize x"],
    )
    sword = _card(
        93,
        "Banner Blade",
        OPP,
        oracle="Equipped creature has mobilize 2.",
        type_line="Artifact — Equipment",
    )
    assert attack_tokens_of(sword) == []
    assert attack_tokens_of(avenger) == []
    board = _ssh2n_t7_board()
    board["battlefield"] = [c for c in board["battlefield"] if c["instance_id"] not in (293, 304)] + [avenger]
    ba._CACHE.clear()
    facts = ba.assess(board)
    assert facts.opponent_clock_unmodelled == ["Avenger of the Fallen (attack trigger)"]
