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
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

# Ensure in-repo src and repo root are importable
REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from arenamcp.gre_bridge import (
    BotBattlePipeServer,
    GREBridge,
    enrich_snapshot_from_pending_response,
)
from arenamcp.action_planner import (
    ActionPlanner,
    ActionType,
    GameAction,
    AUTOPILOT_SYSTEM_PROMPT,
)
from arenamcp.gre_action_matcher import match_action_to_gre
from tools.eval.run import BackendSpec

logger = logging.getLogger("arenamcp.self_play")


def find_instance_id_by_name(name: str, battlefield: list[dict]) -> Optional[int]:
    name_l = name.lower()
    for card in battlefield:
        cname = (card.get("name") or "").lower()
        if cname == name_l or (name_l in cname) or (cname in name_l):
            return int(card.get("instance_id") or 0)
    return None


def map_game_action_to_pipe_command(
    action: GameAction,
    payload: dict,
    context: dict,
    game_state: dict,
) -> dict:
    """Map a planned GameAction to a raw GRE pipe command for BepInEx."""
    atype = action.action_type

    # 1. Pass / Resolve
    if atype in (ActionType.PASS_PRIORITY, ActionType.RESOLVE):
        return {"action": "submit_pass"}

    # 2. Mulligan
    if atype in (ActionType.MULLIGAN_KEEP, ActionType.MULLIGAN_MULL):
        keep = (atype == ActionType.MULLIGAN_KEEP)
        return {"action": "submit_mulligan", "keep": keep}

    # 3. Declare Attackers
    if atype == ActionType.DECLARE_ATTACKERS:
        battlefield = game_state.get("battlefield", [])
        opp_seat = 1
        for p in game_state.get("players", []):
            if not p.get("is_local"):
                opp_seat = p.get("seat_id") or 1
                break

        attacker_entries = []
        for name in action.attacker_names:
            iid = find_instance_id_by_name(name, battlefield)
            if iid is not None:
                attacker_entries.append({
                    "attackerInstanceId": iid,
                    "damageRecipient": {
                        "type": "DamageRecType_Player",
                        "playerSystemSeatId": opp_seat,
                    },
                })
        return {"action": "submit_attackers", "attackers": attacker_entries}

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
                assignments.append({
                    "blockerInstanceId": blocker_id,
                    "attackerInstanceIds": [attacker_id],
                })
        return {"action": "submit_blockers", "blockers": assignments}

    # 5. Select Target
    if atype == ActionType.SELECT_TARGET:
        targets = payload.get("qualifiedTargets") or []
        battlefield = game_state.get("battlefield", [])
        for name in action.target_names:
            iid = find_instance_id_by_name(name, battlefield)
            if iid is not None:
                for t in targets:
                    if int(t.get("targetInstanceId") or 0) == iid:
                        return {"action": "submit_targets", "target_instance_id": iid}
        if targets:
            first_t = int(targets[0].get("targetInstanceId") or 0)
            return {"action": "submit_targets", "target_instance_id": first_t}

    # 6. Play Land, Cast Spell, Activate Ability
    raw_actions = payload.get("actions") or payload.get("options") or payload.get("targets") or []
    if not raw_actions:
        return {"action": "submit_pass"}

    game_objects = {}
    for zone_key in ("battlefield", "hand", "graveyard"):
        for card in game_state.get(zone_key, []):
            iid = int(card.get("instance_id") or 0)
            if iid:
                game_objects[iid] = card

    from arenamcp.card_db import get_card_database
    card_db = get_card_database()

    def scryfall_lookup(grp_id: int) -> Optional[str]:
        info = card_db.get_card_by_arena_id(grp_id)
        return getattr(info, "name", None) if info else None

    ref = match_action_to_gre(action, raw_actions, game_objects, scryfall_lookup)
    if ref:
        for idx, ra in enumerate(raw_actions):
            if (
                ra.get("actionType") == ref.action_type
                and ra.get("grpId") == ref.grp_id
                and ra.get("instanceId") == ref.instance_id
                and ra.get("abilityGrpId") == ref.ability_grp_id
            ):
                return {
                    "action": "submit_action",
                    "action_index": idx,
                    "auto_pass": False,
                }

    logger.warning(f"No action matched {action} (ref={ref}). Defaulting to index 0.")
    if raw_actions:
        first_act = raw_actions[0]
        if first_act.get("actionType") == "ActionType_Pass":
            return {"action": "submit_pass"}
        return {
            "action": "submit_action",
            "action_index": 0,
            "auto_pass": False,
        }
    return {"action": "submit_pass"}


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

        # Check for game end/winner state
        players = game_state.get("players") or []
        for p in players:
            if p.get("status") == "PlayerStatus_Lost":
                # Opposing seat won
                losing_seat = p.get("seat_id")
                self.current_match_winner = "opp" if losing_seat == 1 else "local"

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
        user_message = planner._build_action_prompt(
            game_state, trigger, legal_actions, context
        )

        # Plan action using active planner
        logger.info(f"Self-play decision: seat={seat} ({backend_label}) trigger={trigger} legal={len(legal_actions)}")
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

        # Map active choice to BepInEx action command
        if plan.actions:
            chosen_action = plan.actions[0]
            resp_cmd = map_game_action_to_pipe_command(chosen_action, payload, context, game_state)
        else:
            chosen_action = None
            resp_cmd = {"action": "submit_pass"}

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
            "alt_planned_action": str(alt_action) if alt_action else "pass",
            "submit_command": resp_cmd,
            "latency_ms": round(elapsed_ms, 1),
        }

        with self._log_lock:
            self.decisions_log.append(decision_rec)

        return resp_cmd

    def flush_match_logs(self):
        """Append decisions log to trajectories file and label with winner."""
        with self._log_lock:
            if not self.decisions_log:
                return
            logger.info(f"Flushing {len(self.decisions_log)} decisions for match {self.current_match_id} (winner={self.current_match_winner})")
            with open(self.trajectories_path, "a", encoding="utf-8") as f:
                for dec in self.decisions_log:
                    dec["winner"] = self.current_match_winner
                    f.write(json.dumps(dec, ensure_ascii=False) + "\n")
            self.decisions_log.clear()
            self.current_match_winner = None


def main():
    p = argparse.ArgumentParser(description="Self-play orchestrator for model tuning.")
    p.add_argument("--local-backend", required=True, help="Spec for Player 1 (local)")
    p.add_argument("--opponent-backend", required=True, help="Spec for Player 2 (opponent)")
    p.add_argument("--matches", type=int, default=1, help="Number of matches to play")
    p.add_argument("--sets", default="EOE", help="Sets to generate random decks from")
    p.add_argument("--out-trajectories", type=Path, default=REPO / "tools/eval/data/self_play_trajectories.jsonl")
    p.add_argument("--license-key", default=os.environ.get("MTGACOACH_LICENSE_KEY", ""))
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    orchestrator = SelfPlayOrchestrator(
        local_backend_spec=args.local_backend,
        opp_backend_spec=args.opponent_backend,
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
    for _ in range(30):
        if bridge.connect():
            connected = True
            break
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
