"""A narrow no-use proof for optional life paid to bring a land in untapped.

We only decline when the entering land would be the sole one-mana resource
until our next untap and every known cast/activation needs more. Anything that
could change that budget or create another benefit leaves the normal planner
in charge. This does not judge whether playing the land face was worthwhile.
"""

import re

from arenamcp.combat_keywords import has_combat_keyword, is_creature_now
from arenamcp.mana import get_local_seat_id
from arenamcp.rules_engine import _normalize_mana_symbols

_ENTRY = re.compile(
    r"as (?:this land|cardname|~|[^.\n]+) enters(?: the battlefield)?, you may pay (\d+) life\. "
    r"if you don't, it enters tapped\.\s*\{t\}: add \{([wubrgc])\}\.?",
    re.I,
)
_ALTERNATIVE = re.compile(
    r"\b(?:convoke|delve|improvise|affinity|foretell|suspend|plot|cycling|dash|evoke|emerge|ninjutsu|"
    r"mutate|overload|escape|flashback|disturb|morph|disguise|prototype|cleave|bestow|splice|miracle|"
    r"madness|surge|spectacle|prowl|offering|freerunning|alternate cost|alternative cost)\b"
    r"|cost[^.]*less|without paying|rather than pay|instead of paying",
    re.I,
)
# These public effects can supply cards/resources, enable a sick mana creature,
# or make the immediate land/life state matter before the next normal untap.
_BOARD_UNKNOWN = re.compile(
    r"\buntap\w*|\bunspent\b|\b(?:lose|empty)\b[^.]*\bmana\b|cost[^.]*less"
    r"|\b(?:lands?|permanents?)\b[^.]*\b(?:become|are creatures)\b"
    r"|\b(?:have|has|gain|gains)\b[^.]*\bhaste\b"
    r"|\blandfall\b|\bwhen(?:ever)?\b[^.]*\b(?:land|permanent)\b[^.]*\benter"
    r"|\bat the beginning\b|\byou may pay\b|\byou may (?:cast|play|activate)\b|\bgraveyards?\b"
    r"|\bwhenever (?:you|a player|an opponent) (?:pay|pays|lose|loses)\b[^.]*\blife\b"
    r"|\blife total\b|\blost life\b|\b(?:if|as long as)\b[^.]*\blife\b"
    r"|\badditional\b[^.]*\bmana\b",
    re.I,
)


def _text(card):
    return re.sub(r"<[^>]*>", "", _normalize_mana_symbols(str(card.get("oracle_text") or ""))).replace(
        "’", "'"
    )


def _mana_cost(cost):
    """An exact fixed cost, excluding hybrid/Phyrexian/X and malformed symbols."""
    symbols = re.findall(r"\{([^}]+)\}", _normalize_mana_symbols(str(cost or "")))
    if not symbols or any(not (s.isdigit() or s.upper() in set("WUBRGC")) for s in symbols):
        return None
    return sum(int(s) if s.isdigit() else 1 for s in symbols)


def _activated_costs(text):
    """One cost per colon, ignoring quoted abilities granted to another object."""
    text = re.sub(r'"[^"\n]*"', "", text)
    result = []
    for line in re.split(r"[.\n]", text):
        if ":" not in line:
            continue
        if line.count(":") != 1:
            return None
        cost, effect = line.split(":", 1)
        # Activated keywords such as equip are not colon-form costs; these are
        # deliberately left unknown by the caller rather than priced as zero.
        if re.search(r"\b(?:when|whenever|if)\b", cost, re.I):
            return None
        result.append((cost, effect))
    return result


def _all_activations_unavailable(card, *, battlefield, turn):
    text = _text(card)
    if _ALTERNATIVE.search(text) or re.search(
        r"\b(?:equip|reconfigure|level up|craft|boast|transmute)\b", text, re.I
    ):
        return False
    abilities = _activated_costs(text)
    if abilities is None:
        return False
    for cost, _effect in abilities:
        tapped_cost = "{t}" in cost.lower()
        sick = (
            is_creature_now(card)
            and (card.get("summoning_sickness") or card.get("turn_entered_battlefield") == turn)
            and not has_combat_keyword(card, "haste")
        )
        if battlefield and tapped_cost and (card.get("is_tapped") or sick):
            continue
        price = _mana_cost(cost)
        if price is None or price <= 1:
            return False
    return True


def _land_face(prompt_card, state):
    """Exact prompt identity/face, including entering objects not yet in a zone."""
    if prompt_card.get("type_line") and prompt_card.get("oracle_text"):
        return prompt_card
    facts = {key: value for key, value in prompt_card.items() if value not in (None, "")}
    iid, gid = prompt_card.get("instance_id"), prompt_card.get("grp_id")
    for zone in ("battlefield", "hand"):
        for card in state.get(zone) or []:
            if iid and card.get("instance_id") == iid:
                return {**card, **facts}
            for face in card.get("related_faces") or []:
                if gid and face.get("grp_id") == gid:
                    return {**face, **facts}
    # Optional prompts can precede zone entry. Use only the already-initialized
    # local database; _local_card excludes network-backed Scryfall sources.
    if gid:
        try:
            gid = int(gid)
        except (TypeError, ValueError):
            return {}
        from arenamcp import card_db
        from arenamcp.match_context import _local_card

        if card_db._card_db is not None:
            return {**_local_card(int(gid), 0), **facts}
    return {}


