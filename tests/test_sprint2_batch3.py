"""Sprint 2 Batch 3 regression tests: S2-11..S2-14 (100% customer-deck-agnostic)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.board_assessment import assess
from arenamcp.board_math_picks import board_math_option_pick
from arenamcp.burn_timing import cast_pumped_attackers, hold_overrides, two_turn_reach
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.landfall_order import bluff_land_notes, land_before_cast_order, superfluous_bluff_land
from arenamcp.line_guard import option_note

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


def _player(seat: int, *, is_local: bool, life: int = 20, lands_played: int = 1) -> dict:
    return {
        "seat_id": seat,
        "is_local": is_local,
        "life_total": life,
        "lands_played": lands_played,
    }


def _land(
    iid: int,
    name: str,
    *,
    seat: int = LOCAL,
    tapped: bool = False,
    subtypes: list[str] | None = None,
    oracle: str = "",
    related_faces: list[dict] | None = None,
) -> dict:
    type_line = f"Basic Land — {' '.join(subtypes)}" if subtypes else "Land"
    card = {
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
    if related_faces is not None:
        card["related_faces"] = related_faces
    return card


def _basic(iid: int, name: str, subtype: str, *, seat: int = LOCAL, tapped: bool = False) -> dict:
    return _land(iid, name, seat=seat, tapped=tapped, subtypes=[subtype])


def _creature(
    iid: int,
    name: str,
    seat: int,
    power: int,
    toughness: int,
    oracle: str = "",
    *,
    cost: str = "{1}{R}",
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
    return {
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


def _state(
    *,
    active: int = LOCAL,
    phase: str = "Phase_Main1",
    step: str = "",
    turn_number: int = 8,
    battlefield: list[dict] | None = None,
    hand: list[dict] | None = None,
    stack: list[dict] | None = None,
    graveyard: list[dict] | None = None,
    exile: list[dict] | None = None,
    command: list[dict] | None = None,
    life: int = 20,
    their_life: int = 20,
    lands_played: int = 1,
) -> dict:
    return {
        "match_id": "s2-b3-test",
        "local_seat_id": LOCAL,
        "players": [
            _player(LOCAL, is_local=True, life=life, lands_played=lands_played),
            _player(OPP, is_local=False, life=their_life, lands_played=0),
        ],
        "turn": {
            "turn_number": turn_number,
            "active_player": active,
            "priority_player": active,
            "phase": phase,
            "step": step,
        },
        "battlefield": list(battlefield or ()),
        "hand": list(hand or ()),
        "stack": list(stack or ()),
        "graveyard": list(graveyard or ()),
        "exile": list(exile or ()),
        "command": list(command or ()),
    }


def _cast_opt(idx: int, card: dict, *, payable: bool = True) -> DecisionOption:
    return DecisionOption(
        f"idx:{idx}",
        f"Cast {card['name']}",
        payable=payable,
        meta={"actionType": "ActionType_Cast", "instanceId": card["instance_id"]},
    )


def _play_opt(idx: int, card: dict) -> DecisionOption:
    return DecisionOption(
        f"idx:{idx}",
        f"Play land: {card['name']}",
        payable=None,
        meta={"actionType": "ActionType_Play", "instanceId": card["instance_id"]},
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


# ---------------------------------------------------------------------------
# S2-11: Main-1 Pass [LINE best] Tagging
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phase", ["Phase_Main1", "Main1"])
def test_s2_11_main1_pass_tagged_line_best_when_best_is_noop(phase: str):
    spark = _spell(10, "Quick Spark", "{R}", "Quick Spark deals 2 damage to any target.")
    st = _state(
        phase=phase,
        turn_number=10,
        their_life=12,
        battlefield=[
            _basic(1, "Mountain", "Mountain"),
            _basic(2, "Mountain", "Mountain"),
            _basic(3, "Plains", "Plains", seat=OPP),
            _basic(4, "Island", "Island", seat=OPP),
        ],
        hand=[spark],
    )
    assessment = assess(st)
    ls = assessment.line_search
    assert ls is not None and ls.best is not None
    assert not ls.best.steps[0].land and not ls.best.steps[0].casts
    assert option_note(ls, _pass_opt(), st).startswith("[LINE best:")


def test_s2_11_main1_pass_stays_untagged_when_best_develops_or_casts():
    watcher = _creature(10, "Two-Drop Scout", LOCAL, 2, 2, "", cost="{1}{W}")
    st = _state(
        phase="Phase_Main1",
        turn_number=4,
        battlefield=[
            _basic(1, "Plains", "Plains"),
            _basic(2, "Plains", "Plains"),
            _creature(20, "Opp Watcher", OPP, 2, 2, "", cost="{1}{G}"),
        ],
        hand=[watcher],
    )
    assessment = assess(st)
    ls = assessment.line_search
    assert ls is not None and ls.best is not None and ls.best.steps[0].casts
    assert option_note(ls, _pass_opt(), st) == ""


# ---------------------------------------------------------------------------
# S2-12A: Safe Plain Untapped Land-Before-Cast Guard Into Open Mana
# ---------------------------------------------------------------------------


def test_s2_12a_land_before_cast_reorders_via_planner_entry_point():
    burn = _spell(10, "Open Burn", "{R}", "Open Burn deals 2 damage to any target.")
    mountain = _basic(11, "Mountain", "Mountain")
    st = _state(
        lands_played=0,
        battlefield=[
            _basic(1, "Mountain", "Mountain"),
            _basic(2, "Mountain", "Mountain"),
            _basic(3, "Island", "Island", seat=OPP, tapped=False),
        ],
        hand=[burn, mountain],
    )
    menu = _menu(_cast_opt(0, burn), _play_opt(1, mountain), _pass_opt())
    backend = _CaptureBackend(json.dumps({"option_ids": ["idx:0"], "reasoning": "Cast burn first."}))
    planner = ActionPlanner(backend, land_drop_first=True)

    chosen = planner.plan_decision_options(menu, st)
    assert chosen == ["idx:1"]
    trace = planner.get_last_decision_trace()
    assert trace["land_before_cast"]["replaced"] == "idx:0"
    assert trace["land_before_cast"]["with"] == "idx:1"


def test_s2_12a_land_before_cast_opposite_controls():
    burn = _spell(10, "Open Burn", "{R}", "Open Burn deals 2 damage to any target.")
    mountain = _basic(11, "Mountain", "Mountain")

    # Control 1: opponent tapped out -> keeps cast
    st_tapped = _state(
        lands_played=0,
        battlefield=[
            _basic(1, "Mountain", "Mountain"),
            _basic(2, "Island", "Island", seat=OPP, tapped=True),
        ],
        hand=[burn, mountain],
    )
    menu = _menu(_cast_opt(0, burn), _play_opt(1, mountain), _pass_opt())
    assert land_before_cast_order(menu, st_tapped, "idx:0") is None

    # Open-mana baseline state
    base_bf = [_basic(1, "Mountain", "Mountain"), _basic(2, "Island", "Island", seat=OPP, tapped=False)]

    # Control 2: chosen spell has landfall trigger
    lf = _creature(
        12,
        "Landfall Hound",
        LOCAL,
        2,
        2,
        "Whenever a land you control enters, this creature gets +1/+1 until end of turn.",
        cost="{1}{R}",
    )
    st_lf = _state(lands_played=0, battlefield=base_bf, hand=[lf, mountain])
    menu_lf = _menu(_cast_opt(0, lf), _play_opt(1, mountain), _pass_opt())
    assert land_before_cast_order(menu_lf, st_lf, "idx:0") is None

    # Control 3: another payable landfall creature is in menu
    st_lf2 = _state(lands_played=0, battlefield=base_bf, hand=[burn, lf, mountain])
    menu_lf2 = _menu(_cast_opt(0, burn), _cast_opt(1, lf), _play_opt(2, mountain), _pass_opt())
    assert land_before_cast_order(menu_lf2, st_lf2, "idx:0") is None

    # Control 4: cast draws a card (wants draw before choosing land)
    cantrip = _spell(13, "Optic Spark", "{R}", "Scry 1, then draw a card.", type_line="Sorcery")
    st_draw = _state(lands_played=0, battlefield=base_bf, hand=[cantrip, mountain])
    menu_draw = _menu(_cast_opt(0, cantrip), _play_opt(1, mountain), _pass_opt())
    assert land_before_cast_order(menu_draw, st_draw, "idx:0") is None

    # Control 5: unmodelled spell (Aura) must not be overridden
    aura = _spell(
        14,
        "Restrain Aura",
        "{1}{W}",
        "Enchant creature\nEnchanted creature can't attack or block.",
        type_line="Enchantment — Aura",
    )
    st_aura = _state(lands_played=0, battlefield=base_bf, hand=[aura, mountain])
    menu_aura = _menu(_cast_opt(0, aura), _play_opt(1, mountain), _pass_opt())
    assert land_before_cast_order(menu_aura, st_aura, "idx:0") is None

    # Control 6: two distinct lands in menu
    island = _basic(15, "Island", "Island")
    st_two = _state(lands_played=0, battlefield=base_bf, hand=[burn, mountain, island])
    menu_two = _menu(_cast_opt(0, burn), _play_opt(1, mountain), _play_opt(2, island), _pass_opt())
    assert land_before_cast_order(menu_two, st_two, "idx:0") is None

    # Control 7: land enters tapped or has channel
    tapland = _land(16, "Tapped Crag", oracle="Tapped Crag enters tapped.\n{T}: Add {R}.")
    chanland = _land(17, "Channel Peak", oracle="{T}: Add {R}.\nChannel — {1}{R}, Discard ~: Deal 2.")
    for bad_land in (tapland, chanland):
        st_bad = _state(lands_played=0, battlefield=base_bf, hand=[burn, bad_land])
        menu_bad = _menu(_cast_opt(0, burn), _play_opt(1, bad_land), _pass_opt())
        assert land_before_cast_order(menu_bad, st_bad, "idx:0") is None

    # Control 8: opponent controls land-entry punisher
    punisher = _creature(
        18,
        "Land Punisher",
        OPP,
        2,
        2,
        "Whenever a land enters under an opponent's control, this creature deals 2 damage to that player.",
    )
    st_pun = _state(lands_played=0, battlefield=[*base_bf, punisher], hand=[burn, mountain])
    assert land_before_cast_order(menu, st_pun, "idx:0") is None

    # Control 9: land_drop_first=False via ActionPlanner keeps the chosen creature cast
    bear = _creature(19, "Red Raider", LOCAL, 2, 1, "Haste", cost="{R}", keywords=["haste"])
    st_ok = _state(lands_played=0, battlefield=base_bf, hand=[bear, mountain])
    menu_cre = _menu(_cast_opt(0, bear), _play_opt(1, mountain), _pass_opt())
    planner_off = ActionPlanner(
        _CaptureBackend(json.dumps({"option_ids": ["idx:0"], "reasoning": "Cast creature."})),
        land_drop_first=False,
    )
    assert planner_off.plan_decision_options(menu_cre, st_ok) == ["idx:0"]


# ---------------------------------------------------------------------------
# S2-12B: Late-Game Hellbent Bluff Land Hold
# ---------------------------------------------------------------------------


def test_s2_12b_superfluous_bluff_land_and_prompt_and_controls():
    seventh = _basic(70, "Mountain", "Mountain")
    six_lands = [_basic(i, "Mountain", "Mountain") for i in range(1, 7)]
    st = _state(
        lands_played=0,
        their_life=6,
        battlefield=[
            *six_lands,
            _creature(101, "Attacker One", LOCAL, 2, 2, "", cost="{1}{R}"),
            _creature(102, "Opp Blocker", OPP, 2, 3, "", cost="{2}{W}"),
        ],
        hand=[seventh],
    )
    play_opt = _play_opt(0, seventh)
    pass_opt = _pass_opt()
    menu = _menu(play_opt, pass_opt)

    assert superfluous_bluff_land(play_opt, st) is True
    notes = bluff_land_notes(menu, st)
    assert len(notes) == 1 and notes[0].startswith("BLUFF LAND:")

    ls = assess(st).line_search
    assert ls is not None
    assert option_note(ls, play_opt, st) == ""
    assert option_note(ls, pass_opt, st).startswith("[LINE best:")

    # Normal entry point deterministically holds the bluff land without calling the LLM
    backend = _CaptureBackend(json.dumps({"option_ids": ["pass"], "reasoning": "Hold bluff."}))
    planner = ActionPlanner(backend)
    assert planner.plan_decision_options(menu, st) == ["pass"]
    assert backend.calls == []

    # Control 1: only 5 lands on board -> play it
    st_five = _state(lands_played=0, battlefield=six_lands[:5], hand=[seventh])
    assert superfluous_bluff_land(play_opt, st_five) is False
    assert bluff_land_notes(menu, st_five) == []

    # Control 2: another card in hand -> not hellbent bluff
    spell = _spell(71, "Spark", "{R}", "Spark deals 2 damage to any target.")
    st_two = _state(lands_played=0, battlefield=six_lands, hand=[seventh, spell])
    assert superfluous_bluff_land(play_opt, st_two) is False

    # Control 3: new color fix (Island alongside 6 Mountains)
    new_color = _basic(72, "Island", "Island")
    st_color = _state(lands_played=0, battlefield=six_lands, hand=[new_color])
    assert superfluous_bluff_land(_play_opt(0, new_color), st_color) is False

    # Control 4: cycling/channel/utility/MDFC land
    cyc = _land(73, "Cycle Hill", oracle="{T}: Add {R}.\nCycling {2}")
    util = _land(74, "Loot Peak", oracle="{T}: Add {R}.\n{2}{R}, {T}: Draw a card.")
    mdfc = _land(
        75,
        "Backside Peak",
        oracle="{T}: Add {R}.",
        related_faces=[{"name": "Front Spell", "type_line": "Sorcery", "mana_cost": "{1}{R}"}],
    )
    for special in (cyc, util, mdfc):
        st_sp = _state(lands_played=0, battlefield=six_lands, hand=[special])
        assert superfluous_bluff_land(_play_opt(0, special), st_sp) is False

    # Control 5: board landfall or >6 mana sink or X sink
    lf_perm = _creature(80, "Watcher", LOCAL, 2, 2, "Whenever a land you control enters, gain 1 life.")
    big_sink = _creature(81, "Mega Pump", LOCAL, 3, 3, "{6}{R}: This creature gets +3/+0.")
    x_sink = _creature(82, "Firebreather", LOCAL, 2, 2, "{X}{R}: This creature deals X damage.")
    for perm in (lf_perm, big_sink, x_sink):
        st_perm = _state(lands_played=0, battlefield=[*six_lands, perm], hand=[seventh])
        assert superfluous_bluff_land(play_opt, st_perm) is False

    # Control 6: graveyard flashback costing 7 mana > 6 lands
    fb = _spell(90, "Huge Echo", "{4}{R}", "Deal 5.\nFlashback {6}{R}", type_line="Sorcery")
    st_gy = _state(lands_played=0, battlefield=six_lands, hand=[seventh], graveyard=[fb])
    assert superfluous_bluff_land(play_opt, st_gy) is False


# ---------------------------------------------------------------------------
# S2-13: Stonewalled Pump Attackers, Lifelink Blocking in Reach, Spare Mana
# ---------------------------------------------------------------------------


def test_s2_13_cast_pumped_attackers_excludes_stonewalled_flyer_only():
    flyer_pumper = _creature(
        10,
        "Sky Pumper",
        LOCAL,
        1,
        2,
        "Flying, haste\nWhenever you cast a noncreature spell, this creature gets +2/+0 until end of turn.",
        keywords=["flying", "haste"],
    )
    big_flyer = _creature(20, "Wall Angel", OPP, 5, 5, "Flying", tapped=False, keywords=["flying"])
    ground_blocker = _creature(21, "Ground Brute", OPP, 5, 5, "", tapped=False)

    # Stonewalled by 5/5 untapped flyer -> []
    st_walled = _state(battlefield=[flyer_pumper, big_flyer])
    assert cast_pumped_attackers(st_walled) == []

    # Controls: empty board or ground-only blocker -> flyer gets through -> ["Sky Pumper"]
    assert cast_pumped_attackers(_state(battlefield=[flyer_pumper])) == ["Sky Pumper"]
    assert cast_pumped_attackers(_state(battlefield=[flyer_pumper, ground_blocker])) == ["Sky Pumper"]


def test_s2_13_two_turn_reach_caps_on_lifelink_blocker_and_preserves_vanilla_beasts():
    haster = _creature(10, "Quick Runner", LOCAL, 2, 2, "Haste", keywords=["haste"], turn_entered=8)
    burn_a = _spell(11, "Spark Two", "{R}", "Spark Two deals 2 damage to any target.")
    burn_b = _spell(12, "Bolt Three", "{1}{R}", "Bolt Three deals 3 damage to any target.")
    lifelinker = _creature(
        20,
        "Lifelink Colossus",
        OPP,
        7,
        7,
        "First strike, reach, lifelink",
        tapped=False,
        keywords=["first strike", "reach", "lifelink"],
    )
    vanilla_giant = _creature(21, "Vanilla Colossus", OPP, 7, 7, "Reach", tapped=False, keywords=["reach"])
    lands = [_basic(i, "Mountain", "Mountain") for i in range(1, 5)]

    # Untapped 7/7 FS reach lifelink stops 2+2 attack reach, leaving only 5 burn < 8 life -> ""
    st_ll = _state(
        turn_number=8,
        their_life=8,
        battlefield=[*lands, haster, lifelinker],
        hand=[burn_a, burn_b],
    )
    assert two_turn_reach(st_ll) == ""

    # Control A: same 7/7 reach without lifelink leaves 2+2+5=9 >= 8 (preserves #228 overestimate)
    st_van = _state(
        turn_number=8,
        their_life=8,
        battlefield=[*lands, haster, vanilla_giant],
        hand=[burn_a, burn_b],
    )
    assert two_turn_reach(st_van).startswith("up to ")

    # Control B: flying attacker bypasses ground-only lifelinker
    sky_runner = _creature(
        13, "Sky Runner", LOCAL, 2, 2, "Flying, haste", keywords=["flying", "haste"], turn_entered=8
    )
    ground_ll = _creature(22, "Ground Vampire", OPP, 4, 4, "Lifelink", tapped=False, keywords=["lifelink"])
    st_fly = _state(
        turn_number=8,
        their_life=8,
        battlefield=[*lands, sky_runner, ground_ll],
        hand=[burn_a, burn_b],
    )
    assert two_turn_reach(st_fly).startswith("up to ")

    # Control C: tapped lifelinker allows `now` (2) + caps `later` (0) -> 2 + 0 + 5 = 7 < 8 ("")
    tapped_ll = {**lifelinker, "is_tapped": True}
    st_tap_ll = _state(
        turn_number=8,
        their_life=8,
        battlefield=[*lands, haster, tapped_ll],
        hand=[burn_a, burn_b],
    )
    assert two_turn_reach(st_tap_ll) == ""


def test_s2_13_hold_overrides_spare_mana_and_end_to_end_planner_holds():
    spark = _spell(10, "Instant Spark", "{R}", "Instant Spark deals 2 damage to any target.")
    opt = _cast_opt(0, spark)
    four_mtns = [_basic(i, "Mountain", "Mountain") for i in range(1, 5)]

    # Open board + opponent has 2 untapped lands -> still holds (returns "")
    st_open = _state(
        battlefield=[
            *four_mtns,
            _basic(20, "Island", "Island", seat=OPP, tapped=False),
            _basic(21, "Plains", "Plains", seat=OPP, tapped=False),
        ],
        hand=[spark],
    )
    assert hold_overrides(_menu(opt, _pass_opt()), st_open, [(opt, spark)]) == ""

    # Opponent controls creature -> still holds
    st_cre = _state(
        battlefield=[*four_mtns, _creature(30, "Opp Watcher", OPP, 2, 2, "")],
        hand=[spark],
    )
    assert hold_overrides(_menu(opt, _pass_opt()), st_cre, [(opt, spark)]) == ""

    # Opposite control: empty board and opp tapped out -> spare mana still releases
    st_empty = _state(
        battlefield=[*four_mtns, _basic(22, "Swamp", "Swamp", seat=OPP, tapped=True)],
        hand=[spark],
    )
    assert hold_overrides(_menu(opt, _pass_opt()), st_empty, [(opt, spark)]).startswith(
        "we have more mana than the end step can spend"
    )

    # Normal entry point ActionPlanner.plan_decision_options deterministically holds without calling LLM
    flyer_pumper = _creature(
        40,
        "Sky Pumper",
        LOCAL,
        1,
        2,
        "Flying, haste\nWhenever you cast a noncreature spell, this creature gets +2/+0 until end of turn.",
        keywords=["flying", "haste"],
    )
    big_flyer = _creature(41, "Wall Angel", OPP, 5, 5, "Flying", tapped=False, keywords=["flying"])
    st_planner_hold = _state(
        battlefield=[*four_mtns, flyer_pumper, big_flyer],
        hand=[spark],
    )
    menu_hold = _menu(opt, _pass_opt())
    planner = ActionPlanner(_OfflineBackend())
    assert planner.plan_decision_options(menu_hold, st_planner_hold) == ["pass"]


# ---------------------------------------------------------------------------
# S2-14: Offline Board-Math Fallback for Unseen Threats & Proactive Permanents
# ---------------------------------------------------------------------------


def test_s2_14_board_math_option_pick_unseen_threats_and_proactive_unmodelled_and_controls():
    creature_unseen = _creature(10, "Hidden Two-Drop", LOCAL, 2, 2, "", cost="{1}{G}")
    proactive_eq = _spell(
        11,
        "Forge Hammer",
        "{1}{R}",
        "When this Equipment enters, create a 1/1 Warrior creature token.\nEquipped creature gets +2/+0.\nEquip {2}",
        type_line="Artifact — Equipment",
    )
    reactive_counter = _spell(12, "Cancel Spell", "{1}{U}", "Counter target spell.", type_line="Instant")
    targeted_ench = _spell(
        13,
        "Oblivion Ench",
        "{2}{W}",
        "When Oblivion Ench enters, exile target nonland permanent an opponent controls until ~ leaves.",
        type_line="Enchantment",
    )
    opp_aura = _spell(
        14,
        "Helpful Wings",
        "{1}{W}",
        "Flash\nEnchant creature\nEnchanted creature gets +1/+0 and has flying.",
        type_line="Enchantment — Aura",
        keywords=["flash"],
    )

    lands = [_basic(1, "Mountain", "Mountain"), _basic(2, "Island", "Island"), _basic(3, "Plains", "Plains")]
    st = _state(battlefield=lands, hand=[creature_unseen, proactive_eq])

    # Positive A: payable creature omitted from real TurnProjection.castable -> deploys creature
    step_empty_castable = SimpleNamespace(land="", casts=(), attack=None, post_combat=(), castable=())
    assess_unseen = SimpleNamespace(our_turn=True, lookahead=[step_empty_castable], line_search=None)
    menu_cre = _menu(_cast_opt(0, creature_unseen), _pass_opt())
    with (
        patch("arenamcp.board_assessment.assess", return_value=assess_unseen),
        patch("arenamcp.board_assessment.option_role", return_value="creature"),
    ):
        picked_a, reason_a = board_math_option_pick(menu_cre, st)
    assert picked_a == ["idx:0"]
    assert "not modelled in board-math mana" in reason_a

    # Positive B: proactive unmodelled sorcery-speed Equipment deployed when held and not deferred
    proactive_eq = _spell(
        11,
        "Forge Hammer",
        "{1}{R}",
        "When Forge Hammer enters, amass Fighters 1.\nEquipped creature gets +2/+0.\nEquip {2}",
        type_line="Artifact — Equipment",
    )
    st_eq = _state(battlefield=lands, hand=[proactive_eq])
    step_eq = SimpleNamespace(land="", casts=(), attack=None, post_combat=(), castable=("Forge Hammer",))
    assess_eq = SimpleNamespace(our_turn=True, lookahead=[step_eq], line_search=None, unmodelled=())
    menu_eq = _menu(_cast_opt(0, proactive_eq), _pass_opt())
    with (
        patch("arenamcp.board_assessment.assess", return_value=assess_eq),
        patch("arenamcp.board_assessment.option_role", return_value="other"),
    ):
        picked_b, reason_b = board_math_option_pick(menu_eq, st_eq)
    assert picked_b == ["idx:0"]
    assert "proactive permanent" in reason_b

    # Control 1: counterspell never cast proactively
    menu_ctr = _menu(_cast_opt(0, reactive_counter), _pass_opt())
    with (
        patch("arenamcp.board_assessment.assess", return_value=assess_eq),
        patch("arenamcp.board_assessment.option_role", return_value="counter"),
    ):
        assert board_math_option_pick(menu_ctr, _state(battlefield=lands, hand=[reactive_counter]))[0] == [
            "pass"
        ]

    # Control 2: enchantment with mandatory non-equip target never blindly cast
    menu_tgt = _menu(_cast_opt(0, targeted_ench), _pass_opt())
    with (
        patch("arenamcp.board_assessment.assess", return_value=assess_eq),
        patch("arenamcp.board_assessment.option_role", return_value="other"),
    ):
        assert board_math_option_pick(menu_tgt, _state(battlefield=lands, hand=[targeted_ench]))[0] == [
            "pass"
        ]

    # Control 3: beneficial Aura with only opposing creature on board blocked by unsafe_play_reason
    opp_only_cre = _creature(50, "Opp Knight", OPP, 2, 2, "")
    st_aura = _state(battlefield=[*lands, opp_only_cre], hand=[opp_aura])
    menu_aura = _menu(_cast_opt(0, opp_aura), _pass_opt())
    with (
        patch("arenamcp.board_assessment.assess", return_value=assess_eq),
        patch("arenamcp.board_assessment.option_role", return_value="other"),
    ):
        assert board_math_option_pick(menu_aura, st_aura)[0] == ["pass"]

    # Control 4: deferred post-combat stays pass in Main 1
    step_def = SimpleNamespace(land="", casts=(), attack="all", post_combat=("Hidden Two-Drop",), castable=())
    assess_def = SimpleNamespace(our_turn=True, lookahead=[step_def], line_search=None)
    with (
        patch("arenamcp.board_assessment.assess", return_value=assess_def),
        patch("arenamcp.board_assessment.option_role", return_value="creature"),
    ):
        assert board_math_option_pick(menu_cre, st)[0] == ["pass"]

    # Control 5: mock Step missing `.castable` (intentional hold) keeps pass for both threats & equipment
    step_mock_hold = SimpleNamespace(land="", casts=(), attack=None, post_combat=())
    assess_mock_hold = SimpleNamespace(our_turn=True, lookahead=[step_mock_hold], line_search=None)
    with (
        patch("arenamcp.board_assessment.assess", return_value=assess_mock_hold),
        patch("arenamcp.board_assessment.option_role", return_value="creature"),
    ):
        assert board_math_option_pick(menu_cre, st)[0] == ["pass"]
    with (
        patch("arenamcp.board_assessment.assess", return_value=assess_mock_hold),
        patch("arenamcp.board_assessment.option_role", return_value="other"),
    ):
        assert board_math_option_pick(menu_eq, st_eq)[0] == ["pass"]

    # Control 6 (Major 1): modelled mana rock in step.castable deliberately held -> stays pass
    mana_rock = _spell(15, "Plain Relic", "{2}", "{T}: Add {C}.", type_line="Artifact")
    step_held_rock = SimpleNamespace(
        land="", casts=(), attack=None, post_combat=(), castable=("Plain Relic",)
    )
    assess_held_rock = SimpleNamespace(our_turn=True, lookahead=[step_held_rock], line_search=None)
    with (
        patch("arenamcp.board_assessment.assess", return_value=assess_held_rock),
        patch("arenamcp.board_assessment.option_role", return_value="ramp"),
    ):
        assert board_math_option_pick(
            _menu(_cast_opt(0, mana_rock), _pass_opt()),
            _state(battlefield=lands, hand=[mana_rock]),
        )[0] == ["pass"]

    # Control 7 (Major 1): bounce enchantment ("up to one target") excluded via _REACTIVE_UNMODELLED_ROLES
    bounce_ench = _spell(
        16,
        "Mist Seal",
        "{1}{U}",
        "When Mist Seal enters, return up to one target creature to its owner's hand.",
        type_line="Enchantment",
    )
    step_bounce = SimpleNamespace(land="", casts=(), attack=None, post_combat=(), castable=("Mist Seal",))
    assess_bounce = SimpleNamespace(our_turn=True, lookahead=[step_bounce], line_search=None)
    with (
        patch("arenamcp.board_assessment.assess", return_value=assess_bounce),
        patch("arenamcp.board_assessment.option_role", return_value="bounce"),
    ):
        assert board_math_option_pick(
            _menu(_cast_opt(0, bounce_ench), _pass_opt()),
            _state(battlefield=lands, hand=[bounce_ench]),
        )[0] == ["pass"]

    # Control 8 (Major 1): flash enchantment with empty type_line still recognized via card_types/keywords
    flash_no_tl = {
        "instance_id": 17,
        "name": "Flash Curtain",
        "mana_cost": "{1}{U}",
        "type_line": "",
        "card_types": ["CardType_Enchantment"],
        "oracle_text": "Flash\nWhen Flash Curtain enters, amass Sailors 1.",
        "controller_seat_id": LOCAL,
        "owner_seat_id": LOCAL,
        "keywords": ["flash"],
    }
    with (
        patch("arenamcp.board_assessment.assess", return_value=assess_eq),
        patch("arenamcp.board_assessment.option_role", return_value="other"),
    ):
        assert board_math_option_pick(
            _menu(_cast_opt(0, flash_no_tl), _pass_opt()),
            _state(battlefield=lands, hand=[flash_no_tl]),
        )[0] == ["pass"]


def test_s2_reviewer_edge_cases():
    """Pins edge cases caught during independent review (Majors 2-3, Minors 1-5)."""
    # Major 2 + Minor 4: 0/5 wall stonewalls without killing; 'cant block' and phased out do not stonewall
    monk = _creature(10, "Prowess Monk", LOCAL, 1, 2, "Prowess\nFlying", keywords=["prowess", "flying"])
    reach_wall = _creature(20, "Iron Wall", OPP, 0, 5, "Defender, reach", keywords=["defender", "reach"])
    assert cast_pumped_attackers(_state(battlefield=[monk, reach_wall])) == []

    cant_block_flyer = _creature(
        21, "Reckless Imp", OPP, 5, 5, "Flying\nReckless Imp can't block.", keywords=["flying"]
    )
    phased_flyer = {
        **_creature(22, "Phased Angel", OPP, 5, 5, "Flying", keywords=["flying"]),
        "is_phased_out": True,
    }
    assert cast_pumped_attackers(_state(battlefield=[monk, cant_block_flyer])) == ["Prowess Monk"]
    assert cast_pumped_attackers(_state(battlefield=[monk, phased_flyer])) == ["Prowess Monk"]

    cant_block_ll = _creature(
        23,
        "Brash Leech",
        OPP,
        7,
        7,
        "Reach, lifelink\nBrash Leech can't block.",
        keywords=["reach", "lifelink"],
    )
    attacker = _creature(11, "Swift Hound", LOCAL, 2, 2, "Haste", keywords=["haste"], turn_entered=8)
    burn = _spell(12, "Zap Five", "{3}{R}", "Zap Five deals 5 damage to any target.")
    st_cb_ll = _state(
        turn_number=8,
        their_life=8,
        battlefield=[*(_basic(i, "Mountain", "Mountain") for i in range(1, 5)), attacker, cant_block_ll],
        hand=[burn],
    )
    assert two_turn_reach(st_cb_ll).startswith("up to ")

    # Major 3 & Minor 2: packed {o6oR}/{oX}, commander tax, jump-start, unhydrated nonbasic, Crucible ID mismatch
    seventh = _basic(70, "Mountain", "Mountain")
    six_lands = [_basic(i, "Mountain", "Mountain") for i in range(1, 7)]
    play_opt = _play_opt(0, seventh)

    packed_sink = _creature(80, "Packed Dragon", LOCAL, 3, 3, "{o6oR}: +2/+0 until end of turn.")
    assert (
        superfluous_bluff_land(play_opt, _state(battlefield=[*six_lands, packed_sink], hand=[seventh]))
        is False
    )

    jumpstart_gy = _spell(81, "Jump Bolt", "{R}", "Deal 2.\nJump-start", type_line="Instant")
    assert (
        superfluous_bluff_land(
            play_opt, _state(battlefield=six_lands, hand=[seventh], graveyard=[jumpstart_gy])
        )
        is False
    )

    taxed_cmdr = {
        **_creature(82, "Expensive Cmdr", LOCAL, 4, 4, "Flying", cost="{4}{R}"),
        "commander_casts": 2,
    }
    st_cmdr = _state(battlefield=six_lands, hand=[seventh], command=[taxed_cmdr])
    assert superfluous_bluff_land(play_opt, st_cmdr) is False

    mystery_land = _land(83, "Mystery Sanctuary", oracle="")
    assert (
        superfluous_bluff_land(_play_opt(0, mystery_land), _state(battlefield=six_lands, hand=[mystery_land]))
        is False
    )

    crucible_opt = DecisionOption(
        "idx:9",
        "Play land: Mountain",
        payable=None,
        meta={"actionType": "ActionType_Play", "instanceId": 999},
    )
    assert superfluous_bluff_land(crucible_opt, _state(battlefield=six_lands, hand=[seventh])) is False

    # Minor 1 (seat 0 truthiness) + Minor 3 (PlayMDFC & duplicate Legendary land) + Minor 5 (__new__)
    m0 = _basic(10, "Mountain", "Mountain", seat=0)
    burn0 = _spell(11, "Spark Zero", "{R}", "Spark Zero deals 2 damage to any target.", seat=0)
    opp_land0 = _basic(12, "Island", "Island", seat=1, tapped=False)
    st_seat0 = {
        "local_seat_id": 0,
        "players": [
            {"seat_id": 0, "is_local": True, "life_total": 20, "lands_played": 0},
            {"seat_id": 1, "is_local": False, "life_total": 20, "lands_played": 0},
        ],
        "turn": {"turn_number": 4, "active_player": 0, "phase": "Phase_Main1", "step": ""},
        "battlefield": [_basic(1, "Mountain", "Mountain", seat=0), opp_land0],
        "hand": [burn0, m0],
        "stack": [],
    }
    menu0 = _menu(_cast_opt(0, burn0), _play_opt(1, m0), _pass_opt())
    assert land_before_cast_order(menu0, st_seat0, "idx:0") is not None

    mdfc_opt = DecisionOption(
        "idx:2",
        "Play land: Modal Peak",
        payable=None,
        meta={"actionType": "ActionType_PlayMDFC", "instanceId": 13},
    )
    mdfc_card = _land(13, "Modal Front", oracle="{T}: Add {R}.")
    st_mdfc = {**st_seat0, "hand": [burn0, m0, mdfc_card]}
    assert (
        land_before_cast_order(
            _menu(_cast_opt(0, burn0), _play_opt(1, m0), mdfc_opt, _pass_opt()), st_mdfc, "idx:0"
        )
        is None
    )

    leg_land = {**_land(14, "Legend Citadel", oracle="{T}: Add {R}."), "type_line": "Legendary Land"}
    leg_bf = {**_land(15, "Legend Citadel", seat=0, oracle="{T}: Add {R}."), "type_line": "Legendary Land"}
    st_leg = {**st_seat0, "battlefield": [*st_seat0["battlefield"], leg_bf], "hand": [burn0, leg_land]}
    assert (
        land_before_cast_order(
            _menu(_cast_opt(0, burn0), _play_opt(1, leg_land), _pass_opt()), st_leg, "idx:0"
        )
        is None
    )

    bare_planner = ActionPlanner.__new__(ActionPlanner)
    assert bare_planner._apply_land_before_cast_order(menu0, st_seat0, ["idx:0"]) == ["idx:1"]
