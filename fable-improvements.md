# Autopilot Framework Improvements ("Fable plan")

**Date:** 2026-06-09
**Status:** In progress

## Progress

- [x] **Phase A** — item 4 (state arbiter) + item 6 (control plane) — done 2026-06-09
  - `decision_arbiter.py` (`arbitrate()`); autopilot connected-idle branch now refuses
    ghost decisions; standalone drops `decision_required` + backstop force when the
    arbiter returns None; `set_autopilot`/`get_status` pipe commands added
    (`toggle_autopilot` kept as deprecated alias). Tests: `test_decision_arbiter.py`.
- [x] **Phase B** — item 1 (typed `PendingDecision` pipeline) — done 2026-06-09
  for the interactive families that caused the live stalls (SelectTargets,
  SelectN, Search, Mulligan): `decisions.py` (`PendingDecision`/`DecisionOption`,
  `build_pending_decision`, `submit_option`), planner `plan_decision_options`
  (LLM answers option ids, mechanical validation, deterministic fallback from
  the same set), autopilot `_try_typed_decision_path` runs before legacy
  planning. **ActionsAvailable intentionally stays on the legacy strategic
  path** (turn plans, combat solver, land-drop-first are richer) — it migrates
  in Phase E (below). Tests: `test_decisions.py`, `test_typed_decision_path.py`.
