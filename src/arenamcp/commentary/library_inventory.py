"""Private printed-card census for an unbroken GRE sequence of local draws.

An initial whole library is the submitted deck for this game. Subtract a card
only when the shrinking library, growing hand, GRE instance id and visible
physical identity agree. A shuffle with new ids, a return, a summarized window
or another unsupported transition ends this proof. Never infer draw order.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass


def _positive(value):
    return type(value) is int and value > 0


def _ids(values):
    if type(values) is not list or not all(_positive(i) for i in values):
        return None
    ids = frozenset(values)
    return ids if len(ids) == len(values) else None


def _phase_engine_sentinel(annotation):
    """The exact transient GRE phase/step shape that references the engine as zero."""
    if (
        set(annotation) != {"id", "type", "affectedIds", "details"}
        or not _positive(annotation["id"])
        or annotation["type"] != ["AnnotationType_PhaseOrStepModified"]
        or annotation["affectedIds"] != [0]
        or type(annotation["affectedIds"][0]) is not int
    ):
        return False
    details = annotation["details"]
    if type(details) is not list or len(details) != 2:
        return False
    for detail, key in zip(details, ("phase", "step"), strict=True):
        if (
            type(detail) is not dict
            or set(detail) != {"key", "type", "valueInt32"}
            or detail["key"] != key
            or detail["type"] != "KeyValuePairValueType_int32"
            or type(detail["valueInt32"]) is not list
            or len(detail["valueInt32"]) != 1
            or type(detail["valueInt32"][0]) is not int
            or detail["valueInt32"][0] < 0
        ):
            return False
    return True


def _undisturbed_members(message, zones, library_zone, members):
    """Reject contradictory membership and any identities revealed/rewritten in-place.

    We prove only the printed identities of departing draws. Once Arena starts
    materializing or renaming an in-library member, that proof cannot justify
    treating a later same-id, different-card update as an unchanged deck.
    """
    for zone in zones:
        ids = zone.get("objectInstanceIds")
        if ids is None or zone.get("zoneId") == library_zone:
            continue
        other = _ids(ids)
        if other is None or members & other:
            return False
    objects = message.get("gameObjects", [])
    if not isinstance(objects, list) or any(
        not isinstance(o, dict)
        or not _positive(o.get("instanceId"))
        or o.get("instanceId") in members
        or o.get("zoneId") == library_zone
        for o in objects
    ):
        return False
    deleted = message.get("diffDeletedInstanceIds", [])
    if type(deleted) is not list or any(not _positive(i) or i in members for i in deleted):
        return False
    annotations = message.get("annotations", [])
    persistent = message.get("persistentAnnotations", [])
    if not isinstance(annotations, list) or not isinstance(persistent, list):
        return False
    for index, annotation in enumerate((*annotations, *persistent)):
        if not isinstance(annotation, dict):
            return False
        affected = _ids(annotation.get("affectedIds", []))
        if affected is None:
            if index < len(annotations) and _phase_engine_sentinel(annotation):
                continue
            return False
        if affected & members or annotation.get("affectorId") in members:
            return False
        if "AnnotationType_ObjectIdChanged" not in (annotation.get("type") or []):
            continue
        ids = _ids(annotation.get("affectedIds"))
        if ids is None or ids & members:
            return False
        details = annotation.get("details", [])
        mapped = {}
        for detail in details:
            if not isinstance(detail, dict):
                return False
            key = detail.get("key")
            if key not in ("orig_id", "new_id"):
                continue
            values = detail.get("valueInt32")
            if (
                key in mapped
                or type(values) is not list
                or len(values) != 1
                or not _positive(values[0])
                or values[0] in members
            ):
                return False
            mapped[key] = values[0]
        if set(mapped) != {"orig_id", "new_id"} or mapped["orig_id"] == mapped["new_id"]:
            return False
    return True


@dataclass
class DrawInventory:
    match: str
    seat: int
    library_zone: int
    hand_zone: int
    state_id: int
    members: frozenset[int]
    remaining: Counter[int]

    @classmethod
    def start(cls, message, connection):
        info = message.get("gameInfo") or {}
        match = info.get("matchID")
        if (
            message.get("type") != "GameStateType_Full"
            or type(message.get("gameStateId")) is not int
            or message["gameStateId"] != 1
            or info.get("stage") != "GameStage_Start"
            or type(info.get("gameNumber")) is not int
            or info["gameNumber"] != 1
            or not isinstance(match, str)
            or not match
            or not connection
            or connection.get("seat_source") != 2
            or connection.get("match_id") != match
        ):
            return None
        seat, deck = connection.get("local_seat_id"), connection.get("deck_cards")
        if (
            not _positive(seat)
            or type(deck) is not list
            or not 1 <= len(deck) <= 250
            or not all(type(g) is int and g > 4 for g in deck)
        ):
            return None
        rows = message.get("zones")
        if not isinstance(rows, list) or any(not isinstance(z, dict) for z in rows):
            return None
        zones = [z for z in rows if z.get("ownerSeatId") == seat]
        libraries = [z for z in zones if z.get("type") == "ZoneType_Library"]
        hands = [z for z in zones if z.get("type") == "ZoneType_Hand"]
        if len(libraries) != 1 or len(hands) != 1 or hands[0].get("objectInstanceIds"):
            return None
        library_zone, hand_zone = libraries[0].get("zoneId"), hands[0].get("zoneId")
        members = _ids(libraries[0].get("objectInstanceIds"))
        if (
            not members
            or len(members) != len(deck)
            or not _positive(library_zone)
            or not _positive(hand_zone)
            or library_zone == hand_zone
        ):
            return None
        if not _undisturbed_members(message, rows, library_zone, members):
            return None
        return cls(match, seat, library_zone, hand_zone, 1, members, Counter(deck))

    def _drawn_cards(self, message, removed, hand, objects):
        """Link departed members to this message's own visible hand objects."""
        renamed, drawn = {}, set()
        for a in message.get("annotations", []):
            if not isinstance(a, dict):
                return None
            types = a.get("type") or []
            details = {
                d["key"]: d
                for d in a.get("details", [])
                if isinstance(d, dict) and isinstance(d.get("key"), str)
            }
            if "AnnotationType_ObjectIdChanged" in types:
                old = details.get("orig_id", {}).get("valueInt32")
                new = details.get("new_id", {}).get("valueInt32")
                if type(old) is list and len(old) == 1 and _positive(old[0]) and old[0] in removed:
                    if type(new) is not list or len(new) != 1 or not _positive(new[0]) or old[0] in renamed:
                        return None
                    renamed[old[0]] = new[0]
            if (
                "AnnotationType_ZoneTransfer" in types
                and details.get("category", {}).get("valueString") == ["Draw"]
                and details.get("zone_src", {}).get("valueInt32") == [self.library_zone]
                and details.get("zone_dest", {}).get("valueInt32") == [self.hand_zone]
            ):
                targets = _ids(a.get("affectedIds"))
                if not targets:
                    return None
                drawn.update(targets)
        if self.state_id == 1 and (renamed or hand != removed):
            return None
        now = {renamed.get(i, i) for i in removed}
        if len(now) != len(removed) or not now <= hand or (self.state_id != 1 and now != drawn):
            return None
        rows = [o for o in objects if o.get("instanceId") in now]
        if len(rows) != len(now) or {o.get("instanceId") for o in rows} != now:
            return None
        for o in rows:
            gid = o.get("grpId")
            if (
                o.get("type") != "GameObjectType_Card"
                or o.get("zoneId") != self.hand_zone
                or o.get("ownerSeatId") != self.seat
                or o.get("visibility") != "Visibility_Private"
                or o.get("viewers") != [self.seat]
                or type(gid) is not int
                or gid <= 4
                or o.get("overlayGrpId", gid) != gid
                or o.get("isFacedown")
            ):
                return None
        return Counter(o["grpId"] for o in rows)

    def advance(self, message):
        state_id = message.get("gameStateId")
        if (
            message.get("type") != "GameStateType_Diff"
            or not _positive(state_id)
            or state_id != self.state_id + 1
            or type(message.get("prevGameStateId")) is not int
            or message["prevGameStateId"] != self.state_id
        ):
            return False
        zones = message.get("zones", [])
        if type(zones) is not list or any(not isinstance(z, dict) for z in zones):
            return False
        if any(
            z.get("type") == "ZoneType_Library"
            and z.get("ownerSeatId") == self.seat
            and z.get("zoneId") != self.library_zone
            for z in zones
        ):
            return False
        updates = [z for z in zones if z.get("zoneId") == self.library_zone]
        if len(updates) > 1:
            return False
        members = self.members
        if updates:
            zone = updates[0]
            if (
                zone.get("ownerSeatId", self.seat) != self.seat
                or zone.get("type", "ZoneType_Library") != "ZoneType_Library"
            ):
                return False
            members = _ids(zone.get("objectInstanceIds"))
            if members is None or members - self.members:
                return False
        objects = message.get("gameObjects", [])
        if not isinstance(objects, list) or any(not isinstance(o, dict) for o in objects):
            return False
        if not _undisturbed_members(message, zones, self.library_zone, members):
            return False
        removed = self.members - members
        if removed:
            hands = [z for z in zones if z.get("zoneId") == self.hand_zone]
            if (
                len(hands) != 1
                or hands[0].get("ownerSeatId", self.seat) != self.seat
                or hands[0].get("type", "ZoneType_Hand") != "ZoneType_Hand"
            ):
                return False
            hand = _ids(hands[0].get("objectInstanceIds"))
            if not hand:
                return False
            taken = self._drawn_cards(message, removed, hand, objects)
            if not taken:
                return False
            if taken - self.remaining:
                return False
            self.remaining -= taken
        self.members = members
        self.state_id = state_id
        return True
