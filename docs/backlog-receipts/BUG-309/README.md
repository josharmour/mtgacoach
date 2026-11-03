# BUG-309 — Gameplay / phase lost across equipment selection

**Verdict: verification-only for production code.** Both acceptance halves are
already merged on master; the only unpinned hop was the planner call that stores
the corrected reasoning for a *chosen* equip. This receipt adds that regression
and changes no `src/` file.

Merged to local master as 5ff82b9 (receipt commit 7fcca7f).

## Symptom and evidence

Queue row: "Nested equipment selection forgets combat has passed and promises an
attack now." Acceptance: carry intent through real nested requests with current
phase, source and expiry; disallow a Main2 attack promise while retaining valid
shroud/mana-relay reasons.

Evidence located:

- `docs/backlog-triage-2026-11-01.md:468` — `#309 PARTIAL Gameplay / phase lost across equipment selection`
  (the only occurrence in the triage/stage/evidence docs).
- `../autopilot-improvements.md` — absent from the repo, the same unresolved
  evidence path recorded for BUG-273/275/300/307/308. Treated as a documented
  limitation, not a blocker.
- `docs/BACKLOG_STAGE_2026-11-01.md` and `docs/backlog-evidence-2026-11-01.md`
  have no `#309` row.

## Existing merged work reused (no production change)

1. **Intent across nested requests** — `src/arenamcp/activation_intent.py`
   (`ActivationIntent`, `matches`, `observe_activation`, `activation_intent_note`)
   carries ability/source/turn/phase/lifetime through `SelectTargets`, `SelectN`,
   `Search`, `Group`, `CastingTimeOptions` and `OptionalAction`, expires after
   60 s, resolves ability objects through their parent, and never rewrites the
   current legal menu. Pinned by `tests/test_activation_intent_309.py`.
2. **Phase-aware prompt note** — `equipment_opportunity_note` in
   `src/arenamcp/equipment_opportunities.py` says "Before combat, that can enable
   an attack or a tap ability" in Main1 and "Combat has passed: haste can enable
   a tap ability, not an attack this turn" in Main2.
3. **Main2 attack promise disallowed** — `game_plan.decision_claim_notes` emits
   "The normal attack declaration window has passed; haste does not create
   another combat phase…" for a post-combat haste attack claim, and
   `corrected_decision_reasoning` drops the contradicted sentence and appends the
   fact. Pinned by `tests/test_equipment_phase_309.py`.

## Remaining gap reproduced on master (bd56f34)

The two existing test files call the helpers directly. Nothing asserted the hop
that actually stores the reasoning: `ActionPlanner._note_false_claims`
(`src/arenamcp/action_planner.py:10163`, called from the typed path at
`:9818`). Probe on master with a real `ActionsAvailable` equip menu (Lightning
Greaves, summoning-sick Notary Hobbits, Main2):

```
decision_claim_notes("Equip Greaves so the Notary Hobbits can attack now.", state)
  -> ['The normal attack declaration window has passed; haste does not create another combat phase. ...']
corrected_decision_reasoning(...)
  -> 'Rules correction: The normal attack declaration window has passed; ...'
planner._note_false_claims(state, decision, ["idx:0"])
  -> stored reasoning = 'Rules correction: ...'
     trace = {'uncorrected_reasoning': 'Equip Greaves so the Notary Hobbits can attack now.',
              'false_claims': ['The normal attack declaration window has passed; ...']}
```

So the behaviour is correct; only the end-to-end pin was missing.

## Change

New file `tests/test_equipment_phase_planner_309.py` (6 tests) drives
`ActionPlanner._note_false_claims` with a real equip decision:

| Case | Expectation |
| --- | --- |
| Main2 "…can attack now" | sentence replaced by the timing fact; `uncorrected_reasoning` + `false_claims` recorded |
| Main2 "…can tap for mana" | reasoning unchanged, trace empty |
| Main2 "for shroud protection" | reasoning unchanged, trace empty |
| Main2 "…can attack next turn" | reasoning unchanged, trace empty |
| Main2 "combat has passed so this is only for shroud" | reasoning unchanged, trace empty |
| Main1 "…can attack now" | reasoning unchanged, trace empty |

## Verification

See `focused-tests.txt`: new file 6 passed; the six-file equipment/intent set
107 passed; `ruff check` clean; `ruff format --check` clean.

## Remaining limits

- The retained log packet (8b7bed14 T12) is not bundled; these are equivalent
  board regressions, not a replay reconstruction.
- The claim check is text-level: it fires on an attack word in a post-combat
  equip reason and abstains on negations, future turns and explicit additional
  combat. It does not model whether an additional combat phase is actually
  available beyond that wording.
- No live qualification and no Linux CI receipt.

**BLOCKER:** independent review and Linux CI are still required; nothing here is
verified by the author.
