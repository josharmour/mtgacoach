# 17lands eval — production-coach quality vs statistical ground truth

This is a **separate eval target** from the local-vs-online comparison in
`tools/eval/`. Use this one to answer:

> "Is my production coach actually giving good advice, and did my last
> prompt change make it better or worse?"

The ground truth is **win rates from millions of real Arena games**, not an
LLM judge. So the score is a hard statistic you can defend.

## What this evaluates today

**v1: Mulligan decisions.** For each opening 7, the coach says KEEP or
MULLIGAN. We bucket hands by features (lands in hand, on play/draw, color
count) and use 17lands' empirical keep-WR vs mull-WR within the bucket as
the correct answer. The coach's score is the fraction of decisions where
it picked the higher-WR option.

Why mulligan first: cleanest decision in MTG, single binary output, biggest
leverage on win rate. Easiest place to either earn or lose credibility.

**Future targets** (not yet built): turn-N action eval (using the rich
turn-by-turn columns), combat decisions, sequencing.

## Pipeline

```
[download.py]              → 17lands replay CSV (~300 MB gz, ~2-3 GB raw)
       │
       ▼
[build_mulligan_prompts.py]  → mulligan_prompts.jsonl
       │                       (sampled, bucket-tagged, with per-bucket
       │                        keep_wr/mull_wr/correct in meta)
       ▼
[ tools.eval.run ]            → mulligan_responses.jsonl
       │                       (replay through one or more backends)
       ▼
[score_mulligan.py]           → printed metrics table
```

## Quick run

```powershell
# 1. Cache the data (one-time per set, ~300 MB).
python -m tools.eval.seventeenlands.download --set EOE

# 2. Sample 200 mulligan prompts at diamond+ rank.
python -m tools.eval.seventeenlands.build_mulligan_prompts `
    --csv tools\eval\data\17lands\replay_data_public.EOE.PremierDraft.csv.gz `
    --out tools\eval\data\mulligan_prompts.jsonl `
    --n 200

# 3. Replay them through one or more backends (uses the existing harness).
python -m tools.eval.run `
    --prompts tools\eval\data\mulligan_prompts.jsonl `
    --responses tools\eval\data\mulligan_responses.jsonl `
    --backend online:deepseek-v4-flash

# 4. Score: % higher-WR pick rate, % agreement with diamond+ players.
python -m tools.eval.seventeenlands.score_mulligan `
    --prompts tools\eval\data\mulligan_prompts.jsonl `
    --responses tools\eval\data\mulligan_responses.jsonl
```

Output looks like:

```
backend                        n    parse%  higher_wr%  played%  kept%   mulled%
------------------------------------------------------------------------------------
online:gpt-5.4 [DECOMMISSIONED 2026-07-24 — historical record; no longer on the gateway]
                               200  98%     67.5%       71%      78%     22%

Per-decision breakdown (how each backend handles 'keep' buckets vs 'mull' buckets):
  online:gpt-5.4    on 'keep' buckets: 130/154 = 84.4%
  online:gpt-5.4    on 'mull' buckets: 6/42  = 14.3%
```

The per-decision split is critical. **A backend that picks 'keep' in
'mull' buckets only 14% of the time is biased toward keeps** — fix the
system prompt to weight mulligans more aggressively.

## Reading the score

The headline number is **higher_wr%**. Reference points:

| Score   | Interpretation |
|---------|----------------|
| ~50%    | Coin-flip. Coach is not adding value on mulligan. |
| 60-65%  | Real signal but mediocre. Better than guessing, worse than a strong human. |
| 70-75%  | Coach is matching diamond-tier player decisions on this dimension. |
| 80%+    | Coach is at the limit of what bucket-based ground truth can detect — the remaining 20% are bucket ties or genuinely close calls. |

**`played%`** is the secondary metric — agreement with the actual decision
diamond+ players made on similar hands. Useful as a sanity check (roughly
correlates with higher_wr% but noisier).

## Methodology notes

- **Why diamond+?** At lower ranks, the "actually played" decision is
  noisier and the keep-WR vs mull-WR gap shrinks because everyone plays
  worse. Filter to diamond+ keeps ground truth crisper. Lower this with
  `--min-rank gold` if you want more samples.

- **Why `--min-bucket-n 20`?** A bucket where only 5 people kept the hand
  and 8 mulled has noisy WRs. We need at least 20 in *both* arms before
  we'll trust the bucket's "correct" answer. Buckets that don't meet this
  bar are excluded from sampling; check the build script's bucket coverage
  output.

- **Why bucket on land count + on_play + color count?** These are the
  three features that dominate mulligan EV in practice. Adding more
  features (curve, mana symbols required, specific card identities)
  shrinks bucket sizes faster than it improves ground-truth precision.
  We can refine this if early eval results suggest blind spots.

- **The CSV is per-set.** Re-download for each new Standard/Limited
  release (about every 3 months). Models that were good on EOE may not
  generalize cleanly to a new set's archetypes — re-run after each set
  rotation if mulligan accuracy is product-critical.

## Limitations

- **Limited only.** 17lands does not publish Constructed (Standard,
  Historic) replays. For Constructed coach eval you'll need a different
  ground-truth source (BepInEx replay step-through against your own
  high-rank wins is the closest available).
- **Bucket granularity.** The 3-feature bucket is coarse. Two hands with
  the same land count + colors can still be very different (e.g. 2 lands
  + 5 one-drops vs 2 lands + 5 six-drops). Bucket-tied calls are excluded
  from scoring, but within-bucket variance is a known noise source.
- **Mulligan is one decision per game.** A coach that aces this is good at
  the single most important decision in MTG, but says less about its
  in-game play. Use this as one of several eval signals, not the only one.
