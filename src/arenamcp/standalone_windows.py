"""Turn snapshot normalization and priority window analysis mixin for Standalone Coach."""

from __future__ import annotations

import logging
import re
from typing import Any

from arenamcp.action_planner import ActionPlan, ActionType, GameAction

logger = logging.getLogger(__name__)


class _StandaloneWindowsMixin:
    """Window analysis, snapshot normalization, and advice filtering helpers."""

    def _actions_to_event_payload(self, plan: Any, game_state: dict[str, Any]) -> list[dict[str, Any]]:
        """Convert an ActionPlan into a list of suggested_action dicts for
        the match overlay. Each dict carries the instance_id the BepInEx
        bridge uses to look up the on-screen rectangle.
        """
        payload: list[dict[str, Any]] = []
        if not plan or not getattr(plan, "actions", None):
            return payload

        # Build a name → instance_id map from the battlefield + hand so we can
        # resolve target creatures to their GRE instance IDs.
        name_to_iid: dict[str, int] = {}
        for zone_key in ("hand", "battlefield", "battlefield_player", "battlefield_opponent"):
            zone = game_state.get(zone_key) or []
            if not isinstance(zone, list):
                continue
            for card in zone:
                if not isinstance(card, dict):
                    continue
                cname = card.get("name") or ""
                iid = card.get("instance_id") or 0
                if cname and iid and cname not in name_to_iid:
                    name_to_iid[cname] = int(iid)

        for action in plan.actions:
            ref = getattr(action, "gre_action_ref", None)
            primary_iid = 0
            primary_grp = 0
            if ref is not None:
                primary_iid = int(getattr(ref, "instance_id", 0) or 0)
                primary_grp = int(getattr(ref, "grp_id", 0) or 0)
            if not primary_iid and action.card_name:
                primary_iid = name_to_iid.get(action.card_name, 0)

            # Collect target / attacker / blocker instance IDs
            target_iids: list[int] = []
            for name in getattr(action, "target_names", []) or []:
                iid = name_to_iid.get(name, 0)
                if iid:
                    target_iids.append(iid)
            for name in getattr(action, "attacker_names", []) or []:
                iid = name_to_iid.get(name, 0)
                if iid and iid != primary_iid:
                    target_iids.append(iid)
            for blocker_name, attacker_name in (getattr(action, "blocker_assignments", {}) or {}).items():
                biid = name_to_iid.get(blocker_name, 0)
                aiid = name_to_iid.get(attacker_name, 0)
                if biid:
                    target_iids.append(biid)
                if aiid:
                    target_iids.append(aiid)

            payload.append(
                {
                    "action_type": action.action_type.value,
                    "card_name": action.card_name or "",
                    "instance_id": primary_iid,
                    "grp_id": primary_grp,
                    "target_instance_ids": target_iids,
                    "reason": action.reasoning or "",
                }
            )
        return payload

    @classmethod
    def _normalize_turn_snapshot(cls, game_state: dict[str, Any]) -> dict[str, Any]:
        """Repair stale turn ownership in a local snapshot using strong signals."""
        turn = game_state.get("turn")
        if not isinstance(turn, dict):
            return game_state

        # When the bridge is connected, the turn payload already comes from
        # MtgGameState and priority is sourced from deciding_player.
        # Do not overwrite that authoritative engine state with heuristics.
        if game_state.get("_bridge_connected") or game_state.get("bridge_connected"):
            return game_state

        local_seat = cls._get_local_seat_from_state(game_state)
        if local_seat is None:
            return game_state

        opponent_seat = next(
            (
                player.get("seat_id")
                for player in game_state.get("players", [])
                if player.get("seat_id") != local_seat
            ),
            None,
        )

        decision_type = ((game_state.get("decision_context") or {}).get("type") or "").lower()
        raw_actions = game_state.get("legal_actions_raw") or []
        action_types = {
            action.get("actionType")
            for action in raw_actions
            if isinstance(action, dict) and action.get("actionType")
        }
        phase = turn.get("phase", "")
        stack = game_state.get("stack", []) or []

        inferred_active = None
        if decision_type == "declare_attackers" or action_types & {
            "ActionType_Attack",
            "ActionType_AttackWithGroup",
        }:
            inferred_active = local_seat
        elif decision_type == "declare_blockers" or action_types & {
            "ActionType_Block",
            "ActionType_BlockWithGroup",
        }:
            inferred_active = opponent_seat
        elif action_types & {"ActionType_Play", "ActionType_PlayMDFC"} and "Main" in phase and not stack:
            inferred_active = local_seat

        if inferred_active is not None and turn.get("active_player") != inferred_active:
            logger.debug(
                "Normalized active_player from %s to %s using decision/actions state",
                turn.get("active_player"),
                inferred_active,
            )
            turn["active_player"] = inferred_active

        if (
            inferred_active is not None
            or decision_type
            in {
                "actions_available",
                "declare_attackers",
                "declare_blockers",
            }
        ) and turn.get("priority_player") != local_seat:
            logger.debug(
                "Normalized priority_player from %s to %s using decision/actions state",
                turn.get("priority_player"),
                local_seat,
            )
            turn["priority_player"] = local_seat

        return game_state

    @classmethod
    def _has_meaningful_local_action_window(cls, game_state: dict[str, Any]) -> bool:
        """Return True when the local player still has a fresh actionable window."""
        turn = game_state.get("turn", {})
        local_seat = cls._get_local_seat_from_state(game_state)
        if local_seat is None:
            return False
        if turn.get("active_player") != local_seat or turn.get("priority_player") != local_seat:
            return False

        if game_state.get("pending_decision"):
            return True

        legal_actions = game_state.get("legal_actions", []) or []
        return any(cls._is_meaningful_legal_action(action) for action in legal_actions)

    @staticmethod
    def _is_meaningful_legal_action(action: str) -> bool:
        meaningful_prefixes = (
            "Cast ",
            "Play ",
            "Activate Ability",
            "Action: Activate",
            "Action: Attack",
            "Action: Block",
        )
        return str(action or "").startswith(meaningful_prefixes)

    @classmethod
    def _has_actionable_priority_window(cls, game_state: dict[str, Any]) -> bool:
        """Return True when priority is on the local player and something actionable remains."""
        turn = game_state.get("turn", {})
        local_seat = cls._get_local_seat_from_state(game_state)
        if local_seat is None or turn.get("priority_player") != local_seat:
            return False

        if game_state.get("pending_decision"):
            return True

        legal_actions = game_state.get("legal_actions", []) or []
        return any(cls._is_meaningful_legal_action(action) for action in legal_actions)

    # Local life total at/below this is dangerous enough that *any* window
    # deserves advice, regardless of the trigger (defensive bias-to-speak).
    # Matches the GameStateTrigger low-life threshold (coach.py life_threshold
    # default 5).
    _MEANINGFUL_LOW_LIFE = 5

    # Triggers gated by the meaningful-window predicate. Deliberately scoped to
    # the noisy "filler" triggers only — pass/priority/what's-next windows that
    # are frequently empty. Real decision points are intentionally absent:
    # critical triggers always fire; combat_attackers/combat_blockers are genuine
    # attack/block decisions (combat_blockers fires on the opponent's turn, where
    # gating could wrongly silence a real block); new_turn is the per-turn plan.
    _MEANINGFUL_GATE_TRIGGERS = frozenset(
        {"priority_gained", "opponent_turn", "land_played", "spell_resolved"}
    )

    @classmethod
    def _is_meaningful_advice_window(
        cls,
        game_state: dict[str, Any],
        *,
        has_castable_instants: bool = False,
    ) -> bool:
        """Pure predicate: does this window represent a real decision worth advice?

        Used to gate NON-CRITICAL triggers in the coaching loop. Critical
        triggers (``decision_required`` with a real pending decision,
        ``low_life``, ``threat_detected``, ``stack_spell*``, ...) bypass this
        entirely and always fire — they are meaningful by definition.

        A window is *meaningful* (speak) when the local player actually has a
        choice:
          - a real, named pending decision (scry/discard/target/mulligan/modal/
            pay_costs/declare_*) — the generic ``"Action Required"`` priority
            placeholder is NOT a decision by itself, so it falls through to the
            legal-action analysis, OR
          - the local player holds priority and has a meaningful legal action
            (cast/play/activate/attack/block), OR
          - the local player can respond at instant speed during a window where
            priority is not theirs (opponent's turn / opponent's spell), OR
          - the local player's life is dangerously low.

        It is *trivial* (skip) only when the window is a pure pass/wait window:
        pass-only local priority with no castable instants, the opponent holds
        priority and we have no instant-speed response, or empty legal moves
        with no pending decision.

        Biased toward returning True (speak) when uncertain — missing a real
        decision is worse than an occasional redundant line. ``game_state`` is
        read-only; ``has_castable_instants`` is supplied by the caller
        (``GameStateTrigger._has_castable_instants``) so this stays pure and
        unit-testable without an instance.
        """
        turn = game_state.get("turn", {}) or {}
        local_seat = cls._get_local_seat_from_state(game_state)

        # 1. A real, named pending decision is always meaningful. The generic
        #    "Action Required" priority placeholder is not a decision on its own.
        pending = str(game_state.get("pending_decision") or "").strip()
        if pending and pending != "Action Required":
            return True

        # 2. Dangerously low local life — always worth advice (defensive bias;
        #    the low_life critical trigger covers the common transition, this
        #    keeps later non-critical windows talking while at risk).
        for player in game_state.get("players", []) or []:
            if player.get("is_local"):
                life = player.get("life_total")
                if isinstance(life, (int, float)) and life <= cls._MEANINGFUL_LOW_LIFE:
                    return True
                break

        legal_actions = [str(action or "").strip() for action in (game_state.get("legal_actions", []) or [])]
        is_local_priority = local_seat is not None and turn.get("priority_player") == local_seat

        # 3. Local player holds priority with a real legal play.
        if is_local_priority and any(cls._is_meaningful_legal_action(action) for action in legal_actions):
            return True

        # 4. Priority is not the local player's (opponent turn / opponent spell):
        #    meaningful only if we can respond at instant speed.
        if not is_local_priority:
            return bool(has_castable_instants)

        # 5. Local priority but no meaningful legal play. Pure pass/wait (or
        #    empty) is trivial unless we hold an instant-speed response. Any
        #    unrecognized non-pass/wait legal action biases toward speaking.
        non_pass = [
            action for action in legal_actions if action.lower() not in ("", "wait", "pass", "pass priority")
        ]
        if non_pass:
            return True
        return bool(has_castable_instants)

    @classmethod
    def _summarize_actionable_window(cls, game_state: dict[str, Any], max_items: int = 2) -> str:
        """Build a short debug summary for a stuck/actionable priority window."""
        pending_decision = str(game_state.get("pending_decision") or "").strip()
        legal_actions = [str(action or "").strip() for action in (game_state.get("legal_actions", []) or [])]

        if pending_decision:
            actions = legal_actions[:max_items]
        else:
            actions = [action for action in legal_actions if cls._is_meaningful_legal_action(action)][
                :max_items
            ]

        normalized: list[str] = []
        for action in actions:
            compact = " ".join(action.split())
            if len(compact) > 72:
                compact = compact[:69].rstrip() + "..."
            normalized.append(compact)

        summary = "; ".join(normalized) if normalized else f"legal={len(legal_actions)}"
        if pending_decision:
            return f"decision={pending_decision} | {summary}"
        return summary

    @staticmethod
    def _is_garbled(text: str, threshold: float = 0.4) -> bool:
        """Detect garbled VLM output (e.g. non-vision model processing image tokens).

        Returns True if the text has an abnormally high ratio of punctuation
        and special characters relative to alphanumeric + space content.
        """
        if not text or len(text) < 20:
            return False
        alnum_space = sum(1 for c in text if c.isalnum() or c.isspace())
        ratio = alnum_space / len(text)
        return ratio < threshold

    # Phrases that mark a "do nothing" line, and the action verbs that
    # override them (a line with a real verb is never treated as passive,
    # e.g. "Decline the optional action and pass priority").
    _PASSIVE_PHRASES = (
        "wait",
        "pass",
        "pass priority",
        "no actions",
        "wait for opponent",
        "opponent has priority",
    )
    _ACTION_VERBS = (
        "cast",
        "play",
        "attack",
        "block",
        "activate",
        "kill",
        "destroy",
        "decline",
        "accept",
        "choose",
        "select",
        "keep",
        "mulligan",
        "bottom",
    )

    @classmethod
    def _is_passive_advice(cls, text: str) -> bool:
        """True for short "do nothing" advice (Wait / Pass) with no real action.

        A line is passive only if it matches a silence phrase, contains no
        action verb, and is short — so "Decline the optional action and pass
        priority" (has "decline") is NOT passive. Shared by speak_advice (TTS
        mute) and the coaching loop (skip filler advice the model passed on).
        """
        if not text:
            return False
        clean = text.lower().strip(" .!")
        # Word-boundary match: a bare substring check muted any short advice
        # mentioning "Fabled Passage" because it contains "pass".
        is_passive = any(re.search(rf"\b{re.escape(p)}\b", clean) for p in cls._PASSIVE_PHRASES)
        has_action = any(re.search(rf"\b{re.escape(v)}", clean) for v in cls._ACTION_VERBS)
        return is_passive and not has_action and len(text) < 60

    # A pass narration ("Passing priority to let their spell resolve...") is
    # worth hearing once — the same explanation re-phrased at every priority
    # window of the opponent's turn is noise. speak_advice rate-limits these.
    _PASS_NARRATION_COOLDOWN_S = 45.0

    @classmethod
    def _is_pass_narration(cls, text: str) -> bool:
        """True for advice whose substance is "I'm passing priority"."""
        clean = (text or "").lower().strip(" .!")
        if clean.startswith(("pass priority", "passing priority", "pass the priority")):
            return True
        return clean.endswith(
            ("pass priority", "passing priority", "pass priority now", "passing priority now")
        )

    @classmethod
    def _is_mulligan_pending(cls, curr_state: dict) -> bool:
        """Check if a mulligan decision is currently pending or in progress."""
        if not curr_state or not isinstance(curr_state, dict):
            return False

        # 1. Check explicit pending_decision
        pending = curr_state.get("pending_decision")
        if pending:
            pending_str = str(pending).lower()
            if "mulligan" in pending_str:
                return True

        # 2. Check bridge trigger / request
        bridge_trig = curr_state.get("_bridge_trigger") or {}
        if isinstance(bridge_trig, dict):
            req_type = str(
                bridge_trig.get("_bridge_request_type")
                or bridge_trig.get("request_type")
                or ""
            ).lower()
            req_class = str(bridge_trig.get("_bridge_request_class") or "").lower()
            if "mulligan" in req_type or "mulligan" in req_class:
                return True

        # 3. Check decision_context
        dec_ctx = curr_state.get("decision_context") or {}
        if isinstance(dec_ctx, dict):
            dtype = str(dec_ctx.get("type") or dec_ctx.get("request_type") or "").lower()
            dctx = str(dec_ctx.get("context") or dec_ctx.get("group_context") or "").lower()
            if "mulligan" in dtype or "mulligan" in dctx:
                return True

        return False


