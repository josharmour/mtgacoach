# BUG-300 — draft rationale: restricted tutor outcomes

Verdict: merged to local master as MERGE COMMIT (receipt commit SOURCE COMMIT).
Draft pick and alternative prose that claims a restricted library search finds a
card type its own rules text excludes is now corrected from supplied card data.
The chosen pick, its ranking and the original diagnostic are preserved.

## Symptom

Queue row: "Generated explanations invent card costs, mana, modes, targets or
tutor outcomes." Triage row `docs/BACKLOG_TRIAGE_2026-11-01.md:459`:
`#300 PARTIAL Draft factual rationale errors`.

## Existing work reused

`src/arenamcp/draft_facts.py` already corrected printed costs, simple mana
production, one printed generic discount and free-equip attribution;
`DraftAdvisor._validate` applies those corrections to pick and alternative
reasons and keeps `original_reason`. The cast-stage half of #300 was merged
separately (`docs/NEXT_348_300_349_2026-11-01.md`). Tutor outcomes were only
checked in gameplay (`tutor_claims.correct_tutor_claims`, called from
`action_planner.py`), never in draft prose.

## Remaining gap reproduced on master c743ae7

`reason_corrections("Enlightened Tutor finds our best creature.", ENLIGHTENED,
[ENLIGHTENED])` returned `[]` even though the supplied oracle text is "Search
your library for an artifact or enchantment card, reveal it, put it into your
hand, then shuffle." The same held for "Finale of Devastation finds a land."
against supplied text restricted to "a creature card with mana value X or less".

## Change

- `_tutor_types` reads the first restricted "search your library[/graveyard] for
  … card" clause and returns the card types it names; an unrestricted search
  ("for a card") or an unrecognized restriction returns an empty set.
- `_tutor_corrections` flags a claim that the named card
  "finds/fetches/tutors/searches for/gets" a type outside that set and emits
  "<name>'s search finds <allowed> cards; it cannot find <claimed> cards."
- Wired into `reason_corrections` for every supplied card, so both pick and
  alternative reasons pass through it.

## Controls (no rewrite)

| Reason | Why it is left alone |
| --- | --- |
| Enlightened Tutor finds our best enchantment. | Claimed type is allowed. |
| Enlightened Tutor finds an artifact or enchantment. | Both types are allowed. |
| Enlightened Tutor could find a creature if we splash blue. | Qualified ("if"). |
| Enlightened Tutor doesn't find creatures. | Negated claim. |
| Enlightened Tutor finds our best creature removal. | Ambiguous noun, not a card-type claim. |
| Demonic Tutor finds our best creature. | Supplied text is unrestricted ("for a card"). |
| Finale of Devastation finds our best creature. | Claimed type is allowed. |

## Verification

```
$ PYTHONPATH=. .venv_mac_311/bin/python -m pytest tests/test_draft_facts.py -q
43 passed

$ PYTHONPATH=. .venv_mac_311/bin/python -m pytest tests/test_draft_facts.py \
    tests/test_draft_advisor.py tests/test_draft_narration.py \
    tests/test_draft_captured_facts.py tests/test_limited_rules_cube.py \
    tests/test_draft_unrated_fallback.py tests/test_draft_autoplay.py -q
206 passed

$ .venv_mac_311/bin/python -m ruff check src/arenamcp/draft_facts.py tests/test_draft_facts.py
All checks passed!
$ .venv_mac_311/bin/python -m ruff format --check src/arenamcp/draft_facts.py tests/test_draft_facts.py
2 files already formatted
```

Full command output: `focused-tests.txt`.

## Remaining limits

- Only the first restricted search clause in a card's text is read; compound or
  plural restrictions beyond the named card types abstain.
- Claims about "permanent"/"spell", mana-value-only restrictions, and claims
  without the card name at a sentence start are not checked.
- No live qualification and no Linux CI receipt; independent review is still
  required. This receipt does not claim verification.
