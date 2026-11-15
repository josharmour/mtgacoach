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
    category = str(event.get("category") or "")
    source_zone = game.zones.get(event.get("from_zone"))
    origin = (
        str(getattr(getattr(source_zone, "zone_type", None), "value", "")).replace("ZoneType_", "").lower()
    )
    destination = game.zones.get(event.get("to_zone"))
    destination_type = getattr(getattr(destination, "zone_type", None), "value", None)
    if kind is None and origin and origin != "battlefield" and destination_type == "ZoneType_Battlefield":
        kind = "permanent_entered"
    if kind is None and (category in {"Mill", "Countered", "Counterspell"} or origin == "battlefield"):
        kind = "outcome"
    if kind is None:
        return
    obj = game.game_objects.get(event.get("instance_id")) or game.game_objects.get(
        event.get("orig_instance_id")
    )
    if obj is None:
        return
    payload = {"instance_id": obj.instance_id}
    zone_type = (
        str(getattr(getattr(destination, "zone_type", None), "value", event.get("to_zone", "")))
        .replace("ZoneType_", "")
        .lower()
    )
    visible = _public_card(obj.to_dict(), zone_type)
    if zone_type == "exile" and (
        obj.instance_id != event.get("instance_id") or getattr(obj, "zone_id", None) != event.get("to_zone")
    ):
        # An origin object may still carry its old public visibility after a zone transfer.
        visible = {}
    if zone_type in {"battlefield", "graveyard", "exile", "hand", "library"}:
        payload["to_zone"] = zone_type
    if visible and not visible.get("face_down") and visible.get("name") != "Unknown card":
        name = visible.get("name") if zone_type == "exile" else event.get("card")
        if isinstance(name, str) and not name.startswith(("Card#", "Unknown")):
            payload["name"] = name[:200]
        if type(obj.grp_id) is int:
            payload["grp_id"] = obj.grp_id
    if kind == "outcome":
        # Only known public destinations. A face-down exile never reveals a name.
        if zone_type not in {"graveyard", "exile", "hand", "library", "command"}:
            return
        if category in {"Countered", "Counterspell"} and origin != "stack":
            return
        if category == "Mill" and (origin != "library" or zone_type != "graveyard"):
            return
        payload.update(
            category=category, from_zone=origin, count=1, source_id=event.get("source_id", 0), cards=[]
        )
        if (payload.get("name") and zone_type == "graveyard") or (
            payload.get("grp_id") and zone_type == "exile"
        ):
            # Parser objects carry ids/types, not database-enriched names/rules.
            # The existing public annotation supplies the observed card name.
            card = {k: visible[k] for k in ("grp_id", "type_line", "mana_cost") if k in visible}
            if "name" in payload:
                card["name"] = payload["name"]
            if zone_type == "exile":
                card["instance_id"] = obj.instance_id
            card.setdefault("type_line", " ".join(visible.get("card_types", ())))
            payload["cards"] = [card]
        # Same GRE message/seat/cause is one burst: do not narrate twenty mills separately.
        previous = game._commentary_journal[-1] if game._commentary_journal else None
        if (
            previous
            and previous["kind"] == kind
            and previous["seq"] == game._raw_gre_sequence
            and previous["seat"] == (obj.controller_seat_id or obj.owner_seat_id)
            and all(
                previous["payload"].get(k) == payload.get(k)
                for k in ("category", "from_zone", "to_zone", "source_id")
            )
        ):
            # Published snapshots may still reference the previous record.
            # Replace it rather than mutating their already-published facts.
            game._commentary_journal[-1] = {
                **previous,
                "payload": {
                    **previous["payload"],
                    "count": previous["payload"]["count"] + 1,
                    "cards": (previous["payload"]["cards"] + payload["cards"])[:12],
                },
            }
            return
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


