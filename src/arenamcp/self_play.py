"""Self-play orchestrator for MTGA bot battles.

Runs a named pipe server at \\\\.\\pipe\\mtgacoach_botbattle_v2. When MTGA requests
decisions for local/opponent players in a bot battle, this script queries the
respective model backend (e.g. Champion vs Challenger) via ActionPlanner, converts the
chosen action to a GRE submission command, and logs the decision trajectory.

Usage:
    python -m arenamcp.self_play \\
        --local-backend ollama:gemma4:latest \\
        --opponent-backend ollama:gemma4:challenger \\
        --matches 5 \\
        --sets EOE
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Ensure in-repo src and repo root are importable
REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import contextlib

from tools.eval.run import BackendSpec

from arenamcp.action_planner import (
    _LEGACY_FALLBACK_STRATEGY_TAGS,
    AUTOPILOT_SYSTEM_PROMPT,
    ActionPlanner,
    ActionType,
    GameAction,
    plan_fallback_reason,
)
from arenamcp.gre_action_matcher import match_action_to_gre
from arenamcp.gre_bridge import (
    BotBattlePipeServer,
    GREBridge,
    enrich_snapshot_from_pending_response,
)

logger = logging.getLogger("arenamcp.self_play")

# Default backend for autonomous self-play (local vLLM, OpenAI-compatible).
# Used for BOTH seats when --local-backend/--opponent-backend are omitted.
DEFAULT_BACKEND = "openai-compatible|http://localhost:8003/v1|gemma-4-12b-it"

# Port the GRE bridge server binds for the BepInEx plugin to connect to.
GRE_BRIDGE_PORT = 44222


def _backend_base_url(spec: str) -> str | None:
    """Extract the OpenAI-compatible base_url from a backend spec, if any.

    Only ``openai-compatible|<base_url>|<model>`` specs expose a base URL we can
    health-check. Other forms (``ollama:``, ``online:``) return None.
    """
    if spec and spec.startswith("openai-compatible|"):
        parts = spec.split("|")
        if len(parts) >= 3 and parts[1].strip():
            return parts[1].strip().rstrip("/")
    return None


def vllm_preflight(base_urls: list[str], timeout: float = 3.0) -> dict[str, tuple[bool, Any]]:
    """GET ``<base_url>/models`` for each distinct base URL.

    Returns a mapping ``{base_url: (ok, detail)}`` where ``detail`` is the list
    of available model ids on success or an error string on failure.
    """
    results: dict[str, tuple[bool, Any]] = {}
    for url in base_urls:
        models_url = url.rstrip("/") + "/models"
        try:
            req = urllib.request.Request(models_url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
            try:
                data = json.loads(body)
                ids = [m.get("id") for m in data.get("data", []) if isinstance(m, dict)]
            except Exception:
                ids = []
            results[url] = (True, ids)
            logger.info(f"vLLM preflight OK: {models_url} -> models={ids}")
        except Exception as e:  # urllib.error.URLError, socket.timeout, etc.
            results[url] = (False, str(e))
            logger.warning(f"vLLM preflight FAILED: {models_url} -> {e}")
    return results


def _import_proton_launch():
    """Best-effort import of the Proton launcher module.

    Returns the module or None if it is not available in this build.
    """
    try:
        from arenamcp import proton_launch  # type: ignore

        return proton_launch
    except Exception as e:  # pragma: no cover - depends on platform build
        logger.debug(f"arenamcp.proton_launch unavailable: {e}")
        return None


def mtga_is_running() -> bool:
    """True if an MTGA process is currently running (Proton or native)."""
    pl = _import_proton_launch()
    if pl is not None and hasattr(pl, "is_mtga_running"):
        try:
            return bool(pl.is_mtga_running())
        except Exception:
            pass
    try:
        from arenamcp.desktop.runtime import is_mtga_running

        return bool(is_mtga_running())
    except Exception:
        return False


def find_mtga_install() -> tuple[str | None, str]:
    """Locate the MTGA install directory. Returns ``(path_or_None, source)``."""
    pl = _import_proton_launch()
    if pl is not None and hasattr(pl, "find_mtga_install"):
        try:
            res = pl.find_mtga_install()
            if isinstance(res, tuple):
                return res
            if res:
                return (str(res), "proton_launch")
        except Exception:
            pass
    try:
        from arenamcp.desktop.runtime import find_mtga_install_dir

        return find_mtga_install_dir()
    except Exception:
        return (None, "not_found")


def _port_bindable(port: int = GRE_BRIDGE_PORT) -> bool:
    """True if 127.0.0.1:port can be bound right now (i.e. it is free)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        with contextlib.suppress(Exception):
            s.close()


