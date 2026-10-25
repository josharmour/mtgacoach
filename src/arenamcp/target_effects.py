"""Conservative target polarity shared by planning and automatic submission."""

import re

_QUANTITY = r"(?:(?:up to )?(?:one|two|three|four|five|six|seven|eight|nine|ten|x|\d+|that many)|any number of|another) "
# "Any target" is burn: 2026-10-06 18:56 (bug_20261006_185803) Stingerquill
# Charm's "deals 3 damage to any target" went unread, its other mode ("Target
# creature gains first strike and deathtouch") made the card read beneficial,
# and the autopilot redirected the 3 damage from the opponent's creature to ours.
# A player target can come first: "Target opponent sacrifices a creature" (Break
# Under Pressure, 2026-10-12 23:42, bug_20261012_234250) read as unclassified, so
# the model's correct pick of the opponent was cancelled three times. "Target
# player mills" stays unclassified: self-mill decks aim it at themselves.
_HARMFUL = re.compile(
    rf"\b(?:destroy|exile|sacrifice|sacrifices|counter|return|fight|fights) (?:{_QUANTITY})?target\b"
    rf"|\bdamage to (?:{_QUANTITY}|any )?target\b"
    r"|\bgets? [-−]|\bloses (?:all abilities|flying)\b"
    r"|\btarget [^.]*?(?:you don['’]t control|an opponent controls)"
    r"|\btarget opponent\b"
    r"|\btarget player (?:sacrifices|discards|loses \d+ life)\b"
    r"|\bcursed role token attached to\b"
)
# Role tokens carry no reminder text in Arena's rules text: Spellbook Vendor's
# "create a Sorcerer Role token attached to target creature you control" read
# as unclassified and the model's pick of our own creature was refused
# (2026-10-18 15:06:27 and 20:49:09, MANUAL REQUIRED). Every Role but the
# Cursed Role buffs the creature it enchants. The Grand Evolution's "distribute
# seven +1/+1 counters among ... target creatures you control" (20:23:51) and
# Badgermole Cub's "earthbend 1" (19:21:27) were refused the same way.
_BENEFICIAL = re.compile(
    r"\b(?:target|enchanted|equipped) [^.]*?\bgets? \+(?:\d+|x)/\+(?:\d+|x)"
    r"|\b(?:target|enchanted|equipped) [^.]*?\b(?:gains?|has|have) "
    r"(?:hexproof|indestructible|flying|lifelink|vigilance|trample|haste|first strike|double strike|protection)\b"
    r"|\b(?:put|distribute) [^.]*?\+1/\+1 counters? (?:on|among) [^.]*?\btarget\b"
    r"|\battach [^.]*?\bto target creature you control\b"
    r"|\bcreate an? (?!cursed\b)[a-z'’ -]*?\brole token attached to\b"
    r"|\bearthbend\b"
    r"|\benchanted [^.]*?\bits controller adds\b"
)
# Tapping, stunning or locking a target down hurts it. 2026-10-06 17:48
# (bug_20261006_174855): Seasoned Cryomancer's "tap up to that many target
# creatures and put a stun counter on each of them" was unclassified, so the
# opponent's Hortimancer pick was refused and the autopilot went MANUAL REQUIRED.
# Read with quoted (granted) abilities removed: "Equipped creature has
# '{T}: Tap target creature.'" describes the wearer, not this effect.
_HARMFUL_LOCK = re.compile(
    rf"\btap (?:{_QUANTITY})?target\b(?![^.;]*\byou control\b)"
    rf"|\bstun counters? on (?:{_QUANTITY})?target\b"
    r"|\b(?:target|enchanted) [^.;]*?\b(?:doesn['’]t|don['’]t) untap\b"
    r"|\b(?:target|enchanted) [^.;]*?\bcan['’]t (?:attack|block)\b"
)
# Untapping a target helps it, so "Tap target creature. / Untap target
# creature." modal text stays mixed (unclassified) as before.
_BENEFICIAL_UNTAP = re.compile(rf"(?<!\btap or )\buntap (?:{_QUANTITY})?target\b")
_QUOTED = re.compile(r"[\"“][^\"”]*[\"”]")
_BLINK = re.compile(r"\bexile[^.]*?\breturn (?:it|them|that card|those cards)\b[^.]*?to the battlefield")
# Getting a card back from your graveyard helps you; "return target ... to its
# owner's hand" is the bounce. 2026-10-06 18:57 (bug_20261006_185803):
# Theoretical Necromancer's "Return another target creature card from your
# graveyard to your hand" read as harmful, so every pick of our own card was
# cancelled and the activation was re-picked in a loop.
# A qualifier can sit between "card" and the zone: Rewrite Regrets' "return target
# creature or planeswalker card with mana value 6 or less from your graveyard"
# read as a bounce of our own card and was cancelled (2026-10-12 23:52,
# bug_20261012_235251).
_RECURSION = re.compile(
    rf"\b(?:return|put) (?:{_QUANTITY})?target [^.]*?\bcards?\b[^.]*?\bfrom (?:your|my) graveyard\b"
    rf"|\b(?:return|put) (?:{_QUANTITY})?target [^.]*?\bcards?\b[^.]*?\bfrom a graveyard\b[^.]*?\bunder your control\b"
)
# Exiling a card from your own graveyard spends it as a resource (copy it, cast
# it, feed a cost); it does not hurt the target, and the model's pick should
# stand. 2026-10-18 21:15: Uldaros Theorix's "exile up to one target nonland card
# of each card type from your graveyard. Copy those cards..." read as harmful,
# the model's picks were replaced with no targets, and the BepInEx bridge (which
# cannot submit an empty selection) went MANUAL REQUIRED. "Exile target card
# from a graveyard" (graveyard hate) stays harmful.
_OWN_YARD_EXILE = re.compile(
    rf"\bexile (?:{_QUANTITY})?target [^.]*?\bcards?\b[^.]*?\bfrom (?:your|my) graveyard\b"
)
_ATTACH_KEYWORD = re.compile(r"^\s*(?:equip|reconfigure)\b[^\n]*$", re.IGNORECASE)


