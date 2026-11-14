# F3 bounded compiler contract preparation, 2026-11-14

Candidate source only on96a8fa7; no actual compilation, inference, original-output rewrite or full F3 claim.

The current TokenSpec cannot natively represent token colors or affirmative legendary status. Compiler admission now rejects CreateToken near-fits when printed text explicitly contains a create...color/legendary...token clause. The retry requires the entire source sentence as verbatim Unknown, preserving unsupported semantics rather than guessing a schema extension. Unknown-only source preservation passes; existing simple Treasure creation and unrelated red/destroy text are not rejected. The detector is intentionally bounded and is not a rules engine: unrecognized wording is not certified. Mixed token clauses conservatively require Unknown because current output has no source-span binding; no gameplay action is banned or engine semantics changed.

Compiler prompt also states that loyalty costs, optional physical exile, linked mana and conditional resolutions must remain explicit/Unknown if native fields cannot express them. No deterministic Fury/Cobra effect guessing or Mox rainbow-mana substitution was added. Full compiler/source coverage remains unfinished.

59 OS-denied tests pass:8 new contract/mock-retry tests plus unchanged compiler/schema neighbors. Initial sparse-worktree test setup lacked tools/compile_card_facts.py; copying the unchanged tool restored that neighbor test, no assertion weakened. Ruff clean. Tests use scripted responses, not new actual compiler records. Original five retained outputs untouched. Independent review required; F3 full25/deck/60 sample and authorized new-output evidence still missing.
