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
  card is unusable: exile is inaccessible, and the graveyard keeps the route
  only when recursion is actually usable (the card's own rules text recurs it,
  or the plan names the recursion line) — not for any graveyard card.
"""

from __future__ import annotations

import json

from arenamcp.game_plan import (
    GamePlan,
    GamePlanManager,
    _route_cards_from_state,
    compute_upkeep,
    switch_if_triggered,
    win_route_lost,
)
from arenamcp.plan_ledger import PlanLedger


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
    mgr._plan = GamePlan(
        win_conditions=["aggro beatdown"], path="race for lethal ~T6", switch_if=[SWITCH_CLAUSE]
    )
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
    assert win_route_lost(plan, grave)  # in the graveyard with NO usable recursion: lost
    retrace = dict(zones)
    retrace["graveyard"] = [
        {
            "instance_id": 5,
            "name": "Etali's Favor",
            "oracle_text": "Retrace — You may cast this card from your graveyard by discarding a land card.",
        }
    ]
    assert not win_route_lost(plan, retrace)  # usable recursion keeps the route
    plan_named = GamePlan(
        win_conditions=["win with Etali's Favor"],
        path="cast it and swing",
        active_mechanisms=["return Etali's Favor from the graveyard with backup"],
    )
    assert not win_route_lost(plan_named, grave)  # plan-stated recursion line keeps it
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
    assert reborn.current.route_state == mgr.current.route_state
    assert reborn.current.engines == mgr.current.engines
    assert reborn.current.next_steps == mgr.current.next_steps


REAL_PLAYBOOK_WIN = (
    "Convert counters into damage via trample from Etali's Favor (87300) or "
    "Meteoric Mace (91109), clearing blockers with Daring Discovery (87291)"
)


def test_real_extraction_from_the_live_playbook_sentence():
    """packet_20261110_001105: the real playbook sentence names Etali's Favor
    mid-sentence. Extraction must find it from live board visibility alone —
    no manually seeded route_cards — or the route tracking is blind exactly
    when it mattered (the Favor was on the battlefield and the tracker saw
    nothing)."""
    plan = GamePlan(win_conditions=[REAL_PLAYBOOK_WIN], path="stack counters, swing")
    state = _state(turn=9)
    state["battlefield"].append(
        {"instance_id": 30, "name": "Etali's Favor", "type_line": "Enchantment", "controller_seat_id": 1}
    )
    routes = _route_cards_from_state(plan, state)
    assert "etali's favor" in routes
    # A second named piece sitting in hand is extracted too.
    state["hand"] = [{"instance_id": 31, "name": "Meteoric Mace", "type_line": "Instant"}]
    routes = _route_cards_from_state(plan, state)
    assert "etali's favor" in routes
    assert "meteoric mace" in routes


def test_structured_upkeep_tracks_route_engines_and_next_steps(monkeypatch):
    """The structured upkeep state (E5a): observe() recomputes where each route
    piece sits, which committed engines are in play or lost, and the upcoming
    steps — with zero model calls — and flips honestly when the engine dies."""
    be, mgr = _formed_manager(monkeypatch)
    mgr.current.route_cards = ["synthesizer"]
    mgr.current.turn_plan = [{"turn": 4, "cast": ["World Breaker"], "attack": [], "hold": ""}]
    mgr.observe(_state(turn=4))
    assert be.calls == 1  # upkeep is code-owned: no model call
    route_state = mgr.current.route_state
    assert route_state["status"] == "live"
    assert route_state["pieces"]["synthesizer"] == "battlefield"
    # Route card plus the planned cast are both committed engines.
    assert sorted(mgr.current.engines["in_play"]) == ["synthesizer", "world breaker"]
    assert any("T4" in step for step in mgr.current.next_steps)
    # Engine destroyed: the snapshot flips without any model call.
    mgr.observe(_state(turn=5, synth_on_board=False))
    assert be.calls == 1
    assert mgr.current.route_state["status"] == "lost"
    assert mgr.current.route_state["pieces"]["synthesizer"] == "graveyard"
    assert mgr.current.engines["in_play"] == ["world breaker"]
    assert mgr.current.engines["lost"] == ["synthesizer (graveyard)"]


def test_missing_assessment_never_becomes_a_literal_none_role(monkeypatch):
    """A failed board assessment used to surface as role 'none' and overwrite a
    valid aggressor role. The guard keeps the valid role and logs nothing."""
    be, mgr = _formed_manager(monkeypatch)
    mgr.current.role = "aggressor"  # the plan holds a valid assessed role
    monkeypatch.setattr(
        mgr, "_strategic_key", lambda s, sig=None: ((None, False, False, False), (), (), frozenset())
    )
    mgr.observe(_state(turn=4))
    assert be.calls == 1
    assert mgr.current.role == "aggressor"
    assert not any("none" in update for update in mgr.current.updates)


def test_ledger_carries_the_upkeep_snapshot():
    """The per-turn ledger shows what the plan still had to work with when the
    turn began: live route + piece zones, lost engines, upcoming steps."""
    plan = GamePlan(win_conditions=["aggro beatdown"], path="race for lethal ~T6")
    plan.route_cards = ["synthesizer"]
    plan.turn_plan = [{"turn": 4, "cast": ["World Breaker"], "attack": [], "hold": ""}]
    snapshot = compute_upkeep(plan, _state(turn=4))
    plan.route_state = snapshot["route_state"]
    plan.engines = snapshot["engines"]
    plan.next_steps = snapshot["next_steps"]

    be = FakeBackend([])
    mgr = GamePlanManager(be)
    mgr._plan = plan
    note = mgr._upkeep_ledger_note()
    assert "route live" in note
    assert "synthesizer battlefield" in note
    assert "T4" in note

    ledger = PlanLedger()
    ledger.turn_started(
        4,
        ours=True,
        snapshot={"life": 20},
        role="aggressor",
        upkeep=note,
        decisions_for_turn=lambda turn: [],
    )
    rendered = ledger.entries[-1].line()
    assert "upkeep:" in rendered
    assert "route live" in rendered


def test_negated_or_nonreturning_recursion_does_not_keep_a_graveyard_route():
    """f39b158 gap 1: recursion evidence must be currently usable and
    card-specific. A plan line that NEGATES recursion ("Recursion is
    unavailable"), a recursion mention about a DIFFERENT card, and exiling-only
    mechanics (scavenge, haunt — the card never comes back) all leave the
    graveyard-only route lost; a real self-return mechanic keeps it."""
    zones = {
        "players": [{"is_local": True, "seat_id": 1, "life_total": 20}],
        "turn": {"turn_number": 9, "active_player": 1, "phase": "Main1"},
        "battlefield": [],
        "hand": [],
        "graveyard": [{"instance_id": 5, "name": "Etali's Favor"}],
        "exile": [],
        "command": [],
    }
    plan = GamePlan(win_conditions=["win with Etali's Favor"], path="cast it and swing")
    plan.route_cards = ["etali's favor"]

    def _routed(**parts):
        variant = GamePlan(win_conditions=["win with Etali's Favor"], path="cast it and swing", **parts)
        variant.route_cards = ["etali's favor"]
        return variant

    negated = _routed(active_mechanisms=["Recursion is unavailable"])
    assert win_route_lost(negated, zones)  # a negated line is not evidence

    unrelated = _routed(develop_next="recur the World Breaker later")
    assert win_route_lost(unrelated, zones)  # recursion about ANOTHER card is not evidence

    scavenge = dict(zones)
    scavenge["graveyard"] = [
        {
            "instance_id": 5,
            "name": "Etali's Favor",
            "oracle_text": (
                "Scavenge {2}{G} ({2}{G}, Exile this card from your graveyard: "
                "Put a number of +1/+1 counters equal to its power on target creature.)"
            ),
        }
    ]
    assert win_route_lost(plan, scavenge)  # scavenge exiles the card; it never returns

    haunt = dict(zones)
    haunt["graveyard"] = [
        {
            "instance_id": 5,
            "name": "Etali's Favor",
            "oracle_text": "Haunt (When this creature dies, exile it haunting target creature.)",
        }
    ]
    assert win_route_lost(plan, haunt)  # haunt exiles too

    undying = dict(zones)
    undying["graveyard"] = [
        {
            "instance_id": 5,
            "name": "Etali's Favor",
            "oracle_text": (
                "Undying (When this creature dies, return it to the battlefield "
                "under its owner's control with a +1/+1 counter on it.)"
            ),
        }
    ]
    assert not win_route_lost(plan, undying)  # a real self-return keeps the route


def test_recursion_evidence_must_be_positive_and_share_the_route_cards_clause():
    """0f92eb0 review gap: card-specific POSITIVE recursion evidence must live
    in the SAME actual clause that names the route card.

    'Etali's Favor is lost. Recur World Breaker instead.' — the recursion word
    belongs to the NEXT sentence and to ANOTHER card, so it must not revive
    the graveyard-only Favor route. 'Etali's Favor recursion is not
    available.' — the recursion mention is negated ("is not available"), so
    the route stays lost rather than being read as promised. A clause that
    names the card AND positively promises its return still keeps the route.
    """
    zones = {
        "players": [{"is_local": True, "seat_id": 1, "life_total": 20}],
        "turn": {"turn_number": 9, "active_player": 1, "phase": "Main1"},
        "battlefield": [],
        "hand": [],
        "graveyard": [{"instance_id": 5, "name": "Etali's Favor"}],
        "exile": [],
        "command": [],
    }

    def _routed(wins=None, **parts):
        variant = GamePlan(
            win_conditions=wins or ["win with Etali's Favor"], path="cast it and swing", **parts
        )
        variant.route_cards = ["etali's favor"]
        return variant

    displaced = _routed(develop_next="Etali's Favor is lost. Recur World Breaker instead.")
    assert win_route_lost(displaced, zones)  # next-sentence recursion about ANOTHER card

    negated_available = _routed(active_mechanisms=["Etali's Favor recursion is not available."])
    assert win_route_lost(negated_available, zones)  # "is not available" is a negation

    no_longer = _routed(active_mechanisms=["Etali's Favor recursion is no longer possible."])
    assert win_route_lost(no_longer, zones)  # "no longer possible" is a negation too

    same_clause = _routed(active_mechanisms=["Recur Etali's Favor from the graveyard next turn."])
    assert not win_route_lost(same_clause, zones)  # positive same-clause promise survives

    shared_sentence = _routed(
        wins=["Set up the yard. Recur Etali's Favor with its retrace cost, then swing."]
    )
    assert not win_route_lost(shared_sentence, zones)  # name + recursion in ONE sentence of a part


def test_contrastive_recursion_must_bind_to_the_route_card_itself():
    """b3e70c1 review gap: a recursion ACTION must take the route card as its
    object (or the card must be the subject coming back). A contrastive tail
    detaches the name from the action — 'Recur World Breaker instead of
    Etali's Favor.' rejects the Favor, it does not promise its return — and an
    ambiguous clause that binds neither way stays lost/unknown. No negation
    blacklist growth: contrast splitting and positive binding do the work."""
    zones = {
        "players": [{"is_local": True, "seat_id": 1, "life_total": 20}],
        "turn": {"turn_number": 9, "active_player": 1, "phase": "Main1"},
        "battlefield": [],
        "hand": [],
        "graveyard": [{"instance_id": 5, "name": "Etali's Favor"}],
        "exile": [],
        "command": [],
    }

    def _routed(wins=None, **parts):
        variant = GamePlan(
            win_conditions=wins or ["win with Etali's Favor"], path="cast it and swing", **parts
        )
        variant.route_cards = ["etali's favor"]
        return variant

    rejected_tail = _routed(develop_next="Recur World Breaker instead of Etali's Favor.")
    assert win_route_lost(rejected_tail, zones)  # the Favor is what was rejected

    rather_than = _routed(develop_next="Cast World Breaker rather than recur Etali's Favor.")
    assert win_route_lost(rather_than, zones)  # same detachment, other marker

    ambiguous = _routed(develop_next="Etali's Favor came up while discussing recursion options.")
    assert win_route_lost(ambiguous, zones)  # binds neither way: stays lost

    # The mirror image proves this is binding, not a blanket contrast ban:
    # when the ACTION targets the route card, a contrastive tail about another
    # card must not kill the route.
    positive_contrast = _routed(develop_next="Recur Etali's Favor instead of casting World Breaker.")
    assert not win_route_lost(positive_contrast, zones)

    subject_form = _routed(active_mechanisms=["Etali's Favor returns from the graveyard each end step."])
    assert not win_route_lost(subject_form, zones)  # card-as-subject return binds


def test_observe_records_a_contrastive_recursion_route_as_lost_without_a_model_call(monkeypatch):
    """Real observation path for b3e70c1: when the plan redirects recursion to
    another card ('Recur World Breaker instead of Synthesizer.'), observe()
    must record the graveyard-only route as lost on the code-owned upkeep
    refresh — zero model calls."""
    be, mgr = _formed_manager(monkeypatch)
    mgr.current.route_cards = ["synthesizer"]
    mgr.current.active_mechanisms = ["Recur World Breaker instead of Synthesizer."]
    mgr.observe(_state(turn=5, synth_on_board=False))
    assert be.calls == 1  # upkeep stays code-owned
    assert mgr.current.route_state["status"] == "lost"
    assert mgr.current.route_state["pieces"]["synthesizer"] == "graveyard"


def test_observe_records_a_negated_recursion_route_as_lost_without_a_model_call(monkeypatch):
    """Real observation path for the same gap: when the plan itself says the
    route card's recursion is not available, observe() must record the route
    as lost (not live) on the code-owned upkeep refresh — zero model calls."""
    be, mgr = _formed_manager(monkeypatch)
    mgr.current.route_cards = ["synthesizer"]
    mgr.current.active_mechanisms = ["Synthesizer recursion is not available."]
    mgr.observe(_state(turn=5, synth_on_board=False))
    assert be.calls == 1  # upkeep stays code-owned
    assert mgr.current.route_state["status"] == "lost"
    assert mgr.current.route_state["pieces"]["synthesizer"] == "graveyard"


def test_recursion_binding_requires_immediate_action_adjacency():
    """95f7c0b review gap: a small explicit positive action grammar decides —
    the recursion action must apply IMMEDIATELY to the route card, with only
    supported determiners between verb and card and only supported
    auxiliaries between card and return predicate. A purpose infinitive
    ('Recur World Breaker to replace Etali's Favor.') binds the action to the
    OTHER card, and a non-returning subject ('Etali's Favor never comes
    back.') promises nothing — both leave the graveyard-only route lost.
    Unmatched/ambiguous forms stay lost/unknown; no negation blacklist
    growth and no broadened vocabulary do the deciding."""
    zones = {
        "players": [{"is_local": True, "seat_id": 1, "life_total": 20}],
        "turn": {"turn_number": 9, "active_player": 1, "phase": "Main1"},
        "battlefield": [],
        "hand": [],
        "graveyard": [{"instance_id": 5, "name": "Etali's Favor"}],
        "exile": [],
        "command": [],
    }

    def _routed(wins=None, **parts):
        variant = GamePlan(
            win_conditions=wins or ["win with Etali's Favor"], path="cast it and swing", **parts
        )
        variant.route_cards = ["etali's favor"]
        return variant

    to_replace = _routed(develop_next="Recur World Breaker to replace Etali's Favor.")
    assert win_route_lost(to_replace, zones)  # the action binds World Breaker, not the Favor

    never_back = _routed(active_mechanisms=["Etali's Favor never comes back."])
    assert win_route_lost(never_back, zones)  # a non-returning subject is not a promise

    # The grammar is positive, not a blacklist: the same shapes with the
    # action actually bound to the route card keep the route alive.
    bound_object = _routed(develop_next="Recur Etali's Favor to pressure the planeswalker.")
    assert not win_route_lost(bound_object, zones)  # verb + card, adjacent

    bound_subject_aux = _routed(active_mechanisms=["Etali's Favor will return from the graveyard."])
    assert not win_route_lost(bound_subject_aux, zones)  # supported auxiliary + return predicate


def test_observe_records_a_detached_recursion_route_as_lost_without_a_model_call(monkeypatch):
    """Real observation path for 95f7c0b: when the plan's recursion action is
    grammatically detached from the route card ('Recur World Breaker to
    replace Synthesizer.'), observe() must record the graveyard-only route as
    lost on the code-owned upkeep refresh — zero model calls."""
    be, mgr = _formed_manager(monkeypatch)
    mgr.current.route_cards = ["synthesizer"]
    mgr.current.active_mechanisms = ["Recur World Breaker to replace Synthesizer."]
    mgr.observe(_state(turn=5, synth_on_board=False))
    assert be.calls == 1  # upkeep stays code-owned
    assert mgr.current.route_state["status"] == "lost"
    assert mgr.current.route_state["pieces"]["synthesizer"] == "graveyard"


def test_ledger_captures_fresh_upkeep_not_the_previous_turns_snapshot(monkeypatch):
    """f39b158 gap 2: the turn-5 ledger entry must describe the board as it
    stands NOW (engine destroyed between observations), not the stale turn-4
    snapshot captured before the upkeep refresh."""
    be, mgr = _formed_manager(monkeypatch)
    mgr.current.route_cards = ["synthesizer"]
    mgr.observe(_state(turn=4))  # engine alive at this turn start
    mgr.observe(_state(turn=5, synth_on_board=False))  # destroyed since
    entry = mgr._ledger.entries[-1]
    assert entry.turn == 5
    assert "route lost" in entry.upkeep
    assert "synthesizer (graveyard)" in entry.upkeep


def test_next_steps_are_annotated_when_their_card_is_not_playable(monkeypatch):
    """f39b158 gap 3: an upcoming step naming a card that is not in a playable
    zone is marked completed/unavailable through the real observe() path —
    never presented as an unconditional instruction."""
    be, mgr = _formed_manager(monkeypatch)
    mgr.current.turn_plan = [
        {"turn": 4, "cast": ["World Breaker"]},
        {"turn": 5, "cast": ["Synthesizer"]},
    ]
    synth = {"instance_id": 10, "name": "Synthesizer", "type_line": "Artifact", "controller_seat_id": 1}
    wb = {"instance_id": 20, "name": "World Breaker", "type_line": "Creature"}

    def _variant(battlefield=(synth,), **zones):
        state = _state(turn=4)
        state["battlefield"] = list(battlefield)
        state.update(zones)
        mgr.observe(state)
        return mgr.current.next_steps

    # Already on the battlefield: marked done, not ordered again.
    steps = _variant(battlefield=(synth, {"instance_id": 11, "name": "World Breaker"}))
    assert any("world breaker already in play" in s for s in steps)

    # In hand: playable now, no annotation.
    steps = _variant(hand=[wb])
    assert any(s.startswith("T4:") and "(" not in s.split(": ", 1)[1] for s in steps)

    # Exiled: unavailable.
    steps = _variant(exile=[wb])
    assert any("world breaker unavailable (exile)" in s for s in steps)

    # Nowhere visible: honestly marked, not invented around.
    steps = _variant()
    assert any("world breaker not in a playable zone" in s for s in steps)

    # Graveyard without usable recursion: unavailable.
    steps = _variant(graveyard=[wb])
    assert any("world breaker unavailable (graveyard)" in s for s in steps)
