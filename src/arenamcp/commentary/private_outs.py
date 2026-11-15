"""Local, deterministic counts of named removal spells against a public flyer.

The proof deliberately covers one opposing threat, with known printed targeting
conditions. An unsupported remaining card is not a known non-answer. We name
specific copies, never say 'only X outs', predict a draw or claim castability.
"""

from __future__ import annotations

import re
from collections import Counter

from arenamcp.ability_synthesizer import Damage, Destroy, Exile
from arenamcp.card_db import is_unknown_card_name
from arenamcp.card_facts.source_identity import legacy_lookup_hash
from arenamcp.combat_keywords import has_combat_keyword

_BASIC = re.compile(
    r"(?:Snow )?Basic (?:Snow )?Land (?:—|-) (?:Forest|Mountain|Island|Plains|Swamp|Wastes)", re.I
)
_TARGET_DEFENSE = re.compile(
    r"hexproof|shroud|protection|indestructible|ward\b|regenerate|can't be|cannot be|instead|counter target|phase[sd]? out|shield counter|can't cast|cannot cast",
    re.I,
)
_REMOVAL = re.compile(
    r"(Destroy|Exile) target (creature(?: with flying| or planeswalker)?|nonland permanent|permanent)\."
)


def _named_source(card):
    name = card.get("name")
    return (
        isinstance(name, str)
        and 0 < len(name) <= 100
        and name == name.strip()
        and all(c.isprintable() for c in name)
        and not is_unknown_card_name(name)
        and legacy_lookup_hash(card)
    )


def _removal_effect(card):
    if not _named_source(card) or card.get("type_line") not in ("Instant", "Sorcery"):
        return None
    match = _REMOVAL.fullmatch(card["oracle_text"])
    if match:
        kind, target = match.groups()
        return Destroy(target) if kind == "Destroy" else Exile(target)
    damage = re.fullmatch(
        rf"(?:{re.escape(card['name'])}|This spell) deals ([1-9][0-9]?) damage to (any target|target creature|target creature with flying)\.",
        card["oracle_text"],
    )
    if damage:
        return Damage(amount=int(damage[1]), target=damage[2])
    return None


def _sole_opposing_flyer(snapshot):
    zones = snapshot.get("zones") or {}
    if (
        zones.get("stack")
        or snapshot.get("stack")
        or snapshot.get("controlled_turn", {}).get("deciding_for_opponent")
    ):
        return None
    local, opponent = snapshot.get("local_seat_id"), snapshot.get("opponent_seat_id")
    if type(local) is not int or type(opponent) is not int or {local, opponent} != {1, 2}:
        return None
    candidates = []
    board = zones.get("battlefield")
    if not isinstance(board, list):
        return None
    for card in board:
        if (
            not isinstance(card, dict)
            or card.get("controller_seat_id") not in (local, opponent)
            or card.get("visibility") not in (None, "Visibility_Public", "public")
            or card.get("face_down")
            or not _named_source(card)
        ):
            return None
        if any(
            card.get(key)
            for key in (
                "modified_name",
                "modified_types",
                "removed_abilities",
                "granted_abilities",
                "chosen_player_links",
                "protection_from_players",
                "is_phased_out",
                "copied_from_grp_id",
                "attached_to_id",
            )
        ):
            return None
        if card.get("controller_seat_id") is None or (
            not card.get("oracle_text")
            and not _BASIC.fullmatch(card["type_line"])
            and "Creature" not in card["type_line"].split(" — ")[0].split()
        ):
            return None
        if _TARGET_DEFENSE.search(card["oracle_text"].replace("’", "'")) or any(
            has_combat_keyword(card, key) for key in ("hexproof", "indestructible", "shroud")
        ):
            return None
        if card["controller_seat_id"] == local or _BASIC.fullmatch(card["type_line"]):
            continue
        if (
            card.get("controller_seat_id") != opponent
            or "Creature" not in card["type_line"].split(" — ")[0].split()
            or not has_combat_keyword(card, "flying")
            or type(card.get("instance_id")) is not int
            or card["instance_id"] <= 0
            or type(card.get("power")) is not int
            or card["power"] <= 0
            or card.get("counters")
        ):
            return None
        candidates.append(card)
    return candidates[0] if len(candidates) == 1 else None


