# PROGRESS: wp3-unseen-gate-slice

Branch: `wp3-unseen-gate-slice`. MORNING PRIORITY item 1, measurement half:
everything that CONSUMES the unseen-deck holdout logs
(`mz_unseen_HighNoonControl_vs_BGRoots.log`, `mz_unseen_BWBats_vs_HighNoonControl.log`)
once the curriculum boundary fires. The logs did not exist while this was
built; every claim below is fixture- or smoke-log-measured, and that limit is
stated in the PR.

## 1. Parser generalization (parse_magezero_log.py, run_wp3_pipeline.py)

UWTempo / deck-signature assumptions found:

1. `SessionInfo.label` hardcoded `session{n}_UWTempo_vs_{opp}` — every log's
   sessions were labeled UWTempo regardless of the deck actually played.
2. `SMOKE_OPPONENTS_GEN0` rotation applied only when "smoke" in filename;
   any other log got opponent `session{n}` (no deck identity at all).
3. The #452/#457 hand deck-signature guardrail existed ONLY as tests
   (`tests/test_parse_magezero_log.py::TestHandDeckSignature`, hardcoded
   17-name `UWTEMPO_DECK` frozenset). No runtime check, and none possible
   for any non-UWTempo primary.
4. `PRIMARY_PLAYER = "PlayerA"` / `actor="PlayerA"` / `Player A win rate`
   — the MCTS player is assumed to be PlayerA (mz harness naming). Left in
   place, but now runtime-VERIFIED whenever a deck resolves: if PlayerA's
   hands don't match the designated primary deck, the parse aborts.
5. Build-time tooling pins (unchanged, out of scope for this slice):
   `wp3/deck_inventory.py PRIMARY_DECK`, `wp3/resolve_cards.py DECK_FILES`,
   `taxonomy/build_taxonomy.py`; `magezero_card_map.json` covers only the 85
   training-deck names (unseen decks absent — no dependency here because the
   bridge renders name-only prompts).

Changes (parameterize, do NOT weaken):

- `resolve_primary_deck()`: explicit `--primary-deck` wins; else filename
  inference from `..._<Primary>_vs_<Opponent>.log`; a named deck whose .dck
  is missing raises `DeckSignatureError` (ambiguity fails closed, never
  "skip the check"); no source at all -> `None` = legacy behavior.
- `verify_hand_signature()`: runtime version of the test guardrail — ANY
  off-deck hand card aborts the whole parse with an offender histogram.
  Runs whenever a deck resolves. Signatures derive from the .dck lists
  (`parse_dck_names`, main + SB lines).
- Session labels use the resolved deck name; opponent from filename when
  available. `--primary-deck` / `--decks-dir` plumbed through
  `parse_magezero_log.py` CLI and `run_wp3_pipeline.py` (DeckSignatureError
  -> pipeline exit 43, fail closed).

**Measured legacy parity (real 210MB `mz_train_smoke.log`):** old (master)
parser vs new parser on the same file: **21,557 decisions each,
byte-identical rows** (underscore fields stripped, JSON-compared), identical
stats (`recovered_pass 11,820; ambiguous_unconsumed 6,423;
hand_named_attributed 21,557; hand_dropped_unattributed 52`), identical
session labels. `deck_signature_checked_rows = 0` (no signature source —
legacy path taken, as designed).
Note: the committed intermediate `data/wp3/decisions.jsonl` has 21,609 rows —
it was built at 48a47ac, BEFORE #457 dropped the 52 unattributable-hand rows
(21,609 - 52 = 21,557). Stale intermediate, not a regression; today's master
parser reproduces 21,557.

`MZ_SMOKE_LOG=... pytest tests/test_parse_magezero_log.py::TestHandDeckSignature`:
3 passed in 14.4s with the new parser (pinned UWTempo guardrail intact).

## 2. Corpus builder (`tools/training/wp3/build_unseen_gate_corpus.py`)