def _exile_event_payload(payload, observation):
    """Resolve old and current journal entries using only the present, revealed exile instance."""
    known = {}
    for card in observation.public_state["zones"]["exile"]:
        iid = card.get("instance_id")
        if type(iid) is int and iid > 0:
            known[iid] = None if iid in known else card

    def public_identity(row):
        if (
            not isinstance(row, dict)
            or type(row.get("instance_id")) is not int
            or type(row.get("grp_id")) is not int
        ):
            return {}
        card = known.get(row["instance_id"])
        if not card or card.get("visibility") != "public" or card.get("grp_id") != row["grp_id"]:
            return {}
        return {
            key: card[key]
            for key in ("instance_id", "grp_id", "name", "mana_cost", "type_line")
            if key in card
        }

    result = {key: payload[key] for key in ("category", "from_zone", "to_zone", "count") if key in payload}
    result.update(public_identity(payload))
    result["cards"] = [
        identity for row in (payload.get("cards") or [])[:12] if (identity := public_identity(row))
    ]
    return result


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
            # Project only identities Arena made available to this player.
            hand = (snapshot.get("zones") or {}).get("my_hand", [])
            local = snapshot.get("local_seat_id")
            cards = [_public_card({**c, "visibility": "public"}) for c in hand[:100] if isinstance(c, dict)]
            deck = [v for v in snapshot.get("deck_cards", [])[:250] if type(v) is int]
            cards = [card for card in cards if local and card.get("owner_seat_id") in (None, local)]
            private_state = {"local_seat": local, "hand": cards, "deck": deck}
            from arenamcp.library_counts import observed_library_count

            if local in (1, 2):
                bridge = snapshot.get("_bridge_game_state_id")
                matched = not (snapshot.get("_bridge_connected") or snapshot.get("bridge_connected")) or (
                    type(bridge) is int and bridge == snapshot.get("_log_game_state_id")
                )
                visible = (snapshot.get("library_visible") or []) if matched else []
                known = [
                    {"name": row["name"], "grp_id": row["grp_id"]}
                    for row in visible
                    if isinstance(row, dict)
                    and row.get("owner_seat_id") == local
                    and row.get("name")
                    and type(row.get("grp_id")) is int
                    and row["grp_id"] > 3
                    and not row.get("face_down")
                    and row.get("identity_known", True) is True
                ]
                size = observed_library_count(snapshot)
                if known or size is not None:
                    private_state["library"] = {
                        "size": size,
                        "visible_cards": known,
                        "source": "observed local library; identities may be incomplete and are unordered",
                    }
                inventory = snapshot.get("commentary_library_inventory")
                if matched and isinstance(inventory, list):
                    private_state["library_inventory"] = inventory
                revealed = snapshot.get("revealed_cards")
                if matched and isinstance(revealed, list):
                    seen = [
                        {"name": row["name"], "grp_id": row["grp_id"]}
                        for row in revealed
                        if isinstance(row, dict)
                        and row.get("zone", "").lower() in {"hand", "zonetype_hand"}
                        and row.get("name")
                        and type(row.get("grp_id")) is int
                        and row["grp_id"] > 3
                        and row.get("owner_seat_id", 3 - local) == 3 - local
                        and not row.get("face_down")
                    ]
                    if seen:
                        private_state["revealed_opposing_hand"] = {
                            "cards": seen,
                            "source": "cards revealed in opponent hand; other cards unknown",
                        }
            from .overview_quality import speakable_overview

            overview = snapshot.get("commentary_deck_overview")
            if (
                isinstance(overview, dict)
                and isinstance(overview.get("identity"), str)
                and speakable_overview(overview.get("summary"))
            ):
                private_state["deck_overview"] = {
                    "identity": overview["identity"],
                    "summary": overview["summary"],
                }
            observation = replace(observation, private_state=private_state)
        return replace(observation, snapshot_id=state_fingerprint(observation))

    def observe(self, snapshot, private=False, *, arrived_at=None):
        current = self.project(snapshot, private)
        if arrived_at is not None:
            current = replace(current, arrived_at=arrived_at)
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
            from .timeline import event_lifetime

            if not isinstance(received, (float, int)) or self.clock() - received > event_lifetime(
                record["kind"]
            ):
                continue
            payload = dict(record["payload"])
            if payload.get("to_zone") == "exile":
                payload = _exile_event_payload(payload, current)
            elif record["kind"] == "outcome":
                public_cards = {
                    c.get("grp_id"): c
                    for rows in current.public_state["zones"].values()
                    for c in rows
                    if c.get("grp_id") and not c.get("face_down") and c.get("name") != "Unknown card"
                }
                payload["cards"] = [
                    {
                        **c,
                        **{
                            k: public_cards[c["grp_id"]][k]
                            for k in ("mana_cost", "type_line")
                            if k in public_cards.get(c.get("grp_id"), {})
                        },
                    }
                    for c in payload.get("cards", ())[:12]
                ]
            events.append(
                Evidence(
                    f"gre:{current.epoch.match_id}:{current.epoch.game_number}:{record['id']}",
                    record["kind"],
                    int(record["seq"]),
                    str(record["snapshot_id"]),
                    payload=payload,
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