def source_effect_text(oracle: str, parent_oracle: str = "") -> str:
    """Rules text that decides target polarity for a request's source.

    Arena's equip ability object carries only its keyword line ("Equip {o0}"),
    which names no effect, so Lightning Greaves' equip read as unclassified
    and was cancelled (2026-10-04 22:50). By rule it attaches the parent to a
    creature you control; the parent's own text says what that grants.
    """
    if _ATTACH_KEYWORD.match(re.sub(r"<[^>]*>", "", oracle or "").strip()):
        return f"Attach to target creature you control.\n{parent_oracle or ''}".strip()
    return oracle or ""


_ENCHANT_KEYWORD = re.compile(r"^\s*enchant\b[^\n.]*$", re.IGNORECASE)


def enchant_effect_text(ability_text: str, card_text: str) -> str:
    """What an Aura does to the permanent its cast targets, or "" for other abilities.

    An Aura spell's target comes from its Enchant keyword ("Enchant creature
    you control"), which names no effect; the card's other lines may target
    something else. Sheltered by Ghosts (2026-10-18 15:02:49) read as mixed
    because its enter trigger exiles an opponent's permanent, so the cast
    onto our own creature was refused and cancelled. Only the "enchanted"
    lines describe what happens to the Aura's own target.
    """
    line = re.sub(r"<[^>]*>", "", ability_text or "").strip()
    if not _ENCHANT_KEYWORD.match(line):
        return ""
    effect = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.\n])", re.sub(r"<[^>]*>", "", card_text or ""))
        if re.search(r"\benchanted\b", sentence, re.IGNORECASE)
    ]
    return "\n".join([line, *effect])


def names_a_target_you_control(oracle: str) -> bool:
    """The text restricts a target to your own side ("target creature you control")."""
    text = re.sub(r"<[^>]*>", "", (oracle or "").lower())
    return bool(re.search(r"\b(?:target|enchant) [^.;\n]*?(?<!don['’]t )\byou control\b", text))


def target_effect_is_harmful(oracle: str) -> bool | None:
    """True for harm, False for a known benefit, None for unknown/mixed effects."""
    harmful, beneficial = _polarity(oracle)
    if harmful == beneficial:
        return None
    return harmful


def target_effect_has_polarity(oracle: str) -> bool:
    """False when the text says nothing about helping or hurting its target ("Enchant creature")."""
    return any(_polarity(oracle))


def _polarity(oracle: str) -> tuple[bool, bool]:
    """(reads harmful, reads beneficial); both True for mixed text."""
    text = re.sub(r"<[^>]*>", "", oracle.lower())
    if re.search(r"target [^.]*?you control[^.]*?(?:deals?|fights?)\b[^.]*?target", text):
        return True, True
    harmful = beneficial = False
    for sentence in re.split(r"[.\n]", oracle.lower()):
        for helpful in (_BLINK, _RECURSION):
            if helpful.search(sentence):
                beneficial = True
                sentence = helpful.sub("", sentence)
        sentence = _OWN_YARD_EXILE.sub("", sentence)  # neutral: neither side
        harmful = harmful or bool(_HARMFUL.search(sentence))
        beneficial = beneficial or bool(_BENEFICIAL.search(sentence))
    for sentence in re.split(r"[.\n]", _QUOTED.sub("", text)):
        harmful = harmful or bool(_HARMFUL_LOCK.search(sentence))
        beneficial = beneficial or bool(_BENEFICIAL_UNTAP.search(sentence))
    return harmful, beneficial