MZ logs -> gate-shaped eval prompts, record shape matched to
`gate_strategic_decisions_test.jsonl` (verified against real records: top
keys `{id, system, user, max_tokens, temperature, meta}`; meta carries every
field `gate_play_decisions.evaluate` reads — menu rows with
index/text/action_key/action_type/grp_id/instance_id/name, gold_pick,
gold_equivalent_picks, menu_size, is_land_drop, gold_action_type,
format_bucket, request_type, deck_seen_in_train=False, pass_pick,
first_land_pick, split, variant, twin_id). Prompts rendered by the SAME
production path as the training corpus (`build_magezero_bridge.build_game_state`
-> `gate_play_decisions.build_user_message`); permuted twins via
`gate_play_decisions.permute_decision`, ids `#perm`-suffixed —
`run_b5_gate_eval.check_corpora` accepts the pair (test-proven).

Fail-closed: unresolvable primary deck; primary deck in the known TRAINING
set (UWTempo, Standard-Mono*); off-deck hand (parser guardrail); rendered
prompt found in a training corpus (sha256 exclusion); "score:"/"count:" leak
markers; < --min-records. Land drops excluded by default (matches the seen
strategic gate's `exclude_land_drops: true`); duplicate-prompt policy copied
from the seen gate (same-gold collapse, conflicting-gold group drop).
Manifest carries per-log sha256, primary deck + name count, drop ledger,
class histograms, and honest provenance notes (teacher-pick labels;
name-only prompts).

**Fixture build (BGRoots primary, real .dck names, grammar cut from the
validated smoke-log shapes):** 4 parsed decisions -> 3 records
(1 land_drop_excluded), composition `{ActionType_Cast: 2, ActionType_Pass: 1}`,
3 valid permuted twins (gold answer preserved, order moved), rendered prompt
eyeballed against the raw fixture log (turn/life/hand/menu/gold all match).

## 3. Gate runner wiring (`run_b5_gate_eval.py --unseen-corpus`)

Additive only — absent flag, every new code path is behind
`if unseen_corpus is not None` and the summary json gains no key
(`build_parser().parse_args([])` -> None, test-pinned). With the flag:
preflight `check_corpora`, three extra arms (candidate identity, candidate
permuted, baseline identity) on the unseen slice, and a `generalization`
summary section scored by `gate_play_decisions.evaluate` (one scorer for
both slices): seen/unseen candidate+baseline accuracies (Wilson CIs), raw
gap with independent-resample bootstrap CI, **baseline-adjusted gap**
(cand-base paired per prompt within each slice, difficulty cancels) with
paired-bootstrap CI, and an advisory unseen permutation gap. Reporting only;
exit code untouched.

**Fixture-measured generalization section** (candidate scripted gold-on-seen
/ wrong-on-unseen, baseline gold-on-both, B=200): seen 1.0, unseen 0.0,
gap 1.0 CI [1.0, 1.0], baseline-adjusted 1.0 CI [1.0, 1.0], permutation gap
-1.0 — all exact per construction. Empty-response mismatch dies exit 2.

## 4. Tests

`tests/test_unseen_deck_gate.py`: **26 passed** (deck resolution incl. 3
fail-closed cases; non-UWTempo parsing with correct hand attribution +
comma-name menus; off-deck hand and wrong-primary-designation abort; legacy
logs unchanged; builder shape/twins/manifest/check_corpora; 3 builder
fail-closed cases; generalization bootstrap + end-to-end + fail-closed).
Related suites: `test_parse_magezero_log.py test_run_wp3_pipeline.py
test_magezero_filters.py test_magezero_bridge_leaks.py
test_magezero_combat_micro.py test_gate_play_decisions.py
test_unseen_deck_gate.py` -> **229 passed, 7 skipped** (env-gated skips,
pre-existing).

## Not done / blocked

- END-TO-END on the real unseen logs: impossible until the curriculum
  boundary writes them. First real run:
  `build_unseen_gate_corpus.py --log /home/joshu/mz_unseen_*.log` then
  `run_b5_gate_eval.py ... --unseen-corpus tools/training/data/gate_unseen_deck_test.jsonl`.
- PlayerA==primary-deck ordering in the unseen logs is unverified; if the
  harness seats decks differently, the signature check aborts loudly —
  rerun with `--primary-deck <actual>`.
- No ruff in the venvs on blackwell; py_compile + pytest only.
