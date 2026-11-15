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
        # A deleted member, an unexpected move or a changed printed identity in
        # the library must never leave an old exact claim alive.
        deleted = message.get("diffDeletedInstanceIds") or []
        if not isinstance(deleted, list) or any(not _positive(i) or i in members for i in deleted):
            return False
        if any(
            o.get("instanceId") in members
            and (
                o.get("zoneId", self.library_zone) != self.library_zone
                or o.get("overlayGrpId", o.get("grpId")) != o.get("grpId")
            )
            for o in objects
        ):
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
