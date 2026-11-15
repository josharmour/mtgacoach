"""Local, deterministic counts of named removal spells against a public creature.

One public threat and a proven remaining local population are required. Exact
single-card removal totals additionally require classification of every identity.
An unsupported identity permits only positive named counts.
"""

from __future__ import annotations

import re
from collections import Counter

from arenamcp.ability_synthesizer import Damage
from arenamcp.card_db import get_card_database, is_unknown_card_name
from arenamcp.card_facts.removal import (
    printed_removal_effects,
    reviewed_library_nonremoval,
    supported_public_creature,
)
from arenamcp.card_facts.source_identity import legacy_lookup_hash
from arenamcp.combat_keywords import has_combat_keyword
from arenamcp.mana import mana_cost_to_cmc
from arenamcp.ward import ward_of

_BASIC = re.compile(
    r"(?:Snow )?Basic (?:Snow )?Land (?:—|-) (?:Forest|Mountain|Island|Plains|Swamp|Wastes)", re.I
)
_SAFE_KEYWORDS = frozenset(
    {
        "flying",
        "first strike",
        "double strike",
        "deathtouch",
        "haste",
        "lifelink",
        "menace",
        "reach",
        "trample",
        "vigilance",
        "defender",
        "flash",
    }
)
_BASIC_MANA = re.compile(r"\(?\{(?:o)?T\}: Add \{(?:o)?[WUBRGC]\}\.\)?", re.I)


def _mana_land(card):
    types = card["type_line"].split(" — ")[0].split()
    if "Land" not in types or set(types) - {"Land", "Snow", "Basic", "Legendary"}:
        return False
    text = card["oracle_text"].replace("<i>", "").replace("</i>", "").strip()
    if _BASIC.fullmatch(card["type_line"]):
        return not text or bool(_BASIC_MANA.fullmatch(text))
    lines = text.splitlines()
    if lines and lines[0] in {
        f"{card['name']} enters tapped.",
        "This land enters tapped.",
        "This land enters tapped unless you control two or fewer other lands.",
        "This land enters tapped unless you control a basic land.",
        "This land enters tapped unless you control two or more basic lands.",
    }:
        lines = lines[1:]
    return bool(lines) and all(
        re.fullmatch(r"\{(?:o)?T\}: Add \{(?:o)?[WUBRGC]\}(?: or \{(?:o)?[WUBRGC]\})?\.", line)
        for line in lines
    )


def _simple_text(card):
    # Native localized markup can accompany the Flying keyword's reminder.
    text = card["oracle_text"].replace("<i>", "").replace("</i>", "")
    text = re.sub(
        r" \(This creature can['’]t be blocked except by creatures with flying or reach\.\)",
        "",
        text,
        flags=re.I,
    )
    if _mana_land(card) or supported_public_creature(legacy_lookup_hash(card)):
        return True
    if "Creature" not in card["type_line"].split(" — ")[0].split():
        return False
    return not text.strip() or all(w.strip().casefold() in _SAFE_KEYWORDS for w in re.split(r"[,\n]", text))


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


def _answers_creature(card, threat, *, local_creature=False, local_basic=False):
    """True, False or unknown: can this card directly remove this public creature?"""
    if not isinstance(card, dict) or not _named_source(card):
        return None
    if _mana_land(card) or reviewed_library_nonremoval(card):
        return False
    if card["oracle_text"] and "Creature" in card["type_line"].split(" — ")[0].split() and _simple_text(card):
        # These recognized creature spells have no direct removal of their own.
        return False
    effects = printed_removal_effects(card, local_creature=local_creature, local_basic=local_basic)
    if not effects:
        return None
    types = threat["type_line"].split(" — ")[0].split()
    results = []
    for effect in effects:
        if (
            effect.target.endswith("with flying")
            and not has_combat_keyword(threat, "flying")
            or effect.target.startswith("nonland permanent")
            and "Land" in types
            or effect.target == "artifact"
            and "Artifact" not in types
            or effect.target == "nonartifact creature"
            and "Artifact" in types
            or effect.target == "nonlegendary creature"
            and "Legendary" in types
        ):
            results.append(False)
        elif effect.target.endswith(("with mana value 2 or less", "with mana value 3 or less")):
            # Restrict this comparison to a complete, simple, single-face
            # printed cost. Missing, nonstandard, or ambiguous costs abstain.
            limit = 2 if effect.target.endswith("2 or less") else 3
            cost = threat.get("mana_cost")
            if isinstance(cost, str) and re.fullmatch(r"(?:\{(?:[0-9]+|[WUBRGC])\})+", cost):
                results.append(mana_cost_to_cmc(cost) <= limit)
            else:
                results.append(None)
        elif effect.target == "tapped creature":
            tapped = threat.get("is_tapped")
            results.append(tapped if type(tapped) is bool else None)
        elif isinstance(effect, Damage):
            toughness = threat.get("modified_toughness", threat.get("toughness"))
            marked = threat.get("damage", 0)
            if type(toughness) is int and type(marked) is int and 0 <= marked < toughness:
                results.append(effect.amount >= toughness - marked)
            else:
                results.append(None)
        else:
            results.append(True)
    if True in results:
        return True
    return None if None in results else False


