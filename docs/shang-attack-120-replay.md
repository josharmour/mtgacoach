# #120 / #84 Shang-Chi replay (2026-11-01)

Base: `4bf8fafcff2c09d1d404a4c8c6f4d486c90abf58`. The observed incident ran on
`f4aaacd5`; this is an offline reproduction on the later base, not evidence of
another live incident on that base.

## Evidence and qualification

Match `b717d4e1`, turn 9, 06:43:58 UTC: Shang-Chi 449 was the only offered
attacker. It was a 2/2 without first strike, deathtouch, evasion, or an attack
payoff. Its haste permission applies to activating abilities, not attacking.
Black Widow 478 and Captain America 462 were untapped 4/4s. Black Widow had first
strike; its summoning sickness did not prevent blocking. State 203 records four
first-strike damage from 478 to 449, then Shang-Chi's battlefield-to-graveyard
transfer, with no attacker damage or blocker death. “They likely won't trade a
4/4 for a 2/2” therefore incorrectly described a free kill as a trade.

Source packet directory:
`/Users/joshu/Documents/Codex/2026-10-31/task-2/monitoring/`:

- `b717d4e1-shang-attack-board.json`: reconstructed public battlefield at 195.
- `b717d4e1-shang-attack-raw.json`: GRE diffs and damage/zone evidence.
- `b717d4e1-shang-attack-oracles.json`: verified three combatants' abilities.
- `b717d4e1-shang-attack-candidate.json` and `...-runtime.json`: menu, decisions,
  payment options before mana was spent, and successful attacker submission.

The checked-in `tests/fixtures/combat/b717d4e1-shang-public.json` retains all 17
public battlefield objects, current raw stats/tapped/sickness flags and Arena
ability IDs, enriched with the installed local Arena card database's Oracle
text. Life totals 25/24 come from the decision trace. The fixture keeps the
combat-relevant hand instant, Tamiyo's Safekeeping; it omits the two non-flash
creature cards and unrelated zones. Empty mana pools are a replay assumption
at the declaration boundary, consistent with the final menu offering only
Pass and all green lands and Firdoch Core being tapped. This is a qualified
public-board reproduction, not a recovered complete normalized runtime state.
Titan's earlier chosen shield counter is absent from the board packet; it is
not reconstructed. Titan and Rhino are summoning sick and not offered attackers.

The match later ended in a win after Coach cleanup/manual control. This attack
cost a creature; these records do not establish a match or rank loss.

## Reproduction and change

On the unmodified base, the combat resolver correctly predicts Shang-Chi dies,
no blocker dies and no damage gets through. `combat_choice` holds all attackers.
But `losing_attackers` returns no objection with Safekeeping in hand, and rejects
449 when that instant is removed. The initial regression run was **1 passed,
1 failed**, specifically the guard rejection assertion.

The guard counted two untapped sources (Shang-Chi and Library of Alexandria),
ignored colors/restrictions, and therefore treated `{G}` Safekeeping's
indestructibility as a saving trick. Library supplies colorless; Shang-Chi's
mana is restricted to activating creature abilities and the attacking creature
cannot also tap during combat. None of the sources can cast Safekeeping.

The guard now omits ordinary hand tricks when a required color is provably
unavailable. The proof overestimates potential colors and preserves unknown
sources, rituals/untaps, alternate payment, cost modifiers, hybrid/Phyrexian
costs and free spells. Mandatory attacks, actual attack payoffs, available green
protection and legitimate combat trades remain exempt. Bridge preparation with
an explicit player recipient now removes this exact attacker without issuing
any command. One existing Giant Growth test needed an actual green source;
its previous blue-only fixture could not pay the purportedly affordable spell.

## Boundaries

This is not a complete payment solver: quantities, mutually exclusive colors,
conditional sources, graveyard/prepared/activated tricks and complex payment
chains retain conservative legacy behavior. The line-push bypass is unchanged.
The LLM may still produce the incorrect trade rationale; this fix prevents the
ordinary recorded submission path from acting on it. No live commands,
installation, shared-ledger edits, or full-suite run are part of this change.

## Validation

Final focused batch: **121 passed in 10.57s** using Python 3.11:

```text
PYTHONPATH=src /Volumes/repos/mtgacoach/.venv_mac_311/bin/python -m pytest \
  tests/test_combat_trick_mana.py tests/test_shang_attack_120_replay.py \
  tests/test_losing_attack_guard.py tests/test_autopilot_bridge_attackers.py \
  tests/test_attack_override.py tests/test_combat_guards_20261019.py \
  tests/test_pinger_attack_20261019.py tests/test_wide_attack_guard_20261019.py -q
```

Ruff lint and format checks passed on all five changed Python files;
`git diff --check` passed. Intermediate failures were the deliberately failing
baseline, the blue-only Giant Growth fixture, and two new-test representation
mistakes (uppercase keyword values; expecting IDs rather than blocker objects).
Those were corrected before the final successful batch. No production resolver
change was necessary. The full suite is reserved for the integration owner.
