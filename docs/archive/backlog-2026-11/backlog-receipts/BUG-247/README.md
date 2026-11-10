# BUG-247 — the attack search scored life damage only, not poison or mill wins

Verdict: **real remaining gap, fixed in `combat_strategy._search`.** The solver
(`combat_solver.evaluate_attack`) already treated a tenth poison counter and an
emptied library as a win; the declaration search that decides whether to attack
did not consult `LossState` at all, so a winning toxic or mill attack was valued
at one damage and could be held. Source commit `3e0f415`, merged to local master
as `2c0691f`.

## Symptom and evidence

Queue row: "Block/race evaluation misses alternate-loss damage effects rather
than ordinary life damage." Acceptance: preserve the Odin must-block replay,
verify the next supported poison/mill gap with lethal/nonlethal controls, and
abstain on unsupported effects without reviving obsolete #281.

Evidence read before touching code:

* `docs/backlog-triage-2026-11-01.md` line 81 — "#247: Odin terminal-hit scope
  passes; broader poison/mill remains partial"; line 406 — `| #247 | PARTIAL |
  Strategy / blocks |`; line 440 — `| #281 | OBSOLETE | Strategy / clock |`.
* `docs/backlog-live-qualification-2026-11-01.md` line 49 — "Combat/resources
  #120/#210/#247/#329/#340/#348 … No universal combat or trigger simulation;
  broader #247/#329 remain partial".
* Existing work on master: `tests/test_terminal_combat_hit_247.py` (Odin
  terminal-hit replay, three evidence shapes) and `tests/test_combat_loss_281.py`
  (bounded poison/library modelling in the solver). Both still pass unchanged.
* The queue's `../autopilot-improvements.md` evidence path is not in this
  checkout, so the retained trace is the triage rows above.

## Mechanism on current master

`combat_solver.evaluate_attack` was already correct. Reproduced directly: a
toxic 1/1 against an opponent at nine poison returns `player_loses=True`,
score 1000, "combat hit trigger wins the game"; at eight poison it returns
False and -1000. A mill-three attacker against a three-card library returns True,
against four cards False. So the solver is not the gap.

The gap was `combat_strategy._search` (the declaration line search). Its block
loop accumulated `damage[recipient] += outcome.damage_through`, then scored
`10000` only when `player_damage >= opponent_life` and `-1000 + material` only
when `crackback >= your_life`. It never read `LossState`, so:

```
opp poison 9:   assignments={} score=0.00 dmg=0 crackback=0
opp poison 8:   assignments={} score=0.00 dmg=0 crackback=0
opp library 3:  assignments={} score=0.00 dmg=0 crackback=0
opp library 4:  assignments={} score=0.00 dmg=0 crackback=0
```

A winning attack and a losing one were indistinguishable — both scored zero and
the search held. The memo key had the same blind spot: it hashed `players`,
`battlefield`, `decision_context` and `turn`, but the alternate-loss clocks live
in `zones`, so a milled library or a fresh poison count could reuse the previous
board's answer.

## What changed

`src/arenamcp/combat_strategy.py` only:

* `_finishes(before, after)` — a *new* loss counts; a state that already shows
  ten poison or an empty library is not something this attack caused.
* `_search` reads `loss_state(state)` and `loss_state(state, opponent=True)` once,
  accumulates per-attack `poison_given`/`cards_milled` **only for player
  recipients**, and scores `10000` when life damage, `outcome.player_loses`, or a
  new poison/mill loss ends the game. Lethal life damage keeps precedence.
* The crackback forecast passes our `LossState` into `guarded_block_plan`, so its
  blocks can stop a poison or mill death instead of only life damage; a crackback
  that newly loses scores `-1000 + material`.
* The memo key now includes the four library-count fields from `zones`.
* `CombatChoice` gains trailing `poison_given`/`cards_milled` fields (positional
  constructions in existing tests keep working) and the explanation names them
  when nonzero.

Abstentions kept deliberately:

* an unknown library count (`loss_state` returns `library=None`) cannot score a
  mill win — the attack is still valued for its damage, never as terminal;
