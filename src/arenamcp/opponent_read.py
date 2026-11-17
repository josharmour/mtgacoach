"""Opponent strategy hypotheses from identified, opponent-owned public cards."""

from __future__ import annotations

import re
from collections import OrderedDict
from functools import lru_cache
from threading import Lock

from arenamcp.card_facts.reviewed_notes import corpus, reviewed_print
from arenamcp.card_facts.source_identity import legacy_lookup_hash
from arenamcp.commentary.card_annotations import annotation_digest
from arenamcp.commentary.observations import card_identity_visible, generated_or_changed_print

_PUBLIC_ZONES = ("command", "battlefield", "stack", "graveyard", "exile")


def _public_cards(zones, seat):
    for zone in _PUBLIC_ZONES:
        cards = zones.get(zone)
        if not isinstance(cards, (tuple, list)):
            continue
        for card in cards:
            if (
                isinstance(card, dict)
                and type(card.get("owner_seat_id")) is int
                and card["owner_seat_id"] == seat
                and type(card.get("controller_seat_id")) is int
                and card["controller_seat_id"] == seat
                and type(card.get("grp_id")) is int
                and card["grp_id"] > 0
                and card_identity_visible(card, zone)
                and card.get("object_kind") == "CARD"
                and not generated_or_changed_print(card)
            ):
                yield zone, card


def mark_public_native_prints(state):
    """Bind both players’ public cards to complete locally cached Arena prints."""
    from arenamcp.card_db import initialized_card_database

    local, zones = state.get("local_seat_id"), state.get("zones")
    if type(local) is not int or local not in (1, 2) or not isinstance(zones, dict):
        return
    cards = [card for seat in (local, 3 - local) for _, card in _public_cards(zones, seat)]
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


_MANA_ABILITY = re.compile(
    r"\(?\{o?t\}: add (?:\{o?[wubrgc]\}(?:(?:, |,? or )\{o?[wubrgc]\})*"
    r"|one mana of any color(?: in your commander's color identity)?)\.\)?",
    re.I,
)


_MANA_LAND_ENTRY = re.compile(
    r"this land enters(?: the battlefield)? tapped(?: unless you control (?:"
    r"(?:two or fewer|two or more) other lands|(?:a|an) (?:plains|island|swamp|mountain|forest)"
    r" or (?:a |an )?(?:plains|island|swamp|mountain|forest)))?\.",
    re.I,
)


@lru_cache(maxsize=512)
def _only_fixes_mana(type_line, rules):
    """Recognize complete mana-only land text. Other abilities keep their place in the read."""
    from arenamcp.opening_fetches import FETCH, SHOCK

    if str(type_line or "").lower().partition("—")[0].strip() not in {"land", "legendary land"}:
        return False
    if FETCH.fullmatch(rules):
        return True
    mana_text = _MANA_LAND_ENTRY.sub("", SHOCK.sub("", rules)).strip()
    return bool(
        mana_text and _MANA_ABILITY.search(mana_text) and not _MANA_ABILITY.sub("", mana_text).strip()
    )