def unused_entry_life_payment(decision, state, prompt_cards):
    """Reason to decline, or '' when any concrete benefit/budget is unproved."""
    if decision.request_type != "OptionalAction" or decision.find("optional:decline") is None:
        return ""
    accept = decision.find("optional:accept")
    if accept is None or len(decision.request_id) != 2 or not all(decision.request_id):
        return ""
    local = get_local_seat_id(state)
    turn = state.get("turn") or {}
    if (
        local is None
        or turn.get("active_player") != local
        or str(turn.get("phase") or "").lower().removeprefix("phase_") not in ("main1", "main2")
        or state.get("stack")
        or state.get("hand_complete") is not True
        or state.get("hand_count") != len(state.get("hand") or [])
    ):
        return ""
    player = next((p for p in state.get("players") or [] if p.get("seat_id") == local), {})
    if player.get("mana_pool") != {} or state.get("floating_mana") or state.get("mana_pool"):
        return ""
    if any(
        not isinstance(state.get(zone), list)
        for zone in ("hand", "battlefield", "graveyard", "exile", "command")
    ):
        return ""
    # More than one possible source/prompt card makes this proof ambiguous.
    if len(prompt_cards) != 1:
        return ""
    land = _land_face(prompt_cards[0], state)
    if (
        land.get("controller_seat_id") != local
        or str(land.get("type_line") or "").lower() != "land"
        or not land.get("instance_id")
    ):
        return ""
    entry = _ENTRY.fullmatch(_text(land).strip())
    if entry is None:
        return ""
    question = str(decision.prompt_text or accept.meta.get("promptText") or "")
    if not re.fullmatch(r"\s*pay " + entry[1] + r" life\?\s*", question, re.I):
        return ""
    battlefield = state.get("battlefield")
    if not isinstance(battlefield, list):
        return ""
    for card in battlefield:
        if card.get("instance_id") == land["instance_id"]:
            continue
        if card.get("is_phased_out"):
            return ""  # a phased-out source may return before the next untap
        text = _text(card)
        types = str(card.get("type_line") or "").lower()
        if (
            not types
            or card.get("controller_seat_id") is None
            or card.get("face_down")
            or (not text and "basic land" not in types)
        ):
            return ""
        # Its own entry has already happened (empty stack). Other unpriced
        # resource triggers can create a use before our next untap, including
        # attacks, damage, deaths and opposing creature entries.
        self_entry = re.compile(
            r"^when (?:this creature|cardname|~|" + re.escape(str(card.get("name") or "")) + r") enters?\b",
            re.I,
        )
        remaining_triggers = "\n".join(
            line for line in text.splitlines() if not self_entry.match(line.strip())
        )
        if re.search(
            r"\bwhen(?:ever)?\b[^.]*\b(?:draw|create|add|return|put|investigate|explore|discover|seek|learn|conjure|connive|manifest|venture)\b",
            remaining_triggers,
            re.I,
        ):
            return ""
        if _BOARD_UNKNOWN.search(text) or _ALTERNATIVE.search(text):
            return ""
        if card.get("controller_seat_id") != local:
            continue
        if "land" in types and not card.get("is_tapped"):
            return ""  # another ready land can combine with the new source
        if not _all_activations_unavailable(card, battlefield=True, turn=turn.get("turn_number")):
            return ""
    for card in [*(state.get("hand") or []), *(state.get("command") or [])]:
        text = _text(card)
        types = str(card.get("type_line") or "").lower()
        cost = card.get("modified_cost") if card.get("modified_cost") is not None else card.get("mana_cost")
        price = _mana_cost(cost)
        if (
            not types
            or not text
            or _ALTERNATIVE.search(text)
            or "land" in types
            or price is None
            or price <= 1
        ):
            return ""
        if not _all_activations_unavailable(card, battlefield=False, turn=turn.get("turn_number")):
            return ""
    for zone in ("graveyard", "exile"):
        for card in state.get(zone) or []:
            owner = card.get("owner_seat_id", card.get("controller_seat_id"))
            if owner is None:
                return ""
            if owner != local:
                continue
            # Alternative-zone permission/activations need more than the hand
            # proof; never dismiss a cheap escape/flashback or exiled offer.
            if (
                zone == "exile"
                or not card.get("type_line")
                or _ALTERNATIVE.search(_text(card))
                or re.search(r"\bcast\b|\bgraveyard\b", _text(card), re.I)
            ):
                return ""
            abilities = _activated_costs(_text(card))
            # A plain battlefield {T} ability has no permission to activate
            # from a graveyard. Other costs/keywords stay unpriced here.
            if abilities is None or any("{t}" not in cost.lower() for cost, _effect in abilities):
                return ""
    return (
        f"Decline paying {entry[1]} life for {land.get('name') or 'this land'}: its one mana has no "
        "usable known cast or activation before our next untap. Let it enter tapped; it still "
        "supplies next-turn mana without this life payment."
    )
