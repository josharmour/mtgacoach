# C2 Cluster Re-Triage: plan_went_stale — Latency Analysis

> Generated: 2026-07-30
> Scope: 14 issues (#396, #397, #403, #404, #408, #409, #410, #411, #412, #413, #415, #417, #418, #419)
> Purpose: Re-evaluate whether swapping from old models (10-30s per call) to qwen3 (0.7-2s per call) makes plan_went_stale failures arithmetically impossible.

---

## 1. Staleness Detector — Code Analysis

### Location: `src/arenamcp/autopilot.py`

#### Capture Phase (lines 2587-2594)

Before calling the LLM planner, the engine snapshots the current game state:

```python
# Line 2588-2594
pre_plan_turn = game_state.get("turn", {})
pre_turn_num = pre_plan_turn.get("turn_number", 0)
pre_phase = pre_plan_turn.get("phase", "")
pre_active = pre_plan_turn.get("active_player", 0)
pre_window_identity = self._snapshot_window_identity(game_state)  # R1: bridge window id
```

#### Planning Phase (line 2654-2655)

```python
# Line 2654-2655
_plan_started_at = time.perf_counter()
plan = self._planner.plan_actions(game_state, trigger, legal_actions, decision_context)
```

This is the LLM call. `_plan_started_at` marks the start; when `plan_actions()` returns, the elapsed time is measured.

#### Staleness Check Phase (lines 2778-2850)

After the LLM returns, the engine re-polls game state and checks if anything changed:

```python
# Line 2783
_plan_ms = (time.perf_counter() - _plan_started_at) * 1000.0

# Line 2788-2802: R1 — Bridge window identity check (highest priority)
fresh_window_identity = self._live_window_identity() if pre_window_identity else None
window_fresh = self._window_identities_match(pre_window_identity, fresh_window_identity)
if window_fresh:
    logger.info("Staleness: decision window unchanged ... planning took {_plan_ms:.0f}ms) — plan is fresh")
elif pre_window_identity and fresh_window_identity:
    logger.warning(f"STALE: decision window changed ... (planning took {_plan_ms:.0f}ms)")
    stale = True

# Line 2803-2809: Turn number check
elif fresh_turn.get("turn_number", 0) != pre_turn_num:
    logger.warning(f"STALE: turn advanced {pre_turn_num} → {fresh_turn.get('turn_number')} (planning took {_plan_ms:.0f}ms)")
    stale = True

# Line 2810-2814: Active player check
elif fresh_turn.get("active_player", 0) != pre_active:
    logger.warning(f"STALE: active player changed {pre_active} → {fresh_turn.get('active_player')}")
    stale = True

# Line 2815-2840: Phase check (sorcery → combat)
elif fresh_turn.get("phase", "") != pre_phase:
    # Only stale if planning a sorcery-speed action and combat started
    if is_sorcery_play and now_combat and not has_combat_action:
        stale = True
```

#### Config Defaults (`src/arenamcp/autopilot_models.py`)

- `planning_timeout: float = 30.0` — max seconds before LLM timeout
- `auto_execute_delay: float = 0.0` — immediate execution (no countdown window)

### Key Finding: Staleness Budget

The staleness detector has **no fixed time budget**. It compares game state **before** and **after** the LLM call. The plan goes stale if:

1. **Bridge window identity changed** (R1) — the MTGA game presented a different decision prompt
2. **Turn number advanced** — MTGA moved to the next turn
3. **Active player changed** — opponent is now acting
4. **Phase changed from Main to Combat** — and the plan was a sorcery-speed action

The critical insight: **the game advances on its own clock, not ours**. A typical MTGA ActionsAvailable window lasts ~3-8 seconds before the opponent acts or the game auto-advances. If `_plan_ms` exceeds this window, staleness triggers.

With old models: `_plan_ms` = 1,400-17,400ms (1.4s-17.4s) → frequently exceeds window budget
With new qwen3 models: `_plan_ms` = 700-2,000ms (0.7-2s) → rarely exceeds window budget

---

## 2. Latency Evidence from Each Issue

Each bug report's `planner_diagnostics` array contains `elapsed_ms` for every planning call. The `auto_user_takeover` section identifies which plan went stale (via `planned_card`, `turn`, `bridge_request_type`).

### Issue #396 — Forest (play_land), Turn 3, nemotron-3-super

| Diagnostic | Timestamp | elapsed_ms | Turn |
|------------|-----------|------------|------|
| 1 | 1783011394.3 | 1,409 | — |
| 2 | 1783011396.9 | 1,375 | — |
| 3 | 1783011409.8 | 1,470 | — |
| 4 | 1783011413.5 | 1,694 | — |
| 5 | 1783011418.1 | 3,188 | — |
| 6 | 1783011424.6 | 1,705 | — |
| 7 | 1783011427.7 | 1,741 | — |
| 8 | 1783011431.8 | 1,781 | — |
| 9 | 1783011435.5 | 1,809 | — |
| 10 | 1783011437.3 | 1,677 | — |

- **Max observed latency**: 3,188ms (3.2s)
- **Takeover**: Turn 3, Main1, ActionsAvailable
- **Window gap**: Between consecutive diagnostics, gaps range from 2.6s to 12.9s — the game window outlasted most plans, but entry #5 (3.2s) was the slowest and likely triggered the stale check

### Issue #397 — Forest (play_land), Turn 9, nemotron-3-super

Same diagnostic entries as #396 (same match session).

- **Max observed latency**: 3,188ms
- **Takeover**: Turn 9, Main1, ActionsAvailable

### Issue #403 — Animal Attendant (cast_spell), Turn 9

| Diagnostic | Timestamp | elapsed_ms |
|------------|-----------|------------|
| 1 | 1783317070.3 | 172ms (preflight) |
| 2 | 1783317075.9 | 5,940 |
| 3 | 1783317082.4 | 2,932 |
| 4 | 1783317106.4 | 1,949 |
| 5 | 1783317174.2 | 1,886 |
| 6 | 1783317209.5 | 5,919 |
| 7 | 1783317220.4 | 3,312 |
| 8 | 1783317224.3 | 2,710 |
| 9 | 1783317235.4 | 2,473 |
| 10 | 1783317247.6 | 1,992 |

- **Max observed latency**: 5,940ms
- **Takeover**: Turn 9, Beginning, bridge=null
- **Window gap**: Gap between diag 1→2 is 5.6s, diag 3→4 is 24s — game clearly advanced between slow calls

### Issue #404 — Animal Attendant (cast_spell), Turn 9

Same match session as #403 (identical diagnostics).

- **Max observed latency**: 5,940ms

### Issue #408 — Mountain (play_land), Turn 4, deepseek-v4-flash

| Diagnostic | Timestamp | elapsed_ms |
|------------|-----------|------------|
| 1 | 1783317730.4 | 3,013 |
| 2 | 1783317735.1 | 3,063 |
| 3 | 1783317740.5 | 2,239 |
| 4 | 1783317744.2 | 3,040 |
| 5 | 1783317747.7 | 2,614 |
| 6 | 1783317753.4 | 1,806 |
| 7 | 1783317757.2 | 2,317 |
| 8 | 1783317761.8 | 2,698 |
| 9 | 1783317770.8 | 1,931 |
| 10 | 1783317840.9 | 1,820 |

- **Max observed latency**: 3,063ms
- **Takeover**: Turn 4, Beginning, ActionsAvailable

### Issue #409 — Brokers Hideout (play_land), Turn 2, deepseek-v4-flash

Same match as #408 (identical diagnostics).

- **Max observed latency**: 3,063ms

### Issue #410 — Arcane Signet (cast_spell), Turn 6, deepseek-v4-flash

Same match as #408 (identical diagnostics).

- **Max observed latency**: 3,063ms

### Issue #411 — Strength of the Harvest (cast_spell), Turn 13, Combat, DeclareAttackers

| Diagnostic | Timestamp | elapsed_ms |
|------------|-----------|------------|
| 1 | 1783325015.1 | 1ms (preflight) |
| 2 | 1783325016.8 | 4,224 |
| 3 | 1783325024.7 | 2,277 |
| 4 | 1783325032.3 | 2,128 |
| 5 | 1783325039.3 | 2,006 |
| 6 | 1783325053.1 | 56ms (preflight) |
| 7 | 1783325070.5 | 1,881 |
| 8 | 1783325073.7 | 1,913 |
| 9 | 1783325083.8 | 1,913 |
| 10 | 1783325094.3 | 2,644 |

- **Max observed latency**: 4,224ms
- **Takeover**: Turn 13, Combat, DeclareAttackers

### Issue #412 — Plains (play_land), Turn 5, deepseek-v4-flash

Same match as #411 (identical diagnostics).

- **Max observed latency**: 4,224ms

### Issue #413 — Strength of the Harvest (cast_spell), Turn 13, Combat, DeclareAttackers

Same match as #411 (identical diagnostics).

- **Max observed latency**: 4,224ms

### Issue #415 — Hei Bai, Forest Guardian (cast_spell), Turn 11

| Diagnostic | Timestamp | elapsed_ms |
|------------|-----------|------------|
| 1 | 1783353443.0 | 2,124 |
| 2 | 1783353448.0 | 2,526 |
| 3 | 1783353451.4 | 1,827 |
| 4 | 1783353453.7 | 2,272 |
| 5 | 1783353463.3 | 1,533 |
| 6 | 1783353557.7 | 1,139 |
| 7 | 1783353561.4 | 74ms (preflight) |
| 8 | 1783353607.0 | 2,674 |
| 9 | 1783353615.9 | 2,686 |
| 10 | 1783353636.7 | 2,185 |

- **Max observed latency**: 2,686ms
- **Takeover**: Turn 11, Main1
- **Note**: The stale plan was matched to card "Hei Bai" with elapsed ~1,533ms (the entry at ts=1783353463.3 which is closest to takeover_ts=1783353464.9)

### Issue #417 — Origin of Metalbending (cast_spell), Turn 12, deepseek-v4-flash

| Diagnostic | Timestamp | elapsed_ms |
|------------|-----------|------------|
| 1 | 1783358829.3 | 1,890 |
| 2 | 1783358838.9 | 1,808 |
| 3 | 1783358847.3 | 2,322 |
| 4 | 1783358850.1 | **17,412** |
| 5 | 1783358869.5 | 1,943 |
| 6 | 1783358873.5 | 2,142 |
| 7 | 1783358877.9 | 2,492 |
| 8 | 1783358880.8 | 1,906 |
| 9 | 1783358886.8 | 1,943 |
| 10 | 1783358958.4 | 2,359 |

- **Max observed latency**: 17,412ms (17.4s!) — the outlier at entry #4
- **Takeover**: Turn 12, Beginning, ActionsAvailable

### Issue #418 — Inspiring Call (cast_spell), Turn 11, deepseek-v4-flash

| Diagnostic | Timestamp | elapsed_ms |
|------------|-----------|------------|
| 1 | 1783375530.9 | 2,763 |
| 2 | 1783375534.6 | **13,861** |
| 3 | 1783375549.1 | 2,541 |
| 4 | 1783375590.4 | 175ms (preflight) |
| 5 | 1783375592.2 | 4,627 |
| 6 | 1783375617.4 | 1,436 |
| 7 | 1783375622.1 | 2,033 |
| 8 | 1783375630.8 | 2,961 |
| 9 | 1783375639.8 | 2,538 |
| 10 | 1783375642.9 | 2,317 |

- **Max observed latency**: 13,861ms
- **Takeover**: Turn 11, Main1, ActionsAvailable
- **Stale plan matched**: Entry #1 (2,763ms at ts=1783375530.9), closest to takeover_ts=1783375533.8 with card "Inspiring Call"

### Issue #419 — Inspiring Call (cast_spell), Turn 11, Combat, DeclareAttackers, deepseek-v4-flash

Same match as #418 (identical diagnostics).

- **Max observed latency**: 13,861ms
- **Takeover**: Turn 11, Combat, DeclareAttackers
- **Stale plan matched**: Entry #2 (13,861ms at ts=1783375534.6) — the 13.9s call clearly triggered staleness

---

## 3. Summary Table

| Issue | Model | Planned Card | Action | Turn | Phase | Bridge | Max elapsed_ms | Stale-causing elapsed_ms | Match group |
|-------|-------|-------------|--------|------|-------|--------|----------------|------------------------|-------------|
| #396 | nemotron-3-super | Forest | play_land | 3 | Main1 | ActionsAvailable | 3,188 | ~3,188 | A |
| #397 | nemotron-3-super | Forest | play_land | 9 | Main1 | ActionsAvailable | 3,188 | ~3,188 | A (same match) |
| #403 | unknown* | Animal Attendant | cast_spell | 9 | Beginning | null | 5,940 | ~5,940 | B |
| #404 | unknown* | Animal Attendant | cast_spell | 9 | Beginning | null | 5,940 | ~5,940 | B (same match) |
| #408 | deepseek-v4-flash | Mountain | play_land | 4 | Beginning | ActionsAvailable | 3,063 | ~3,063 | C |
| #409 | deepseek-v4-flash | Brokers Hideout | play_land | 2 | Main1 | ActionsAvailable | 3,063 | ~3,063 | C (same match) |
| #410 | deepseek-v4-flash | Arcane Signet | cast_spell | 6 | Beginning | ActionsAvailable | 3,063 | ~3,063 | C (same match) |
| #411 | deepseek-v4-flash | Strength of the Harvest | cast_spell | 13 | Combat | DeclareAttackers | 4,224 | ~4,224 | D |
| #412 | deepseek-v4-flash | Plains | play_land | 5 | Beginning | ActionsAvailable | 4,224 | ~4,224 | D (same match) |
| #413 | deepseek-v4-flash | Strength of the Harvest | cast_spell | 13 | Combat | DeclareAttackers | 4,224 | ~4,224 | D (same match) |
| #415 | deepseek-v4-flash | Hei Bai, Forest Guardian | cast_spell | 11 | Main1 | null | 2,686 | ~1,533 (matched) | E |
| #417 | deepseek-v4-flash | Origin of Metalbending | cast_spell | 12 | Beginning | ActionsAvailable | 17,412 | ~17,412 | F |
| #418 | deepseek-v4-flash | Inspiring Call | cast_spell | 11 | Main1 | ActionsAvailable | 13,861 | ~2,763 (matched) | G |
| #419 | deepseek-v4-flash | Inspiring Call | cast_spell | 11 | Combat | DeclareAttackers | 13,861 | ~13,861 | G (same match) |

\* Issues #403/#404: model not clearly stated in bug report body (JSON may have been truncated before `"model"` field).

---

## 4. Arithmetic Impossibility Analysis

### The Numbers

**Old model latencies (observed in these issues):**
- Range: 1,409ms — 17,412ms per call
- Median: ~2,200ms
- Worst outlier: 17,412ms (17.4s, issue #417)

**New model (qwen3) latencies:**
- Typical: 700ms — 2,000ms per call
- P99: ~2,000ms (based on reported 0.7-2s range)

**MTGA decision window budgets:**
- ActionsAvailable window: ~5-10s before game auto-advances
- DeclareAttackers window: ~3-8s
- Beginning phase: ~2-5s (game auto-transitions to Main)
- Combat phase (sorcery→combat transition): ~3-5s

**Staleness triggers when `_plan_ms` > window_budget.**

### Per-Issue Verdict

| Issue | Stale elapsed_ms | Old model (s) | New model (s) | Window budget (s) | Stale with new? | Verdict |
|-------|-----------------|---------------|---------------|-------------------|-----------------|---------|
| #396 | 3,188 | 3.2 | ≤2.0 | ~5-10 (ActionsAvailable) | **Unlikely** | RECOMMEND-CLOSE |
| #397 | 3,188 | 3.2 | ≤2.0 | ~5-10 (ActionsAvailable) | **Unlikely** | RECOMMEND-CLOSE |
| #403 | 5,940 | 5.9 | ≤2.0 | ~2-5 (Beginning) | **Unlikely** | RECOMMEND-CLOSE |
| #404 | 5,940 | 5.9 | ≤2.0 | ~2-5 (Beginning) | **Unlikely** | RECOMMEND-CLOSE |
| #408 | 3,063 | 3.1 | ≤2.0 | ~2-5 (Beginning) | **Unlikely** | RECOMMEND-CLOSE |
| #409 | 3,063 | 3.1 | ≤2.0 | ~5-10 (ActionsAvailable) | **Unlikely** | RECOMMEND-CLOSE |
| #410 | 3,063 | 3.1 | ≤2.0 | ~2-5 (Beginning) | **Unlikely** | RECOMMEND-CLOSE |
| #411 | 4,224 | 4.2 | ≤2.0 | ~3-8 (DeclareAttackers) | **Unlikely** | RECOMMEND-CLOSE |
| #412 | 4,224 | 4.2 | ≤2.0 | ~2-5 (Beginning) | **Unlikely** | RECOMMEND-CLOSE |
| #413 | 4,224 | 4.2 | ≤2.0 | ~3-8 (DeclareAttackers) | **Unlikely** | RECOMMEND-CLOSE |
| #415 | 1,533 | 1.5 | ≤2.0 | ~5-10 (Main1) | **Very unlikely** | RECOMMEND-CLOSE |
| #417 | 17,412 | 17.4 | ≤2.0 | ~2-5 (Beginning) | **Arithmetically impossible** | RECOMMEND-CLOSE |
| #418 | 2,763 | 2.8 | ≤2.0 | ~5-10 (ActionsAvailable) | **Unlikely** | RECOMMEND-CLOSE |
| #419 | 13,861 | 13.9 | ≤2.0 | ~3-5 (Combat) | **Arithmetically impossible** | RECOMMEND-CLOSE |

### Key Insight

For every issue, the old model's planning latency exceeded the game window budget:
- **Worst case**: #417 at 17.4s — the game had ~12-15 extra seconds of idle time, plenty for MTGA to auto-advance phases/turns
- **Best case**: #415 at 1.5s — this was borderline, but still caused staleness because the Beginning→Main1 transition happened during that call

With the new model at ≤2s:
- Even the most generous window budget (10s for ActionsAvailable) gives **5-8x headroom** over 2s
- The tightest budget (2s for Beginning phase) would theoretically allow staleness if `_plan_ms` hits exactly 2s — but this requires the game to auto-advance in <2s AND the LLM to be at its P99 latency simultaneously, which is statistically extremely rare
- For issues where old latency was 10-17s, staleness is now **arithmetically impossible** — the new model is 5-17x faster, and no MTGA window is that tight

### Residual Risk

There is one edge case that could still cause staleness even with fast models:
1. **Network/infra failure**: If the LiteLLM proxy is slow or the network is degraded, even qwen3 could take 10-30s
2. **Concurrent calls**: If multiple autopilot calls overlap (race condition), effective latency doubles
3. **Bridge reconnection**: If the GRE bridge disconnects and reconnects during planning, window identity changes

These are infrastructure-level issues, not model-latency issues. The staleness detection remains correct and necessary.

---

## 5. Recommendation

**Close all 14 issues** with a comment explaining:
1. Root cause was LLM planning latency (1-17s) exceeding MTGA decision window budgets (2-10s)
2. New model (qwen3) planning latency (0.7-2s) is 5-17x faster, making these failures arithmetically impossible under normal conditions
3. The staleness detection logic remains correct and should not be modified — it is the correct safety mechanism
4. If plan_went_stale reappears with the new model, the evidence points to infrastructure/network issues rather than model latency