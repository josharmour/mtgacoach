# MTGA Coach: Conversation Mode

## Goal

Add a conversational, interactive coaching session that proactively discusses game dynamics, alongside the existing turn-based spoken advice. The user can switch between **Conversation** and **Turn Advice** during a match without restarting the app.

Conversation mode should discuss changing roles, threats, resource trades, likely opponent plans, and decisions worth considering. Users can interrupt, ask follow-up questions, or request less commentary. Switching back restores concise, turn-triggered spoken instructions.

This document is an implementation plan; it does not indicate that Conversation mode has been built or deployed.

## Intended experience

| Situation | Conversation mode behavior |
| --- | --- |
| Match starts | Briefly describes the deck's plan and asks how much discussion the user wants. |
| Opponent reveals a meaningful card | Explains how it changes the likely matchup or priorities. |
| Board dynamics change | Explains shifts such as becoming the defensive player or needing to race. |
| User asks “Why not attack?” | Answers using the current position and preceding conversation. |
| An urgent decision appears | Pauses broader discussion and offers concise, actionable guidance. |
| Nothing meaningful changes | Stays quiet instead of narrating every event. |
| User switches to Turn Advice | Cancels conversational speech and resumes the existing advice flow. |

MageZero supplies supporting position evidence where compatible. The coaching LLM handles conversation and explanations. Unverified model scores remain explicitly uncalibrated.

## Swarm implementation approach

Use three specialist subagents and a coordinating agent. Assign distinct file ownership after inspecting the current repository, and agree on shared interfaces before parallel implementation. The coordinator owns integration and resolves changes that cross ownership boundaries.

### Wave 1: Inspect and design

| Agent | Responsibility | Deliverable |
| --- | --- | --- |
| Conversation agent | Map coaching triggers, session state, prompts, and game context. Design conversation memory and proactive topic selection. | Session lifecycle, memory schema, event priorities, and proposed interfaces. |
| Voice agent | Inspect microphone input, transcription, TTS, interruption, and playback ownership. Identify existing capabilities and gaps. | Voice lifecycle and cancellation design, including failure handling. |
| Desktop agent | Inspect the desktop session and settings flow. Design mode switching, listening/speaking indicators, transcript, and verbosity controls. | UI behavior and settings contract. |
| Coordinator | Trace the end-to-end lifecycle, assess MageZero evidence, define shared interfaces, and establish Turn Advice regression checks. | Integrated design, bounded implementation tasks, and file ownership. |

Start by inspecting existing coaching, desktop session, TTS, backend, and MageZero integration code. Reuse working components rather than introducing a second independent coaching pipeline.

The first deliverable is one concrete design grounded in the current code, not four independent implementations.

### Wave 2: Build a usable vertical slice

1. **Explicit session modes**
   - Persist the selected mode.
   - Allow mid-match switching without an app restart.
   - Cancel obsolete queued speech and requests when the mode changes.
   - Preserve the existing Turn Advice behavior.

2. **One conversation controller**
   - Combine game events, user questions, and speech state into one session.
   - Share the game-state source and AI connection across both modes.
   - Track match, state revision, and mode/session identity so obsolete responses can be discarded.

3. **Push-to-talk and typed questions**
   - Support questions such as “Explain that,” “What changed?” and “What's my plan?”
   - Ground answers in the current position and relevant conversation history.
   - Defer optional always-listening voice until the basic interaction is reliable.

4. **Interruptible speech**
   - Stop current playback immediately when the user presses push-to-talk or stops speech.
   - Ensure only one response plays at a time.
   - Cancel or ignore responses belonging to an old position, match, or mode.

5. **Compact transcript and status**
   - Show user questions and coach responses.
   - Clearly indicate listening, thinking, and speaking states.
   - Provide a stop-speaking control and an accessible mode switch.

### Wave 3: Add proactive game-dynamics commentary

Build a topic selector driven by meaningful changes rather than every turn or log event:

- Matchup and opponent-archetype developments.
- Shifts between attacking and defending.
- Mana, card advantage, tempo, and clock changes.
- Important threats and interaction windows.
- Whether the current plan is succeeding.

Add a speaking cooldown, repetition suppression, and **Quiet / Balanced / Detailed** settings. User questions take priority over proactive commentary. Urgent game decisions interrupt longer explanations; pending questions should remain available to answer afterward when relevant.

Maintain a compact match memory containing the current plan, recently discussed topics, user questions, and relevant revealed information. Reset match-specific memory at match boundaries. Distinguish observed facts from hypotheses about hidden information.

### Wave 4: Integrate MageZero evidence

Provide structured evidence to the conversation controller:

- Model and checkpoint identity.
- Deck and position compatibility.
- Evaluated afterstates versus policy-only preferences.
- Material assessment changes where comparisons are valid.
- Explicit uncertainty and fallback reasons.

Do not let a small uncalibrated score movement automatically trigger speech or a claim about winning chances. Initially, use MageZero to support explanations while preserving the existing tactical candidate ranking.

The conversation feature must remain useful when MageZero is unavailable or the deck is unsupported. It must not describe policy preferences as evaluated outcomes or imply that a one-ply estimate is full rules-engine search.

Real Arena/XMage state and legal-action parity, followed by held-out coaching comparisons, remain prerequisites for granting experimental model evidence greater recommendation authority. These are product validation steps, not new per-generation training acceptance gates.

