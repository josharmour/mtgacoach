# BUG-210 — attack tax: current-source verification receipt

Queue item: **BUG-210**, "Attack tax".
Symptom: *Attack-tax choices can select an unaffordable army or repeat cancelled
declarations.*
Existing work: *Cancellation recovery and current-mana attack-tax selection are
merged; broader resource/turn reasoning remains partial.*

Acceptance (verbatim from `docs/autopilot-active-work.json`):

1. Verify current payable subsets, tax conditions, legal empty withdrawal, no
   retry loop and no false attribution to unrelated casts; preserve mandatory
   attacks and legal alternatives.
2. Before editing, inspect current master and existing tests. A historical OPEN
   label is not proof the current implementation still fails. Record a narrow
   reproduction or a clearly bounded remaining gap.

## Verdict

**No source change is required on master `abc898c`.** Every acceptance point is
already implemented by the merged #210 repairs and pinned by a passing test on
the current tree. The remaining gaps are the conservative limits the batch doc
already accepted (`docs/TACTICAL_210_BATCH_2026-11-01.md`), and they fail in the
safe direction: an unknown tax is never treated as free, so the autopilot can
attack with fewer creatures than a different legal sequence would allow, but it
does not submit an army it cannot pay for.

This receipt is the bounded current-source regression record required by queue
step 6 ("retain a bounded current-source regression receipt and a master
documentation/test commit before progressing"). No behaviour was changed, so no
new test was added; the existing 46 attack-tax tests plus 31 adjacent
bridge/combat tests are the regression evidence.

## What was inspected before deciding not to edit

- `git log` for #210: `b914f12` (detect an attack tax behind an unpayable
  PayCosts cancel), `f917975` (withdraw a taxed declaration instead of
  re-declaring it), `fa20953` (finalize the withdraw on the refreshed request),
  merged as `aa4921f`; then `5ccc99e` ("Budget public attack taxes before
  projecting or submitting attacks"), which added `src/arenamcp/attack_tax.py`
  and wired it into both combat searches, the submission path, the coach text
  and the multi-turn search guard.
- `src/arenamcp/attack_tax.py` in full (214 lines): `Tax.applies`,
  `taxes(state)`, `AttackBudget.cost/payment/player_payment`, and
  `affordable_entries`.
- `src/arenamcp/autopilot_bridge.py`: both combat-search call sites (`967`,
  `1068`), submission thinning before any game command (`1241`–`1256`),
  held-selection withdrawal (`1371`–`1377`, `1417`–`1426`),
  `_withdraw_taxed_attack` (`1539`), declaration recording (`1535`) and the safe
  default (`3012`–`3020`).
- `src/arenamcp/autopilot.py`: tax state (`176`–`180`),
  `_cancel_unpayable_costs` (`958`), `_attack_tax_follows_declaration` (`992`),
  `_attack_tax_pending` (`1014`), `_clear_attack_tax` (`1019`),
  `_note_attack_declared` (`1087`).
- `src/arenamcp/combat_strategy.py`: line-search hold override (`183`) and the
  per-attacker payable enumeration (`232`, `254`, `292`).
- `src/arenamcp/autopilot_modes.py:49 _should_decline_optional_cost`: an attack
  tax reaches neither the ward branch nor the LLM pay/decline verdict, so the
  cancel path stays fast and inside the 5 s attribution window.
- All three attack-tax test files, plus the batch doc and the planning doc.

## Acceptance map

| Acceptance point | Implementation | Test |
| --- | --- | --- |
| Current payable subsets | `affordable_entries` keeps a payable intended selection, else searches payable subsets; bridge thins before any game command | `test_observed_finale_selection_unpayable_but_public_subset_retained`, `test_valid_intended_subset_is_not_replaced`, `test_solver_selects_strongest_payable_attack_and_recomputes_mana_changes`, `test_player_only_solver_filters_unpayable_subsets`, `test_bridge_submission_thins_even_line_push_before_any_game_command` |
| Tax conditions | public-fact parsing only: untapped-source condition, Enduring Story condition, walker scope, control change, phasing, face-down | `test_enduring_story_condition_is_public_not_assumed[True-1]`, `[False-0]`, `[None-None]`, `test_tapped_archangel_does_not_tax_but_untapped_one_covers_walkers`, `test_prison_does_not_tax_planeswalker_or_battle_attacks`, `test_control_change_and_phasing_change_tax`, `test_tapped_conditional_tax_is_not_assumed_inactive_on_all_future_turns`, `test_tax_activation_invalidates_assessment_cache` |
| Unknown cost/condition never free | `amount=None` propagates to `cost()`/`payment()` → no payable subset | `test_unknown_cost_or_condition_does_not_become_free_attack[{X},{W},conditional]`, `test_unknown_global_attack_cost_does_not_allow_a_free_walker_bypass`, `test_multi_turn_search_and_disabled_fallback_never_claim_free_lethal`, `test_coach_does_not_label_raw_unpayable_power_lethal`, `test_tactical_main_phase_branch_does_not_publish_unpayable_lethal` |
| Legal empty withdrawal | deselect held attackers, finalize on the refreshed request, fall back to a plain empty submit | `test_a_taxed_attack_request_is_withdrawn_then_finalized_on_the_refreshed_request`, `test_withdraw_falls_back_to_the_plain_no_attack_submit`, `test_the_adapter_withdraw_clears_selections_then_finalizes`, `test_the_adapter_withdraw_with_nothing_held_just_finalizes`, `test_tax_thinning_does_not_finalize_stale_selected_attackers` |
| No retry loop | turn-scoped flag set on the cancel, cleared only after withdraw+finalize, expires with its turn | `test_unpayable_cost_right_after_attacking_is_an_attack_tax`, `test_the_tax_flag_survives_until_the_finalize_completes`, `test_a_failed_finalize_leaves_the_tax_flag_set`, `test_the_tax_flag_expires_with_its_turn`, `test_the_safe_default_submits_the_withdraw_instead_of_refusing`, `test_the_safe_default_still_refuses_without_the_tax` |
| No false attribution to unrelated casts | declaration must be fresher than every cast/activation and inside 5 s of the same turn | `test_a_newer_cast_is_still_withheld_rather_than_called_a_tax`, `test_an_old_attack_declaration_is_not_a_tax` |
| Mandatory attacks and legal alternatives | mustAttack creatures keep a free recipient; a paid player attack failing does not drop a free walker attack; CR 508.1d note at the withdraw site | `test_must_attack_does_not_require_payment_but_preserves_free_recipient`, `test_submission_keeps_mandatory_free_walker_attack_when_paid_player_attack_fails`, `test_a_mustAttack_attacker_is_withdrawn_too` |
| Mana sources are real sources | restricted/costed/sick/tapped/phased sources excluded; attackers never pay their own tax; main-phase floating mana not reserved | `test_main_phase_floating_mana_is_not_reserved_through_combat`, `test_restricted_sick_tapped_and_attacking_sources_do_not_pay_twice`, `test_supported_creature_mana_bonus_requires_holding_that_creature_back`, `test_tax_sources_stack_and_tapped_unconditional_source_still_taxes` |

## Commands and results

```
$ .venv_mac_311/bin/python -m pytest tests/test_attack_tax.py \
    tests/test_attack_tax_loop_210.py tests/test_attack_tax_planning_210.py -q
46 passed in 0.91s

$ .venv_mac_311/bin/python -m pytest tests/test_pay_costs_flow.py \
    tests/test_pay_costs_race.py tests/test_autopilot_bridge_attackers.py \
    tests/test_combat_strategy.py tests/test_explicit_no_attack.py -q
31 passed

$ .venv_mac_311/bin/python -m pytest <all eight files above> -q
77 passed in 7.30s        # docs/backlog-receipts/BUG-210/focused-tests.txt
```

Ruff was not run because no Python file changed in this item.

## Edge-case probes (current tree, no code change)

Each row was executed against master before deciding not to edit; every row is
also pinned by one of the tests above.

| Scenario | Observed |
| --- | --- |
| Tax permanent controlled by us | cost 0 — our own prison does not tax our attacks |
| Tax permanent phased out / face down | no taxes parsed |
| Tax permanent with no controller fields | taxed (conservative) |
| Tax permanent controlled by a third seat | taxed (conservative) |
| mustAttack creature + free planeswalker recipient, one payable attacker | payable subset keeps the walker attack plus one paid player attack |
| Unknown `{X}` tax with plenty of mana | thinning returns no entries — never free |
| Battle recipient under Ghostly Prison | cost 0 — battles are not taxed by "you" clauses |
| Restricted mana ("Activate only as a sorcery", "Activate only if …", "Spend this mana only …", costed activation) | excluded from both `mana_options` and `AttackBudget.sources` |

## Remaining limits (unchanged, conservative)

- Taxes whose amount is defined by a public count (`{X}` on Sphere of Safety,
  Collective Restraint) stay unknown → no attack is submitted into them even
  when X would be affordable. Safe direction; improving it needs a separate,
  explicitly scoped change.
- Phyrexian costs (`{W/P}` on Norn's Annex) stay unknown.
- Untyped floating pools, restricted/conditional/costed/sacrifice payment
  sources, some mana bonuses and vigilance-payment timing are not modelled.
- No live qualification was performed: this receipt is current-source unit and
  bridge-level evidence only.

## BLOCKER

No live Arena qualification and no Linux CI receipt for this item (this session
may not push). The item therefore stays **merged awaiting review**; an
independent reviewer must confirm the acceptance map above against a real match
or CI run before it can be marked verified.
