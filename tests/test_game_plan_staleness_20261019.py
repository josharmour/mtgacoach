"""Game-plan staleness and validation (2026-10-19 Standard Ranked, Mono-Red Burn).

#13 41753907: the plan formed at 09:02:21 ("T7: Effigy+Strike face, swing 4
unblocked -> opponent at -1; done") predated Unflinching Courage on Dawnwing
Marshal (4/4 flying, trample, lifelink at 09:02:23); the reform started for it
yielded to our first T7 decision and T7 ran on the old plan. f9e0ec3b: the T10
plan was requested at 09:18:55, Lyra Dawnbringer resolved at 09:19:08 and the
plan installed at 09:19:29 still attacked into her.
#23 a74bbc90: "their only blocker Moseo has shadow and cannot block" (Moseo has
flying and blocks Slickshot Show-Off).
#31 92c09c7a: "hold: {R}{R} spare - no instants in hand"; the model then passed
on Hired Claw's {1}{R} twice, "banking {1}{R}".
"""

from __future__ import annotations

import json
import logging
import threading
from copy import deepcopy

import pytest

from arenamcp import board_assessment as ba
from arenamcp.board_assessment import assess
from arenamcp.game_plan import (
    GamePlan,
    GamePlanManager,
    board_basis,
    board_changes,
    compose_strategy_block,
    validate_plan,
)

US, THEM = 2, 1

CARDS = {
    "Mountain": {"type_line": "Basic Land — Mountain", "oracle_text": "({T}: Add {R}.)",
                 "card_types": ["CardType_Land"]},
    "Plains": {"type_line": "Basic Land — Plains", "oracle_text": "({T}: Add {W}.)",
               "card_types": ["CardType_Land"]},
    "Fleeting Effigy": {"type_line": "Creature — Elemental", "mana_cost": "{R}", "power": 2, "toughness": 2,
                        "oracle_text": "Haste\nAt the beginning of your end step, return this creature to its "
                        "owner's hand.\n{2}{R}: This creature gets +2/+0 until end of turn.",
                        "card_types": ["CardType_Creature"]},
    "Hired Claw": {"type_line": "Creature — Lizard Mercenary", "mana_cost": "{R}", "power": 1, "toughness": 2,
                   "oracle_text": "Whenever you attack with one or more Lizards, this creature deals 1 damage to "
                   "target opponent.\n{1}{R}: Put a +1/+1 counter on this creature. Activate only if an "
                   "opponent lost life this turn and only once each turn.",
                   "card_types": ["CardType_Creature"]},
    "Slickshot Show-Off": {"type_line": "Creature — Bird Wizard", "mana_cost": "{1}{R}", "power": 1,
                           "toughness": 2, "oracle_text": "Flying, haste\nWhenever you cast a noncreature "
                           "spell, this creature gets +2/+0 until end of turn.\nPlot {1}{R} (You may pay {1}{R} "
                           "and exile this card from your hand. Cast it as a sorcery on a later turn without "
                           "paying its mana cost. Plot only as a sorcery.)",
                           "card_types": ["CardType_Creature"]},
    "Emberheart Challenger": {"type_line": "Creature — Mouse Warrior", "mana_cost": "{1}{R}", "power": 2,
                              "toughness": 2, "oracle_text": "Haste\nProwess", "card_types": ["CardType_Creature"]},
    "Lightning Strike": {"type_line": "Instant", "mana_cost": "{1}{R}",
                         "oracle_text": "Lightning Strike deals 3 damage to any target.",
                         "card_types": ["CardType_Instant"]},
    "Burst Lightning": {"type_line": "Instant", "mana_cost": "{R}",
                        "oracle_text": "Kicker {4}\nBurst Lightning deals 2 damage to any target. If this spell "
                        "was kicked, it deals 4 damage instead.", "card_types": ["CardType_Instant"]},
    "Dawnwing Marshal": {"type_line": "Creature — Cat Soldier", "mana_cost": "{1}{W}", "power": 2,
                         "toughness": 2, "oracle_text": "Flying\n{4}{W}: Creatures you control get +1/+1 until "
                         "end of turn.", "card_types": ["CardType_Creature"]},
    "Unflinching Courage": {"type_line": "Enchantment — Aura", "mana_cost": "{1}{G}{W}",
                            "oracle_text": "Enchant creature\nEnchanted creature gets +2/+2 and has trample and "
                            "lifelink.", "card_types": ["CardType_Enchantment"]},
    "Lyra Dawnbringer": {"type_line": "Legendary Creature — Angel", "mana_cost": "{3}{W}{W}", "power": 5,
                         "toughness": 5, "oracle_text": "Flying\nFirst strike\nLifelink\nOther Angels you "
                         "control get +1/+1 and have lifelink.", "card_types": ["CardType_Creature"]},
    "Inspiring Overseer": {"type_line": "Creature — Angel Cleric", "mana_cost": "{2}{W}", "power": 2,
                           "toughness": 1, "oracle_text": "Flying\nWhen this creature enters, you gain 1 life "
                           "and draw a card.", "card_types": ["CardType_Creature"]},
    "Moseo, Vein's New Dean": {"type_line": "Legendary Creature — Bird Skeleton Warlock", "mana_cost": "{2}{B}",
                               "power": 2, "toughness": 1, "oracle_text": "Flying\nWhen Moseo enters, create a "
                               "1/1 black and green Pest creature token.", "card_types": ["CardType_Creature"]},
    "Hinterland Sanctifier": {"type_line": "Creature — Rabbit Cleric", "mana_cost": "{W}", "power": 1,
                              "toughness": 2, "oracle_text": "Whenever another creature you control enters, "
                              "you gain 1 life.", "card_types": ["CardType_Creature"]},
    "Sazh's Chocobo": {"type_line": "Creature — Bird", "mana_cost": "{G}", "power": 2, "toughness": 3,
                       "oracle_text": "Landfall — Whenever a land you control enters, put a +1/+1 counter on "
                       "this creature.", "card_types": ["CardType_Creature"]},
    "Brute": {"type_line": "Creature — Ogre", "mana_cost": "{2}{R}", "power": 3, "toughness": 3,
              "oracle_text": "This creature can't block.", "card_types": ["CardType_Creature"]},
    "Boltwave": {"type_line": "Sorcery", "mana_cost": "{R}", "oracle_text": "Boltwave deals 3 damage to each "
                 "opponent.", "card_types": ["CardType_Sorcery"]},
    "Tectonic Hazard": {"type_line": "Sorcery", "mana_cost": "{R}", "oracle_text": "Tectonic Hazard deals 1 "
                        "damage to each opponent and each creature they control.",
                        "card_types": ["CardType_Sorcery"]},
    "Bristly Bill, Spine Sower": {"type_line": "Legendary Creature — Human Druid", "mana_cost": "{1}{G}",
                                  "power": 2, "toughness": 2, "oracle_text": "Landfall — Whenever a land you "
                                  "control enters, put a +1/+1 counter on target creature.",
                                  "card_types": ["CardType_Creature"]},
}  # fmt: skip


