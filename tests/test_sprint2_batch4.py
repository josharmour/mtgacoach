"""Sprint 2 Batch 4 regression tests: S2-15..S2-19 (100% customer-deck-agnostic)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.board_assessment import _schedule, _Spell, _spell_value, card_role
from arenamcp.board_math_picks import board_math_option_pick, line_fallback_option_pick
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.mulligan_policy import (
    MulliganSituation,
    hand_card,
    mulligan_fallback_choice,
    mulligan_verdict,
    situation,
)
from arenamcp.play_safety import filter_play_options, own_creature_target_missing, unsafe_play_reason

pytestmark = pytest.mark.usefixtures("unlimited_search_deadline")

LOCAL, OPP = 1, 2


class _OfflineBackend:
    def complete(self, *args, **kwargs):
        raise RuntimeError("offline")


def _player(seat: int, *, is_local: bool, life: int = 20, lands_played: int = 1, mull: int = 0) -> dict:
    return {
        "seat_id": seat,
        "is_local": is_local,
        "life_total": life,
        "lands_played": lands_played,
        "mulligan_count": mull,
    }


def _basic(iid: int, name: str, subtype: str, *, seat: int = LOCAL, tapped: bool = False) -> dict:
    return {
        "instance_id": iid,
        "name": name,
        "type_line": f"Basic Land — {subtype}",
        "card_types": ["CardType_Land"],
        "subtypes": [subtype],
        "controller_seat_id": seat,
        "owner_seat_id": seat,
        "is_tapped": tapped,
        "oracle_text": "",
    }


def _fetch(iid: int, name: str = "Synthetic Passage") -> dict:
    return {
        "instance_id": iid,
        "name": name,
        "type_line": "Land",
        "card_types": ["CardType_Land"],
        "subtypes": [],
        "controller_seat_id": LOCAL,
        "owner_seat_id": LOCAL,
        "oracle_text": (
            "{oT}, Sacrifice this land: Search your library for a basic land card, "
            "put it onto the battlefield tapped, then shuffle."
        ),
    }


def _creature(
    iid: int,
    name: str,
    seat: int,
    power: int,
    toughness: int,
    *,
    cost: str = "{1}{G}",
    oracle: str = "",
    tapped: bool = False,
    phased_out: bool = False,
    turn_entered: int = 1,
) -> dict:
    return {
        "instance_id": iid,
        "name": name,
        "type_line": "Creature — Test",
        "card_types": ["CardType_Creature"],
        "mana_cost": cost,
        "controller_seat_id": seat,
        "owner_seat_id": seat,
        "power": power,
        "toughness": toughness,
        "oracle_text": oracle,
        "is_tapped": tapped,
        "is_phased_out": phased_out,
        "turn_entered_battlefield": turn_entered,
    }


def _spell(
    iid: int,
    name: str,
    cost: str,
    type_line: str,
    oracle: str,
    *,
    seat: int = LOCAL,
) -> dict:
    ctypes = [
        f"CardType_{part}"
        for part in ("Instant", "Sorcery", "Creature", "Artifact", "Enchantment")
        if part.lower() in type_line.lower()
    ]
    return {
        "instance_id": iid,
        "name": name,
        "type_line": type_line,
        "card_types": ctypes,
        "mana_cost": cost,
        "oracle_text": oracle,
        "controller_seat_id": seat,
        "owner_seat_id": seat,
    }


def _main1_state(
    lands: list[dict],
    creatures: list[dict],
    hand: list[dict],
    *,
    our_life: int = 20,
    opp_life: int = 20,
    turn_number: int = 7,
) -> dict:
    return {
        "local_seat_id": LOCAL,
        "turn": {
            "turn_number": turn_number,
            "active_player": LOCAL,
            "priority_player": LOCAL,
            "phase": "Phase_Main1",
            "step": "",
        },
        "players": [
            _player(LOCAL, is_local=True, life=our_life, lands_played=1),
            _player(OPP, is_local=False, life=opp_life, lands_played=0),
        ],
        "hand": hand,
        "battlefield": [*lands, *creatures],
        "stack": [],
        "graveyard": [],
        "exile": [],
    }


def _cast_menu(spells: list[dict]) -> PendingDecision:
    opts = [
        DecisionOption(
            f"cast:{card['instance_id']}",
            f"Cast {card['name']}",
            payable=True,
            meta={
                "actionType": "ActionType_Cast",
                "instanceId": card["instance_id"],
                "grpId": card["instance_id"],
            },
        )
        for card in spells
    ]
    opts.append(DecisionOption("pass", "Pass", payable=True))
    return PendingDecision((1, 1), "ActionsAvailable", tuple(opts), can_pass=True)


# --- S2-15: Mulligan carrier-dependent spells & 6-card 1-lander fallback ------------------------


def test_s2_15_carrierless_pump_trick_and_bite_not_auto_kept_on_3_lands():
    lands = [_basic(1, "Forest", "Forest"), _basic(2, "Forest", "Forest"), _basic(3, "Forest", "Forest")]
    heavy1 = _creature(10, "Heavy Beast A", LOCAL, 4, 4, cost="{4}{G}")
    heavy2 = _creature(11, "Heavy Beast B", LOCAL, 5, 5, cost="{4}{G}")
    heavy3 = _creature(12, "Heavy Beast C", LOCAL, 6, 6, cost="{5}{G}")

    pump = _spell(13, "Instant Pump", "{1}{G}", "Instant", "Target creature gets +3/+3 until end of turn.")
    bite = _spell(
        14,
        "Instant Bite",
        "{1}{G}",
        "Instant",
        "Target creature you control deals damage equal to its power to target creature you don't control.",
    )
    assert hand_card(pump).needs_carrier is True
    assert hand_card(bite).needs_carrier is True

    for trick in (pump, bite):
        state = {
            "local_seat_id": LOCAL,
            "players": [_player(LOCAL, is_local=True, mull=0), _player(OPP, is_local=False)],
            "turn": {"turn_number": 0, "active_player": OPP},
            "hand": [*lands, trick, heavy1, heavy2, heavy3],
            "battlefield": [],
        }
        assert mulligan_verdict(situation(state)) is None

    # Opposite control: adding a 2-drop creature restores the auto-keep.
    two_drop = _creature(15, "Early Bear", LOCAL, 2, 2, cost="{1}{G}")
    keep_state = {
        "local_seat_id": LOCAL,
        "players": [_player(LOCAL, is_local=True, mull=0), _player(OPP, is_local=False)],
        "turn": {"turn_number": 0, "active_player": OPP},
        "hand": [*lands, pump, two_drop, heavy2, heavy3],
        "battlefield": [],
    }
    assert mulligan_verdict(situation(keep_state))[0] == "keep"


def test_s2_15_modal_optional_and_token_spells_keep_needs_carrier_false():
    modal = _spell(
        20,
        "Modal Choice",
        "{1}{G}",
        "Instant",
        "Choose one —\n• Target creature gets +2/+2 until end of turn.\n• Draw a card.",
    )
    optional = _spell(
        21,
        "Optional Buff",
        "{1}{G}",
        "Sorcery",
        "Put a +1/+1 counter on up to one target creature.",
    )
    token_buff = _spell(
        22,
        "Token Buff",
        "{1}{W}",
        "Sorcery",
        "Target creature gets +1/+1 until end of turn.\nCreate a 1/1 white Soldier creature token.",
    )
    counter_buff = _spell(
        23,
        "Counter Buff",
        "{1}{G}",
        "Sorcery",
        "Put a +1/+1 counter on target creature.",
    )
    trailing_may = _spell(
        24,
        "Buff With Bonus",
        "{1}{G}",
        "Instant",
        "Target creature you control gets +2/+2 until end of turn. You may draw a card.",
    )
    opt_opp_bite = _spell(
        25,
        "UpTo Opp Bite",
        "{1}{G}",
        "Instant",
        "Target creature you control deals damage equal to its power to up to one target creature you don't control.",
    )
    split_mix = {
        **_spell(26, "Boost // Sight", "{G} // {1}{U}", "Instant", "Target creature you control gets +2/+2."),
        "related_faces": [
            {
                "name": "Boost",
                "type_line": "Instant",
                "mana_cost": "{G}",
                "oracle_text": "Target creature you control gets +2/+2 until end of turn.",
            },
            {
                "name": "Sight",
                "type_line": "Instant",
                "mana_cost": "{1}{U}",
                "oracle_text": "Draw a card.",
            },
        ],
    }
    assert hand_card(modal).needs_carrier is False
    assert hand_card(optional).needs_carrier is False
    assert hand_card(token_buff).needs_carrier is False
    assert hand_card(counter_buff).needs_carrier is True
    assert hand_card(trailing_may).needs_carrier is True
    assert hand_card(opt_opp_bite).needs_carrier is True
    assert hand_card(split_mix).needs_carrier is False


def test_s2_15_extreme_lands_with_multiple_pump_tricks_still_auto_mulligan():
    forest = _basic(1, "Forest", "Forest")
    trick1 = _spell(10, "Pump A", "{G}", "Instant", "Target creature gets +2/+2 until end of turn.")
    trick2 = _spell(11, "Pump B", "{G}", "Instant", "Put a +1/+1 counter on target creature.")
    drop1 = _creature(12, "Bear A", LOCAL, 2, 2, cost="{1}{G}")
    drop2 = _creature(13, "Bear B", LOCAL, 2, 2, cost="{1}{G}")
    drop3 = _creature(14, "Hill A", LOCAL, 3, 3, cost="{2}{G}")
    drop4 = _creature(15, "Hill B", LOCAL, 4, 4, cost="{3}{G}")

    seven_zero = [hand_card(c) for c in (trick1, trick2, drop1, drop2, drop3, drop4, drop1)]
    seven_one = [hand_card(c) for c in (forest, trick1, trick2, drop1, drop2, drop3, drop4)]
    assert mulligan_verdict(MulliganSituation(cards=seven_zero, mulligans=0, on_play=True))[0] == "mulligan"
    assert mulligan_verdict(MulliganSituation(cards=seven_one, mulligans=0, on_play=True))[0] == "mulligan"
    assert mulligan_verdict(MulliganSituation(cards=seven_zero, mulligans=1, on_play=True))[0] == "mulligan"
    assert (
        mulligan_fallback_choice(MulliganSituation(cards=seven_zero, mulligans=1, on_play=True))[0]
        == "mulligan"
    )
    assert (
        mulligan_fallback_choice(MulliganSituation(cards=seven_zero, mulligans=0, on_play=True))[0]
        == "mulligan"
    )


def test_s2_15_six_card_one_lander_fallback_mulligans_without_real_one_drop():
    two_drop_a = _creature(30, "Bear A", LOCAL, 2, 2, cost="{1}{G}")
    two_drop_b = _creature(31, "Bear B", LOCAL, 2, 2, cost="{1}{G}")
    three_drop = _creature(32, "Centaur", LOCAL, 3, 3, cost="{2}{G}")
    four_drop = _creature(33, "Baloth", LOCAL, 4, 4, cost="{3}{G}")
    five_drop = _creature(34, "Wurm", LOCAL, 5, 5, cost="{4}{G}")
    one_drop_pump = _spell(35, "Tiny Buff", "{G}", "Instant", "Target creature gets +2/+2 until end of turn.")
    adv_trick_beast = {
        **_creature(38, "Beast // Trick", LOCAL, 4, 4, cost="{3}{G}"),
        "related_faces": [
            {
                "name": "Trick",
                "type_line": "Instant — Adventure",
                "mana_cost": "{G}",
                "oracle_text": "Target creature you control gets +2/+2 until end of turn.",
            }
        ],
    }

    mull_cards = [
        hand_card(_basic(1, "Forest", "Forest")),
        *(hand_card(c) for c in (one_drop_pump, two_drop_a, two_drop_b, three_drop, four_drop, five_drop)),
    ]
    mull_adv = [
        hand_card(_basic(1, "Forest", "Forest")),
        *(hand_card(c) for c in (adv_trick_beast, two_drop_a, two_drop_b, three_drop, four_drop, five_drop)),
    ]
    assert (
        mulligan_fallback_choice(MulliganSituation(cards=mull_cards, mulligans=1, on_play=True))[0]
        == "mulligan"
    )
    assert (
        mulligan_fallback_choice(MulliganSituation(cards=mull_adv, mulligans=1, on_play=True))[0]
        == "mulligan"
    )

    # Opposite control 1: real castable 1-drop creature keeps.
    one_drop_cre = _creature(36, "Elf", LOCAL, 1, 1, cost="{G}")
    keep_cre = [
        hand_card(_basic(1, "Forest", "Forest")),
        *(hand_card(c) for c in (one_drop_cre, two_drop_a, two_drop_b, three_drop, four_drop, five_drop)),
    ]
    assert mulligan_fallback_choice(MulliganSituation(cards=keep_cre, mulligans=1, on_play=True))[0] == "keep"

    # Opposite control 2: basic fetchland keeps.
    keep_fetch = [
        hand_card(_fetch(1)),
        *(hand_card(c) for c in (one_drop_pump, two_drop_a, two_drop_b, three_drop, four_drop, five_drop)),
    ]
    assert (
        mulligan_fallback_choice(MulliganSituation(cards=keep_fetch, mulligans=1, on_play=True))[0] == "keep"
    )

    # Opposite control 3: 1-mana landcycler keeps.
    landcycler = _creature(37, "Giant Cycler", LOCAL, 6, 5, cost="{5}{G}", oracle="Forestcycling {1}")
    keep_cycle = [
        hand_card(_basic(1, "Forest", "Forest")),
        *(hand_card(c) for c in (landcycler, two_drop_a, two_drop_b, three_drop, four_drop, five_drop)),
    ]
    assert (
        mulligan_fallback_choice(MulliganSituation(cards=keep_cycle, mulligans=1, on_play=True))[0] == "keep"
    )


# --- S2-16: Sorcery permanent before higher-CMC instant removal -----------------------------


def test_s2_16_sequences_sorcery_creature_before_higher_cmc_instant_removal():
    lands = [*(_basic(i, "Swamp", "Swamp") for i in range(1, 6))]
    creature_2 = _creature(101, "Cheap Rogue", LOCAL, 2, 2, cost="{1}{B}")
    instant_rem_3 = _spell(102, "Pricey Instant Removal", "{1}{B}{B}", "Instant", "Destroy target creature.")
    opp_blocker = _creature(201, "Opp Vanilla", OPP, 2, 2, cost="{1}{B}")

    state = _main1_state(
        lands, [opp_blocker], [instant_rem_3, creature_2], our_life=20, opp_life=20, turn_number=9
    )
    menu = _cast_menu([instant_rem_3, creature_2])

    picked, _why = board_math_option_pick(menu, state)
    assert picked == ["cast:101"]
    assert ActionPlanner(_OfflineBackend()).plan_decision_options(menu, state) == ["cast:101"]


def test_s2_16_two_sorceries_still_pick_higher_cmc_first():
    lands = [*(_basic(i, "Swamp", "Swamp") for i in range(1, 7))]
    creature_2 = _creature(101, "Cheap Bear", LOCAL, 2, 2, cost="{1}{B}")
    sorcery_4 = _spell(102, "Sorcery Plague", "{3}{B}", "Sorcery", "Destroy target creature.")
    opp_cre = _creature(201, "Opp Threat", OPP, 4, 4, cost="{3}{B}")

    state = _main1_state(lands, [opp_cre], [creature_2, sorcery_4], our_life=20, opp_life=20, turn_number=11)
    menu = _cast_menu([creature_2, sorcery_4])

    picked, _why = board_math_option_pick(menu, state)
    assert picked == ["cast:102"]


def test_s2_16_urgent_survival_still_allows_line_fallback_instant_removal():
    lands = [*(_basic(i, "Swamp", "Swamp") for i in range(1, 6))]
    creature_2 = _creature(101, "Slow Bear", LOCAL, 2, 2, cost="{1}{B}")
    instant_rem_3 = _spell(102, "Emergency Slice", "{1}{B}{B}", "Instant", "Destroy target creature.")
    opp_lethal = _creature(201, "Opp Titan", OPP, 6, 6, cost="{4}{B}{B}")

    state = _main1_state(
        lands, [opp_lethal], [creature_2, instant_rem_3], our_life=6, opp_life=20, turn_number=9
    )
    menu = _cast_menu([creature_2, instant_rem_3])

    board_pick, _ = board_math_option_pick(menu, state)
    assert board_pick == ["cast:101"]
    # Survival mode is urgent, so line_fallback_option_pick still prioritizes aimed removal.
    override = line_fallback_option_pick(menu, state, board_pick)
    assert override is not None and override[0] == ["cast:102"]


# --- S2-17: Stop greedy baseline from scheduling reactive pump & Main-1 instant cantrips ------


def test_s2_17_spell_value_zeroes_pump_and_main1_instant_draw_selection():
    pump = _spell(1, "Trick", "{G}", "Instant", "Target creature gets +2/+2 until end of turn.")
    inst_draw = _spell(2, "Instant Cantrip", "{1}{U}", "Instant", "Draw a card.")
    inst_scry = _spell(3, "Instant Scry", "{U}", "Instant", "Scry 2, then draw a card.")
    sorc_draw = _spell(4, "Sorcery Draw", "{2}{U}", "Sorcery", "Draw two cards.")
    etb_draw_cre = _creature(
        5, "Draw Owl", LOCAL, 1, 1, cost="{1}{U}", oracle="Flying\nWhen this creature enters, draw a card."
    )

    aura_pump = _spell(
        6, "Aura Buff", "{W}", "Enchantment — Aura", "Enchant creature\nEnchanted creature gets +1/+1."
    )
    sorc_pump = _spell(7, "Sorcery Buff", "{G}", "Sorcery", "Target creature gets +3/+3 until end of turn.")

    def sp(c):
        return _Spell(c, c["name"], card_role(c), 2, (), False)

    assert _spell_value(sp(pump), survival=False, theirs=[]) == 0.0
    assert _spell_value(sp(pump), survival=True, theirs=[]) == 0.0
    assert _spell_value(sp(inst_draw), survival=False, theirs=[], allow_instant_utility=False) == 0.0
    assert _spell_value(sp(inst_scry), survival=False, theirs=[], allow_instant_utility=False) == 0.0

    # Opposite controls: permanent/sorcery pumps, sorcery draw, creature draw, end-step utility, and survival mode remain > 0.
    assert _spell_value(sp(aura_pump), survival=False, theirs=[], allow_instant_utility=False) > 0.0
    assert _spell_value(sp(sorc_pump), survival=False, theirs=[], allow_instant_utility=False) > 0.0
    assert _spell_value(sp(sorc_draw), survival=False, theirs=[], allow_instant_utility=False) > 0.0
    assert _spell_value(sp(etb_draw_cre), survival=False, theirs=[], allow_instant_utility=False) > 0.0
    assert _spell_value(sp(inst_draw), survival=False, theirs=[], allow_instant_utility=True) > 0.0
    assert _spell_value(sp(inst_draw), survival=True, theirs=[], allow_instant_utility=False) > 0.0

    # _schedule holds instant draw on Main 1 when not survival, casts it on instant_only_first=True.
    lands = [_basic(1, "Island", "Island"), _basic(2, "Island", "Island")]
    budgets = [{"mana": 2, "sources": [hand_card(l) for l in lands], "land": None}]
    sched_main = _schedule(
        [sp(inst_draw)], [dict(budgets[0])], survival=False, theirs=[], instant_only_first=False
    )
    sched_eop = _schedule(
        [sp(inst_draw)], [dict(budgets[0])], survival=False, theirs=[], instant_only_first=True
    )
    assert sched_main == [[]]
    assert [s.name for s in sched_eop[0]] == ["Instant Cantrip"]


def test_s2_17_planner_passes_main1_instant_cantrip_when_only_nonland_play():
    lands = [_basic(1, "Island", "Island"), _basic(2, "Island", "Island"), _basic(3, "Island", "Island")]
    cantrip = _spell(101, "Optic Plunder", "{1}{U}", "Instant", "Draw a card.")
    heavy_cre = _creature(102, "Six Drop", LOCAL, 6, 5, cost="{5}{U}")

    state = _main1_state(lands, [], [cantrip, heavy_cre], turn_number=5)
    menu = _cast_menu([cantrip])
    assert ActionPlanner(_OfflineBackend()).plan_decision_options(menu, state) == ["pass"]


# --- S2-18: Respect line-search hold over greedy instant stand-in ------------------------------


def test_s2_18_greedy_standin_does_not_fire_instant_over_searched_hold(monkeypatch):
    from arenamcp import board_assessment

    lands = [*(_basic(i, "Mountain", "Mountain") for i in range(1, 5))]
    inst_burn = _spell(101, "Instant Burn", "{R}", "Instant", "Deals 2 damage to any target.")
    sorc_sweep = _spell(102, "Sorcery Sweep", "{1}{R}", "Sorcery", "Deals 2 damage to each creature.")
    held_cre = _creature(103, "Held Bear", LOCAL, 2, 2, cost="{1}{R}")
    opp_cre = _creature(201, "Opp 2/2", OPP, 2, 2, cost="{1}{G}")

    state = _main1_state(lands, [opp_cre], [inst_burn, sorc_sweep, held_cre], turn_number=7)

    # Best line holds (casts=()); baseline greedily schedules the instant burn -> should PASS
    # and must NOT fall through to backup_picks and cast an unscheduled creature.
    best_holds = SimpleNamespace(
        casts=(),
        land="",
        castable=("Instant Burn", "Sorcery Sweep", "Held Bear"),
        post_combat=(),
        attack=(),
    )
    greedy_inst = SimpleNamespace(steps=[SimpleNamespace(casts=("Instant Burn",))])
    fake_assess_inst = SimpleNamespace(
        our_turn=True,
        lookahead=[best_holds],
        unmodelled=[],
        line_search=SimpleNamespace(baseline=greedy_inst, best=None),
    )
    monkeypatch.setattr(board_assessment, "assess", lambda _s: fake_assess_inst)
    menu_inst = _cast_menu([inst_burn, held_cre])
    assert board_math_option_pick(menu_inst, state)[0] == ["pass"]

    # Opposite control: baseline greedily schedules a sorcery-speed sweep -> stands in.
    greedy_sorc = SimpleNamespace(steps=[SimpleNamespace(casts=("Sorcery Sweep",))])
    fake_assess_sorc = SimpleNamespace(
        our_turn=True,
        lookahead=[best_holds],
        unmodelled=[],
        line_search=SimpleNamespace(baseline=greedy_sorc, best=None),
    )
    monkeypatch.setattr(board_assessment, "assess", lambda _s: fake_assess_sorc)
    menu_sorc = _cast_menu([sorc_sweep])
    assert board_math_option_pick(menu_sorc, state)[0] == ["cast:102"]

    # Mixed greedy (instant + sorcery): picks sorcery and reason omits held instant.
    greedy_mixed = SimpleNamespace(steps=[SimpleNamespace(casts=("Instant Burn", "Sorcery Sweep"))])
    fake_assess_mixed = SimpleNamespace(
        our_turn=True,
        lookahead=[best_holds],
        unmodelled=[],
        line_search=SimpleNamespace(baseline=greedy_mixed, best=None),
    )
    monkeypatch.setattr(board_assessment, "assess", lambda _s: fake_assess_mixed)
    menu_mixed = _cast_menu([inst_burn, sorc_sweep])
    mixed_pick, mixed_why = board_math_option_pick(menu_mixed, state)
    assert mixed_pick == ["cast:102"]
    assert "Sorcery Sweep" in mixed_why and "Instant Burn" not in mixed_why

    # Held greedy instant does not block unmodelled proactive permanent deployment.
    proactive_art = _spell(104, "Unmodelled Relic", "{2}", "Artifact", "Whenever you attack, draw a card.")
    state_art = _main1_state(lands, [opp_cre], [inst_burn, proactive_art], turn_number=7)
    best_holds_art = SimpleNamespace(
        casts=(),
        land="",
        castable=("Instant Burn",),
        post_combat=(),
        attack=(),
    )
    fake_assess_art = SimpleNamespace(
        our_turn=True,
        lookahead=[best_holds_art],
        unmodelled=["Unmodelled Relic"],
        line_search=SimpleNamespace(baseline=greedy_inst, best=None),
    )
    monkeypatch.setattr(board_assessment, "assess", lambda _s: fake_assess_art)
    menu_art = _cast_menu([inst_burn, proactive_art])
    assert board_math_option_pick(menu_art, state_art)[0] == ["cast:104"]

    # Instant token-maker is treated as a creature, not held as a noncreature instant.
    from arenamcp.board_math_picks import _is_instant_noncreature

    tok_inst = _spell(
        105, "Flash Soldiers", "{1}{W}", "Instant", "Create two 1/1 white Soldier creature tokens."
    )
    assert _is_instant_noncreature(tok_inst, "") is False


# --- S2-19: Own-creature target safety for mandatory fight/bite spells -------------------------


def test_s2_19_blocks_fight_and_bite_with_no_friendly_creatures_and_allows_controls():
    lands = [_basic(1, "Forest", "Forest"), _basic(2, "Forest", "Forest")]
    fight = _spell(
        101,
        "Empty Fight",
        "{G}",
        "Sorcery",
        "Target creature you control fights target creature you don't control.",
    )
    bite = _spell(
        102,
        "Empty Bite",
        "{1}{G}",
        "Instant",
        "Target creature you control deals damage equal to its power to target creature you don't control.",
    )
    opp_cre = _creature(201, "Opp Bear", OPP, 2, 2)

    state = _main1_state(lands, [opp_cre], [fight, bite])
    assert own_creature_target_missing(fight, state)
    assert own_creature_target_missing(bite, state)
    assert unsafe_play_reason(state, fight, "ActionType_Cast")
    assert unsafe_play_reason(state, bite, "ActionType_Cast")

    menu = _cast_menu([fight, bite])
    filtered = filter_play_options(menu, state)
    assert {o.option_id for o in filtered.options} == {"pass"}
    assert ActionPlanner(_OfflineBackend()).plan_decision_options(menu, state) == ["pass"]

    # Phased-out friendly creature still counts as missing.
    phased = _creature(150, "Phased Bear", LOCAL, 2, 2, phased_out=True)
    state_phased = _main1_state(lands, [opp_cre, phased], [fight, bite])
    assert own_creature_target_missing(bite, state_phased)

    # Trailing optional/bonus clauses must NOT excuse a mandatory "target creature you control".
    trailing_may_bite = _spell(
        106,
        "Bonus Bite",
        "{1}{G}",
        "Instant",
        "Target creature you control deals damage equal to its power to up to one target creature you don't control.",
    )
    assert own_creature_target_missing(trailing_may_bite, state)

    # Missing battlefield key never assumes empty board.
    assert own_creature_target_missing(bite, {"local_seat_id": LOCAL}) == ""

    # Seat 0 works both for empty-board blocking (even without players.is_local) and present-board allowing.
    state_seat0_empty = {
        "local_seat_id": 0,
        "players": [{"seat_id": 0, "life_total": 20}, {"seat_id": 1, "life_total": 20}],
        "battlefield": [
            {**_creature(202, "Seat1 Opp", 1, 2, 2), "controller_seat_id": 1, "owner_seat_id": 1}
        ],
    }
    assert own_creature_target_missing(bite, state_seat0_empty)

    # Permanent with ETB "target creature you control" is NOT blocked (enters battlefield as a permanent).
    etb_aura_perm = _spell(
        107,
        "Buff Enchantment",
        "{1}{G}",
        "Enchantment",
        "When this enchantment enters, put a +1/+1 counter on target creature you control.",
    )
    assert own_creature_target_missing(etb_aura_perm, state) == ""

    # Opposite control 1: friendly creature present -> allowed.
    our_cre = _creature(151, "Friendly Bear", LOCAL, 2, 2)
    state_ours = _main1_state(lands, [opp_cre, our_cre], [fight, bite])
    assert own_creature_target_missing(fight, state_ours) == ""
    assert own_creature_target_missing(bite, state_ours) == ""

    # Opposite control 2: seat 0 local player with seat 0 creature -> allowed.
    state_seat0 = {
        "local_seat_id": 0,
        "players": [{"seat_id": 0, "is_local": True, "life_total": 20}, {"seat_id": 1, "life_total": 20}],
        "battlefield": [
            {**_creature(152, "Seat0 Bear", 0, 2, 2), "controller_seat_id": 0, "owner_seat_id": 0},
            {**_creature(202, "Seat1 Opp", 1, 2, 2), "controller_seat_id": 1, "owner_seat_id": 1},
        ],
    }
    assert own_creature_target_missing(bite, state_seat0) == ""

    # Opposite control 3: unknown controller does not assume missing.
    unknown_cre = {
        **_creature(153, "Unknown Bear", LOCAL, 2, 2),
        "controller_seat_id": None,
        "owner_seat_id": None,
    }
    state_unknown = _main1_state(lands, [opp_cre, unknown_cre], [bite])
    assert own_creature_target_missing(bite, state_unknown) == ""

    # Opposite control 4: modal, "up to one", and token-creating spells stay legal.
    modal_bite = _spell(
        103,
        "Modal Bite",
        "{1}{G}",
        "Instant",
        "Choose one —\n• Target creature you control deals damage equal to its power to target creature you don't control.\n• Gain 4 life.",
    )
    opt_bite = _spell(
        104,
        "Optional Bite",
        "{1}{G}",
        "Sorcery",
        "Up to one target creature you control deals damage equal to its power to target creature you don't control.",
    )
    tok_fight = _spell(
        105,
        "Wolf Fight",
        "{2}{G}",
        "Sorcery",
        "Create a 2/2 green Wolf creature token. Then target creature you control fights target creature you don't control.",
    )
    for ok_spell in (modal_bite, opt_bite, tok_fight):
        assert own_creature_target_missing(ok_spell, state) == ""
