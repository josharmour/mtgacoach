# Self-Improvement Plan

**Date:** 2026-06-09
**Status:** In Progress — packet recorder completed 2026-06-09
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

## Stage 3 — Data volume from REAL gameplay (BotBattleScene parked)

**Status update 2026-06-09 (late):** the `BotBattleScene` path got further
than ever after the scene-allowlist fix (`1c94179`) — decks generated,
scene dispatched, strategies swapped — but stalls at the matchmaking
handshake: it is MTGA's internal dev scene being coaxed against the
production frontdoor, and the server side doesn't cooperate. **Parked.**
The Self-Play button works mechanically but cannot currently deliver a
match; treat it as experimental until the handshake is understood.

The volume sources that DO work, today:

| Source | How | Good for | Weight |
|---|---|---|---|
| **Autopilot vs Sparky (bot match)** | proven live 2026-06-09 (full match, mulligan→win) | unattended volume, legality/protocol reps, autopilot regression | high for legality, medium for strategy |
| **Human + coach vs real humans** | normal play with advice on | strategy, metagame, mulligans — ground truth | highest |
| **Autopilot vs real humans (ranked)** | works, used live | strategy under real opposition | high (use judiciously) |

The missing piece for unattended Sparky grinding is **requeueing between
matches** — menu navigation. XTest input is dead on Wayland, but the
plugin can do it the same way BotBattleBridge drives scenes: decompiled
`HomePageContentController.SetupBotMatch(selectedEvent, ...)` is the
client's own bot-match entry point. A `queue_bot_match` bridge command
invoking it is a far smaller lift than the BotBattleScene handshake and
turns autopilot into an unattended data grinder:
`queue_bot_match → autopilot plays → packet recorded → repeat`.

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

## Stage 5 — Training on the actual hardware: dual RTX PRO 6000 Blackwell

The machine has **2× RTX PRO 6000 Blackwell (96 GB each, 192 GB total)** —
this is a different league than the 16 GB assumptions baked into older
docs, and it changes the plan in three ways:

1. **Serve a much bigger local model.** vLLM with `--tensor-parallel-size 2`
   runs 70B-class models (Qwen2.5-72B, Llama-3.3-70B, gemma-3-27b at full
   precision) with real headroom. The "is local good enough?" question from
   tools/eval should be re-asked at 27B–72B, not 12B — the quality gap vs
   the online proxy may already close *before any fine-tuning*.
2. **Fine-tune without compromises.**
   - gemma-4-12b: **full fine-tune** (not just LoRA) fits comfortably on one
     card with optimizer sharding; LoRA/DPO is trivial.
   - 27B: full FT across both cards (FSDP/DeepSpeed ZeRO-3); QLoRA on one.
   - 70B-class: QLoRA/DPO across both cards is routine; the existing
     `tools/training/train.py` (TRL-based, `--load_in_4bit`) works as-is,
     just point `--model_id` higher and launch with `accelerate` (FSDP).
3. **Train and serve simultaneously** — one card serves the champion in
   vLLM while the other trains the challenger; gate runs then swap.
   `run_pipeline.py`'s champion/challenger flow maps directly onto the
   two-GPU split.

Concrete first recipe (after ~50 real matches of packets):

```bash
# 1. dataset from packets (judge-verified pairs)
python -m tools.training.build_dataset --trajectories <packets.jsonl> \
    --judge-backend online:gpt-5.4 --dedup --hard-mine \
    --out-dpo tools/training/data/dpo_dataset.json

# 2. DPO on gemma-4-12b — GPU 1, full precision, no 4-bit needed
CUDA_VISIBLE_DEVICES=1 python -m tools.training.train \
    --model_id google/gemma-4-12b-it \
    --dataset tools/training/data/dpo_dataset.json \
    --method dpo --epochs 1 \
    --output_dir tools/training/checkpoints/gemma4_dpo_v1

# 3. gate: eval harness, challenger vs champion (GPU 0 serves champion)
# 4. promote: swap vLLM to the merged checkpoint with TP as needed
```

## Build order (updated 2026-06-09)

1. [x] **Match-packet recorder** — done 2026-06-09: created `match_packets.py`, hooked decision choice in `autopilot.py` and outcomes in `request_tracker.py`, wired start/stop/save in `standalone.py`. Tests: `test_match_packets.py`.
2. **`queue_bot_match` plugin command**: unattended Sparky grinding via
   `HomePageContentController.SetupBotMatch` (replaces BotBattleScene).
3. **Judge + dataset dry run**: `build_dataset.py --judge-backend
   online:gpt-5.4 --dedup --hard-mine` over the first ~20 packets; inspect
   pair quality by hand.
4. **Local model baseline**: serve a 27B/70B on the Blackwells (vLLM TP=2),
   run tools/eval against the online proxy — fine-tuning may not even be
   the first win.
5. **Phase E typed migration** (1-2 days): ActionsAvailable onto the typed
   pipeline; the (state, option_id) format is the training interface.
6. **First DPO run** per the recipe above; gate; promote.

## Quality gates (from tools/eval README)

- Legality < 4.5 → cannot be the autopilot path, full stop.
- Correctness/reasoning gap < 0.5 vs online → local can take over autopilot
  planning.
- Latency: local on the Blackwells will crush the online round-trip;
  perceived speed matters when the quality gap is small.
