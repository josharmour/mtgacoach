"""Life-threshold anthems and "draw if you didn't attack" punishers (ledger #177, #180, 2026-10-19).

Match c0af235f (``tests.valkyrie_boards_20261019``), Brawl, starting life 25:

- #177: Righteous Valkyrie, "As long as you have at least 7 life more than your
  starting life total, creatures you control get +2/+2." At T12 they were at
  32 (25 + 7): all seven of their creatures had it (Valkyrie 4/6, Aven
  Gagglemaster 6/5, Warden 4/4, Faramir 5/5, three Human Soldiers 3/3). One
  point of our damage switched it off; we held, and their T13 fliers took 14.
  The line search projected their next attack with the +2/+2 whatever our
  attack did to their life.
- #180: Faramir, Prince of Ithilien: "... you draw a card if they didn't attack
  you that turn. Otherwise, create three 1/1 white Human Soldier creature
  tokens." Each of our holds (T8, T12) drew them a card; the T10 attack gave
  them the three Soldiers. Neither side of it was in the attack-vs-hold verdict.
"""

from __future__ import annotations

from copy import deepcopy

from tests import valkyrie_boards_20261019 as vb

from arenamcp import line_search_moves as moves
from arenamcp.board_assessment import assess
from arenamcp.board_model import build_board_model, starting_life
from arenamcp.line_search import _Search
from arenamcp.line_search_moves import attack_punisher, hold_punishers, life_anthem, life_anthem_facts

# The thirteen tokens the T12 best line attacks with (the Warriors but 576, the able Rabbits).
TOKENS = (600, 602, 603, 607, 609, 610, 611, 615, 617, 618, 619, 620, 625)
T12_STATS = {  # their creatures at gsid 306, with the +2/+2
    "Aven Gagglemaster": (6, 5), "Warden of Evos Isle": (4, 4), "Faramir, Prince of Ithilien": (5, 5),
    "Righteous Valkyrie": (4, 6), "Human Soldier": (3, 3),
}  # fmt: skip


def _search(state: dict) -> _Search:
    search = _Search(
        build_board_model(state), survival=False, lethal_now=False, hard_ms=1e9, groups=16, width=2,
        k_next=4, opponent=None,
    )  # fmt: skip
    search.free = True
    return search


def _stats(bodies) -> dict[str, tuple[int, int]]:
    return {b["name"]: (b["power"], b["toughness"]) for b in bodies}


# --- #177 ---------------------------------------------------------------------------------------


def test_the_anthem_text_and_the_starting_life():
    valkyrie = vb.card(547, "Righteous Valkyrie", vb.OPPONENT)
    assert life_anthem(valkyrie) == (7, 2, 2, "all")
    leyline = {"name": "Leyline of Hope", "oracle_text": "If you would gain life, you gain that much life plus 1 "
               "instead.\nAs long as you have at least 7 life more than your starting life total, creatures "
               "you control get +2/+2."}  # fmt: skip
    assert life_anthem(leyline) == (7, 2, 2, "all")
    assert life_anthem(vb.card(560, "Faramir, Prince of Ithilien", vb.OPPONENT)) is None
    # Brawl: the commanders say so (Player.log's startingLifeTotal is 25); a player's own value wins.
    state = vb.t12_declare_attackers()
    assert starting_life(state) == 25 and build_board_model(state).starting_life == 25
    constructed = {k: v for k, v in state.items() if k not in ("commander_grp_ids", "command")}
    assert starting_life(constructed) == 20
    constructed["players"] = deepcopy(state["players"])
    constructed["players"][1]["starting_life"] = 30
    assert starting_life(constructed) == 30


def test_the_board_facts_say_one_damage_switches_it_off():
    t12 = assess(vb.t12_declare_attackers())
    line = "1 damage switches off Righteous Valkyrie's +2/+2 (they are at 32: on from 32, starting life 25)"
    assert line in t12.flags and line in t12.facts_line()
    # T10 at 27: off, five life away (their T11 Gagglemaster gained six).
    assert life_anthem_facts(build_board_model(vb.t10_main1())) == [
        "Righteous Valkyrie's +2/+2 is off; 5 life they gain switches it on (they are at 27: on from 32, "
        "starting life 25)"
    ]
    far = vb.t10_main1()
    far["players"][1]["life_total"] = 20
    assert life_anthem_facts(build_board_model(far)) == []


