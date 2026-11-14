# T1 bounded cancellation admission correction

Candidate starts at measurement-reviewed3244e5f, based on master8b73355.
No existing worktree or runtime settings changed. Original dirtyT1 preserved.

The largest retained measured category is cancelled planning296.156seconds.
That category is not all avoidable: cancelling obsolete requests is required
safety. This slice does not suppress cancellation, delay observation, reuse
answers or claim to recover that total.

Source defect: _call_llm could dispatch an already-cancelled request (including
a request cancelled during background-yield preparation); local hedge/repair
cancellation tokens could hide an outer request already cancelled. Legacy retry
could dispatch again after cancellation occurred during its first attempt.

Correction checks both outer and explicit leg tokens before background yield
and immediately before each normal/legacy backend invocation. Already obsolete
decisions raise the existing request-changed error without dispatch. Current
requests retain original token/budgets/backend compatibility. No persistent
cache, window-identity weakening or settings change.

Synthetic red/green: new six-case suite on exact3244e5f yields5failed1passed;
corrected source plus action-plan cancellation, request-change and combat
recovery neighbors yields28passed under macOS OS deny-network (5.27s). Tests
use deterministic mock backends, not inference or timed savings claims. Initial
broader run also included proxy socket tests:18passed4setup errors because OS
network denial correctly blocked local HTTP binds; those tests were not rerun
without containment. Ruff and diff checks pass.

Evidence local /Users/joshu/t1-runtime-test-20261114/baseline-admission.txt and
corrected-admission.txt. This is a source-level safety/waste prevention slice,
not proof these admission races caused the retained historical cancellations.
Real largest-source causal attribution and before/after saved-clock evidence
remain unavailable; original T1 acceptance remains open. Independent source
review is required before integration. No live model calls, activation or push.