class OpponentRead:
    """Keep first public sightings, scoped to a game and local seat.

    A late planning worker can read an older board. Only sightings at or before
    that board's sequence are evidence, even when a newer reveal arrived first.
    """

    def __init__(self):
        self._games = OrderedDict()
        self._printed_cards = OrderedDict()
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
        visible = list(facts.values())
        # The two most recent native prints can change the read even when four
        # older, curated hypotheses already exist. Keep the strongest reviewed
        # hypotheses and reserve one of the four slots for the new evidence.
        selected = visible[:4]
        newest = next((fact for fact in visible if fact["kind"] == "opponent_revealed_rules"), None)
        if newest is not None and newest not in selected:
            selected[-1] = newest
        for fact in selected:
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
                    meta = {
                        "name": card["name"],
                        "mana_cost": card.get("mana_cost") or "",
                        "type_line": card.get("type_line") or "",
                        "oracle_text": card.get("oracle_text") or "",
                    }
                    with self._lock:
                        self._printed_cards[(card["grp_id"], digest)] = meta
                        self._printed_cards.move_to_end((card["grp_id"], digest))
                        if len(self._printed_cards) > 512:
                            self._printed_cards.popitem(last=False)
                    current.setdefault(
                        (card["grp_id"], digest),
                        (
                            seq,
                            card["grp_id"],
                            zone,
                            (card["name"], rules, _only_fixes_mana(card.get("type_line"), rules)),
                        ),
                    )
        epoch = (match, game, local)
        with self._lock:
            seen, first_command = self._games.setdefault(epoch, ({}, {}))
            for name, sighting in current.items():
                if sighting[2] == "command" and sighting[3] is not None:
                    first_command[name] = min(seq, first_command.get(name, seq))
                if name not in seen or sighting[0] < seen[name][0]:
                    seen[name] = sighting
            if len(seen) > 256:
                oldest = sorted(seen, key=lambda k: seen[k][0])[: len(seen) - 256]
                for key in oldest:
                    del seen[key]
                    first_command.pop(key, None)
            self._games.move_to_end(epoch)
            if len(self._games) > 4:
                self._games.popitem(last=False)
            evidence = {name: sight for name, sight in seen.items() if sight[0] <= seq}
            command_times = {key: when for key, when in first_command.items() if when <= seq}
            ordered_printed = {
                key: dict(self._printed_cards[key])
                for key, value in evidence.items()
                if value[3] is not None and key in self._printed_cards
            }
        reviewed = {key: value for key, value in evidence.items() if value[3] is None}
        facts = _inferences(reviewed, opponent)
        # Ask about the newest public evidence, including a permanent that left
        # play. Observed rules are context for inference, never a scripted call.
        ordered = sorted(
            ((key, value) for key, value in evidence.items() if value[3] is not None),
            key=lambda item: (-item[1][0], item[0]),
        )
        # In Brawl the opening command-zone print can explain why two much
        # later spells belong together. Retain its observed rules, even after
        # the card moves. A sighting alone doesn't establish commander status.
        command = [row for row in ordered if row[0] in command_times]
        # A second real card can occupy the same command zone. Favor cards
        # actually visible in this snapshot before previously seen cards.
        command.sort(key=lambda row: not (row[0] in current and current[row[0]][2] == "command"))
        others = [row for row in ordered if row[0] not in command_times]
        # Always show the latest reveal. For the companion slot prefer a
        # strategic ability or spell over a second plain mana source.
        recent = others[:1]
        if len(others) > 1:
            recent.append(next((row for row in others[1:] if not row[1][3][2]), others[1]))
        selected = {key for key, _ in command[:2] + recent}
        observed = [row for row in ordered if row[0] in selected]
        facts.update(self._card_facts_inferences(ordered, evidence, opponent, ordered_printed))
        if observed:
            references, rules = [], []
            for (gid, digest), (when, _, zone, (name, text, _)) in observed:
                if (gid, digest) in command_times:
                    when, zone = command_times[gid, digest], "command"
                references.append(
                    dict(name=name, grp_id=gid, rules_hash=digest, observed_zone=zone, source_sequence=when)
                )
                zone_note = " (seen in opposing command zone)" if zone == "command" else ""
                rules.append(f"{name}{zone_note}: {text}")
            facts["state:opponent_read:observed:" + ":".join(str(ref["grp_id"]) for ref in references)] = {
                "kind": "opponent_revealed_rules",
                "source": "native_public_prints",
                "visibility": "public",
                "opponent_seat": opponent,
                "text": " ".join(rules),
                "evidence": references,
            }
        return facts

    @staticmethod
    def _card_facts_inferences(ordered, seen, opponent, printed_map=None):
        """Build evidence-backed inferences from cached CardFacts threat/relationship layers."""
        if not ordered:
            return {}
        try:
            from arenamcp.card_facts.client import facts_for
            from arenamcp.card_facts.prompt_render import candidate_fact_hashes, normalized_scryfall_card
        except Exception:
            return {}
        printed_map = printed_map or {}
        seen_names = {
            value[3][0] if value[3] is not None else key
            for key, value in seen.items()
            if isinstance(key, str) or value[3] is not None
        }
        # Reconstruct flat seen name -> metadata for public prints.
        by_name = {}
        query_cards = []
        norm_digest_by_key = {}
        for key, value in ordered:
            gid, digest = key
            when, _, zone, payload = value
            if payload is not None:
                by_name[payload[0]] = (when, gid, zone, digest, payload[1])
                meta = printed_map.get(key)
                if isinstance(meta, dict):
                    query_cards.append(meta)
                    norm = normalized_scryfall_card(meta)
                    if norm is not None:
                        query_cards.append(norm)
                        digests = candidate_fact_hashes(meta)
                        if len(digests) > 1:
                            norm_digest_by_key[key] = digests[1]
        seen_names |= set(by_name)
        if not query_cards:
            return {}
        try:
            records = facts_for(query_cards, memory_only=True)
        except Exception:
            return {}
        if not isinstance(records, dict) or not records:
            return {}
        inferences = {}
        for name in sorted(by_name):
            when, gid, zone, digest, _rules = by_name[name]
            norm_digest = norm_digest_by_key.get((gid, digest))
            rec = records.get(digest) or (records.get(norm_digest) if norm_digest else None)
            if not isinstance(rec, dict) or rec.get("rules_hash") not in {digest, norm_digest}:
                continue
            ev = [
                {
                    "name": name,
                    "rules_hash": digest,
                    "grp_id": gid,
                    "observed_zone": zone,
                    "source_sequence": when,
                }
            ]
            threat = rec.get("threat") or {}
            if isinstance(threat, dict):
                for field in ("plan", "answers"):
                    for i, item in enumerate((threat.get(field) or [])[:2]):
                        text = item.get("text", "") if isinstance(item, dict) else str(item or "")
                        if text.strip():
                            key = f"state:opponent_read:cf_{field}:{gid}:{i}"
                            inferences[key] = {
                                "kind": "opponent_plan_inference",
                                "source": "cached_public_card_facts",
                                "visibility": "public",
                                "opponent_seat": opponent,
                                "text": f"Inference from {name}: {text.strip()}",
                                "evidence": list(ev),
                            }
            rels = rec.get("relationships") or {}
            if isinstance(rels, dict):
                for link in (rels.get("links") or [])[:4]:
                    if not isinstance(link, dict):
                        continue
                    partner = str(link.get("card") or "").strip()
                    claim = link.get("claim")
                    claim_text = (
                        claim.get("text", "") if isinstance(claim, dict) else str(claim or "")
                    ).strip()
                    if not partner or not claim_text:
                        continue
                    key = f"state:opponent_read:cf_pair:{gid}:{partner.lower().replace(' ', '_')[:24]}"
                    if partner in seen_names:
                        prefix = (
                            f"Inference: Both {name} and {partner} have appeared publicly. "
                            f"Possible interaction: "
                        )
                    else:
                        prefix = (
                            f"Inference: {partner} is a possible unseen partner for {name}; "
                            f"no confirmed public sighting in this game. "
                        )
                    inferences[key] = {
                        "kind": "opponent_plan_inference",
                        "source": "cached_public_card_facts",
                        "visibility": "public",
                        "opponent_seat": opponent,
                        "text": prefix + claim_text,
                        "evidence": list(ev),
                    }
        return inferences

    def threat_classes(self, state: dict | None) -> list[str]:
        """Return active public opponent threat categories for G4 strategy switches.

        Derived purely from public opposing zone objects and card text/facts —
        never reads hidden hands or unknown opponent library cards.
        """
        if not isinstance(state, dict):
            return []
        local = state.get("local_seat_id")
        if type(local) is not int or local not in (1, 2):
            return []
        opponent = 3 - local
        zones = state.get("zones")
        if not isinstance(zones, dict):
            zones = state
        classes: set[str] = set()
        for zone, card in _public_cards(zones, opponent):
            type_line = str(card.get("type_line") or "").lower()
            oracle = str(card.get("oracle_text") or "").lower()
            kws = {str(k).lower() for k in (card.get("keywords") or ())}
            has_flying_kw = "flying" in kws or bool(re.search(r"(?:^|\n|, )flying\b", oracle))
            if has_flying_kw and "creature" in type_line:
                classes.add("flyers")
            if (
                zone == "graveyard"
                and any(
                    kw in oracle
                    for kw in ("flashback", "escape", "unearth", "disturb", "from your graveyard")
                )
            ) or any(kw in oracle for kw in ("return target", "from your graveyard", "reanimate", "mill")):
                classes.add("graveyard")
            if "counter target" in oracle:
                classes.add("counterspells")
            if "you win the game" in oracle or (
                any(kw in oracle for kw in ("search your library", "storm", "cascade", "discover"))
                and not re.search(
                    r"search your library for (?:a |up to \w+ )?(?:basic )?(?:land|plains|island|swamp|mountain|forest)",
                    oracle,
                )
            ):
                classes.add("fast_combo")
            if any(kw in oracle for kw in ("deals 3 damage", "deals 2 damage", "haste")) and (
                "instant" in type_line or "sorcery" in type_line or "creature" in type_line
            ):
                classes.add("aggro_burn")
        # Include opponent tokens on public battlefield when detecting wide boards.
        creature_count = sum(
            1
            for card in zones.get("battlefield") or ()
            if isinstance(card, dict)
            and (card.get("controller_seat_id") or card.get("owner_seat_id")) == opponent
            and not card.get("face_down")
            and "creature"
            in (
                " ".join([str(t) for t in card.get("card_types") or ()] + [str(card.get("type_line") or "")])
            ).lower()
        )
        if creature_count >= 4:
            classes.add("wide_board")
        return sorted(classes)


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