def private_removal_fact(snapshot, game, *, allow_private, card_lookup):
    if allow_private is not True or not game or not isinstance(snapshot, dict):
        return None
    # A bridge overlay and its GRE census can describe different revisions.
    state_id = snapshot.get("_log_game_state_id")
    bridge = snapshot.get("_bridge_game_state_id")
    if (snapshot.get("_bridge_connected") or snapshot.get("bridge_connected")) and (
        type(bridge) is not int or bridge != state_id
    ):
        return None
    threat = _sole_opposing_flyer(snapshot)
    if not threat:
        return None
    seat = snapshot["local_seat_id"]
    match = snapshot.get("match_id")
    number = snapshot.get("game_number")
    rows = snapshot["zones"]["battlefield"]
    battlefield = {(c.get("instance_id"), c.get("grp_id"), c.get("controller_seat_id")) for c in rows}
    if len(battlefield) != len(rows):
        return None
    counts = game.commentary_library_counts(
        match, number, seat, state_id, allow_private=allow_private, battlefield=battlefield
    )
    if not counts:
        return None
    qualified = Counter()
    for gid, count in counts.items():
        card = card_lookup(gid)
        if not isinstance(card, dict):
            continue
        effect = _removal_effect(card)
        if isinstance(effect, (Destroy, Exile)) or (
            isinstance(effect, Damage)
            and type(threat.get("toughness")) is int
            and 0 < threat["toughness"] <= effect.amount
        ):
            qualified[card["name"]] += count
    if not qualified or len(qualified) > 3:
        return None
    return {
        "match_id": match,
        "game_number": number,
        "seat": seat,
        "state_id": state_id,
        "threat_id": threat["instance_id"],
        "threat": threat["name"],
        "remaining": sum(counts.values()),
        "answers": dict(sorted(qualified.items())),
    }


def private_outs_for_desktop(coach, snapshot):
    from arenamcp.match_context import prepared_card

    from .config import CommentaryConfig

    settings = getattr(coach, "settings", {})
    config = CommentaryConfig.from_settings(settings)
    if not config.enabled or config.private_information is not True:
        return None
    server = getattr(getattr(coach, "_mcp", None), "_server", None)
    try:
        return private_removal_fact(
            snapshot,
            getattr(server, "game_state", None),
            allow_private=True,
            card_lookup=prepared_card,
        )
    except (TypeError, KeyError, ValueError, AttributeError):
        return None


def private_removal_line(fact, snapshot):
    """Recheck the public threat and privacy epoch at desktop admission."""
    if (
        not isinstance(fact, dict)
        or fact.get("match_id") != snapshot.get("match_id")
        or type(fact.get("game_number")) is not int
        or fact["game_number"] != snapshot.get("game_number")
        or type(fact.get("state_id")) is not int
        or fact["state_id"] != snapshot.get("_log_game_state_id")
        or type(fact.get("seat")) is not int
        or fact["seat"] != snapshot.get("local_seat_id")
    ):
        return None
    threat = _sole_opposing_flyer(snapshot)
    if (
        not threat
        or type(fact.get("threat_id")) is not int
        or (threat["instance_id"], threat["name"]) != (fact["threat_id"], fact.get("threat"))
    ):
        return None
    answers = fact.get("answers")
    size = fact.get("remaining")
    if (
        type(size) is not int
        or not 1 <= size <= 250
        or not isinstance(answers, dict)
        or not 1 <= len(answers) <= 3
    ):
        return None
    if (
        any(
            type(count) is not int
            or count < 1
            or not isinstance(name, str)
            or not 1 <= len(name) <= 100
            or name != name.strip()
            or not name.isprintable()
            for name, count in answers.items()
        )
        or sum(answers.values()) > size
    ):
        return None
    copies = ", ".join(
        f"{count} {'copy' if count == 1 else 'copies'} of {name}" for name, count in answers.items()
    )
    return threat[
        "instance_id"
    ], f"Against {threat['name']}, your {size}-card library still has {copies} for creature removal."
