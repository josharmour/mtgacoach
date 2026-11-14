# Required normal-cast admission, offline source correction

The approved five-card pilot's exact Chrome Mox output omitted its normal {0}
cast mode. Added bounded compiler admission: a printed single-face nonland with
an explicit braced mana cost must include a `cast` mode. Existing validator
still checks that mode's cost against print. No missing mode/cost/effect is
synthesized. Land, no-cost, split/multiface inputs stay outside this guard;
this is not proof of complete coverage of additional/alternative costs.

The exact retained pilot Mox artifact is a hash-checked regression fixture.
Persistent omission fails after the existing bounded mock repair attempt, rather
than becoming a successful record. Existing token-contract synthetic fixture
now includes its printed {1} cast mode; its original Unknown/retry assertions
remain unchanged. 115 focused mocked/offline tests pass. OS sandbox unavailable
in this session, so no new OS-contained claim; no actual model calls occurred.
Independent review required before integration.

Cobra remains a separate event-schema/consumer gap, not a harmless spelling
normalization: the actual record includes a proper cast mode and explicit
landfall subject. Current consumer expects a lossy ETB tag and no modes for the
special land-entry mana path. Do not erase timing or casting semantics merely
to pass that consumer. No Cobra output or original pilot evidence modified.
