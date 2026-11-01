# Public sacrifice continuation (#280)

The old search removed creatures killed in combat without resolving visible
death-triggered sacrifices. Its next board could retain a creature that Grave
Pact forced its controller to sacrifice, and omit Tergrid's possible transfer.

`tests/fixtures/sacrifice_280_equivalent.json` is a constructed legal public
equivalent: Grizzly Bears attacks into Hill Giant while the attacker controls
Grave Pact and Tergrid, God of Fright. The old `76a50e0` transition retained the
opponent's Giant. The new transition sacrifices it and preserves the optional
transfer. This is **not** the missing historical #280 packet, a replay of its
T17/T19 plan, or proof that its game was winnable.

## Representation and boundaries

`sacrifice_continuation.resolve` returns a choice tree. The affected player
chooses each victim, each controller orders simultaneous triggers, and
Tergrid's controller can decline or accept a transfer. All legal alternatives
within the supported model remain in the tree. It reads public objects only.

The parsed clauses are whole Oracle abilities: the Grave Pact/Dictate form,
the self-or-another-creature form, and Tergrid's optional transfer form. No card
name selects a strategy. Chained deaths use the sources present immediately
before each event. Initial simultaneous combat deaths use last known
information; queued triggers survive source removal. New triggers are stacked
in active/nonactive player order, with the owner's ordering choices preserved.
Tokens can cause death triggers and be sacrificed but cannot be transferred by
Tergrid. A graveyard object can be transferred only while still there.

Transfers require explicit public printed power/toughness. The new body has
fresh identity, the receiving controller, its original owner and printed stats;
old counters, damage, attachments and temporary keyword grants do not follow it.
The normal summoning-sickness/entry bookkeeping receives its new identity.

Enumeration is bounded to 512 visited states and six simultaneous triggers per
controller. Exceeding either bound discards the projection instead of selecting
the first completed branch. The model is for two known controllers.

## Search integration

Own and opponent ordinary-combat continuations, including the opponent
policy's crackback valuation, consume the resulting board. The choice trees
remain available on `Moves.sacrifice_choices`. The selected continuation uses
public material minimax; the existing combat block solver is not retrofitted
with exhaustive multi-turn sacrifice valuation. These lines are explicitly
advisory. They cannot override a plan via the line guard or establish forced
race/concede conclusions. A selected legal line is not a claim that every
other block or victim choice loses.

Known unsupported interactions return reasons and no scored line: separate
first-strike steps, extra combats, pending stack/spell transitions, commander
zone choices, face-down victims, competing legends, conditional/extra death
effects, ETBs or continuous effects on transfers, copies without original
characteristics, sacrifice prevention/replacements, activated outlets and
delayed sacrifices. Unsupported search also disables the legacy fallback's
lethal/survival claims, since that fallback omits these same effects. Removing
the unsupported public condition or supplying missing printed stats permits a
fresh assessment; cache keys include those facts.

## Rule references and verification

Checked against the official [Comprehensive Rules, effective September 25,
2026](https://media.wizards.com/2026/downloads/MagicCompRules%2020260925.txt):
603.3b (controller ordering and APNAP), 603.10a (death/sacrifice lookback) and
113.7a (queued abilities independent of their sources). The official
[Kaldheim release notes](https://magic.wizards.com/en/news/feature/kaldheim-release-notes-2021-01-22)
confirm Tergrid's optional nontoken transfer and its dependence on the same
card remaining in the graveyard.

Focused regressions cover both controllers, alternative victims, optional
transfers, fresh characteristics, chained deaths, simultaneous source deaths,
APNAP with a source sacrificed before resolution, tokens, suppression,
prevention, exhausted bounds, memo refresh, and fail-closed public forecasts.
The integration owner owns the aggregate suite and deployment.
