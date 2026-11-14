# B2 in-memory aggregate CLI source, 2026-11-14

Adds explicit `--aggregate-only --bundles DIRECTORY` to the offline benchmark. Cases and private source manifest remain in memory; neither is written. `--cases` and `--report` are forbidden in this mode. Output is allowlisted aggregate status counts, total timings, inert generation-refusal attempts and explicit snapshot-only/facts-empty/not-assessed scope. No model runner or endpoint discovery. Existing export mode remains separately explicit and still requires `--cases`.

Use in a dedicated process under OS deny-network: planner diagnostics are suppressed process-wide during this operation and restored afterward. Errors return a generic message without case text or filenames. This is not a replacement for OS containment or a concurrent-service API. No real corpus run performed for this change. No B2 gate waiver or production-equivalence claim.

38 synthetic/adjacent tests pass OS-denied (7 CLI plus31 existing benchmark). An initial neighbor run lacked its test fixture import; copying the unchanged fixture and package marker resolved setup, with no assertion changes. Test source import used exact4e7ddfe B2 checkout. Ruff clean. Independent review required.

## Independent-review corrections

Initial f2a8ff8 did not block normal static database initialization and only redirected Python streams. Corrected aggregate scope now makes `_local_card` unknown and refuses `get_card_database`, preventing normal fallback factory/thread/cache initialization. Ability text remains unknown rather than card-derived. It also saves/replaces/restores descriptors1/2 plus Python streams, covering direct FD writes and subprocesses inheriting those descriptors. Dedicated-process use only: this is not a sandbox against arbitrary file writes, delayed native-library buffers or work escaping the scope. No background work is permitted; OS denial remains required. Tests prove actual static/ability path refuses factory, direct os.write and synchronous child diagnostics are suppressed, and descriptors restore after an exception. 40 contained tests pass after correction. No real corpus run.
