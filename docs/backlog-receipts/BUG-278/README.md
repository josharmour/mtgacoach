# BUG-278 — defender-role plans hold despite a supported surviving attack

**Verdict: verification-only.** No production source change was needed. The
defender label is not wired to any consumer that can veto an attack
declaration, and the guards that do decide hold-vs-attack already compare net
lifelink and crackback. This receipt pins that behaviour with a new regression
file and records the bounded gap that remains.

Merged to local master as `MERGE COMMIT` (receipt commit `SOURCE COMMIT`).

## Symptom and evidence

Queue row: *"Strategy / attacks — Defender-role plans repeatedly hold despite a
supported surviving attack in a losing race."* Existing work: *"Existing
race/attack guards provide partial protection but do not establish this sequence
is corrected."*

Evidence located on master:

| Source | Line | Text |
| --- | --- | --- |
| `docs/backlog-triage-2026-11-01.md` | 437 | `#278 \| PARTIAL \| Strategy / attacks` |
| `docs/BACKLOG_STAGE_2026-11-01.md` | 136 | `#273/#278/#284 packets remain missing … no historical closure claimed` |
| `docs/latency-222-id-audit-20261101.json` | — | no entry with id 278 |
| `git log --oneline --all --grep=278` | — | empty |

The queue row's evidence paths (`../autopilot-improvements.md`, a backlog-evidence
row) do not exist in this checkout, and the historical packet is missing, so the
boards below are reconstructed from the production modules rather than replayed.

## What the production path actually does (verified on master)

| Consumer | Location | Effect on a declaration |
| --- | --- | --- |
| `_role` | `src/arenamcp/board_assessment.py:3448` | `all_in` returns `ROLE_AGGRESSOR` **before** the "behind" branch, so a `defender` label implies some defensive line survives their next attack |
| `survival_mode` | `board_assessment.py:1329` | role participates, but `race == "behind"` alone already sets it; it never reads a declaration |
| `role_guard` | `board_assessment.py:3733` | fires only on `ActionsAvailable` picks and "never overrides a land, creature, removal, counter, life gain or pass" — it cannot touch declare-attackers |
| `_posture` | `src/arenamcp/line_search.py:966` | ranks hold against attack from simulated lines (net lifelink and crackback are inside those numbers); no role input |
| `attack_push` / `attack_line_note` | `src/arenamcp/line_guard.py:780`, `:907` | driven by the searched posture only; no role input |
| `_check_attack_crackback` | `src/arenamcp/action_planner.py:2170` | trims or empties an attack by life math (`life + lifelink − worst-case crackback`) only |
| `survival_mode` consumers | `src/arenamcp/game_plan.py:598`, `:951`, `:2615`, `src/arenamcp/line_search_guard.py:326–400` | trim card-draw first when defending, annotate a plan line, or steer an `ActionsAvailable` pick — none can veto a declaration |

So the label reaches prompt text and plan trimming, never the declaration itself.

## Reproduction (reconstructed boards)

Grid over our bodies × their bodies × our life × their life through the real
modules (`assess`, then `attack_push`, then the crackback guard):

* 1199 boards came back labelled `defender`;
* of those, **24** had a searched posture of `attack` — i.e. the label does not
  veto the search's verdict;
* on **2** of them (`2× 4/4` against `2× 3/3`, opponent at 10, us at 11 or 12)
  the empty declaration is pushed to both attackers and the crackback guard keeps
  them;
* **0** defender-labelled boards were found where the crackback guard empties an
  all-in declaration.

Pinned push board (`2× 4/4` vs `2× 3/3`, us 12, them 10):

```
role='defender' survival_mode=False all_in=False lethal_now=False posture='attack'
posture_reason='attack with Ours0, Ours1: survives (life 9, 9, 9; opponent at 2);
                hold: survives (life 12, 12, 12; opponent at 10)'
push names=['Ours0', 'Ours1'] ids=[100, 101] applies=True setting=on
kept=['Ours0', 'Ours1'] guard_hold=False fallback='' advice=''
```

Pinned reject board (`2× 1/1` vs `4× 2/2`, us 6, them 20):

```
role='control/stabilize' survival_mode=True all_in=False lethal_now=False posture='hold'
posture_reason='attack with Ours0, Ours1: dead on T9; hold: dead on T11'
push=None
kept=[] guard_hold=True fallback='planner_crackback_guard'
advice='Holding Ours0 and Ours1 back: not lethal this turn, and their crackback would leave us at -2.'
```

## Controls

| Board | Role | Posture | Push | Crackback guard |
| --- | --- | --- | --- | --- |
| push, us 12 vs them 10 (`2×4/4` vs `2×3/3`) | defender | attack | applies, ids `[100,101]` | keeps both attackers |
| push, us 11 vs them 10 (same bodies) | defender | attack | applies, ids `[100,101]` | keeps both attackers |
| reject, us 6 vs them 20 (`2×1/1` vs `4×2/2`) | control/stabilize | hold ("dead on T9" vs "dead on T11") | none | empties the declaration |
| defender + attack with value gap under `ATTACK_PUSH_MARGIN = 6.0` (`line_guard.py:742`) | defender | attack | none — the declared hold stands within margin | untouched |

The last row is deliberate conservatism in the push guard (it also requires a
robust +2/+0 trick proxy), not a role veto: the search's own margin for calling
a posture is `POSTURE_MARGIN = 3.0` (`line_search.py:124`) while the push needs
twice that gap before overriding a model declaration.

## Verification

New file `tests/test_defender_hold_278.py` (3 tests) pins:

1. the defender-labelled push board at both life totals — role, posture,
   prompt verdict text (`Verdict: ATTACK.`), push verdict ids/setting, and that
   the crackback guard keeps both attackers with no fallback;
2. the reject board — posture `hold`, prompt verdict text (`Verdict: HOLD.`
   plus the "tapped now does not make an attack free" paragraph), no push, and
   the crackback guard emptying the declaration with
   `fallback_reason == "planner_crackback_guard"`.

Commands and output are in [focused-tests.txt](focused-tests.txt):

* new file alone — **3 passed**;
* combat/attack guard set (`test_crackback_guard`, `test_attack_push_20261019`,
  `test_race_role_20261019`, `test_lifelink_crackback_20261025`,
  `test_attack_line_guard_20261019`, `test_line_guard`) — **124 passed**;
* adjacent consumers (`test_color_protection_273`, `test_board_assessment`) —
  **197 passed**;
* role prompt text (`test_role_text_20261019`) — **2 passed**;
* ruff check + format check on the new file — clean (ruff 0.15.22).

## Remaining limits

* The historical packet for #278 is missing (`docs/BACKLOG_STAGE_2026-11-01.md:136`);
  these boards are reconstructed from module behaviour, not replayed from a match.
* No live qualification: nothing here exercises a real MTGA client.
* The push guard's conservative band above is unchanged by this item; if a future
  report shows a hold standing where the search prefers attacking by less than
  6.0 value, that is a separate tuning question with its own reproduction.
* **BLOCKER:** independent review and Linux CI are still required before this row
  can leave `merged_awaiting_review`. Nothing here was verified by its author.
