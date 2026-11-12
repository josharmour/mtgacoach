"""Programmatic plan upkeep — one plan per match (E5, 2026-11-12).

The plan is kept for the whole match and updated by code between reforms;
replanning happens only on a real change. Real logged cases:

* role flip: bundle 00651f77 turn 5 logs "Game plan reform (role flip)"
  (standalone.log:719295 shows the same trigger on 2026-10-24, turn 11). Role
  flips caused 218 of the 746 reforms in the 100-match corpus; E5 folds the
  new role into the kept plan instead of reforming — through the real
  request_reform path too, not just observe().
* commander change: bundle 034f2385 turn 7 logged two reforms in one turn for
  the commander coming down (standalone.log:393303, 2026-10-09 turn 7, same
  trigger). E5 records the event on the kept plan; the live command zone is
  rendered with every decision anyway.
* switch_if: standalone.log:1536881 carries the real clause
  "Synthesizer destroyed -> attack freely with World Breaker". E5 checks such
  clauses against the live board — a "destroyed" name must actually have left
  our battlefield before the clause fires (the first draft fired while the
  Synthesizer was still standing), and an opponent's copy of a named card
  never satisfies our clause.
* engine loss: destroying our own engine (the Synthesizer above) is exactly
  the switch_if case — one reform, from the kept plan's own prompt.
* win route: bundle 00651f77 kept "win with Etali's Favor" for six turns after
  the Favor had been exiled. E5 replans ("win route lost") once every route
  card is unusable: exile is inaccessible, the graveyard still recurs.
"""

from __future__ import annotations

import json

from arenamcp.game_plan import GamePlan, GamePlanManager, switch_if_triggered, win_route_lost


