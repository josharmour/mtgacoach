# F3 scalar effect native types, 2026-11-14

Compiler-only source preparation onf50b81e. Extends existing raw admission to the native integer fields of GainLife, Draw, Damage, Pump, AddMana and PutCounters. Previously those decoded raw values unchanged, allowing stringX/Boolean/fraction/null to masquerade as a typed numeric effect. New admission rejects these types before decoding, using existing retry/failure flow; a mock variable-damage response retries to verbatimUnknown rather than guessing an amount.

Valid integers, signed Pump, omitted native defaults, unknown extensions and Unknown effect text are preserved. This is type validation, not range/printed-value/effect legality proof. Relational mana, loyalty and unsupported lifecycle semantics remain unimplemented; no original actual outputs changed and no new actual sample exists.

102 OS-denied tests pass (37 new scalar-field/retry controls plus65 previous compiler/schema/native-type tests). Ruff clean. Independent review required; no inference, customer serving, or fullF3 sample/acceptance claim.