* damage assigned to a planeswalker is not a player hit, so no poison counter;
* only printed, unconditional toxic/poisonous/infect and mill text is recognized
  (`combat_loss.player_hit`); activated grants, conditional triggers and
  replacement effects stay out of scope, so nothing here revives #281.

## Acceptance map

| Acceptance clause | Where it lives | Pinned by |
|---|---|---|
| Preserve the Odin must-block replay | unchanged solver/assessment path | `tests/test_terminal_combat_hit_247.py` (4 tests, still green) |
| Verify the next supported poison/mill gap with lethal/nonlethal controls | tenth vs ninth poison; mill to three vs four cards; crackback poison/mill death vs safe | `test_tenth_poison_counter_wins_and_ninth_does_not`, `test_mill_emptying_the_library_wins_and_one_card_left_does_not`, `test_crackback_poison_death_is_not_a_free_attack`, `test_crackback_mill_death_is_not_a_free_attack` |
| Abstain on unsupported effects | unknown library count; planeswalker recipient | `test_unknown_library_count_abstains_from_a_mill_win`, `test_damage_to_a_planeswalker_is_not_a_player_hit` |
| Do not revive obsolete #281 | no new simulation; only printed unconditional text via `combat_loss.player_hit` | this receipt, limits below |
| Inspect current master before patching | solver reproduced correct first; only the declaration search changed | this receipt |

## Commands and results

```
$ .venv_mac_311/bin/python -m pytest -q tests/test_alternate_loss_choice_247.py \
    tests/test_combat_strategy.py tests/test_combat_loss_281.py \
    tests/test_terminal_combat_hit_247.py tests/test_combat_memo_invalidation_20261101.py \
    tests/test_combat_followups_199_220_212.py tests/test_big_board_combat_20261019.py \
    tests/test_losing_attack_guard.py tests/test_free_attack_rule.py \
    tests/test_our_planeswalkers_in_combat.py tests/test_watchdog_attack_20261019.py \
    tests/test_attack_tax.py tests/test_combat_solver_budget.py \
    tests/test_block_declaration_20261019.py -q
182 passed in 14.14s

$ .venv_mac_311/bin/python -m ruff check src/arenamcp/combat_strategy.py \
    tests/test_alternate_loss_choice_247.py
All checks passed!
$ .venv_mac_311/bin/python -m ruff format --check src/arenamcp/combat_strategy.py \
    tests/test_alternate_loss_choice_247.py
2 files already formatted
```

Full output: [focused-tests.txt](focused-tests.txt).

## The new regression bites

With pre-fix behaviour restored (`git show 3e0f415^:src/arenamcp/combat_strategy.py`
into the working tree):

```
FAILED test_tenth_poison_counter_wins_and_ninth_does_not
FAILED test_mill_emptying_the_library_wins_and_one_card_left_does_not
FAILED test_crackback_poison_death_is_not_a_free_attack
FAILED test_crackback_mill_death_is_not_a_free_attack
FAILED test_library_count_change_invalidates_the_cached_answer
FAILED test_damage_to_a_planeswalker_is_not_a_player_hit
6 failed, 1 passed in 6.00s

>       assert lethal.assignments == {1: {"playerSystemSeatId": 2}}
E       AssertionError: assert {} == {1: {'playerSystemSeatId': 2}}
```

The one passing test is the unknown-library abstention, which must hold both
before and after — it is a control, not a fix.

## Remaining limits

* No replay of a live poison/mill game window; these are deterministic board
  reproductions of the logged mechanism on current master.
* Only printed, unconditional self abilities are recognized. Activated grants,
  conditional triggers, replacement effects and unknown library sizes stay out —
  the same boundary as `combat_loss`'s module docstring.
* The crackback forecast still uses the visible board only; unknown tricks and
  triggers are excluded from both sides of the race.
* No Linux CI receipt for this commit yet.

## BLOCKER

Independent review and a real Linux CI run are still required before this item
can move past `mergedawaitingreview`. Nothing here was verified by its author.
