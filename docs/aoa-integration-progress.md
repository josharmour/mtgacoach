# Arena On Air integration: first implementation

This feature branch starts at `7619a50fd5864352da8838e118a6471702bd41ae`, which contains the staged gameplay changes and `AOA-Integration.md` from the initial inspection. Work is isolated from the active master checkout. No merge, push, deployment, live configuration changes, model downloads, or service restarts are part of this implementation.

## Available in the desktop application

Tools → **Voice Engine…** selects Kokoro (the default) or **Chatterbox server**. Chatterbox is independent of commentary and can speak ordinary Coach speech. Its endpoint, speech-only key-file path, and Atlas/Mira/Default voice are stored separately from the existing Kokoro settings. The configuration dialog checks local configuration only; it does not contact a server or validate server readiness. Cancelling it saves nothing.

The adapter speaks the Arena On Air `/v1/audio/speech` protocol: a bounded mono 24 kHz signed 16-bit PCM response is validated and wrapped in WAV for the existing desktop playback owner. It rejects redirects, unexpected formats, malformed or oversized audio, and late output. No Torch dependency, voice assets, standalone Arena On Air application, second playback owner, or new service is introduced.

Only one Chatterbox request may be in flight per desktop TTS manager. Busy, unavailable, malformed, and invalid-configuration cases fall back to Kokoro or the available system voice. There are no automatic remote retries. Cancellation invalidates output and checks between reads; it does **not** cancel server-side GPU synthesis. A replacement while the earlier request is still in flight goes directly to fallback. Socket operations use bounded timeouts, and a monotonic deadline rejects late audio; cancellation is not an immediate network abort.

Remote worker results now cross a Qt signal to reach the GUI thread. Old remote failures and downloads cannot clear newer request state or replay cancelled speech. A late local-worker result cannot speak over an active remote generation. Completion still comes from playback, never merely from synthesis.

No live speech endpoint or device was exercised while matches were running. Warm fallback latency and Windows/Linux/macOS audio behavior require separate qualification. The one-second essential-speech release gate is **not** established by these unit tests.

## Implemented foundations, not enabled in the runtime

- `commentary/contracts.py`: protocol version 1, match/game epochs, observation/evidence provenance, utterance IDs/categories/expiry/voice/anchors, and acknowledgment states.
- `commentary/observations.py`: detached, conservative public projection of a published Coach snapshot. It excludes private hands, library/deck contents, names of players, raw GRE/freeform events, and decision requests; hidden/private object identities are redacted. Combat state and explicitly supplied event provenance survive the projection. An action submission and a disappearing stack object never become an inferred resolution.
- `desktop/speech_arbiter.py`: a pure scheduler for essential speech, ordered workflows, commentary, and ambient speech. It has one active and one pending line, expiry/evidence validation, essential preemption, epoch/mute clearing, anchor completion dependencies, and duplicate/late-ack protection. An explicit admission cap bounds per-epoch ID bookkeeping.

The scheduler and observation adapter are intentionally not connected to `CoachSession` or the parser yet. Existing narration remains the default. There is no commentary checkbox that silently substitutes unfinished behavior.

## Remaining integration

1. Establish the headless facade and matching versioned transport in Arena On Air while preserving its standalone entrypoint.
2. Wire authoritative observations and transient parser events outside state locks, worker lifecycle/cancellation, and delivery acknowledgments.
3. Integrate the speech arbiter with the desktop owner and classify engine messages; suppress only routine autoplay submission narration at its source. Add immediate mode/mute controls and evidence revalidation before playback.
4. Add deterministic commentary, then audited generative dialogue, persona/cast controls, bounded admission and token/request-time budgets that yield to strategy/gameplay. Do not enable generation before admission enforcement exists.
5. Add output-ownership coordination for the standalone application. Local Chatterbox, pinned optional worker environments, speculative synthesis, and contention/platform qualification remain separate gates.

## Focused verification

Final local result: **76 tests passed** across `tests/aoa`, `tests/test_desktop_speech_completion.py`, `tests/test_desktop_tts_handoff.py`, and `tests/test_tts_worker_priority.py`. Ruff lint and formatting checks pass for all changed Python files. `git diff --check` passes with `core.whitespace=cr-at-eol`, preserving the existing CRLF convention in the desktop window file.

The `tests/aoa` suite uses saved synthetic snapshots, mocked HTTP responses, fake playback, and tiny Qt event-loop tests. It performs no model inference, network access, gameplay launch, or real audio playback. Existing desktop speech-completion/handoff tests are also exercised. Test logs go to `/tmp`, and repository test fixtures redirect persistent state to temporary directories.

For the lightweight new tests alone:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src QT_QPA_PLATFORM=offscreen \
  QT_MAC_DISABLE_FOREGROUND_APPLICATION_TRANSFORM=1 \
  ARENAMCP_LOG_FILE=/tmp/aoa-tests.log OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
  python -m pytest tests/aoa --confcutdir=tests/aoa -o addopts='' -p no:cacheprovider -q
```

The integration plan's live performance, GPU contention, and cross-platform release gates remain untested.