def test_their_attack_loses_the_bonus_below_the_threshold():
    search = _search(vb.t12_declare_attackers())
    theirs = list(search.model.theirs)
    assert _stats(theirs) == T12_STATS
    assert search.anthem_bodies(theirs, 32) == theirs  # still on
    off = search.anthem_bodies(theirs, 26)
    assert _stats(off) == {
        "Aven Gagglemaster": (4, 3), "Warden of Evos Isle": (2, 2), "Faramir, Prince of Ithilien": (3, 3),
        "Righteous Valkyrie": (2, 4), "Human Soldier": (1, 1),
    }  # fmt: skip
    assert all(b["_origin"] in {t["instance_id"] for t in theirs} for b in off)
    # Back above the threshold (life gain) it is on again; without the Valkyrie it stays off.
    assert _stats(search.anthem_bodies(off, 33)) == T12_STATS
    no_valkyrie = [b for b in theirs if b["name"] != "Righteous Valkyrie"]
    assert _stats(search.anthem_bodies(no_valkyrie, 40))["Human Soldier"] == (1, 1)
    # At T10 (27, off) a projection at 32 or more puts it on.
    t10 = _search(vb.t10_main1())
    assert _stats(t10.anthem_bodies(list(t10.model.theirs), 32))["Righteous Valkyrie"] == (4, 6)


def test_the_lines_see_the_anthem_end_when_our_attack_lands():
    result = assess(vb.t12_declare_attackers()).line_search
    attack = result.best
    # The thirteen tokens put them at 26: their T13 attack hits for 5 (25 -> 20), not 14.
    assert [(s.opp_life_after, s.life_after) for s in attack.steps[:1]] == [(26, 20)]
    hold = result.first_action[("noattack",)]
    assert hold.steps[0].life_after == 15  # holding keeps the +2/+2: 10 of their 14 come in


def test_without_the_anthem_the_attack_line_meets_the_full_crackback(monkeypatch):
    monkeypatch.setattr(moves, "life_anthem", lambda card: None)
    model = build_board_model(vb.t12_declare_attackers())
    from arenamcp.line_search import search_lines

    result = search_lines(model, survival=False, lethal_now=False, hard_ms=1e9)
    tokens = result.first_action[("attack", frozenset(TOKENS))]
    # The same thirteen-token attack: their +2/+2 fliers still hit for 10 (25 -> 15), and holding
    # ranks above it (the pre-#177 verdict was "hold").
    assert tokens.steps[0].life_after == 15
    assert result.posture == "hold"


# --- #180 ---------------------------------------------------------------------------------------


def test_faramir_draws_them_a_card_on_a_hold_and_makes_three_soldiers_on_an_attack():
    faramir = vb.card(560, "Faramir, Prince of Ithilien", vb.OPPONENT)
    assert attack_punisher(faramir) == (1, 3, 1, 1)
    plain = {"name": "Spymaster", "oracle_text": "At the beginning of your end step, if no creatures attacked "
             "you this turn, draw a card."}  # fmt: skip
    assert attack_punisher(plain) == (1, 0, 0, 0)
    assert attack_punisher(vb.card(547, "Righteous Valkyrie", vb.OPPONENT)) == (0, 0, 0, 0)


def test_the_hold_cost_in_the_facts_and_the_posture():
    state = vb.t12_declare_attackers()
    cost, text = hold_punishers(build_board_model(state))
    # A card for them (1.5) against three 1/1s (1.5 each) when we attack: net, attacking costs more.
    assert cost == 1.5 - 3 * 1.5
    assert (
        text
        == "holding draws them a card (Faramir, Prince of Ithilien; attacking gives them 3 1/1 tokens instead)"
    )
    facts = assess(state)
    assert text in facts.flags
    assert facts.line_search.posture == "attack" and facts.line_search.posture_reason.endswith(text)
    # Only before our attack on our turn, and only for a Faramir that saw their last end step.
    blockers = vb.t10_declare_blockers()
    assert hold_punishers(build_board_model(blockers)) == (0.0, "")
    fresh = vb.t12_declare_attackers()
    next(c for c in fresh["battlefield"] if c["instance_id"] == 560)["turn_entered_battlefield"] = 12
    assert hold_punishers(build_board_model(fresh)) == (0.0, "")


def test_a_pure_draw_punisher_is_a_hold_cost():
    state = vb.t12_declare_attackers()
    faramir = next(c for c in state["battlefield"] if c["instance_id"] == 560)
    faramir["oracle_text"] = (
        "At the beginning of your end step, if no creatures attacked you this turn, draw a card."
    )
    cost, text = hold_punishers(build_board_model(state))
    assert cost == 1.5 and text == "holding draws them a card (Faramir, Prince of Ithilien)"
