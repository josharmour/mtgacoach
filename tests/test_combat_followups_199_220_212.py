"""Regression tests for ledger #199(a)-(c), #220's hasRestrictions residue and #212.

#199(a) (review 2026-10-20): ``board_assessment._combat_life`` returned the
lifelink gains mixed with the signed death-trigger life swings, and the line
search added the mixture to "life gained this turn" (#195) — a death drain
shrank a life-scaled token count. The two are separate components now.
#199(b)/(c) (concede): a double striker's beginning-of-combat +1/+1 counter is
+2 damage towards the cap, not +1; a counter trigger still on the stack in
Step_BeginCombat counts too.
#212 (match 51a59f12 T14, 18:35:49): on a 21-attacker board the combat solver
held everything while the line search's best line attacked (lethal on T16,
race ahead); the hold stood and we conceded on T17 having never attacked. A
big-board hold-all is now weighed by the line search.
"""

from __future__ import annotations

from copy import deepcopy

import pytest
from tests import race_boards_20261019 as rb
from tests import valkyrie_boards_20261019 as vb
from tests.race_boards_20261019 import card
from tests.test_attack_push_20261019 import TOKENS
from tests.threat_boards_20261019 import ME, OPP, harbin_t11

from arenamcp import board_assessment as ba
from arenamcp import combat_solver
from arenamcp.attack_potential import ExtraCombat, attack_round
from arenamcp.board_model import build_board_model
from arenamcp.combat_strategy import combat_choice
from arenamcp.concede import _counter_damage, _their_turn_engines
from arenamcp.line_search import _Search
from arenamcp.line_search_moves import combat_counter_growth

pytestmark = pytest.mark.usefixtures("unlimited_search_deadline")


def _body(iid: int, name: str, controller: int = rb.OPPONENT, **extra) -> dict:
    return ba._body(card(iid, name, controller, turn_entered_battlefield=1, **extra), 10, {})


# --- #199(a): lifelink gains separated from the death-trigger swings -------------------------


def test_combat_life_separates_lifelink_gains_from_death_swings():
    # A 3/3 lifelink attacker whose death drains its controller 2 into a 4/4 blocker:
    # it deals its damage (gaining 3) and dies. The net attacker gain mixes both
    # (+3 lifelink, -2 drain); the lifelink component is the 3 alone.
    att = {
        **_body(11, "Grizzly Bears", power=3, toughness=3),
        "keywords": ["lifelink"],
        "death_swing": [-2, 0],
    }
    blk = _body(12, "Hill Giant", power=4, toughness=4)
    damage, dead_att, dead_blk, net_gain, net_heal, ll_gain, ll_heal = ba._combat_life([att], [blk], 20)
    # The 3/3 attacker trades into the 4/4 blocker: it dies, the blocker lives.
    assert (damage, dead_att, dead_blk) == (0, {11}, set())
    assert (net_gain, ll_gain) == (1, 3)
    assert (net_heal, ll_heal) == (0, 0)
    # The three-value form is unchanged for callers that ignore life entirely.
    assert ba._combat([att], [blk], 20) == (0, {11}, set())


def test_lifelink_drain_is_the_lifelink_alone():
    att = {**_body(21, "Hill Giant"), "keywords": ["lifelink"]}
    # Unblocked: the drain is the attacker's lifelink power (Hill Giant is a 3/3).
    assert ba.lifelink_drain([att], [], 20) == 3


def test_attack_round_composes_the_separated_components():
    flier = {
        "instance_id": 1,
        "name": "Flier",
        "power": 2,
        "toughness": 2,
        "_atk_extra": ExtraCombat(1, "Aurelia", True),
    }

    def once(attackers, blockers, life):
        # Net gain mixes a death drain (-1); the lifelink part is one less.
        return (2, set(), set(), 5, -1, 4, 0) if life == 20 else (1, set(), set(), 3, -1, 3, 0)

    assert attack_round([flier], [], 20, once) == (3, set(), set(), 8, -2, 7, 0)


GERMINATE_TEXT = (
    "create x 2/2 colorless wizard soldier creature tokens named cadet, where x is the amount of life "
    "you gained this turn."
)


def _finish_all(source: dict):
    model = build_board_model(deepcopy(source))
    search = _Search(
        model, survival=False, lethal_now=False, hard_ms=1e9, opponent=None,
        groups=8, width=8, k_next=4,
    )  # fmt: skip
    search.free = True
    node = next(iter(dict(search.roots()).values()))
    _base, sources = search.sources(node, None)
    casts = next(c for c in search.cast_sets(node, sources, instant_only=False) if c)
    mid = search.begin(node, None, casts)
    ids = tuple(b["instance_id"] for b in node.ours)
    return search.finish(mid, ("all", ids))