def card(iid: int, name: str, seat: int, **extra) -> dict:
    return {"instance_id": iid, "name": name, "owner_seat_id": seat, "controller_seat_id": seat,
            **deepcopy(CARDS[name]), **extra}  # fmt: skip


def lands(first: int, count: int, seat: int, name: str = "Mountain") -> list[dict]:
    return [card(first + k, name, seat, turn_entered_battlefield=1) for k in range(count)]


def board(turn: int, active: int, battlefield: list[dict], hand: list[dict], *, life=None) -> dict:
    life = life or {US: 20, THEM: 20}
    return {
        "match_id": "m-20261019",
        "local_seat_id": US,
        "opponent_seat_id": THEM,
        "turn": {"turn_number": turn, "active_player": active, "priority_player": active,
                 "phase": "Phase_Main1", "step": ""},
        "players": [
            {"seat_id": THEM, "is_local": False, "life_total": life[THEM], "lands_played": 0, "hand_size": 4},
            {"seat_id": US, "is_local": True, "life_total": life[US], "lands_played": 0, "hand_size": len(hand)},
        ],
        "battlefield": battlefield,
        "hand": hand,
        "graveyard": [],
        "exile": [],
        "stack": [],
        "zones": {"library_count": 30, "library_count_source": "log_zone_membership",
                  "opponent_hand_count": 4},
    }  # fmt: skip


@pytest.fixture(autouse=True)
def _fresh_assessments(monkeypatch):
    # The line guard is on by default since 2026-10-20 (guard_settings.GUARD_DEFAULTS); these tests
    # read its verdicts in shadow unless they set a mode.
    monkeypatch.setenv("ARENAMCP_LINE_GUARD", "shadow")
    ba._CACHE.clear()
    yield
    ba._CACHE.clear()


class ScriptedBackend:
    """Returns the scripted plans in order; ``gates`` hold a call until released."""

    def __init__(self, responses, gates=None):
        self.responses = list(responses)
        self.gates = list(gates or [])
        self.calls: list[tuple[str, dict]] = []
        self.entered = [threading.Event() for _ in range(8)]

    def complete(self, system, user, max_tokens, **kwargs):
        index = len(self.calls)
        self.calls.append((user, kwargs))
        self.entered[index].set()
        if index < len(self.gates) and self.gates[index] is not None:
            assert self.gates[index].wait(timeout=3)
        return self.responses[index] if index < len(self.responses) else "{}"


def plan_json(path: str, turns: list[dict], role: str = "aggressor") -> str:
    return json.dumps({"role": role, "role_reason": "burn race with a hasty board", "turns": turns,
                       "win_conditions": ["Burn plus hasty attackers"], "path": path,
                       "threat": "their flier", "develop_next": "attackers"})  # fmt: skip


# --- #13: a plan written before the opponent's board changed -------------------------------


