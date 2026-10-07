"""Conservative recognition of a creature's printed combat keywords."""

import re
from functools import lru_cache

_KEYWORDS = frozenset(
    {
        "deathtouch",
        "defender",
        "double strike",
        "first strike",
        "flying",
        "haste",
        "hexproof",
        "indestructible",
        "lifelink",
        "menace",
        "reach",
        "shroud",
        "trample",
        "vigilance",
    }
)


@lru_cache(maxsize=2048)
def printed_combat_keywords(oracle: str) -> frozenset[str]:
    """Read keyword lines, excluding grants, conditions, tokens, and reminder text.

    Text such as "another creature gains indestructible" does not make the
    source indestructible. Unmodeled continuous effects must not be invented
    from a keyword appearing somewhere in the source's rules text.
    """
    text = re.sub(r"<[^>]*>", "", oracle).lower()
    while re.search(r"\([^()]*\)", text):
        text = re.sub(r"\([^()]*\)", "", text)
    found = set()
    for line in re.split(r"[\n.;]", text):
        for phrase in re.split(r",|\band\b", line):
            keyword = phrase.strip()
            if keyword not in _KEYWORDS:
                break
            found.add(keyword)
    return frozenset(found)


# Arena's keyword ability grpIds, as GRE game objects list them in
# uniqueAbilities (printed and granted alike).
ABILITY_KEYWORDS = {
    1: "deathtouch",
    2: "defender",
    3: "double strike",
    6: "first strike",
    8: "flying",
    9: "haste",
    10: "hexproof",
    12: "lifelink",
    13: "reach",
    14: "trample",
    15: "vigilance",
    104: "indestructible",
    142: "menace",
}


def ability_keywords(ability_ids) -> list[str]:
    """The combat keywords among a game object's ability grpIds."""
    found = []
    for ability_id in ability_ids or []:
        keyword = ABILITY_KEYWORDS.get(ability_id)
        if keyword and keyword not in found:
            found.append(keyword)
    return found


def has_combat_keyword(card: dict, keyword: str) -> bool:
    """Printed keywords, plus keywords the game object currently has.

    2026-10-05: Titanbones wore an opponent's Medic's Kitesail. Its rules
    text says only reach, so the solver let 2/2s "chump" a 13-power flyer
    and sent both of our flyers into a lethal crackback.
    """
    keyword = keyword.lower()
    return keyword in (card.get("keywords") or ()) or keyword in printed_combat_keywords(
        card.get("oracle_text") or ""
    )


# --- "Can't be blocked" -------------------------------------------------------
#
# bug_20261006_184540: Tetsuko Umezawa, Fugitive ("Creatures you control with
# power or toughness 1 or less can't be blocked.") made Rank Rat, Yuriko,
# Theoretical Necromancer (4/1) and Tetsuko herself unblockable, yet the combat
# solver, the prompt's "If X blocks Y" lines and the losing-attack guard all
# treated them as blockable: T24 the guard held Yuriko back because "Cadet 2/2
# can block" it, and the T20 prompt computed "attack with nobody".
#
# Arena records the result as a Qualification annotation (QualificationType 32,
# CantBeBlocked) that the log parser does not keep yet, so this reads the grant
# from the visible board: rules text plus current power/toughness. Only
# unconditional text, or a condition the board can check, counts. "As long
# as ..." conditions, "this turn" effects and "except by two or more
# creatures" stay as rules text for the model to read.

_UNBLOCKABLE_TAIL = r"(?: gets [+-]\d+/[+-]\d+ and| has [a-z, ]+ and| can't block and)? can't be blocked\.?"
_GRANT = re.compile(
    r"^(?P<other>other )?creatures you control"
    r"(?: with (?P<stat>power or toughness|power|toughness) (?P<n>\d+) or (?P<dir>less|greater))?"
    r" can't be blocked\.?$"
)
_ATTACHED_GRANT = re.compile(r"^(?:enchanted|equipped) creature" + _UNBLOCKABLE_TAIL + "$")
_COLORS = ("white", "blue", "black", "red", "green")


@lru_cache(maxsize=2048)
def _text_lines(oracle: str) -> tuple[str, ...]:
    text = re.sub(r"<[^>]*>", "", oracle).lower().replace("’", "'")
    while re.search(r"\([^()]*\)", text):
        text = re.sub(r"\([^()]*\)", "", text)
    return tuple(line.strip() for line in text.splitlines() if line.strip())


def _rules_lines(card: dict) -> tuple[str, ...]:
    """Lower-case rules-text lines without markup or reminder text."""
    return _text_lines(str(card.get("oracle_text") or ""))


@lru_cache(maxsize=2048)
def _own_evasion(name: str, oracle: str) -> tuple[bool, tuple[tuple[bool, str], ...]]:
    """(unconditionally unblockable, ((is "except by", blocker descriptor), ...)) from own text."""
    names = {"this creature", "~", "cardname"}
    if name:
        names.update({name, name.split(",")[0].strip()})
    subject = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True) if n)
    unblockable = re.compile(rf"^(?:{subject}){_UNBLOCKABLE_TAIL}$")
    restriction = re.compile(rf"^(?:{subject}) can't be blocked (except )?by (.+?)\.?$")
    whole, partial = False, []
    for line in _text_lines(oracle):
        if unblockable.match(line):
            whole = True
            continue
        match = restriction.match(line)
        if match and not re.search(r"\bthis turn\b|\bas long as\b|\bif\b|\bmore\b", match.group(2)):
            partial.append((bool(match.group(1)), match.group(2)))
    return whole, tuple(partial)


def _evasion(card: dict) -> tuple[bool, tuple[tuple[bool, str], ...]]:
    name = str(card.get("modified_name") or card.get("name") or "").lower().strip()
    return _own_evasion(name, str(card.get("oracle_text") or ""))


