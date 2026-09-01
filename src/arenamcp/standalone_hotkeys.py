"""Keyboard hotkeys, backend switching, and interactive controls mixin for Standalone Coach."""

from __future__ import annotations

import logging
import sys
import threading
import time
import traceback

from arenamcp.backend_health import is_backend_error_text
from arenamcp.coach import get_models_for_mode

logger = logging.getLogger(__name__)

# The `keyboard` package must never be imported on macOS: its darwin backend
# calls abort() during import when the process lacks root/Accessibility
# rights, killing the interpreter before any except clause can run.
keyboard = None
if sys.platform != "darwin":
    try:
        import keyboard
    except ImportError:
        logger.warning("keyboard module not available - hotkeys disabled")
else:
    logger.info("keyboard hotkeys disabled on macOS (unsupported backend)")


class _StandaloneHotkeysMixin:
    """Hotkey event handlers and backend provider cycling."""

    def _on_mute_hotkey(self) -> None:
        """F5 - Toggle TTS mute."""
        if self._voice_output:
            muted = self._voice_output.toggle_mute()
            self.ui.status("VOICE", f"{'MUTED' if muted else 'UNMUTED'} (saved)")
        else:
            self.ui.status("VOICE", "TTS not enabled")

    def _on_voice_hotkey(self) -> None:
        """F6 - Change TTS voice."""
        if self._voice_output:
            voice_id, desc = self._voice_output.next_voice()
            self.ui.status("VOICE", f"Changed to: {desc} (saved)")
            try:
                self._voice_output.speak("Voice changed.", blocking=False)
            except Exception as e:
                logger.debug(f"TTS voice confirmation failed: {e}")
        else:
            self.ui.status("VOICE", "TTS not enabled")

    def _on_speed_hotkey(self) -> None:
        """F8 - Cycle TTS speed."""
        if self._voice_output:
            speed = self._voice_output.cycle_speed()
            self.ui.status("SPEED", f"{speed}x")
            try:
                self._voice_output.speak("Speed changed.", blocking=False)
            except Exception as e:
                logger.debug(f"TTS speed confirmation failed: {e}")
        else:
            self.ui.status("SPEED", "TTS not enabled")

    def _on_swap_seat_hotkey(self) -> None:
        """F8 - Swap local seat (fix wrong player detection)."""
        if not self._mcp:
            return

        try:
            from arenamcp.server import game_state

            # Get current state
            players = list(game_state.players.keys())
            current = game_state.local_seat_id

            if len(players) >= 2:
                # Swap to the other seat
                new_seat = [s for s in players if s != current][0] if current else players[0]
                # Use source=3 (User) to lock it
                game_state.set_local_seat_id(new_seat, source=3)
                self.ui.status("SEAT", f"Swapped to Seat {new_seat} (LOCKED - won't auto-change)")
                logger.info(f"Manual seat swap: {current} -> {new_seat} (locked by User)")
            else:
                self.ui.status("SEAT", f"Only {len(players)} player(s) detected, cannot swap")
        except Exception as e:
            self.ui.error(f"Seat swap failed: {e}")
            logger.error(f"Seat swap error: {e}")

    def _on_win_plan_hotkey(self, turns: int) -> None:
        """Handle win-in-N-turns hotkey press (keys 2-8)."""
        if not self._coach or not self._mcp:
            return

        # 5-second cooldown to prevent spam
        now = time.time()
        last = getattr(self, "_last_win_plan_time", 0.0)
        if now - last < 5.0:
            self.ui.status("WIN-PLAN", "Cooldown — wait a few seconds")
            return
        self._last_win_plan_time = now

        def _do():
            try:
                # Ensure latest log data is processed
                self._mcp.poll_log()
                game_state = self._mcp.get_game_state()

                turn_num = game_state.get("turn", {}).get("turn_number", 0)
                if turn_num <= 0:
                    self.ui.status("WIN-PLAN", "No active game")
                    logger.info("Win plan: no active game (turn=0)")
                    return

                self.ui.status("WIN-PLAN", f"Planning win in {turns} turns...")
                self.ui.log(f"\n[bold cyan]--- WIN-IN-{turns} PLAN (generating...) ---[/]")
                logger.info(f"Win plan: requesting {turns}-turn plan")

                library_summary = self._compute_library_summary(game_state)
                plan = self._coach.get_win_plan(game_state, turns, library_summary)

                logger.info(f"Win plan: got response, {len(plan)} chars")
                if plan:
                    self.ui.advice(plan, f"WIN-IN-{turns}")
                    self._record_advice(plan, f"win_in_{turns}", game_state=game_state)
                    self.speak_advice(plan, blocking=False)
                else:
                    self.ui.status("WIN-PLAN", "No plan generated (timeout or error)")
                    self.ui.log("[yellow]Win plan returned empty — API may have timed out[/]")
            except Exception as e:
                logger.error(f"Win plan error: {e}", exc_info=True)
                self.ui.error(f"Win plan failed: {e}")

        threading.Thread(target=_do, daemon=True).start()

    def _win_plan_worker(self, game_state: dict) -> None:
        """Background worker: compute win-in-2 and win-in-3 plans using a thinking model.

        Spawned automatically at the start of each of your turns. Uses a separate
        thinking-enabled backend so it doesn't interfere with real-time coaching.

        Parses VIABLE: YES/NO from the LLM response. Only stores viable plans
        and plays a sound alert. The plan is read aloud only on Ctrl+0 press.
        """

        try:
            # Lazy-init thinking model
            if self._thinking_model is None:
                from arenamcp.coach import pick_thinking_model

                self._thinking_model = pick_thinking_model()
                if self._thinking_model is None:
                    logger.info("No thinking model available — win plan worker disabled")
                    # Sentinel to avoid retrying every turn
                    self._thinking_model = ""
                    return
            if self._thinking_model == "":
                return  # Previously determined unavailable

            # Stagger background win plan by 3s to yield network/proxy priority
            # to instant turn-opening tactical advice
            time.sleep(3.0)

            from arenamcp.coach import ProxyBackend

            thinking_backend = ProxyBackend(model=self._thinking_model, enable_thinking=True)

            library_summary = self._compute_library_summary(game_state)
            turn_num = game_state.get("turn", {}).get("turn_number", 0)

            # Process 2-turn plan first; if viable, skip 3-turn plan to conserve bandwidth
            for n in (2, 3):
                try:
                    plan = self._coach.get_win_plan(
                        game_state,
                        n,
                        library_summary,
                        backend=thinking_backend,
                    )
                except Exception as e:
                    logger.warning(f"Win-in-{n} future failed: {e}")
                    continue

                if not plan or is_backend_error_text(plan):
                    continue

                # Parse viability from first line
                first_line = plan.split("\n", 1)[0].strip()
                is_viable = first_line.upper().startswith("VIABLE: YES") or first_line.upper().startswith(
                    "VIABLE:YES"
                )

                # Strip the VIABLE: line from the plan text
                if first_line.upper().startswith("VIABLE:"):
                    plan = plan.split("\n", 1)[1].strip() if "\n" in plan else ""

                if not is_viable:
                    logger.info(f"Win-in-{n} plan not viable, skipping")
                    continue

                # Staleness check: don't store if game has advanced >2 turns
                current_turn = 0
                try:
                    current_state = self._mcp.get_game_state()
                    current_turn = current_state.get("turn", {}).get("turn_number", 0)
                except Exception as e:
                    logger.debug(f"Could not check current turn for win plan staleness: {e}")
                if current_turn and current_turn - turn_num > 2:
                    logger.info(f"Win plan stale (started turn {turn_num}, now {current_turn})")
                    break

                logger.info(f"VIABLE win-in-{n} plan found ({len(plan)} chars)")

                # Store pending plan (no text output, no TTS — wait for Ctrl+0)
                self._pending_win_plan = plan
                self._pending_win_plan_turns = n
                self._pending_win_plan_turn = turn_num
                self._record_advice(plan, f"win_in_{n}", game_state=game_state)

                # Play ascending two-tone alert
                try:
                    from arenamcp.voice import play_beep

                    play_beep(frequency=1047, duration=0.12, volume=0.4)  # C6
                    time.sleep(0.08)
                    play_beep(frequency=1319, duration=0.12, volume=0.4)  # E6
                except Exception as e:
                    logger.debug(f"Win plan beep failed: {e}")

                self.ui.status("WIN-PLAN", f"WIN IN {n} FOUND — Ctrl+0 to hear")
                break  # First viable result wins

            if hasattr(thinking_backend, "close"):
                thinking_backend.close()

        except Exception as e:
            logger.error(f"Win plan worker error: {e}", exc_info=True)

    def _on_read_win_plan(self) -> None:
        """Numpad 0 — Read the pending win plan aloud via TTS."""
        plan = self._pending_win_plan
        if not plan:
            self.ui.status("WIN-PLAN", "No win plan available")
            return
        turns = self._pending_win_plan_turns
        self.ui.advice(plan, f"WIN-IN-{turns}")
        self.speak_advice(plan, blocking=False)
        # Clear pending state after reading
        self._pending_win_plan = None
        self.ui.status("WIN-PLAN", "")

    def _on_restart_hotkey(self) -> None:
        """F9 - Restart the coach."""
        self.ui.status("RESTART", "Restarting coach...")
        logger.info("F9 restart requested")
        self._restart_requested = True
        self._running = False

    def set_backend(self, provider: str, model: str | None = None) -> None:
        """Explicitly set the backend provider.

        Fast path: when only the model changes (same provider), swap the model
        on the existing backend instead of recreating everything.
        """
        if self.draft_mode:
            self.ui.status("PROVIDER", "Not available in draft mode")
            return

        if provider != "online":
            logger.info(f"set_backend({provider!r}) coerced to 'online' — app is online-only")
            provider = "online"

        try:
            from arenamcp.coach import CoachEngine, create_backend

            same_provider = provider == self.backend_name and self._coach is not None
            old_backend = self._coach._backend if self._coach else None

            if same_provider and old_backend is not None:
                # Fast path: just swap the model on the existing backend.
                # Close persistent session so next call starts with new model.
                if hasattr(old_backend, "close"):
                    old_backend.close()
                old_backend.model = model
                old_backend._turns = 0
                # Reset persistent-mode failure flag so new model gets a fresh try
                if hasattr(old_backend, "_persistent_failed"):
                    old_backend._persistent_failed = False
                actual_model = model or "default"
            else:
                # Full switch: close old backend, create new one
                if old_backend and hasattr(old_backend, "close"):
                    old_backend.close()
                progress_cb = self.ui.subtask if self.ui else None
                llm_backend = create_backend(provider, model=model, progress_callback=progress_cb)
                self._coach = CoachEngine(backend=llm_backend)
                actual_model = getattr(llm_backend, "model", "default")

                # Reconfigure voice input if needed
                if self._voice_input:
                    self._voice_input.transcription_enabled = True
                    logger.info("Voice transcription enabled: True")

            self.backend_name = provider
            self.model_name = model
            self.ui.status("PROVIDER", f"Switched to {provider.upper()} ({actual_model})")
            model_display = f"{provider}/{actual_model}" if actual_model else provider
            self.ui.status("MODEL", model_display)
            logger.info(f"Switched to {provider} backend, model: {actual_model}")
            self._consecutive_errors = 0  # Reset error counter on manual switch

            # Clear backend failure state — user explicitly chose a new provider
            if self._backend_failed:
                self._backend_failed = False
                self._original_backend = None
                self._original_model = None
                self._mark_backend_healthy()
                logger.info("Cleared backend failure state (user changed provider)")
        except Exception as e:
            self.ui.error(f"Failed to set provider {provider}: {e}")
            logger.error(f"Set provider error: {e}")
            logger.debug(traceback.format_exc())

    def fallback_to_local(self, reason: str = "") -> bool:
        """Report an online-backend failure. The app is online-only (local
        mode removed 2026-06-11), so there is nothing to fall back to —
        surface the error and stay on online. ``set_backend`` / a restart
        clears the failed state.

        Returns False always (no fallback happened); kept under the old name
        so the failure-detection callers don't need to change shape.
        """
        if self._backend_failed:
            return False
        self._backend_failed = True
        short_reason = (reason or "unknown error")[:180].replace("\n", " ")
        self.ui.log(f"\n[bold red]Online backend failed: {short_reason}[/]")
        self.ui.log(
            "[bold yellow]Check your subscription and connectivity — "
            "advice resumes when api.mtgacoach.com responds again.[/]\n"
        )
        self.ui.status("BACKEND", "ERROR — online unavailable")
        logger.error(f"Online backend failure (online-only, no fallback): {reason}")
        return False

    # Backward-compatible alias
    def fallback_to_ollama(self, reason: str = "") -> bool:
        return self.fallback_to_local(reason)

    def check_advice_for_backend_failure(self, advice: str) -> bool:
        """Check if an advice response indicates a backend auth/billing failure.

        Auth/billing errors (401, expired, credit) are deterministic — retrying
        won't help. These mark the backend failed immediately with a
        persistent error in the UI (online-only: there is no fallback).

        Transient errors (timeouts, rate limits) use a counter: after 3
        consecutive failures the backend is marked failed.

        Returns True if the failed state was just entered.
        """
        if not advice:
            return False

        # Already in failed state — don't keep trying to fall back
        if self._backend_failed:
            return False

        from arenamcp.backend_detect import is_query_failure_retriable

        # Detect backend errors — either prefixed "Error …" from the backend wrapper,
        # or raw short error text (e.g. "Credit balance is too low") that the CLI
        # returns as normal assistant text.  The len<200 guard prevents false
        # positives on real advice that incidentally contains words like "account".
        if is_query_failure_retriable(advice) and (is_backend_error_text(advice) or len(advice) < 200):
            self._report_backend_failure(advice)

            # Auth/billing errors are permanent — fall back immediately
            _AUTH_INDICATORS = (
                "401",
                "403",
                "authenticate",
                "unauthorized",
                "expired",
                "credit",
                "billing",
                "subscription",
                "api key",
                "not logged in",
            )
            advice_lower = advice.lower()
            is_auth_error = any(ind in advice_lower for ind in _AUTH_INDICATORS)

            if is_auth_error:
                logger.warning(f"Auth/billing failure — immediate Ollama fallback: {advice[:120]}")
                return self.fallback_to_ollama(reason=advice[:200])

            # Transient errors: count and fallback after threshold
            self._consecutive_errors = getattr(self, "_consecutive_errors", 0) + 1
            max_errors = getattr(self, "_max_errors_before_fallback", 3)
            logger.warning(
                f"Backend failure detected ({self._consecutive_errors}/{max_errors}): {advice[:120]}"
            )

            if self._consecutive_errors >= max_errors:
                return self.fallback_to_ollama(reason=advice[:200])
        else:
            # Reset counter on successful response
            self._consecutive_errors = 0
            self._mark_backend_healthy()

        return False

    def _on_provider_cycle_hotkey(self) -> None:
        """F11 - Toggle between online and local mode."""
        if self.draft_mode:
            return

        new_mode = "local" if self.backend_name == "online" else "online"
        display_name = "Online" if new_mode == "online" else "Local"

        self.ui.log(f"\n[MODE] Switching to {display_name}...")
        self.set_backend(new_mode, None)
        # Invalidate cached model list
        self._model_list_for: str | None = None
        self._model_list: list = []

    def _on_model_cycle_hotkey(self) -> None:
        """F12 - Cycle through models within the current mode."""
        if self.draft_mode:
            return

        mode = self.backend_name

        # Rebuild model list when mode changes
        if getattr(self, "_model_list_for", None) != mode:
            self._model_list = get_models_for_mode(mode)
            self._model_list_for = mode

        models = self._model_list
        if len(models) <= 1:
            self.ui.log(f"\n[MODEL] Only one model available for {mode}\n")
            return

        # Find current index
        current_idx = -1
        for i, (_, mid) in enumerate(models):
            if mid == self.model_name:
                current_idx = i
                break
        # If current model is None (default), match the None entry
        if current_idx == -1 and self.model_name is None:
            for i, (_, mid) in enumerate(models):
                if mid is None:
                    current_idx = i
                    break

        next_idx = (current_idx + 1) % len(models)
        display_name, new_model = models[next_idx]

        self.set_backend(mode, new_model)
        label = display_name if display_name != "Default" else "(default)"
        self.ui.log(f"\n[MODEL] {mode} -> {label}\n")

    def _on_style_toggle_hotkey(self) -> None:
        """F2 - Toggle advice style between Quick and Chatty."""
        # Normalize any legacy value ("concise"/"verbose") to the new names.
        current = (self.advice_style or "").lower()
        if current in ("concise", "quick"):
            self.advice_style = "chatty"
        else:
            self.advice_style = "quick"
        self.ui.status("STYLE", self.advice_style)
        label = self.advice_style.capitalize()
        self.ui.log(f"\n[STYLE] Changed to {label}\n")

    def _on_frequency_toggle_hotkey(self) -> None:
        """F3 - Toggle advice frequency.

        Two modes:
          - "every_priority": advice on every *meaningful* priority window
            (frequent, but the meaningful-window gate drops pass-only/no-instant
            filler — this is the recommended default).
          - "start_of_turn": advice once per turn (quieter).
        Both modes always fire critical triggers (decisions, low life, threats).
        """
        self.advice_frequency = (
            "every_priority" if self.advice_frequency == "start_of_turn" else "start_of_turn"
        )
        label = "EVERY DECISION" if self.advice_frequency == "every_priority" else "START OF TURN"
        self.ui.status("FREQ", label)
        self.ui.log(f"\n[FREQ] Changed to {label}\n")

    def _on_voice_cycle_hotkey(self) -> None:
        """F6 - Cycle TTS voice."""
        if self._voice_output:
            try:
                voice_id, desc = self._voice_output.next_voice()
                self.ui.status("VOICE_ID", desc)
                self.ui.log(f"\n[VOICE] Changed to: {desc}\n")
                self.speak_advice("Voice changed.", blocking=False)
            except Exception as e:
                self.ui.log(f"Error changing voice: {e}")

    def _reinit_coach(self):
        """Reinitialize the coach backend with current settings."""
        try:
            from arenamcp.coach import CoachEngine, create_backend

            llm_backend = create_backend(self.backend_name, model=self.model_name)
            self._coach = CoachEngine(backend=llm_backend)

            # Get actual model name for display if it was auto-selected
            actual_model = getattr(llm_backend, "model", self.model_name or "default")
            self.model_name = actual_model  # Sync back

            # Configure voice input based on backend
            if self._voice_input:
                enable_transcription = True
                self._voice_input.transcription_enabled = enable_transcription
                logger.info(
                    f"Voice transcription enabled: {enable_transcription} (Backend: {self.backend_name})"
                )

            logger.info(f"Re-initialized {self.backend_name} backend, model: {actual_model}")
        except Exception as e:
            self.ui.log(f"\nbackend init failed: {e}\n")
            logger.error(f"Backend init error: {e}")

    def _register_hotkeys(self) -> None:
        """Register hotkeys."""
        if not keyboard:
            return

        if not self._register_keyboard:
            # In pipe/launcher mode, only register critical global hotkeys
            # that must work even when MTGA has focus (autopilot steals it)
            try:
                keyboard.on_press_key(
                    "f1", lambda _: self._autopilot and self._autopilot.on_cancel(), suppress=False
                )
                keyboard.on_press_key(
                    "f4", lambda _: self._autopilot and self._autopilot.on_abort(), suppress=False
                )
                keyboard.on_press_key("f12", lambda _: self.toggle_autopilot(), suppress=False)
                logger.info("Global autopilot hotkeys registered (F1/F4/F12)")
            except Exception as e:
                logger.warning(f"Global hotkey registration failed: {e}")
            return

        try:
            keyboard.on_press_key("f2", lambda _: self._on_style_toggle_hotkey(), suppress=False)
            keyboard.on_press_key("f3", lambda _: self._on_frequency_toggle_hotkey(), suppress=False)
            keyboard.on_press_key("f5", lambda _: self._on_mute_hotkey(), suppress=False)
            keyboard.on_press_key("f6", lambda _: self._on_voice_cycle_hotkey(), suppress=False)
            keyboard.on_press_key("f7", lambda _: self._on_bug_report_hotkey(), suppress=False)
            keyboard.on_press_key("f8", lambda _: self._on_swap_seat_hotkey(), suppress=False)
            keyboard.on_press_key("f10", lambda _: self.run_speed_test(), suppress=False)
            keyboard.on_press_key("f11", lambda _: self._on_provider_cycle_hotkey(), suppress=False)
            keyboard.on_press_key("f12", lambda _: self._on_model_cycle_hotkey(), suppress=False)
            keyboard.add_hotkey("ctrl+0", lambda: self._on_read_win_plan(), suppress=False)
            logger.info("Hotkeys registered")
        except Exception as e:
            logger.warning(f"Hotkey registration failed: {e}")

    def _unregister_hotkeys(self) -> None:
        """Unregister hotkeys."""
        if keyboard:
            try:
                keyboard.unhook_all()
            except (ValueError, KeyError, Exception):
                pass  # Already unhooked or error
