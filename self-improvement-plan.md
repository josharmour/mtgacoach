# Self-Improvement Plan

**Date:** 2026-06-09
**Status:** Proposed — packet recorder not yet started
**Companion doc:** [fable-improvements.md](fable-improvements.md) (the typed
decision pipeline this plan trains against)

## Goal

Make standard advice **and** autopilot measurably better over time, using the
matches that get played anyway — plus unlimited self-play volume — as the
training signal for a custom local model.

The four candidate mechanisms (auto-file per match, self-play, fine-tune
gemma-4, talk GRE directly) are **not alternatives — they're stages of one
loop**, and most of the components already exist in the repo.

## The loop, end to end

```
play (real match or self-play)
  → match packet auto-recorded          (decisions + outcomes + replay)
  → judge pass labels each decision      (GPT-5.4 via proxy, async/nightly)
  → build_dataset.py                     (judge-verified DPO pairs; exists)
  → LoRA DPO on gemma-4-12b              (tools/training/train.py; exists, 5080)
  → eval-harness gates                   (tools/eval; legality ≥ 4.5 etc.; exists)
  → champion promotion to local vLLM     (run_pipeline.py gating; exists)
  → better autopilot → better packets → repeat
```

The judge spend on the proxy literally converts into local model weights.

## Stage 1 — Match packets (build next; everything exists in pieces)

**Every match becomes a labeled training packet, NOT a bug report.** A bug
per match is noise nobody reads; a data packet per match compounds.

At match end, automatically record:

- every decision the autopilot/coach faced, as a typed `PendingDecision`
  (option ids + labels + payability — `decisions.py`)
- the chosen option id(s) + the `RequestTracker` outcome
  (ADVANCED / REJECTED / ROLLED_BACK)
- the GRE replay file (the plugin already records these —
  `mtgacoach_Replay9.rply` observed live)
- match result, deck, opponent archetype guess

The Phase D stall corpus (`stall_corpus.py`) is exactly this mechanism for
*failures*; extend it to **all** decisions, win or lose. Bug reports stay
reserved for anomalies (stalls, rejections) — which already auto-file.

## Stage 2 — Judge pass (exists: `build_dataset.py --judge-backend`)

Asynchronously (post-match or nightly), GPT-5.4 through the proxy reviews
each packet: *given this state and these options, was the chosen option
right? If not, which one?* Outcome-independent — extracts good moves from
losses and flags lucky bad moves in wins. This produces judge-verified
DPO chosen/rejected pairs; the June improvements already implemented the
judge, state-dedup, and hard-example mining behind flags.

## Stage 3 — Self-play for volume, real matches for direction

Bot-vs-bot with random decks drifts from ladder reality. Use each source
for what it's good at:

| Source | Good for | Weight |
|---|---|---|
| Real matches | strategy, metagame, mulligans | high |
| Self-play | legality/protocol reps, rare request types, sheer volume | medium-low for strategy, high for legality |

Self-play status: the full chain (orchestrator → `start_bot_battle` →
`BotBattleScene` → vLLM answering both seats) was dead for one reason —
the plugin's home-scene allowlist went stale with a client update
(fixed 2026-06-09, `1c94179`). First live dispatch confirmed:
`BotBattleScene.Load: matches=1 sets='EOE'`.

Champion/challenger gating (`run_pipeline.py`) already prevents a bad
checkpoint from being promoted: hard legality/reasoning floors, win-rate
secondary.

## Stage 4 — "Talk GRE directly" = fable Phase E (highest leverage)

Train the model on the typed pipeline's native format:
**(structured state, option list) → option_id**, not free prose we parse.

- the answer space collapses → a 12B LoRA has a far easier learning problem
- legality is guaranteed by construction (answers validate against the
  option set mechanically)
- the same format serves advice (render the chosen option as a sentence)
  and autopilot (submit the id)

This is why fable Phase E (migrate ActionsAvailable onto `PendingDecision`)
matters beyond reliability: it defines the training interface.

## Build order

1. **Match-packet recorder** (~half day): extend `stall_corpus.py` →
   `match_packets.py`; hook the match-end path; include replay reference.
2. **Self-play verification** (in progress): first full bot battle through
   the fixed plugin; wire its trajectories into the same packet format.
3. **Judge + dataset dry run**: run `build_dataset.py --judge-backend
   online:gpt-5.4 --dedup --hard-mine` over the first ~20 packets; inspect
   pair quality by hand.
4. **Phase E typed migration** (1-2 days): ActionsAvailable onto the typed
   pipeline; retire string planning.
5. **First LoRA run** once there are ~50 real matches + a few hundred
   self-play games: DPO on gemma-4-12b (4-bit, 5080), gate with the eval
   harness, promote to local vLLM if it clears.

## Quality gates (from tools/eval README, unchanged)

- Legality < 4.5 → cannot be the autopilot path, full stop.
- Correctness/reasoning gap < 0.5 vs online at 12-14B → strong signal the
  local path can replace online for autopilot planning.
- Latency: local on the 5080 should beat the online round-trip — perceived
  speed matters when the quality gap is small.
