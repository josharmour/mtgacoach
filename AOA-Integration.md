# Arena On Air Integration Plan

Analysis date: October 28, 2026. Scope: feasibility and implementation planning only.

**Both integrations are feasible as opt-in features.** I recommend phased adoption: add Chatterbox through a provider adapter first, then integrate Arena On Air commentary behind explicit speech priorities and inference limits. Arena On Air should remain independently installable and supported.

The current working trees were inspected without changing files, configurations, or running sessions. `~/repos/arenaonair` resolves to `/Volumes/repos/arenaonair`; `~/repos/mtgacoach` resolves to `/Volumes/repos/mtgacoach`. Both repositories contain substantial uncommitted changes; the findings below describe those files, not necessarily released capabilities. No models, benchmarks, or tests were run during the live session. Saving this document is the only subsequent repository change made for this request.

## What is reusable today

| Area | Observed implementation | Integration implication |
|---|---|---|
| Coach state ingestion | [`server.get_game_state()`](src/arenamcp/server.py) reads published snapshots and incorporates bridge information. [`GameState.publish_snapshot()`](src/arenamcp/gamestate.py) provides a consistent state boundary. | Coach should remain the authoritative source for integrated commentary. Snapshot polling alone can miss transient events, so preserve event provenance too. |
| Arena On Air ingestion | [`ArenaOnAirApp._consume_snapshot()`](../arenaonair/src/arenaonair/app.py) combines `GameStateBuilder`, `EventDiffer`, and `StoryModel`. Its [`GameState`, `Event`, and related models](../arenaonair/src/arenaonair/models.py) differ from Coach’s dictionaries. | Reusable logic, but an explicit state/event adapter is required. A second independent log watcher would introduce disagreement and duplicated work. |
| Personas and orchestration | [`PERSONAS`](../arenaonair/src/arenaonair/personas.py) includes Classic, Esports hype desk, Test-match understatement, and Pro Tour. [`GenerativeBooth`](../arenaonair/src/arenaonair/llm_booth.py) manages roles, evidence, recent speech, freshness, and dialogue dependencies. | “HypeDesk” corresponds to the `esports` persona in this checkout; there is no separate HypeDesk subsystem to merge. |
| Prompt and factual checks | [`JsonClient`](../arenaonair/src/arenaonair/llm_booth.py) generates structured dialogue and makes a separate factual-audit request. `GenerativeBooth.process()` can repair consequential calls. Context is bounded by serialized characters. | Preserve grounding and auditing, but adapt transport, token limits, cancellation, and admission. One commentary exchange is normally **two LLM calls**, potentially more with repair. |
| Speech sequencing | [`SpeechQueue` / `SpeechPump`](../arenaonair/src/arenaonair/speech.py) track salience, delivery, and dependent replies. [`AudioAhead`](../arenaonair/src/arenaonair/speech_audio.py) prepares one line ahead. | Reuse dialogue bookkeeping and freshness rules. Do not introduce a second playback owner inside Coach. |
| Coach playback and acknowledgments | [`TtsManager`](src/arenamcp/desktop/tts_manager.py) renders asynchronously, suppresses stale generations, and reports speech status. [`CoachSession`](src/arenamcp/desktop/coach_session.py) forwards acknowledgments to the engine. | This remains the integrated app’s audio authority. Its current newest-request-wins behavior needs a priority arbiter before commentary is enabled. |
| Ordered Grind narration | [`_drain_grind_speech()`](src/arenamcp/standalone_auto_quest.py) serializes narration on a worker using [`narrate_and_wait()`](src/arenamcp/speech_completion.py). | Commentary must not repeatedly supersede these utterances or falsely satisfy their completion waits. |
| Chatterbox | [`ChatterboxEngine`](../arenaonair/src/arenaonair/platform/tts_chatterbox.py), [`RemoteChatterboxEngine`](../arenaonair/src/arenaonair/platform/tts_remote.py), and [`tts_server.create_app()`](../arenaonair/src/arenaonair/tts_server.py) already exist. | Synthesis, conditioning caches, voice assets, and the remote protocol are useful starting points. Playback and lifecycle need adaptation. |

## Architecture recommendation

Keep commentary generation and voice synthesis independently selectable: Arena On Air commentary should work with Kokoro; Chatterbox should work with ordinary Coach speech.

