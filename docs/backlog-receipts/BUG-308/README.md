# BUG-308 — Gameplay / removal and draw-trigger sequencing

## Verdict

Verification-only for production code. The bounded same-turn adversarial
comparison merged earlier already satisfies every clause of this row's
acceptance on current master: both legal cast orders are compared from one
public state, the enemy draw-punisher response is enumerated adversarially,
once-per-turn usage is preserved, and protection is preserved. No reproduced
unsupported sequence was found inside the supported shape, so this item adds
no source change and no new test file; it records the reproduction and the
bounded gaps that remain outside that shape.

Merged to local master as 5758e9b (receipt commit f0f50c8).

## Symptom and evidence

Queue row: `docs/autopilot-active-work.json` → `BUG-308`, "Removal, creature
entry, draw and enemy-trigger sequences are evaluated in the wrong order."

Evidence located:

- `docs/backlog-triage-2026-11-01.md:467` — `| #308 | OPEN | Gameplay / removal and draw-trigger sequencing |`
- `docs/backlog-triage-2026-11-01.md:88` — "#308: one new current-turn engine/entrant sequence compares spell orders and …"
- `docs/backlog-triage-2026-11-01.md:60` — "#308 and #329 remain active after narrower repairs."
- `docs/BACKLOG_STAGE_2026-11-01.md:109` — "#311/#308: object-bound blink and public trigger/response consequences get …"
- `docs/backlog-evidence-2026-11-01.md:49,51,61,74` — source commits `20fcae8` / `a7c6508`, integrated as `b9c9dd5`; 409 affected local tests passed; "#308 remains open".
- `../autopilot-improvements.md` does not exist under `docs/` (same unresolved evidence path as BUG-273/275/300/306/307). Treated as a documented limitation, not a blocker.

## Existing work reused (no production change)

- `src/arenamcp/entry_draw_transition.py` — `resolve()` enumerates one entry event through one draw engine and one opposing punisher; branches are returned in a stable order for the caller to score adversarially and the helper never chooses one. Abstains via `UnsupportedContinuation` on unknown characteristics, ward payment, target restrictions, multiple engines, unmodelled death/return, and Army creation without a token factory.
- `src/arenamcp/entry_draw_projection.py` — bounded current-turn projection used by the search.
- `src/arenamcp/public_sequence.py` — `TriggerUseMemory` (parent-linked observed draw-trigger use) and `public_sequence_concerns()` recheck.
- `src/arenamcp/sequence_value.py` — bounded outcome vectors (`sequence_values`) plus prompt notes (`sequence_value_notes`).
- Planner wiring: `action_planner.py:~4394` observes trigger use per decision; `~9091` appends the outcome vectors to the option prompt; `~9512` records them in the decision trace; `~9904` rechecks a chosen rationale against observed use.

## Reproduction on current master

Probe (`/tmp/probe308.py`) drives the real modules with the retained board from
`tests/test_entry_draw_search_308.py` (Enduring Innocence + Orcish Bowmasters +
Answer + Test Recruit), not a historical replay.

1. Same-state order comparison — `search_lines(...)` ranks
   `Answer → Enduring Innocence → Test Recruit` at v=34.80 (draws=1) above
   `Test Recruit → Enduring Innocence` at v=33.30 (draws=0), and
   `evaluate_plan` reproduces both orders from that one result:
   removal-first v=34.80 draws=1 persistent=[]; entry-first v=20.00 draws=1
   persistent=['Enduring Innocence'] army=[('Orc Army', 1)]; entrant-first
   v=33.30 draws=0.
2. Enemy response — the entry-first leaf keeps the engine as an enchantment
   after the ping kills its body and shows the opposing Army at 1 counter,
   i.e. the punisher's branch is scored rather than assumed away.
3. Once-per-turn — with the trigger unknown, `sequence_values` emits both
   branches ("if draw trigger unused; Orcish Bowmasters pings us": life −1,
   draws 1, Army +1) and ("if draw trigger already spent": life +0, draws 0).
   With the engine id marked spent it emits only the spent branch.
4. Recheck — a rationale claiming "Enduring Innocence draws us a card" while
   that trigger is observed spent yields exactly one concern ("…was already
   observed this turn. This entry cannot trigger that same object again…");
   an acknowledged rationale ("already drew this turn") yields none.
5. Protection — with the entrant carrying "Protection from black", the ping
   target list is `[None (us), 70 (Enduring Innocence)]`: the protected entrant
   is omitted as a legal target while the engine still dies and returns as an
   enchantment (`died=(70,)`).

## Controls

| Control | Observed |
| --- | --- |
| Removal before entry | ranked first, draws 1 |
| Entry before removal | ranked lower, draws 0 |
| Entrant before engine | ranked above entry-first, draws 0 |
| Enemy ping on us | life −1, draws 1, Army +1 |
| Enemy ping on entrant | entrant removed |
| Trigger already spent | draw branch suppressed |
| Trigger unknown | both branches emitted |
| Rationale claims a spent draw | one concern raised |
| Rationale acknowledges the spend | no concern |
| Entrant protected from black | not a legal ping target |

## Verification

See `focused-tests.txt`: 157 passed across the nine #308 files; 554 passed with
the line-search / board-assessment / sacrifice-continuation neighbours; ruff
check clean and ruff format clean on the touched modules and tests.

## Remaining limits

- The search abstains outside the current turn, so later-turn entry-draw chains
  are still unmodelled.
- The prompt vector projects only fixed-damage removal as its alternative;
  exile/destroy removal is modelled by the search but not by that vector.
- The prompt vector's ping targets are us or the entering creature only; other
  legal targets are not projected there.
- `TriggerUseMemory.observe` needs the stack ability text to carry the full draw
  clause; effect-only ability text leaves use unobserved (conservative: both
  branches stay available).
- The historical packet is not bundled here; these are equivalent board
  regressions built from the ledger row, not a claim of complete replay
  reconstruction.
- No live qualification was run.

**BLOCKER:** independent review and a Linux CI receipt are still required for
this item. Nothing here is verified by its author.
