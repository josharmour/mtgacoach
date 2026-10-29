# AOA integration implementation and acceptance

Worktree: `/Users/joshu/repos/mtgacoach-aoa` (`/Volumes/repos/mtgacoach-aoa`).
Branch: `codex/aoa-integration`. Baseline: `7619a50fd5864352da8838e118a6471702bd41ae`.
The initial contracts/remote-provider commit was `2632102944c6ac2fad2efd6e26b4a1e53933f2d0`.
The original integration was merged and rolled out. The speech repair below supersedes
the original configuration UI and voice-routing choices; live settings and history
are preserved during rollout.

## Implementation checklist

| Plan requirement | Implementation | Verification / qualification |
|---|---|---|
| Versioned observations, evidence, epochs and delivery | `commentary/contracts.py`, `events.py`, `observations.py`; bounded 128-event GRE journal in GameState, copied through the published server snapshot | Replay tests cover transient casts/resolutions, stack disappearance, combat, life, turns, game boundaries, private/facedown cards and stable fingerprints |
| Observed outcomes, not submitted intentions | GRE `CastSpell`, `Resolve`, `PlayLand` annotations remain separate; no inference of resolution from a missing stack object | Provenance tests; deterministic lines only use observed facts |
| Authoritative Coach ingestion | PipeAdapter hands published state to one background service; no second watcher, database reader, app or history writer | Real JSON worker tests and mocked service tests; worker bootstrap bypasses Coach's application initializer |
| Headless AOA logic and personas | `commentary/headless.py` wraps the independently installed/source AOA `GenerativeBooth`, `StoryModel`, personas, native validation and factual audit | All four personas and two-caster anchor/heard behavior tested against sibling AOA source; separate standalone test suite also exercised |
| Deterministic path and audited announcers | Grounded event calls need no AOA package/model. Generative mode uses Coach's existing backend and the actual AOA booth; short compact prompts retain native validators | Generation+audit, rejected audit, unsupported resolution and disabled repairs tested. Grounded mode deliberately speaks at most one event call; cast/style primarily affect the generative booth |
| One playback owner and speech priority | CoachSession routes speech through `CommentaryController` and `SpeechArbiter`: essential, ordered workflow, commentary, routine/ambient | Tests cover exclusive advice/announcer routing, terminal acknowledgments for suppressed workflows, essential preemption, one active/one pending, expiry, duplicate IDs, stale callbacks and dependent replies |
| Playback is the delivery boundary | Accepted/rendered does not count as heard; only started→finished updates the AOA ledger | Mocked TTS lifecycle and native AOA delivery tests |
| Freshness at every expensive boundary | Match/game and snapshot checks before generation/audit, after result, after synthesis and before playback; eight-second calls/fifteen-second analysis; mode changes clear optional work | Fact-change, mode-change, epoch, stale-render and mute tests. Invalidation is conservative: a changed public snapshot can choose silence |
| Exclusive speech content | Announcer mode suppresses normal Coach workflow/routine/ambient audio while preserving UI text and completion acknowledgments; essential alerts remain eligible with the selected engine | Mode-switch, suppression, provider and workflow-completion tests |
| Immediate mute/mode controls | One visible speech switch controls desktop playback; choosing announcers assigns Coach ownership; older internal configuration remains supported | Controller/service tests. The choice does not stop another process; turn standalone playback off yourself before assigning Coach ownership |
| Remote Chatterbox independent of commentary | Dedicated speech key file, no LLM/license credential fallback, bounded reads, redirect rejection, mono 24 kHz PCM→WAV, cancellation, no retries, explicit failure without switching engines; long advice is chunked and joined | Real server-default, Atlas and Mira audio rendered; transport/cancellation and desktop completion tests. Audible device acceptance remains pending |
| Optional local Turbo | Separate interpreter/process, hash-locked dependencies, immutable model revision, offline local load, cached Atlas/Mira conditioning, default voice, licensed assets, CPU/MPS/CUDA selection, failure containment | Mocked PCM/protocol validation; published wheel API inspected. No Torch/model loaded or downloaded. Actual speed, OOM/device behavior and audio are pending |
| Inference protection | One exchange/no backlog, default two/max four per minute, two calls, six-second total deadline, 6,000 input-token conservative bound/call, 512/256 output, 48,000/3,072 aggregate, twelve request-seconds/minute including cancelled calls | Budget tests, proxy nonwaiting/preemption tests, socket cancellation tests. Local scheduling cannot prove cross-application gateway enforcement |
| Gameplay and strategy remain independent | Proxy protects foreground/strategy calls, cancels commentary and rejects optional admission while protected; no strategy refresh or decision dependency introduced | Separate strategy, proxy and existing gameplay narration regressions |
| Controls and readiness | Speech on/off; Kokoro/Chatterbox; Coach advice/Announcers; Server Chatterbox; discovered Voice catalog and Preview. Existing ArenaOnAir setup is reused; runtime internals are hidden | Offscreen UI tests and visual review; actual server schema checked. Server default omits voice, ignoring old troubleshooting pairs. Preview never saves or unmutes normal speech |
| Telemetry | Proxy supplies first-token/request/token metrics; service logs observation age/admission/budget/drop reasons; controller logs queue/render/playback lifecycle timings; voice failure status is visible | No private text in added service/controller metrics. Native AOA logging stays on worker stderr, not standalone history |
| Packaging | Worker source and licensed assets included in PyInstaller spec; bootstrap works with an external Python interpreter, without requiring Coach's dependencies in the voice environment | Source/bootstrap smoke tests. Windows Python 3.11 runtime + 3.12 executable builds and OS playback require platform qualification |
| Controlled acceptance | `commentary/qualification.py` evaluates matched OFF/ON JSON measurements; explicit local benchmark under `runtime/chatterbox/` | Evaluator tests reject missing measurements or any failed gate. No fabricated performance results |