| Approach | Tradeoff | Recommendation |
|---|---|---|
| Import the entire Arena On Air application into Coach | Fast prototype, but brings lifecycle, configuration, history, playback, and Python-version coupling. | Avoid. |
| Headless Arena On Air worker with a versioned protocol | Preserves its existing logic and standalone app; isolates crashes and dependencies. Requires IPC and explicit delivery feedback. | **Preferred first commentary integration.** |
| Shared, headless commentary package | Clean long-term reuse across both apps; requires deliberate extraction and release/version management. | Follow once the adapter contract is proven. |
| Selectively port modules | Reasonable for small persona definitions or synthesis adapters; copying the entire booth creates two diverging implementations. | Use narrowly, with provenance and shared contract tests. |
| Remote commentary API | Centralizes model policy but adds network, authentication, privacy, and service operations. The existing TTS server is not such an API. | Optional later; unnecessary for the initial integration. |

The first headless facade should live in Arena On Air and accept normalized observations, settings, cancellation, and delivery results. It should emit utterance proposals without opening audio devices, starting a log watcher, or writing standalone history. Its normal desktop entrypoint remains supported.

Concrete proposed boundaries:

| Proposed boundary | Responsibility and existing touchpoints |
|---|---|
| `arenamcp.commentary.observations` | Convert published Coach state plus observed events into the booth contract. Integrate around `parser.py`, `gamestate.py`, and `server.py`; enqueue bounded work outside state locks. |
| `arenaonair.headless` | Expose `GenerativeBooth`, persona selection, event/story processing, and validation without constructing `ArenaOnAirApp`. |
| `arenamcp.commentary.client` | Own worker lifecycle, protocol versions, epochs, bounded queues, and cancellation. |
| `arenamcp.speech_policy` | Classify routine action narration, commentary, ordered workflow narration, manual intervention, and errors. |
| `arenamcp.desktop.speech_arbiter` | Select the next utterance and enforce preemption before `TtsManager`. |
| Provider adapters behind `tts_worker.py` / `TtsManager` | Return audio and metadata; desktop playback supplies completion. Keep Kokoro available. |
| Settings/UI | Extend [`settings.py`](src/arenamcp/settings.py), [`compact_coach.py`](src/arenamcp/desktop/compact_coach.py), `main_window.py`, `coach_session.py`, and `standalone_voice.py`. |

The observation contract needs match/game identity, monotonic arrival time, source sequence, snapshot identity, public/private visibility, and evidence references. It must distinguish **action submitted**, **action observed**, and **spell resolved**. Autopilot requesting an action is not sufficient evidence to announce its outcome.

## Speech behavior

During autoplay, commentary mode should suppress only routine execution narration at its source—for example, the submission speech paths in [`autopilot.py`](src/arenamcp/autopilot.py). Continue showing action explanations in the UI and logs. Avoid a broad filter in `speak_advice()` that could also swallow intervention or error messages.

Use this ordering:

1. Manual intervention, actionable errors, and urgent status.
2. Ordered Grind/deck-review/draft workflow narration.
3. Commentary.
4. Optional ambient speech.

A dramatic commentary event remains below an operational error even if Arena On Air labels it “must speak.”

Each utterance should carry an ID, category, match/game epoch, expiry, provider/voice, interruptibility, and optional dialogue-anchor ID. An analyst reply becomes eligible only after its opening line finishes playback.

Preserve the existing lifecycle: `accepted → started → finished`, or a terminal `stopped`, `superseded`, `failed`, or `muted`. Rendering or downloading audio must never count as hearing it. Existing playback completion uses process checks or timing estimates depending on platform, so device-failure cases need validation too.

Commentary should:

- Yield immediately to essential speech.
- Wait behind protected workflow narration, expiring while it waits.
- Revalidate facts before generation, after audit, after synthesis, and before playback.
- Discard invalidated replies when their opening line is cancelled.
- Clear pending work on match/game changes and mode changes.
- Fall back to a short grounded template or silence when late.

An explicit global mute remains respected. Otherwise, essential speech should use a warm, reliable Kokoro/system fallback if Chatterbox cannot meet its deadline.

Running the standalone Arena On Air app alongside Coach can still produce two independent audio streams. Integrated mode therefore needs an explicit output-ownership choice or cooperative lease; it should not silently stop the standalone app.

## Chatterbox assessment

**Remote Chatterbox: phased go.** This is the smallest useful first integration, especially if dedicated speech hardware remains available.

