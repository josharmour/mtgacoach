# ws04 — Honest served-model contract (Task 04) — IMPLEMENTATION NOTES
# Base: a956ac1 (checkpoint-1 remediation tip). Status: design + cross-repo fields frozen from
# Astra's amendments + live magezero runner.py/server.py evidence (read-only).

## Source evidence (read-only, live fleet NOT touched)
- magezero/src/magezero/runner.py:810-825 writes manifest_version:1 with model_id, deck, version, gen,
  format{}, gate_threshold (promotion, GATE_THRESHOLD), candidate_sha256, arch, dtype, trained_at.
  Missing per amendment: deck_counts, deck_hash, checkpoint_hash, encoder/action versions,
  certification evidence, promotion_status, capabilities.
- magezero/src/magezero/server.py: /evaluate (msgpack: indices+offsets, ignores "model"),
  /predict alias, /healthz (current_id = "{CURRENT_DECK}/ver{CURRENT_VERSION}"), /models
  (glob models/*/ver*/manifest.json, resident=[current_id], default=current_id). No request_index
  echo, no served model_id in responses, no checkpoint_hash.
- The server, when no manifests exist on disk, INVENTS a default manifest with gate_threshold 0.60
  and not even deck_counts — exactly the fabrication the amendments forbid. ws04 must remove that.

## Contract v2 (amended: inherits Astra's CHECKPOINT1-REVIEW amendments verbatim)

### Manifest (schema_version=2, one schema for runner/server/client)
{
  "schema_version": 2,
  "model_id": "UWTempo/ver2",
  "human_name": "UWTempo/ver2",              # informational only; cache/scoping uses checkpoint_hash
  "gen": 13,
  "deck": {
    "name": "UWTempo",
    "deck_counts": {...},                    # count-normalized per canonical hashing below
    "deck_hash": "<sha256 canonical>",       # over deck_counts per _canonical_deck_counts
    "size": 60,                              # mainboard counted; sideboard excluded
    "singleton": false,
    "commander": null
  },
  "format": {"family": "constructed", "variant": "standard"},
  "gate": {
    "deck_similarity_threshold": 0.60,       # selector input (was overloaded gate_threshold)
    "promotion_win_rate_threshold": 0.50,    # runner promotion gate (was overloaded gate_threshold)
  },
  "checkpoint_hash": "<sha256 of model.pt.gz bytes>",   # immutable model identity
  "encoder_version": "...",                  # from magezero encoder emit
  "action_schema_version": "...",
  "value_target": {                          # semantics from actual trainer, not a name alone
    "kind": "search_blended",
    "perspective": "actor",
    "range": [-1.0, 1.0],
    "terminal_handling": "win=1 loss=-1 draw=0",
    "truncation_handling": "bootstrapped_at_cap",
    "search_blend": {"strength": 0.35, "source": "mcts_visits"}
  },
  "promotion_status": "certified|uncertified|rejected",
  "certification": {                          # REQUIRED non-empty iff status == certified
    "evaluated_at": "...",
    "criteria_version": "...",
    "panel": {"decks": [...], "games": N, "wins": W, "losses": L, "draws": D},
    "aggregation": "mean_win_rate",
    "threshold": 0.50
  },
  "capabilities": {"warm": false},           # warming unsupported until implemented
  "protocol_version": 2,
  "trained_at": "..."
}

### Canonical deck hashing (amendment: define and test against real training deck)
- Normalize: strip setuptools artifacts; collapse whitespace; NFC-normalize names;
  lowercase ASCII-ish (`casefold`); entries "Name" with count; sideboard ("Sideboard"/"SB:")
  EXCLUDED from hash inputs and from deck.size; commander counted in main; counts clamped >=0;
  zero-count entries dropped. Then deck_hash = sha256(sorted JSON of {"cards": [[name,count]...sorted]}, separators=(",",":")).
- Verify against a real exported training-deck manifest (UWTempo ver2; fetched read-only from
  blackwell's magezero models dir - NOT regenerated - and hash-asserted in tests).

### Inference protocol v2 (single + batch envelope, ws04 client half)
- Single: {"request_index": 0, "model_id": req(), "checkpoint_hash": req(), "indices": [...]}
- Batch:  {"items": [{"request_index": i, "offset": o, "num_indices": n}, ...], "model_id": ...,
           "checkpoint_hash": ..., "indices": [...], "offsets": [...]}
- Response envelope (server): for each row {"request_index": j, "value": float in [-1,1],
  "policy_player": 128 floats, "policy_opponent": 128 floats}; batch wrapper additionally carries
  {"served_model_id", "served_checkpoint_hash", "protocol_version": 2} once, and rows are exactly
  count-locked, order-locked (all-or-none request_index echo, no partial echo), finite, unique.
- Mismatched requested model_id on a single-model server: HTTP 409 + {"error": "model-mismatch"}.
  NOT a silent fallback to default.
- Legacy positional (no indices echoed) remains supported for protocol_version 1 servers until
  05 lands; ws04 client labels this mode and does NOT claim verified ordering.

## Files (ws04 worktree)
- src/arenamcp/model_zoo.py        — ModelSpec v2 fields, parse-reject v1 manifests missing
                                     schema_version (explicit, not silent), from_dict strictness
- src/arenamcp/magezero_client.py  — request-side correlation + served-identity validation
- tests/test_model_contract.py     — schema roundtrip, canonical deck hashing vs real deck,
                                     wrong-model 409, identity echo validation, capability gating

## Acceptance (from task 04 + amendments)
1. Real runner-shaped manifest parses and matches the actual training deck (deck_hash verified).
2. rejected/uncertified cannot masquerade as certified (certification block structurally required).
3. Wrong-model request rejected (409), not fallback.
4. Response identity matches the loaded model (checkpoint_hash validated client-side).
5. Tiny mocked models only; no GPU/production loads; no live-fleet changes.