class FakeBackend:
    """Records calls and returns a scripted response per call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.requests = []

    def complete(self, system, user, max_tokens, **kwargs):
        self.calls += 1
        self.requests.append((system, user, kwargs))
        if self._responses:
            return self._responses.pop(0)
        return "{}"


def _plan_json(path="race for lethal ~T6", win="aggro beatdown", role="aggressor"):
    return json.dumps(
        {
            "role": role,
            "win_conditions": [win, "grind with card advantage"],
            "path": path,
            "threat": "opponent flyers",
            "develop_next": "deploy a 2-drop",
            "switch_if": [SWITCH_CLAUSE],
        }
    )


def _state(turn=3, synth_on_board=True):
    """Minimal live board: our Synthesizer engine and a chump blocker.

    ``synth_on_board=False`` models the engine having been destroyed: the
    Synthesizer sits in our graveyard and only World Breaker remains.
    """
    battlefield = [
        {"instance_id": 10, "name": "Synthesizer", "type_line": "Artifact", "controller_seat_id": 1},
        {"instance_id": 11, "name": "World Breaker", "type_line": "Creature", "controller_seat_id": 1},
    ]
    graveyard = []
    if not synth_on_board:
        battlefield = battlefield[1:]
        graveyard = [{"instance_id": 10, "name": "Synthesizer"}]
    return {
        "players": [
            {"is_local": True, "seat_id": 1, "life_total": 20, "hand_size": 5},
            {"is_local": False, "seat_id": 2, "life_total": 20, "hand_size": 5},
        ],
        "turn": {"turn_number": turn, "active_player": 1, "phase": "Main1"},
        "battlefield": battlefield,
        "graveyard": graveyard,
        "hand": [],
        "exile": [],
        "command": [],
    }


def _key(role="aggressor", cmd_ids=(), cmd_casts=()):
    """A strategic key in _strategic_key shape (facts, cmd zone, engines)."""
    return ((role, False, False, False), cmd_ids, cmd_casts, frozenset())


def _formed_manager(monkeypatch, key=None):
    """A manager holding a freshly formed plan (one model call spent)."""
    be = FakeBackend([_plan_json()])
    mgr = GamePlanManager(be)
    monkeypatch.setattr(mgr, "_strategic_key", lambda s, sig=None: key or _key())
    mgr.maybe_reform(_state(turn=3))
    assert be.calls == 1
    assert mgr.current is not None
    mgr.observe(_state(turn=3))  # seeds the upkeep baseline for the formed plan
    return be, mgr


def test_role_flip_updates_the_kept_plan_without_a_reform(monkeypatch):
    """00651f77 T5 (and 218 corpus-wide): a role flip used to cost a reform.

    E5: observe() folds the new role into the kept plan — no model call.
    """
    be, mgr = _formed_manager(monkeypatch)
    monkeypatch.setattr(mgr, "_strategic_key", lambda s, sig=None: _key(role="defender"))
    mgr.observe(_state(turn=4))
    assert be.calls == 1
    assert mgr.current.role == "defender"
    assert any("role defender <- aggressor" in update for update in mgr.current.updates)


def test_role_flip_through_request_reform_costs_no_model_call(monkeypatch):
    """The real decision-loop path: request_reform() observes first (upkeep
    folds the role), then _should_reform must not name a reason for a pure
    role flip — no background model call is scheduled."""
    be, mgr = _formed_manager(monkeypatch)
    monkeypatch.setattr(mgr, "_strategic_key", lambda s, sig=None: _key(role="defender"))
    mgr._last_attempt_at = None  # skip the post-formation cooldown so the reason check is reached
    requested = mgr.request_reform(_state(turn=4))
    assert requested is False
    assert be.calls == 1
    assert mgr.current.role == "defender"
    assert any("role defender <- aggressor" in update for update in mgr.current.updates)


def test_commander_entering_the_battlefield_is_recorded_not_replanned(monkeypatch):
    """034f2385 T7: the commander coming down caused two reforms in one turn.

    E5: the strategy survives; the event is recorded on the kept plan — and
    the real maybe_reform path agrees (no model call).
    """
    be, mgr = _formed_manager(monkeypatch)
    monkeypatch.setattr(mgr, "_strategic_key", lambda s, sig=None: _key(cmd_ids=(77,), cmd_casts=(1,)))
    mgr.observe(_state(turn=4))
    assert be.calls == 1
    assert any("commander entered the battlefield; strategy kept" in update for update in mgr.current.updates)
    mgr.maybe_reform(_state(turn=4))
    assert be.calls == 1


SWITCH_CLAUSE = "Synthesizer destroyed -> attack freely with World Breaker"


def test_switch_if_destroyed_clause_fires_only_after_the_destruction():
    """standalone.log:1536881: 'Synthesizer destroyed -> attack freely with
    World Breaker' rode unread for whole matches. The clause fires once the
    Synthesizer has actually left our battlefield — not while it still stands
    (the first draft's fixture had it alive and fired anyway) — and never off
    an opponent's copy."""
    plan = GamePlan(win_conditions=["aggro beatdown"], path="race for lethal ~T6", switch_if=[SWITCH_CLAUSE])
    basis = {"creatures": [], "local_seat_id": 1}
    # Engine still standing: nothing has happened yet.
    assert switch_if_triggered(plan, _state(), basis=basis) == ""
    # Engine destroyed (graveyard), World Breaker still ours: fires.
    assert switch_if_triggered(plan, _state(synth_on_board=False), basis=basis) == SWITCH_CLAUSE
    # Safety assertions preserved: World Breaker gone too -> no reckless attack.
    gone = _state(synth_on_board=False)
    gone["battlefield"] = []
    gone["graveyard"] = [
        {"instance_id": 10, "name": "Synthesizer"},
        {"instance_id": 11, "name": "World Breaker"},
    ]
    assert switch_if_triggered(plan, gone, basis=basis) == ""
    assert switch_if_triggered(GamePlan(win_conditions=["x"]), _state(), basis=basis) == ""


def test_opponents_copy_never_satisfies_our_clause_or_route():
    """E5a ownership fix: an opponent-controlled permanent with a name our
    plan mentions is not ours to use."""
    plan = GamePlan(win_conditions=["aggro beatdown"], path="race for lethal ~T6", switch_if=[SWITCH_CLAUSE])
    basis = {"creatures": [], "local_seat_id": 1}
    theirs = _state(synth_on_board=False)
    theirs["battlefield"] = [
        {"instance_id": 90, "name": "World Breaker", "type_line": "Creature", "controller_seat_id": 2}
    ]
    # Their World Breaker does not satisfy OUR clause.
    assert switch_if_triggered(plan, theirs, basis=basis) == ""
    # Their copy does not keep OUR route alive once ours is unreachable.
    route = GamePlan(win_conditions=["aggro beatdown"], path="race for lethal ~T6")
    route.route_cards = ["world breaker"]
    assert win_route_lost(route, theirs)


def test_a_met_switch_if_clause_replans_the_kept_plan(monkeypatch):
    """E5: a satisfied switch_if clause is a real change — it reforms once,
    from the kept plan's own prompt."""
    be = FakeBackend([])
    mgr = GamePlanManager(be)
    mgr._plan = GamePlan(win_conditions=["aggro beatdown"], path="race for lethal ~T6", switch_if=[SWITCH_CLAUSE])
    mgr._last_sig = (3,)
    mgr._last_reform_turn = 3
    key = _key()
    mgr._last_key = key
    assert mgr._should_reform((4,), key=key, state=_state(synth_on_board=False)) == "switch_if met"


def test_engine_loss_through_maybe_reform_consumes_exactly_one_model_call(monkeypatch):
    """Engine availability/loss through the real cadence path: destroying our
    Synthesizer trips its own switch_if clause and reforms exactly once, from
    the kept plan's prompt; with the engine intact nothing happens."""
    be = FakeBackend(
        [
            _plan_json(),
            _plan_json(path="attack freely with World Breaker", win="swing with World Breaker"),
        ]
    )
    mgr = GamePlanManager(be)
    monkeypatch.setattr(mgr, "_strategic_key", lambda s, sig=None: _key())
    mgr.maybe_reform(_state(turn=3))
    assert be.calls == 1
    mgr.observe(_state(turn=3))
    # Engine intact on a later turn: no reform.
    mgr.maybe_reform(_state(turn=4))
    assert be.calls == 1
    # Engine destroyed: exactly one reform carrying the kept plan forward.
    mgr.maybe_reform(_state(turn=5, synth_on_board=False))
    assert be.calls == 2
    reform_user = be.requests[1][1]
    assert "race for lethal ~T6" in reform_user  # replanned FROM the kept plan
    assert mgr.current.path == "attack freely with World Breaker"


def test_win_route_lost_exile_is_inaccessible_graveyard_still_recurs():
    """00651f77: 'win with Etali's Favor' stayed for six turns after the Favor
    was exiled. Exile is inaccessible -> route lost; the graveyard recurs ->
    route alive; hand obviously alive."""
    zones = {
        "players": [{"is_local": True, "seat_id": 1, "life_total": 20}],
        "turn": {"turn_number": 9, "active_player": 1, "phase": "Main1"},
        "battlefield": [],
        "hand": [],
        "graveyard": [],
        "exile": [],
        "command": [],
    }
    plan = GamePlan(win_conditions=["win with Etali's Favor"], path="cast it and swing")
    plan.route_cards = ["etali's favor"]
    assert win_route_lost(plan, zones)  # nowhere we can reach it
    grave = dict(zones)
    grave["graveyard"] = [{"instance_id": 5, "name": "Etali's Favor"}]
    assert not win_route_lost(plan, grave)  # usable recursion keeps the route
    hand = dict(zones)
    hand["hand"] = [{"instance_id": 5, "name": "Etali's Favor"}]
    assert not win_route_lost(plan, hand)
    exiled = dict(zones)
    exiled["exile"] = [{"instance_id": 5, "name": "Etali's Favor"}]
    assert win_route_lost(plan, exiled)  # THE regression: exile is not usable
    # No route cards recorded -> nothing to lose.
    assert not win_route_lost(GamePlan(win_conditions=["deck them"]), zones)


def test_upkeep_survives_a_hostile_state_and_keeps_the_plan(monkeypatch):
    """Advisory by design: a malformed snapshot must never discard the plan."""
    be, mgr = _formed_manager(monkeypatch)
    broken = {"turn": {"turn_number": 5}, "players": None, "battlefield": None}
    mgr.observe(broken)
    assert be.calls == 1
    assert mgr.current.path == "race for lethal ~T6"


def test_reset_clears_the_upkeep_baseline(monkeypatch):
    """A new match must start upkeep clean: no stale baseline key and no stale
    update entries leaking into the next match's plan."""
    be, mgr = _formed_manager(monkeypatch)
    monkeypatch.setattr(mgr, "_strategic_key", lambda s, sig=None: _key(role="defender"))
    mgr.observe(_state(turn=4))  # match-1 role fold lands in updates
    assert any("role defender" in u for u in mgr.current.updates)
    mgr.reset()
    assert mgr.current is None
    # Fresh match: form again and confirm updates start empty on an unchanged key.
    monkeypatch.setattr(mgr, "_strategic_key", lambda s, sig=None: _key())
    be._responses.append(_plan_json(role="aggressor"))
    mgr.maybe_reform(_state(turn=2))
    assert be.calls == 2
    baseline = len(mgr.current.updates)
    mgr.observe(_state(turn=2))
    mgr.observe(_state(turn=3))
    assert len(mgr.current.updates) == baseline
    assert not any("role defender" in u for u in mgr.current.updates)


def test_updates_and_route_cards_persist_across_a_reload(monkeypatch):
    """Reset/persistence contract: export_for_reload()/restore_after_reload()
    round-trips upkeep state (updates + route_cards) without a model call."""
    be, mgr = _formed_manager(monkeypatch)
    monkeypatch.setattr(mgr, "_strategic_key", lambda s, sig=None: _key(role="defender"))
    mgr.observe(_state(turn=4))
    saved = mgr.export_for_reload()
    assert saved["plan"]["updates"], "upkeep updates must be part of the saved plan"
    assert saved["plan"]["route_cards"] is not None

    reborn_backend = FakeBackend([])
    reborn = GamePlanManager(reborn_backend)
    reborn.restore_after_reload(saved, _state(turn=5))
    assert reborn_backend.calls == 0  # restored without a model call
    assert reborn.current.role == "defender"
    assert reborn.current.updates == mgr.current.updates
    assert reborn.current.route_cards == mgr.current.route_cards
