"""Opponent strategy hypotheses from identified, opponent-owned public cards."""

from __future__ import annotations

from collections import OrderedDict
from threading import Lock

from arenamcp.card_facts.reviewed_notes import corpus, reviewed_print
from arenamcp.commentary.card_annotations import annotation_digest
from arenamcp.commentary.observations import generated_or_changed_print

_PUBLIC_ZONES = ("command", "battlefield", "stack", "graveyard", "exile")


class OpponentRead:
    """Keep first public sightings, scoped to a game and local seat.

    A late planning worker can read an older board. Only sightings at or before
    that board's sequence are evidence, even when a newer reveal arrived first.
    """

    def __init__(self):
        self._games = OrderedDict()
        self._lock = Lock()

    def observation_facts(self, observation):
        return self._facts(
            observation.public_state,
            observation.epoch.match_id,
            observation.epoch.game_number,
            observation.source_sequence,
        )

    def plan_block(self, state):
        if not isinstance(state, dict):
            return ""
        facts = self._facts(
            state, state.get("match_id"), state.get("game_number"), state.get("commentary_source_sequence")
        )
        if not facts:
            return ""
        lines = [
            "  Opponent plan inferences from public cards. Use these possibilities when weighing which threats to answer."
        ]
        for fact in list(facts.values())[:4]:
            names = ", ".join(item["name"] for item in fact["evidence"])
            lines.append(f"    Evidence ({names}): {fact['text']}")
        return "\n".join(lines)

    def _facts(self, state, match, game, seq):
        local = state.get("local_seat_id")
        if (
            not isinstance(match, str)
            or not match.strip()
            or type(game) is not int
            or game < 1
            or type(local) is not int
            or local not in (1, 2)
            or type(seq) is not int
            or seq < 0
        ):
            return {}
        opponent = 3 - local
        zones = state.get("zones")
        if not isinstance(zones, dict):
            zones = state
        current = {}
        for zone in _PUBLIC_ZONES:
            cards = zones.get(zone)
            if not isinstance(cards, (tuple, list)):
                continue
            for card in cards:
                if not isinstance(card, dict) or (
                    type(card.get("owner_seat_id")) is not int
                    or card["owner_seat_id"] != opponent
                    or type(card.get("controller_seat_id")) is not int
                    or card["controller_seat_id"] != opponent
                    or card.get("visibility") not in (None, "public", "Public", "Visibility_Public")
                    or card.get("face_down")
                    or card.get("isFacedown")
                    or card.get("object_kind") != "CARD"
                    or generated_or_changed_print(card)
                ):
                    continue
                printed = reviewed_print(card)
                if printed is not None:
                    current.setdefault(printed.name, (seq, card["grp_id"], zone))
        epoch = (match, game, local)
        with self._lock:
            seen = self._games.setdefault(epoch, {})
            for name, sighting in current.items():
                if name not in seen or sighting[0] < seen[name][0]:
                    seen[name] = sighting
            self._games.move_to_end(epoch)
            if len(self._games) > 4:
                self._games.popitem(last=False)
            evidence = {name: sight for name, sight in seen.items() if sight[0] <= seq}
        return _inferences(evidence, opponent)


def _inferences(seen, opponent):
    if not seen:
        return {}
    _, records, _, approved = corpus()
    seen = {
        name: sight for name, sight in seen.items() if annotation_digest(records[name]) == approved.get(name)
    }
    links, plans = {}, {}

    def fact(text, names):
        return {
            "kind": "opponent_plan_inference",
            "source": "reviewed_public_card_facts",
            "visibility": "public",
            "opponent_seat": opponent,
            "text": text,
            "evidence": [
                {
                    "name": name,
                    "rules_hash": records[name]["rules_hash"],
                    "grp_id": seen[name][1],
                    "observed_zone": seen[name][2],
                    "source_sequence": seen[name][0],
                }
                for name in names
            ],
        }

    for name in sorted(seen):
        record = records[name]
        for link in record["relationships"]["links"]:
            partner = link["card"]
            if partner not in records or annotation_digest(records[partner]) != approved.get(partner):
                continue
            names = sorted((name, partner)) if partner in seen else [name]
            pair = sorted((record["rules_hash"][:12], records[partner]["rules_hash"][:12]))
            key = "state:opponent_read:pair:" + ":".join(pair)
            if key in links:
                continue
            if partner in seen:
                prefix = f"Both {name} and {partner} have appeared. A possible strategy is their interaction."
            else:
                prefix = (
                    f"{partner} is a possible partner for {name}. It has no confirmed sighting in this read."
                )
            links[key] = fact(f"Inference: {prefix} {link['claim']['text']}", names)
        for i, claim in enumerate(record["threat"]["plan"]):
            key = f"state:opponent_read:threat:{seen[name][1]}:{i}"
            plans[key] = fact(
                f"Inference from {name}: the opponent may build around this effect. {claim['text']}", [name]
            )
    return links | plans