def _marshal_boards():
    """41753907: opponent's T6 before Unflinching Courage, and our T7 after it (opp 10 life)."""
    ours = [card(289, "Emberheart Challenger", US, turn_entered_battlefield=3), *lands(400, 3, US)]
    hand = [card(310, "Fleeting Effigy", US), card(221, "Lightning Strike", US),
            card(330, "Slickshot Show-Off", US)]  # fmt: skip
    before = board(6, THEM, [card(294, "Dawnwing Marshal", THEM, turn_entered_battlefield=4),
                             *lands(500, 3, THEM, "Plains"), *ours], hand, life={US: 20, THEM: 6})  # fmt: skip
    after = board(
        7, US,
        [card(294, "Dawnwing Marshal", THEM, power=4, toughness=4, keywords=["flying", "trample", "lifelink"],
              turn_entered_battlefield=4),
         card(314, "Unflinching Courage", THEM, attached_to_id=294, turn_entered_battlefield=6),
         *lands(500, 3, THEM, "Plains"), *ours],
        hand, life={US: 16, THEM: 10},
    )  # fmt: skip
    return before, after


T7_PLAN = plan_json(
    "T7: Effigy+Strike face, swing 4 unblocked -> opponent at -1; done",
    [{"turn": "T", "land": "", "cast": ["Fleeting Effigy", "Lightning Strike"],
      "attack": "Emberheart Challenger, Fleeting Effigy", "hold": ""}],
)  # fmt: skip


def test_board_changes_name_the_aura_buff_and_the_life_gain():
    before, after = _marshal_boards()
    old, new = board_basis(before), board_basis(after)
    assert old["creatures"] == [
        {"id": "294", "name": "Dawnwing Marshal", "power": 2, "toughness": 2, "keywords": ["flying"],
         "modified": False}
    ]  # fmt: skip
    assert new["creatures"][0]["modified"] is True
    assert board_changes(old, new, threats_only=True) == [
        "Dawnwing Marshal is now 4/4 flying, trample, lifelink (was 2/2 flying)",
        "Unflinching Courage entered",
    ]
    assert board_changes(old, new)[-1] == "the opponent gained 4 life (6 -> 10)"
    assert board_changes(old, deepcopy(old)) == []
    assert board_changes({}, new) == [] and board_changes(old, {}) == []


def test_minor_changes_mark_the_plan_but_are_not_threats():
    before, _after = _marshal_boards()
    old = board_basis(before)
    bear = deepcopy(before)
    bear["battlefield"].append(card(600, "Hinterland Sanctifier", THEM))
    grown = deepcopy(before)
    grown["battlefield"][0].update(power=3, toughness=3)  # a combat trick: not modified
    assert board_changes(old, board_basis(bear)) == ["Hinterland Sanctifier 1/2 entered"]
    assert board_changes(old, board_basis(bear), threats_only=True) == []
    assert board_changes(old, board_basis(grown), threats_only=True) == []
    assert board_changes(old, board_basis(grown)) == ["Dawnwing Marshal is now 3/3 flying (was 2/2 flying)"]
    lyra = deepcopy(before)
    lyra["battlefield"].append(card(700, "Lyra Dawnbringer", THEM))
    assert board_changes(old, board_basis(lyra), threats_only=True) == [
        "Lyra Dawnbringer 5/5 flying, first strike, lifelink entered"
    ]
    # A vanilla body as big as our biggest creature (Emberheart Challenger 2/2) blocks it.
    ogre = deepcopy(before)
    ogre["battlefield"].append(card(701, "Brute", THEM))
    assert board_changes(old, board_basis(ogre), threats_only=True) == ["Brute 3/3 entered"]
    ours = next(c for c in ogre["battlefield"] if c["name"] == "Emberheart Challenger")
    ours.update(power=5, toughness=5)  # with a 5/5 of ours, a 3/3 no longer is
    assert board_changes(old, board_basis(ogre), threats_only=True) == []


def test_the_decision_prompt_flags_a_plan_written_before_the_aura():
    before, after = _marshal_boards()
    mgr = GamePlanManager(ScriptedBackend([T7_PLAN, T7_PLAN]))
    mgr._plan = GamePlan(win_conditions=["x"], path="y", turn_formed=4)  # a prior plan: high effort next
    mgr._last_sig, mgr._last_reform_turn = (4,), 4
    plan = mgr._reform(deepcopy(before), 6)
    assert plan is not None and plan.basis["creatures"][0]["power"] == 2
    mgr._plan = plan
    block = mgr.strategy_block(deepcopy(after))
    this_turn = next(line for line in block.splitlines() if line.startswith("  THIS TURN"))
    assert this_turn.endswith(
        "[game plan T6, written before the opponent's board changed: re-check its attacks]"
    ), this_turn
    assert (
        "  PLAN PREDATES THE OPPONENT'S BOARD (game plan turn 6): Dawnwing Marshal is now 4/4 flying, "
        "trample, lifelink (was 2/2 flying); Unflinching Courage entered; the opponent gained 4 life "
        "(6 -> 10)." in block
    )
    # The same board the plan was written from: no flag.
    same = mgr.strategy_block(deepcopy(before))
    assert "PLAN PREDATES" not in same and "written before" not in same
    # A plan without a recorded board (restored, or older) is never flagged.
    assert "PLAN PREDATES" not in compose_strategy_block(
        assess(deepcopy(after)),
        GamePlan(win_conditions=["x"], path="y", turn_formed=6, turn_plan=[dict(plan.turn_plan[0])]),
        state=deepcopy(after),
    )