# Wording that can hurt what an effect targets. Looser than _HARMFUL on
# purpose: it never classifies, it only vetoes moving a pick (or making a blind
# fallback pick) onto our own permanent when the text is not cleanly helpful.
_TARGET_HARM = re.compile(
    r"\b(?:damage|destroy|exile|sacrifice|return|fights?|stun)\b[^.]*?\b(?:any|targets?)\b"
    r"|\bcounter (?:up to |another )?target\b|\bshuffles? (?:it|them) into\b"
    r"|\b(?:target|enchanted)\b[^.]*?(?:\bgets? [-−]|[-−](?:\d+|x)/|\bloses\b|\bcan['’]t (?:attack|block)\b"
    r"|\b(?:doesn['’]t|don['’]t) untap\b|\bshuffles?\b)"
)
_ACTIVATION_COST = re.compile(r"^[^:\n]*:", re.MULTILINE)


def effect_mentions_blink(oracle: str) -> bool:
    """Exile followed by return to the battlefield needs contextual target intent."""
    text = _QUOTED.sub("", re.sub(r"<[^>]*>", "", (oracle or "").lower()))
    return bool(_BLINK.search(text))


def effect_mentions_harm(oracle: str) -> bool:
    """True when the text could hurt what it targets, costs and granted abilities aside."""
    text = _QUOTED.sub("", re.sub(r"<[^>]*>", "", (oracle or "").lower()))
    for sentence in re.split(r"[.\n]", _ACTIVATION_COST.sub("", text)):
        for helpful in (_BLINK, _RECURSION, _OWN_YARD_EXILE):
            sentence = helpful.sub("", sentence)
        if _TARGET_HARM.search(sentence):
            return True
    return False


# Wording that can help what an effect targets: the mirror of _TARGET_HARM. It
# never classifies; it only vetoes keeping a pick of an opponent's permanent
# (or player) when the effect's polarity is unknown and the text could buff it.
_TARGET_BENEFIT = re.compile(
    r"\+(?:\d+|x)/\+(?:\d+|x)|\+1/\+1 counters?"
    r"|\b(?:gains?|has|have)\b[^.]*?\b(?:hexproof|indestructible|shroud|flying|lifelink|vigilance|trample"
    r"|haste|first strike|double strike|protection|ward|menace|reach|deathtouch)\b"
    r"|\b(?:has|have|gains?) [\"“]"
    r"|\buntap\b|\brole token\b|\bearthbend\b|\bphases? out\b"
    r"|\bdraws?\b|\bgains? (?:\d+ |x )?life\b|\bits controller (?:adds|gains|draws|creates)\b"
)


def effect_mentions_benefit(oracle: str) -> bool:
    """True when the text could help what it targets (costs aside)."""
    text = re.sub(r"<[^>]*>", "", (oracle or "").lower())
    for sentence in re.split(r"[.\n]", _ACTIVATION_COST.sub("", text)):
        if _TARGET_BENEFIT.search(sentence):
            return True
    return False


# Arena's own verdict on each target (SelectTargetsReq target.highlight, shown
# in the client as green/red glows): Hot = a good target for the chooser, Cold
# = a bad one (removal on our own creature, a buff on theirs), Tepid = no
# opinion, ReplaceRole = the creature already wears our Role of that name and
# the new one would replace it (no gain; the client draws it Cold). Live
# 2026-10-18 (56 distinct target requests across both Player.logs): every
# harmful effect read opp:Hot/own:Cold, every buff own:Hot/opp:Cold, and the
# effects our text rules could not classify were Tepid.
GOOD_TARGET, NEUTRAL_TARGET, BAD_TARGET = "Hot", "Tepid", "Cold"
_BAD_HIGHLIGHTS = {"Cold", "ReplaceRole"}


# Wotc.Mtgo.Gre.External.Messaging.HighlightType, for bridges that report the number.
_HIGHLIGHT_NAMES = ("None", "Cold", "Tepid", "Hot", "Counterspell", "Random", "CopySpell", "ReplaceRole")


def target_highlight(value: object) -> str:
    """Normalized highlight ("Hot", "Cold", "Tepid", "ReplaceRole"), or "" when absent."""
    name = str(value or "").strip()
    if name.startswith("HighlightType_"):
        name = name[len("HighlightType_") :]
    if name.isdigit():
        name = _HIGHLIGHT_NAMES[int(name)] if int(name) < len(_HIGHLIGHT_NAMES) else ""
    return "" if name in ("", "None") else name


def highlight_is_bad(value: object) -> bool:
    return target_highlight(value) in _BAD_HIGHLIGHTS
