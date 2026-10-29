"""Journaled clone/update/acknowledgement workflow; never edits a pre-existing deck.

Bridge writes are asynchronous and may time out after submission. Each mutation is
sent at most once per journal; subsequent ticks only reconcile observations. The
journal deliberately survives cancellation/restart, but grants no autoplay consent.
"""

from __future__ import annotations

import json
import os
import uuid
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from arenamcp.deck_library import records_from_bridge


def _counts(main):
    result = Counter()
    for gid, count in main:
        result[int(gid)] += int(count)
    return result


class QuestDeckOperation:
    def __init__(self, path: Path):
        self.path = path

    def _load(self):
        if not self.path.exists():
            return None
        data = json.loads(self.path.read_text())
        if data.get("version") != 1:
            raise ValueError("Unknown quest deck journal version; review it before continuing")
        # The first native implementation tried this nonexistent setter on the
        # detached clone BEFORE calling CreateDeck. Recover only that exact old
        # failure, not arbitrary errors or timeouts that may follow submission.
        if (
            data.get("stage") == "creating"
            and "attempt_id" not in data
            and data.get("target") is None
            and data.get("submission_error")
            == "no method set_Name/1 on Wizards.Mtga.Decks.Client_DeckSummary"
        ):
            data["stage"] = "create_rejected"
        return data

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)

    def step(self, *, bridge, spec, objective, log_path, allowed, attempt_id=None):
        """One reconciliation step. ``allowed`` rechecks live consent after reads.

        Return (observed DeckRecord or None, explanation). Only a matching server
        acknowledgement AND a fresh bridge readback can return a verified record.
        """
        from arenamcp.quest_deck_persistence import (
            DeckSaveOffset,
            capture_deck_save_offset,
            read_deck_save,
        )

        if not allowed():
            return None, "Grind paused or cancelled before deck preparation."
        response = bridge.draft_command("get_decks", timeout=12.0)
        if not response.get("ok") or not isinstance(response.get("decks"), list):
            return None, "Waiting for a fresh bridge deck library; originals are unchanged."
        decks = records_from_bridge(response)
        if not allowed():
            return None, "Grind paused or cancelled during deck readback."
        data = self._load()
        if data and data["stage"] == "create_rejected":
            if attempt_id is None or attempt_id == data.get("attempt_id"):
                return None, "Clone was not submitted: " + data[
                    "submission_error"
                ] + ". Fix the bridge and restart Grind to retry."
            # A positively identified pre-submission failure can be retried
            # on a new explicit session. Uncertain writes never enter here.
            if any(d.name == data["name"] for d in decks.values()):
                return None, "Rejected clone name now exists; review the operation before retrying."
            self.path.replace(self.path.with_name(self.path.stem + "-rejected-" + uuid.uuid4().hex + ".json"))
            data = None
        desired = [[gid, count] for gid, count in spec.main]
        if data and (
            data["objective"] != objective or data["main"] != desired or data["format"] != spec.format
        ):
            if data["stage"] == "verified" and data["objective"] != objective:
                # A new quest may need another color. Keep the verified deck and
                # its receipt, then create a separate clone for the new objective.
                self.path.replace(self.path.with_name(f"quest-deck-{data['target']}.json"))
                data = None
            else:
                # Never abandon an uncertain write and clone again on retry.
                return (
                    None,
                    "A prior quest deck operation needs reconciliation; no duplicate will be created.",
                )
        if data is None:
            sources = sorted(
                (d for d in decks.values() if d.complete and d.format == spec.format and not d.snapshot),
                key=lambda d: d.deck_id,
            )
            if not sources:
                return None, f"No readable {spec.format} source deck to clone; other formats are preserved."
            if capture_deck_save_offset(log_path) is None:
                return None, "A readable detailed Arena log is required to verify deck saves."
            data = {
                "version": 1,
                "objective": objective,
                "main": desired,
                "format": spec.format,
                "event_id": spec.event_id,
                "name": "Quest Grind " + uuid.uuid4().hex[:16],
                "before": sorted(decks),
                "source": sources[0].deck_id,
                "target": None,
                "stage": "creating",
                "attempt_id": attempt_id,
            }
            # Persist intent BEFORE sending: even an exception/timeout cannot
            # cause a second clone on retry or on a later explicitly started run.
            self._save(data)
            if not allowed():
                return None, "Cancelled before clone submission; journal retained for review."
            response = bridge.draft_command(
                "create_deck", timeout=20.0, source_deck_id=data["source"], name=data["name"]
            )
            if not response.get("ok"):
                data["submission_error"] = str(response.get("error") or "unknown create result")
                if response.get("not_submitted") is True:
                    data["stage"] = "create_rejected"
                self._save(data)
                if data["stage"] == "create_rejected":
                    return None, "Clone was not submitted: " + data[
                        "submission_error"
                    ] + ". Fix the bridge and restart Grind to retry."
                return None, "Clone result uncertain: " + data[
                    "submission_error"
                ] + "; reconciling without retrying creation."
            return None, "Quest deck clone submitted; waiting for its new identity."

        target = decks.get(data.get("target"))
        if data["stage"] == "creating":
            matches = [
                d
                for d in decks.values()
                if d.deck_id not in data["before"] and d.name == data["name"] and d.format == data["format"]
            ]
            if len(matches) != 1:
                return None, "Waiting for the uniquely identified quest clone; creation will not be repeated."
            target = matches[0]
            data["target"] = target.deck_id
            data["stage"] = "created"
            self._save(data)
        if (
            target is None
            or target.deck_id in data["before"]
            or target.name != data["name"]
            or target.format != spec.format
        ):
            return None, "Quest clone identity changed or is unavailable; no deck will be edited."
        if data["stage"] == "created":
            checkpoint = capture_deck_save_offset(log_path)
            if checkpoint is None:
                return None, "Waiting for a readable Arena log before populating the quest clone."
            data["checkpoint"] = asdict(checkpoint)
            data["stage"] = "updating"
            self._save(data)
            if not allowed():
                return None, "Cancelled before deck update; journal retained for review."
            response = bridge.draft_command(
                "update_deck",
                timeout=20.0,
                deck_id=target.deck_id,
                piles={"main": spec.main, "sideboard": [], "command_zone": [], "companions": []},
            )
            if not response.get("ok"):
                data["submission_error"] = str(response.get("error") or "unknown update result")
                self._save(data)
                return None, "Save result uncertain: " + data["submission_error"] + "; waiting for readback."
            return None, "Quest deck update submitted; waiting for Arena to acknowledge the save."

        same = (
            _counts(target.main) == _counts(spec.main)
            and not target.sideboard
            and not target.command_zone
            and not target.companions
        )
        saved = read_deck_save(
            log_path,
            after_offset=DeckSaveOffset(**data["checkpoint"]),
            deck_id=target.deck_id,
            name=target.name,
            main=spec.main,
            deck_format=spec.format,
        )
        if not same or not saved:
            return (
                None,
                "Waiting for matching saved deck and server acknowledgement; no duplicate or queue submission.",
            )
        if not allowed():
            return None, "Grind paused or cancelled during save verification."
        data["stage"] = "verified"
        self._save(data)
        return target, f"Verified saved {target.name} ({spec.format}); no cards were crafted."
