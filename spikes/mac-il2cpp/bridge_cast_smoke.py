#!/usr/bin/env python3
"""Cast a creature on native Mac through the real gre_bridge.GREBridge client.

Stands in for the coach on 127.0.0.1:44222 (the coach must not be running).
Handles play/draw and Keep, plays lands, and passes only when nothing is
castable (at most MAX_PASSES times). When a creature is castable it puts Arena
in the background, casts it with identity checks, pays with auto-pay and lets it
resolve. Every submission is checked against Player.log. Requests the Mac bridge
does not support yet are left to the player.

Run: PYTHONPATH=src <venv>/bin/python spikes/mac-il2cpp/bridge_cast_smoke.py
"""

from __future__ import annotations

import glob
import os
import re
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from g2_smoke import CHOOSE_START_SEAT, PLAYER_LOG, confirm, log_text_since, say  # noqa: E402
from probe_client import send as probe_send  # noqa: E402

from arenamcp.gre_bridge import GREBridge  # noqa: E402

MAX_PASSES = 40
OBJECT_ID_CHANGED = (
    r'"affectedIds": \[ {orig} \], "type": \[ "AnnotationType_ObjectIdChanged" \], "details": \[ '
    r'\{{ "key": "orig_id", "type": "KeyValuePairValueType_int32", "valueInt32": \[ {orig} \] \}}, '
    r'\{{ "key": "new_id", "type": "KeyValuePairValueType_int32", "valueInt32": \[ (\d+) \]'
)
ZONE_TRANSFER = (
    r'"affectedIds": \[ {instance} \], "type": \[ "AnnotationType_ZoneTransfer" \]'
    r'.{{0,400}}?"key": "category", "type": "KeyValuePairValueType_string", "valueString": \[ "(\w+)" \]'
)