- [x] **Phase C** — items 2 + 3 (request-identity tracking + submission FSM) — done 2026-06-09
  `request_tracker.py`: content-addressed request identity (request type +
  option set — survives the GRE's msgId/gameStateId churn), one in-flight
  submission per request, observation-driven settlement (different decision
  → ADVANCED; same fingerprint after a 2s grace → REJECTED), hard cap of 3
  submissions per request (then one MANUAL REQUIRED + stand-down), escapes
  only after ≥2 real rejections. Wired into the typed-decision path; resets
  on new match. Tests: `test_request_tracker.py`.
- [x] **Phase D** — item 5 (stall corpus → CI harness) — done 2026-06-09
  `stall_corpus.py` records a full fixture (PendingDecision + planner answer +
  outcome) on every typed-path dead end (exhausted / submit_failed) into
  `~/.arenamcp/stall_corpus/` (bounded, 200). `tools/eval/replay_stalls.py`
  replays a corpus offline; curated fixtures for the three 2026-06-09 live
  failures live in `tests/fixtures/stalls/` and run in pytest
  (`test_stall_corpus.py`) — including the assertion that the deterministic
  pick never chooses an unpayable cast.
- [~] **Phase E** (in progress) — GroupRequest (London bottoming) joined the
  typed pipeline 2026-06-09 late: options = cards to bottom, LLM picks by id,
  legacy worst-card ranking stays as the fallback when the LLM gives no valid
  pick. Remaining: ActionsAvailable migration + mulligan fingerprint
  collision (below).
- [ ] **Phase E remainder** — migrate ActionsAvailable onto the typed pipeline
  (turn plans / combat solver / land-drop-first re-expressed over
  `DecisionOption`s) and retire the legacy string planning path entirely —
  the acceptance-criteria grep proof lands here. Also in scope:
  - **GroupRequest (London bottoming / ordering) as a typed family** — first
    live run (2026-06-09 19:01) showed the legacy planner answering
    `mulligan_keep` against a Group window before the group handler ran.
  - **Mulligan fingerprint collision**: keep/mull option sets are identical
    across mulligan rounds, so round 2 looks like a re-present of round 1
    (false REJECTED; cap could exhaust on a triple mulligan). Real fix:
    plugin surfaces gameStateId/msgId in get_pending_actions and the
    fingerprint incorporates them for static-option families.
**Origin:** First live bridge sessions on Linux/Proton (2026-06-09). Two bot
matches and one ranked Brawl match surfaced a *class* of autopilot failures
that per-bug patches cannot close out. This doc is the durable plan for the
five framework changes that close the class.

---

## The failure class (what actually happened live)

The GRE bridge hands us **typed, authoritative data** — request type, message
id, candidate ids, min/max counts, payability, can_pass. The current pipeline
throws that structure away and round-trips decisions through human-readable
strings and heuristic counters:

```
bridge request (typed) → gamestate renders strings ("Cast X [OK]",
"Activate Ability: Y") → planner re-parses strings → legality re-derived by
substring heuristics → executor re-resolves names back to ids → progress
inferred from trigger-ping counts
```

Every live failure on 2026-06-09 was that round-trip leaking:

| # | Failure | Root cause | Band-aid shipped |
|---|---------|-----------|------------------|
| 1 | Legal `activate_ability` plans dropped as illegal | `"Activate Ability: X"` parsed as card name `"Ability: X"` | prefix fix (`7fb38d9`) |
| 2 | Unpayable casts submitted → cast/cancel livelock, UI machine-gunned | `[OK]` is a *text tag*; fallback auto-pick ignored it | tag-aware scorer + rollback memory (`7fb38d9`) |
| 3 | Escape hatch cancelled the user's own casts 0.5s after casting | "window repeated 7x" counts trigger *pings* (several/sec), not attempts; AutoRespond consumed the client request object while the GRE kept waiting → frozen targeting arrow | 12s age gate (`5427b45`) |
| 4 | Same dead window replanned + re-spoken every 2s (LLM + TTS spiral) | backstop re-forces `decision_required` with no notion of "given up" | given-up window sig (`4cfd730`) |
| 5 | Valid target picks dropped → targeting stall (Nurturing Presence) | `"select_target"` vs `"target_selection"` substring miss; `"Select target for X"` unparseable | context-type mapping (`7212120`) |
| 6 | Planning against ghost decisions (`pending='Select Targets'`, `bridge=None`) | log-derived decision vs bridge-pending disagree; no arbitration rule | none — needs item 4 below |
| 7 | AP toggle raced between UI and external automation | `toggle_autopilot` is stateful with no get/set | none — needs item 6 below |

The shipped band-aids are *defensive backstops* — keep them — but they
compensate for the architecture. The five changes below fix it.

---

## 1. Typed `PendingDecision` pipeline (single source of truth)

**The change.** One dataclass flows from bridge poll → planner → executor,
unchanged:

```python
@dataclass(frozen=True)
class DecisionOption:
    option_id: str          # stable handle: bridge index / instance id / grpId
    label: str              # display only — NEVER parsed
    payable: bool | None    # autotap solution exists (casts), else None
    meta: dict              # oracle text, P/T, zone — prompt enrichment only

@dataclass(frozen=True)
class PendingDecision:
    request_id: tuple[int, int]   # (gameStateId, msgId) — identity, see item 2
    request_type: str             # bridge enum name ("SelectTargets", ...)
    options: list[DecisionOption]
    min_select: int
    max_select: int
    can_pass: bool
    can_cancel: bool
    source_label: str             # "Nurturing Presence" — display only
```

- The **planner prompt** renders `options` as a numbered list; the LLM answers
  with `option_id`(s), not card names.
- The **executor** submits by `option_id`. No name→id re-resolution.
- **Legality checking is deleted**, not fixed: an answer outside `options` is
  rejected mechanically at parse time (re-ask once, then deterministic
  fallback *from the same option list*).
- The deterministic fallback picks from `options` honoring `payable` — the
  `[OK]`-tag heuristic dies.

**Why this is feasible now.** The plugin already serializes everything needed
(`get_pending_actions`: `actions`, `target_candidates`, `select_n_ids`,
`min/max`, shape flags — see CLAUDE.md "Bridge get_pending_actions
serialization status"). The structure exists at the *execution* layer
(`autopilot.py:4341-4429` resolves bridge candidates); it must be lifted to
*planning* instead of stopping at execution.

**Touchpoints.** `gre_bridge.py` (build `PendingDecision` in the poller),
`action_planner.py` (new prompt builder + option-id answer parser; delete
`_is_action_legal` family for decision windows), `autopilot.py` (executor
takes `option_id`s), `gamestate.py` (strings become display-only).

**Kills:** failures 1, 2, 5 wholesale; most of 6.

---

## 2. Request-identity progress tracking

**The change.** Every guard keys on `request_id = (gameStateId, msgId)`:

- Submitted a response to request M → M is *settled pending outcome*.
- M disappears / new request arrives → **ADVANCED** (progress).
- M re-presents (same identity re-sent by GRE) → **REJECTED** (real signal).
- "Stuck" is *defined*: ≥2 REJECTED outcomes for the same request id.

No more window signatures built from mutable fields (`gameStateId` churns
every cycle, so cross-window loops looked like fresh windows), no more
trigger-ping counters (several pings/sec made everything look "repeated 7x"
instantly).

**Touchpoints.** `autopilot.py`: replace `_priority_window_signature` /
`_window_repeat_count` consumers; plugin already returns the original
message ids in `request_payload` (verify `msgId` is surfaced explicitly —
small Plugin.cs addition if not).

**Kills:** the detection half of failures 3 and 4.

---

## 3. Per-request submission state machine

**The change.** A tiny FSM per `request_id`:

```
PENDING → SUBMITTED → ADVANCED | REJECTED | ROLLED_BACK
```

Rules:
- **One in-flight submission per request.** A second submit for the same
  request id cannot fire until the first has an outcome. Machine-gunning
  becomes structurally impossible — the runaway rate limiter becomes a
  never-fires assertion.
- Every outcome is attributed: PayCosts-cancel marks the originating cast's
  request **ROLLED_BACK** (replaces the name-keyed rollback dict).
- The AutoRespond escape is a transition permitted **only** from a request
  with ≥K REJECTED outcomes. It can never again fire on a request nobody has
  attempted (failure 3's destructive half).
- ROLLED_BACK casts are excluded from the next `PendingDecision.options`
  for the rest of the turn (replaces `_filter_rolled_back_casts`).

**Touchpoints.** `autopilot.py` (new `RequestTracker` class; `process_trigger`
and `_execute_action` consult it), `gre_bridge.py` (poller feeds outcomes).

**Kills:** failures 2, 3 (prevention half), runaway class entirely.

---

## 4. One state arbiter (bridge-authoritative)

**The change.** A single function produces the canonical `PendingDecision`
(or `None`); nothing else reads `_bridge_request_type` / `pending_decision` /
`legal_actions` directly:

```
def arbitrate(bridge_poll, log_state) -> Optional[PendingDecision]:
    if bridge.connected:
        return from_bridge(bridge_poll)      # nothing pending → None. Full stop.
    return from_log(log_state)               # fallback only when bridge is DOWN
```

- Bridge connected + nothing pending → **there is no decision**: no planning,
  no advice, no TTS, regardless of what stale log parsing says.
  (Doctrine already in CLAUDE.md — "empty bridge action list is
  authoritative" — but enforced today in scattered call sites that each get
  it slightly wrong.)
- The standalone backstop force-fires only when the arbiter returns a
  decision whose `request_id` hasn't been settled (ties into items 2/3 —
  replaces the given-up-window sig).

**Touchpoints.** new `decision_arbiter.py` (or `gre_bridge.py`),
`standalone.py` (trigger loop + backstop), `autopilot.py` (entry gate),
`coach.py` (advice path uses the same arbiter so coach and autopilot can
never disagree about whether a decision exists).

**Kills:** failure 6, the backstop half of failure 4.

---

## 5. Stall corpus → CI regression harness

**The change.** Every MANUAL REQUIRED / REJECTED / ROLLED_BACK automatically
appends a fixture to `~/.arenamcp/stall_corpus/`:

```json
{
  "pending_decision": { ...the PendingDecision JSON... },
  "planner_answer":   { "option_ids": [...], "raw_llm": "..." },
  "outcome":          "REJECTED",
  "context":          { "turn": 7, "phase": "Main1", "commit": "7212120" }
}
```

- `tools/eval/replay_stalls.py` replays the corpus through planner-parse +
  executor-resolve (no live game needed — everything is ids and structures
  after item 1) and asserts: answer ∈ options, submission builds, fallback
  deterministic.
- Curated fixtures get promoted into `tests/fixtures/stalls/` and run in the
  normal pytest suite. Each of today's seven failures would have been a
  one-line fixture instead of a live debugging session.
- The plugin's replay recorder (`enable_replay`) covers the deeper layer when
  a GRE-level repro is needed.

**Touchpoints.** `autopilot.py` (fixture dump on terminal outcomes — the bug
report path already snapshots most of this), new `tools/eval/replay_stalls.py`,
`tests/test_stall_corpus.py`.

---

## 6. (Bonus) Idempotent control plane

`toggle_autopilot` → `set_autopilot {"enabled": bool}` + `get_status`
returning `{enabled, state, pending_request_id, last_outcome}` over the pipe
protocol (`pipe_adapter.py`, `coach_tab.py`). Toggles raced the UI against
external automation twice on 2026-06-09 (AP flipped off mid-stall by a state
probe). Keep `toggle_autopilot` as a deprecated alias.

---

## Sequencing & effort

| Phase | Items | Effort | Risk | Unblocks |
|-------|-------|--------|------|----------|
| A | 4 (arbiter) + 6 (control plane) | ~half day | low | kills ghost-decision planning immediately; safe alone |
| B | 1 (PendingDecision) | ~1-2 days | medium — touches planner prompt + executor | deletes string-matching class |
| C | 2 + 3 (identity + FSM) | ~1 day | medium — replaces live guard logic | deletes loop/runaway class |
| D | 5 (corpus harness) | ~half day | low | locks it all in CI |

Order matters: A is standalone and immediately valuable. B defines the types
C keys on. D last, once the types are stable.

**Migration safety:** each phase keeps the current defensive backstops
(rollback memory, age gate, given-up windows, runaway breaker) until the
replacement is proven live; they become assertions/telemetry afterwards, not
behavior. Re-run the `tools/eval` harness after B (planner prompt shape
changes — see CLAUDE.md "When to re-run the eval").

## Acceptance criteria (the class is closed when…)

1. A full bot match completes hands-off with zero MANUAL REQUIRED events.
2. Grep proof: no decision-path code parses a display string
   (`"Cast "`, `"Activate"`, `"Select target"`) — strings render *from*
   structures, never back *into* them.
3. Submitting twice to the same request id is impossible by construction
   (unit test asserts the FSM refuses).
4. With the bridge connected and idle, the coach emits zero planning calls
   and zero TTS for log-derived ghost decisions (arbiter test).
5. The stall corpus replays green in CI, including fixtures for all seven
   2026-06-09 failures.
