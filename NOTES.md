# ws1012 — Offline corpus lane: tasks 10 + 11 + 12 — IMPLEMENTATION NOTES
# Base: a956ac1. Status: evidence gathered; edits queued against live dirty-tree modules.

## Task 10 — separate policy-label eligibility from game outcome
Evidence (build_magezero_bridge.py):
- build_record L467-470: any outcome == "unknown" row → (None, "outcome_unknown") even under
  --outcome all. run_wp3_pipeline.py--outcome all describes itself as "preserve valid decisions
  from close/lost games", but bridge pre-drop makes that unreachable for unknown-outcome rows.
Fix plan:
1. build_record gains `outcome_mode: str` parameter (default "all"):
   - mode all: keep unknown-outcome rows, set meta["outcome"]="unknown",
     meta["policy_label"]="menu.pick" (PURE policy eligibility — no outcome supervision attached).
   - mode won_only: keep the existing drop (reason outcome_unknown).
2. meta gains observability facts: menu_size, prompt_ok (the R1 guard), so downstream counters
   can reconcile raw→filtered→rendered.
3. Counters: pipeline manifest counts by (outcome, policy_label) and reconciles
   raw = rendered + sum(drops).

## Task 11 — legacy renderer must not assert missing facts
Evidence: build_game_state is shared between bridge records & prompts. Legacy render path fills
zero CMC / empty stack / fresh ETB / local priority when facts are absent.
Fix plan: explicit render mode flag on bridge record construction ("legacy_render"): only in
legacy mode, missing stack/active/cost/opponent-hand render as omitted/unknown markers;
production live-state formatting untouched. Same counters track rejects with reasons.

## Task 12 — freece corpus lineage/split stability
Evidence: manifest already includes output hashes + git sha (not sufficient for dirty source);
splits are shuffle-based (membership changes when games appended); missing game_id falls back to
session+turn (can split one game).
Fix plan:
1. game_id normalization: canonical "source:<log-hash>:game:<gid>"; missing gid → quarantine
   counter (no fallback split).
2. Split assignment: deterministic hash(gid) % ALLOCATION with fixed seed salt stored IN the
   manifest → appending games never re-members existing ones; migrating old manifests via
   --split-migration flag re-derives with same rule.
3. Lineage: parser/encoder schema version, card-map hash, teacher checkpoint id (unknown ->
   "teacher_unknown" explicitly), input log hashes (already partially there), dirty-source hash
   (git sha AND content hash of pipeline modules).

## Coordination with other workstreams
- ws0608 (coach correctness) does not touch tools/training — no conflicts.
- ws04's manifest v2 is consumed by task 13's fixture provenance, which will align field names.

## Files
- tools/training/build_magezero_bridge.py  (t10+t11)
- tools/training/run_wp3_pipeline.py       (t10 counters, t12 splits/lineage)
- tests/test_bridge_policy_eligibility.py  (t10)
- tests/test_pipeline_split_stability.py   (t12)