The protocols are currently incompatible: Coach’s remote path requests `model="kokoro"` and WAV, while Arena On Air returns mono 24 kHz signed 16-bit PCM and accepts `atlas`, `mira`, or `default`. Add a dedicated adapter that validates the response and wraps PCM for Coach’s playback. Do not merely point the existing Remote TTS setting at the service.

Use a separate speech credential; Coach’s current remote path falls back to language-model/license credentials. Preserve Arena On Air’s bounded reads, redirect rejection, and redacted failures.

The server rejects concurrent synthesis with 503 instead of queueing. That is useful backpressure, but it has no request-priority or synthesis-abort protocol. Client cancellation can discard output without reclaiming GPU work. Avoid retry storms and keep optional speech out of essential-message paths.

**Local Chatterbox: conditional go, optional isolated runtime.** Arena On Air selects CUDA, MPS, or CPU and caches per-speaker conditioning. CUDA model-load OOM can fall back to CPU, but that does not establish acceptable CPU latency or recovery from generation-time OOM.

Its synthesis is currently whole-line. The remote server also completes synthesis before sending PCM; network chunking is **not incremental model generation**. Moreover, `SpeechPump` uses `prepare_audio()` when available, so its normal remote path buffers prepared audio before playback.

Arena On Air’s local PCM playback deliberately lets a started chunk finish. Reuse synthesis, but route audio through Coach’s cancellable playback. Disable speculative audio initially: `AudioAhead` can wait for an obsolete synthesis future before handling a replacement.

