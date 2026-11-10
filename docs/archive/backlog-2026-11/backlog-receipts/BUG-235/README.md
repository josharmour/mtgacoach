# BUG-235 — mana-illegal plan steps removed while later steps still depend on them

Verdict: **no production source change needed on local master `23dd4c5`.** The
three merged mechanisms the queue names as existing work cover the acceptance
criteria; the one half that had no regression at all (the deterministic
dependent-step cleanup) is now pinned by
`tests/test_plan_mana_dependencies_235.py`.

Historical evidence: `autopilot-improvements.md` row 235 (trace `89f64a23`),
"GamePlan validation (turn 7): T+1: dropped Rofellos, Llanowar Emissary — not
mana-legally legal (plan needs 2 mana budget 2 G)" at 07:07 and the same shape
for The Notary Hobbits at 08:27. The plan landed degraded: its path, priorities
and attack still assumed the dropped cast.

## Acceptance map

| Acceptance clause | Where it lives on master | Pinned by |
|---|---|---|
| Coloured mana (pips, not just counts) | `_validate_turns` trims until `_pip_matching(pips, sources)` holds; the drop issue names the budget and colours (`game_plan.py:966`) | `test_a_colored_pip_shortfall_drops_the_cast_and_its_dependents` (Forest + Wastes, Rofellos {G}{G}: 2 mana, 1 green) |
| Commander tax | `command_costs` from `_our_commanders` feeds the cast's cost (`game_plan.py:812`, `:889`) | `tests/test_strategic_plan.py::test_the_commander_tax_counts_against_the_plans_mana` |
| Sequential spending | one `used` identity set across T/T+1/T+2, per-turn sources from the plan's own land drops, rocks and static reducers from their next turn (`game_plan.py:851`, `:1008`) | `tests/test_strategic_plan.py::test_a_plan_may_cast_our_commander_from_the_command_zone_once` ("already cast earlier in the plan"), `tests/test_plan_duplicate_copies_20261019.py` (same-name twin lookup), `tests/test_plan_lines.py` budget replay |
| Remove dependent steps | `_check_dropped_cast_dependencies` clears unconditional `path`, `develop_next`, `resource_priorities` and the dropped turn's `attack`/`hold`, keeping conditional waits and same-name bodies on the board (`game_plan.py:651`) | new file, all three tests |
| …or a legal repair within the original deadline | `_repair_mana_plan` runs once, only when no JSON retry already used the opportunity, inside the first call's `deadline`, and keeps the validated fallback on failure (`game_plan.py:5102`) | `tests/test_plan_mana_repair.py` (8 tests: repair once, no third call, fallback kept, retry uses the opportunity, no extra request while a decision is pending / budget spent / backend blocked, cancellation discards the stale plan) |
| Rejected evidence reaches the next generation | drop issues stay on the plan and return as `PREVIOUS VALIDATION` in `_plan_availability_block` | `test_the_degraded_plan_carries_the_drop_back_to_the_next_reform` |

Mechanism commits already on master: `6194d8a` (2026-10-31, "Repair
mana-illegal plans once without extending their budget") added both
`_repair_mana_plan` and `_check_dropped_cast_dependencies`; `11a5f19`
(2026-10-31, "Ground every plan instruction in current card availability") put
the per-turn mana budget and "castable alone does not mean castable together"
into the generation prompt and the availability block.

## Commands and results

```
$ .venv_mac_311/bin/python -m pytest tests/test_plan_mana_dependencies_235.py -q
3 passed in 6.68s

$ .venv_mac_311/bin/python -m pytest tests/test_plan_mana_dependencies_235.py \
    tests/test_plan_mana_repair.py tests/test_plan_availability_feedback.py \
    tests/test_plan_cost_reducers_20261019.py -q
31 passed in 6.84s

$ .venv_mac_311/bin/python -m pytest tests/test_strategic_plan.py tests/test_plan_lines.py -q
85 passed in 18.97s

$ .venv_mac_311/bin/python -m ruff check tests/test_plan_mana_dependencies_235.py
All checks passed!
$ .venv_mac_311/bin/python -m ruff format --check tests/test_plan_mana_dependencies_235.py
1 file already formatted
```

## The new regression bites

Both halves were mutation-checked against master before committing:

* stubbing `_check_dropped_cast_dependencies` to a no-op fails
  `test_a_colored_pip_shortfall_drops_the_cast_and_its_dependents`
  (`plan.path == ""` fails — the path kept "Deploy Rofellos now").
* deleting the conditional guard in its `checked()` helper fails
  `test_a_conditional_wait_on_the_dropped_card_survives`
  ("Wait for a second green source before Rofellos" was removed).

The source was restored with `git checkout -- src/arenamcp/game_plan.py`
immediately after each mutation; the committed tree carries no source change.

## Remaining limits

* **Restricted mana is not modelled.** A source whose mana may only be spent on
  a subset ("spend this mana only to cast artifact spells", treasure-style
  restrictions) counts as ordinary coloured mana in the plan validator. That is
  over-permissive — it can accept a schedule that is unpayable at execution —
  and is *not* the #235 symptom (dropping legal steps). Fixing it needs a
  restriction field on the mana sources, which the snapshot does not carry.
* **{X} costs** are checked at their printed mana value only.
* **Untyped floating pools** (mana already in the pool when the plan is formed)
  are not counted; the validator uses untapped sources.
* No live replay of trace `89f64a23` against api.mtgacoach.com; this receipt is
  deterministic-reproduction evidence only.

## BLOCKER

Independent review and a real Linux CI receipt are still required; the
implementer may not claim this item verified. Nothing was pushed or deployed.
