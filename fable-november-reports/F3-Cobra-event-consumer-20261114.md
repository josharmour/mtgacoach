# Cobra event-only consumer compatibility

A narrow predicate consumes the exact retained approved-pilot Lotus Cobra shape
at actual land entry for an already-present body in `Moves.sources`. It requires
the complete printed one-mana landfall sentence, explicit matching landfall
subject, a single one-mana AddMana effect, no conditions, and an unchanged normal
cast mode/cost with no additional costs. The otherwise ambiguous `any` spelling
is bound to that complete source sentence, not accepted as a general mana type.

No compiler record is mutated. Landfall is not renamed to ETB; no cast-time
record semantics are enabled. Existing generic cast/effect classification remains
conservative. This improves only this event path, not full Cobra planning or F3.
No mana is added without a land entry or without an existing body, and temporary
mana is not persisted as a rock. The original source/record sample is retained
hash-identical; compiler Unknowns and unsupported compound events still abstain.

224 focused offline tests passed (new actual-record source transition and
negative cases, full line-search suite and existing compiler-search adapter).
No new inference occurred. OS wrapper unavailable in this session; no fresh
OS-contained claim. Independent review required before integration or acceptance.

## Independent rework: unknown obligations and confidence

040f655 was independently rejected: preserved unknown schema obligations on the
record, entry, effect, mode or cost could be ignored, and confidence=failed could
enter the new event path. The corrected predicate requires exact high confidence
and recursively refuses preserved `_unknown_fields` throughout all nested
record dataclasses (including body and mana metadata). It neither removes nor
normalizes those obligations. The exact seven independent failing cases now
pass, alongside distinct-body mana accounting and positive real-record tests.
243 combined tests pass locally; independent successor review remains required.
No actual model call or record regeneration occurred.