def test_a_reform_for_a_new_threat_keeps_running_alongside_a_decision(caplog):
    import time

    before, after = _marshal_boards()
    mgr = GamePlanManager(backend=None)
    mgr._plan = GamePlan(win_conditions=["x"], path="y", turn_formed=6, basis=board_basis(before))
    cancel = threading.Event()
    mgr._inflight, mgr._inflight_cancel, mgr._inflight_lane = True, cancel, None
    mgr._inflight_started_at = time.monotonic()  # fresh: would yield
    mgr._inflight_basis = board_basis(after)
    with caplog.at_level(logging.INFO, logger="arenamcp.game_plan"):
        assert mgr.yield_for_decision() is False
    assert not cancel.is_set() and mgr._inflight is True
    assert "the current plan predates Dawnwing Marshal is now 4/4" in caplog.text
    # Nothing new in the in-flight snapshot: a fresh reform still yields.
    mgr._inflight_basis = board_basis(before)
    assert mgr.yield_for_decision() is True
    assert cancel.is_set() and mgr._inflight is False


def _lyra_boards():
    """f9e0ec3b: the opponent's T9 before and after Lyra Dawnbringer; our T10."""
    ours = [card(368, "Emberheart Challenger", US, turn_entered_battlefield=4),
            card(387, "Hired Claw", US, turn_entered_battlefield=8), *lands(400, 2, US)]  # fmt: skip
    hand = [card(220, "Lightning Strike", US), card(230, "Burst Lightning", US)]
    theirs = [
        card(374, "Inspiring Overseer", THEM, turn_entered_battlefield=7),
        *lands(500, 5, THEM, "Plains"),
    ]
    before = board(9, THEM, [*theirs, *ours], hand, life={US: 20, THEM: 14})
    lyra = card(390, "Lyra Dawnbringer", THEM, turn_entered_battlefield=9)
    after = board(9, THEM, [*theirs, lyra, *ours], hand, life={US: 20, THEM: 14})
    ours_now = board(10, US, [*theirs, lyra, *ours], hand, life={US: 17, THEM: 18})
    return before, after, ours_now


T10_PLAN = plan_json(
    "T10 Strike kills Overseer then swing ~4",
    [{"turn": "T", "cast": ["Lightning Strike"], "attack": "Emberheart Challenger, Hired Claw",
      "hold": "Strike must kill the flying blocker pre-combat"}],
)  # fmt: skip


def _with_prior_plan(mgr: GamePlanManager, state: dict, turn: int) -> None:
    mgr.observe(state)
    mgr._plan = GamePlan(win_conditions=["x"], path="y", turn_formed=turn, basis=board_basis(state))
    mgr._last_sig, mgr._last_reform_turn = mgr._signature(state), turn
    mgr._last_key = mgr._strategic_key(state)
    mgr._seed = mgr._last_seed = None


def test_a_threat_on_their_turn_supersedes_the_inflight_reform(monkeypatch, plan_threads, caplog):
    now = [100.0]
    monkeypatch.setattr("arenamcp.game_plan.time.monotonic", lambda: now[0])
    before, after, _ours = _lyra_boards()
    gates = [threading.Event() for _ in range(3)]
    backend = ScriptedBackend([T10_PLAN] * 3, gates=gates)
    mgr = GamePlanManager(backend)
    earlier = deepcopy(before)
    earlier["turn"].update(turn_number=8, active_player=US, priority_player=US)
    _with_prior_plan(mgr, earlier, 8)
    try:
        assert mgr.request_reform(deepcopy(before))  # their T9 starts: plan our T10
        assert backend.entered[0].wait(timeout=2)
        now[0] += 7.0  # a51dea64: requested 10:23:44, Lyra resolved 10:23:51
        with caplog.at_level(logging.INFO, logger="arenamcp.game_plan"):
            assert mgr.request_reform(deepcopy(after))  # superseded and re-requested now
        assert backend.calls[0][1]["cancel_event"].is_set()
        assert (
            "Superseding the in-flight game plan for turn 10: the opponent's board changed after its "
            "snapshot (Lyra Dawnbringer 5/5 flying, first strike, lifelink entered)" in caplog.text
        )
        assert backend.entered[1].wait(timeout=2)
        assert "Lyra Dawnbringer" in backend.calls[1][0]
        # A burst (a51dea64: Valkyrie 10:22:39, Ajani 10:22:42): a reform that has
        # only just started is re-aimed at the newest board.
        now[0] += 3.0
        more = deepcopy(after)
        more["battlefield"].append(card(391, "Inspiring Overseer", THEM, turn_entered_battlefield=9))
        assert mgr.request_reform(deepcopy(more))
        assert backend.calls[1][1]["cancel_event"].is_set()
        assert backend.entered[2].wait(timeout=2)
        # Once it has run a while, a later threat the same turn leaves it to land.
        now[0] += mgr._YIELD_GRACE_S + 1.0
        last = deepcopy(more)
        last["battlefield"].append(card(392, "Inspiring Overseer", THEM, turn_entered_battlefield=9))
        assert not mgr.request_reform(last)
        assert not backend.calls[2][1]["cancel_event"].is_set()
    finally:
        for gate in gates:
            gate.set()
    for thread in plan_threads:
        thread.join(timeout=3)
    assert len(backend.calls) == 3
    assert mgr.current.turn_formed == 9
    assert [c["id"] for c in mgr.current.basis["creatures"]] == ["374", "390", "391"]
    assert mgr.plan_status(after)["last_outcome"] == "ok"


