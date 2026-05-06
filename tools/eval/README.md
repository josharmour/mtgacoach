# mtgacoach LLM eval harness

Quantify how local Ollama models compare to the production online (GPT-5.4)
backend on **your** real coach prompts. The harness is a 3-step pipeline:

1. **Capture** real prompts during live play (or use the seed corpus).
2. **Run** each prompt through every backend you want to compare.
3. **Judge + report** with a strong online model as the rubric grader.

All artifacts live under `tools/eval/data/` (gitignored — see `.gitignore`).

## Where to run this from

**Run from Windows (PowerShell)**, not WSL — Ollama listens on Windows
`localhost:11434` and the desktop app's prompt capture also runs there. From
WSL the default `localhost:11434` won't reach Ollama; use
`--backend openai-compatible|http://<windows-host-ip>:11434/v1|<model>`
or set `OLLAMA_HOST=0.0.0.0:11434` in Ollama's config and use the host IP.

## Known issues with the seed corpus

`data/seed_prompts.jsonl` is **synthetic and not rules-verified**. It exists
to validate the pipeline end-to-end before you have real captures, not to
produce trustworthy quality numbers. Confirmed issues from a live run on
2026-05-06:

- `seed-002-removal` lists `Cast Cut Down (target Sheoldred)` as a legal
  action, but Cut Down only hits creatures with mana value ≤2 or
  toughness ≤2; Sheoldred is mv-4 toughness-5. The legal_actions field
  is wrong, so models that "follow" the prompt's lie get penalized.
- The judge model (GPT-5.4) also occasionally gets card text wrong on
  edge cases (e.g. Glistening Deluge's color clause), so seed-corpus
  judge scores have noise from both the prompt AND the judge.

**Use seed_prompts.jsonl only for plumbing checks.** For real signal,
capture from live play (next section) — those prompts come from the real
rules engine and don't have these bugs.

## Quick start (no capture, just seed corpus)

Suggested first run with what's installed: pick one ~8B, one ~14B, and the
biggest 26-30B you have.

```powershell
# PowerShell, repo root
$env:MTGACOACH_LICENSE_KEY = "<your license key>"

python -m tools.eval.run `
    --prompts tools/eval/data/seed_prompts.jsonl `
    --responses tools/eval/data/responses.jsonl `
    --backend online:gpt-5.4 `
    --backend ollama:gemma4:latest `
    --backend ollama:qwen2.5:14b `
    --backend ollama:gemma4:26b

# Score every response with GPT-5.4 as the judge.
python -m tools.eval.judge `
    --prompts tools/eval/data/seed_prompts.jsonl `
    --responses tools/eval/data/responses.jsonl `
    --scores tools/eval/data/scores.jsonl `
    --judge-backend online:gpt-5.4

# Print summary + write CSV.
python -m tools.eval.report `
    --responses tools/eval/data/responses.jsonl `
    --scores tools/eval/data/scores.jsonl `
    --csv tools/eval/data/report.csv
```

## Capturing real prompts (recommended)

The seed corpus is tiny and synthetic. The realistic comparison comes from
*your own* games. The desktop coach honors the env var
`MTGACOACH_PROMPT_DUMP_PATH`: when set, every `ProxyBackend.complete()` call
appends one JSONL record (`system`, `user`, `model`, `max_tokens`,
`temperature`) to that file. Zero overhead when the env var is unset.

```powershell
# In the same shell that launches the desktop app:
$env:MTGACOACH_PROMPT_DUMP_PATH = "$HOME\.arenamcp\eval_prompts.jsonl"
python -m arenamcp.desktop
```

Play 2-3 games normally. Each coach call appends one line. Aim for **at
least 30 prompts** for the eval to be informative — variance on small N
masks real differences.

Then point `--prompts` at that file:

```bash
python -m tools.eval.run --prompts ~/.arenamcp/eval_prompts.jsonl ...
```

## Backend specs

`--backend` is repeatable. Format:

| Spec                                          | Routes to                          |
|-----------------------------------------------|------------------------------------|
| `online:gpt-5.4`                              | `api.mtgacoach.com` (proxy)        |
| `online:claude-sonnet-4-6`                    | proxy → whatever model it pins     |
| `ollama:gemma4:latest`                        | `localhost:11434/v1` (your 8B)     |
| `ollama:qwen2.5:14b`                          | `localhost:11434/v1` (your 14B)    |
| `ollama:deepseek-r1:14b`                      | `localhost:11434/v1` (reasoning 14B)|
| `ollama:gemma4:26b`                           | `localhost:11434/v1` (your 26B)    |
| `ollama:glm-4.7-flash:latest`                 | `localhost:11434/v1` (your 30B MoE)|
| `openai-compatible\|http://host:8000/v1\|model`| arbitrary OpenAI-compatible server (note: `\|`, not `:`, because URL+model both contain `:`) |

Online specs need `--license-key` (or env `MTGACOACH_LICENSE_KEY`).

## Scoring rubric

The judge (`tools/eval/judge.py`) sends each `(prompt, response)` pair to a
strong online model with this rubric, each scored 1-5:

- **correctness**  — Does the advice match what an expert MTG coach would
  do given the legal actions and game state?
- **reasoning**    — Is the reasoning grounded in *specific* prompt facts
  (cards, mana, threats), not generic platitudes? Penalize hallucinated
  cards or rules.
- **conciseness**  — Is the advice short enough to be spoken in real time?
- **legality**     — Does the advice reference an action that's actually
  in the prompt's listed legal actions?

`report.py` prints per-backend means + median latency + median response
length, and writes a CSV.

## Reading the results

A useful shape of result:

```
backend                        n    err  latency_med  chars_med  correc reason concis legal  overall
ollama:llama3.1:8b             32   0    580ms        180        3.21   3.10   4.50   4.00   3.70
ollama:qwen2.5:14b             32   0    1850ms       210        3.95   3.80   4.20   4.40   4.09
online:gpt-5.4                 32   0    1100ms       150        4.45   4.30   4.60   4.85   4.55
```

Reading guide:

- **Latency** is what the user notices first. Local 8B on a 5080 should
  beat online round-trip; the perceived-speed gap matters more than raw
  quality if it's small.
- **Legality < 4.5** for any backend means the model is regularly
  hallucinating actions — bigger problem than soft-quality gap.
- **Correctness/reasoning gap < 0.5** with a 14B local model is a strong
  hardware-upgrade signal: a 70B-quant on 48GB is likely to close the
  gap entirely.
- **Conciseness** drops on local models that ignore the "max 2 sentences"
  instruction. Fix in the system prompt, not the eval.

## Idempotence + re-running

Each step writes to JSONL files and skips already-recorded
`(prompt_id, backend)` pairs. Safe to interrupt and resume. To re-run a
specific backend, delete its rows from `responses.jsonl` (or use a
different `--responses` path) before re-running.

## File layout

```
tools/eval/
├── README.md            ← you are here
├── __init__.py
├── run.py               ← step 2: replay prompts through backends
├── judge.py             ← step 3a: LLM-as-judge scoring
├── report.py            ← step 3b: aggregate + summary
└── data/                (gitignored — your eval artifacts)
    ├── seed_prompts.jsonl   ← ships in repo, tiny starter corpus
    ├── prompts.jsonl        ← captured from live play (you create)
    ├── responses.jsonl      ← run.py output
    ├── scores.jsonl         ← judge.py output
    └── report.csv           ← report.py output
```
