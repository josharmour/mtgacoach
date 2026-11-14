# F3 compiler native-type admission, 2026-11-14

Source-only on9a57c6d. Mox relational color/physical exile and loyalty still need exact producer/consumer lifecycle contracts; adding a field or guessing fixed mana is not safe. No implementation of those semantics is claimed.

A separate deterministic compiler gap is now guarded before permissive legacy decoding: explicit native cost integer counts cannot be fractional/string/bool, native booleans cannot be truthy strings/numbers, token count is integer, token power/toughness integer-or-null, and explicitly supplied native cost/token/filter containers must be objects. This prevents `"false"` becomingTrue or1.5 becoming1. Missing/default fields, null section compatibility, alternate modes, Unknown payloads and unrecognized extension keys remain unchanged; unknown extensions are not executable proof. Types alone do not validate numeric meaning, printed completeness or effect legality.

Compiler retries through its existing bounded path; no decoder-wide compatibility change, schema expansion, original record rewriting, inference or customer deployment.65 OS-denied tests pass (14new plus51unchanged compiler/schema), including actual compile_one fake-backend retry before coercion and preserved valid/Unknown values. Ruff clean. Independent review required; full F3 sample and semantic acceptance remain open.
