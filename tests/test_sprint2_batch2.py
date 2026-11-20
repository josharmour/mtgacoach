"""Sprint 2 Batch 2 regression tests: S2-5..S2-10 (100% customer-deck-agnostic)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from arenamcp import mulligan_policy
from arenamcp.action_planner import ActionPlanner
from arenamcp.board_assessment import assess, has_unmodelled_additional_cost
from arenamcp.board_math_picks import board_math_option_pick
from arenamcp.burn_timing import (
    combat_holds,
    hold_overrides,
    is_benign_stack_entry,
    opponent_early_window_holds,
    stack_is_ours_or_benign,
    upkeep_holds,
)
from arenamcp.coach import CoachEngine
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.line_search_moves import _unmodelled_effect_reasons
from arenamcp.play_safety import (
    beneficial_aura_only_opposing_host,
    filter_play_options,
    own_stack_reserved_option,
)
from arenamcp.stack_responses import (
    discard_response_note,
    discard_response_pass,
    discard_threat_on_stack,
)

pytestmark = pytest.mark.usefixtures("unlimited_search_deadline")

LOCAL, OPP = 1, 2


class _CaptureBackend:
    def __init__(self, reply: str = '{"option_ids": ["pass"], "reasoning": "hold"}') -> None:
        self.reply = reply
        self.calls: list[dict] = []

    def complete(self, system_prompt, user_message, max_tokens=1024, **kwargs):
        self.calls.append(
            {
                "system": system_prompt,
                "user": user_message,
                "class": kwargs.get("call_class"),
                "max_tokens": max_tokens,
            }
        )
        return self.reply


class _OfflineBackend:
    def complete(self, *args, **kwargs):
        raise RuntimeError("offline")


def _player(seat: int, *, is_local: bool, life: int = 20, mulligans: int = 0) -> dict:
    return {
        "seat_id": seat,
        "is_local": is_local,
        "life_total": life,
        "mulligan_count": mulligans,
    }


def _land(
    iid: int,
    name: str = "Mountain",
    *,
    seat: int = LOCAL,
    tapped: bool = False,
    subtypes: list[str] | None = None,
    oracle: str = "",
) -> dict:
    type_line = f"Basic Land — {' '.join(subtypes)}" if subtypes else "Land"
    return {
        "instance_id": iid,
        "name": name,
        "type_line": type_line,
        "card_types": ["CardType_Land"],
        "subtypes": list(subtypes or ()),
        "controller_seat_id": seat,
        "owner_seat_id": seat,
        "is_tapped": tapped,
        "oracle_text": oracle,
    }


def _basic(iid: int, name: str, subtype: str, seat: int = LOCAL) -> dict:
    return _land(iid, name, seat=seat, subtypes=[subtype])


def _creature(
    iid: int,
    name: str,
    seat: int,
    power: int,
    toughness: int,
    oracle: str = "",
    *,
    cost: str = "{1}{W}",
    tapped: bool = False,
    keywords: list[str] | None = None,
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
        "turn_entered_battlefield": turn_entered,
        "keywords": list(keywords or ()),
    }


def _spell(
    iid: int,
    name: str,
    cost: str,
    oracle: str,
    *,
    type_line: str = "Instant",
    seat: int = LOCAL,
    keywords: list[str] | None = None,
    related_faces: list[dict] | None = None,
) -> dict:
    ctypes = []
    low = type_line.lower()
    for word, tag in (
        ("instant", "CardType_Instant"),
        ("sorcery", "CardType_Sorcery"),
        ("enchantment", "CardType_Enchantment"),
        ("creature", "CardType_Creature"),
        ("planeswalker", "CardType_Planeswalker"),
        ("artifact", "CardType_Artifact"),
    ):
        if word in low:
            ctypes.append(tag)
    card = {
        "instance_id": iid,
        "name": name,
        "mana_cost": cost,
        "type_line": type_line,
        "card_types": ctypes,
        "oracle_text": oracle,
        "controller_seat_id": seat,
        "owner_seat_id": seat,
        "keywords": list(keywords or ()),
    }
    if related_faces is not None:
        card["related_faces"] = related_faces
    return card


def _state(
    *,
    active: int = LOCAL,
    phase: str = "Phase_Main1",
    step: str = "",
    turn_number: int = 4,
    battlefield: list[dict] | None = None,
    hand: list[dict] | None = None,
    stack: list[dict] | None = None,
    life: int = 20,
    their_life: int = 20,
    mulligans: int = 0,
    priority: int = LOCAL,
) -> dict:
    return {
        "match_id": "s2-b2-test",
        "local_seat_id": LOCAL,
        "players": [
            _player(LOCAL, is_local=True, life=life, mulligans=mulligans),
            _player(OPP, is_local=False, life=their_life),
        ],
        "turn": {
            "turn_number": turn_number,
            "active_player": active,
            "priority_player": priority,
            "phase": phase,
            "step": step,
        },
        "battlefield": list(battlefield or ()),
        "hand": list(hand or ()),
        "stack": list(stack or ()),
        "graveyard": [],
        "exile": [],
    }


def _cast_opt(idx: int, card: dict, *, payable: bool = True) -> DecisionOption:
    return DecisionOption(
        f"idx:{idx}",
        f"Cast {card['name']}",
        payable=payable,
        meta={"actionType": "ActionType_Cast", "instanceId": card["instance_id"]},
    )


def _pass_opt() -> DecisionOption:
    return DecisionOption("pass", "Pass", payable=True, meta={"actionType": "ActionType_Pass"})


def _menu(*options: DecisionOption) -> PendingDecision:
    return PendingDecision(
        request_id=(1, 1),
        request_type="ActionsAvailable",
        options=tuple(options),
        can_pass=True,
    )


def _mull_menu() -> PendingDecision:
    return PendingDecision(
        request_id=(1, 0),
        request_type="Mulligan",
        options=(
            DecisionOption("mull:keep", "Keep", payable=True, meta={"mulliganCount": 0}),
            DecisionOption("mull:mull", "Mulligan", payable=True, meta={"mulliganCount": 0}),
        ),
        can_pass=False,
    )


# ---------------------------------------------------------------------------
# S2-5: Mulligan Policy — Split/Room & Adventure costs, Basic fetches, Fallback
# ---------------------------------------------------------------------------


def test_s2_5_split_room_and_adventure_costs_vs_dfc_control():
    room = _spell(
        10,
        "Left Wing // Right Wing",
        "{1}{R}{3}{U}{U}",
        "Unlock either half.",
        type_line="Enchantment — Room",
        related_faces=[
            {"name": "Left Wing", "mana_cost": "{1}{R}", "type_line": "Enchantment — Room"},
            {"name": "Right Wing", "mana_cost": "{3}{U}{U}", "type_line": "Enchantment — Room"},
        ],
    )
    hc = mulligan_policy.hand_card(room)
    assert hc.mana_value == 2
    assert len(hc.alt_costs) == 2

    mountains = [mulligan_policy.hand_card(_basic(i, "Mountain", "Mountain")) for i in (1, 2)]
    forests = [mulligan_policy.hand_card(_basic(i, "Forest", "Forest")) for i in (3, 4)]
    assert mulligan_policy.castable_with(hc, mountains) is True
    assert mulligan_policy.castable_with(hc, forests) is False

    # "//" cost string without related_faces
    split_slash = _spell(
        11,
        "Front Slash // Back Slash",
        "{1}{W} // {4}{B}",
        "Mode text",
        type_line="Instant // Sorcery",
    )
    hc_slash = mulligan_policy.hand_card(split_slash)
    assert hc_slash.mana_value == 2
    plains = [
        mulligan_policy.hand_card(_basic(5, "Plains", "Plains")),
        mulligan_policy.hand_card(_basic(6, "Mountain", "Mountain")),
    ]
    assert mulligan_policy.castable_with(hc_slash, plains) is True

    # Adventure front + cheap adventure face (even when named "Front // Back")
    adv = _creature(12, "Test Giant // Test Quest", LOCAL, 4, 4, "Trample", cost="{3}{G}")
    adv["related_faces"] = [{"name": "Test Quest", "mana_cost": "{1}{G}", "type_line": "Instant — Adventure"}]
    hc_adv = mulligan_policy.hand_card(adv)
    assert hc_adv.mana_value == 4
    assert mulligan_policy.castable_with(hc_adv, forests) is True

    # Spell // Spell MDFC with front cost + only back face in related_faces preserves front cost!
    mdfc = _spell(
        17,
        "Front Bolt // Back Ritual",
        "{1}{B}",
        "Front effect.",
        type_line="Sorcery",
        related_faces=[{"name": "Back Ritual", "mana_cost": "{5}{B}{R}", "type_line": "Sorcery"}],
    )
    hc_mdfc = mulligan_policy.hand_card(mdfc)
    assert hc_mdfc.mana_value == 2
    swamps = [
        mulligan_policy.hand_card(_basic(7, "Swamp", "Swamp")),
        mulligan_policy.hand_card(_basic(8, "Swamp", "Swamp")),
    ]
    assert mulligan_policy.castable_with(hc_mdfc, swamps) is True

    # Opposite control: transforming DFC with empty back-face mana_cost must NOT become 0-mana castable
    dfc = _creature(13, "Test Transformer", LOCAL, 5, 5, "Flying", cost="{4}{W}{W}")
    dfc["related_faces"] = [{"name": "Transformed Side", "mana_cost": "", "type_line": "Creature — Elder"}]
    hc_dfc = mulligan_policy.hand_card(dfc)
    assert hc_dfc.mana_value == 6
    assert mulligan_policy.castable_with(hc_dfc, plains) is False

    # Room keeps a 3-land opening hand deterministically via mulligan_verdict
    hand = [
        _basic(1, "Mountain", "Mountain"),
        _basic(2, "Mountain", "Mountain"),
        _basic(3, "Forest", "Forest"),
        room,
        _creature(14, "Red Two-Drop", LOCAL, 2, 1, "", cost="{1}{R}"),
        _creature(15, "Red Three-Drop", LOCAL, 3, 2, "", cost="{2}{R}"),
        _creature(16, "Green Top-End", LOCAL, 5, 5, "", cost="{3}{G}{G}"),
    ]
    st = _state(active=LOCAL, turn_number=0, phase="Phase_Beginning", hand=hand, mulligans=0)
    verdict = mulligan_policy.mulligan_verdict(mulligan_policy.situation(st))
    assert verdict is not None and verdict[0] == "keep"


def test_s2_5_basic_fetch_land_scoped_to_mulligan_only():
    basic_fetch_text = (
        "{T}, Sacrifice ~: Search your library for a basic land card, "
        "put it onto the battlefield tapped, then shuffle."
    )
    fetch_land = _land(1, "Basic Wilds", oracle=basic_fetch_text)
    hc_fetch = mulligan_policy.hand_card(fetch_land)
    assert hc_fetch.fetches_basic is True
    # Battlefield _land_colors MUST stay empty so tapped fetch never masquerades as instant mana.
    assert mulligan_policy._land_colors(fetch_land) == frozenset()
    assert hc_fetch.produces == frozenset()

    blue_two = mulligan_policy.hand_card(_creature(10, "Test Drake", LOCAL, 2, 1, "Flying", cost="{1}{U}"))
    mountain = mulligan_policy.hand_card(_basic(2, "Mountain", "Mountain"))
    assert mulligan_policy.colors_reachable(blue_two, [mountain, hc_fetch]) is True
    assert mulligan_policy.castable_with(blue_two, [mountain, hc_fetch]) is True

    # Control: mana-taxed fetcher "{1}, {T}, Sacrifice ~" is NOT a free basic fetchland.
    paid_fetch = _land(
        3,
        "Taxed Vista",
        oracle="{1}, {T}, Sacrifice ~: Search your library for a basic land card, put it onto the battlefield tapped, then shuffle.",
    )
    assert mulligan_policy.hand_card(paid_fetch).fetches_basic is False


def test_s2_5_seven_card_color_screw_fallback_and_controls():
    # Color screw: 3 Plains + 4 expensive R/U spells -> verdict is None, offline planner mulligans
    plains = [_basic(i, "Plains", "Plains") for i in range(1, 4)]
    screw_spells = [
        _creature(10, "Off-Color A", LOCAL, 4, 4, "", cost="{2}{R}{R}"),
        _creature(11, "Off-Color B", LOCAL, 4, 4, "", cost="{3}{U}"),
        _creature(12, "Off-Color C", LOCAL, 5, 5, "", cost="{3}{R}{U}"),
        _spell(13, "Off-Color D", "{2}{U}{U}", "Draw two cards.", type_line="Sorcery"),
    ]
    st_screw = _state(active=LOCAL, turn_number=0, phase="Phase_Beginning", hand=plains + screw_spells)
    assert mulligan_policy.mulligan_verdict(mulligan_policy.situation(st_screw)) is None
    planner = ActionPlanner(_OfflineBackend())
    assert planner.plan_decision_options(_mull_menu(), st_screw) == ["mull:mull"]

    # Opposite control 1: replacing 1 Plains with a basic fetchland fixes colors -> fallback keeps
    basic_fetch = _land(
        4,
        "Basic Wilds",
        oracle="{T}, Sacrifice ~: Search your library for a basic land card, put it onto the battlefield tapped, then shuffle.",
    )
    st_fetch = _state(
        active=LOCAL,
        turn_number=0,
        phase="Phase_Beginning",
        hand=plains[:2] + [basic_fetch] + screw_spells,
    )
    assert planner.plan_decision_options(_mull_menu(), st_fetch) == ["mull:keep"]

    # Opposite control 2: including an affordable Basic landcycler keeps
    cycler = _creature(
        25,
        "Coast Crawler",
        LOCAL,
        5,
        5,
        "Basic landcycling {2}",
        cost="{4}{W}",
    )
    st_cyc = _state(
        active=LOCAL,
        turn_number=0,
        phase="Phase_Beginning",
        hand=plains + [cycler] + screw_spells[:3],
    )
    assert planner.plan_decision_options(_mull_menu(), st_cyc) == ["mull:keep"]

    # Opposite control 3: 3 lands + early interaction + 6-drop bomb keeps deterministically
    reactive = _spell(20, "Small Spark", "{W}", "Small Spark deals 2 damage to target creature.")
    top_end = _creature(21, "Heavy Titan", LOCAL, 6, 6, "", cost="{5}{W}")
    five_drop = _creature(22, "Mid Wing", LOCAL, 4, 4, "", cost="{4}{W}")
    four_drop = _creature(23, "Mid Guard", LOCAL, 3, 4, "", cost="{3}{W}")
    st_react = _state(
        active=LOCAL,
        turn_number=0,
        phase="Phase_Beginning",
        hand=plains + [reactive, four_drop, five_drop, top_end],
    )
    v_react = mulligan_policy.mulligan_verdict(mulligan_policy.situation(st_react))
    assert v_react is not None and v_react[0] == "keep"


# ---------------------------------------------------------------------------
# S2-6: Unmodelled mandatory additional cast costs & board-math backup pick
# ---------------------------------------------------------------------------


def test_s2_6_mandatory_additional_cast_cost_not_free_kill():
    sac_kill = _spell(
        10,
        "Costly Sever",
        "{B}",
        "As an additional cost to cast this spell, sacrifice a creature.\nDestroy target creature.",
        type_line="Sorcery",
    )
    assert has_unmodelled_additional_cost(sac_kill) is True
    assert _unmodelled_effect_reasons(sac_kill) == "its additional cost"

    # Opposite controls: optional, mana-alternative, and life costs are NOT unmodelled extra actions.
    optional_sac = _spell(
        11,
        "Optional Bite",
        "{B}",
        "As an additional cost to cast this spell, you may sacrifice a creature.",
        type_line="Sorcery",
    )
    mana_alt = _spell(
        12,
        "Flexible Flare",
        "{1}{R}",
        "As an additional cost to cast this spell, sacrifice an artifact or pay {3}.\n~ deals 5 damage to target creature.",
    )
    life_extra = _spell(
        13,
        "Blood Pact",
        "{B}",
        "As an additional cost to cast this spell, pay 2 life.",
        type_line="Sorcery",
    )
    assert has_unmodelled_additional_cost(optional_sac) is False
    assert has_unmodelled_additional_cost(mana_alt) is False
    assert has_unmodelled_additional_cost(life_extra) is False

    # Line search & assessment must NOT schedule sac_kill as a free kill on T
    st = _state(
        battlefield=[
            _basic(1, "Swamp", "Swamp"),
            _basic(2, "Swamp", "Swamp"),
            _creature(3, "Our Pawn", LOCAL, 2, 2, "", cost="{1}{B}"),
            _creature(4, "Opp Threat", OPP, 4, 4, "", cost="{3}{B}"),
        ],
        hand=[sac_kill],
    )
    result = assess(st)
    assert "Costly Sever" not in result.lookahead[0].casts


def test_s2_6_board_math_option_pick_backup_vs_hold_and_defer_controls():
    threat = _creature(10, "Playable Knight", LOCAL, 3, 3, "", cost="{2}{W}")
    sac_kill = _spell(
        11,
        "Costly Sever",
        "{W}",
        "As an additional cost to cast this spell, sacrifice a creature.\nDestroy target creature.",
        type_line="Sorcery",
    )
    st = _state(
        battlefield=[
            _basic(1, "Plains", "Plains"),
            _basic(2, "Plains", "Plains"),
            _basic(3, "Plains", "Plains"),
        ],
        hand=[threat, sac_kill],
    )
    menu = _menu(_cast_opt(0, threat), _cast_opt(1, sac_kill), _pass_opt())

    # Positive: line wanted to deploy ("Missing Plan Card"), which isn't in menu -> deploys Playable Knight
    step_want = type(
        "Step", (), {"land": "", "casts": ("Missing Plan Card",), "attack": None, "post_combat": ()}
    )()
    fake_assess = type("Assess", (), {"our_turn": True, "lookahead": [step_want], "line_search": None})()
    with (
        patch("arenamcp.board_assessment.assess", return_value=fake_assess),
        patch("arenamcp.board_assessment.option_role", return_value="creature"),
    ):
        picked, reason = board_math_option_pick(menu, st)
    assert picked == ["idx:0"]
    assert "deploying biggest payable threat instead" in reason

    # Control 1: line intentionally HELD everything (scheduled=empty, greedy=empty) -> must pass
    step_hold = type("Step", (), {"land": "", "casts": (), "attack": None, "post_combat": ()})()
    fake_hold = type("Assess", (), {"our_turn": True, "lookahead": [step_hold], "line_search": None})()
    with (
        patch("arenamcp.board_assessment.assess", return_value=fake_hold),
        patch("arenamcp.board_assessment.option_role", return_value="creature"),
    ):
        picked_hold, _ = board_math_option_pick(menu, st)
    assert picked_hold == ["pass"]

    # Control 2: line deferred threat until post-combat in Main1 -> must pass
    step_defer = type(
        "Step",
        (),
        {"land": "", "casts": ("Missing Plan Card",), "attack": "all", "post_combat": ("Playable Knight",)},
    )()
    fake_defer = type("Assess", (), {"our_turn": True, "lookahead": [step_defer], "line_search": None})()
    with (
        patch("arenamcp.board_assessment.assess", return_value=fake_defer),
        patch("arenamcp.board_assessment.option_role", return_value="creature"),
    ):
        picked_defer, _ = board_math_option_pick(menu, st)
    assert picked_defer == ["pass"]

    # Control 3: only sac_kill (unmodelled additional cost) is offered -> never backup-picked -> pass
    sac_menu = _menu(_cast_opt(1, sac_kill), _pass_opt())
    with (
        patch("arenamcp.board_assessment.assess", return_value=fake_assess),
        patch("arenamcp.board_assessment.option_role", return_value="creature"),
    ):
        picked_sac, _ = board_math_option_pick(sac_menu, st)
    assert picked_sac == ["pass"]


# ---------------------------------------------------------------------------
# S2-7: Own-Stack Patience — Any Own Spell + Instant-Only Lethal Abort
# ---------------------------------------------------------------------------


def test_s2_7_own_noncreature_spell_holds_flash_aura_and_combat_lethal_still_passes():
    own_ench = _spell(
        50,
        "Test Aura-Catcher",
        "{W}",
        "Enchant permanent\nWhen ~ enters, exile target nonland permanent.",
        type_line="Enchantment — Aura",
    )
    flash_aura = _spell(
        10,
        "Swift Shield",
        "{1}{W}",
        "Flash\nEnchant creature\nEnchanted creature gets +1/+1 and gains hexproof until end of turn.",
        type_line="Enchantment — Aura",
        keywords=["flash"],
    )
    counter = _spell(11, "Deny", "{1}{U}", "Counter target spell.")
    zap = _spell(12, "Zap", "{R}", "Zap deals 2 damage to any target.")

    bf = [
        _basic(1, "Plains", "Plains"),
        _basic(2, "Plains", "Plains"),
        _basic(3, "Mountain", "Mountain"),
        _basic(4, "Island", "Island"),
        _creature(20, "Vanguard", LOCAL, 4, 4, ""),
    ]
    st = _state(battlefield=bf, hand=[flash_aura, counter, zap], stack=[own_ench], their_life=4)

    # Positive: beneficial Flash Aura held even though own spell on stack is non-creature enchantment
    assert "beneficial Aura" in own_stack_reserved_option(_cast_opt(0, flash_aura), flash_aura, st)
    # Control: counterspell NOT held
    assert own_stack_reserved_option(_cast_opt(1, counter), counter, st) == ""

    # Control: own ABILITY on stack does NOT hold Flash Aura
    own_ability = {**own_ench, "object_kind": "ABILITY"}
    st_abil = _state(battlefield=bf, hand=[flash_aura], stack=[own_ability], their_life=4)
    assert own_stack_reserved_option(_cast_opt(0, flash_aura), flash_aura, st_abil) == ""

    # Combat lethal on board (4 power vs 4 life), face burn on stack only 2 (< 4):
    # ActionPlanner._own_stack_continuation_pass must STILL hold & auto-pass!
    menu = _menu(_cast_opt(0, flash_aura), _cast_opt(2, zap), _pass_opt())
    backend = _CaptureBackend('{"option_ids": ["idx:0"], "reasoning": "would misplay"}')
    planner = ActionPlanner(backend, timeout=5.0, land_drop_first=False)
    assert planner.plan_decision_options(menu, st) == ["pass"]
    assert backend.calls == []
    assert planner.get_last_decision_trace().get("policy") == "own_stack_continuation"

    # Opposite control: face burn >= their_life (2 damage vs 2 life) -> aborts auto-pass, consults LLM!
    st_burn_lethal = _state(battlefield=bf, hand=[flash_aura, zap], stack=[own_ench], their_life=2)
    backend_lethal = _CaptureBackend('{"option_ids": ["idx:2"], "reasoning": "lethal burn"}')
    planner_lethal = ActionPlanner(backend_lethal, timeout=5.0, land_drop_first=False)
    assert planner_lethal.plan_decision_options(menu, st_burn_lethal) == ["idx:2"]
    assert len(backend_lethal.calls) == 1


# ---------------------------------------------------------------------------
# S2-8: Burn Timing — Kicker cost + Benign Opposing Stack Abilities
# ---------------------------------------------------------------------------


def test_s2_8_kicker_cost_in_hold_overrides():
    kicked_burn = _spell(
        10,
        "Kicked Spark",
        "{R}",
        "Kicker {4}\nKicked Spark deals 2 damage to any target. If this spell was kicked, it deals 4 damage instead.",
    )
    plain_burn = _spell(11, "Plain Spark", "{R}", "Plain Spark deals 2 damage to any target.")

    st4 = _state(
        battlefield=[_basic(i, "Mountain", "Mountain") for i in range(1, 5)],
        hand=[kicked_burn],
    )
    opt_k = _cast_opt(0, kicked_burn)
    # 4 spare mana <= full kicked cost 5 -> keeps holding!
    assert hold_overrides(_menu(opt_k, _pass_opt()), st4, [(opt_k, kicked_burn)]) == ""

    # 6 spare mana > full kicked cost 5 -> releases!
    st6 = _state(
        battlefield=[_basic(i, "Mountain", "Mountain") for i in range(1, 7)],
        hand=[kicked_burn],
    )
    assert "more mana" in hold_overrides(_menu(opt_k, _pass_opt()), st6, [(opt_k, kicked_burn)])

    # Control: plain 1-mana burn with 2 spare mana still releases!
    st2 = _state(
        battlefield=[_basic(1, "Mountain", "Mountain"), _basic(2, "Mountain", "Mountain")],
        hand=[plain_burn],
    )
    opt_p = _cast_opt(1, plain_burn)
    assert "more mana" in hold_overrides(_menu(opt_p, _pass_opt()), st2, [(opt_p, plain_burn)])


def test_s2_8_benign_opposing_abilities_vs_threatening_controls():
    gain_ability = {
        "instance_id": 91,
        "controller_seat_id": OPP,
        "object_kind": "ABILITY",
        "name": "Soul Trigger",
        "oracle_text": "Creatures your opponents control enter tapped.\nWhenever a creature an opponent controls enters, you gain 1 life.",
    }
    fetch_ability = {
        "instance_id": 92,
        "controller_seat_id": OPP,
        "object_kind": "ABILITY",
        "name": "Passage Ability",
        "oracle_text": "{T}, Sacrifice ~: Search your library for a basic land card, put it onto the battlefield tapped, then shuffle. Then if you control four or more lands, untap that land.",
    }
    gain_spell = {
        "instance_id": 93,
        "controller_seat_id": OPP,
        "name": "Healing Spell",
        "type_line": "Instant",
        "oracle_text": "Target player gains 4 life.",
    }
    gain_plus_draw = {
        "instance_id": 94,
        "controller_seat_id": OPP,
        "object_kind": "ABILITY",
        "oracle_text": "When ~ enters, you gain 2 life and draw a card.",
    }

    assert is_benign_stack_entry(gain_ability) is True
    assert is_benign_stack_entry(fetch_ability) is True
    assert is_benign_stack_entry(gain_spell) is False
    assert is_benign_stack_entry(gain_plus_draw) is False
    fetch_plus_counters = {
        "instance_id": 95,
        "controller_seat_id": OPP,
        "object_kind": "ABILITY",
        "oracle_text": "Search your library for a basic land card, put it onto the battlefield tapped, then put two +1/+1 counters on it.",
    }
    assert is_benign_stack_entry(fetch_plus_counters) is False
    hand_exile_trig = {
        "instance_id": 96,
        "controller_seat_id": OPP,
        "object_kind": "ABILITY",
        "oracle_text": "When ~ enters, look at target opponent's hand. You may exile a nonland card from it.",
    }

    zap = _spell(10, "Test Bolt", "{1}{R}", "Test Bolt deals 3 damage to any target.")
    bf = [_basic(1, "Mountain", "Mountain"), _basic(2, "Mountain", "Mountain")]
    menu = _menu(_cast_opt(0, zap), _pass_opt())

    # Benign opposing lifegain ability -> stack_is_ours_or_benign holds in Main1, Upkeep, Combat, Early Window
    st_main = _state(
        active=LOCAL, phase="Phase_Main1", battlefield=bf, hand=[zap], stack=[gain_ability], their_life=10
    )
    assert stack_is_ours_or_benign(st_main) is True
    planner = ActionPlanner(_CaptureBackend('{"option_ids": ["idx:0"]}'), timeout=5.0, land_drop_first=False)
    assert planner.plan_decision_options(menu, st_main) == ["pass"]

    # Control: opposing lifegain SPELL -> NOT benign -> lets planner decide / not held
    st_spell = _state(
        active=LOCAL, phase="Phase_Main1", battlefield=bf, hand=[zap], stack=[gain_spell], their_life=10
    )
    assert stack_is_ours_or_benign(st_spell) is False
    assert planner.plan_decision_options(menu, st_spell) == ["idx:0"]

    # Control: lethal burn against benign lifegain ability (3 damage vs 3 life) -> NOT held!
    st_lethal = _state(
        active=LOCAL, phase="Phase_Main1", battlefield=bf, hand=[zap], stack=[gain_ability], their_life=3
    )
    assert planner.plan_decision_options(menu, st_lethal) == ["idx:0"]

    # Benign basic fetch ability on opp's T2 BeginCombat holds instant burn!
    st_opp_bc = _state(
        active=OPP,
        phase="Phase_Combat",
        step="Step_BeginCombat",
        battlefield=bf,
        hand=[zap],
        stack=[fetch_ability],
        their_life=15,
    )
    assert len(opponent_early_window_holds(menu, st_opp_bc)) == 1
    st_opp_exile = _state(
        active=OPP,
        phase="Phase_Main1",
        battlefield=bf,
        hand=[zap],
        stack=[hand_exile_trig],
        their_life=15,
    )
    assert opponent_early_window_holds(menu, st_opp_exile) == []
    assert (
        len(
            upkeep_holds(
                menu,
                _state(
                    active=LOCAL,
                    phase="Phase_Beginning",
                    step="Step_Upkeep",
                    battlefield=bf,
                    hand=[zap],
                    stack=[gain_ability],
                ),
            )
        )
        == 1
    )
    assert (
        len(
            combat_holds(
                menu,
                _state(
                    active=LOCAL,
                    phase="Phase_Combat",
                    step="Step_DeclareBlock",
                    battlefield=bf,
                    hand=[zap],
                    stack=[gain_ability],
                ),
            )
        )
        == 1
    )


# ---------------------------------------------------------------------------
# S2-9: Must-Answer Opposing Discard on Stack
# ---------------------------------------------------------------------------


def test_s2_9_must_answer_opposing_discard_and_controls():
    reveal_discard = {
        "instance_id": 90,
        "controller_seat_id": OPP,
        "name": "Mind Probe",
        "type_line": "Sorcery",
        "targeting": [LOCAL],
        "oracle_text": "Target opponent reveals their hand. You choose a nonland card from it. That player discards that card.",
    }
    strike = _spell(10, "Face Strike", "{1}{R}", "Face Strike deals 3 damage to any target.")
    mountain = _basic(5, "Mountain", "Mountain")
    bf = [_basic(1, "Mountain", "Mountain"), _basic(2, "Mountain", "Mountain")]

    st = _state(
        active=OPP,
        phase="Phase_Main1",
        battlefield=bf,
        hand=[strike, mountain],
        stack=[reveal_discard],
    )
    threat = discard_threat_on_stack(st)
    assert threat is not None and threat.opponent_chooses is True and threat.instants_eligible is True
    note = discard_response_note(st)
    assert "RESPOND NOW" in note and "Face Strike" in note

    # Entry point: CoachEngine._format_stack_section includes discard_response_note
    coach = CoachEngine.__new__(CoachEngine)
    coach._clean_oracle_for_prompt = lambda s: s
    coach._rules_in_deck_reference = lambda obj, gs: False
    formatted = "\n".join(coach._format_stack_section(st, LOCAL))
    assert "RESPOND NOW" in formatted and "Face Strike" in formatted

    # Entry point: ActionPlanner overrides LLM Pass -> casts Face Strike before discard resolves
    menu = _menu(_cast_opt(0, strike), _pass_opt())
    backend = _CaptureBackend('{"option_ids": ["pass"], "reasoning": "hold"}')
    planner = ActionPlanner(backend, timeout=5.0, land_drop_first=False)
    assert planner.plan_decision_options(menu, st) == ["idx:0"]
    assert planner.get_last_decision_trace().get("discard_response_guard", {}).get("with") == "idx:0"

    # Control 1: creature-only discard does NOT threaten instant spells
    creature_only = {
        **reveal_discard,
        "oracle_text": "Target opponent reveals their hand. You choose a creature card from it. That player discards that card.",
    }
    st_cre = _state(
        active=OPP, phase="Phase_Main1", battlefield=bf, hand=[strike, mountain], stack=[creature_only]
    )
    t_cre = discard_threat_on_stack(st_cre)
    assert t_cre is not None and t_cre.instants_eligible is False
    assert discard_response_note(st_cre) == ""
    assert discard_response_pass(menu, st_cre, ["pass"]) is None

    # Control 2: opponent's own loot ("Draw a card, then discard a card.") is NOT a threat to us
    opp_loot = {
        "instance_id": 91,
        "controller_seat_id": OPP,
        "name": "Self Loot",
        "type_line": "Instant",
        "oracle_text": "Draw a card, then discard a card.",
    }
    st_loot = _state(active=OPP, phase="Phase_Main1", battlefield=bf, hand=[strike], stack=[opp_loot])
    assert discard_threat_on_stack(st_loot) is None

    # Control 3: blind discard when we have a spare land in hand -> do NOT force-override pass
    blind_discard = {
        "instance_id": 92,
        "controller_seat_id": OPP,
        "name": "Blind Rot",
        "type_line": "Sorcery",
        "targeting": [LOCAL],
        "oracle_text": "Target opponent discards a card.",
    }
    st_spare = _state(
        active=OPP, phase="Phase_Main1", battlefield=bf, hand=[strike, mountain], stack=[blind_discard]
    )
    assert discard_response_pass(menu, st_spare, ["pass"]) is None


# ---------------------------------------------------------------------------
# S2-10: Never cast beneficial "either"-scope Aura onto opponent's creature
# ---------------------------------------------------------------------------


def test_s2_10_beneficial_aura_withheld_when_only_opposing_creatures_exist():
    cantrip_aura = _spell(
        10,
        "Sky Cantrip Aura",
        "{1}{W}",
        "Flash\nEnchant creature\nWhen this Aura enters, draw a card.\nEnchanted creature gets +1/+0 and has flying.",
        type_line="Enchantment — Aura",
        keywords=["flash"],
    )
    plain_buff_aura = _spell(
        11,
        "Pure Buff Aura",
        "{W}",
        "Enchant creature\nEnchanted creature gets +1/+1 for each enchantment you control and has first strike.",
        type_line="Enchantment — Aura",
    )
    pacify_aura = _spell(
        12,
        "Cage Aura",
        "{1}{W}",
        "Enchant creature\nEnchanted creature can't attack or block.",
        type_line="Enchantment — Aura",
    )

    opp_flyer = _creature(20, "Opp Flyer", OPP, 2, 1, "Flying", keywords=["flying"])
    opp_wolf = _creature(21, "Opp Wolf", OPP, 2, 2, "")
    plains = [_basic(1, "Plains", "Plains"), _basic(2, "Plains", "Plains")]

    # Mirror bug_20261119_174048.json: 24 life vs 4 opp power, 0 friendly creatures -> BOTH beneficial Auras withheld!
    st_safe = _state(
        battlefield=plains + [opp_flyer, opp_wolf],
        hand=[cantrip_aura, plain_buff_aura, pacify_aura],
        life=24,
    )
    assert "only opposing creatures" in beneficial_aura_only_opposing_host(cantrip_aura, st_safe)
    assert "only opposing creatures" in beneficial_aura_only_opposing_host(plain_buff_aura, st_safe)

    menu = _menu(_cast_opt(0, cantrip_aura), _cast_opt(1, plain_buff_aura), _pass_opt())
    filtered = filter_play_options(menu, st_safe)
    assert [o.option_id for o in filtered.options] == ["pass"]

    planner = ActionPlanner(_CaptureBackend('{"option_ids": ["idx:0"]}'), timeout=5.0, land_drop_first=False)
    assert planner.plan_decision_options(menu, st_safe) == ["pass"]

    # Opposite control 1: facing lethal on board (4 life vs 5 power) -> cantrip Aura allowed as desperation draw,
    # while zero-payoff buff Aura stays withheld!
    opp_three = _creature(22, "Opp Brute", OPP, 3, 3, "")
    st_lethal = _state(
        battlefield=plains + [opp_three, opp_wolf],
        hand=[cantrip_aura, plain_buff_aura],
        life=4,
    )
    assert beneficial_aura_only_opposing_host(cantrip_aura, st_lethal) == ""
    assert "only opposing creatures" in beneficial_aura_only_opposing_host(plain_buff_aura, st_lethal)

    # Opposite control 2: when we control a friendly creature -> both beneficial Auras allowed!
    our_body = _creature(30, "Our Squire", LOCAL, 1, 1, "")
    st_host = _state(
        battlefield=plains + [our_body, opp_flyer],
        hand=[cantrip_aura, plain_buff_aura],
        life=24,
    )
    assert beneficial_aura_only_opposing_host(cantrip_aura, st_host) == ""
    assert beneficial_aura_only_opposing_host(plain_buff_aura, st_host) == ""

    # Opposite control 3: harmful Aura ("can't attack or block") when only opponent has creatures -> allowed!
    assert beneficial_aura_only_opposing_host(pacify_aura, st_safe) == ""
