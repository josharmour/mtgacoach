#!/usr/bin/env python3
"""G2 smoke test: play/draw, Keep and one land drop submitted through the probe.

Each submission is confirmed independently from Player.log, which records every
client->GRE response the game actually sends. Run it, then start a Bot Match and
leave the play/draw, mulligan and first land drop to the probe. It submits
nothing else and exits after the land is confirmed.
"""

from __future__ import annotations

import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe_client import send  # noqa: E402

PLAYER_LOG = os.path.expanduser("~/Library/Logs/Wizards Of The Coast/MTGA/Player.log")
OUTGOING_TYPE = re.compile(r'"type": "ClientMessageType_(\w+)"')
OUTGOING_FIELD = re.compile(r'"(actionType|instanceId|grpId|decision)": "?(\w+)"?')
CHOOSE_START_SEAT = re.compile(
    r'"type": "GREMessageType_ChooseStartingPlayerReq", "systemSeatIds": \[ (\d+) \]'
)


def say(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def log_text_since(offset: int) -> str:
    with open(PLAYER_LOG, "rb") as log:
        log.seek(offset)
        return log.read().decode("utf-8", "replace")


def outgoing_since(offset: int) -> list[tuple[str, dict]]:
    text = log_text_since(offset)
    responses = []
    for match in OUTGOING_TYPE.finditer(text):
        if match.group(1) == "UIMessage":
            continue
        body = text[match.end() : match.end() + 2000]
        end = body.find("UnityEngine.DebugLogHandler")
        if end != -1:
            body = body[:end]
        fields: dict = {}
        for key, value in OUTGOING_FIELD.findall(body):
            fields.setdefault(key, value)
        responses.append((match.group(1), fields))
    return responses


def confirm(offset: int, message_type: str, predicate, timeout: float = 8.0) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for kind, fields in outgoing_since(offset):
            if kind == message_type and predicate(fields):
                return fields
        time.sleep(0.25)
    return None


def describe(pending: dict) -> str:
    if not pending.get("has_pending"):
        return f"no request ({pending.get('reason')})"
    text = pending["request"].rsplit(".", 1)[-1]
    actions = pending.get("actions")
    if actions:
        text += " [" + ", ".join(f"{a['type']}#{a['instance_id']}" for a in actions) + "]"
    return text


def main() -> int:
    offset = os.path.getsize(PLAYER_LOG)
    did = {"start": False, "keep": False}
    last_description = None
    started = time.time()
    while time.time() - started < 1200:
        try:
            pending = send("pending")
        except OSError as exc:
            say(f"PROBE UNREACHABLE: {exc}")
            return 1
        if not pending.get("ok"):
            say(f"PROBE ERROR: {pending.get('error')}")
            time.sleep(1)
            continue
        description = describe(pending)
        if description != last_description:
            say(f"pending: {description}")
            last_description = description
        request = pending.get("request", "") if pending.get("has_pending") else ""

        if request.endswith("ChooseStartingPlayerRequest") and not did["start"]:
            seats = CHOOSE_START_SEAT.findall(log_text_since(0))
            if not seats:
                say("play/draw pending but local seat not found in Player.log; leaving it to you")
                did["start"] = True
                continue
            seat = seats[-1]
            mark = os.path.getsize(PLAYER_LOG)
            result = send(f"choose_start {seat}")
            say(f"CHOOSE PLAY (seat {seat}) via probe -> {result}")
            did["start"] = True
            seen = confirm(mark, "ChooseStartingPlayerResp", lambda f: True)
            say(f"{'CONFIRMED' if seen is not None else 'NOT SEEN'} in Player.log: ChooseStartingPlayerResp")

        elif request.endswith("MulliganRequest") and not did["keep"]:
            mark = os.path.getsize(PLAYER_LOG)
            result = send("keep")
            say(f"KEEP via probe -> {result}")
            did["keep"] = True
            seen = confirm(mark, "MulliganResp", lambda f: f.get("decision") == "MulliganOption_AcceptHand")
            say(f"{'CONFIRMED' if seen is not None else 'NOT SEEN'} in Player.log: MulliganResp AcceptHand")
            if not (result.get("ok") and seen is not None):
                say("G2 SMOKE FAILED at Keep")
                return 1

        elif request.endswith("ActionsAvailableRequest"):
            lands = [a for a in pending.get("actions", []) if a["type"] in ("Play", "ActionType_Play")]
            if lands:
                land = lands[0]
                mark = os.path.getsize(PLAYER_LOG)
                result = send(f"submit_action {land['index']} {land['instance_id']}")
                say(f"PLAY LAND instance {land['instance_id']} (grp {land['grp_id']}) via probe -> {result}")
                seen = confirm(
                    mark,
                    "PerformActionResp",
                    lambda f: f.get("actionType") == "ActionType_Play"
                    and f.get("instanceId") == str(land["instance_id"]),
                )
                say(f"{'CONFIRMED' if seen is not None else 'NOT SEEN'} in Player.log: PerformActionResp {seen}")
                if result.get("ok") and seen is not None:
                    say("G2 SMOKE PASSED: submitted through GRE request objects and confirmed in Player.log")
                    return 0
                say("G2 SMOKE FAILED at land drop")
                return 1
        time.sleep(0.5)
    say("TIMED OUT after 20 minutes")
    return 1


if __name__ == "__main__":
    sys.exit(main())
