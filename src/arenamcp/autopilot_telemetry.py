"""Autopilot telemetry, debug info, bug reporting, and takeover tracking mixin.

Extracted from autopilot.py: methods are unchanged and mixed back into AutopilotEngine.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from arenamcp.action_planner import ActionPlan, ActionType, GameAction

logger = logging.getLogger(__name__)


class _AutopilotTelemetryMixin:
    """Telemetry, debug info, bug report buffering, and statistics collection."""

    def stats(self) -> dict[str, int]:
        """Execution statistics."""
        return {
            "executed": self._actions_executed,
            "skipped": self._actions_skipped,
            "plans": self._plans_completed,
        }

    @property
    def path_stats(self) -> dict[str, int]:
        """Execution path usage statistics."""
        return dict(self._path_stats)

    def get_debug_info(self) -> dict[str, Any]:
        """Collect comprehensive autopilot state for bug reports."""
        info: dict[str, Any] = {
            "state": self._state.value,
            "config": {
                "dry_run": self._config.dry_run,
                "afk_mode": self._config.afk_mode,
                "land_drop_mode": self._config.land_drop_mode,
                "auto_pass_priority": self._config.auto_pass_priority,
                "auto_resolve": self._config.auto_resolve,
                "auto_execute_delay": self._config.auto_execute_delay,
                "planning_timeout": self._config.planning_timeout,
                "prefer_deterministic": self._config.prefer_deterministic,
                "enable_vision_fallback": self._config.enable_vision_fallback,
            },
            "stats": {
                "actions_executed": self._actions_executed,
                "actions_skipped": self._actions_skipped,
                "plans_completed": self._plans_completed,
                "consecutive_failed_verifications": self._consecutive_failed_verifications,
                "consecutive_plan_failures": self._consecutive_plan_failures,
                "effective_planning_timeout": self._effective_planning_timeout,
                "path_stats": dict(self._path_stats),
            },
            "current_action_idx": self._current_action_idx,
            "land_drop_last_turn": self._land_drop_last_turn,
            "has_vision_scan": self._has_vision_scan,
            "gre_bridge_connected": self._gre_bridge.connected,
            "blocked_actions": [list(key) for key in self._blocked_action_keys],
        }

        # Current plan details
        plan = self._current_plan
        if plan:
            info["current_plan"] = {
                "trigger": plan.trigger,
                "turn_number": plan.turn_number,
                "strategy": plan.overall_strategy,
                "num_actions": len(plan.actions),
                "actions": [
                    {
                        "type": a.action_type.value,
                        "card_name": a.card_name,
                        "target_names": a.target_names,
                        "reasoning": a.reasoning,
                        "confidence": a.confidence,
                        "has_gre_ref": a.gre_action_ref is not None,
                    }
                    for a in plan.actions
                ],
            }
        else:
            info["current_plan"] = None

        # Screen mapper state
        try:
            info["screen_mapper"] = {
                "window_rect": self._mapper.window_rect,
                "cache_size": getattr(self._mapper, "cache_size", 0),
            }
        except Exception as e:
            logger.debug(f"Could not read screen_mapper state: {e}")
            info["screen_mapper"] = {"error": "unavailable"}

        # Planner backend info
        try:
            backend = self._planner._backend
            info["planner_backend"] = type(backend).__name__
            info["planner_model"] = getattr(backend, "model", "unknown")
        except Exception as e:
            logger.debug(f"Could not read planner state: {e}")
            info["planner_backend"] = "unavailable"

        # Vision mapper info
        if self._vision_mapper:
            try:
                info["vision_mapper"] = {
                    "backend": type(self._vision_mapper._backend).__name__,
                    "model": getattr(self._vision_mapper._backend, "model", "unknown"),
                    "cache_size": len(self._vision_mapper._cache),
                }
            except Exception as e:
                logger.debug(f"Could not read vision_mapper state: {e}")
                info["vision_mapper"] = {"error": "unavailable"}

        return info

    def _record_autopilot_decision(
        self,
        game_state: dict[str, Any],
        trigger: str,
        action_type: str,
        summary: str,
    ) -> None:
        """Emit a synthetic advice-history entry for an autopilot decision."""
        fn = getattr(self, "_advice_recorder", None)
        if not callable(fn):
            return
        try:
            fn(
                advice=f"[autopilot] {action_type}: {summary}",
                trigger=trigger,
                game_state=game_state,
            )
        except Exception as e:
            logger.debug(f"_record_autopilot_decision failed: {e}")

    def _maybe_record_trajectory(
        self,
        game_state: dict[str, Any],
        trigger: str,
        legal_actions: list[str] | None,
        decision_context: dict[str, Any] | None,
        plan: ActionPlan | None,
        latency_ms: float,
    ) -> None:
        """Record this planning decision to an attached TrajectoryRecorder."""
        recorder = getattr(self, "_trajectory_recorder", None)
        if recorder is None:
            return
        try:
            from arenamcp.action_planner import AUTOPILOT_SYSTEM_PROMPT, plan_fallback_reason

            prompt_user = self._planner._build_action_prompt(
                game_state, trigger, legal_actions, decision_context
            )
            planned = plan.actions[0] if (plan and plan.actions) else None
            request_type = (
                game_state.get("_bridge_request_type")
                or game_state.get("_bridge_request_class")
                or trigger
            )
            recorder.record_decision(
                game_state=game_state,
                prompt_system=AUTOPILOT_SYSTEM_PROMPT,
                prompt_user=prompt_user,
                planned_action=planned,
                request_type=request_type,
                latency_ms=latency_ms,
                fallback_reason=plan_fallback_reason(plan),
            )
        except Exception as e:
            logger.debug(f"_maybe_record_trajectory failed (ignored): {e}")

    def _report_fallback_bug(
        self,
        action: GameAction,
        game_state: dict[str, Any],
        reason_tag: str,
    ) -> None:
        """Immediately dispatch a deduped bridge-miss bug report."""
        if self._bug_report_fn is None:
            return

        if reason_tag == "planner_action_stale":
            return

        req_type = game_state.get("_bridge_request_type") or ""
        req_class = game_state.get("_bridge_request_class") or ""
        if (
            game_state.get("_bridge_in_intermission")
            or game_state.get("match_ended")
            or req_type.startswith("Intermission")
            or req_class.startswith("Intermission")
        ):
            return

        dedupe_key = (reason_tag,) + self._action_block_key(action, game_state)
        if dedupe_key in self._reported_bridge_bug_keys:
            return
        self._reported_bridge_bug_keys.add(dedupe_key)

        gre_ref = getattr(action, "gre_action_ref", None)
        ref_info = None
        if gre_ref is not None:
            try:
                ref_info = (
                    gre_ref.to_dict()
                    if hasattr(gre_ref, "to_dict")
                    else {
                        "action_type": getattr(gre_ref, "action_type", ""),
                        "grp_id": getattr(gre_ref, "grp_id", 0),
                        "instance_id": getattr(gre_ref, "instance_id", 0),
                    }
                )
            except Exception:
                ref_info = None

        bridge_info = {
            "connected": getattr(self._gre_bridge, "connected", False),
            "failed_methods": sorted(self._gre_bridge_failed_methods),
        }

        extra = {
            "auto_fallback_bug": {
                "reason_tag": reason_tag,
                "action_type": action.action_type.value,
                "card_name": action.card_name or "",
                "target_names": list(action.target_names or []),
                "attacker_names": list(action.attacker_names or []),
                "blocker_assignments": dict(action.blocker_assignments or {}),
                "select_card_names": list(action.select_card_names or []),
                "modal_index": action.modal_index,
                "numeric_value": action.numeric_value,
                "gre_action_ref": ref_info,
                "bridge": bridge_info,
                "bridge_request_type": game_state.get("_bridge_request_type"),
                "bridge_request_class": game_state.get("_bridge_request_class"),
                "decision_context": game_state.get("decision_context"),
                "turn": (game_state.get("turn") or {}).get("turn_number"),
                "phase": (game_state.get("turn") or {}).get("phase"),
                "timestamp": time.time(),
            }
        }
        reason = (
            f"auto: bridge fallback ({reason_tag}) on "
            f"{action.action_type.value} {action.card_name or ''}".strip()
        )
        try:
            threading.Thread(
                target=self._bug_report_fn,
                args=(reason, extra),
                daemon=True,
            ).start()
        except Exception as e:
            logger.debug(f"fallback-bug dispatch failed: {e}")

    def _record_user_takeover(
        self,
        plan: Any,
        game_state: dict[str, Any],
        reason: str,
    ) -> None:
        """Record a user-takeover event for end-of-match telemetry."""
        if self._bug_report_fn is None:
            return

        actions = getattr(plan, "actions", None) or []
        first = actions[0] if actions else None
        action_type = getattr(first, "action_type", None)
        action_type_str = action_type.value if action_type else "?"
        card_name = getattr(first, "card_name", "") or ""

        if action_type in (ActionType.PASS_PRIORITY, ActionType.RESOLVE, None):
            logger.debug(
                f"user takeover ({reason}) on planned {action_type_str} — benign, not buffering a bug report"
            )
            return

        if time.time() - self._last_exec_success_ts < 10.0:
            logger.debug(
                f"plan stale ({reason}) but autopilot executed "
                f"{time.time() - self._last_exec_success_ts:.1f}s ago — own "
                "action advanced the state, not a user takeover"
            )
            return

        extra = {
            "auto_user_takeover": {
                "reason_tag": reason,
                "planned_action": action_type_str,
                "planned_card": card_name,
                "planned_strategy": getattr(plan, "overall_strategy", ""),
                "planned_voice_advice": getattr(plan, "voice_advice", ""),
                "num_planned_actions": len(actions),
                "bridge_connected": getattr(self._gre_bridge, "connected", False),
                "bridge_request_type": game_state.get("_bridge_request_type"),
                "bridge_request_class": game_state.get("_bridge_request_class"),
                "decision_context": game_state.get("decision_context"),
                "turn": (game_state.get("turn") or {}).get("turn_number"),
                "phase": (game_state.get("turn") or {}).get("phase"),
                "timestamp": time.time(),
            }
        }
        reason_str = (
            f"auto: user took over from autopilot ({reason}) — planned {action_type_str} {card_name}".strip()
        )
        self._pending_fallback_bugs.append((reason_str, extra))
        self._recent_takeovers.append(
            {
                "ts": time.time(),
                "action_type": action_type_str,
                "card_name": card_name.strip().lower(),
                "extra": extra,
            }
        )
        del self._recent_takeovers[:-10]
        self._notify(
            "AUTOPILOT",
            f"Standing down — your move (it wanted: {action_type_str} {card_name})".strip(),
        )

    def _reclassify_matching_takeovers(self, action: "GameAction") -> None:
        """Relabel provisional takeovers this verified execution disproves."""
        action_type = getattr(action, "action_type", None)
        name = (getattr(action, "card_name", "") or "").strip().lower()
        atype_str = action_type.value if action_type else ""
        now = time.time()
        for rec in self._recent_takeovers:
            if now - rec["ts"] > 30.0:
                continue
            if rec["action_type"] != atype_str:
                continue
            if rec["card_name"] and name and rec["card_name"] != name:
                continue
            tk = rec["extra"].get("auto_user_takeover")
            if isinstance(tk, dict) and tk.get("reason_tag") != "self_recovered_replan":
                tk["original_reason_tag"] = tk.get("reason_tag")
                tk["reason_tag"] = "self_recovered_replan"
                logger.info(
                    f"Reclassified takeover record ({atype_str} {name!r}) — "
                    "autopilot executed it moments later"
                )

    def flush_fallback_bugs_for_match(self) -> int:
        """Dispatch up to N sampled fallback bugs from the current match."""
        buf = self._pending_fallback_bugs
        self._pending_fallback_bugs = []
        buf = [
            (reason, extra)
            for reason, extra in buf
            if (extra.get("auto_user_takeover") or {}).get("reason_tag") != "self_recovered_replan"
        ]
        if not buf or self._bug_report_fn is None:
            return 0

        import random

        cap = max(1, int(self._max_fallback_bugs_per_match))
        if len(buf) <= cap:
            picked = list(buf)
        else:
            picked = random.sample(buf, cap)

        logger.info(f"Flushing {len(picked)}/{len(buf)} fallback bug(s) from this match (max {cap})")
        for reason, extra in picked:
            try:
                threading.Thread(
                    target=self._bug_report_fn,
                    args=(reason, extra),
                    daemon=True,
                ).start()
            except Exception as e:
                logger.debug(f"flush-bug dispatch failed: {e}")
        return len(picked)
