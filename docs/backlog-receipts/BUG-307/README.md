# BUG-307 — death-return engine undervalued in removal valuation

## Verdict

Merged to local master as MERGE COMMIT (receipt commit SOURCE COMMIT). Production change:
`board_assessment._spell_value` no longer credits a removal spell with killing the drain
engine of a creature that returns as an enchantment. Block valuation already retained the
engine (#270/#284) and needed no change; the removal half was the open gap.

## Symptom and evidence

Queue row: "Gameplay / death-return engine undervalued in blocks — Death-return creatures or
engines are treated as gone or harmless after their body dies."

Evidence located:

- `docs/backlog-triage-2026-11-01.md:466` — `| #307 | PARTIAL | Gameplay / death-return engine undervalued in blocks |`
  (only occurrence in the triage doc).
- `docs/BACKLOG_EVIDENCE_2026-11-01.md` — no `#307` row (grep returned nothing).
- `docs/BACKLOG_STAGE_2026-11-01.md` — silent on `#307`.
- `../autopilot-improvements.md` — does not exist anywhere under `docs/`, the same unresolved
  evidence path as #273/#275/#300. Treated as a documented limitation, not a blocker.

## Existing work reused (no duplicate patch)

- `src/arenamcp/persistent_returns.py` — `returned_enchantment`, `engine_value`,
  `return_credit`, `annotate_return_credit` (visible exile / suppression withholds credit).
- `src/arenamcp/combat_solver.py` — `_material` / `_death_material` subtract the retained
  engine credit, so a block trade already keeps the drain/draw engine and loses only the body.
- `src/arenamcp/line_search_moves.py` — search material counts body + engine for a live board
  and the residual engine for a returning one.
- Pinned by `tests/test_retained_engine_value_284.py`, `tests/test_combat_persistence_284.py`,
  `tests/test_held_removal_floor_20261019.py`.

## Remaining gap reproduced on master (before the change)

Probe against real code paths (`board_assessment._spell_value`, removal spell "destroy target
creature" vs a 2/2 drain body with Enduring-Tenacity-style return text):

```
with return   : 7.0     # engine counted as removed although it returns as an enchantment
no return     : 7.0
exile blocked : 7.0     # correct: visible exile really kills the engine
```

The returning body was valued exactly like a plain one, i.e. removal was credited with
killing an engine that survives. After the change:

```
with return   : 5.0     # body only (power 2 + removal base 3)
no return     : 7.0     # power 2 + engine role 2 + base 3
exile blocked : 7.0     # credit withheld, engine counted as removed again
```

## Change

- `src/arenamcp/board_assessment.py`: new `_keeps_engine(body)` helper (returns False when
  `_return_credit_blocked` is set, else asks `persistent_returns.returned_enchantment`) and the
  removal/bounce threat line now reads
  `b["power"] + (0.0 if _keeps_engine(b) else engine_role(b))`.
- New regression file `tests/test_death_return_removal_value_307.py`.

## Controls

| Case | Result |
| --- | --- |
| Drain body with return text | removal worth body only (5.0) |
| Same body without return text | full value (7.0) |
| Return body with visible exile (`_return_credit_blocked`) | full value (7.0) |
| Draw-engine return body (`engine_role == 0`) | unchanged (5.0 both ways) — no invented discount |
| Block half: `_material` / `_death_material` on a returning drain body | 10.0 / 6.0 — engine credit retained, body lost |
| Block half: plain drain body | 7.0 / 7.0 — full loss |
| Mixed board (returning + plain drain body) | plain body still wins the "most dangerous target" pick |

## Verification

See `focused-tests.txt`: new file 5 passed; valuation/search regression batch 442 passed;
plan/line batch 296 passed; ruff check and format clean.

## Remaining limits

- Only the printed self-return rule is recognised (`persistent_returns._return_rule`); blink,
  reanimation and "return at end of turn" wording abstain.
- The whole engine role is withheld for a returning body, including token-making activations
  that a noncreature enchantment may not be able to use; deliberately conservative.
- Historical raw packet is not bundled; boards are reconstructed from the ledger row.
- No live qualification and no Linux CI receipt in this environment.

## BLOCKER

Independent review and a Linux CI run are still required; nothing here is verified by the
author.
