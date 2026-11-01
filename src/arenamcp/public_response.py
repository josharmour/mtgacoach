"""One public discard-for-indestructible response, not hidden-card prediction.

Only a fixed-damage spell and an unconditional, zero-mana self-protection
activation are supported. Unknown availability is distinct from no response.
The caller owns adversarial choice and the ordinary spell/turn transition.
"""

import re
from dataclasses import dataclass

from arenamcp.sacrifice_continuation import UnsupportedContinuation


class UnsupportedResponse(UnsupportedContinuation):
    pass


def raw(card):
    return card.get("_card") or card


def text(card):
    card = raw(card)
    value = re.sub(r"<[^>]*>", "", str(card.get("oracle_text") or "")).replace("’", "'")
    name = str(card.get("name") or "")
    return re.sub(re.escape(name), "SELF", value, flags=re.I) if name else value


def relevant(card):
    return bool(re.search(r"discard[^\n]*:[^\n]*indestructible", text(card), re.I))


def fixed_burn(card):
    if not any(t in str(raw(card).get("type_line") or "").lower() for t in ("instant", "sorcery")):
        return None
    return re.fullmatch(
        r"(?:SELF|cardname|~) deals (?P<damage>\d+) damage to (?:any target|target creature)\."
        r"(?: You gain (?P<gain>\d+) life\.)?",
        " ".join(text(card).split()),
        re.I,
    )


@dataclass(frozen=True)
class DiscardProtection:
    target_id: int
    damage: int
    taps: bool


def response(model, source, target, *, current: bool, count: int = 1):
    """None means unrelated/unavailable; raise for relevant unsupported effects."""
    if target is None or not relevant(target):
        return None
    # Exile, bounce and toughness reduction do not care about indestructible.
    rules = text(source)
    if not re.search(r"\bdamage\b|\bdestroy\b", rules, re.I):
        return None
    # A snapshot count does not survive an intervening draw step. Even zero
    # now cannot prove a later-turn activation unavailable.
    if not current:
        raise UnsupportedResponse("public protection response: later-turn continuation unmodelled")
    if count != 1:
        raise UnsupportedResponse("public protection response: multi-spell continuation unmodelled")
    hand = model.their_hand
    if type(hand) is int and hand == 0:
        return None
    if type(hand) is not int or hand < 0:
        raise UnsupportedResponse("public protection response: opponent hand count unknown")
    if model.has_pending_stack or model.stack_nonempty or not model.our_turn:
        raise UnsupportedResponse(
            "public protection response: pending stack or opponent-turn window unmodelled"
        )
    if model.phase not in ("Phase_Main1", "Phase_Main2"):
        raise UnsupportedResponse("public protection response: non-main-phase window unmodelled")
    if type(raw(target).get("damage", 0)) is not int:
        raise UnsupportedResponse("public protection response: marked damage unknown")
    protectors = [c for c in model.battlefield if relevant(c)]
    if len(protectors) != 1:
        raise UnsupportedResponse("public protection response: multiple protection sources unmodelled")
    protection = re.search(
        r"(?:^|\n)Discard a card: (?:SELF|this creature|cardname|~) gains indestructible until end of turn\."
        r"(?P<tap>\s*Tap it\.)?(?=\s*(?:\n|$))",
        text(target),
        re.I,
    )
    burn = fixed_burn(source)
    if not protection or not burn:
        raise UnsupportedResponse(
            "public protection response: activation cost/restriction or spell effect unmodelled"
        )
    # These can change availability, payment, damage or its consequences. A
    # printed permission alone does not establish that it is currently usable.
    for card in model.battlefield:
        c = raw(card)
        if c.get("controller_seat_id") is None or c.get("face_down") or c.get("is_phased_out"):
            raise UnsupportedResponse("public protection response: battlefield identity incomplete")
        rules = text(card)
        # Only the search's fixed self-death token continuation is supported
        # here. Other public death/discard consequences can reverse the choice.
        death_lines = [
            line for line in rules.splitlines() if re.search(r"\b(?:when|whenever)\b.*\bdies?\b", line, re.I)
        ]
        if death_lines:
            from arenamcp.death_bodies import token_specs_on_death

            if (
                c.get("instance_id") != target["instance_id"]
                or len(death_lines) != 1
                or not token_specs_on_death(card)
            ):
                raise UnsupportedResponse("public protection response: death continuation unmodelled")
        if re.search(
            r"can't activate|cannot activate|loses all abilities|activated abilities[^.]*(?:can't|cost)|"
            r"(?:can't|cannot)[^.\n]*\bactivate\b|activate[^.\n]*\bonly\b|"
            r"activate only|only once (?:each|per) turn|"
            r"\b(?:instead|prevent|double|triple|infect|wither)\b|"
            r"\b(?:can't|cannot) (?:gain|lose)|"
            r"\b(?:when|whenever)\b[^.\n]*(?:discard|gain[^.\n]*life|deals?[^.\n]*damage|dealt[^.\n]*damage|cast)\b",
            rules,
            re.I,
        ) or c.get("removed_abilities"):
            raise UnsupportedResponse(
                "public protection response: activation lock, replacement or relevant trigger unmodelled"
            )
    return DiscardProtection(target["instance_id"], int(burn["damage"]), bool(protection["tap"]))


def protected_body(body, damage):
    """A scratch body; expire both the grant and marked damage at cleanup."""
    return {
        **body,
        "keywords": [*body.get("keywords", ()), "indestructible"],
        "_public_response_original": body,
        "_combat_damage": max(0, raw(body).get("damage", 0)) + damage,
        "_track_combat_damage": True,
    }


def expire(body):
    original = body.get("_public_response_original")
    if original is None:
        return body
    # Preserve permanent P/T/counter changes made by combat; remove only our
    # temporary keyword and damage, not a preexisting indestructible grant.
    result = {**body, "keywords": list(original.get("keywords", ())), "_combat_damage": 0}
    result.pop("_public_response_original", None)
    result.pop("_track_combat_damage", None)
    return result