def ensure_mtga_running() -> None:
    """Launch MTGA via the Proton launcher if it is not already running.

    Prefers ``arenamcp.proton_launch`` (per product design); falls back to the
    desktop runtime launcher if that module is unavailable.
    """
    if mtga_is_running():
        logger.info("MTGA already running; skipping launch.")
        return

    logger.info("MTGA not running; launching...")
    pl = _import_proton_launch()
    if pl is not None and hasattr(pl, "launch_mtga"):
        pl.launch_mtga()
        if hasattr(pl, "wait_for_mtga_process"):
            pl.wait_for_mtga_process()
        else:
            for _ in range(120):
                if mtga_is_running():
                    break
                time.sleep(1.0)
        logger.info("MTGA launch initiated via proton_launch.")
        return

    # Fallback: desktop runtime launcher.
    logger.warning("arenamcp.proton_launch unavailable; using desktop.runtime launcher fallback.")
    try:
        from arenamcp.desktop.runtime import find_mtga_install_dir
        from arenamcp.desktop.runtime import launch_mtga as rt_launch
    except Exception as e:
        logger.error(f"No MTGA launcher available: {e}")
        sys.exit(5)
    install_dir, _ = find_mtga_install_dir()
    if not install_dir:
        logger.error("Cannot launch MTGA: install directory not found.")
        sys.exit(5)
    rt_launch(install_dir)
    for _ in range(120):
        if mtga_is_running():
            break
        time.sleep(1.0)
    logger.info("MTGA launch initiated via desktop.runtime fallback.")


def run_dry_run(local_spec: str, opp_spec: str) -> int:
    """Check self-play readiness and report. Returns process exit code.

    Does NOT launch MTGA or start any match.
    """
    logger.info("=== self-play readiness check (--dry-run) ===")
    ok = True

    # 1. MTGA install location
    install_dir, source = find_mtga_install()
    if install_dir:
        logger.info(f"MTGA install: FOUND ({source}) -> {install_dir}")
    else:
        logger.error("MTGA install: NOT FOUND")
        ok = False

    # 2. MTGA process state (informational)
    running = mtga_is_running()
    logger.info(f"MTGA process running: {running}")

    # 3. GRE bridge port availability
    bindable = _port_bindable(GRE_BRIDGE_PORT)
    if bindable:
        logger.info(f"GRE bridge port {GRE_BRIDGE_PORT}: bindable (free)")
    else:
        logger.error(
            f"GRE bridge port {GRE_BRIDGE_PORT}: NOT bindable (in use). "
            "Stop the desktop coach/bridge before running self-play."
        )
        ok = False

    # 4. vLLM reachability for each distinct openai-compatible base_url
    base_urls: list[str] = []
    for spec in (local_spec, opp_spec):
        b = _backend_base_url(spec)
        if b and b not in base_urls:
            base_urls.append(b)
    if base_urls:
        results = vllm_preflight(base_urls)
        for url, (good, _detail) in results.items():
            if not good:
                logger.error(f"vLLM unreachable: {url}")
                ok = False
    else:
        logger.info("No openai-compatible backends to preflight.")

    if ok:
        logger.info("=== readiness: OK ===")
        return 0
    logger.error("=== readiness: NOT READY ===")
    return 1


def find_instance_id_by_name(name: str, battlefield: list[dict]) -> int | None:
    name_l = name.lower()
    for card in battlefield:
        cname = (card.get("name") or "").lower()
        if cname == name_l or (name_l in cname) or (cname in name_l):
            return int(card.get("instance_id") or 0)
    return None


# WP-0.4: `plan_fallback_reason` now lives in action_planner.py beside ActionPlan,
# because the real-match capture path (autopilot -> TrajectoryRecorder) needs the
# same classification and must not import self_play to get it. It is re-exported
# here so existing callers and tests keep working.
#
# It reads the structured `ActionPlan.fallback_reason` set at the source, instead
# of sniffing an "[auto-pick]" prefix out of overall_strategy — that prefix is a
# human-facing debug string, and tying contamination filtering to its exact
# wording meant a harmless copy edit could silently stop tagging fallbacks.
_FALLBACK_STRATEGY_TAGS = _LEGACY_FALLBACK_STRATEGY_TAGS


