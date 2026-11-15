# F3 shared executable-source admission

Source-only candidate on 4bbd422. No inference, activation, server writes or full F3 acceptance.

Compiler direct compile_one, batch skip path and stamping, plus client hash lookup,
now share executable_source_hash. Legacy supported single-face four-field hashes
are byte-for-byte unchanged. Unsupported native markup/symbol dialect, malformed
braces and linked/multiface representations return unresolved before model calls
or consumer lookup. No native-to-Scryfall name alias is invented. Different
unmarked source text still has a different hash and misses rather than borrowing.

Raw Scryfall faces survive the compatibility Card/PrintedCard projections through
a deep copy. Compilation refuses these sources because the present native facts
schema cannot bind modes/effects to individual faces. This is loss-prevention, not
implemented multiface semantics or native normalization. Existing historical
multiface facts are no longer requested through these marked inputs; historical
records are not deleted. Sources that concealed all face metadata before reaching
this boundary cannot be magically reconstructed.

61 focused tests passed locally (new admission/face tests, original compiler and
client suites). Ruff and diff checks pass. Local run is not asserted OS-contained;
independent adversarial review remains required. Tests use synthetic/public card
text and fake backends, no live requests. Full F3 sample, independent semantics,
normalized native envelope and supported face ownership remain open.

## Independent rejection and correction

24cdbe6 missed native linked_face_ids and treated absent required fields as empty.
Correction preserves linked IDs through MTGADatabaseAdapter and _local_card even
if face lookup fails, and full Scryfall faces through both CardInfo adapters.
Shared admission now refuses linked IDs and absent fields, while explicit empty
Oracle text remains valid for vanilla cards. 68 local focused tests pass including
native unresolved-face projection and both Scryfall lookup methods. Independent
re-review required; prior 61 tests were not complete route coverage.

The second independent review additionally rejected falsy malformed face metadata.
Admission now allows only absent/None or an empty native list for each face signal;
False, zero, empty string/dict/tuple and populated lists refuse without normalization.
100 local focused tests pass; independent re-review remains required.

Independent synthetic contradiction probes found empty raw cache face metadata
could erase nonempty object faces. The projection now monotonically preserves a
refusal signal from either representation, including malformed metadata; neither
empty representation overrides the other. 112 local focused tests pass, with
both-direction conflicting and malformed controls. This is a synthetic boundary
repair, not evidence of such a conflict in the user's actual installed cache.
