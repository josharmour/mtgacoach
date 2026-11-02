# BUG-270 — blocking undervalues creatures that return as noncreature enchantment engines

Verdict: **no production change needed; the body-versus-engine split this ID names is
merged and pinned.** On master a dying return creature is valued as two separate things:
the creature body it actually loses, and the residual engine it keeps as a noncreature
enchantment. The block search compares those two numbers, so an equal body trade that
keeps a draw engine is affordable while the same trade without the return is not. Exile
and death-trigger suppression withhold the credit, counters and combat state do not
survive the zone change, and an ineligible or unknown identity earns nothing. Verification
only: the two #270/#307 files pass on master (`37 passed`), together with the three other
files that consume the same helper (`34 passed`). No source commit; merged to local master
as `<merge>` (receipt commit `<source>`).

## Symptom and evidence

Queue row 21: "Blocking undervalues creatures that return as noncreature enchantment
engines." Existing work recorded in the queue: "Returned identity/type/owner cleanup and
body-versus-engine facts are merged." Acceptance: compare block/hold on a retained return
case — lose the creature body but preserve only the actual returned engine — and handle
exile, counters and conditional return accurately.

Evidence read before touching code:

* `docs/backlog-triage-2026-11-01.md:429` — `| #270 | PARTIAL | Strategy / blocks |`.
  Historical label only.
* `tests/test_combat_persistence_284.py` docstring — the retained case is Enduring
  Innocence 275 (2/1) allowed to block Kaito 247 (4/5) at 16 life, from
  `gameplay-ultra-audit.json` G03, `d1bad78e` T7. That audit file and that commit are both
  absent from this checkout, so the test reconstructs the recorded combat facts rather
  than replaying hidden responses — exactly what its docstring claims.
* `../autopilot-improvements.md` is absent from this checkout and
  `backlog-evidence-2026-11-01.md` has no #270 row. Recorded rather than treated as a
  missing trace.
* There is no per-ID audit JSON for #270 (the only audit JSON here covers the latency IDs),
  so this receipt works from the triage row and the merged tests.

## What the block valuation does now

`persistent_returns.py`:

* `returned_enchantment(card)` builds a **fresh** permanent: `type_line: "Enchantment"`,
  `card_types: ["CardType_Enchantment"]`, no power/toughness, no counters, no attachments,
  no damage, untapped and not in combat, controlled by its owner when the text says so.
  It returns `None` for tokens, copies, face-down or phased-out cards, noncreatures, and
  cards whose printed text lost the return ability — so a conditional return that cannot
  apply earns nothing.
* `return_value(card)` is the bounded residual engine value (draw engine 3.0,
  lifegain-drain 4.0, otherwise 2.0), gated on the printed "dies, if it was a creature"
  text so ordinary creatures never pay for return parsing.
* `return_credit(card)` signs that value by ownership: positive when the dying controller
  keeps the engine, negative when a stolen creature returns to its owner. Unknown
  ownership earns no credit.
* `annotate_return_credit(battlefield)` withholds credit when visible interference would
  prevent the return (death-trigger suppression, or an "exile … instead" replacement).

`combat_solver.py`:

* `_material(card)` (`151`) values body plus ongoing resources;
* `_death_material(card)` (`169`) is `max(0, _material(card) - return_credit(card))` — the
  loss from dying **after** the bounded retained-engine credit;
* `_material_of` caches that per search, so hundreds of fights over the same bodies do not
  re-parse printed text.

The consequence for blocking is the comparison the acceptance asks for: with the return,
the blocker's death costs body-minus-engine; without it, the same death costs the full
body. The pinned test drives both sides of that comparison.

## Controls on master

| Control | Where | Merged as |
|---|---|---|
| returned enchantment identity: fresh permanent, no body, no counters/attachments/damage/combat state, owner control | `persistent_returns.py:29` `returned_enchantment` | `1df203b`, `7403b0f` |
| body-versus-engine split in block valuation (`_death_material`) | `combat_solver.py:169`, `persistent_returns.py:130` `return_value` | `51e26ac` |
| exile / suppression withholds credit, refreshed per board | `persistent_returns.py:157` `annotate_return_credit`, `death_triggers.py:262` | `51e26ac`, `bacac2d` |
| stolen engine credited to its owner (negative for the thief) | `persistent_returns.py:136` `return_credit` | `51e26ac` |
| line-search nodes keep returned engines as persistent noncreature permanents | `line_search_moves.py:1569`, `2463`, `2916` | `bacac2d`, `6a62c78` |

All five commits are ancestors of local master (`git merge-base --is-ancestor` checked for
each).

## Verification

```
$ .venv_mac_311/bin/python -m pytest tests/test_retained_engine_value_284.py tests/test_combat_persistence_284.py -q
37 passed in 6.63s

$ .venv_mac_311/bin/python -m pytest tests/test_session_exact_closure.py tests/test_entry_draw_search_308.py tests/test_gain_drain_fast_path_20261101.py -q
34 passed in 1.32s
```

The acceptance points this ID turns on:

* **block versus hold on a retained case** —
  `test_retained_draw_engine_makes_safe_equal_body_trade_affordable` runs the real block
  search at both a tiny and a huge option budget: with the return text the plan blocks
  (`assignments == {275: 3}`) and both sides lose material 6; with the return text removed
  from the same card the plan holds (`assignments == {}`). That is precisely "lose the body,
  keep only the actual engine".
* **the body is never free** — `test_return_credit_never_makes_engine_body_free`: an 8/8
  engine still costs 18 to lose against material 21.
* **exile and suppression** — `test_visible_interference_blocks_credit_and_refresh_invalidates_memo`
  parametrizes three interference texts ("abilities don't trigger", "exile it instead",
  "exiled from anywhere instead"), asserts blocking stops being affordable while they are
  visible, asserts credit returns when they phase out, and asserts the memo is refreshed.
* **counters** — `test_return_is_fresh_and_owner_controlled_without_mutating_snapshot`
  gives the dying body three +1/+1 counters plus damage, tapped/attacking/blocking state
  and an attachment, then asserts the returned permanent has none of them while the input
  snapshot is unmutated. Body value still counts those counters because it reads effective
  power/toughness.
* **conditional return** — `test_no_return_for_noncreature_tokens_absent_or_lost_abilities`
  parametrizes noncreature types, empty types, tokens (three spellings), phased out,
  face down, copy and lost ability text; all yield no return and therefore no credit.
* **ownership** — `test_stolen_engine_returns_value_to_opponent_instead_of_retaining_it`
  asserts a stolen engine credits −3 to the thief.

Full output: [focused-tests.txt](focused-tests.txt).

## Remaining limits

* **No exact board replay.** The retained packet (`gameplay-ultra-audit.json` G03,
  `d1bad78e`) is absent from this checkout; the tests reconstruct its recorded combat facts.
* **Bounded valuation, not a rules engine.** Engine value is a fixed small number per role,
  not a simulation of what the engine will draw or drain before the game ends.
* **Interference detection is textual.** It recognizes visible suppression and "exile …
  instead" replacements; it does not model every replacement effect or trigger counter.
* No live qualification against a real Arena match.
* No Linux CI receipt for this commit yet.

## BLOCKER

Independent review and a real Linux CI run are still required before this item can move past
`merged_awaiting_review`. Nothing here was verified by its author.