def test_no_supersede_on_our_turn_but_the_prompt_flags_the_late_plan(plan_threads, caplog):
    before, _after, ours_now = _lyra_boards()
    gate = threading.Event()
    backend = ScriptedBackend([T10_PLAN], gates=[gate])
    mgr = GamePlanManager(backend)
    earlier = deepcopy(before)
    earlier["turn"].update(turn_number=8, active_player=US, priority_player=US)
    _with_prior_plan(mgr, earlier, 8)
    try:
        assert mgr.request_reform(deepcopy(before))
        assert backend.entered[0].wait(timeout=2)
        # Our T10 started with Lyra on the board: the call is left to land.
        assert not mgr.request_reform(deepcopy(ours_now))
        assert not backend.calls[0][1]["cancel_event"].is_set()
        mgr.observe(deepcopy(ours_now))
        with caplog.at_level(logging.INFO, logger="arenamcp.game_plan"):
            gate.set()
            plan_threads[0].join(timeout=3)
    finally:
        gate.set()
    assert (
        "GamePlan for turn 10 predates changes on the opponent's board (Lyra Dawnbringer 5/5 flying, "
        "first strike, lifelink entered" in caplog.text
    )
    block = mgr.strategy_block(deepcopy(ours_now))
    assert "attack: Emberheart Challenger, Hired Claw" in block
    assert "written before the opponent's board changed" in block
    assert "PLAN PREDATES THE OPPONENT'S BOARD (game plan turn 9): Lyra Dawnbringer 5/5 flying" in block
    assert len(backend.calls) == 1


def test_a_threat_after_the_plan_landed_replans_once_on_their_turn(monkeypatch, plan_threads):
    now = [100.0]
    monkeypatch.setattr("arenamcp.game_plan.time.monotonic", lambda: now[0])
    before, after, ours_now = _lyra_boards()
    backend = ScriptedBackend([T10_PLAN, T10_PLAN, T10_PLAN])
    mgr = GamePlanManager(backend)
    earlier = deepcopy(before)
    earlier["turn"].update(turn_number=8, active_player=US, priority_player=US)
    _with_prior_plan(mgr, earlier, 8)
    # The role and lethal flags hold still: only the board-threat reason can fire.
    monkeypatch.setattr(mgr, "_strategic_key", lambda state, sig=None: mgr._last_key)
    assert mgr.request_reform(deepcopy(before))  # plan our next turn
    plan_threads[-1].join(timeout=3)
    assert len(backend.calls) == 1
    now[0] += mgr._REFRESH_INTERVAL_S
    assert mgr.request_reform(deepcopy(after))  # Lyra: "new opposing threat"
    plan_threads[-1].join(timeout=3)
    assert len(backend.calls) == 2 and "Lyra Dawnbringer" in backend.calls[1][0]
    now[0] += mgr._REFRESH_INTERVAL_S
    more = deepcopy(after)
    more["battlefield"].append(card(391, "Inspiring Overseer", THEM, turn_entered_battlefield=9))
    assert mgr._should_reform(mgr._signature(more), key=mgr._last_key, board=board_basis(more)) == (
        "new opposing threat"
    )
    assert not mgr.request_reform(more)  # one threat-driven reform per turn
    # On our turn a threat only marks the plan.
    mgr2 = GamePlanManager(ScriptedBackend([T10_PLAN]))
    _with_prior_plan(mgr2, deepcopy(before), 9)
    mgr2._last_reform_turn = 9
    assert mgr2._should_reform(mgr2._signature(ours_now), our_turn=True, key=mgr2._last_key,
                               board=board_basis(ours_now)) == "new opposing threat"  # fmt: skip
    assert not mgr2.request_reform(deepcopy(ours_now))
    assert len(backend.calls) == 2


def test_the_reform_prompt_says_what_the_prior_plan_predates():
    before, after = _marshal_boards()
    backend = ScriptedBackend([T7_PLAN])
    mgr = GamePlanManager(backend)
    mgr._plan = GamePlan(win_conditions=["x"], path="y", turn_formed=6, basis=board_basis(before))
    mgr._reform(deepcopy(after), 7)
    assert (
        "PLAN PREDATES THE OPPONENT'S BOARD (game plan turn 6): Dawnwing Marshal is now 4/4"
        in (backend.calls[0][0])
    )


# --- #23: keyword and blocking claims -----------------------------------------------------


def _moseo_board(turn: int = 7, active: int = THEM) -> dict:
    """a74bbc90 T7 (theirs): our Slickshot Show-Off against Moseo (flying) and friends."""
    battlefield = [
        card(303, "Slickshot Show-Off", US, turn_entered_battlefield=4), *lands(400, 4, US),
        card(347, "Moseo, Vein's New Dean", THEM, turn_entered_battlefield=7),
        card(280, "Hinterland Sanctifier", THEM, turn_entered_battlefield=3), *lands(500, 3, THEM, "Plains"),
    ]  # fmt: skip
    return board(turn, active, battlefield, [card(421, "Mountain", US)], life={US: 12, THEM: 15})