def test_life_gained_this_turn_counts_lifelink_not_death_drains():
    # Our flying lifelink 4/4 gets through (gains 4); our 3/3 trades with their 3/3 and its
    # death drains us 2. The Germinate-style cast after combat scales with the lifelink alone:
    # four Cadets — the mixed net gain (2) used to make two.
    marshal = dict(
        card(11, "Dawnwing Marshal", rb.LOCAL),
        power=4,
        toughness=4,
        keywords=["flying", "lifelink"],
        oracle_text="Flying\nLifelink",
        text="Flying\nLifelink",
        sick=False,
        tapped=False,
        entered=1,
        turn_entered_battlefield=1,
    )
    trader = dict(
        card(12, "Grizzly Bears", rb.LOCAL),
        power=3,
        toughness=3,
        oracle_text="When this creature dies, you lose 2 life.",
        text="When this creature dies, you lose 2 life.",
        sick=False,
        tapped=False,
        entered=1,
        turn_entered_battlefield=1,
    )
    blocker = dict(
        card(13, "Grizzly Bears", rb.OPPONENT),
        power=3,
        toughness=3,
        sick=False,
        tapped=False,
        entered=1,
        turn_entered_battlefield=1,
    )
    source = rb.board(
        turn=10,
        life=20,
        their_life=20,
        ours=[
            (11, "Dawnwing Marshal", False, 1),
            (12, "Grizzly Bears", False, 1),
            (16, "Mountain", False, 1),
        ],
        theirs=[(13, "Grizzly Bears", False, 1)],
        hand=[(15, "Grizzly Bears")],
        phase="Phase_Combat",
    )
    source["battlefield"] = [marshal, trader, blocker] + [
        c for c in source["battlefield"] if c["instance_id"] in (16,)
    ]
    germinate = dict(
        card(15, "Lightning Strike", rb.LOCAL),
        name="Germinate Recruits",
        type_line="Instant",
        card_types=["CardType_Instant"],
        mana_cost="{R}",
        oracle_text=GERMINATE_TEXT.capitalize(),
        text=GERMINATE_TEXT,
    )
    source["hand"] = [germinate]
    finished = _finish_all(source)
    cadets = [b for b in finished.ours if b.get("name") == "Cadet"]
    assert len(cadets) == 4


# --- #199(b)/(c): the concede engine's beginning-of-combat counters --------------------------


def test_a_double_strikers_combat_counter_counts_twice_towards_the_cap():
    harbin = {"instance_id": 444, "name": "Harbin", "power": 6, "toughness": 5}
    assert _counter_damage({444: 1}, [harbin]) == 1
    assert _counter_damage({444: 1}, [{**harbin, "keywords": ["double strike"]}]) == 2
    # Two counters on a vanilla body are still +2 damage.
    assert _counter_damage({444: 2}, [harbin]) == 2


def test_beginning_of_combat_trigger_on_the_stack_counts_for_the_concede_cap():
    board = harbin_t11()
    veteran = next(c for c in board["battlefield"] if c["instance_id"] == 455)
    assert combat_counter_growth(veteran) == ((1, "creature", False),)

    def growth_at(phase: str, step: str) -> dict[int, int]:
        state = deepcopy(board)
        state["turn"].update(phase=phase, step=step)
        model = build_board_model(deepcopy(state))
        _drain, growth, notes = _their_turn_engines(
            state,
            ME,
            OPP,
            state["battlefield"],
            list(model.theirs),
            our_turn=False,
            declared=False,
        )
        return growth

    # Still in Step_BeginCombat: Siege Veteran's trigger is on the stack and grows them (#199(c)).
    stacked = growth_at("Phase_Combat", "Step_BeginCombat")
    assert stacked and sum(stacked.values()) == 1
    assert any(n == 1 for n in stacked.values())
    # By Step_DeclareAttack it has resolved into the snapshot: no further growth.
    assert growth_at("Phase_Combat", "Step_DeclareAttack") == {}


# --- #212: a big-board hold-all is weighed by the line search --------------------------------


def test_big_board_hold_all_is_weighed_by_the_line_search(monkeypatch):
    monkeypatch.setenv("ARENAMCP_ATTACK_PUSH", "on")
    combat_solver._MEMO.clear()
    state = vb.t12_declare_attackers()
    choice = combat_choice(state)
    assert choice is not None and choice.assignments
    assert set(choice.assignments) == set(TOKENS)
    from arenamcp.combat_targets import recipient_key

    local = next(p["seat_id"] for p in state["players"] if p.get("is_local"))
    assert all(
        recipient_key(r)[0] == "player" and recipient_key(r)[1] != local for r in choice.assignments.values()
    )
    assert "#212" in choice.explanation and "hold all" in choice.explanation


def test_shadow_keeps_the_solver_hold(monkeypatch):
    monkeypatch.setenv("ARENAMCP_ATTACK_PUSH", "shadow")
    combat_solver._MEMO.clear()
    state = vb.t12_declare_attackers()
    choice = combat_choice(state)
    assert choice is not None and choice.assignments == {}
    assert choice.explanation.startswith("hold all attackers")
