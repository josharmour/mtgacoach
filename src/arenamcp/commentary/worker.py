"""Versioned headless JSON-lines worker; no app lifecycle, audio or file writes."""

from __future__ import annotations

import json
import logging
import sys
import time

from .config import CommentaryConfig
from .contracts import PROTOCOL_VERSION, Observation, SpeechCategory, Utterance
from .headless import HeadlessBooth, deterministic

MAX_LINE = 1_048_576


def emit(payload):
    sys.stdout.write(json.dumps({"protocol_version": PROTOCOL_VERSION, **payload}) + "\n")
    sys.stdout.flush()


def receive():
    line = sys.stdin.readline(MAX_LINE + 1)
    if not line:
        raise EOFError()
    if len(line) > MAX_LINE:
        raise ValueError("oversized command")
    payload = json.loads(line)
    if payload.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("protocol mismatch")
    return payload


def main():
    logging.basicConfig(level=logging.CRITICAL, stream=sys.stderr)
    facade = None
    configuration = None
    current_job = None

    def rpc(system, context, audit):
        emit(
            {
                "type": "llm_request",
                "job_id": current_job,
                "system": system,
                "context": context,
                "audit": audit,
            }
        )
        while True:
            response = receive()
            if response.get("type") == "delivery" and facade:
                facade.delivered(response["utterance_id"], response["state"])
                continue
            if response.get("type") != "llm_result" or response.get("job_id") != current_job:
                raise ValueError("unexpected broker response")
            if "error" in response:
                raise RuntimeError("broker_declined")
            return response["result"]

    emit({"type": "ready", "capabilities": ["deterministic", "audited_booth", "delivery"]})
    while True:
        try:
            request = receive()
        except EOFError:
            return 0
        if request.get("type") == "shutdown":
            return 0
        if request.get("type") == "delivery":
            if facade:
                facade.delivered(request["utterance_id"], request["state"])
            continue
        if request.get("type") != "observe":
            raise ValueError("unknown command")
        current_job = request["job_id"]
        try:
            config = CommentaryConfig.from_wire(request["config"])
            observation = Observation.from_wire(request["observation"])
            if not config.enabled:
                emit(
                    {
                        "type": "result",
                        "job_id": current_job,
                        "utterances": [],
                        "status": {"state": "disabled"},
                    }
                )
                continue
            if config.generative:
                if facade is None or configuration != config:
                    facade = HeadlessBooth(config, rpc, time.monotonic)
                    configuration = config
                native, status = facade.generate(observation)
                evidence_time = min(
                    (e.received_at or observation.arrived_at for e in observation.events),
                    default=observation.arrived_at,
                )
                lines = [
                    Utterance(
                        item.uid,
                        item.text,
                        SpeechCategory.COMMENTARY,
                        observation.epoch,
                        min(
                            item.expires_ts or observation.arrived_at + 15,
                            evidence_time + (8 if item.role == "play_by_play" else 15),
                        ),
                        voice=item.voice or "atlas",
                        anchor_id=item.anchor_uid,
                        evidence_ids=tuple(e.event_id for e in observation.events),
                        snapshot_id=observation.snapshot_id,
                        role=item.role,
                        speed=item.rate,
                    )
                    for item in native
                ]
            else:
                lines = deterministic(observation, config)
                status = {"state": "deterministic"}
            emit(
                {
                    "type": "result",
                    "job_id": current_job,
                    "utterances": [u.to_wire() for u in lines],
                    "status": status,
                }
            )
        except Exception as error:
            # No exception strings: optional dependencies may embed paths or credentials.
            emit(
                {
                    "type": "result",
                    "job_id": current_job,
                    "utterances": [],
                    "status": {"state": "unavailable", "reason": type(error).__name__},
                }
            )


if __name__ == "__main__":
    raise SystemExit(main())
