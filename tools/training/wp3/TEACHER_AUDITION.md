# Teacher Audition: DeepSeek-V4-Flash-0731 on the strategic gate

Run id: `teacher_audition_dsv4` — 2026-08-01, blackwell.

Question: should distillation come from MageZero (curriculum) or from a
frontier model? First frontier candidate scored: DeepSeek-V4-Flash-0731,
the live coach, served at `http://localhost:8002/v1` (vLLM, model name
`deepseek-v4-flash`), queried through the production `ProxyBackend` path,
which sends `chat_template_kwargs: {"thinking": false}` by default — i.e.
the exact no-thinking configuration the live coach uses.

## Method

Reused the b5 gate pipeline pieces unchanged, pointed at the external
endpoint (no GPU touched, no server started):

- Generation: `tools.eval.run`, backend
  `openai-compatible|http://localhost:8002/v1|deepseek-v4-flash`,
  concurrency 4, temperature 0.0, max_tokens 400 (from the corpus records —
  identical decoding to every LoRA arm). Full n=550 plus the full permuted
  twin (1,100 requests, ~24 min, 0 backend errors); no sampling was needed.
- Scoring: `tools.training.gate_play_decisions gate` with the identical
  corpus (`gate_strategic_decisions_test.jsonl`, n=550), permuted twin, and
  rubric (G1–G7, 10,000 bootstraps) used for the four LoRA arms. Baseline
  arm: the gemma-4-12B-it responses from the `b5_v3_oracle` run
  (`gate_b5_v3_oracle_baseline_test.jsonl`) — same corpus, same scorer;
  this is the file behind the 0.3909 anchor.
- Outputs: `tools/training/data/teacher_audition_dsv4_summary.json`,
  `tools/training/data/gate_report_teacher_audition_dsv4.json`, response
  files `gate_teacher_audition_dsv4_candidate_test{,_permuted}.jsonl`
  (data dir is gitignored; the summary json is force-added with this
  report).

## Results

| arm | overall agreement with gate reference (n=550) |
|---|---|
| DeepSeek-V4-Flash-0731 (no thinking) | **0.3636** [95% CI 0.3245, 0.4047] |
| gemma-4-12B-it base (the student) | 0.3909 |
| LoRA arms v1/v2c/v2p/v3 (distilled gen-1 MageZero) | 0.240 – 0.264 |
| best no-knowledge reflex (`always_pass_else_first`) | 0.3964 |

Paired bootstrap dsv4 minus base: point delta -0.0273,
95% CI [-0.0727, +0.0182] — the CI includes zero; dsv4 is statistically
indistinguishable from the untuned 12B student on this corpus, and both
sit at or just below the pass-happy reflex policy.

Permutation consistency (same 550 decisions, menus shuffled):

- identity 0.3636 vs permuted 0.3127 — gap 0.0509 (G3 threshold 0.05,
  marginal fail)
- action agreement across the permutation: **0.5473** (dsv4 keeps the same
  action on 54.7% of decisions when the menu order changes)
- same-index rate 0.2891 (it is not just echoing a position)

Format quality: schema_valid 1.000, legality 1.000, pick extraction
0.9982, zero legality violations on both arms. Full G1–G7 verdict:
BLOCKED (G3 by 0.0009, G5, G6, G7 — the same legs every arm including the
base-vs-itself comparison would trip, since nothing beats the reflex
floor on this corpus).

Slices (candidate vs base):

- decisive resource-commit (n=368): 0.3587 vs 0.3451 — delta +0.0136,
  CI [-0.0380, +0.0652]
- passive pass/no-block (n=182): 0.3736 vs 0.4835 — the base's overall
  edge comes entirely from agreeing with the reference's passes
- by request type: dsv4 better on DeclareBlockers (0.429 vs 0.143) and
  SelectTargets (0.533 vs 0.300); worse on ActionsAvailable
  (0.337 vs 0.415) and SelectN (0.091 vs 0.364, n=11)

## Five disagreements, quoted, for a human to adjudicate

The gate reference is the owner's actual click in a real replay. It is
not guaranteed correct; these are chosen so a reader can judge who is
right.

**1. `pd-mtgacoach_Replay40-0017` (DeclareAttackers) — reference plausibly right, dsv4 followed the embedded solver hint.**
Board: your Unstoppable Slasher 2/3 [deathtouch] vs two Scathe Zombies
2/2; prompt states "If Scathe Zombies blocks: GOOD — Scathe Zombies dies,
Slasher lives" and "Crackback: 4pwr vs your 20 life — safe", but also
"Computed optimal attack: attack with nobody". Reference: `1. Attack with
Unstoppable Slasher`. dsv4 picked `2. Declare no attackers`: "Solver says
no attack; crackback safe … your Slasher would trade with a 2/2." The
stated trade reasoning is factually wrong (the prompt says blocks are
GOOD for the attacker), but the pick literally matches the prompt's own
solver line — the corpus record contradicts itself here.

