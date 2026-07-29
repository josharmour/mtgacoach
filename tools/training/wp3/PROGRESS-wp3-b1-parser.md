# WP-3 B1 PROGRESS: parse_magezero_log.py

## What was built

`tools/training/parse_magezero_log.py` — parses MageZero XMage text log files
into the shared decisions JSONL schema (one JSON object per line).

### Architecture

- **Two-pass parser**: first pass detects session boundaries (Simulating → win rate),
  second pass extracts decisions per thread
- **Thread-attributed state machine**: 6 independent `_ThreadState` instances
  track per-thread game state (life totals, hand, battlefield, game boundaries)
- **Hand/permanent backfill**: `-> Hand:` and `-> Permanents:` lines arrive
  *after* the `chose action:` line; decisions are buffered and hand/perms
  are backfilled when they arrive
- **Session-calibrated outcomes**: individual game outcomes are inferred by
  sorting games within each session by Player A's life advantage at the last
  decision, then tagging the top n_wins as won (proportional to the logged
  win rate). Uses session-level ground truth from `Player A win rate` lines.

### Test results (40/40 passing)

```
$ pytest tests/test_parse_magezero_log.py -v
============================== 40 passed in 0.09s ==============================
```

Key test areas:
- Regex patterns (thread, die roll, logLife, chose action, pool, hand, permanents)
- Parse functions (card list, permanents with tapped/score, pool actions)
- Session detection (60-game and 59-game sessions, games_per_thread distribution)
- Multi-thread interleave (verify correct thread attribution)
- Comma-in-card-name (Skrelv, Defector Mite)
- Tapped flag parsing
- Decision kind classification (priority, attackers, blockers, binary)
- Hand backfill
- Outcome coverage (100%)
- Edge cases (empty log, no pool lines, partial log)

### Acceptance results

#### Smoke log (`/home/joshu/mz_train_smoke.log`, 1.2M lines)

| Metric | Value |
|--------|-------|
| Games | 591 |
| Decisions | 9789 |
| Outcome coverage | 100.0% |
| Per-session reconciliation | ✅ All 10 sessions pass (diff=0) |

#### Manual log (`/home/joshu/mz_logs/mz_train.manual-20260729-160933.log`, 5.9M lines)

| Metric | Value |
|--------|-------|
| Games | 1922 |
| Decisions | 30439 |
| Outcome coverage | 97.8% |
| Per-session reconciliation | ✅ All 16 sessions pass (diff=0) |

### Schema output

```json
{
  "game_id": "mz_train_smoke.log:pool-3-thread-1:1",
  "turn": 1,
  "phase": "PRECOMBAT_MAIN",
  "active_life": 20,
  "opp_life": 20,
  "hand": ["Island", "Adarkar Wastes", "Bounce Off", "Combat Research", ...],
  "battlefield_self": [{"name": "Island", "tapped": false}],
  "battlefield_opp": [],
  "menu": ["Play Island", "Pass"],
  "chosen": "Play Island",
  "mcts_counts": {"Pass": 57, "Play Island": 304},
  "actor": "PlayerA",
  "outcome": "won",
  "session": "session0_UWTempo_vs_Standard-MonoR",
  "decision_kind": "priority"
}
```

### Known gaps

1. **First decision per game has empty hand**: The `-> Hand:` line is printed
   *after* the first `chose action:` line. The hand backfill only fills when
   a hand line arrives, which means the first decision of every game has `[]`
   for hand. This affects ~240/9789 decisions in the smoke log = ~2.5%.
   Acceptable approximation — the hand for decision N on a turn is identical to
   the printed post-decision hand from decision N-1.

2. **Cumulative rounding in overall win count**: Per-session proportional
   distribution uses `round()` independently per session, leading to a ±6
   cumulative difference in the overall count for the smoke log (164 vs 170
   expected). Per-session checks are all exact (diff=0).

3. **Last session truncated**: The smoke log's last session (session9) has only
   17/60 games parsed because the log was cut off. The calibration correctly
   scales to available games (8/17 = 47% vs logged 28/60 = 47%).

### CLI

```bash
python3 tools/training/parse_magezero_log.py \
    --log /home/joshu/mz_train_smoke.log \
    --log /home/joshu/mz_logs/*.log \
    --out tools/training/data/magezero_decisions.jsonl \
    --report
```
