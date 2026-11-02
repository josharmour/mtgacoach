# BUG-273 — attack sequencing neglects available protection or pumps that change blocking/lethal

Verdict: **no production change needed; the protection half of this ID is merged and now
pinned at the bridge's submission point.** On master an attack that only feeds an untapped
blocker is held *unless* an affordable instant-speed play could change that combat, and
"affordable" means the mana is actually untapped and the card is actually castable in the
combat window. The new file drives the supported precombat sequence (untapped Plains, `{W}`
protection instant in hand) through `_prepare_attack_submission` — the pass every attack
takes before it is sent — and contrasts it with the hold control, two unavailable-protection
controls (land tapped, wrong colour) and a sorcery-speed control, without ever forcing an
all-in. Verification only: `7 passed` for the new file, `98 passed` for the guard/protection
regression set, `191 passed` for the other consumers of the same guard. No source commit;
merged to local master as `ca5ef9f` (receipt commit `2ad966e`).

## Symptom and evidence

Queue row 22: "Attack sequencing neglects available protection or pumps that change
blocking/lethal." Existing work recorded in the queue: "Bounded combat and tactical guards
exist; complete sequencing remains partial." Acceptance: reproduce one supported
precombat/protection sequence with actual mana and legal windows, contrast hold, nonlethal
and unavailable-protection controls without forcing all-in attacks.

Evidence read before touching code:

* `docs/backlog-triage-2026-11-01.md:432` — `| #273 | OPEN | Strategy / attacks |`.
  Historical label only.
* `docs/BACKLOG_STAGE_2026-11-01.md:136` — "#273/#278/#284 packets remain missing". The
  stage doc claims no closure for this ID either way.
* `../autopilot-improvements.md` is absent from this checkout and
  `backlog-evidence-2026-11-01.md` has no #273 row. Recorded rather than treated as a
  missing trace.
* `tests/test_color_protection_273.py` already covers protection *mechanics* (printed and
  Arena-granted protection, blocking restrictions, damage prevention, lifelink/trample,
  memo invalidation, future projection, crackback, GRE grant serialization, line-search
  expiry, untargeted destroy) but has no precombat sequencing case and never runs the
  bridge's submission pass.

## What the guard does now (the supported sequence)

`combat_strategy.py`:

* `_possible_tricks(state, local, attacker_ids)` (`737`) lists instant-speed plays visible
  to us: hand instants/flash filtered by `combat_trick_mana.missing_trick_color`, plus
  graveyard flashback/jump-start/retrace/escape, prepared creatures and activated
  abilities. A card that cannot be paid for in this window never enters the list.
* `losing_attackers(...)` (`1449`) judges each attacker on its own. It builds
  `affordable = [(text, cost, identity) … if cost <= mana]` (`1552`) from the untapped mana
  we actually have, then returns `{}` — keeping the whole attack — when any affordable
  trick's text matches `_SAVES_ATTACKER` (`1566`), which includes "gains protection from".
  Plain pumps are not blanket-excused: they are collected as `pumps` and judged per attacker
  by `_doomed`, so a pump that does not stop the answering group does not save the attack.
* The hold reason names the mechanism: "… kill it and survive; nothing gets through, it has
  no attack or death payoff, and no affordable trick could change that combat".

`autopilot_bridge.py`:

* `_prepare_attack_submission(...)` (`1166`) applies that guard at `1195` unless the action
  is a line push, then thins the survivors by `attack_tax.affordable_entries` (`1243`). This
  is the single pass every declared attack goes through, so pinning it here pins what is
  actually sent.

## Controls on master

| Control | Board | Result |
|---|---|---|
| protection available | untapped Plains + Gods Willing in hand | attack kept (`{}`, sent `[500]`) |
| hold control | same board, no trick | held (`{500}`, sent `[]`) |
| unavailable — land tapped | trick in hand, Plains tapped | held |
| unavailable — wrong colour | trick in hand, only a Mountain untapped | held |
| unavailable — no legal window | protection at sorcery speed | held |
| partial attack | 2/2 doomed + 5/5 that beats the blocker | only the 2/2 held (sent `[504]`) |
| partial attack with protection | same two attackers + affordable trick | both kept (sent `[500, 504]`) |

The last two rows are the "without forcing all-in attacks" half of the acceptance: the guard
thins a doomed attacker out of a larger attack instead of holding everything or sending
everything.

## Verification

```
$ .venv_mac_311/bin/python -m pytest tests/test_attack_protection_273.py -q
7 passed in 6.66s

$ .venv_mac_311/bin/python -m pytest tests/test_attack_protection_273.py tests/test_losing_attack_guard.py tests/test_attack_holds_20261019.py tests/test_combat_guards_20261019.py tests/test_color_protection_273.py -q
98 passed in 4.63s

$ .venv_mac_311/bin/python -m pytest tests/test_all_in.py tests/test_attack_push_20261019.py tests/test_cant_be_blocked.py tests/test_oops_emote.py tests/test_pinger_attack_20261019.py tests/test_session_land_and_mobilize.py tests/test_shang_attack_120_replay.py tests/test_sneak_all_in_20261019.py tests/test_walker_attack_guard_20261019.py tests/test_wide_attack_guard_20261019.py -q
191 passed in 27.61s

$ .venv_mac_311/bin/python -m ruff check tests/test_attack_protection_273.py && .venv_mac_311/bin/python -m ruff format --check tests/test_attack_protection_273.py
All checks passed!
1 file already formatted
```

The second block is every test file that names `losing_attackers` plus the existing #273
protection file; the third is the remaining consumers of the same guard (all-in forcing,
line push, can't-be-blocked, walker and wide-attack guards). Full output:
[focused-tests.txt](focused-tests.txt).

## Remaining limits

* **The cast itself is still the planner's job.** The guard decides whether an attack is
  worth declaring given an affordable trick; nothing deterministically casts that trick in
  the precombat main before the declaration. `cast_notes.trick_timing_note` (`99`) only
  fires for pumps matching `gets +N/+N until end of turn` (`89`), so a protection spell cast
  before combat gets no timing note at all. That sequencing gap is real and is left as-is:
  it needs a prompt/planner change with its own reproduction, not a guard change.
* **Protection credit is not colour-checked.** Any affordable text matching
  `_SAVES_ATTACKER` keeps the whole attack; a "protection from <quality>" trick is credited
  without checking that the quality covers the answering group. A colourless blocker or two
  differently-coloured blockers can still answer it. Not changed here — it would narrow a
  deliberately conservative abstention and is outside this acceptance.
* **Reconstructed board.** The historical #273 packet is absent from this checkout (stage doc
  line 136), so the tests reconstruct a precombat main rather than replaying hidden GRE
  responses.
* No live qualification against a real Arena match.
* No Linux CI receipt for this commit yet.

## BLOCKER

Independent review and a real Linux CI run are still required before this item can move past
`merged_awaiting_review`. Nothing here was verified by its author.