def describe_raw_action(raw: dict) -> str:
    """Human/greppable description of a raw GRE action dict."""
    atype = str(raw.get("actionType") or "ActionType_Unknown")
    fields = [f"{k}={raw.get(k)}" for k in ("grpId", "instanceId", "abilityGrpId") if raw.get(k)]
    return f"{atype}({', '.join(fields)})" if fields else atype


def resolve_match_winner(players: list[dict]) -> str | None:
    """Resolve the winning seat label from player statuses.

    Returns ``"local"``/``"opp"`` (matching the bot-battle seat labels) or None
    when the outcome is not determinable.

    Seat ids are NOT a proxy for which seat the local model played: MTGA assigns
    seat 1 to whoever is on the play, so the previous ``losing_seat == 1``
    heuristic mislabeled the winner on every match where the local bot was not
    seated first — poisoning both the DPO preference direction and the gate's
    win-rate count. Attribution now follows ``is_local``, and refuses to guess
    when the flag is absent (an unlabeled record is dropped downstream; a
    wrongly labeled one trains the model backwards).
    """
    if not players:
        return None
    losers = [p for p in players if p.get("status") == "PlayerStatus_Lost"]
    if not losers:
        return None
    if len(players) > 1 and len(losers) == len(players):
        logger.warning("All players report PlayerStatus_Lost; treating match outcome as undetermined.")
        return None

    loser = losers[0]
    if loser.get("is_local") is not None:
        return "opp" if loser.get("is_local") else "local"

    local_seat = next((p.get("seat_id") for p in players if p.get("is_local")), None)
    loser_seat = loser.get("seat_id")
    if local_seat is not None and loser_seat is not None:
        return "opp" if loser_seat == local_seat else "local"

    logger.error(
        "Cannot attribute match winner: no player carries is_local "
        f"(players={[{k: p.get(k) for k in ('seat_id', 'status')} for p in players]}). "
        "Leaving winner unset rather than guessing by seat id."
    )
    return None


def map_game_action_to_pipe_command(
    action: GameAction,
    payload: dict,
    context: dict,
    game_state: dict,
) -> dict:
    """Map a planned GameAction to a raw GRE pipe command for BepInEx."""
    cmd, _info = map_game_action_to_pipe_command_ex(action, payload, context, game_state)
    return cmd


