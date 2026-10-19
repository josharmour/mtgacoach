"""Concede margin: the most defensive play, and Auras that stay (ledger #52, 2026-10-19).

Match a9cd5a2e (10:38:52, our T10, 5 life): the concede reason said "their 3/3
flying Skyward Spider and 4 more damage". The Spider was 5/4: 2/2 printed, a
+1/+1 counter, Shardmage's Rescue (+1/+1) and Sheltered by Ghosts (+1/+0). The
best case for us treated the two Auras' static buffs as "until end of turn"
pumps and cut it to 3/3. The concede was right (5 flying damage vs 5 life),
but the margin came from the projected BEST line, which may race with the only
chump blocker, and from the block solver's "best blocks", which stop chumping
once every block loses: on an exactly lethal board with a chump available the
coach would have conceded a game that holding the chump back leaves at 0, not
a clear -2.
"""

from __future__ import annotations

from tests.test_concede import board, creature, permanent

from arenamcp.concede import _best_case_view, _max_flow, _most_chumped_life, estimate_loss

US, THEM = 1, 2


def _mountains(first: int, count: int, *, tapped: bool = False) -> list[dict]:
    return [
        permanent(first + k, "Mountain", US, "Basic Land — Mountain", "({T}: Add {R}.)", is_tapped=tapped)
        for k in range(count)
    ]


def _spider_board() -> dict:
    """a9cd5a2e at 10:38:52: our T10 Main 1 after Emberheart Challenger resolved, 5 life vs 27."""
    spider = creature(
        287, "Skyward Spider", THEM, 5, 4, keywords=("flying",), tapped=True, entered=3,
        text="Ward {2}\nThis creature has flying as long as it's modified.",
        type_line="Creature — Spider Human Hero", printed_power=2, printed_toughness=2,
        counters={"CounterType_P1P1": 1},
    )  # fmt: skip
    rescue = permanent(
        302, "Shardmage's Rescue", THEM, "Enchantment — Aura",
        "Flash\nEnchant creature you control\nAs long as this Aura entered this turn, enchanted creature has "
        "hexproof.\nEnchanted creature gets +1/+1.", attached_to_id=287,
    )  # fmt: skip
    ghosts = permanent(
        297, "Sheltered by Ghosts", THEM, "Enchantment — Aura",
        "Enchant creature you control\nWhen this Aura enters, exile target nonland permanent an opponent "
        "controls until this Aura leaves the battlefield.\nEnchanted creature gets +1/+0 and has lifelink and "
        "ward {2}.", attached_to_id=287,
    )  # fmt: skip
    token = creature(316, "Spider", THEM, 2, 1, keywords=("reach",), tapped=True, entered=7, text="Reach")
    young = creature(328, "Skyward Spider", THEM, 2, 2, entered=9, printed_power=2, printed_toughness=2)
    ember = creature(
        336, "Emberheart Challenger", US, 2, 2, keywords=("haste",), entered=10, text="Haste\nProwess",
        type_line="Creature — Mouse Warrior",
    )  # fmt: skip
    plains = [permanent(400 + k, "Plains", THEM, "Basic Land — Plains", "({T}: Add {W}.)") for k in range(4)]
    battlefield = [spider, rescue, ghosts, token, young, ember, *_mountains(281, 3, tapped=True), *plains]
    return board(battlefield, life=5, opp_life=27, active=US, phase="Phase_Main1", turn=10)


def test_attached_auras_keep_their_static_buff_after_our_turn():
    state = _spider_board()
    view, _caps, notes = _best_case_view(state, local=US, opponent=THEM, our_turn=True, turn=10)
    spider = next(c for c in view["battlefield"] if c.get("instance_id") == 287)
    assert (spider["power"], spider["toughness"]) == (5, 4)
    assert not [note for note in notes if note.startswith("Skyward Spider counted as")]


def test_an_until_end_of_turn_pump_still_wears_off():
    state = _spider_board()
    spider = next(c for c in state["battlefield"] if c.get("instance_id") == 287)
    spider["power"], spider["toughness"] = 8, 7  # +3/+3 more than counters and Auras explain
    view, _caps, notes = _best_case_view(state, local=US, opponent=THEM, our_turn=True, turn=10)
    spider = next(c for c in view["battlefield"] if c.get("instance_id") == 287)
    assert (spider["power"], spider["toughness"]) == (5, 4)
    assert "Skyward Spider counted as 5/4 once this turn ends" in notes


def test_the_a9cd5a2e_board_is_still_a_concede_with_the_real_spider():
    estimate = estimate_loss(_spider_board())
    assert estimate.confidence >= 0.95, estimate.blockers
    assert "5/4 flying Skyward Spider" in estimate.reason


def _ogres(life: int) -> dict:
    """Our turn: two untapped 4/4s across from our only creature, a hasty 2/2 that could race or chump."""
    theirs = [creature(10, "Ogre", THEM, 4, 4), creature(11, "Ogre Brute", THEM, 4, 4)]
    ours = [creature(20, "Emberheart Challenger", US, 2, 2, keywords=("haste",), entered=10)]
    return board(theirs + ours + _mountains(30, 3), life=life, active=US, phase="Phase_Main1", turn=10)


def test_an_exactly_lethal_board_with_a_chump_is_not_conceded():
    # 8 through the solver's blocks (it stops chumping once every block loses); holding
    # the 2/2 back to chump leaves exactly 0: dead, but by no margin.
    estimate = estimate_loss(_ogres(life=4))
    assert estimate.confidence < 0.95
    assert "thin margin: 4 through vs 4 life" in estimate.blockers
    assert estimate.facts["defensive_life_after"] == 0


def test_a_chump_that_still_leaves_us_two_short_concedes():
    estimate = estimate_loss(_ogres(life=2))
    assert estimate.confidence >= 0.95, estimate.blockers


def test_the_most_chumped_life_is_an_upper_bound_that_counts_each_attacker_once():
    def body(name, power, toughness, *keywords):
        return {"instance_id": hash(name) % 1000, "name": name, "power": power, "toughness": toughness,
                "keywords": list(keywords), "oracle_text": ""}  # fmt: skip

    ogre, wurm, drake = body("Ogre", 4, 4), body("Wurm", 6, 6, "trample"), body("Drake", 3, 3, "flying")
    bears = [body(f"Bear {k}", 2, 2) for k in range(3)]
    # Three chumps but one 4/4 to block: 4 stopped, not 12; the flier always connects.
    assert _most_chumped_life([ogre, drake], bears, 10) == 10 - 3
    # A trampler is only slowed by the blockers' toughness; double strike hits twice.
    assert _most_chumped_life([wurm], bears[:2], 10) == 10 - 2
    striker = body("Striker", 3, 3, "double strike")
    assert _most_chumped_life([striker, ogre], bears[:1], 20) == 20 - 4
    assert _max_flow([4, 4], [[4, 0], [4, 3]], [4, 3]) == 7
