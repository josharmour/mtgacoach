# B2 in-memory aggregate CLI source, 2026-11-14

Adds explicit `--aggregate-only --bundles DIRECTORY` to the offline benchmark. Cases and private source manifest remain in memory; neither is written. `--cases` and `--report` are forbidden in this mode. Output is allowlisted aggregate status counts, total timings, inert generation-refusal attempts and explicit snapshot-only/facts-empty/not-assessed scope. No model runner or endpoint discovery. Existing export mode remains separately explicit and still requires `--cases`.

Use in a dedicated process under OS deny-network: planner diagnostics are suppressed process-wide during this operation and restored afterward. Errors return a generic message without case text or filenames. This is not a replacement for OS containment or a concurrent-service API. No real corpus run performed for this change. No B2 gate waiver or production-equivalence claim.

38 synthetic/adjacent tests pass OS-denied (7 CLI plus31 existing benchmark). An initial neighbor run lacked its test fixture import; copying the unchanged fixture and package marker resolved setup, with no assertion changes. Test source import used exact4e7ddfe B2 checkout. Ruff clean. Independent review required.