def zone_transfer(offset: int, instance_id: int, timeout: float = 8.0) -> tuple[int, str] | None:
    """Follow ObjectIdChanged from instance_id and return (new_id, category)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = log_text_since(offset)
        changed = re.search(OBJECT_ID_CHANGED.format(orig=instance_id), text)
        current = int(changed.group(1)) if changed else instance_id
        moved = re.search(ZONE_TRANSFER.format(instance=current), text, re.S)
        if moved:
            return current, moved.group(1)
        time.sleep(0.25)
    return None


def frontmost_app() -> str:
    front = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True).stdout.strip()
    info = subprocess.run(["lsappinfo", "info", "-only", "name", front], capture_output=True, text=True).stdout
    match = re.search(r'"LSDisplayName"="([^"]+)"', info)
    return match.group(1) if match else info.strip()


def main_thread_ticks() -> int:
    return int(probe_send("ping").get("ticks", -1))


def submit_by_identity(bridge: GREBridge, pending: dict, action: dict) -> dict:
    return bridge._send_safe(
        {
            "action": "submit_action",
            "action_index": pending["actions"].index(action),
            "auto_pass": False,
            "expected_instance_id": action["instanceId"],
            "expected_action_type": action["actionType"],
            "expected_game_state_id": pending.get("game_state_id", -1),
        }
    )


def main() -> int:
    bridge = GREBridge()
    say("waiting for the Mac bridge to connect on 127.0.0.1:44222")
    started = time.time()
    while not bridge.connect():
        if time.time() - started > 180:
            say("FAILED: the Mac bridge never connected")
            return 1
        time.sleep(0.25)
    say(f"connected: {bridge._send_safe({'action': 'ping'})}")
    # MTGA's own card database: name via TitleId, and Types "2" = Creature.
    raw = sorted(glob.glob(os.path.expanduser(
        "~/Library/Application Support/Steam/steamapps/common/MTGA/MTGA_Data/Downloads/Raw/Raw_CardDatabase_*.mtga"
    )))[-1]
    cards = sqlite3.connect(f"file:{raw}?mode=ro", uri=True)

    def describe(grp_id: int) -> tuple[str, str]:
        row = cards.execute(
            "SELECT l.Loc, c.Types FROM Cards c JOIN Localizations_enUS l ON l.LocId = c.TitleId "
            "WHERE c.GrpId = ? LIMIT 1",
            (grp_id,),
        ).fetchone()
        if not row:
            return f"grp {grp_id}", ""
        return row[0], "Creature" if "2" in str(row[1]).split(",") else str(row[1])

    passes = 0
    guard_tested = False
    handled: set = set()
    last_summary = None
    deadline = None  # the 25-minute budget starts with the match, not while waiting for one
    while deadline is None or time.time() < deadline:
        pending = bridge.get_pending_actions() or {}
        if not pending.get("has_pending"):
            time.sleep(0.4)
            continue
        if deadline is None:
            deadline = time.time() + 1500
            say("match detected")
        request = pending.get("request_class", "")
        key = (request, pending.get("game_state_id"), pending.get("msg_id"))
        actions = pending.get("actions") or []
        summary = request + " " + ", ".join(
            f"{a['actionType']} {describe(a['grpId'])[0]}" for a in actions if a["actionType"] != "Pass"
        )
        if summary != last_summary:
            say(f"pending: {summary}")
            last_summary = summary

        if request == "ChooseStartingPlayerRequest" and key not in handled:
            handled.add(key)
            seats = CHOOSE_START_SEAT.findall(log_text_since(0))
            if seats:
                mark = os.path.getsize(PLAYER_LOG)
                say(f"play/draw -> {bridge.submit_choose_starting_player(int(seats[-1]))}")
                seen = confirm(mark, "ChooseStartingPlayerResp", lambda f: True)
                say(f"  {'CONFIRMED' if seen is not None else 'NOT SEEN'}: ChooseStartingPlayerResp")

        elif request == "MulliganRequest" and key not in handled:
            handled.add(key)
            mark = os.path.getsize(PLAYER_LOG)
            say(f"keep -> {bridge.submit_mulligan(True)}")
            seen = confirm(mark, "MulliganResp", lambda f: f.get("decision") == "MulliganOption_AcceptHand")
            say(f"  {'CONFIRMED' if seen is not None else 'NOT SEEN'}: MulliganResp AcceptHand")

        elif request == "ActionsAvailableRequest":
            lands = [a for a in actions if a["actionType"] == "Play"]
            creatures = [
                a for a in actions
                if a["actionType"] == "Cast" and a.get("hasAutoTap") and "Creature" in describe(a["grpId"])[1]
            ]
            if lands:
                land = lands[0]
                if not guard_tested:
                    guard_tested = True
                    wrong = dict(land, instanceId=land["instanceId"] + 100000)
                    rejected = bridge._send_safe(
                        {"action": "submit_action", "action_index": actions.index(land),
                         "expected_instance_id": wrong["instanceId"]}
                    )
                    say(f"identity guard (wrong instance on purpose) -> {rejected}")
                mark = os.path.getsize(PLAYER_LOG)
                say(f"land {describe(land['grpId'])[0]} #{land['instanceId']} -> {submit_by_identity(bridge, pending, land)}")
                moved = zone_transfer(mark, land["instanceId"])
                say(f"  server: {moved}")
            elif creatures:
                creature = creatures[0]
                name = describe(creature["grpId"])[0]
                subprocess.run(["open", "-a", "Finder"], check=False)
                time.sleep(1.5)
                before = main_thread_ticks()
                time.sleep(1.0)
                after = main_thread_ticks()
                say(f"Arena backgrounded: front app = {frontmost_app()!r}, main-thread ticks +{after - before} in 1 s")
                mark = os.path.getsize(PLAYER_LOG)
                say(f"cast {name} #{creature['instanceId']} -> {submit_by_identity(bridge, pending, creature)}")
                seen = confirm(
                    mark, "PerformActionResp",
                    lambda f: f.get("actionType") == "ActionType_Cast" and f.get("instanceId") == str(creature["instanceId"]),
                )
                say(f"  {'CONFIRMED' if seen is not None else 'NOT SEEN'}: PerformActionResp Cast")
                paid = False
                for _ in range(40):
                    follow = bridge.get_pending_actions() or {}
                    follow_class = follow.get("request_class", "")
                    if follow_class in ("PayCostsRequest", "AutoTapActionsRequest"):
                        say(f"payment: {follow_class}, {follow.get('auto_tap_solution_count')} auto-pay solution(s)")
                        say(f"auto-pay -> {bridge.submit_auto_tap(0)}")
                        paid = True
                        break
                    if follow_class == "ActionsAvailableRequest":
                        break  # already paid; we hold priority with the spell on the stack
                    if follow_class and follow_class not in ("",):
                        say(f"MANUAL: {follow_class} is not supported by the Mac bridge yet; finish it in Arena")
                        return 2
                    time.sleep(0.25)
                cast = zone_transfer(mark, creature["instanceId"])
                say(f"  server: {cast} (paid via auto-pay request: {paid})")
                mark = os.path.getsize(PLAYER_LOG)
                resolve_state = bridge.get_pending_actions() or {}
                if resolve_state.get("request_class") == "ActionsAvailableRequest" and resolve_state.get("can_pass"):
                    say(f"pass to resolve -> {bridge.submit_pass()}")
                resolved = zone_transfer(mark, cast[0], timeout=12.0) if cast else None
                say(f"  server: {resolved}")
                ok = seen is not None and cast is not None and cast[1] == "CastSpell"
                say(f"BRIDGE CAST SMOKE {'PASSED' if ok else 'FAILED'}; click back into Arena, the game is yours")
                return 0 if ok else 1
            elif pending.get("can_pass") and passes < MAX_PASSES:
                passes += 1
                bridge.submit_pass()
        elif key not in handled:
            handled.add(key)
            say(f"MANUAL: {request} is not supported by the Mac bridge yet; handle it in Arena")
        time.sleep(0.4)
    say("TIMED OUT")
    return 1


if __name__ == "__main__":
    sys.exit(main())