The plan proposed eventually locating the facade in `arenaonair.headless`. This
branch supplies the facade in Coach's isolated worker and imports AOA's existing
modules without copying the booth or editing the separate, active AOA checkout.
A shared/released headless package remains a future extraction; the complete
integrated path works with a compatible AOA source checkout today.

## Safe end-to-end test sequence

After ranked matches finish, close the old Coach instance before launching an
updated checkout. Never run two instances against the same game. After rollout,
use the existing **MTGA Coach.app** launcher as usual.

1. Open **Speech & voices** from the compact panel or Tools menu.
2. Choose **Kokoro** or **Chatterbox**. For the existing speech server, keep
   **Server Chatterbox** checked. Coach reads the existing ArenaOnAir configuration;
   no URL, key, command, or model entry is required in this dialog.
3. Leave **Voice → Server default** to let the server decide. This is different
   from the explicit **Chatterbox original voice**. Atlas and Mira are offered
   only after the server advertises them. **Preview** auditions the current
   unsaved choice, even while Speech is off, without changing saved preferences.
4. Choose **Coach advice** or **Announcers narrate the game**, turn **Speech on**,
   and Save. Announcers replace normal spoken advice; essential alerts still use
   the selected voice engine. A Chatterbox failure does not silently switch to
   Kokoro. Try Preview again or explicitly choose Kokoro.
5. In a nonranked test, check mute/stop, a mode switch mid-line, game boundaries,
   and dependent analyst replies. UI explanations remain available in either mode.
6. Local Chatterbox requires a separate installed runtime and model; it is not
   installed on the current Mac. The dialog explains this and offers Server
   Chatterbox/Kokoro. Developer setup remains in `runtime/chatterbox/README.md`.

![Simplified speech controls](images/aoa-simple-speech.png)

### Speech repair verification (2026-10-29)

- Root cause: the Kokoro rendering worker inherited saved mute, so its warmup
  probe returned no audio and exited with status 1. It now renders independently
  of playback mute and does not persist per-utterance voice or speed changes.
- Actual installed Python 3.12 runtime, real Qt QProcess startup, real Kokoro
  synthesis and real server-default Chatterbox synthesis passed with a temporary
  muted profile. Audio was inspected as WAV (24 kHz), not played audibly.
- Server health reported hybrid ready; its actual OpenAPI schema advertises
  Atlas, Mira and original voice, with Atlas as the omitted-voice default.
  Explicit Atlas/Mira and omitted-voice requests generated valid PCM/WAV.
- Real long-advice qualification exposed four-second request timeouts with
  large chunks. Requests now contain at most 100 characters; a 464-character
  natural advice sample rendered in five requests (1.19–1.88 seconds each),
  producing 688,320 frames at 24 kHz in 8.52 seconds without playback.
- UI review uses synthetic preferences; no game screenshots or credentials are
  included. Live settings/history and the shared speech service are unchanged.
- Local Chatterbox installation/hardware qualification, Windows runtime and
  audible playback remain unverified. No new service or model was installed.

## Performance and deployment gates still to qualify

Code and isolated tests do not establish hardware or cross-application capacity.
Required live qualification remains: Windows/macOS/Linux playback and device
failures; Windows packaged runtimes; local CUDA/MPS/CPU (and ROCm if deployed),
memory/frame-time impact; speech-server performance under shared load; gateway
priority/admission across Coach, Sobriety Copilot, coding/Hermes and other clients;
and proof that upstream cancellation actually frees shared resources. No gateway
configuration, credentials or other application was changed. Announcers enable the audited booth. Local Chatterbox remains optional; choosing
a voice engine never changes the shared server default.