def _validated(state: dict, plan: dict) -> GamePlan:
    parsed = GamePlanManager._parse(json.dumps(plan), state["turn"]["turn_number"])
    return validate_plan(parsed, assess(state), state)


def test_a_false_shadow_and_cant_block_claim_is_rewritten():
    state = _moseo_board()
    plan = _validated(state, {
        "role": "race", "role_reason": "their lifegain outlasts a slow race",
        "win_conditions": ["Repeated pumped Slickshot flying beats plus drawn burn reach"],
        "path": "Swing Slickshot every turn; first drawn burn kills Moseo/Sanctifier",
        "turns": [
            {"turn": "T", "land": "Mountain",
             "attack": "Slickshot Show-Off (flying; their only blocker Moseo has shadow and cannot block",
             "hold": "Nothing castable - hand is all lands"},
            {"turn": "T+1", "attack": "Slickshot Show-Off (flying, unblockable - only flyer Moseo has shadow)"},
        ],
    })  # fmt: skip
    t8, t10 = plan.turn_plan[0], plan.turn_plan[1]
    assert t8["attack"] == (
        "Slickshot Show-Off (flying; their only blocker Moseo has no shadow [checked] and CAN block [checked]"
    )
    assert t10["attack"] == (
        "Slickshot Show-Off (flying, can be blocked by Moseo, Vein's New Dean [checked] - only flyer Moseo "
        "has no shadow [checked])"
    )
    assert "T: plan claim corrected: Moseo, Vein's New Dean has no shadow (it has flying)" in plan.issues
    assert "T: plan claim corrected: Moseo, Vein's New Dean can block Slickshot Show-Off" in plan.issues
    assert (
        "T+1: plan claim corrected: Slickshot Show-Off can be blocked by Moseo, Vein's New Dean"
        in plan.issues
    )
    # The true claims ("Slickshot ... flying", "flying beats") are untouched.
    assert plan.win_conditions == ["Repeated pumped Slickshot flying beats plus drawn burn reach"]


def test_true_conditional_and_unverifiable_claims_are_left_alone():
    state = _moseo_board()
    state["battlefield"] += [
        card(610, "Brute", THEM, turn_entered_battlefield=5),
        card(611, "Dawnwing Marshal", THEM, turn_entered_battlefield=5),
        card(612, "Unflinching Courage", THEM, attached_to_id=611, turn_entered_battlefield=6),
    ]
    texts = [
        "Slickshot Show-Off (flying) swings",
        "Brute can't block, so Slickshot Show-Off swings",  # its own text: true
        "Slickshot Show-Off attacks once Moseo is tapped and cannot block",  # a condition
        "Dawnwing Marshal has lifelink now",  # an Aura may grant it: not checked
        "Hinterland Sanctifier has no flying, so Slickshot Show-Off flies over",
        # Credited to an effect (as in 2,611 logged plans: Tetsuko, Rogue's Passage).
        "use Rogue's Passage to make Slickshot Show-Off unblockable",
        "attack: Slickshot Show-Off (unblockable under Tetsuko)",
        "Moseo attacks and we cannot block it",  # about us, not Moseo
    ]
    plan = _validated(state, {"role": "race", "role_reason": "their lifegain outlasts a slow race",
                              "win_conditions": ["burn"], "path": " | ".join(texts), "turns": []})  # fmt: skip
    assert plan.path == " | ".join(texts)
    assert not [issue for issue in plan.issues if "claim corrected" in issue]


# --- #31: mana holds with nothing to spend them on ------------------------------------------


def _chocobo_board() -> dict:
    """92c09c7a T7 (theirs): Slickshot and three Mountains; Effigy, Hired Claw and a Mountain in hand."""
    battlefield = [
        card(491, "Slickshot Show-Off", US, turn_entered_battlefield=6), *lands(400, 3, US),
        card(450, "Sazh's Chocobo", THEM, power=6, toughness=7, counters={"+1/+1": 4},
             turn_entered_battlefield=2),
        *lands(500, 6, THEM, "Plains"),
    ]  # fmt: skip
    hand = [card(561, "Fleeting Effigy", US), card(562, "Hired Claw", US), card(563, "Mountain", US)]
    return board(7, THEM, battlefield, hand, life={US: 16, THEM: 14})


CHOCOBO_PLAN = {
    "role": "race", "role_reason": "their clock is evasive; our blockers stop none of it",
    "win_conditions": ["Pumped Slickshot swings plus topdecked burn reach"], "path": "deploy and swing",
    "turns": [
        {"turn": "T", "land": "Mountain", "cast": ["Fleeting Effigy", "Hired Claw"],
         "attack": "Slickshot Show-Off, Fleeting Effigy",
         "hold": "{R}{R} spare - no instants in hand; if drawn Shock/Lightning Strike, cast pre-combat to pump "
                 "Slickshot"},
        {"turn": "T+1", "cast": [], "attack": "Slickshot Show-Off, Fleeting Effigy, Hired Claw",
         "hold": "{R}{R} spare - bank toward kicked Burst Lightning if drawn; else grow Hired Claw with its "
                 "counter after the Lizard ping"},
    ],
}  # fmt: skip


