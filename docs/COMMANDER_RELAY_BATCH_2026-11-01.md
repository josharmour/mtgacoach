# Commander and equipment follow-up integration

Installed baseline: `5612969a9453d441e2a1ef070e6a6edaca2223d1`.
Frozen preceding batch: `e34c06258dbe78095f9573188d33f07ff0042797` remains unchanged; its #350/#308/#329 fixes are ancestors of this branch.
Branch: `codex/next-commander-relay`. No live master, app, bridge, settings or queue changes.

## Added commits and scope

- Source `ece79d1`, integrated `6f95d60`: #329 Drannith/current outside-hand cast restrictions across commander facts, projection, search and plan validation. Keeps identity/cost visible; hand casts and resolving spells remain unaffected. Removal can unlock a later modeled turn.
- Source `6f26de8`, integrated `7e53da1`: Greaves/Notary mana relay context, current raw Arena cost evidence preserved separately from legal choices, bounded reconsideration of false no-tap-payoff claims. No artificial legal cast or automatic equip policy.
- Source `2e2384c`, integrated `979626a`: commander replacement destination, recovery timing and known cast tax comparison. The retained Memory Lapse incident returns to library, not directly to hand: five mana after recovery versus seven from command. No forced destination choice.

Planner composition retains #350 protection reconsideration, #349 final cast-payoff correction, then equipment-payoff correction. No other review/deadline mechanism was replaced.

## Verification and failure classification

Combined 47-file run: 1,283 passed and five failed in60.02s. Two failures are CPU timing limits; the existing Fblthp shorthand/library-dependency test fails; an exact75.0-second assertion does not allow deadline preparation time; the commander optional-choice test allowed a background planner to consume its mocked responses.

The timeout test now checks a positive timeout bounded by75 seconds, preserving the actual deadline requirement. The commander handoff test disables its unrelated background manager so its two-response correction sequence is deterministic; production scheduling is unchanged. Four source files were formatted with AST equivalence verified.

The Fblthp shorthand gap predates these commits and remains unresolved: `Fblthp unblockable chip damage` is retained despite that named library card being unavailable. Other explicit/ambiguous shorthand controls remain in the focused checks. This batch does not claim all plan-dependency validation is fixed. Worker reproduced this failure on untouched4bf8faf; neither the alias helper nor this assertion is changed here.

Focused rerun:213 passed, three failed (existing Fblthp shorthand and both timing cases). The corrected timeout and commander handoff tests pass. Hand-only searches now preserve the prior own-body restriction cache, adding opponent identity only when outside-hand spells exist;18 restriction tests pass afterward, while both timing cases still exceed240ms (281.991/300.985ms). The final pre-legal-batch timing comparison and activation qualification are recorded in the external readiness artifact. No complete7000+ suite, CI, external private-data inference or live-game qualification was performed.

## Remaining limits

- Drannith: same-turn removal-then-cast search, conditional restrictions and arbitrary ability-layer exceptions remain incomplete; noncreature restriction sources are fixed in bounded search.
- Equipment relay: exact colorless-per-tribe effects, current targets and generic/colorless quoted hand costs only. Fresh menus must validate every action; no guaranteed execution or optimal-Ugin claim.
- Replacement: narrow rendered English single-commander hand/library prompts, known prior cast counts; current modifiers and complex library placement remain separate. Neither branch is forced.
- Preceding #308 remains conservative unsupported-boundary protection, not full multi-spell/horizon sequence ranking. Earlier timing volatility remains disclosed.

Activate only after a coordinated Coach closure; Arena may remain open for these Python-only changes. Do not close or launch either app automatically.

The final pre-legal comparison (production-equivalent to frozen e34c062, before the three new legal commits) also failed both unchanged timing limits in4.30s. These timing failures are shared with the prior batch under current machine conditions; they are not claimed fixed or caused by this integration. No timing threshold was relaxed. The batch has no identified new functional regression, with the three existing failures explicitly qualified.
