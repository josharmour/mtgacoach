# Attack-tax planning recurrence #210

The 4159449e T11 declaration sent Badgermole Cub, Arboreal Grazer, Mutable
Explorer and Mutavault at the opponent after Finale of Devastation resolved.
Dáin's public Enduring Story tax required four mana. Arena offered only Field
of Ruin's one mana after those attackers were selected. Cancellation and
withdrawal worked; this patch addresses the earlier unaffordable selection
and forecasts. It makes no claim about the match result or a forced win.

## Reproduction

`tests/fixtures/attack_tax_210_finale_public.json` contains public battlefield
state reconstructed from full GRE state304 in the monitor's
`4159449e-finale-tax-raw.json`. That is the recovery state after cancelling
the cost. Held attack flags are cleared solely to reevaluate the four observed
proposed attackers. Text comes from the installed Arena database; the observed
Finale/Craterhoof haste and trample grants are retained. No hidden hand or
library contents are supplied. This is not a full pre-Finale sequence replay.

The prior integrated revision `5d59144` submits all four IDs
`[453, 467, 500, 506]` through `_prepare_attack_submission` with the same fixture.
The new budget rejects that combination and permits a subset. Holding
Mutavault back makes its mana available, including the visible Badgermole Cub
bonus. The mana that floated during Main1 is not carried into combat.

## Changes

- Preserve Arena's public positive Storied/Enduring Story annotation as a
  player fact, including player diffs and checkpoint restore. It does not
  disappear when the qualifying permanents leave. Missing evidence remains
  unknown; current board counts do not invent historical acquisition.
- Parse generic per-attacker defensive taxes from Oracle clauses, adding
  stacked costs and respecting player/planeswalker scope, current controller,
  phasing, the untapped-source condition and the public Story condition.
- Both combat searches and the tactical main-phase hint reject subsets whose
  supported sources cannot pay. Selected attackers cannot also serve as
  ordinary tap-mana sources. Mana creatures tapped for the payment are not
  also counted as crackback blockers in that projection.
- Submission preserves a payable intended choice. Otherwise it chooses a
  payable subset before issuing commands. Mandatory attackers can decline
  paid attacks, but a free legal planeswalker recipient still satisfies their
  attack requirement. Old retained selections omitted by the new subset use
  the existing acknowledged withdrawal path instead of a blind empty submit.
- Coach warnings, raw lethal labels and solver suggestions share the budget.
  Multi-turn lines explicitly report unsupported attack payments/source
  tapping; legacy fallback clocks cannot republish the same free-attack
  lethal or survival claims. Even a currently tapped conditional tax can
  become active later, so it does not license free future attacks.

## Bounded semantics

This is a supported lower bound for public mana, not a complete payment
engine. It counts recognized unconditional tap abilities, basic land types,
fixed/recognized variable quantities and the recognized creature-tap bonus.
It omits floating pools whose spending restrictions are not serialized,
Treasure/sacrifice payments, restricted/conditional/costly mana abilities,
aura/doubler bonuses and vigilance attackers tapping during payment. Unknown
conditions, X/colored/additional attack costs do not become free attacks.
General self-taxing creatures and arbitrary continuous/replacement effects
are not represented. The bounded subset search can choose fewer attackers
than another legal sequence; it does not prove every omitted line impossible.
Arena remains the authority on payment and declaration legality.

## Rule references

The official [Hobbit release notes](https://magic.wizards.com/en/news/feature/the-hobbit-release-notes)
confirm that Enduring Story persists for the player, that Dáin taxes attacks
at its controller, and that paying the tax is not required merely to satisfy
an attack requirement when no free recipient is available.
The [Comprehensive Rules](https://media.wizards.com/2026/downloads/MagicCompRules%2020260807.pdf),
106.4 and 508.1f–j, cover mana emptying, attacker tapping, determining attack
costs, mana abilities and payment. Current declarations are evaluated separately
from mana that existed in a prior step.
