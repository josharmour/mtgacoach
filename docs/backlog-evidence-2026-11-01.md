# Backlog evidence index — 2026-11-01

This index accompanies the [canonical triage](backlog-triage-2026-11-01.md).
It preserves 263 historical source/test locator rows from the audit, reconciled to
current dispositions. The other 87 records have no row in this historical locator
matrix; absence here neither proves a missing implementation nor closes an issue.
A filename or reachable revision is a lead to inspect, not proof that a scenario
passed. Private captured reports, local task metadata and raw diagnostic transcripts
are deliberately excluded.

## Current integration receipts

| Published source | Verification | Scope |
|---|---|---|
| `6dd40ff` | [CI: 8,779 passed, 22 skipped](https://github.com/josharmour/mtgacoach/actions/runs/41300613476) | Corrected game-boundary guards (#711) |
| `d9995a6` | [CI: 8,783 passed, 22 skipped](https://github.com/josharmour/mtgacoach/actions/runs/41302555523) | Narrow unsupported-survival wording (#244 remains open) |
| `b9c9dd5` | [CI: 8,850 passed, 22 skipped](https://github.com/josharmour/mtgacoach/actions/runs/41315581772); 409 affected local tests passed | Corrected bounded entry/draw model (#308 remains open) |

The latest #308 chain is source commits `20fcae8` and `a7c6508`, integrated as
`edafc0f` and `7756ece`, with release-note commit `b9c9dd5`. The initial review found
two token-entry omissions (19 controls passed). Both original regressions now pass
unchanged; existing-Army growth and no-engine/nonqualifying/token-before-engine
controls also pass. Source hashes stayed stable during the independent 409-test run.
One earlier owner timing failure is retained in local evidence; thresholds were not
weakened. No claim of live or dual-Coach load qualification is made.

Auditor source-qualification receipts: 251 tests for the #120/#240/#287 closure
review; 144 tests for #340/#345/#348; one failing #329 residual probe with one passing
control; and 27 affected #340 tests rerun on the #308 revision. These overlapping
runs must not be added together as unique test coverage. Live installation remains
separate from published-source acceptance.

## Subsequent modal and performance review

The #329 follow-up (`6092e15`, integrated as `a319e01`) repairs the independently
reproduced automatic modal substitution without supported cast-trigger payoff.
Selected choices continue through ordinary target/cancel handling. Its standalone
integration checks passed 194 tests, including game-boundary compatibility. The
historical umbrella remains partial.

The docs-only `fb8bc4d` run failed the same three latency checks twice despite the
preceding code-identical source CI pass. A local pre-#308/current comparison passed
both versions and did not establish #308 as the cause. The subsequent performance
candidate (`b1cfa78`, integrated as `8b1dfb0`) adds a bounded blocked-fight cache;
the resolver body is AST-identical and independent mutation/identity probes are
retained in `tests/test_combat_fight_cache_review.py`. Matched cold-cache tests
showed about 7–8% improvement on two slow boards with unchanged search counts and
outcomes. No burn gain, original Linux cause, or dual-Coach load qualification is
claimed. Timing limits and truncation assertions were not weakened. These changes
were validated together in `ce29324`: 1,041 affected tests passed locally, but
[CI failed three performance checks](https://github.com/josharmour/mtgacoach/actions/runs/41322604504)
with 8,868 passed and 22 skipped. Source publication and live deployment remain distinct.

## Latest published batch and scoped record closures

Published `0b33a56` combines the #84 diagnostic correction (`9c40bb5`) and a
second performance optimization (`0b33a56`, source `946990b`). The former aligns
recorded explanations with final attack removals, mandatory additions and attack
taxes while preserving the original explanation separately. It does not change
attack selection or close historical #84. The latter reuses fight keys within a
block search and protection keys by current content, including nested mutations.

The combined local run passed **1,050 tests** with stable source hashes. In
[CI run 41327515639](https://github.com/josharmour/mtgacoach/actions/runs/41327515639),
**8,912 passed, 22 skipped and two performance checks failed**: SLOW_202111 took
499ms against 480ms, and burn search truncated. SLOW_212608 now passes. Lint and
hosted-service checks passed. No timing limit was relaxed; release performance
qualification remains incomplete.

The separate 17:28 UTC audit verified these bounded offline closures against the
same source revision without production edits:

| ID | Evidence | Limits |
|---|---|---|
| #292 | Synchronized durable-save/upload test, retry/saved-output tests and reload busy/timeout controls | Local writes still block reload; HTTP 429 retries after save do not. No new live reload-under-429 qualification. |
| #337 | Eight existing terminal-life regressions and four retained minimized-event replays, including duplicate-terminal and unchanged-input checks | Explicit life totals and positive-life concessions remain authoritative. Not complete match replay or a historical-data rewrite. |

The audit's **23 existing tests and four retained-event replays passed**. Source
and test hashes matched the pinned revision. The canonical unresolved-record count
is now **114**, not 114 demonstrated current bugs. #267 remains pending because its
original delayed-result sequence was not recovered; no #267 closure is counted.
Private incident identifiers, captures and local task paths remain unpublished.

## Historical source/test locators

Statuses are reconciled; locator provenance remains historical. "Reachable" describes
the auditor's published-source ancestry check, not a new test run.

| ID | Current disposition | Historical commit provenance | Revision locators | Test locators |
|---|---|---|---|---|
| #1 | FIX_RECORDED | all cited revisions reachable | a6ce8a0 (reachable), e1b74ba (reachable) | `tests/test_action_planner_legality.py`, `tests/test_all_in_labels_20261020.py`, `tests/test_attack_line_guard_20261019.py`, `tests/test_attack_override.py`, `tests/test_big_board_planner_20261019.py`, `tests/test_block_assignment_completeness.py`, `tests/test_block_declaration_20261019.py`, `tests/test_coach_engine_castability.py`, `tests/test_combat_identity.py`, `tests/test_compact_coach.py`, `tests/test_game_plan_unusable_and_kills_20261019.py`, `tests/test_narration.py`, `tests/test_prompt_compaction_20261019.py`, `tests/test_prompt_duplication.py`, `tests/test_target_selection_grounding.py`, `tests/test_target_state_facts.py` |
| #2 | FIX_RECORDED | all cited revisions reachable | a6ce8a0 (reachable), e1b74ba (reachable) | `tests/race_boards_20261019.py`, `tests/test_action_planner_legality.py`, `tests/test_attack_line_guard_20261019.py`, `tests/test_attack_override.py`, `tests/test_big_board_planner_20261019.py`, `tests/test_block_declaration_20261019.py`, `tests/test_cast_pumps_20261019.py`, `tests/test_coach_engine_castability.py`, `tests/test_combat_identity.py`, `tests/test_decisions.py`, `tests/test_game_plan_unusable_and_kills_20261019.py`, `tests/test_narration.py`, `tests/test_planner_combat_voice.py`, `tests/test_prompt_compaction_20261019.py`, `tests/test_prompt_duplication.py`, `tests/test_target_selection_grounding.py`, `tests/test_target_state_facts.py`, `tests/test_targeting_fallback_decline.py` |
| #3 | FIX_PENDING | all cited revisions reachable | 4904bd3 (reachable), a6ce8a0 (reachable) | `tests/test_action_planner_legality.py`, `tests/test_attack_override.py`, `tests/test_big_board_planner_20261019.py`, `tests/test_block_declaration_20261019.py`, `tests/test_prompt_compaction_20261019.py` |
| #4 | FIX_RECORDED | all cited revisions reachable | 129e2bc (reachable), a6ce8a0 (reachable) | `tests/test_autopilot_bridge_blockers.py`, `tests/test_big_board_planner_20261019.py`, `tests/test_block_declaration_20261019.py`, `tests/test_creature_type_choice_20261019.py`, `tests/test_prompt_compaction_20261019.py` |
| #5 | FIX_PENDING | all cited revisions reachable | 9c7dae5 (reachable), a6ce8a0 (reachable) | `tests/test_big_board_planner_20261019.py`, `tests/test_block_declaration_20261019.py`, `tests/test_draft_unrated_rares.py`, `tests/test_prompt_compaction_20261019.py` |
| #6 | FIX_RECORDED | all cited revisions reachable | 129e2bc (reachable), a6ce8a0 (reachable) | `tests/test_big_board_planner_20261019.py`, `tests/test_draft_pick_two_fra.py`, `tests/test_prompt_compaction_20261019.py` |
| #7 | FIX_RECORDED | all cited revisions reachable | 2232fd5 (reachable) | `tests/test_big_board_planner_20261019.py`, `tests/test_prompt_compaction_20261019.py` |
| #8 | FIX_RECORDED | all cited revisions reachable | a6ce8a0 (reachable), e1b74ba (reachable) | `tests/test_prompt_compaction_20261019.py` |
| #9 | FIX_RECORDED | all cited revisions reachable | c9dfda7 (reachable), 9953267 (reachable) | Semantic candidates: `tests/test_reasoning_choice_conflict.py` |
| #10 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | `tests/boards_fra_quickdraft_2026_10_08.py`, `tests/boards_pro_yfra_2026_10_18.py`, `tests/fra_alchemy_draft2_2026_10_18.py`, `tests/fra_alchemy_draft_2026_10_18.py`, `tests/fra_contender_draft_2026_10_13.py`, `tests/race_boards_20261019.py`, `tests/test_big_board_planner_20261019.py`, `tests/test_prompt_compaction_20261019.py`, `tests/test_race_math_20261019.py`, `tests/test_target_veto_2026_10_18.py` |
| #11 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | `tests/race_boards_20261019.py`, `tests/test_race_math_20261019.py` |
| #12 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | `tests/test_combat_guards_20261019.py`, `tests/test_prompt_compaction_20261019.py` |
| #13 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | `tests/test_game_plan_staleness_20261019.py` |
| #14 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | `tests/test_combat_guards_20261019.py` |
| #15 | FIX_RECORDED | all cited revisions reachable | c9dfda7 (reachable), 8afdd78 (reachable) | Semantic candidates: `tests/test_target_state_facts.py` |
| #16 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | `tests/test_burn_targeting_20261019.py` |
| #17 | FIX_RECORDED | all cited revisions reachable | a6ce8a0 (reachable), e1b74ba (reachable) | Semantic candidates: `tests/test_mac_bridge_adapter.py`, `tests/test_autopilot_bridge_blockers.py` |
| #18 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | `tests/test_combat_guards_20261019.py` |
| #19 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | `tests/race_boards_20261019.py`, `tests/test_line_guard.py`, `tests/test_line_search.py`, `tests/test_line_search_pings_20261019.py` |
| #20 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | `tests/test_line_search_pings_20261019.py` |
| #21 | FIX_RECORDED | all cited revisions reachable | c9dfda7 (reachable), 7e2fb5c (reachable) | Semantic candidates: `tests/test_false_lethal_claim.py` |
| #22 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | `tests/test_combat_guards_20261019.py` |
| #23 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | `tests/test_cast_pumps_20261019.py`, `tests/test_decision_claims_20261019.py`, `tests/test_game_plan_staleness_20261019.py`, `tests/test_prompt_compaction_20261019.py` |
| #24 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | `tests/test_screen_read_under_autopilot_20261019.py` |
| #25 | OFFLINE_CLOSED | all cited revisions reachable | 410a7b5 (reachable) | `tests/test_combat_guards_20261019.py` |
| #26 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | `tests/test_burn_targeting_20261019.py` |
| #27 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | `tests/race_boards_20261019.py`, `tests/test_cast_growth_20261019.py`, `tests/test_landfall_growth_20261019.py` |
| #28 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | Semantic candidates: `tests/test_mulligan_bottom_roles_20261019.py` |
| #29 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | `tests/test_match_records_restart_20261019.py` |
| #30 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | `tests/test_outage_handling_20261019.py` |
| #31 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | `tests/test_game_plan_staleness_20261019.py` |
| #32 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | `tests/test_auto_queue_outage_20261019.py` |
| #33 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | `tests/test_match_records_restart_20261019.py` |
| #34 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | `tests/test_combat_guards_20261019.py` |
| #35 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | `tests/test_combat_guards_20261019.py` |
| #36 | FIX_RECORDED | all cited revisions reachable | 08979c3 (reachable) | `tests/test_decision_layer_20261019.py` |
| #37 | FIX_RECORDED | all cited revisions reachable | 08979c3 (reachable) | `tests/test_decision_layer_20261019.py`, `tests/test_planner_menu_picks.py` |
| #38 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | `tests/test_targeting_fallback_decline.py` |
| #39 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | `tests/test_planner_menu_picks.py` |
| #40 | FIX_RECORDED | all cited revisions reachable | 87515cc (reachable) | `tests/test_autopilot_livelock.py`, `tests/test_hand_loss_choice_20261019.py`, `tests/test_prompt_labels_20261019.py`, `tests/test_unpayable_activation_paycosts.py` |
| #41 | OFFLINE_CLOSED | all cited revisions reachable | 87515cc (reachable) | `tests/test_planner_menu_picks.py`, `tests/test_prompt_labels_20261019.py` |
| #42 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | `tests/test_empty_boards_walkers_20261019.py`, `tests/test_line_search_opponent.py`, `tests/test_line_search_walkers_20261019.py`, `tests/test_walker_growth_229.py` |
| #43 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | `tests/test_line_search_pings_20261019.py` |
| #44 | FIX_RECORDED | all cited revisions reachable | 87515cc (reachable) | `tests/test_prompt_labels_20261019.py` |
| #45 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | `tests/test_pinger_attack_20261019.py` |
| #46 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | `tests/test_role_text_20261019.py` |
| #47 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | `tests/test_match_records_restart_20261019.py` |
| #48 | FIX_RECORDED | all cited revisions reachable | 410a7b5 (reachable) | `tests/test_role_text_20261019.py` |
| #49 | OFFLINE_CLOSED | all cited revisions reachable | 410a7b5 (reachable) | `tests/test_game_plan_staleness_20261019.py` |
| #50 | FIX_RECORDED | all cited revisions reachable | ae018f4 (reachable) | `tests/test_ward_tax_20261019.py` |
| #51 | FIX_RECORDED | all cited revisions reachable | ae018f4 (reachable), 08979c3 (reachable) | `tests/test_ward_tax_20261019.py` |
| #52 | FIX_RECORDED | all cited revisions reachable | 0adfe43 (reachable) | `tests/test_concede_margin_20261019.py` |
| #53 | FIX_RECORDED | all cited revisions reachable | 87515cc (reachable) | `tests/test_prompt_labels_20261019.py` |
| #54 | FIX_RECORDED | all cited revisions reachable | 4d6c333 (reachable), 08979c3 (reachable) | `tests/test_decision_layer_20261019.py` |
| #55 | FIX_RECORDED | all cited revisions reachable | 27bfa4e (reachable), 9d51fbd (reachable) | `tests/test_auto_queue_restart_real_log_20261019.py` |
| #56 | FIX_RECORDED | all cited revisions reachable | 591d937 (reachable) | `tests/test_critical_decision_budget.py`, `tests/test_death_trigger_redirect_20261019.py`, `tests/test_decision_layer_20261019.py`, `tests/test_face_reach_redirect_20261019.py`, `tests/test_pending_stack_damage_20261019.py` |
| #57 | FIX_RECORDED | all cited revisions reachable | 08979c3 (reachable) | `tests/test_decision_layer_20261019.py` |
| #58 | FIX_RECORDED | all cited revisions reachable | 9f27d6e (reachable) | `tests/test_game_plan.py`, `tests/test_game_plan_parse_20261019.py`, `tests/test_game_plan_unusable_and_kills_20261019.py` |
| #59 | FIX_RECORDED | all cited revisions reachable | 08979c3 (reachable), 1cf39c4 (reachable) | `tests/test_decision_layer_20261019.py` |
| #60 | FIX_RECORDED | all cited revisions reachable | 08979c3 (reachable), 9b7d618 (reachable) | `tests/test_attack_line_guard_20261019.py`, `tests/test_decision_layer_20261019.py` |
| #61 | FIX_RECORDED | all cited revisions reachable | 8d9b7b8 (reachable), b720163 (reachable) | `tests/test_kept_spell_loop_20261019.py`, `tests/test_restart_own_stack_20261019.py` |
| #62 | FIX_RECORDED | all cited revisions reachable | 8a79058 (reachable) | `tests/test_restart_own_stack_20261019.py` |
| #63 | FIX_RECORDED | all cited revisions reachable | 75da604 (reachable), 536ce35 (reachable) | `tests/test_mana_payment_color_20261019.py` |
| #64 | FIX_RECORDED | all cited revisions reachable | 75da604 (reachable), 08979c3 (reachable) | `tests/test_decision_layer_20261019.py`, `tests/test_game_plan_unusable_and_kills_20261019.py` |
| #65 | FIX_RECORDED | all cited revisions reachable | 75da604 (reachable), a1b8511 (reachable) | `tests/test_kept_spell_loop_20261019.py`, `tests/test_modal_no_kill_switch_20261019.py` |
| #66 | FIX_PENDING | all cited revisions reachable | e8cfba2 (reachable) | `tests/test_turn_clock_wrap_up_20261019.py` |
| #67 | FIX_RECORDED | all cited revisions reachable | dfeb84f (reachable) | `tests/test_action_planner_legality.py`, `tests/test_watchdog_attack_20261019.py` |
| #68 | FIX_RECORDED | all cited revisions reachable | 08979c3 (reachable) | `tests/test_decision_layer_20261019.py` |
| #69 | FIX_RECORDED | all cited revisions reachable | 87515cc (reachable) | `tests/test_prompt_labels_20261019.py` |
| #70 | FIX_RECORDED | all cited revisions reachable | 9f27d6e (reachable) | `tests/test_game_plan_unusable_and_kills_20261019.py` |
| #71 | FIX_RECORDED | all cited revisions reachable | b046062 (reachable) | `tests/test_self_cancel_memory_20261019.py` |
| #72 | FIX_RECORDED | all cited revisions reachable | 7dc7c0b (reachable) | `tests/test_match_records_resume_20261019.py` |
| #73 | FIX_RECORDED | all cited revisions reachable | 87515cc (reachable) | `tests/test_prompt_labels_20261019.py` |
| #74 | FIX_RECORDED | all cited revisions reachable | 6e90c97 (reachable) | `tests/test_block_trick_risk_20261019.py` |
| #75 | FIX_RECORDED | all cited revisions reachable | 8aa4363 (reachable) | `tests/test_attack_line_guard_20261019.py`, `tests/test_guard_hold_20261019.py`, `tests/test_pinger_attack_20261019.py` |
| #76 | FIX_RECORDED | all cited revisions reachable | 38afb42 (reachable) | `tests/test_land_drop_timing_20261019.py` |
| #77 | FIX_RECORDED | all cited revisions reachable | 08979c3 (reachable) | `tests/test_decision_layer_20261019.py` |
| #78 | FIX_RECORDED | all cited revisions reachable | c470bdf (reachable) | `tests/test_pinger_attack_20261019.py` |
| #79 | FIX_RECORDED | all cited revisions reachable | e4a3bf6 (reachable) | `tests/test_hand_loss_choice_20261019.py` |
| #80 | FIX_RECORDED | all cited revisions reachable | 6d97e0f (reachable) | `tests/test_burn_targeting_20261019.py`, `tests/test_burn_timing_20261019.py`, `tests/test_decision_layer_20261019.py`, `tests/test_hold_plan_chip_228.py`, `tests/test_pending_stack_damage_20261019.py` |
| #81 | FIX_RECORDED | all cited revisions reachable | 086d6f1 (reachable) | `tests/test_screen_read_under_autopilot_20261019.py` |
| #82 | FIX_RECORDED | all cited revisions reachable | 95ffce2 (reachable) | `tests/test_cast_growth_20261019.py` |
| #83 | FIX_RECORDED | all cited revisions reachable | 08979c3 (reachable) | `tests/test_decision_layer_20261019.py` |
| #84 | FIX_PENDING | all cited revisions reachable | ea50d1d (reachable), a8aaec9 (reachable) | `tests/test_claim_descriptions_20261019.py`, `tests/test_decision_claims_20261019.py` |
| #85 | FIX_RECORDED | all cited revisions reachable | 5ae13d5 (reachable) | `tests/test_auto_queue_stall.py`, `tests/test_autopilot_fixes_20261019.py` |
| #86 | FIX_RECORDED | all cited revisions reachable | 268f8bc (reachable) | `tests/test_false_kill_claim_20261019.py`, `tests/test_follow_up_burn_kill_20261019.py` |
| #87 | FIX_RECORDED | all cited revisions reachable | ee186e9 (reachable) | `tests/test_modal_sweep_20261019.py`, `tests/test_mode_kill_claim_20261019.py` |
| #88 | FIX_RECORDED | all cited revisions reachable | ee186e9 (reachable) | `tests/test_planning_consistency.py`, `tests/test_stack_ability_names_20261019.py` |
| #89 | FIX_RECORDED | all cited revisions reachable | ee186e9 (reachable) | `tests/test_deleted_instance_ids_20261019.py` |
| #90 | FIX_RECORDED | all cited revisions reachable | ad5c5ce (reachable) | `tests/test_concede_margin_20261019.py` |
| #91 | FIX_RECORDED | all cited revisions reachable | ee186e9 (reachable) | `tests/test_empty_boards_walkers_20261019.py`, `tests/test_walker_growth_229.py` |
| #92 | FIX_RECORDED | all cited revisions reachable | 5a093e8 (reachable) | `tests/test_game_plan_unusable_and_kills_20261019.py` |
| #93 | FIX_RECORDED | all cited revisions reachable | ceaa703 (reachable) | `tests/test_counter_tax_on_our_spell_20261019.py`, `tests/test_pay_prompt_asker_20261019.py` |
| #94 | FIX_RECORDED | all cited revisions reachable | 811e4b1 (reachable) | `tests/race_boards_20261019.py`, `tests/test_cast_pumps_20261019.py`, `tests/test_decision_layer_20261019.py`, `tests/test_line_search_walkers_20261019.py`, `tests/test_race_math_20261019.py` |
| #95 | FIX_RECORDED | all cited revisions reachable | a2e2cb0 (reachable) | `tests/test_opponent_attack_20261019.py` |
| #96 | FIX_RECORDED | all cited revisions reachable | a2e2cb0 (reachable) | `tests/test_combat_counters_root_20261020.py`, `tests/test_opponent_attack_20261019.py` |
| #97 | FIX_RECORDED | all cited revisions reachable | a2e2cb0 (reachable) | `tests/race_boards_20261019.py`, `tests/test_combat_counters_root_20261020.py`, `tests/test_opponent_attack_20261019.py` |
| #98 | FIX_RECORDED | all cited revisions reachable | f304975 (reachable) | `tests/test_bridge_prompt_enrichment.py`, `tests/test_clock_facts_20261019.py` |
| #99 | FIX_RECORDED | all cited revisions reachable | 4b7811b (reachable) | `tests/test_counter_types_20261019.py` |
| #100 | FIX_RECORDED | all cited revisions reachable | 1cf39c4 (reachable) | `tests/test_decision_layer_20261019.py` |
| #101 | FIX_RECORDED | all cited revisions reachable | bb0023f (reachable) | `tests/test_mulligan_three_lands_20261019.py` |
| #102 | FIX_RECORDED | all cited revisions reachable | bff4967 (reachable) | `tests/test_assign_damage_20261019.py` |
| #103 | FIX_RECORDED | all cited revisions reachable | cbb793a (reachable), 17859ea (reachable) | `tests/test_follow_up_burn_combo_20261019.py`, `tests/test_follow_up_burn_kill_20261019.py` |
| #104 | FIX_RECORDED | all cited revisions reachable | 4c2e315 (reachable) | `tests/test_pay_prompt_asker_20261019.py` |
| #105 | FIX_RECORDED | all cited revisions reachable | 059a211 (reachable) | `tests/test_autopilot_fixes_20261019.py` |
| #106 | FIX_RECORDED | all cited revisions reachable | 731a9d0 (reachable), c2876c1 (reachable), 7f4dafb (reachable) | `tests/test_autopilot_fixes_20261019.py`, `tests/test_plan_ledger_countered_106.py` |
| #107 | FIX_RECORDED | all cited revisions reachable | bfcd95d (reachable) | `tests/test_autopilot_fixes_20261019.py` |
| #109 | FIX_RECORDED | all cited revisions reachable | 861ceb7 (reachable) | `tests/test_marked_damage_kill_20261019.py`, `tests/test_mode_kill_claim_20261019.py` |
| #110 | FIX_RECORDED | all cited revisions reachable | 17859ea (reachable) | `tests/test_follow_up_burn_combo_20261019.py` |
| #111 | FIX_RECORDED | all cited revisions reachable | 3e10261 (reachable) | `tests/test_line_target_instance_20261019.py` |
| #112 | FIX_RECORDED | all cited revisions reachable | 3ebaa2b (reachable), 48aed26 (reachable), 7f4dafb (reachable) | `tests/test_room_door_threats_20261020.py` |
| #113 | FIX_RECORDED | all cited revisions reachable | a2dc983 (reachable) | `tests/test_attack_holds_20261019.py` |
| #114 | FIX_RECORDED | all cited revisions reachable | 6a9cd3f (reachable) | `tests/test_game_plan_parse_20261019.py`, `tests/test_plan_unquoted_items_20261019.py` |
| #115 | FIX_RECORDED | all cited revisions reachable | 85ff906 (reachable) | `tests/test_bridge_prompt_enrichment.py`, `tests/test_stack_and_solver_prompt.py`, `tests/test_stack_order_20261019.py`, `tests/test_ward_tax_20261019.py` |
| #116 | FIX_RECORDED | all cited revisions reachable | 7b9cdbe (reachable) | `tests/test_line_gap_20261019.py` |
| #118 | FIX_RECORDED | all cited revisions reachable | 7b9cdbe (reachable) | `tests/test_action_planner_legality.py`, `tests/test_line_gap_20261019.py` |
| #119 | FIX_RECORDED | all cited revisions reachable | 1aee0d9 (reachable) | `tests/test_casting_options_fallback_20261019.py` |
| #120 | OFFLINE_CLOSED | all cited revisions reachable | a2dc983 (reachable) | `tests/test_attack_holds_20261019.py` |
| #121 | FIX_PENDING | all cited revisions reachable | 1aee0d9 (reachable) | `tests/test_casting_option_id_aliases.py`, `tests/test_casting_options_fallback_20261019.py`, `tests/test_llm_down_fallback.py` |
| #122 | FIX_RECORDED | all cited revisions reachable | 58ccbf7 (reachable) | `tests/test_pending_stack_damage_20261019.py`, `tests/test_stack_and_solver_prompt.py` |
| #123 | FIX_RECORDED | all cited revisions reachable | 58ccbf7 (reachable) | `tests/test_card_db_type_lines.py`, `tests/test_face_down_20261019.py` |
| #124 | FIX_RECORDED | all cited revisions reachable | 58ccbf7 (reachable) | `tests/test_pending_stack_damage_20261019.py` |
| #125 | FIX_RECORDED | all cited revisions reachable | 58ccbf7 (reachable) | `tests/test_pending_stack_damage_20261019.py` |
| #126 | OFFLINE_CLOSED | all cited revisions reachable | 591d937 (reachable) | `tests/test_death_trigger_redirect_20261019.py`, `tests/test_face_reach_redirect_20261019.py` |
| #127 | FIX_RECORDED | all cited revisions reachable | 29e57a6 (reachable) | `tests/test_walker_attack_guard_20261019.py` |
| #128 | FIX_RECORDED | all cited revisions reachable | f595b49 (reachable) | `tests/engine_boards_20261019.py`, `tests/test_attack_line_guard_20261019.py`, `tests/test_lifegain_growth_20261019.py` |
| #129 | FIX_RECORDED | all cited revisions reachable | f595b49 (reachable) | `tests/engine_boards_20261019.py`, `tests/test_end_step_and_consuls_20261019.py` |
| #130 | FIX_RECORDED | all cited revisions reachable | f595b49 (reachable) | `tests/test_walker_target_facts_20261019.py` |
| #131 | FIX_RECORDED | all cited revisions reachable | c8ad024 (reachable) | `tests/test_autopilot_fixes_20261019.py` |
| #132 | FIX_RECORDED | all cited revisions reachable | f595b49 (reachable) | `tests/engine_boards_20261019.py`, `tests/test_drain_engines_20261019.py` |
| #133 | FIX_RECORDED | all cited revisions reachable | f595b49 (reachable) | `tests/engine_boards_20261019.py`, `tests/test_board_assessment.py`, `tests/test_drain_engines_20261019.py`, `tests/test_line_search.py` |
| #134 | FIX_RECORDED | all cited revisions reachable | f08cde9 (reachable) | `tests/test_autopilot_fixes_20261019.py` |
| #135 | FIX_RECORDED | all cited revisions reachable | 1d98937 (reachable) | `tests/test_autopilot_fixes_20261019.py` |
| #136 | FIX_RECORDED | all cited revisions reachable | 1450d9e (reachable) | `tests/test_autopilot_bridge_lock.py`, `tests/test_autopilot_stale_vs_bridge.py`, `tests/test_burn_targeting_20261019.py`, `tests/test_burn_timing_20261019.py`, `tests/test_decision_layer_20261019.py`, `tests/test_false_kill_claim_20261019.py`, `tests/test_hold_plan_chip_228.py` |
| #137 | FIX_RECORDED | all cited revisions reachable | 1450d9e (reachable) | `tests/test_autopilot_bridge_lock.py`, `tests/test_autopilot_stale_vs_bridge.py`, `tests/test_burn_timing_20261019.py`, `tests/test_decision_layer_20261019.py` |
| #138 | FIX_PENDING | all cited revisions reachable | a8aaec9 (reachable) | Semantic/platform inspection required |
| #139 | FIX_RECORDED | all cited revisions reachable | 1450d9e (reachable) | `tests/test_autopilot_bridge_lock.py`, `tests/test_autopilot_stale_vs_bridge.py`, `tests/test_burn_timing_20261019.py` |
| #140 | FIX_RECORDED | all cited revisions reachable | 1450d9e (reachable) | `tests/test_autopilot_bridge_lock.py`, `tests/test_autopilot_stale_vs_bridge.py`, `tests/test_burn_timing_20261019.py`, `tests/test_reversal_recheck_failure_20261019.py` |
| #142 | FIX_RECORDED | all cited revisions reachable | 6718a73 (reachable) | `tests/test_mode_kill_claim_20261019.py` |
| #143 | FIX_RECORDED | all cited revisions reachable | f595b49 (reachable) | `tests/engine_boards_20261019.py`, `tests/test_draw_growth_20261019.py` |
| #144 | FIX_RECORDED | all cited revisions reachable | 6718a73 (reachable) | `tests/test_mode_kill_claim_20261019.py` |
| #145 | FIX_RECORDED | issue referenced in reachable commit subject | 5b9cfdd | `tests/test_face_reach_redirect_20261019.py` |
| #146 | FIX_RECORDED | all cited revisions reachable | 0e81101 (reachable) | `tests/test_autopilot_fixes_20261019.py` |
| #147 | FIX_RECORDED | all cited revisions reachable | f595b49 (reachable) | `tests/engine_boards_20261019.py`, `tests/test_burn_timing_20261019.py`, `tests/test_held_removal_floor_20261019.py` |
| #148 | FIX_RECORDED | all cited revisions reachable | 1b9ec0e (reachable) | `tests/test_autopilot_fixes_20261019.py` |
| #149 | FIX_RECORDED | issue referenced in reachable commit subject | 5ce145f | `tests/test_chump_drops_cumulative_20261019.py` |
| #150 | FIX_RECORDED | all cited revisions reachable | b837b54 (reachable) | `tests/test_autopilot_fixes_20261019.py` |
| #151 | FIX_RECORDED | issue referenced in reachable commit subject | 7341869, 20f6464 | `tests/test_reversal_recheck_failure_20261019.py` |
| #152 | OFFLINE_CLOSED | issue referenced in reachable commit subject | 9b7d618 | `tests/test_attack_line_guard_20261019.py`, `tests/test_attack_push_20261019.py` |
| #153 | FIX_RECORDED | all cited revisions reachable | 1450d9e (reachable) | `tests/test_burn_timing_20261019.py`, `tests/test_planner_micro_wins_226.py` |
| #154 | FIX_RECORDED | all cited revisions reachable | f595b49 (reachable) | `tests/engine_boards_20261019.py`, `tests/test_held_removal_floor_20261019.py` |
| #155 | FIX_RECORDED | all cited revisions reachable | b574434 (reachable) | `tests/test_big_board_planner_20261019.py` |
| #156 | FIX_RECORDED | all cited revisions reachable | b574434 (reachable) | `tests/leonardo_boards_20261019.py`, `tests/test_leonardo_sneak_20261019.py`, `tests/test_sneak_margin_20261019.py` |
| #157 | OFFLINE_CLOSED | issue referenced in reachable commit subject | d5733e4 | `tests/test_leonardo_sneak_20261019.py` |
| #158 | FIX_RECORDED | all cited revisions reachable | b574434 (reachable) | `tests/test_distant_win_ranking_20261019.py` |
| #159 | FIX_RECORDED | all cited revisions reachable | b574434 (reachable) | `tests/leonardo_boards_20261019.py`, `tests/test_big_board_planner_20261019.py` |
| #160 | FIX_RECORDED | all cited revisions reachable | b574434 (reachable) | `tests/test_leonardo_sneak_20261019.py` |
| #161 | FIX_RECORDED | all cited revisions reachable | 976e434 (reachable) | `tests/leonardo_boards_20261019.py`, `tests/test_race_role_20261019.py`, `tests/valkyrie_boards_20261019.py` |
| #162 | FIX_PENDING | all cited revisions reachable | b574434 (reachable) | `tests/test_attack_push_20261019.py`, `tests/test_big_board_combat_20261019.py`, `tests/test_big_board_planner_20261019.py`, `tests/test_big_board_tactical_line_20261019.py` |
| #163 | FIX_RECORDED | all cited revisions reachable | 8bed177 (reachable) | `tests/test_autopilot_fixes_20261019.py` |
| #164 | FIX_RECORDED | issue referenced in reachable commit subject | 6056fed | `tests/test_plan_duplicate_copies_20261019.py` |
| #165 | FIX_RECORDED | all cited revisions reachable | 92d814c (reachable) | `tests/test_autopilot_fixes_20261019.py`, `tests/test_land_drop_timing_20261019.py` |
| #166 | FIX_RECORDED | all cited revisions reachable | 6fc05ec (reachable) | `tests/test_autopilot_fixes_20261019.py` |
| #167 | FIX_RECORDED | all cited revisions reachable | e0f2ac2 (reachable) | `tests/test_autopilot_fixes_20261019.py` |
| #168 | FIX_RECORDED | all cited revisions reachable | 66ea744 (reachable) | `tests/test_wide_attack_guard_20261019.py` |
| #169 | FIX_RECORDED | all cited revisions reachable | 66ea744 (reachable) | `tests/test_assign_damage_labels_20261019.py` |
| #170 | FIX_RECORDED | all cited revisions reachable | 66ea744 (reachable) | `tests/test_plan_cost_reducers_20261019.py` |
| #171 | FIX_RECORDED | all cited revisions reachable | 66ea744 (reachable) | `tests/test_block_declaration_20261019.py`, `tests/test_chump_next_attack_20261019.py`, `tests/test_damage_doubler_blocks.py` |
| #172 | FIX_RECORDED | all cited revisions reachable | f595b49 (reachable) | `tests/engine_boards_20261019.py`, `tests/test_token_engines_20261019.py` |
| #173 | FIX_RECORDED | all cited revisions reachable | 1dc006c (reachable) | `tests/test_autopilot_fixes_20261019.py` |
| #174 | FIX_RECORDED | all cited revisions reachable | 4b9b76c (reachable) | `tests/test_autopilot_log_catch_up.py`, `tests/test_log_summarized_marker_20261019.py` |
| #175 | FIX_RECORDED | all cited revisions reachable | 1b3c2f6 (reachable) | `tests/test_autopilot_fixes_20261019.py` |
| #176 | FIX_RECORDED | no direct commit locator | Not recorded | Semantic candidates: `tests/test_big_board_combat_20261019.py::test_a_god_below_its_devotion_is_no_blocker` |
| #177 | FIX_RECORDED | all cited revisions reachable | 976e434 (reachable) | `tests/test_attack_push_20261019.py`, `tests/test_life_anthem_20261019.py`, `tests/valkyrie_boards_20261019.py` |
| #178 | OFFLINE_CLOSED | all cited revisions reachable | 976e434 (reachable) | `tests/test_attack_push_20261019.py` |
| #179 | FIX_RECORDED | all cited revisions reachable | 976e434 (reachable) | `tests/test_sneak_margin_20261019.py` |
| #180 | FIX_RECORDED | all cited revisions reachable | 976e434 (reachable) | `tests/test_life_anthem_20261019.py`, `tests/valkyrie_boards_20261019.py` |
| #181 | FIX_RECORDED | all cited revisions reachable | 7e3b00d (reachable) | `tests/test_autopilot_fixes_20261019.py` |
| #182 | FIX_RECORDED | all cited revisions reachable | 77ecd69 (reachable) | `tests/test_big_board_tactical_line_20261019.py`, `tests/test_prompt_compaction_20261019.py` |
| #184 | FIX_RECORDED | all cited revisions reachable | 0a1a7be (reachable) | `tests/test_death_drain_blocks_20261019.py` |
| #185 | FIX_RECORDED | all cited revisions reachable | 0a1a7be (reachable) | `tests/test_death_drain_blocks_20261019.py`, `tests/test_room_door_threats_20261020.py`, `tests/test_temporary_tokens_20261019.py` |
| #186 | FIX_RECORDED | all cited revisions reachable | 337b7b4 (reachable) | `tests/test_autopilot_card_name_normalize.py`, `tests/test_creature_type_choice_20261019.py` |
| #187 | FIX_RECORDED | all cited revisions reachable | 27da840 (reachable) | `tests/test_autopilot_stale_vs_bridge.py`, `tests/test_sneak_gap_20261019.py` |
| #188 | FIX_RECORDED | all cited revisions reachable | 27da840 (reachable) | `tests/test_big_board_tactical_line_20261019.py`, `tests/test_doomed_attack_search_20261019.py` |
| #189 | FIX_RECORDED | all cited revisions reachable | 2f8ef46 (reachable) | `tests/test_autopilot_stale_vs_bridge.py`, `tests/test_sneak_all_in_20261019.py`, `tests/test_sneak_margin_20261019.py` |
| #190 | FIX_RECORDED | all cited revisions reachable | 2f8ef46 (reachable) | `tests/test_auto_reload_20261019.py` |
| #191 | FIX_RECORDED | all cited revisions reachable | f6b16a7 (reachable) | `tests/test_attack_potential_20261019.py`, `tests/test_autopilot_stale_vs_bridge.py`, `tests/threat_boards_20261019.py` |
| #192 | FIX_RECORDED | all cited revisions reachable | d2f7d59 (reachable) | `tests/conftest.py`, `tests/myrel_boards_20261019.py`, `tests/test_block_search_deadline_20261019.py` |
| #193 | FIX_RECORDED | all cited revisions reachable | f6b16a7 (reachable) | `tests/test_attack_potential_20261019.py`, `tests/threat_boards_20261019.py` |
| #194 | FIX_RECORDED | all cited revisions reachable | f6b16a7 (reachable) | `tests/test_attack_potential_20261019.py`, `tests/test_autopilot_stale_vs_bridge.py`, `tests/threat_boards_20261019.py` |
| #195 | FIX_RECORDED | all cited revisions reachable | ed8eb9d (reachable) | `tests/test_autopilot_fixes_20261019.py`, `tests/test_combat_followups_199_220_212.py` |
| #196 | FIX_RECORDED | all cited revisions reachable | 303abce (reachable) | `tests/test_autopilot_fixes_20261019.py` |
| #197 | FIX_RECORDED | all cited revisions reachable | 8113b1d (reachable) | `tests/test_counted_drain_20261019.py` |
| #198 | FIX_RECORDED | all cited revisions reachable | 0803998 (reachable) | `tests/test_attack_potential_20261019.py`, `tests/test_autopilot_fixes_20261019.py`, `tests/test_combat_counters_root_20261020.py`, `tests/test_counted_drain_20261019.py` |
| #199 | FIX_PENDING | all cited revisions reachable | 8b13cb7 (reachable), 3a5f516 (reachable) | `tests/test_auto_queue_stall.py`, `tests/test_combat_followups_199_220_212.py`, `tests/test_prompt_bulk_224.py`, `tests/test_race_math_20261019.py` |
| #200 | FIX_RECORDED | all cited revisions reachable | 1741d60 (reachable) | Semantic candidates: `tests/test_mac_bridge_adapter.py` |
| #201 | FIX_PENDING | all cited revisions reachable | 7a1df5c (reachable), d7232af (reachable), 7f4dafb (reachable) | Semantic/platform inspection required |
| #202 | FIX_RECORDED | all cited revisions reachable | 9eae73b (reachable), 7f4dafb (reachable) | Semantic candidates: `tests/test_prompt_content_207_202.py`, `tests/test_manual_required_20261025.py` |
| #203 | FIX_RECORDED | all cited revisions reachable | c760260 (reachable), 8a8b8c7 (reachable), 21687e9 (reachable), 7f4dafb (reachable) | `tests/test_select_from_groups_203.py` |
| #204 | FIX_RECORDED | all cited revisions reachable | d668c19 (reachable) | `tests/test_desktop_deck_editor.py`, `tests/test_mac_bridge_deck_ops.py` |
| #205 | FIX_PENDING | all cited revisions reachable | aa9475c (reachable) | `tests/test_deck_analysis_cancel.py`, `tests/test_game_plan.py` |
| #206 | FIX_PENDING | all cited revisions reachable | 1adc79d (reachable), 7f4dafb (reachable) | `tests/test_priority_after_cast_206.py` |
| #207 | FIX_RECORDED | all cited revisions reachable | 3143b8c (reachable), 9eae73b (reachable), 7f4dafb (reachable) | `tests/test_autopilot_search_stall.py`, `tests/test_log_summary_marker_20261020.py`, `tests/test_prompt_content_207_202.py` |
| #208 | FIX_RECORDED | all cited revisions reachable | 2ca1675 (reachable) | `tests/test_all_in_labels_20261020.py` |
| #210 | PARTIAL | all cited revisions reachable | b914f12 (reachable), f917975 (reachable), fa20953 (reachable), 7f4dafb (reachable) | `tests/test_attack_tax_loop_210.py` |
| #211 | FIX_RECORDED | all cited revisions reachable | 71411d6 (reachable), 7f4dafb (reachable) | `tests/test_concede_outs_and_timing_20261020.py` |
| #212 | OFFLINE_CLOSED | all cited revisions reachable | 3a5f516 (reachable) | `tests/test_combat_followups_199_220_212.py` |
| #213 | FIX_RECORDED | all cited revisions reachable | 71411d6 (reachable), 7f4dafb (reachable) | `tests/test_concede_outs_and_timing_20261020.py` |
| #215 | FIX_PENDING | all cited revisions reachable | c1be00a (reachable) | `tests/test_backend_circuit_breaker.py`, `tests/test_coach_session.py`, `tests/test_compact_coach.py`, `tests/test_proxy_request_metrics.py`, `tests/test_startup_health_status.py` |
| #216 | FIX_RECORDED | all cited revisions reachable | 3370a6d (reachable) | Semantic candidates: `tests/test_mac_bridge_adapter.py::test_must_attack_goblin_token_shapes_and_submits_with_qualified_attackers` |
| #217 | FIX_PENDING | all cited revisions reachable | 95498d0 (reachable) | Semantic/platform inspection required |
| #218 | FIX_RECORDED | all cited revisions reachable | 1f9a7f9 (reachable) | `tests/test_seventeenlands_catalog.py` |
| #219 | FIX_PENDING | all cited revisions reachable | 8b13cb7 (reachable) | `tests/test_prompt_bulk_224.py` |
| #220 | FIX_RECORDED | all cited revisions reachable | 3a5f516 (reachable) | `tests/test_blocker_caps_20261021.py`, `tests/test_combat_followups_199_220_212.py` |
| #221 | OFFLINE_CLOSED | all cited revisions reachable | 1fc3866 (reachable) | `tests/test_decking_clock_221.py` |
| #222 | FIX_PENDING | all cited revisions reachable | d27b878 (reachable) | `tests/test_backend_circuit_breaker.py`, `tests/test_llm_latency_222.py`, `tests/test_proxy_request_metrics.py` |
| #224 | FIX_RECORDED | all cited revisions reachable | 8b13cb7 (reachable) | `tests/test_prompt_bulk_224.py` |
| #225 | FIX_RECORDED | all cited revisions reachable | 51047f3 (reachable) | Semantic candidates: `tests/test_decision_wakeup.py` |
| #226 | FIX_PENDING | all cited revisions reachable | 277e81b (reachable), d27b878 (reachable) | `tests/test_autopilot_progress.py`, `tests/test_llm_latency_222.py`, `tests/test_planner_micro_wins_226.py` |
| #227 | OFFLINE_CLOSED | all cited revisions reachable | d27b878 (reachable) | `tests/test_hold_recompute_227.py` |
| #228 | OFFLINE_CLOSED | all cited revisions reachable | 147b40c (reachable), d27b878 (reachable) | `tests/test_hold_plan_chip_228.py` |
| #229 | OFFLINE_CLOSED | all cited revisions reachable | 12eb23a (reachable) | `tests/test_modal_sweep_20261019.py`, `tests/test_walker_growth_229.py` |
| #233 | FIX_PENDING | all cited revisions reachable | 8e36cae (reachable) | `tests/test_action_json_recovery_233.py`, `tests/test_assign_damage_20261019.py` |
| #238 | FIX_PENDING | no direct commit locator | Not recorded | Semantic/platform inspection required |
| #251 | OFFLINE_CLOSED | all cited revisions reachable | f4ecbbd (reachable) | `tests/test_blink_target_guard_251.py` |
| #252 | OFFLINE_CLOSED | all cited revisions reachable | f4ecbbd (reachable) | `tests/test_blink_removal_forecast_20261025.py` |
| #255 | FIX_PENDING | all cited revisions reachable | f4ecbbd (reachable), 5abbfbb (reachable) | Semantic/platform inspection required |
| #258 | FIX_PENDING | no direct commit locator | Not recorded | Semantic/platform inspection required |
| #259 | FIX_PENDING | no direct commit locator | Not recorded | Semantic/platform inspection required |
| #261 | FIX_PENDING | no direct commit locator | Not recorded | `tests/test_ranked_failures_261.py` |
| #262 | OFFLINE_CLOSED | no direct commit locator | Not recorded | Semantic/platform inspection required |
| #263 | OFFLINE_CLOSED | no direct commit locator | Not recorded | Semantic/platform inspection required |
| #290 | FIX_PENDING | no direct commit locator | Not recorded | `tests/test_death_trigger_redirect_20261019.py` |
| #291 | OFFLINE_CLOSED | no direct commit locator | Not recorded | `tests/test_aura_stack_timing.py` |
| #292 | OFFLINE_CLOSED | no direct commit locator | Not recorded | Semantic/platform inspection required |
| #293 | FIX_PENDING | no direct commit locator | Not recorded | `tests/test_cube_format_inference.py` |
| #295 | FIX_PENDING | no direct commit locator | Not recorded | Semantic/platform inspection required |
| #296 | FIX_PENDING | no direct commit locator | Not recorded | Semantic/platform inspection required |
| #297 | FIX_PENDING | no direct commit locator | Not recorded | Semantic/platform inspection required |
| #298 | FIX_PENDING | no direct commit locator | Not recorded | `tests/test_surveil_and_ability_labels.py` |
| #299 | FIX_PENDING | no direct commit locator | Not recorded | `tests/test_stack_and_solver_prompt.py` |
| #301 | OFFLINE_CLOSED | no direct commit locator | Not recorded | `tests/test_stack_responses.py` |
| #302 | OFFLINE_CLOSED | no direct commit locator | Not recorded | `tests/test_assign_damage_split.py` |
| #304 | FIX_PENDING | no direct commit locator | Not recorded | `tests/test_stack_order_20261019.py` |
| #305 | FIX_PENDING | no direct commit locator | Not recorded | `tests/test_land_drop_timing_20261019.py` |
| #310 | OFFLINE_CLOSED | no direct commit locator | Not recorded | Semantic/platform inspection required |
| #312 | FIX_PENDING | no direct commit locator | Not recorded | `tests/test_draft_plan_acceptance.py`, `tests/test_target_state_facts.py` |
| #313 | FIX_PENDING | no direct commit locator | Not recorded | `tests/test_trigger_selection_context_313.py` |
| #314 | FIX_PENDING | no direct commit locator | Not recorded | `tests/test_revealed_library_selection_314.py` |
| #315 | FIX_PENDING | no direct commit locator | Not recorded | Semantic/platform inspection required |
| #316 | OFFLINE_CLOSED | no direct commit locator | Not recorded | Semantic/platform inspection required |
| #319 | FIX_PENDING | no direct commit locator | Not recorded | `tests/test_ward_tax_20261019.py` |
| #320 | FIX_PENDING | no direct commit locator | Not recorded | `tests/test_ward_tax_20261019.py` |
| #329 | PARTIAL | no direct commit locator | Not recorded | Semantic/platform inspection required |
| #330 | OFFLINE_CLOSED | no direct commit locator | Not recorded | `tests/test_target_guard_session_20261031.py` |
| #331 | OFFLINE_CLOSED | no direct commit locator | Not recorded | Semantic/platform inspection required |
| #332 | FIX_PENDING | no direct commit locator | Not recorded | `tests/test_public_selection_332.py`, `tests/test_ward_tax_20261019.py` |
| #333 | OFFLINE_CLOSED | no direct commit locator | Not recorded | `tests/test_ward_tax_20261019.py` |
| #334 | OFFLINE_CLOSED | no direct commit locator | Not recorded | `tests/test_cast_growth_risk_334.py` |
| #335 | OFFLINE_CLOSED | no direct commit locator | Not recorded | `tests/test_target_guard_session_20261031.py` |
| #336 | OFFLINE_CLOSED | no direct commit locator | Not recorded | `tests/test_target_guard_session_20261031.py` |
| #337 | OFFLINE_CLOSED | no direct commit locator | Not recorded | `tests/test_terminal_life_337.py` |
| #339 | OFFLINE_CLOSED | no direct commit locator | Not recorded | `tests/test_session_land_and_mobilize.py` |
