# mtgacoach — Project Roadmap & Suggested TODOs

A prioritized list of suggested tasks, feature enhancements, and technical debt items for `mtgacoach` (`arenamcp`).

---

## 🎯 High Priority

### 1. Audio-Primary UI Overhaul & Live "Brain / Context" Stream Inspector
- **Goal**: Redesign the PySide desktop UI to be **Audio-Primary** with a sleek, minimal main dashboard (focused on voice input/output status, advice corner, and minimal essential controls), accompanied by a expandable **Full Live Debug Stream** window displaying the coach's real-time "brain" context.
- **Context**: Users want an uncluttered, voice-driven main view during gameplay, with the ability to pop open a detailed streaming inspector for complete transparency into what the LLM sees and thinks.
- **Target Files**: [main_window.py](file:///Volumes/repos/mtgacoach/src/arenamcp/desktop/main_window.py), [coach_tab.py](file:///Volumes/repos/mtgacoach/src/arenamcp/desktop/coach_tab.py), [hud.py](file:///Volumes/repos/mtgacoach/src/arenamcp/desktop/hud.py), new `desktop/brain_stream_window.py`.
- **UI Architecture & Layout**:
  - **Minimal Audio-Primary View**:
    - Clean glassmorphism dark theme with audio waveform / PTT voice indicator.
    - Minimal essential controls: PTT / Mute button, Quick/Chatty toggle, AP toggle, and "Suggest Deck" button.
    - Floating click-through in-game HUD overlay for hands-free MTGA play.
  - **Expandable "Brain & Context Stream" Inspector**:
    - **Live Prompt Stream**: Full text of the exact prompt sent to the LLM (raw GRE state, hand, battlefield, library draw odds, turn memory).
    - **Live Reasoning Stream**: Real-time token streaming of LLM reasoning traces (e.g. DeepSeek reasoning content / Gemma thinking).
    - **Engine Telemetry**: Live latency counter (e.g. `129ms vLLM`), trigger event log, and bridge connection health.

### 2. "Suggest Compatible Deck" Feature (Inventory & Wildcard Aware)
- **Goal**: Offer a dedicated "Suggest Compatible Deck" action any time a player is viewing or preparing to join an MTGA match/event (Standard, Historic, Brawl, Timeless, Explorer, Pauper, Artisan, etc.).
- **Context**: Captures active event metadata (`Event_GetCoursesV2` / `Event.GetActiveEventsV2`), player inventory (`GetPlayerCardsV3`), and current wildcard balances (`PlayerInventory` Common/Uncommon/Rare/Mythic counts).
- **Target Files**: [deck_builder.py](file:///Volumes/repos/mtgacoach/src/arenamcp/deck_builder.py), [parser.py](file:///Volumes/repos/mtgacoach/src/arenamcp/parser.py), [standalone.py](file:///Volumes/repos/mtgacoach/src/arenamcp/standalone.py), [coach_tab.py](file:///Volumes/repos/mtgacoach/src/arenamcp/desktop/coach_tab.py).
- **Approach**:
  1. Detect match/event format selection in MTGA (e.g. `Pauper`, `Brawl`, `Standard`, `Timeless`).
  2. Surface a **"Suggest Compatible Deck"** UI action and proactive voice prompt whenever an event is selected.
  3. Analyze candidate archetypes using format legality, owned card inventory (`grpId` counts), and available wildcards:
     - **0-Wildcard Decks**: 100% buildable from existing collection.
     - **Budget Crafting**: Fits within player's exact available wildcard count.
     - **Meta Top-Tier**: Top win-rate meta decks for the event, highlighting exact wildcard craft costs.
  4. Output 1-click MTGA importable decklist strings directly to clipboard and UI deck tab.

### 2. Native macOS Global Hotkeys
- **Goal**: Implement a native macOS global hotkey listener for push-to-talk (F6), VLM screen analyze (F3), and autopilot toggle (F12).
- **Context**: The `keyboard` Python package hard-aborts on macOS without root/Accessibility privileges. Hotkeys are currently safely bypassed on `darwin`.
- **Target File**: [window_tracking.py](file:///Volumes/repos/mtgacoach/src/arenamcp/desktop/window_tracking.py) / new `desktop/hotkeys_darwin.py`.
- **Approach**: Use PyObjC `NSEvent.addGlobalMonitorForEventsMatchingMask` or Qt-native event filters.

### 2. FastAPI Website & Gateway Test Coverage in CI
- **Goal**: Enable web & proxy test modules (`test_patreon_litellm.py`, `test_proxy_server_responses.py`, `test_proxy_server_signup.py`, `test_trial_endpoint.py`) to run cleanly in standard test runs without requiring `fastapi` in the base venv.
- **Context**: Currently, running `pytest tests` flags missing `fastapi` module imports if `website` extras aren't installed.
- **Target Files**: [tests/test_proxy_server_responses.py](file:///Volumes/repos/mtgacoach/tests/test_proxy_server_responses.py), `pyproject.toml`.
- **Approach**: Add graceful `pytest.importorskip("fastapi")` guards at the top of website test modules.

### 3. Bo3 Sideboard Strategy Assistance
- **Goal**: Provide automated sideboarding recommendations between games in Best-of-Three (Bo3) matches.
- **Context**: `gamestate.py` already captures sideboard cards from `SubmitDeckReq` and `ConnectResp`.
- **Target Files**: [coach.py](file:///Volumes/repos/mtgacoach/src/arenamcp/coach.py), [standalone.py](file:///Volumes/repos/mtgacoach/src/arenamcp/standalone.py).
- **Approach**: Construct a specialized sideboard prompt when transitioning between Bo3 games, analyzing opponent's revealed deck/archetype against the player's 15-card sideboard.

---

## 🚀 Enhancements & Features

### 4. Advanced Draft Overlay Customization & Auto-Pick
- **Goal**: Enhance the PySide draft overlay with customizable color-pair preference filters and optional auto-pick countdown for quick draft modes.
- **Target Files**: [card_overlay.py](file:///Volumes/repos/mtgacoach/src/arenamcp/desktop/card_overlay.py), [draft_eval.py](file:///Volumes/repos/mtgacoach/src/arenamcp/draft_eval.py).
- **Features**:
  - Allow users to lock preferred color pairs (e.g. forced "Selesnya / GW").
  - Surface synergies with previously picked cards in current draft pool.

### 5. DeepSeek & Local VLM Vision Guidance Overlay
- **Goal**: Expand visual board analysis (`F3` / `Screen`) using local VLM endpoints (e.g., Qwen2-VL or Llava) alongside online VLM models.
- **Target Files**: [vision_mapper.py](file:///Volumes/repos/mtgacoach/src/arenamcp/vision_mapper.py), [coach.py](file:///Volumes/repos/mtgacoach/src/arenamcp/coach.py).
- **Approach**: Render bounding boxes on overlay UI for visual confirmation of card targets when GRE data is partial.

### 6. Automated Match Replay Summarizer & Post-Match Review
- **Goal**: Auto-generate a post-match breakdown after victory or defeat, highlighting key turning points and misplays.
- **Target Files**: [match_history.py](file:///Volumes/repos/mtgacoach/src/arenamcp/match_history.py), [standalone.py](file:///Volumes/repos/mtgacoach/src/arenamcp/standalone.py).
- **Approach**: Feed `action_history` and life-total change snapshots into an offline LLM prompt at match conclusion (`/analyze` command).

---

## 🔧 Infrastructure & Refactoring

### 7. Unity GRE Bridge Auto-Prewarming & Request Enrichment
- **Goal**: Pre-warm card name resolution (`grp_id` -> name lookup) during match loading (`ConnectResp`) for both the GRE bridge and log parser pipelines, eliminating SQLite lookup overhead during decision prompt generation.
- **Context & Architecture**: Per `docs/DECISIONS.md`, observation/coaching intelligence is log-derived (for 100% cross-platform parity), while the Unity GRE bridge (`MtgaCoachBridge.dll`) remains the primary engine for **Autopilot action submissions**, **in-game HUD card position highlights**, and **replay recording**.
- **Target Files**: [gre_bridge.py](file:///Volumes/repos/mtgacoach/src/arenamcp/gre_bridge.py), [mtgadb.py](file:///Volumes/repos/mtgacoach/src/arenamcp/mtgadb.py), [gamestate.py](file:///Volumes/repos/mtgacoach/src/arenamcp/gamestate.py).

### 8. Headless PySide Qt Test Fixtures
- **Goal**: Add headless PySide QApplication test fixtures (`QT_QPA_PLATFORM=offscreen`) so desktop GUI tests (`test_window_tracking.py`, `test_desktop_hotkeys.py`) run seamlessly in headless CI environments.
- **Target Files**: [tests/conftest.py](file:///Volumes/repos/mtgacoach/tests/conftest.py), `tests/test_window_tracking.py`.

### 9. GitHub Actions Automated Release Building
- **Goal**: Fully automate tag-triggered release packaging for `mtgacoach-Setup.exe` (Windows Inno Setup) and `.whl` artifacts on GitHub Actions.
- **Target File**: `.github/workflows/installer.yml`.

---

## 📊 Maintenance & Hygiene

- [ ] Periodically update Scryfall & 17Lands local cache files for new MTG set releases.
- [ ] Monitor LiteLLM gateway token throughput and power draw metrics on dual Blackwell GPUs.
- [ ] Re-verify BepInEx 6 macOS IL2CPP support status as upstream releases mature.
