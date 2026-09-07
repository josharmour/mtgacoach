# ws13 — Encoder/action compatibility evidence — IMPLEMENTATION NOTES
# Base: a956ac1. Status: fixture collection design; NO parity claims until
# authoritative fixtures exist.

## Goal (task 13, re-scoped per reviewer contract)
Build a SMALL VERSIONED corpus of (Arena-shaped state, authoritative XMage-emitted
feature/action vector) pairs and prove exact parity — not hash-overlap claims.

## Diverse case list (10 minimum, both seats)
1. both seats symmetrical — same state encoded from each local_seat_id
2. active/priority change — same board, priority flips
3. duplicate cards — two identical-name permanents with distinct instance ids
4. stolen permanent — controller != owner
5. tapped/sick creature — is_tapped + is_summoning_sick
6. stack target — spell targeting a permanent on stack
7. unknown opp hand — no cards_in_hand / top-level counts absent
8. supported card baseline — full encoder coverage for simple vanilla creature
9. unsupported card — novel/unknown card must take the encoder's official unsupported path
10. combat agnostic — attack/block flags cross-check

## What counts as authoritative (extracted fragment: available evidence)
- /home/joshu/repos/magezero/src/magezero/dataset.py: rows are [N, A+4] float32 with
  [policy(A), resultLabel, stateScore, isPlayer(0/1), actionType]; batch tuple
  (idxs, offsets, policies, values, is_players, action_types). A+4 column layout IS the
  reference contract. Any parity fixture must store per-row: indices list, action_type int,
  is_player int, and the model's own value — NOT a Python encoder recomputation.
- Authoritative fixtures come from XMAGE-JAVA emitted logs (the training writer). We do
  NOT regenerate fixtures with the Python encoder. If the reference writer can't be
  reproduced, we document the blocker and mark compatibility UNVERIFIED.
- Fixture provenance: source log SHA256 + java artifact/version hash + parser version +
  encoder_version + action_schema_version from the model manifest (task04 contract v2).

## Contract alignment (inherited)
- action_schema_version, encoder_version, checkpoint_hash flow through ws04's manifest v2.
- Comparison must be per-row EXACT (indices AND action slots AND value perspective
  (perspective=actor per task04 semantics)), including collisions and the 128-slot layout.

## Files (planned)
- tests/fixtures/xmage_parity/corpus.jsonl   — versioned fixture records w/ provenance
- tests/test_encoder_parity.py               — skipped until fixtures exist; documents blocker
- tools/eval/build_parity_fixtures.py        — parser from XMage training logs (this task)

## Explicit non-claims
- No "encoder parity established" statements until fixtures land. Tests ship as
  xfail/skip with reason "authoritative-fixtures-missing".
- No live fleet changes to obtain fixtures (logs copied read-only).
