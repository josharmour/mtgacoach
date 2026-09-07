# Integrated Swarm Notes — Tasks 04, 05, 06, 07, 08, 09, 10, 11, 12, 13

## ws04/05 — Honest served-model contract & discovery (Tasks 04 & 05)
- Frozen manifest v2 schema with strict certification, capability gating, and canonical deck hashing.
- Request-side correlation (`request_index`) + served-identity validation (`checkpoint_hash`).
- Discovery endpoint accessor (`/models`) with dynamic residency and active host scoping.
- Stale snapshot invalidation on discovery/fetch failure.
- Warm request gating strictly bound to verified endpoint capability.

## ws13 — Encoder/action compatibility evidence (Task 13)
- Real XMage parity fixture comparison tool and strictly-labeled synthetic harness fixtures.
- Strict schema validation, authoritative provenance checking, and paired row identity verification.
- Explicit non-claims: tests strictly mark unverified compatibility without authoritative reference fixtures.

## ws1012 — Offline pipeline, bridge filtering, and corpus lineage (Tasks 10, 11 & 12)
- Policy-label eligibility separated from game outcome: unknown outcomes preserved under `--outcome all`.
- Legacy renderer stops asserting missing facts; explicit legacy-render mode.
- Fixed manifest drop counter accounting and preserved opponent combat flags.
- Canonical game IDs invariant under path renames; deterministic hash-based split assignment.
- Complete lineage tracking: input hashes, dirty-tree hashes, configs, schemas, and quarantine stats.

## ws0608 — Coaching correctness, gating, and lookahead presentation (Tasks 06, 07, 08 & 09)
- Local seat deck normalization isolating hero cards and excluding opponent/stolen permanents.
- Preserved card multiplicity and distinction between full registered deck vs observed subset.
- Opponent hand sampling preserving hidden-hand uncertainty and visible multiplicity.
- Neural afterstates restricted strictly to verified mechanical transitions; explicit fallback.
- Calibrated delta display (0.20 -> 0.35 raw maps to +7.5% normalized delta).
- Honest algorithm description (1-ply lookahead) and hypothesized threat attribution.