def map_game_action_to_pipe_command_ex(
    action: GameAction,
    payload: dict,
    context: dict,
    game_state: dict,
) -> tuple[dict, dict]:
    """Map a planned GameAction to a GRE pipe command, plus submission info.

    Returns ``(command, info)`` where ``info`` carries:

    * ``submitted_action`` — what was ACTUALLY sent to the GRE. This is not
      always the planned action: unresolvable card names, an empty action list,
      or a failed action match all silently substituted something else.
    * ``fallback`` / ``fallback_reason`` — set when the submission diverged from
      the plan, so the dataset builder can drop the record instead of training
      on a decision the model never made.
    """
    atype = action.action_type
    info: dict[str, Any] = {"fallback": False, "fallback_reason": "", "submitted_action": str(action)}

    def result(cmd: dict, submitted: str, reason: str = "") -> tuple[dict, dict]:
        info["submitted_action"] = submitted
        info["fallback_reason"] = reason
        info["fallback"] = bool(reason)
        return cmd, info

    # 1. Pass / Resolve
    if atype in (ActionType.PASS_PRIORITY, ActionType.RESOLVE):
        return result({"action": "submit_pass"}, "pass")

    # 2. Mulligan
    if atype in (ActionType.MULLIGAN_KEEP, ActionType.MULLIGAN_MULL):
        keep = atype == ActionType.MULLIGAN_KEEP
        return result(
            {"action": "submit_mulligan", "keep": keep},
            "mulligan_keep" if keep else "mulligan_mull",
        )

    # 3. Declare Attackers
    if atype == ActionType.DECLARE_ATTACKERS:
        battlefield = game_state.get("battlefield", [])
        opp_seat = 1
        for p in game_state.get("players", []):
            if not p.get("is_local"):
                opp_seat = p.get("seat_id") or 1
                break

        attacker_entries = []
        resolved_names: list[str] = []
        for name in action.attacker_names:
            iid = find_instance_id_by_name(name, battlefield)
            if iid is not None:
                resolved_names.append(name)
                attacker_entries.append(
                    {
                        "attackerInstanceId": iid,
                        "damageRecipient": {
                            "type": "DamageRecType_Player",
                            "playerSystemSeatId": opp_seat,
                        },
                    }
                )
        submitted = f"declare_attackers({', '.join(resolved_names) or 'none'})"
        # Fewer attackers submitted than planned = a different attack than the
        # model chose; do not credit the model for the outcome.
        reason = "attackers_unresolved" if len(resolved_names) != len(action.attacker_names) else ""
        return result({"action": "submit_attackers", "attackers": attacker_entries}, submitted, reason)

    # 4. Declare Blockers
    if atype == ActionType.DECLARE_BLOCKERS:
        bridge_blockers = payload.get("blockers") or []
        battlefield = game_state.get("battlefield", [])

        def name_of(iid: int) -> str:
            for c in battlefield:
                if int(c.get("instance_id") or 0) == iid:
                    return (c.get("name") or "").lower()
            return ""

        bridge_by_name = {}
        for b in bridge_blockers:
            iid = int(b.get("blockerInstanceId") or 0)
            n = name_of(iid)
            if n:
                bridge_by_name[n] = b

        assignments = []
        for blocker_name, attacker_name in action.blocker_assignments.items():
            bn = blocker_name.lower()
            b_entry = bridge_by_name.get(bn)
            if not b_entry:
                for k, v in bridge_by_name.items():
                    if bn in k or k in bn:
                        b_entry = v
                        break
            if not b_entry:
                continue

            blocker_id = int(b_entry["blockerInstanceId"])
            an = attacker_name.lower()
            attacker_id = None
            legal_attackers = b_entry.get("attackerInstanceIds") or []
            for aid in legal_attackers:
                cand_name = name_of(int(aid))
                if cand_name == an or (an in cand_name) or (cand_name in an):
                    attacker_id = int(aid)
                    break
            if attacker_id is None and len(legal_attackers) == 1:
                attacker_id = int(legal_attackers[0])

            if attacker_id is not None:
                assignments.append(
                    {
                        "blockerInstanceId": blocker_id,
                        "attackerInstanceIds": [attacker_id],
                    }
                )
        submitted = (
            "declare_blockers("
            + (", ".join(f"{a['blockerInstanceId']}->{a['attackerInstanceIds'][0]}" for a in assignments))
            + ")"
            if assignments
            else "declare_blockers(none)"
        )
        reason = "blockers_unresolved" if len(assignments) != len(action.blocker_assignments) else ""
        return result({"action": "submit_blockers", "blockers": assignments}, submitted, reason)

    # 5. Select Target
    if atype == ActionType.SELECT_TARGET:
        targets = payload.get("qualifiedTargets") or []
        battlefield = game_state.get("battlefield", [])
        for name in action.target_names:
            iid = find_instance_id_by_name(name, battlefield)
            if iid is not None:
                for t in targets:
                    if int(t.get("targetInstanceId") or 0) == iid:
                        return result(
                            {"action": "submit_targets", "target_instance_id": iid},
                            f"select_target({name})",
                        )
        if targets:
            first_t = int(targets[0].get("targetInstanceId") or 0)
            # The planned target was not among the qualified targets, so the
            # first legal one was substituted — the model did not choose this.
            return result(
                {"action": "submit_targets", "target_instance_id": first_t},
                f"select_target(instance {first_t})",
                "target_unmatched",
            )

    # 6. Play Land, Cast Spell, Activate Ability
    raw_actions = payload.get("actions") or payload.get("options") or payload.get("targets") or []
    if not raw_actions:
        return result({"action": "submit_pass"}, "pass", "no_raw_actions")

    game_objects = {}
    for zone_key in ("battlefield", "hand", "graveyard"):
        for card in game_state.get(zone_key, []):
            iid = int(card.get("instance_id") or 0)
            if iid:
                game_objects[iid] = card

    from arenamcp.card_db import get_card_database

    card_db = get_card_database()

    def scryfall_lookup(grp_id: int) -> str | None:
        card_info = card_db.get_card_by_arena_id(grp_id)
        return getattr(card_info, "name", None) if card_info else None

    ref = match_action_to_gre(action, raw_actions, game_objects, scryfall_lookup)
    if ref:
        # Absent GRE id fields mean "none", which GREActionRef represents as 0.
        # Comparing them raw made `None != 0` true for every action dict that
        # omitted abilityGrpId (the common case for casts and land drops), so a
        # correctly matched action still fell through to the index-0 default.
        def _id(value: Any) -> int:
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        for idx, ra in enumerate(raw_actions):
            if (
                ra.get("actionType") == ref.action_type
                and _id(ra.get("grpId")) == _id(ref.grp_id)
                and _id(ra.get("instanceId")) == _id(ref.instance_id)
                and _id(ra.get("abilityGrpId")) == _id(ref.ability_grp_id)
            ):
                return result(
                    {"action": "submit_action", "action_index": idx, "auto_pass": False},
                    str(action),
                )

    # Nothing matched the plan. Index 0 is submitted so the match keeps moving,
    # but it is an arbitrary legal action, NOT the model's decision — tag it so
    # build_dataset.py drops the record instead of crediting the model.
    logger.warning(f"No action matched {action} (ref={ref}). Defaulting to index 0.")
    first_act = raw_actions[0]
    if first_act.get("actionType") == "ActionType_Pass":
        return result({"action": "submit_pass"}, "pass", "action_unmatched")
    return result(
        {"action": "submit_action", "action_index": 0, "auto_pass": False},
        describe_raw_action(first_act),
        "action_unmatched",
    )


