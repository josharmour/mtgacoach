"""Opponent strategy hypotheses from identified, opponent-owned public cards."""

from __future__ import annotations

from collections import OrderedDict
from threading import Lock

from arenamcp.card_facts.reviewed_notes import corpus, reviewed_print
from arenamcp.card_facts.source_identity import legacy_lookup_hash
from arenamcp.commentary.card_annotations import annotation_digest
from arenamcp.commentary.observations import card_identity_visible, generated_or_changed_print

_PUBLIC_ZONES = ("command", "battlefield", "stack", "graveyard", "exile")


def _public_cards(zones, opponent):
    for zone in _PUBLIC_ZONES:
        cards = zones.get(zone)
        if not isinstance(cards, (tuple, list)):
            continue
        for card in cards:
            if (
                isinstance(card, dict)
                and type(card.get("owner_seat_id")) is int
                and card["owner_seat_id"] == opponent
                and type(card.get("controller_seat_id")) is int
                and card["controller_seat_id"] == opponent
                and type(card.get("grp_id")) is int
                and card["grp_id"] > 0
                and card_identity_visible(card, zone)
                and card.get("object_kind") == "CARD"
                and not generated_or_changed_print(card)
            ):
                yield zone, card


def mark_public_native_prints(state):
    """Bind the final, public opponent snapshot to complete locally cached Arena prints."""
    from arenamcp.card_db import initialized_card_database

    local, zones = state.get("local_seat_id"), state.get("zones")
    if type(local) is not int or local not in (1, 2) or not isinstance(zones, dict):
        return
    cards = [card for _, card in _public_cards(zones, 3 - local)]
    for card in cards:
        card.pop("public_native_print", None)
    if not cards:
        return
    db = initialized_card_database()
    native = db.get_raw_mtgadb() if db is not None else None
    if native is None:
        return
    prints = native.cached_cards({card["grp_id"] for card in cards})
    for card in cards:
        gid = card["grp_id"]
        printed = prints.get(gid)
        if (
            printed is None
            or printed.grp_id != gid
            or printed.is_token
            or printed.linked_face_ids
            or not printed.oracle_text
            or "Basic Land" in printed.type_line
            or not all(
                card.get(k) == getattr(printed, k) for k in ("name", "type_line", "oracle_text", "mana_cost")
            )
        ):
            continue
        digest = legacy_lookup_hash(card)
        if digest:
            card["public_native_print"] = f"{gid}:{digest}"


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
            if fact["kind"] == "opponent_revealed_rules":
                lines.append(
                    "    What plan, if any, do these public sightings suggest? Cite the cards and keep any inference conditional."
                )
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
        for zone, card in _public_cards(zones, opponent):
            printed = reviewed_print(card)
            if printed:
                current[printed.name] = (seq, card["grp_id"], zone, None)
                continue
            receipt = card.get("public_native_print")
            if not isinstance(receipt, str):
                continue
            digest = legacy_lookup_hash(card)
            if digest and receipt == f"{card['grp_id']}:{digest}":
                from arenamcp.match_context import oracle_lines

                rules = " ".join(oracle_lines(card["oracle_text"]))
                if rules and len(rules) <= 620:
                    current[(card["grp_id"], digest)] = (seq, card["grp_id"], zone, (card["name"], rules))
        epoch = (match, game, local)
        with self._lock:
            seen = self._games.setdefault(epoch, {})
            for name, sighting in current.items():
                if name not in seen or sighting[0] < seen[name][0]:
                    seen[name] = sighting
            if len(seen) > 256:
                oldest = sorted(seen, key=lambda k: seen[k][0])[: len(seen) - 256]
                for key in oldest:
                    del seen[key]
            self._games.move_to_end(epoch)
            if len(self._games) > 4:
                self._games.popitem(last=False)
            evidence = {name: sight for name, sight in seen.items() if sight[0] <= seq}
        reviewed = {key: value for key, value in evidence.items() if value[3] is None}
        facts = _inferences(reviewed, opponent)
        # Ask about the newest public evidence, including a permanent that left
        # play. Observed rules are context for inference, never a scripted call.
        observed = sorted(
            ((key, value) for key, value in evidence.items() if value[3] is not None),
            key=lambda item: (-item[1][0], item[0]),
        )[:2]
        if observed:
            references, rules = [], []
            for (gid, digest), (when, _, zone, (name, text)) in observed:
                references.append(
                    dict(name=name, grp_id=gid, rules_hash=digest, observed_zone=zone, source_sequence=when)
                )
                rules.append(f"{name}: {text}")
            facts["state:opponent_read:observed:" + ":".join(str(ref["grp_id"]) for ref in references)] = {
                "kind": "opponent_revealed_rules",
                "source": "native_public_prints",
                "visibility": "public",
                "opponent_seat": opponent,
                "text": " ".join(rules),
                "evidence": references,
            }
        return facts


def _inferences(seen, opponent):
    if not seen:
        return {}
    _, records, _, approved = corpus()
    seen = {
        name: sight for name, sight in seen.items() if annotation_digest(records[name]) == approved.get(name)
    }
    observed_pairs, plans, possible_partners = {}, {}, {}

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
            links = observed_pairs if partner in seen else possible_partners
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
    # The booth and game plan have bounded space. Prefer two observed pieces
    # and a threat actually seen before suggesting an unseen partner.
    return observed_pairs | plans | possible_partners
