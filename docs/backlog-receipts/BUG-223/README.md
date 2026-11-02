# BUG-223 — decision latency / background contention

**Verdict: measurement coverage added; no production source change required on
master `78fa182`.**

The scheduling mechanism for this item already exists and is exercised at the
behaviour level (`tests/test_proxy_call_classes.py`: shared lane, supersede,
drop, foreground bypass, env kill switch). What was missing is the
acceptance-required *measurement* fixture: nothing proved that queue delay,
client setup and generation are separable in one metrics record, or that an
urgent call keeps its budget while background work holds the lane. That gap is
closed by `tests/test_latency_223_background_contention.py`.

## Acceptance map

| Acceptance point | Evidence |
|---|---|
| Measure separate queue/network/generation delays with synthetic fixtures | `test_lane_wait_is_measured_separately_from_setup_and_generation`: one record shows `lane_wait_ms` 263.2, `client_setup_ms` 0.0 and generation (`ttft_ms - client_setup_ms - lane_wait_ms`) 0.2 ms |
| Urgent work meets its budget | `test_urgent_decision_never_queues_and_keeps_its_budget`: a `decision.combat` call returns inside its 1 s budget with `lane_wait_ms == 0` and `total_ms <= 1000` while a `background.game_plan` call holds the lane |
| Abandoned work cancels | `test_abandoned_background_work_cancels_and_the_replacement_completes` (status `cancelled`, reason `superseded`, stream closed) and `test_dropped_background_call_reports_the_queue_delay_it_spent` (status `dropped`, nothing sent to the model) |
| A useful plan can still complete | Same supersede test: the replacement returns usable content with status `ok` and `lane_wait_ms < 100`, i.e. a supersede takes the lane at once instead of queueing behind the abandoned call |

## Commands and results

```
$ .venv_mac_311/bin/python -m pytest tests/test_latency_223_background_contention.py -q
4 passed in 6.41s

$ .venv_mac_311/bin/python -m pytest \
    tests/test_latency_223_background_contention.py \
    tests/test_proxy_call_classes.py tests/test_proxy_cancellation.py \
    tests/test_proxy_request_metrics.py tests/test_proxy_priority.py \
    tests/test_proxy_backend_timeout.py -q
76 passed in 40.70s

$ .venv_mac_311/bin/python -m ruff check tests/test_latency_223_background_contention.py
All checks passed!
$ .venv_mac_311/bin/python -m ruff format --check tests/test_latency_223_background_contention.py
1 file already formatted
```

## Decomposition evidence

One synthetic record from the new fixture (a deck-playbook call queued behind a
held game-plan call for ~250 ms):

| Field | Value |
|---|---|
| `client_setup_ms` | 0.0 |
| `lane_wait_ms` | 263.2 |
| `ttft_ms` | 263.4 |
| `first_content_ms` | 263.4 |
| `total_ms` | 263.5 |
| generation (`ttft - setup - lane`) | 0.2 |

The same fields are emitted on every `[PROXY] Request metrics` line, including
cancelled and dropped calls (`proxy._finish_cancelled` records them), so a live
receipt can attribute delay per call class without log-adjacency guessing.

## Remaining limits

- **No live qualification.** This session may not push or deploy, so there is no
  real-gateway receipt under load; the fixtures use fake streams and prove the
  accounting and scheduling contract, not model TTFT under contention.
- **Priority reorders the waiting queue only.** Background calls send
  `priority: 10` while decisions send nothing (server default 0), so decisions
  overtake *queued* background work in vLLM — but a game plan that is already
  generating is not evicted. `protect()` deliberately skips
  `background.game_plan` so BUG-141 plans can finish; measuring whether a
  running plan still delays an urgent first token needs a live run.
- **Linux CI receipt pending**, same blocker as CI-PERF: this session cannot
  push to trigger the hosted workflow.