### Wave 5: Test and release

Reassign the specialist agents to adversarial reviews:

| Review | Coverage |
| --- | --- |
| Session review | Stale context, match transitions, repeated commentary, and mode switching. |
| Voice review | Interruption, overlapping playback, transcription failures, and slow responses. |
| Product review | Factual grounding, usefulness, verbosity, uncertainty, and unchanged Turn Advice behavior. |

Begin with recorded-game replay, then live opt-in testing. Test unavailable AI services, microphone failures, delayed responses, and mode changes during speech. The coordinator integrates fixes and runs focused regression checks before packaging a release.

Measure proactive commentary frequency, repetition, stale-response suppression, interruption responsiveness, response latency, and user-rated usefulness. Compare against Turn Advice on the same recorded games. Agree on numerical release thresholds before evaluating the results rather than choosing thresholds after seeing them.

## Acceptance criteria

- Switching modes works without restarting the app.
- Turn Advice retains its existing behavior.
- User questions interrupt speech and receive context-aware answers.
- Only one voice response plays at a time.
- Stale advice is discarded after relevant game changes.
- Match transitions clear old match context and queued speech.
- Commentary adds useful context without repeatedly restating advice.
- Unsupported MageZero positions fall back cleanly.
- Uncalibrated evidence is not presented as a verified win probability.
- AI or microphone failures leave the UI responsive and explain the problem clearly.

## Recommended first milestone

Deliver a working mode switch, push-to-talk and typed conversation, interruptible speech, and a transcript. Add proactive game-dynamics commentary after that interaction loop is reliable, then validate MageZero's contribution and prepare an opt-in desktop release.

## Hermes / local GLM 5.3 execution handoff

### Verified local setup

At handoff, this machine's `~/.hermes/config.yaml` selects provider `litellm`, model `glm-5.3-flash`, and base URL `http://10.0.0.10:8444/v1`. This gateway routes to the local GLM inference service. Hermes exposes `delegate_task`; its installed implementation inherits the parent model unless delegation configuration explicitly overrides it. The inspected delegation configuration permits five concurrent children and does not pin another model/provider.

Use the existing authenticated Hermes configuration. Do not copy credentials into this document, prompts, progress files, or source control. Recheck the effective model/provider at execution time, including any session overrides. Keep the coordinator and all workers on the local GLM endpoint. This Markdown file describes work; opening it alone does not start agents or change Hermes settings.

### Start instructions

Open Hermes with `~/repos/mtgacoach` as the working directory, confirm the local GLM model is selected, and give it this prompt:

> Read AGENTS.md and conversation-mode.md in ~/repos/mtgacoach. Implement the Conversation Mode plan using yourself as coordinator and three specialist subagents through delegate_task. Use the existing local glm-5.3-flash provider for all agents; verify effective routing before delegation. Begin by inspecting the repository and agreeing on interfaces and file ownership, then implement the first vertical slice and continue through the documented waves. Preserve unrelated work and existing Turn Advice behavior. Maintain conversation-mode-progress.md with completed work, pending work, decisions, test evidence and exact resume instructions. Do not stop at producing another plan. Do not publish, deploy, restart shared inference services, or modify training infrastructure as part of this implementation. Report concrete blockers and distinguish implemented, tested and released functionality.

### Execution rules for the coordinator

1. Read applicable repository instructions and inspect `git status` before edits. This checkout already contains unrelated work; do not reset, overwrite or broadly stage it.
2. Verify the selected model and endpoint without revealing credentials. Confirm `delegate_task` is enabled and not paused. If delegation is unavailable, record that limitation explicitly rather than claiming a swarm ran.
3. Start with three children (conversation, voice, desktop), below the configured five-child ceiling. Reduce concurrency if local inference contention causes timeouts. Do not start another model server or change GPU allocation to accommodate workers.
4. Give each worker a self-contained brief, shared contracts, exact owned files, relevant repository instructions, acceptance criteria and required reporting format. Workers must not edit files owned by another worker without coordinator agreement.
5. Coordinate shared interfaces before parallel implementation. Merge and test one working vertical slice before expanding proactive behavior. Follow the wave assignments above; the coordinator handles cross-cutting integration and review.
6. Require workers to return changed files, behavior changes, tests run and results, unresolved risks and next steps. Inspect actual changes and test evidence before marking a task complete.
7. Maintain `conversation-mode-progress.md` after each milestone and before ending a session. Record current mode/controller contracts, ownership, completed and pending tasks, exact test commands/results, blockers, and a concise next-session prompt. Do not store raw game transcripts or credentials there.
8. On resume, reread the plan, progress file, repository instructions and current diffs. Verify the recorded state rather than redoing completed work or assuming prior workers are still running.
9. Keep implementation separate from release. Source changes and passing tests do not update an installed Windows client. Record packaging and live validation as pending until actually completed.

### Infrastructure boundaries

This feature belongs in MTGA Coach. Reuse its existing AI backend and optional MageZero connection. Do not restart GLM, alter LiteLLM credentials/routing, change Hermes global configuration, run MageZero workloads on NVIDIA GPUs, or modify the training pipeline to build this feature. If an infrastructure dependency fails, diagnose and report it separately; do not silently replace local inference with an external paid provider.
