"""Conservative card-text roles and supported enabler/payoff links for Limited."""

import html
import re
from functools import lru_cache

# What each mechanic means in a spoken or prompted explanation.
MECHANIC_LABELS = {
    "scry": "scry",
    "surveil": "surveil",
    "draw": "card draw",
    "lifegain": "life gain",
    "plus_counters": "+1/+1 counters",
    "artifact_entry": "artifacts entering",
    "creature_entry": "creatures entering",
    "noncreature_spell": "noncreature spells",
    "instant_sorcery": "instants and sorceries",
    "jace": "a Jace",
    "planeswalker": "planeswalkers",
    "loyalty": "loyalty counters",
    "graveyard": "a full graveyard",
    "noncombat_damage": "noncombat damage",
    "dies": "creatures dying",
    "prepared": "creatures that enter prepared",
    "off_hand": "spells cast from outside the hand",
}

# Keyword actions whose effects live only in reminder text, which Arena's
# card database omits. FRA's "Empower Jace N" puts loyalty counters on a Jace
# token, first creating a blue Jace planeswalker token with "[−1]: Surveil 1"
# and "[−3]: Draw a card" if we have none. The token's surveil and draw are
# real but cost a loyalty activation, so they are indirect enablers.
_KEYWORD_EFFECTS = (
    (re.compile(r"\bempower jace\b"), {"jace", "planeswalker", "loyalty"}, {"surveil", "draw"}),
)

_SCRIED_THIS_TURN = re.compile(
    r"\b(?:as long as|if|only if) you(?:'ve| have) "
    r"(scried or surveilled|surveilled or scried|scried|surveilled) this turn\b"
)
_CAST_TRIGGER = re.compile(
    r"^whenever you cast (?:a |an |your first |your second )?"
    r"(noncreature|instant or sorcery|instant|sorcery) spell\b"
)
_GRAVEYARD_PAYOFF = re.compile(
    r"\bthreshold\b|\b(?:seven|\d+) or more cards in your graveyard\b"
    r"|\bfor (?:each|every) (?:\w+ )?cards? in your graveyard\b"
    r"|\bnumber of cards in your graveyard\b"
    # Reanimation wants something worth returning already in the graveyard.
    r"|\breturn (?:up to one )?target [^.]*\bcard from your graveyard to the battlefield\b"
)
_PLANESWALKER_PAYOFF = re.compile(
    r"\b(?:unless|if|as long as) you control a planeswalker\b|^planeswalkers you control\b"
)
# Life-gained-this-turn/game conditions pay off life gain as much as "whenever you
# gain life" does (FRA Alchemy: Flourishing Forces, Germinate Recruits, Valgavoth).
_LIFE_GAINED_PAYOFF = re.compile(
    r"\blife you(?:'ve| have)? gained this (?:turn|game)\b"
    r"|\b(?:if|as long as) you(?:'ve| have)? gained life this turn\b"
)
_LIFELINK = re.compile(r"^lifelink\b|\b(?:has|have|gains?) lifelink\b")
# Re-using a prepared creature: making it prepared again (Hexhaven Dueling Arena) or
# returning it to hand to recast (Rescue Girl). 2026-10-18 pro GW draft.
_PREPARED_PAYOFF = re.compile(
    r"\btarget creature (?:that attacked this turn )?becomes prepared\b"
    r"|\breturn another target (?:permanent|creature|nonland permanent) you control to its owner's hand\b"
)
_JACE_PAYOFF = re.compile(r"\bbehold a jace\b|\byou control a jace\b|\bjace planeswalkers? you control\b")
# FRA Rakdos: pingers enable "an opponent was dealt noncombat damage" payoffs.
_PING = re.compile(r"\bdeals? (?:\d+|x) damage to (?:each|target) (?:opponent|player)\b")
_PING_PAYOFF = re.compile(r"\bopponents? (?:is|was|has been|have been) dealt noncombat damage\b")
# Sacrifice outlets enable death triggers.
_SAC_OUTLET = re.compile(r"\bsacrifice (?:a|another|one) (?:other )?(?:creature|nontoken creature)\b")
_DIES_PAYOFF = re.compile(
    r"^whenever (?:another |a |one or more )?(?:other )?(?:nontoken )?creatures?(?: or planeswalkers?)?"
    r" you control (?:dies|die)\b"
)
# Mana that "can't be spent to cast spells from your hand" (FRA Heartwood Crafter)
# still casts spells from anywhere else and pays activated abilities: a card cast
# from exile through its own ability (Emrakul, the Exigent Doom), flashback, a
# card cast from the graveyard, and the spell a prepared creature carries.
_OFF_HAND_MANA = re.compile(r"\bcan't be spent to cast spells from your hand\b")
_OFF_HAND_CAST = re.compile(
    r"\bfor as long as it remains exiled\b|\bflashback\b|\benters prepared\b"
    r"|\bcast (?:it|this card|that card|them) from (?:exile|your graveyard)\b"
)
_ARTIFACT_PAYOFF = re.compile(
    r"\bnumber of artifacts you control\b|\bfor each artifact you control\b"
    r"|\bsacrifice another artifact\b|\bartifact tokens would be created\b"
)


