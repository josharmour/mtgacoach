"""Recheck the premises of a native semantic audit without importing ArenaOnAir in the UI."""

import re

from arenamcp.player_names import actor_name

_TOKEN = re.compile(r"(?:identity|field|board|life|(?:seat|hand|object)[0-9]{1,12})")


def scope_keys(scope):
    if not isinstance(scope, str) or len(scope) > 512:
        return ()
    keys = scope.removeprefix("audit:").split(",")
    if (
        not scope.startswith("audit:")
        or len(keys) > 24
        or "identity" not in keys
        or keys != sorted(set(keys))
        or not all(_TOKEN.fullmatch(k) for k in keys)
    ):
        return ()
    return tuple(keys)


def permanent_fact(card):
    if not card.get("grp_id") or not card.get("name") or card.get("face_down"):
        return None
    return {
        k: card[k]
        for k in (
            "instance_id",
            "grp_id",
            "name",
            "controller_seat_id",
            "type_line",
            "power",
            "toughness",
            "is_tapped",
        )
        if card.get(k) is not None
    }


def _board(observation):
    # Mirror the native *audited view*, including its cross-zone completeness cap.
    # Equality with the original native fact is checked before issuing this scope.
    public = observation.public_state
    objects = []
    complete = True
    all_ids = [
        c.get("instance_id")
        for cards in public["zones"].values()
        for c in cards
        if type(c.get("instance_id")) is int
    ]
    if len(all_ids) != len(set(all_ids)) or any(
        c.get("instance_id") in all_ids for c in observation.private_state.get("hand", ())
    ):
        return None  # Native snapshot resolves object identity across zones.
    for zone, cards in public["zones"].items():
        members = [c for c in cards if type(c.get("instance_id")) is int]
        complete &= len(members) <= 32
        for c in members[:32]:
            seat = c.get("controller_seat_id")
            objects.append(
                dict(
                    instance_id=c["instance_id"],
                    name=c.get("name"),
                    grp_id=c.get("grp_id"),
                    type=c.get("type_line"),
                    zone=zone,
                    controller=seat,
                    controller_name=actor_name(public, seat)
                    if seat in [p.get("seat_id") for p in public["players"]]
                    else None,
                    power=c.get("power"),
                    toughness=c.get("toughness"),
                    tapped=c.get("is_tapped"),
                )
            )
    return {"objects": objects[:48], "complete": complete and len(objects) <= 48, "source": "public_GRE"}


def value(observation, token):
    public = observation.public_state
    local = public.get("local_seat_id")
    if token == "identity":
        return [local, [(p["seat_id"], actor_name(public, p["seat_id"])) for p in public["players"]]]
    if token in ("board", "field"):
        board = _board(observation)
        if board is None:
            return None
        if token == "field":
            board["objects"] = [
                {k: v for k, v in c.items() if k not in {"zone", "controller_name"} and v is not None}
                for c in board["objects"]
                if c["zone"] == "battlefield"
            ]
        return board
    if token == "life":
        return [(p["seat_id"], p.get("life_total")) for p in public["players"]]
    n = int(re.search(r"[0-9]+$", token)[0])
    if token.startswith("object"):
        hits = [c for c in public["zones"]["battlefield"] if c.get("instance_id") == n]
        return permanent_fact(hits[0]) if len(hits) == 1 else None
    if token.startswith("seat"):
        for p in public["players"]:
            if p["seat_id"] == n:
                return dict(seat=n, name=actor_name(public, n), life=p.get("life_total"))
    if token.startswith("hand") and n == local and "hand" in observation.private_state:
        cards = observation.private_state["hand"]
        if all(c.get("grp_id") and c.get("name") and type(c.get("instance_id")) is int for c in cards):
            return dict(
                seat=n,
                cards=[{k: c[k] for k in ("instance_id", "name", "grp_id")} for c in cards[:20]],
                source="visible_owner_GRE",
                complete=len(cards) <= 20,
            )
    return None


def binding(scope, tracked, sources, observation, text):
    """Unknown provenance: withhold; known timing/computed claims: exact snapshot only."""
    from .timeline import historical_scope

    facts, deps, refs = (tracked.get(k) for k in ("facts", "deps", "refs"))
    if not isinstance(facts, dict) or not isinstance(deps, dict) or not refs or set(refs) != set(facts):
        return None
    if any(facts[k] != deps[k] for k in facts.keys() & deps.keys()):
        return None
    keys = {"identity"}
    snapshot = scope == "snapshot" or "?" in text
    for key, fact in {**facts, **deps}.items():
        if not isinstance(fact, dict):
            return None
        if key.startswith("event:"):
            if fact.get("kind") == "game_read":
                snapshot = True
            elif key not in sources:
                return None
            elif sources[key].visibility != "public":
                snapshot = True
            continue
        if re.fullmatch(r"card:[0-9]+", key) and fact.get("source") == "local_card_database":
            if set(fact) - {
                "name",
                "mana_cost",
                "type_line",
                "rules",
                "source",
                "mana_paid",
                "hypothetical_arithmetic",
            }:
                return None  # Future derived card evidence may depend on mutable state.
            if "hypothetical_arithmetic" in fact:
                keys.add("life")
            continue
        if (
            key.startswith("state:annotation:")
            and key in deps
            and fact.get("source") == "reviewed_card_facts"
        ):
            snapshot = True
            continue
        if (
            key.startswith("state:opponent_read:")
            and key in deps
            and fact.get("source") == "reviewed_public_card_facts"
            and fact.get("kind") == "opponent_plan_inference"
            and fact.get("visibility") == "public"
        ):
            snapshot = True
            continue
        if key in {"state:turn", "state:game_read", "private:deck_plan"} and key in deps:
            snapshot = True
            continue
        if key not in deps:
            return None
        if key == "state:local_seat":
            if fact.get("seat") != observation.public_state.get("local_seat_id"):
                return None
            continue
        token = {"state:battlefield": "field", "state:board": "board"}.get(key)
        if token is None:
            m = re.fullmatch(r"(state:player|state:permanent|private:hand):([0-9]{1,12})", key)
            if not m:
                return None
            token = {"state:player": "seat", "state:permanent": "object", "private:hand": "hand"}[m[1]] + m[2]
        if value(observation, token) != fact:
            return None
        keys.add(token)
    if snapshot:
        return "snapshot", ""
    if historical_scope(scope, deps, text) and keys == {"identity"} and any(k in sources for k in refs):
        return "historical", ""
    if scope != "position":
        return "snapshot", ""
    wire = "audit:" + ",".join(sorted(keys))
    return ("position", wire) if scope_keys(wire) else None
