# F3 explicit printed-field validation, 2026-11-14

Bounded deterministic source correction on29654dd. Existing validator checked name/header mana cost but never compared record.type_line with printed source; an explicitly declared `cast` mode also bypassed cost comparison when its own mana field was empty.

Now type_line must match under the existing NFC/case/whitespace normalization, and an existing cast mode must retain the printed mana cost even when blank/zero. Alternate modes are unchanged; absent cast modes remain absent rather than invented; empty printed cost still matches empty recorded cost. No card-name rules, new schema, inferred effects or source-record edits. This prevents mismatched explicit fields from being silently accepted but does not solve missing Mox cast modes, relational mana, Fury/Cobra Unknowns or F3 sample quality.

62 synthetic/compiler/schema tests pass with OS network denial, including11 new explicit-field controls and unchanged neighbors. Initial newtest import attempted Cost from package rather than ability_synthesizer; corrected test import only. Ruff clean. Independent review required; no actual compilation/inference/export, no fullF3 gate claim.