**2. `pd-mtgacoach_Replay31-0014` (ActionsAvailable) — reference right.**
T9, 7 mana any color, opp at 20 with Sentinel Spider 4/4 and Colossal
Majesty. Menu: `1. Cast Ral Zarek, Guest Lecturer / 2. Cast Rush of Dread
/ 3. Pass`. Rush of Dread's text in the prompt: Spree — opponent
sacrifices half their creatures + discards half their hand + loses half
their life. Reference: Rush of Dread. dsv4 (and the base) picked Ral
Zarek: "Develop planeswalker with spare mana, no better play." With
7 mana all three Rush modes are affordable; the reference play is clearly
stronger.

**3. `pd-mtgacoach_Replay31-0036` (DeclareBlockers) — reference right, dsv4 hallucinates the attack.**
You at 5 life; attackers Baloth Packhunter 3/3 [trample] and Ilysian
Caryatid 1/1; your Emeritus of Woe 5/4 can block. Reference: `1. Block
Baloth Packhunter with Emeritus of Woe` (kills the 3/3 cleanly, take 1).
dsv4 picked `3. Decline to block`: "Blocking trades Emeritus for a 3/3
… take 7 from the two Packhunters." Both claims are false — a 5/4 does
not trade with a 3/3, and only one Packhunter is attacking.

**4. `pd-mtgacoach_Replay16-0004` (ActionsAvailable) — close call, defensible both ways.**
T3, WW available, empty board. Menu: `1. Cast Origin of Spider-Man [OK] /
2. Cast Skyward Spider [OK] / 3. Pass`. Reference: Skyward Spider (a
creature; enables the Sheltered by Ghosts aura in hand next turn). dsv4
(and the base) picked Origin of Spider-Man: "Establish board presence
early with a Spider token and a +1/+1 counter." The saga also makes a
body; sequencing creature-first is conventional but the disagreement is
not a blunder.

**5. `pd-mtgacoach_Replay60-0051` (SelectTargets) — reference unverifiable from the prompt.**
Earthbender Ascension's triggered ability must target one of your lands;
menu: `1. Target Forest / 2. Target Ba Sing Se`. Reference: Ba Sing Se.
dsv4 picked Forest: "ramp mana for future plays." The prompt does not
contain Earthbender Ascension's oracle text, so neither the model nor a
human reader can derive which target is better from the prompt alone —
the reference encodes information the prompt does not carry. (The base
model also picked Forest.)

## What this does and does not prove

This run measures one thing: agreement with the owner's Gold-level clicks
on 550 real replay menus, in the exact no-thinking serving configuration
of the live coach, single greedy sample. On that measure the frontier
candidate is a wash with the untuned 12B student (0.364 vs 0.391, CI on
the delta spans zero) and roughly 10–12 points above the gen-1-MageZero
LoRA band (0.24–0.26). It does not prove dsv4 plays at the reference's
level or above it: agreement with a Gold player is a lower-bound proxy
that actively penalizes any teacher stronger than the label source, and
the corpus rewards passivity (a pass-happy reflex scores 0.396, above
every model tested). The slice evidence cuts both ways — dsv4 is no worse
than base on the decisive resource-commit slice (+0.014, n.s.) and much
better on blocks and targeting, but examples 2 and 3 show it making
outright errors a curriculum teacher must not make, and its 54.7% action
agreement under menu permutation means nearly half its picks flip with
menu order — low conviction, likely worse if distilled. What it does
establish for the teacher decision: distilling dsv4 answers on this
prompt distribution cannot be expected to lift gate agreement above the
~0.39 base anchor (the teacher does not exceed it), so a dsv4
distillation would buy format cleanliness (schema/legality were perfect)
but no measured strategic headroom — consistent with rl-pipeline-fix.md's
"a frontier-model teacher raises the ceiling to that model's strength,
which is not expert at Magic". The MageZero-vs-frontier question is
therefore still open at the top: this run rules dsv4-as-is out as a
strategic ceiling-raiser, but says nothing about MageZero gens 2–6, about
dsv4 with thinking enabled (explicitly not measured here — the live-coach
config disables it), or about a search-augmented teacher. Those need the
same 550-prompt audition before any distillation run is launched.
