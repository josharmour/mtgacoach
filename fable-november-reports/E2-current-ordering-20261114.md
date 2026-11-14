## Native field ownership correction (review pending)

Independent review rejected d03b3e1: a custom DeckPlaybook subclass could echo
captured original text but supply empty, invented, truncated or duplicated string
partitions. Partitioning now requires the exact native DeckPlaybook class and
calls its base field renderer directly. Native string fields and an in-range
native-integer index set are required; joining the complete fields in original
order must equal the captured block exactly. Only then are whole fields grouped
locally by their source-owned indices. Custom subclasses/providers stay unsplit.
This does not infer qualifier ownership from a line multiset or trust an
arbitrary parts method. Exceptions, malformed fields/indices, and changed
rendering all retain the exact captured context.

295 focused/neighbor tests pass, including all four independent custom-string
repros; separate native-parts-override and custom-subclass controls added.
Original multiline field adjacency tests remain unchanged. No inference,
integration or cache/performance/full-E2 acceptance claim; review required.

---

## Partition type correction (review pending)

Independent review rejected `2e2c58b` because a three-element malformed renderer
result could unpack successfully while dropping the context (None fields) or
raising later in the final join (list/dict fields). All three returned fields
must now be native strings before equality/partition acceptance; otherwise the
exact captured context is retained unsplit. Actual-prompt regressions cover all
three cases alongside prior provider/renderer/unpack failures. This validates the
optional renderer's type boundary, not arbitrary malicious renderer semantics.
No integration, inference or acceptance waiver.

---

## Optional partition failure correction (review pending)

Independent review rejected b7b8c9c because an optional second provider lookup
could abort the actual decision prompt despite an already valid captured strategy.
Provider lookup, parts rendering and unpacking now fall back to the exact captured
unsplit block on exceptions. No private text or exception detail is logged.
This only guards optional partitioning, not initial strategy acquisition.

New actual-prompt tests cover provider exceptions, renderer exceptions and invalid
parts shape; captured multiline context occurs exactly once and decision options
remain. Including the two independent repros and prior focused suites:285passed.
No inference, integration or gate waiver. Renewed independent review required.

---

## Current-master recovery (2026-11-14; renewed review pending)

Reviewed ordering source `95b349b` was absent from master. Recovered exactly the
eight-file net diff `f916a88..95b349b` onto `1b150f0`; no rejected line-prefix
splitter or older E1/E4 ancestor is imported. It applies without conflicts.
This changes actual system/user prompt ordering when loaded, not a dormant flag.
Structural whole-field playbook rendering and exact captured-context equality
fallback remain; all field/option/permanent/warning content must remain intact.

Focused cache-order, duplication, situational rules, match-context, game-plan,
deck-strategy, A3, E4 and corrected E1 suites: 280 tests pass in8.72s with fake
backends; Ruff clean. This local run is not claimed OS-contained. No live
inference, server setting, deployment or restart occurred. Independent semantic
re-review and full CI are required before any integration.

This is not full E2 acceptance: the actual GamePlanManager system instruction
prefix still differs; shared user blocks do not establish shared system prefix.
No real cached-token/TTFT before-after result or E3 prerequisite completion is
claimed. All earlier rejection/correction and evidence limitations remain below.

---

# E2 current-master ordering replay — source candidate only

Base f916a88f3e9e88deed2dd05740116c7282e984e9 (contains T1 cancellation
admission). Applied only the six-file information-ordering/test delta from
fable-nov/base..18a61c3 using clean three-way application. No probe/credentials
code, server flags, model calls, or old unrelated runtime code imported.

Changes: strategic policy first for typed and declaration system prompts;
reference head, playbook core, seen rules, variable tail and candidate rules
before current decision content; game-plan seed between reference head/rules.
Rendered lines preserved; multiline continuation tests retained. This does not
create shared decision/GamePlanManager system content. Do4/Do5 conflict and
actual per-class cached-token/TTFT before-after gates remain OPEN.

Tests in exact source copy on Mac, OS deny-network:
- prompt-cache order, duplication, situational rules, T1 cancellation admission:
  36 passed in6.84s.
- match context, game plan, game-plan parse, A3 outcome:145passed in1.86s.
Ruff and diff-check pass. Sources/tests only, no inference; fixture card cache
may initialize locally under confinement. No model-quality/cache-performance
claim. Broad full suite not run (board incomplete). This candidate requires
independent source review; no integration or activation performed.

Private logs: /Users/joshu/e2-ordering-test-20261114/e2-focused.txt and
e2-neighbors.txt. Original E2worktree untouched. No acceptance waiver or E3
prerequisite completion follows from this compatibility replay.


## Independent rejection and correction

7d3e205 was rejected: prefix parsing could detach schema-valid multiline plan
qualifiers beginning RULE, Mechanism or Unless, and move rule continuations
beginning Primary plan into the core. Byte retention alone was insufficient.

Corrected renderer now derives partitions from complete fields at their source
in DeckPlaybook, never from rendered text prefixes. Existing decision_context
joins the same original field sequence; decision_context_parts returns that
original plus stable/variable groups. Embedded newlines remain inside the field.
Planner accepts these parts only when original rendering equals its captured
context; unknown/custom/changed providers remain unsplit. Opaque helper now
always preserves the entire context. Removed unsafe text-classification contract
tests and replaced them with stricter unsplit/structural-boundary assertions.

Real DeckPlaybook.parse tests cover both directions of label collision; actual
public decision prompt tests assert full multiline primary field remains
contiguous with its qualifiers for all three collision prefixes, while tax/window
changes still occur after stable reference. Existing ordinary multiline and
source-information checks remain. 220 focused+adjacent tests, including complete
deck-strategy file, pass under OS deny-network in9.11s. All original E2 evidence
and scope gates remain open. Independent re-review required.