Turbo is the appropriate initial model. Upstream documents Turbo as 350M parameters and Original as 500M with CFG/exaggeration controls. The Turbo model card identifies an MIT license and built-in watermarking. These are model characteristics, not latency guarantees. [Official Chatterbox documentation](https://github.com/resemble-ai/chatterbox), [Turbo model card](https://huggingface.co/ResembleAI/chatterbox-turbo).

Arena On Air’s [`usage.md`](../arenaonair/docs/usage.md) reports:

- Warm Apple Silicon samples: Turbo 2.8–3.6 seconds; Original 6.6–8.3 seconds.
- Dedicated R9700 deployment samples: 1.04–3.50 seconds, median 2.05 seconds.
- Approximately 2.9 GiB peak allocated GPU memory and 4.2 GiB service usage.

These are repository-reported measurements, **not verified current service performance**. The document also contains an earlier “not installed” statement before its later deployment report. Confirm current hardware assignment and service configuration before choosing a deployment.

Start with Atlas, Mira, and the model default. Preserve separate provider-specific voice settings so switching providers never overwrites a Kokoro selection. The packaged references include attribution, modification notes, and a [CMU license file](../arenaonair/src/arenaonair/assets/voices/CMU-LICENSE.txt); carry those assets and notices into distribution.

Keep Torch out of Coach’s default install. Coach supports Python ≥3.10; Arena On Air requires ≥3.11. Chatterbox brings substantial Torch, torchaudio, NumPy, Transformers, and audio dependencies, with restrictive upstream pins. Use a separately locked worker environment and pinned model revisions rather than assuming the two existing environments can be combined. [Upstream dependency manifest](https://raw.githubusercontent.com/resemble-ai/chatterbox/master/pyproject.toml).

Coach’s Windows workflow includes both Python 3.12 executable packaging and a Python 3.11 runtime installation path; both need smoke coverage. macOS MPS, Windows CUDA/CPU, Linux audio dependencies, and any ROCm deployment need separate qualification. Hybrid Turbo/Original residency should remain a later option because it loads both models.

## Inference budget and protection

Preserve `separate_strategy_planning`: low-effort decisions continue consuming the latest completed plan without waiting. Commentary must neither trigger strategy refreshes nor become part of the action dependency chain.

Coach already has cancellation, call labels, telemetry, and a process-wide background lane in [`ProxyBackend`](src/arenamcp/backends/proxy.py). Its priority support is route-specific and can be disabled when rejected. Those mechanisms do not prove that the current LiteLLM/upstream deployment enforces priorities across applications.

Do not place commentary in the existing background lane unchanged: it could hold the lane ahead of strategy. Add non-waiting commentary admission, with strategy able to displace commentary, and gateway-level limits covering other clients.

Proposed conservative pilot limits—not measured capacity:

| Resource | Initial policy |
|---|---|
| Concurrent commentary | One exchange; no waiting request backlog |
| Rate | Start at two exchanges/minute; hard ceiling four |
| Calls per exchange | One generation plus one audit; repairs disabled |
| Context | ≤6,000 input tokens per call, including instructions |
| Output | ≤512 generation tokens and ≤256 audit tokens |
| Aggregate ceiling | ≤48,000 input and 3,072 output tokens/minute |
| Deadline | Six seconds total for generation and audit; skip if insufficient time remains |
| Additional occupancy bound | ≤12 cumulative request-seconds/minute, including cancelled attempts |
| Speech | One active line plus one pending line; initially no speculative synthesis |
| Freshness | Play call must start within eight seconds; analysis within fifteen seconds, subject to evidence validity |

These limits should be enforced together; reaching any limit pauses optional work. Maintain factual auditing—an unaudited response is dropped.

Reserve interactive admission for Coach and Sobriety Copilot. Admit commentary only from spare capacity, below gameplay and strategy. Coding/Hermes remain best-effort according to the supplied priority, consistent with the prior recorded preference. Current server capacity and enforcement were not inspected.

Log observation age, admission/drop reason, queue delay, first-token time, generation/audit tokens, cancellation, synthesis time, playback start/end, and fallback. Confirm upstream cancellation actually releases resources. On shared GPUs, client-side cancellation and process niceness alone are insufficient protection.

## User-facing controls

Keep the main controls simple:

- **Autoplay speech:** Action narration / Arena On Air commentary / Essential messages only.
- **Style:** Classic / Esports hype desk / Test-match / Pro Tour.
- **Cast:** One or two commentators; Calls / Balanced / Analysis; commentary frequency.
- **Voice engine:** Kokoro / Chatterbox on this computer / Chatterbox server.
- Provider-specific voice selection, speed, preview, and visible readiness/fallback status.
- Separate commentary mute and global mute.
- Public-information-only commentary by default; explicit control before including private hand/deck facts.

Advanced settings can expose endpoint, credential reference, model variant, device, and budget. Turning commentary off should take effect immediately without restarting gameplay.

## Incremental implementation and acceptance gates

1. **Define contracts and replay fixtures.**
   Establish versioned observations, utterances, and acknowledgments from saved fixtures. Verify casts versus resolutions, transient stack events, combat, game boundaries, unknown information, and private-state filtering. No model calls required.

2. **Add speech categories and arbitration.**
   Keep existing narration as the default. Acceptance: commentary cannot interrupt ordered Grind narration; intervention preempts commentary; every tracked utterance receives exactly one terminal result; stale audio never finishes a newer ID.

3. **Add remote Chatterbox independently.**
   Adapt PCM, authentication, voices, deadlines, and fallback. Acceptance: busy/offline/malformed responses recover; cancellation produces no replay; Kokoro still works with commentary off. Exercise Windows, macOS, and Linux playback.

4. **Add the headless booth and Coach observation adapter.**
   Begin with deterministic commentary and Kokoro, then enable audited generative commentary under the budget. Acceptance: only routine autoplay narration is suppressed; reported outcomes are observed; dependent replies require successful playback; missing facts produce silence rather than invented explanations.

5. **Qualify local Chatterbox and optional pre-generation.**
   Benchmark cold/warm start, both voices, longer lines, memory, frame-time impact, and concurrent gameplay. Enable one-line preparation only after proving it cannot delay essential speech.

6. **Run controlled contention and standalone regression tests.**
   Compare matched replays with commentary off/on, strategy off/on, and representative shared load. Proposed release gates: no additional decision timeouts or fallback actions; p95 decision latency within 10% and 200 ms of baseline; essential-message audio begins within one second on a warm fallback; playback preemption within 250 ms; zero stale-game speech and zero overlapping Coach utterances. Confirm Arena On Air still launches, comments, changes personas, and speaks independently.

Existing tests worth extending include [`test_grind_narration.py`](tests/test_grind_narration.py), [`test_desktop_speech_completion.py`](tests/test_desktop_speech_completion.py), [`test_separate_strategy_planning.py`](tests/test_separate_strategy_planning.py), and Arena On Air’s [`test_llm_booth.py`](../arenaonair/tests/test_llm_booth.py), [`test_speech_audio.py`](../arenaonair/tests/test_speech_audio.py), and [`test_tts_remote.py`](../arenaonair/tests/test_tts_remote.py).

The remaining deployment questions are current dedicated speech capacity, actual gateway admission/cancellation behavior, release readiness of the dirty Arena On Air changes, and whether the desired listening experience favors sparse calls or frequent two-caster exchanges. None prevents designing the integration; they determine whether generative commentary and local Chatterbox pass the release gates.