def _verified_native_abilities(card):
    """Every native ability must resolve to a line of the already-qualified print."""
    if card.get("native_abilities_incomplete"):
        return False
    ids = card.get("ability_grp_ids")
    if ids is None or ids == []:
        return True
    if not isinstance(ids, (tuple, list)) or any(type(i) is not int or i <= 0 for i in ids):
        return False
    lines = {line.strip() for line in card["oracle_text"].splitlines()}
    printed_types = card["type_line"].split(" — ")[0].split()
    self_names = {card["name"]}
    if "Legendary" in printed_types:
        self_names.add(card["name"].partition(",")[0])
    for kind in ("Creature", "Land"):
        if kind in printed_types:
            self_names.add("this " + kind.lower())
            self_names.add("This " + kind.lower())
    try:
        lookup = get_card_database().get_ability_text
        for identity in ids:
            text = lookup(identity)
            if not isinstance(text, str) or not text.strip():
                return False
            text = text.strip()
            if text in lines or (_mana_land(card) and _BASIC_MANA.fullmatch(text)):
                continue
            if "CARDNAME" not in text or not any(
                re.sub(r"\bCARDNAME\b", lambda _, name=name: name, text) in lines for name in self_names
            ):
                return False
    except (OSError, ValueError, LookupError, RuntimeError):
        return False
    return True


def _public_removal_threat(snapshot):
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
            or type(card.get("controller_seat_id")) is not int
            or card.get("controller_seat_id") not in (local, opponent)
            or card.get("visibility") not in (None, "Visibility_Public", "public")
            or card.get("face_down")
            or not _named_source(card)
            or not _simple_text(card)
            or not _verified_native_abilities(card)
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
        if ward_of(card) or any(
            has_combat_keyword(card, key) for key in ("hexproof", "indestructible", "shroud")
        ):
            return None
        if card["controller_seat_id"] == local or _mana_land(card):
            continue
        if (
            "Creature" not in card["type_line"].split(" — ")[0].split()
            or type(card.get("instance_id")) is not int
            or card["instance_id"] <= 0
            or type(card.get("power")) is not int
            or card["power"] <= 0
            or card.get("counters")
        ):
            return None
        candidates.append(card)
    if len(candidates) <= 1:
        return candidates[0] if candidates else None
    from arenamcp.board_assessment import highest_public_creature_threat

    return highest_public_creature_threat(board, candidates, local, opponent)


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
    threat = _public_removal_threat(snapshot)
    if not threat:
        return None
    seat = snapshot["local_seat_id"]
    match = snapshot.get("match_id")
    number = snapshot.get("game_number")
    rows = snapshot["zones"]["battlefield"]
    battlefield = {c.get("instance_id"): c for c in rows}
    if len(battlefield) != len(rows):
        return None
    counts = game.commentary_library_counts(
        match, number, seat, state_id, allow_private=allow_private, battlefield=battlefield
    )
    if counts is None:
        return None
    qualified = Counter()
    complete = True
    ours = [c for c in rows if c["controller_seat_id"] == seat]
    local_creature = any("Creature" in c["type_line"].split(" — ")[0].split() for c in ours)
    local_basic = any(_BASIC.fullmatch(c["type_line"]) for c in ours)
    for gid, count in counts.items():
        card = card_lookup(gid)
        answers = _answers_creature(card, threat, local_creature=local_creature, local_basic=local_basic)
        if answers is True:
            qualified[card["name"]] += count
        elif answers is None:
            complete = False
    if not qualified and not complete:
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
        **({"direct_removal_total": sum(qualified.values())} if complete else {}),
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
    threat = _public_removal_threat(snapshot)
    if (
        not threat
        or type(fact.get("threat_id")) is not int
        or (threat["instance_id"], threat["name"]) != (fact["threat_id"], fact.get("threat"))
    ):
        return None
    answers = fact.get("answers")
    size = fact.get("remaining")
    if type(size) is not int or not 0 <= size <= 250 or not isinstance(answers, dict) or len(answers) > size:
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
    copies = [f"{count} {'copy' if count == 1 else 'copies'} of {name}" for name, count in answers.items()]
    if "direct_removal_total" in fact:
        total = fact["direct_removal_total"]
        if type(total) is not int or total != sum(answers.values()):
            return None
        if not total:
            text = f"Your {size}-card library has no direct removal left for {threat['name']}."
            return threat["instance_id"], text
        noun = "card" if total == 1 else "cards"
        lead = f"Direct removal for {threat['name']}: {total} {noun} among the {size} left"
        text = f"{lead}, {', '.join(copies)}."
    elif answers:
        lead = f"Against {threat['name']}, your {size}-card library includes"
        text = f"{lead} {', '.join(copies)} as potential removal."
    else:
        return None
    if len(text) > 600:
        example = ", ".join(copies[:2])
        if "direct_removal_total" in fact:
            text = f"{lead}, including {example}."
        else:
            text = f"{lead} {example} among its potential removal cards."
    if "direct_removal_total" in fact:
        # GRE membership proves a population, not a freshly randomized library.
        # Preserve that distinction when speaking the uniform-draw calculation.
        percent = f"{100 * total / size:.1f}".removesuffix(".0")
        odds = f" On a random draw, that’s about {percent} percent."
        if len(text) + len(odds) <= 600:
            text += odds
    return threat["instance_id"], text