class SelfPlayOrchestrator:
    """Orchestrates bot battles between two models and records trajectories."""

    def __init__(
        self,
        local_backend_spec: str,
        opp_backend_spec: str,
        trajectories_path: Path,
        license_key: str = "",
    ):
        self.local_spec = BackendSpec.parse(local_backend_spec, license_key)
        self.opp_spec = BackendSpec.parse(opp_backend_spec, license_key)
        self.trajectories_path = trajectories_path
        self.trajectories_path.parent.mkdir(parents=True, exist_ok=True)

        self.local_planner = ActionPlanner(backend=self.local_spec.build())
        self.opp_planner = ActionPlanner(backend=self.opp_spec.build())

        self.current_match_id = None
        self.current_match_winner = None
        self.decisions_log: list[dict] = []
        self._log_lock = threading.Lock()

    def handle_decision_request(self, req: dict) -> dict:
        """Called when a bot player requires a decision."""
        start_time = time.perf_counter()
        seat = req.get("seat")
        request_type = req.get("request_type")
        payload = req.get("payload") or {}
        context = req.get("context") or {}
        game_state = req.get("game_state") or {}

        match_id = game_state.get("match_id") or "unknown_match"
        self.current_match_id = match_id

        # Check for game end/winner state. Attribution is is_local-based, not
        # seat-based (see resolve_match_winner); an undetermined outcome leaves
        # the previous value alone rather than inventing a winner.
        players = game_state.get("players") or []
        winner = resolve_match_winner(players)
        if winner is not None:
            self.current_match_winner = winner

        # Enrich game state for planner
        poll = {
            "ok": True,
            "has_pending": True,
            "request_type": request_type.replace("Request", ""),
            "request_class": request_type,
            "request_payload": payload,
            "decision_context": context,
        }
        enrich_snapshot_from_pending_response(game_state, poll, bridge_connected=True)
        game_state["_bridge_request_type"] = request_type

        # Select planner and backend label
        if seat == "local":
            planner = self.local_planner
            alt_planner = self.opp_planner
            backend_label = self.local_spec.label
            alt_backend_label = self.opp_spec.label
        else:
            planner = self.opp_planner
            alt_planner = self.local_planner
            backend_label = self.opp_spec.label
            alt_backend_label = self.local_spec.label

        trigger = context.get("type") or "decision_required"
        legal_actions = game_state.get("legal_actions") or []
        legal_actions_raw = payload.get("actions") or payload.get("options") or payload.get("targets") or []

        # Build prompt messages (for logging)
        user_message = planner._build_action_prompt(game_state, trigger, legal_actions, context)

        # Plan action using active planner
        logger.info(
            f"Self-play decision: seat={seat} ({backend_label}) trigger={trigger} legal={len(legal_actions)}"
        )
        plan = planner.plan_actions(
            game_state,
            trigger,
            legal_actions=legal_actions,
            decision_context=context,
            legal_actions_raw=legal_actions_raw,
        )

        # Plan action using alternative planner (for DPO preference pairing)
        plan_alt = alt_planner.plan_actions(
            game_state,
            trigger,
            legal_actions=legal_actions,
            decision_context=context,
            legal_actions_raw=legal_actions_raw,
        )

        # Map active choice to BepInEx action command.
        #
        # WP-0.4: `submitted_action` is what actually reached the GRE, which is
        # NOT always `planned_action` — an unresolvable card name, an empty
        # action list, or a failed action match all substitute something else.
        # `fallback` is true whenever the submitted decision did not come from
        # the model (planner fallback/preflight, or a substituted submission);
        # build_dataset.py hard-excludes those records.
        plan_reason = plan_fallback_reason(plan)
        if plan.actions:
            chosen_action = plan.actions[0]
            resp_cmd, submit_info = map_game_action_to_pipe_command_ex(
                chosen_action, payload, context, game_state
            )
        else:
            chosen_action = None
            resp_cmd = {"action": "submit_pass"}
            submit_info = {
                "fallback": True,
                "fallback_reason": "planner_no_actions",
                "submitted_action": "pass",
            }

        fallback_reason = plan_reason or submit_info["fallback_reason"]
        alt_reason = plan_fallback_reason(plan_alt)

        if plan_alt.actions:
            alt_action = plan_alt.actions[0]
        else:
            alt_action = None

        elapsed_ms = (time.perf_counter() - start_time) * 1000

        # Log decision
        decision_rec = {
            "match_id": match_id,
            "ts": time.time(),
            "seat": seat,
            "backend": backend_label,
            "alt_backend": alt_backend_label,
            "request_type": request_type,
            "turn": game_state.get("turn", {}).get("turn_number", 0),
            "phase": game_state.get("turn", {}).get("phase", ""),
            "prompt_system": AUTOPILOT_SYSTEM_PROMPT,
            "prompt_user": user_message,
            "planned_action": str(chosen_action) if chosen_action else "pass",
            "submitted_action": submit_info["submitted_action"],
            "fallback": bool(fallback_reason),
            "fallback_reason": fallback_reason,
            "alt_planned_action": str(alt_action) if alt_action else "pass",
            "alt_fallback": bool(alt_reason),
            "alt_fallback_reason": alt_reason,
            "submit_command": resp_cmd,
            "latency_ms": round(elapsed_ms, 1),
        }
        if fallback_reason:
            logger.info(
                f"Decision tagged fallback ({fallback_reason}): planned={decision_rec['planned_action']!r} "
                f"submitted={decision_rec['submitted_action']!r} — excluded from training credit."
            )

        with self._log_lock:
            self.decisions_log.append(decision_rec)

        return resp_cmd

    def flush_match_logs(self):
        """Append decisions log to trajectories file and label with winner."""
        with self._log_lock:
            if not self.decisions_log:
                return
            logger.info(
                f"Flushing {len(self.decisions_log)} decisions for match {self.current_match_id} (winner={self.current_match_winner})"
            )
            if self.current_match_winner is None:
                logger.warning(
                    f"Match {self.current_match_id} ended with no attributable winner; "
                    f"its {len(self.decisions_log)} decisions carry winner=null and will be "
                    "skipped by the dataset builder."
                )
            n_fallback = sum(1 for d in self.decisions_log if d.get("fallback"))
            if n_fallback:
                logger.info(
                    f"{n_fallback}/{len(self.decisions_log)} decisions were fallbacks "
                    "(tagged, excluded from training credit)."
                )
            with open(self.trajectories_path, "a", encoding="utf-8") as f:
                for dec in self.decisions_log:
                    dec["winner"] = self.current_match_winner
                    f.write(json.dumps(dec, ensure_ascii=False) + "\n")
            self.decisions_log.clear()
            self.current_match_winner = None