For each platform and shared-load condition, capture the same replay with
commentary off/on and strategy off/on. JSON input for the evaluator must include:

```json
{
  "replay": "stable-replay-id",
  "strategy": true,
  "load": "controlled-load-description",
  "platform": "platform-and-device",
  "decisions": [{"id": "decision-1", "seconds": 1.0, "timeout": false, "fallback": false}],
  "essential_start_seconds": [0.3],
  "preemption_seconds": [0.1],
  "stale_game_utterances": 0,
  "overlapping_utterances": 0,
  "upstream_cancellation_verified": false,
  "gateway_admission_verified": false
}
```

The schema example is **not a measurement**. Supply at least twenty matching
ordered decision IDs, actual speech measurements, and verified gateway flags.
Run `PYTHONPATH=src python -m arenamcp.commentary.qualification baseline.json commentary.json`.
It requires p95 decision latency within **both** 10% and 200 ms, no additional
timeouts/fallback actions, every measured essential start ≤1 s, preemption ≤250 ms,
zero stale/overlapping utterances, and both gateway checks. Missing data fails.

## Validation record

- Final combined integration, desktop, game-state, workflow, watchdog/concede,
  strategy and proxy regression suite: **348 passed** (61.43 seconds). This
  includes the new tests and the existing selected regression suites.
- Standalone AOA `test_llm_booth.py`, `test_speech_audio.py`, `test_tts_remote.py`:
  **77 passed**, with one third-party Starlette deprecation warning; no model or
  audio device used. Source checkout HEAD was `20d87e8c5034f247f551bef326a4ac4d7983eff9`
  plus its existing local state; it was read only.
- Tests use temporary state, offscreen Qt, no bytecode/cache writes in source,
  mock model/audio, and loopback mock servers on random ports where needed.
- Local dependency resolution completed and the published Chatterbox 0.1.7
  `from_local` API was verified by wheel inspection. Installation, weights,
  live speech server tests and hardware benchmarks were intentionally not run.

- Ruff passed for every changed Python module and test; `git diff --check` passed.
- `uv build --wheel --out-dir /tmp/aoa-wheel-check` succeeded using an isolated
  temporary build environment (the active Coach environment was not changed).
- Wheel inspection confirmed packaged bootstrap/worker/UI sources and both voice
  assets/license; no Torch/Chatterbox default dependency. The extracted wheel
  worker completed its JSON handshake under `python -S` (stdlib only).
- Offscreen settings preview: [dialog image](aoa-settings-preview.png).

- Final saved-mute follow-up: **21 passed** for pipe voice, desktop completion,
  and controller tests (13.21 seconds); includes a new persisted-mute regression.
  Affected files passed Ruff and the wheel was rebuilt.

## Settings validation follow-up (2026-10-29)

The screenshot's generic HTTP(S) endpoint warning is reproducible when the
unsaved provider selection is Chatterbox server and its URL is empty. The
persisted provider may still be Kokoro because validation precedes every write.
Kokoro does not validate an inactive Chatterbox endpoint. Save and Apply/Preview
share the same validation; selecting remote Chatterbox requires a URL and its
separate speech-key file even when autoplay speech is Action narration.

The dialog now explains missing remote configuration, selects the Runtime tab
and focuses the relevant field. Provider-specific fields and generative style/
cast controls are disabled when inactive, with visible setup guidance. Invalid
explicit commentary runtime/source paths and non-executable local Python files
are rejected before settings are written. Cancel remains side-effect free;
Save does not trigger voice preview. New offscreen tests cover these paths,
persisted choices on reopening, and a mocked worktree engine launch that pins
PYTHONPATH while retaining the normal shared runtime/single-instance location.

No running Coach, game, settings, models, or services were changed or launched.
These GUI source changes require a future full desktop launch to load; an engine
reload alone does not replace the dialog code. Live audio, remote speech service,
and generative backend behavior remain unverified. The ordinary and worktree
launches still share the user profile; do not run them concurrently.

Updated offscreen preview: [settings guidance](aoa-settings-validation.png).

Validation: the expanded selection completed with 99 passing tests, one Windows-
only skip, and one launch-fixture failure (missing repository marker). After
correcting that fixture, its targeted rerun passed: **100 selected tests passed
in total, one skipped**. Ruff and whitespace checks passed. The UI was rendered
and visually inspected offscreen; all controls and guidance fit at 570×640.