def rules_profile(card: dict) -> dict:
    return _rules_profile(str(card.get("type_line") or ""), str(card.get("oracle_text") or ""))


@lru_cache(maxsize=4096)
def _rules_profile(type_line: str, oracle_text: str) -> dict:
    text = html.unescape(re.sub(r"<[^>]*>", "", oracle_text)).lower()
    # Reminder text varies by source (Scryfall prints it, Arena does not);
    # keyword effects come from _KEYWORD_EFFECTS so both read the same.
    text = re.sub(r"\([^)]*\)", "", text.replace("’", "'"))
    lines = list(dict.fromkeys(part.strip() for part in re.split(r"[.\n]", text) if part.strip()))
    enables, payoffs, indirect = set(), set(), set()
    kind = type_line.lower()
    front = kind.split("//")[0]
    body = "creature" in kind
    unconditional_body = body
    if body:
        enables.add("creature_entry")
    if "artifact" in kind:
        enables.add("artifact_entry")
    if front.strip() and "creature" not in front and "land" not in front:
        enables.add("noncreature_spell")
    if "instant" in front or "sorcery" in front:
        enables.add("instant_sorcery")
    if "planeswalker" in front:
        enables.update({"planeswalker", "loyalty"})
        if "jace" in front:
            enables.add("jace")
    for line in lines:
        trigger, effect = (
            (line.split(",", 1) + [""])[:2] if line.startswith(("when ", "whenever ", "at ")) else ("", line)
        )
        for mechanic, pattern in (
            ("scry", r"^whenever you (?:scry|surveil or scry)\b"),
            ("surveil", r"^whenever you (?:surveil|scry or surveil)\b"),
            ("draw", r"^whenever you draw a card$"),
            ("lifegain", r"^whenever you gain life$"),
            (
                "plus_counters",
                r"^whenever you put (?:one or more |a )?\+1/\+1 counters? on a creature(?: you control)?$",
            ),
            ("loyalty", r"^whenever you put (?:one or more |a )?loyalty counters? on a planeswalker"),
        ):
            if re.search(pattern, trigger):
                payoffs.add(mechanic)
        cast = _CAST_TRIGGER.search(trigger)
        if cast:
            payoffs.add("noncreature_spell" if cast.group(1) == "noncreature" else "instant_sorcery")
        if re.fullmatch(r"prowess(?:, prowess)*", line):
            payoffs.add("noncreature_spell")
        if re.match(r"noncreature spells you cast cost\b", line):
            payoffs.add("noncreature_spell")
        if re.match(r"instant and sorcery spells you cast cost\b", line):
            payoffs.add("instant_sorcery")
        scried = _SCRIED_THIS_TURN.search(line)
        if scried:
            # "This turn" conditions (Surveillance Phantasm attacking, Proctor
            # of Potential returning) are payoffs too, not only "whenever" triggers.
            if "scried" in scried.group(1):
                payoffs.add("scry")
            if "surveilled" in scried.group(1):
                payoffs.add("surveil")
        if _GRAVEYARD_PAYOFF.search(line):
            payoffs.add("graveyard")
        if _PLANESWALKER_PAYOFF.search(line):
            payoffs.add("planeswalker")
        if _JACE_PAYOFF.search(line):
            payoffs.add("jace")
        if _LIFE_GAINED_PAYOFF.search(line):
            payoffs.add("lifegain")
        if _LIFELINK.search(line):
            enables.add("lifegain")
        if _PING_PAYOFF.search(line):
            payoffs.add("noncombat_damage")
        if _PING.search(effect):
            enables.add("noncombat_damage")
        if _DIES_PAYOFF.search(trigger):
            payoffs.add("dies")
        if _SAC_OUTLET.search(line):
            enables.add("dies")
        if _ARTIFACT_PAYOFF.search(line):
            payoffs.add("artifact_entry")
        if _OFF_HAND_MANA.search(line):
            enables.add("off_hand")
        if _OFF_HAND_CAST.search(line):
            payoffs.add("off_hand")
        if re.search(r"\benters prepared\b", line):
            # Casting the prepared copy is casting its instant or sorcery.
            enables.update({"noncreature_spell", "instant_sorcery", "prepared"})
        if _PREPARED_PAYOFF.search(line):
            payoffs.add("prepared")
        for kind_name in ("artifact", "creature"):
            # Older Oracle wording ("... enters the battlefield under your
            # control") and current wording ("another creature you control
            # enters"). Color/type qualifiers stay excluded: a restricted
            # trigger is not generic support for every body.
            subject = r"whenever (?:an? |another |one or more )(?:other )?" + kind_name + r"s? "
            if re.fullmatch(
                subject + r"enters?(?: the battlefield)? under your control", trigger
            ) or re.fullmatch(subject + r"you control enters?(?: the battlefield)?", trigger):
                payoffs.add(kind_name + "_entry")
        for mechanic, pattern in (
            ("scry", r"\bscry \d"),
            ("surveil", r"\bsurveil \d"),
            ("draw", r"\bdraw (?:a|one|two|three|\d+) cards?"),
            ("lifegain", r"\bgain (?:\d+|x|one|two|three) life\b"),
            ("plus_counters", r"\bput\b[^.]*?\+1/\+1 counters?"),
            ("graveyard", r"\bsurveil \d|\bmills? (?:\d+|x|one|two|three|four|five|six) cards?"),
        ):
            if re.search(pattern, effect):
                enables.add(mechanic)
        for keyword, direct, via_token in _KEYWORD_EFFECTS:
            if keyword.search(effect):
                enables.update(direct)
                indirect.update(via_token)
        if re.search(r"\bcreate\b[^.]*?\b(?:artifact|treasure|food|clue|thopter|servo|heartwood)\b", effect):
            enables.add("artifact_entry")
        if re.search(r"\bcreate\b[^.]*?\bplaneswalker token\b", effect):
            enables.update({"planeswalker", "loyalty"})
        if re.search(r"\bcreate\b[^.]*?\bcreature tokens?\b", effect):
            enables.add("creature_entry")
            body = True
            if not trigger and not re.search(r"\bif\b|:", effect):
                unconditional_body = True
    return {
        "body": body,
        "unconditional_body": unconditional_body,
        "enables": sorted(enables),
        "payoffs": sorted(payoffs),
        # Enabled only through something the card creates (the Jace token's
        # loyalty abilities), so weaker than a direct enabler.
        "indirect": sorted(indirect - enables),
    }


def synergy_evidence(first: dict, second: dict) -> list[dict]:
    evidence = []
    for source, payoff in ((first, second), (second, first)):
        source_roles, payoff_roles = rules_profile(source), rules_profile(payoff)
        for mechanic in set(source_roles["enables"]) & set(payoff_roles["payoffs"]):
            evidence.append(
                {"source_id": source["grp_id"], "payoff_id": payoff["grp_id"], "mechanic": mechanic}
            )
        for mechanic in set(source_roles.get("indirect") or []) & set(payoff_roles["payoffs"]):
            evidence.append(
                {
                    "source_id": source["grp_id"],
                    "payoff_id": payoff["grp_id"],
                    "mechanic": mechanic,
                    "indirect": True,
                }
            )
    return sorted(evidence, key=lambda edge: (edge["source_id"], edge["payoff_id"], edge["mechanic"]))


def synergy_graph(cards: list[dict]) -> list[dict]:
    unique = list({card["grp_id"]: card for card in cards}.values())
    return [
        edge
        for index, first in enumerate(unique)
        for second in unique[index + 1 :]
        for edge in synergy_evidence(first, second)
    ]
