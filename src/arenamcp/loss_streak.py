"""Durable, local-only three-match autoplay review queue.

Only authoritative match results count (never individual games in a BO3).
No network or support submission is performed by this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path


class LossStreakStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        try:
            self.data = json.loads(path.read_text())
            if (
                not isinstance(self.data, dict)
                or self.data.get("version") != 1
                or not isinstance(self.data.get("matches"), dict)
                or not isinstance(self.data.get("order"), list)
                or not isinstance(self.data.get("jobs"), dict)
            ):
                raise ValueError("unsupported state")
        except (OSError, ValueError):
            self.data = {"version": 1, "matches": {}, "order": [], "jobs": {}}

    def _prune(self):
        # Preserve pending work and a recent correction/dedup window. Cut only
        # at a known streak boundary so triplet alignment cannot shift.
        order = self.data["order"]
        jobs = self.data["jobs"]
        pending = {mid for job in jobs.values() if job["status"] == "pending" for mid in job["matches"]}
        completed_ends = {job["matches"][-1] for job in jobs.values() if job["status"] == "complete"}
        cut = 0
        for index, mid in enumerate(order[:-60]):
            if mid in pending:
                break
            record = self.data["matches"][mid]
            if record["result"] != "loss" or not record["eligible"] or mid in completed_ends:
                cut = index + 1
        for mid in order[:cut]:
            self.data["matches"].pop(mid, None)
        if cut:
            self.data["order"] = order[cut:]
        finished = [key for key, job in jobs.items() if job["status"] != "pending"]
        for key in finished[:-20]:
            del jobs[key]

    def save(self):
        self._prune()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.data, indent=2))
        os.replace(temp, self.path)

    def new_session(self):
        with self.lock:
            self.data["session"] = self.data.get("session", 0) + 1
            self.save()

    def observe(self, match_id, deck, eligible):
        if not match_id:
            return
        with self.lock:
            matches = self.data["matches"]
            if match_id not in matches:
                matches[match_id] = {
                    "deck": deck,
                    "eligible": bool(eligible),
                    "result": None,
                    "evidence": {},
                    "session": self.data.get("session", 0),
                }
                self.data["order"].append(match_id)
            else:
                record = matches[match_id]
                if record["result"] is not None:
                    return
                record["eligible"] &= bool(eligible)
                if deck and record["deck"] and deck != record["deck"]:
                    record["eligible"] = False
                record["deck"] = record["deck"] or deck
            self.save()

    def evidence(self, match_id, evidence):
        with self.lock:
            if match_id in self.data["matches"]:
                self.data["matches"][match_id]["evidence"][evidence["game_id"]] = evidence
                self.save()

    def finish(self, match_id, result):
        with self.lock:
            record = self.data["matches"].get(match_id)
            if not record or record["result"] == result:
                return
            record["result"] = result
            # Recompute after corrections; unknown/manual/deck changes break a run.
            run = []
            for mid in self.data["order"]:
                item = self.data["matches"][mid]
                if item["result"] is None:
                    run = []
                    continue
                if not item["eligible"] or not item["deck"] or item["result"] != "loss":
                    run = []
                    continue
                if run:
                    previous = self.data["matches"][run[-1]]
                    if previous["deck"] != item["deck"] or previous.get("session", 0) != item.get(
                        "session", 0
                    ):
                        run = []
                run.append(mid)
                if len(run) == 3:
                    key = hashlib.sha256(json.dumps(run).encode()).hexdigest()[:20]
                    self.data["jobs"].setdefault(key, {"matches": list(run), "status": "pending"})
                    run = []
            # A correction invalidates reports not yet delivered.
            for job in self.data["jobs"].values():
                if any(
                    self.data["matches"][m]["result"] != "loss"
                    for m in job["matches"]
                    if m in self.data["matches"]
                ):
                    job["status"] = "superseded"
            self.save()

    def pending(self):
        with self.lock:
            return [
                (key, json.loads(json.dumps(job)))
                for key, job in self.data["jobs"].items()
                if job["status"] == "pending"
            ]

    def complete(self, key, report):
        with self.lock:
            job = self.data["jobs"][key]
            if job["status"] != "pending":
                return False
            job.update(status="complete", report=report)
            self.save()
            return True


def resolve_match_result(results, seat, team):
    """Resolve only explicit match scope, never a game's result or guessed identity."""
    for row in results:
        if row.get("scope") != "MatchScope_Match":
            continue
        result = str(row.get("result", "")).lower()
        if "draw" in result:
            return "draw"
        winner = row.get("winningTeamId")
        if isinstance(winner, int) and winner > 0 and team is not None:
            return "win" if winner == team else "loss"
        if row.get("seatId") is not None and seat is not None:
            if "win" in result:
                return "win" if row["seatId"] == seat else "loss"
            if "loss" in result:
                return "loss" if row["seatId"] == seat else "win"
    return None
