# ws0608 — Coaching correctness: tasks 06 + 07 + 08 — IMPLEMENTATION NOTES
# Base: a956ac1. Status: evidence gathered; implementation queued. Coordinates with task03-r3
# on mcts_evaluator.py (06/08 touch it) — serialized AFTER task03's a956ac1 is integrated.

## Task 06 — hero's verified deck only
Evidence (_apply_magezero_lookahead in mcts_evaluator.py):
- hero_deck extraction: un-filtered zones (hand,battlefield,graveyard,exile,command) picks up
  OPPONENT cards (fixture: hero Forest + opponent Island/Malcolm all feed deck identity).
- prefers legacy hero_deck_list over producer deck_cards.
Fix plan: ONE extraction helper (magezero_gating.extract_hero_deck) reading `deck_cards`
(first) with GRP-ID resolution and multiplicity; falls back to zones filtered by
controller_seat_id == local_seat; full-deck vs observed-subset distinction returned
(deck_source tag); format variant/singleton/commander checks stay in select (task05 wires).
- ModelZooClient.select already requires >=40 seen cards for full-deck Jaccard (task06 keeps).

## Task 07 — opponent hand uncertainty (magezero_gating.sample_opponent_hands)
Evidence:
- reads player cards_in_hand / hand_count only; top-level opponent_hand_count and
  zones.opponent_hand_count unsupported (fixture hand_size=4 → [[]]).
- random.seed(seed) mutates GLOBAL RNG state (fix: local random.Random(seed)).
Fix plan:
1. extract count via MCTSEvaluator._resolve_opponent_hand_count (SHARED — the task03 helper).
2. Distinction: known count N → N cards per sample; known 0 → empty hands;
   missing/unknown (tier "unknown") → coverage metadata flag `hand_count_known=False`
   and no fabricated sample.
3. Revealed cards: multiplicity respected, exhausted pool cards NOT re-added (test).
4. Local RNG; plus coverage metadata (pool_size, n_revealed, coverage_ratio) in return
   (dataclass or dict) — no claims that sampled hands are facts.

## Task 08 — restrict neural afterstates to supported transitions
Evidence (_create_mechanical_afterstate in mcts_evaluator.py):
- cast branch: takes mana from pool WITHOUT checking colors, ignores Convoke/alternative costs.
- land branch: always untapped entry even for typed lands / Forest with ETB tapped.
- attack afterstate: life math only; does not mark attackers tapped or is_attacking.
Fix plan: supported set = {land (basic or explicitly-untapped), cast creature Without colored
requirements beyond pool SIZE, pass}; and ONLY when the full cost check passes. Anything else
returns None (afterstate unavailable) — branch falls back to prior-based pseudo value, carrying
`afterstate_supported=False` in branch.details (task09 will surface provenance).
- Then CONSUME shared transition logic with afterstate.py adapters where valid; no claim that
  AfterstateSimulator is an exact engine.

## Coordination
- mcts_evaluator.py edits (06/08) queued on top of a956ac1 after ws04 does NOT need mcts_evaluator
  (ws04 touches only model_zoo + client). ws0608 is the ONLY stream allowed to edit
  mcts_evaluator after task03; integration owner (me) combines.

## Files
- src/arenamcp/magezero_gating.py      (06/07)
- src/arenamcp/mcts_evaluator.py       (06/08; after b956ac1 rebase)
- src/arenamcp/afterstate.py           (08, shared transition adapters)
- tests/test_hero_deck_extraction.py / test_opponent_hand_uncertainty.py / test_supported_afterstates.py
