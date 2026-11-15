"""Typed fetch lands and their available mana sources during the London mulligan."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace

from arenamcp.card_facts.source_identity import single_face_source

LAND_PIPS = {"plains": "W", "island": "U", "swamp": "B", "mountain": "R", "forest": "G"}
TYPE = r"(plains|island|swamp|mountain|forest)"
FETCH = re.compile(
    rf"\{{o?t\}}, pay 1 life, sacrifice [^:\n]+: search your library for an? {TYPE} or {TYPE} card, "
    r"put it onto the battlefield, then shuffle\.",
    re.I,
)
SHOCK = re.compile(
    r"as (?:this land|[^.\n]+) enters(?: the battlefield)?, you may pay 2 life\. "
    r"if you (?:don't|do not), (?:it|[^.\n]+) enters(?: the battlefield)? tapped\.",
    re.I,
)


@dataclass(frozen=True)
class FetchTarget:
    grp_id: int
    name: str
    count: int
    colors: frozenset[str]
    entry_life: int | None  # Extra to use immediately; None means a previous land drop is needed.
    in_hand: tuple[int, ...]


def opening_cards(state, raw, cards):
    """Attach physical target choices to opening hand evaluations, using only submitted prints."""
    searches = [
        FETCH.fullmatch(str(c.get("oracle_text") or "").strip()) if h.is_land else None
        for c, h in zip(raw, cards, strict=True)
    ]
    if (
        not any(searches)
        or state.get("game_number") != 1
        or (state.get("turn") or {}).get("turn_number") != 0
    ):
        return cards
    if any(state.get(zone) for zone in ("battlefield", "graveyard", "exile", "stack")):
        return cards
    deck = state.get("deck_cards")
    if not isinstance(deck, list) or not deck or any(type(g) is not int or g <= 0 for g in deck):
        return cards
    # Only a complete opening hand can be subtracted from the submitted list.
    if len(raw) != 7 or any(type(c.get("grp_id")) is not int or c["grp_id"] <= 0 for c in raw):
        return cards
    ids = [c.get("instance_id") for c in raw]
    if any(type(i) is not int or i <= 0 for i in ids) or len(set(ids)) != len(ids):
        return cards
    outside = Counter(c["grp_id"] for c in raw)
    remaining = Counter(deck)
    if outside - remaining:
        return cards
    remaining.subtract(outside)
    for c in state.get("command") or []:
        if c.get("owner_seat_id") == state.get("local_seat_id"):
            remaining[c.get("grp_id")] -= 1

    from arenamcp.deck_profile import _resolve

    targets = []
    for grp, count in remaining.items():
        face = _resolve(grp)
        types, _, subtypes = str(face.get("type_line") or "").lower().partition("—")
        if "land" not in types.split() or "creature" in types.split() or not single_face_source(face):
            continue
        pips = frozenset(LAND_PIPS[t] for t in subtypes.split() if t in LAND_PIPS)
        if not pips:
            continue
        rules = str(face.get("oracle_text") or "").lower()
        shock = SHOCK.search(rules)
        # Recognize a known entry cost or delay, including the optional shock payment.
        extra = SHOCK.sub("", rules)
        tapped = re.search(
            r"(?:this land|"
            + re.escape(str(face.get("name") or "").lower())
            + r") enters(?: the battlefield)? tapped\.",
            extra,
        )
        if tapped:
            extra = extra[: tapped.start()] + extra[tapped.end() :]
        extra = re.sub(r"(?m)^cycling \{o?\d+\}(?: \([^\n]*\))?[.]?$", "", extra)
        # Only intrinsic/basic mana abilities and reminder text can accompany
        # a verified source. Lands with replacement effects or additional
        # restrictions need their own treatment before promising usable mana.
        extra = re.sub(r"\(?\{o?t\}: add \{o?[wubrg]\}(?:(?:, |,? or )\{o?[wubrg]\})*\.\)?", "", extra)
        if extra.strip() or (not rules.strip() and "basic" not in types.split()):
            continue
        held = tuple(c["instance_id"] for c in raw if c["grp_id"] == grp)
        if count > 0 or held:
            targets.append(
                FetchTarget(
                    grp, face.get("name") or "land", count, pips, 2 if shock else None if tapped else 0, held
                )
            )
    local = state.get("local_seat_id")
    life = next((p.get("life_total") for p in state.get("players") or [] if p.get("seat_id") == local), None)
    if type(life) is not int or life <= 0:
        return cards
    result = []
    for card, match, original in zip(cards, searches, raw, strict=True):
        if match:
            allowed = {LAND_PIPS[t.lower()] for t in match.groups()}
            choices = tuple(t for t in targets if t.colors & allowed and life > 1)
            card = _with_returned(replace(card, fetch_pool=choices, fetch_life=life), set())
        elif card.is_land:
            text = str(original.get("oracle_text") or "").lower()
            entry_life = 2 if SHOCK.search(text) else None if "tapped" in text and "enter" in text else 0
            card = replace(card, entry_life=entry_life)
        result.append(card)
    return result


def _with_returned(card, returned):
    choices = []
    for target in card.fetch_pool:
        count = target.count + sum(i in returned for i in target.in_hand)
        if count > 0:
            choices.append(replace(target, count=count))
    return replace(
        card, fetch_targets=tuple(choices), produces=frozenset().union(*(t.colors for t in choices))
    )


def after_bottom(kept, returned):
    if not returned or not any(card.fetch_pool for card in kept):
        return kept
    return [_with_returned(card, returned) if card.fetch_pool else card for card in kept]


def can_pay(pips, value, lands):
    """Assign both colored and generic payments to distinct lands and available target copies."""
    requirements = sorted(pips, key=len) + [None] * (value - len(pips))
    taken = Counter()

    life = next(land.fetch_life for land in lands if land.fetch_targets is not None)

    def assign(index, used, life_paid, cheapest_entry):
        if index == len(requirements):
            # All but the final land drop can untap before paying on curve.
            return not requirements or (cheapest_entry is not None and life_paid + cheapest_entry < life)
        need = requirements[index]
        for n, land in enumerate(lands):
            if n in used or (need and not land.produces & need):
                continue
            if land.fetch_targets is None:
                cheapest = _cheaper_entry(cheapest_entry, land.entry_life)
                if assign(index + 1, used | {n}, life_paid, cheapest):
                    return True
                continue
            for target in land.fetch_targets:
                if taken[target.grp_id] >= target.count or (need and not target.colors & need):
                    continue
                cost = life_paid + 1
                if cost >= life:
                    continue
                taken[target.grp_id] += 1
                paid = assign(index + 1, used | {n}, cost, _cheaper_entry(cheapest_entry, target.entry_life))
                taken[target.grp_id] -= 1
                if paid:
                    return True
        return False

    return assign(0, set(), 0, None)


def _cheaper_entry(a, b):
    if a is None:
        return b
    return a if b is None else min(a, b)