def test_a_mana_hold_with_nothing_to_spend_it_on_becomes_spend_it():
    state = _chocobo_board()
    plan = _validated(state, deepcopy(CHOCOBO_PLAN))
    t8, t10 = plan.turn_plan[0], plan.turn_plan[1]
    assert t8["cast"] == ["Fleeting Effigy", "Hired Claw"] and t8["mana"] == 4
    assert t8["hold"] == (
        "if drawn Shock/Lightning Strike, cast pre-combat to pump Slickshot; spend spare mana this turn "
        "(Hired Claw): unused mana empties at the end of each step and phase"
    )
    assert t10["hold"].startswith(
        "else grow Hired Claw with its counter after the Lizard ping; spend spare mana"
    )
    assert any(
        issue.startswith(
            "T: dropped the mana hold '{R}{R} spare - no instants in hand' (nothing in hand or on"
        )
        for issue in plan.issues
    ), plan.issues
    assert any("(mana can't be banked;" in issue for issue in plan.issues)
    # The decision prompt on our T8 carries it in THIS TURN.
    t8_state = deepcopy(state)
    t8_state["turn"].update(turn_number=8, active_player=US, priority_player=US)
    block = compose_strategy_block(assess(t8_state), plan, state=t8_state)
    this_turn = next(line for line in block.splitlines() if line.startswith("  THIS TURN"))
    assert "unused mana empties at the end of each step and phase [game plan T7]" in this_turn


def test_a_hold_for_an_instant_in_hand_or_a_future_draw_is_kept():
    state = _chocobo_board()
    state["hand"].append(card(564, "Burst Lightning", US))
    plan_data = deepcopy(CHOCOBO_PLAN)
    plan_data["turns"][0]["hold"] = "{R} open for Burst Lightning on their attacker"
    plan_data["turns"][1]["hold"] = "{R} open for Shock if drawn"
    plan = _validated(state, plan_data)
    assert plan.turn_plan[0]["hold"] == "{R} open for Burst Lightning on their attacker"
    assert plan.turn_plan[1]["hold"] == "{R} open for Shock if drawn"
    assert not [issue for issue in plan.issues if "mana hold" in issue]
    # Statements, a named spend ({1}{R} for Hired Claw's counter) and stored mana are not holds to drop.
    plan_data = deepcopy(CHOCOBO_PLAN)
    plan_data["turns"][0]["hold"] = "no spare mana this turn; {1}{R} reserved for Hired Claw's counter"
    plan_data["turns"][1]["hold"] = "bank Treasure tokens for a bigger turn"
    plan = _validated(_chocobo_board(), plan_data)
    assert plan.turn_plan[0]["hold"] == "no spare mana this turn; {1}{R} reserved for Hired Claw's counter"
    assert plan.turn_plan[1]["hold"] == "bank Treasure tokens for a bigger turn"
    # Formed on our own turn, "if drawn" no longer applies: the draw has happened.
    ours = _chocobo_board()
    ours["turn"].update(turn_number=8, active_player=US, priority_player=US)
    plan_data = deepcopy(CHOCOBO_PLAN)
    plan_data["turns"] = [dict(plan_data["turns"][0], hold="keep {R} open for any drawn instant")]
    plan = _validated(ours, plan_data)
    assert "keep {R} open" not in plan.turn_plan[0]["hold"]


# --- #49: a creature that returns to hand at our end step ------------------------------------


def _effigy_board(turn: int, active: int, *, ours_on_board: list[dict], hand: list[dict]) -> dict:
    """10a78106: Fleeting Effigy and Hired Claw against Bristly Bill (mono-G landfall)."""
    battlefield = [*ours_on_board, card(620, "Bristly Bill, Spine Sower", THEM, power=4, toughness=4,
                                        counters={"+1/+1": 2}, turn_entered_battlefield=3),
                   *lands(500, 4, THEM, "Plains")]  # fmt: skip
    return board(turn, active, battlefield, hand, life={US: 20, THEM: 7})


def test_an_end_step_leaver_is_recast_every_turn_of_the_plan():
    # 10:30:48 (their T6): "T+1: dropped Fleeting Effigy (already cast earlier in the plan)" x2.
    state = _effigy_board(6, THEM, ours_on_board=[card(562, "Hired Claw", US, turn_entered_battlefield=3),
                                                  *lands(400, 3, US)],
                          hand=[card(561, "Fleeting Effigy", US), card(563, "Mountain", US)])  # fmt: skip
    plan = _validated(state, {
        "role": "aggressor", "role_reason": "burn race", "win_conditions": ["Recurring Effigy swings"],
        "path": "recast Effigy each turn",
        "turns": [
            {"turn": "T", "land": "Mountain", "cast": ["Fleeting Effigy"], "attack": "Fleeting Effigy + Hired Claw"},
            {"turn": "T+1", "cast": ["Fleeting Effigy"], "attack": "Fleeting Effigy + Hired Claw"},
            {"turn": "T+2", "cast": ["Fleeting Effigy", "Fleeting Effigy"], "attack": "Fleeting Effigy"},
        ],
    })  # fmt: skip
    assert [step["cast"] for step in plan.turn_plan] == [["Fleeting Effigy"]] * 3
    assert not [issue for issue in plan.issues if "earlier in the plan" in issue]
    assert "T+2: dropped Fleeting Effigy (already cast this turn)" in plan.issues
    assert [step["attack"] for step in plan.turn_plan[:2]] == ["Fleeting Effigy + Hired Claw"] * 2


