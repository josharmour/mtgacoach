"""Small public event journal, and replayable normalization outside state locks."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace

from .contracts import Evidence
from .observations import _public_card, observation_from_snapshot


def journal_event(game, event):
    """Called under GameState's existing lock: bounded scalar work only, no I/O."""
    if not game.match_id or not game.game_number or event.get("type") != "zone_transfer":
        return
    kind = {"CastSpell": "action_observed", "Resolve": "spell_resolved", "PlayLand": "land_drop"}.get(
        event.get("category")
    )
    if kind is None:
        return
    obj = game.game_objects.get(event.get("instance_id")) or game.game_objects.get(
        event.get("orig_instance_id")
    )
    if obj is None:
        return
    visible = _public_card(obj.to_dict())
    payload = {"instance_id": obj.instance_id}
    destination = game.zones.get(event.get("to_zone"))
    zone_type = (
        str(getattr(getattr(destination, "zone_type", None), "value", event.get("to_zone", "")))
        .replace("ZoneType_", "")
        .lower()
    )
    if zone_type in {"battlefield", "graveyard", "exile", "hand", "library"}:
        payload["to_zone"] = zone_type
    if not visible.get("face_down") and visible.get("name") != "Unknown card":
        name = event.get("card")
        if isinstance(name, str) and not name.startswith(("Card#", "Unknown")):
            payload["name"] = name[:200]
        if type(obj.grp_id) is int:
            payload["grp_id"] = obj.grp_id
    game._commentary_event_number += 1
    game._commentary_journal.append(
        {
            "id": game._commentary_event_number,
            "seq": game._raw_gre_sequence,
            "kind": kind,
            "payload": payload,
            "seat": obj.controller_seat_id or obj.owner_seat_id,
            "match_id": game.match_id,
            "game_number": game.game_number,
            "snapshot_id": str(getattr(game, "log_game_state_id", 0)),
            "received_at": time.monotonic(),
            "turn_number": getattr(getattr(game, "turn_info", None), "turn_number", None),
            "phase": getattr(getattr(game, "turn_info", None), "phase", None),
            "active_player": getattr(getattr(game, "turn_info", None), "active_player", None),
        }
    )
    del game._commentary_journal[:-128]


def state_fingerprint(observation):
    body = {
        "epoch": observation.to_wire()["epoch"],
        "public": observation.public_state,
        "private": observation.private_state,
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]


class ObservationAdapter:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.previous = None
        self.sequence = 0
        self.seen_event = 0

    def project(self, snapshot, private=False):
        """No disk, models or mutable GameState access; only the published copy."""
        self.sequence += 1
        seq = max(self.sequence, int(snapshot.get("commentary_source_sequence") or 0))
        observation = observation_from_snapshot(
            snapshot, source_sequence=seq, snapshot_id=str(seq), arrived_at=self.clock()
        )
        public = observation.public_state
        public["local_seat_id"] = snapshot.get("local_seat_id")
        if isinstance(snapshot.get("last_game_result"), str):
            public["last_game_result"] = snapshot["last_game_result"][:40]
        if private:
            # Explicit consent only covers the local player's hand and deck.
            hand = (snapshot.get("zones") or {}).get("my_hand", [])
            local = snapshot.get("local_seat_id")
            cards = [_public_card({**c, "visibility": "public"}) for c in hand[:100] if isinstance(c, dict)]
            deck = [v for v in snapshot.get("deck_cards", [])[:250] if type(v) is int]
            cards = [card for card in cards if local and card.get("owner_seat_id") in (None, local)]
            private_state = {"local_seat": local, "hand": cards, "deck": deck}
            overview = snapshot.get("commentary_deck_overview")
            if (
                isinstance(overview, dict)
                and isinstance(overview.get("identity"), str)
                and isinstance(overview.get("summary"), str)
                and 0 < len(overview["summary"]) <= 500
            ):
                private_state["deck_overview"] = {
                    "identity": overview["identity"],
                    "summary": overview["summary"],
                }
            observation = replace(observation, private_state=private_state)
        return replace(observation, snapshot_id=state_fingerprint(observation))

    def observe(self, snapshot, private=False):
        current = self.project(snapshot, private)
        previous = self.previous
        boundary = previous is None or previous.epoch != current.epoch
        if boundary:
            self.seen_event = 0
        events = []

        def add(kind, data=None, seat=None, suffix="state", visibility="public"):
            events.append(
                Evidence(
                    f"{current.epoch.match_id}:{current.epoch.game_number}:{current.source_sequence}:{suffix}:{kind}",
                    kind,
                    current.source_sequence,
                    current.snapshot_id,
                    payload=data or {},
                    seat=seat,
                    visibility=visibility,
                    received_at=current.arrived_at,
                    turn_number=current.public_state["turn_info"].get("turn_number"),
                    phase=current.public_state["turn_info"].get("phase"),
                    active_player=current.public_state["turn_info"].get("active_player"),
                )
            )

        if boundary:
            add("game_start")
        if private:
            prior = previous.private_state if not boundary else {}
            own = current.private_state
            if own.get("hand") != prior.get("hand") and own.get("hand"):
                add("hand_changed", {"count": len(own["hand"])}, own.get("local_seat"), visibility="private")
            overview = own.get("deck_overview")
            if overview and overview != prior.get("deck_overview"):
                add("deck_overview", overview, own.get("local_seat"), visibility="private")
        old = previous.public_state if not boundary else {}
        turn = current.public_state["turn_info"]
        if old and turn.get("turn_number") != old.get("turn_info", {}).get("turn_number"):
            add(
                "turn_start",
                {"turn_number": turn.get("turn_number"), "active_player": turn.get("active_player")},
                turn.get("active_player"),
            )
        old_players = {p.get("seat_id"): p for p in old.get("players", [])}
        for player in current.public_state["players"]:
            seat, life = player.get("seat_id"), player.get("life_total")
            before = old_players.get(seat, {}).get("life_total")
            if type(life) is int and type(before) is int and life != before:
                add("life_change", {"from": before, "to": life, "delta": life - before}, seat, str(seat))
        if current.public_state.get("last_game_result") and not old.get("last_game_result"):
            add("game_end", {"result": current.public_state["last_game_result"]})
        cards = current.public_state["zones"]["battlefield"]
        attackers = [c for c in cards if c.get("is_attacking")]
        old_attackers = {
            c.get("instance_id") for c in old.get("zones", {}).get("battlefield", []) if c.get("is_attacking")
        }
        if attackers and {c.get("instance_id") for c in attackers} != old_attackers:
            add(
                "combat",
                {"attackers": attackers, "count": len(attackers)},
                attackers[0].get("controller_seat_id"),
            )
        for record in snapshot.get("commentary_events", ())[-128:]:
            if record.get("id", 0) <= self.seen_event:
                continue
            self.seen_event = record["id"]
            if (record.get("match_id"), record.get("game_number")) != (
                current.epoch.match_id,
                current.epoch.game_number,
            ):
                continue
            received = record.get("received_at", 0)
            if not isinstance(received, (float, int)) or self.clock() - received > 30:
                continue
            events.append(
                Evidence(
                    f"gre:{current.epoch.match_id}:{current.epoch.game_number}:{record['id']}",
                    record["kind"],
                    int(record["seq"]),
                    str(record["snapshot_id"]),
                    payload=dict(record["payload"]),
                    seat=record.get("seat"),
                    received_at=received,
                    turn_number=record.get("turn_number"),
                    phase=record.get("phase"),
                    active_player=record.get("active_player"),
                )
            )
        self.previous = current
        from .timeline import select_events

        return replace(current, events=select_events(events, current, limit=32))