def _int_stat(card: dict, key: str) -> int | None:
    value = card.get(key)
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_creature(card: dict) -> bool:
    kinds = " ".join(str(kind) for kind in card.get("card_types") or [])
    return "creature" in f"{kinds} {card.get('type_line') or ''}".lower()


def _controller(card: dict):
    return card.get("controller_seat_id") or card.get("owner_seat_id")


def own_text_unblockable(card: dict) -> bool:
    """The creature's own rules text says, unconditionally, that it can't be blocked."""
    return _evasion(card)[0]


def _granted_by(source: dict, creature: dict) -> bool:
    """`source`'s static ability makes `creature` (same controller) unblockable now."""
    for line in _rules_lines(source):
        match = _GRANT.match(line)
        if not match:
            continue
        if match.group("other") and source is creature:
            continue
        if not match.group("stat"):
            return True
        limit = int(match.group("n"))
        stats = [_int_stat(creature, key) for key in ("power", "toughness")]
        wanted = {"power": stats[:1], "toughness": stats[1:], "power or toughness": stats}[
            match.group("stat")
        ]
        if any(value is None for value in wanted):
            continue  # unknown stat: the condition can't be checked
        if any(value <= limit if match.group("dir") == "less" else value >= limit for value in wanted):
            return True
    return False


def annotate_cant_be_blocked(battlefield: list | None) -> dict[int, str]:
    """Mark creatures that can't be blocked right now; return {instance_id: why}.

    Sets ``card["cant_be_blocked"]`` to the source's name (or "its own rules
    text") and removes a stale mark, so it is safe to call again on the same
    board. Covers the creature's own unconditional text, "creatures you control
    [with power/toughness N or less] can't be blocked" from any permanent its
    controller controls (the creature itself included), and an attached Aura or
    Equipment that says "enchanted/equipped creature ... can't be blocked".
    """
    cards = [card for card in battlefield or [] if isinstance(card, dict)]
    found: dict[int, str] = {}
    for creature in cards:
        if not _is_creature(creature):
            continue
        identity = _int_stat(creature, "instance_id")
        why = "its own rules text" if own_text_unblockable(creature) else ""
        for source in cards:
            if why:
                break
            if source.get("is_phased_out"):
                continue
            if _controller(source) == _controller(creature) and _granted_by(source, creature):
                why = str(source.get("name") or "a permanent you control")
                continue
            attached_to = _int_stat(source, "attached_to_id")
            types = f"{source.get('type_line') or ''} {source.get('card_types') or ''}".lower()
            if attached_to is None and re.search(r"\b(?:aura|equipment)\b", types):
                attached_to = _int_stat(source, "parent_instance_id")
            if (
                identity is not None
                and attached_to == identity
                and any(_ATTACHED_GRANT.match(line) for line in _rules_lines(source))
            ):
                why = str(source.get("name") or "an attached permanent")
        if why:
            creature["cant_be_blocked"] = why
            if identity is not None:
                found[identity] = why
        else:
            creature.pop("cant_be_blocked", None)
    return found


def _blocker_matches(descriptor: str, blocker: dict) -> bool | None:
    """Whether a blocker fits "creatures with flying", "Walls", ...; None when unreadable."""
    source = blocker.get("_card") if isinstance(blocker.get("_card"), dict) else blocker
    match = re.fullmatch(r"creatures with power (\d+) or (less|greater)", descriptor)
    if match:
        power = _int_stat(blocker, "power")
        if power is None:
            return None
        limit = int(match.group(1))
        return power <= limit if match.group(2) == "less" else power >= limit
    match = re.fullmatch(r"creatures with ([a-z ]+)", descriptor)
    if match and match.group(1) in _KEYWORDS:
        return has_combat_keyword(blocker, match.group(1))
    match = re.fullmatch(rf"({'|'.join(_COLORS)}) creatures", descriptor)
    if match:
        colors = source.get("modified_colors") or source.get("colors")
        if not colors:
            return None
        return any(match.group(1) in str(color).lower() for color in colors)
    if descriptor == "artifact creatures":
        return "artifact" in f"{source.get('type_line') or ''} {source.get('card_types') or ''}".lower()
    if descriptor == "creature tokens":
        kind = f"{source.get('object_kind') or ''} {source.get('type_line') or ''}".lower()
        return "token" in kind or bool(blocker.get("is_token"))
    words = [word.strip() for word in re.split(r",? or |, ", descriptor) if word.strip()]
    if words and all(re.fullmatch(r"[a-z]+s", word) for word in words):
        subtypes = {str(kind).lower().removeprefix("subtype_") for kind in source.get("subtypes") or []}
        if not subtypes and source.get("type_line"):
            subtypes = set(str(source["type_line"]).lower().partition("—")[2].split())
        return any(word in (sub + "s", sub + "es") for word in words for sub in subtypes)
    return None


def can_be_blocked_by(attacker: dict, blocker: dict) -> bool:
    """False when the attacker's evasion stops this blocker; True otherwise.

    Reads the ``cant_be_blocked`` mark (see ``annotate_cant_be_blocked``), the
    attacker's own unconditional "can't be blocked", and its own static
    per-blocker restrictions ("can't be blocked by creatures with power 2 or
    greater", "... except by Spirits"). A restriction that can't be checked
    against this blocker allows the block.
    """
    if attacker.get("cant_be_blocked"):
        return False
    whole, restrictions = _evasion(attacker)
    if whole:
        return False
    for only_these, descriptor in restrictions:
        fits = _blocker_matches(descriptor, blocker)
        if fits is not None and fits != only_these:
            return False
    return True