def test_attacking_with_a_leaver_needs_its_recast_that_turn():
    # 10a78106 T3 (low-effort first plan): "cast Boltwave + Hired Claw; attack: Fleeting Effigy" on 2 lands.
    state = _effigy_board(1, US, ours_on_board=[],
                          hand=[card(561, "Fleeting Effigy", US), card(564, "Boltwave", US),
                                card(562, "Hired Claw", US), card(565, "Tectonic Hazard", US),
                                *[card(570 + k, "Mountain", US) for k in range(3)]])  # fmt: skip
    plan = _validated(state, {
        "role": "aggressor", "role_reason": "burn race", "win_conditions": ["chip"], "path": "curve out",
        "turns": [
            {"turn": "T", "land": "Mountain", "cast": ["Fleeting Effigy"], "attack": "Fleeting Effigy"},
            {"turn": "T+1", "land": "Mountain", "cast": ["Boltwave", "Hired Claw"], "attack": "Fleeting Effigy"},
            {"turn": "T+2", "land": "Mountain", "cast": ["Tectonic Hazard"],
             "attack": "Hired Claw, Fleeting Effigy"},
        ],
    })  # fmt: skip
    t1, t3, t5 = plan.turn_plan
    assert t1["cast"] == ["Fleeting Effigy"] and t1["attack"] == "Fleeting Effigy"
    assert t3["cast"] == ["Boltwave", "Hired Claw"] and t3["attack"] == "" and t3["mana"] == 2
    assert (
        "T+1: Fleeting Effigy can't attack: it returns to our hand at our end step and recasting it needs "
        "1 more mana (2 of 2 already planned)" in plan.issues
    )
    assert (
        t5["cast"] == ["Tectonic Hazard", "Fleeting Effigy"] and t5["attack"] == "Hired Claw, Fleeting Effigy"
    )
    assert (
        "T+2: casts Fleeting Effigy to attack with it (it returns to our hand at our end step)" in plan.issues
    )


def test_a_plan_cannot_block_with_a_leaver_on_their_turn():
    # 10:31:59 / 10:33:18 (their T8, T10): {2}{R} held "to pump-block/trade Bristly Bill in their combat".
    state = _effigy_board(8, THEM, ours_on_board=[card(562, "Hired Claw", US, turn_entered_battlefield=3),
                                                  *lands(400, 4, US)],
                          hand=[card(561, "Fleeting Effigy", US), card(563, "Mountain", US)])  # fmt: skip
    plan = _validated(state, {
        "role": "defender", "role_reason": "their clock is faster than ours",
        "win_conditions": ["Effigy chip damage plus topdecked burn reach"],
        "path": "Recast Effigy each turn, chip face; Fleeting Effigy blocks Bill when it attacks",
        "turns": [
            {"turn": "T", "land": "Mountain", "cast": ["Fleeting Effigy"], "attack": "Fleeting Effigy",
             "hold": "{2}{R} reserved to pump-block/trade Bristly Bill in their combat; spare mana for any drawn "
                     "instant burn"},
            {"turn": "T+1", "cast": ["Fleeting Effigy"], "attack": "none",
             "hold": "{4} open through their combat: {2}{R} pumps Effigy to trade with Bristly Bill if attacked "
                     "by it; spare {R} unused"},
        ],
    })  # fmt: skip
    t9, t11 = plan.turn_plan
    # T9 names no creature: Effigy's pump is no instant-speed use on their turn (it is in our hand).
    assert t9["hold"] == (
        "spare mana for any drawn instant burn; spend spare mana this turn (Hired Claw, Fleeting Effigy): "
        "unused mana empties at the end of each step and phase"
    )
    assert t11["hold"].startswith(
        "Fleeting Effigy can't block on their turn [checked: back in our hand at our end step]; "
        "spend spare mana this turn"
    )
    assert plan.path == (
        "Recast Effigy each turn, chip face; Fleeting Effigy blocks Bill when it attacks [checked: Fleeting "
        "Effigy returns to our hand at our end step; it can't block on their turn]"
    )
    assert any(
        issue.startswith(
            "T: dropped the mana hold '{2}{R} reserved to pump-block/trade Bristly Bill in their"
        )
        for issue in plan.issues
    ), plan.issues
    assert any(
        issue.startswith("T+1: dropped '{4} open through their combat: {2}{R} pumps Effigy to trade")
        and issue.endswith("(Fleeting Effigy returns to our hand at our end step)")
        for issue in plan.issues
    ), plan.issues