def main():
    p = argparse.ArgumentParser(description="Self-play orchestrator for model tuning.")
    p.add_argument(
        "--local-backend",
        default=None,
        help=f"Spec for Player 1 (local). Defaults to {DEFAULT_BACKEND} when omitted.",
    )
    p.add_argument(
        "--opponent-backend",
        default=None,
        help=f"Spec for Player 2 (opponent). Defaults to {DEFAULT_BACKEND} when omitted.",
    )
    p.add_argument("--matches", type=int, default=1, help="Number of matches to play")
    p.add_argument("--sets", default="EOE", help="Sets to generate random decks from")
    p.add_argument(
        "--out-trajectories", type=Path, default=REPO / "tools/eval/data/self_play_trajectories.jsonl"
    )
    p.add_argument("--license-key", default=os.environ.get("MTGACOACH_LICENSE_KEY", ""))
    p.add_argument(
        "--auto",
        action="store_true",
        help="Autonomous flow: launch MTGA if needed, wait for the bridge, and run.",
    )
    p.add_argument(
        "--launch-mtga",
        action="store_true",
        help="Launch MTGA via arenamcp.proton_launch if not already running (implied by --auto).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Check readiness and exit WITHOUT launching MTGA or starting any match.",
    )
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    # Resolve backend specs. Both default to the local vLLM backend when omitted
    # (and in --auto mode). Explicit specs are always respected for back-compat.
    local_spec = args.local_backend or DEFAULT_BACKEND
    opp_spec = args.opponent_backend or DEFAULT_BACKEND

    # --dry-run: readiness check only. Never launches MTGA or starts a match.
    if args.dry_run:
        sys.exit(run_dry_run(local_spec, opp_spec))

    autonomous = args.auto or args.launch_mtga

    # vLLM reachability preflight (informational for normal runs).
    base_urls: list[str] = []
    for spec in (local_spec, opp_spec):
        b = _backend_base_url(spec)
        if b and b not in base_urls:
            base_urls.append(b)
    if base_urls:
        vllm_preflight(base_urls)

    # --auto / --launch-mtga: ensure MTGA is up before binding the bridge.
    if autonomous:
        ensure_mtga_running()

    orchestrator = SelfPlayOrchestrator(
        local_backend_spec=local_spec,
        opp_backend_spec=opp_spec,
        trajectories_path=args.out_trajectories,
        license_key=args.license_key,
    )

    # Start the named pipe server
    server = BotBattlePipeServer()
    server_thread = threading.Thread(
        target=server.start,
        args=(orchestrator.handle_decision_request,),
        daemon=True,
    )
    server_thread.start()

    # Create GRE bridge client to trigger MTGA commands
    bridge = GREBridge()
    bridge._reconnect_cooldown = 0.5
    logger.info("Connecting to MTGA BepInEx plugin...")
    connected = False
    # In autonomous mode MTGA may still be booting, so wait much longer for the
    # bridge plugin to connect (~180s) and log friendly progress.
    max_wait = 180 if autonomous else 30
    for i in range(max_wait):
        if bridge.connect():
            connected = True
            break
        if i % 10 == 0:
            logger.info(f"waiting for MTGA + bridge plugin... ({i}/{max_wait}s)")
        time.sleep(1.0)

    if not connected:
        logger.error("Could not connect to MTGA BepInEx plugin. Ensure MTGA is running.")
        sys.exit(2)

    # Start bot battle matches
    for m in range(args.matches):
        logger.info(f"Starting Match {m + 1} of {args.matches}...")
        cmd = {
            "action": "start_bot_battle",
            "sets": args.sets,
            "matches": 1,
        }

        resp = None
        for attempt in range(60):
            resp = bridge._send_safe(cmd, timeout=15.0)
            if resp and resp.get("ok"):
                break
            err = resp.get("error") if resp else "timeout/no response"
            logger.info(f"start_bot_battle attempt {attempt + 1}/60 failed: {err}. Retrying in 2s...")
            time.sleep(2.0)

        if not resp or not resp.get("ok"):
            err = resp.get("error") if resp else "timeout"
            logger.error(f"Failed to start bot battle after retries: {err}")
            sys.exit(3)

        # Wait for match to complete
        match_done = False
        while not match_done:
            time.sleep(3.0)
            status = bridge._send_safe({"action": "bot_battle_status"}, timeout=5.0) or {}
            if status.get("matches_completed", 0) >= 1:
                match_done = True
            if status.get("last_error"):
                logger.error(f"Plugin error: {status['last_error']}")
                sys.exit(4)

        # Save match logs
        orchestrator.flush_match_logs()

    logger.info("Self-play run complete!")
    server.stop()


if __name__ == "__main__":
    main()
