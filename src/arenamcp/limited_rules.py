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
    "artifact_presence": "artifact permanents to count or sacrifice",
    "artifact_tokens": "creating artifact tokens",
    "equipment_card": "Equipment cards to tutor or put onto the battlefield",
    "spell_recursion": "instant or sorcery cards to recur from the graveyard",
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
_ARTIFACT_PRESENCE_PAYOFF = re.compile(
    r"\bnumber of artifacts you control\b|\bfor each artifact you control\b"
    r"|\bsacrifice another artifact\b"
)
_EQUIPMENT_CARD_PAYOFF = re.compile(
    r"\bsearch your library for an equipment card(?=,|$)"
    r"|\bput an equipment card from your hand onto the battlefield\b"
)
_SPELL_RECURSION_PAYOFF = re.compile(
    r"\btarget instant or sorcery card in your graveyard gains flashback\b"
    r"|\breturn (?:up to one )?target instant or sorcery card from your graveyard to your hand\b"
)


def rules_profile(card: dict) -> dict:
    return _rules_profile(str(card.get("type_line") or ""), str(card.get("oracle_text") or ""))


def _plain_rules(oracle_text: str) -> str:
    text = html.unescape(re.sub(r"<[^>]*>", "", oracle_text)).lower().replace("’", "'")
    return re.sub(r"\([^)]*\)", "", text)


@lru_cache(maxsize=4096)
def _rules_profile(type_line: str, oracle_text: str) -> dict:
    # Reminder text varies by source (Scryfall prints it, Arena does not);
    # keyword effects come from _KEYWORD_EFFECTS so both read the same.
    text = _plain_rules(oracle_text)
    lines = list(dict.fromkeys(part.strip() for part in re.split(r"[.\n]", text) if part.strip()))
    enables, payoffs, indirect = set(), set(), set()
    kind = type_line.lower()
    front = kind.split("//")[0]
    body = "creature" in kind
    unconditional_body = body
    if body:
        enables.add("creature_entry")
    if "artifact" in kind:
        enables.update({"artifact_entry", "artifact_presence"})
    if "equipment" in front:
        enables.add("equipment_card")
    if front.strip() and "creature" not in front and "land" not in front:
        enables.add("noncreature_spell")
    if "instant" in front or "sorcery" in front:
        enables.update({"instant_sorcery", "spell_recursion"})
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
            ("exhaust", r"^whenever you activate an exhaust ability$"),
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
        if re.match(r"^exhaust\s*[—–-]\s*[^:]+:", line):
            enables.add("exhaust")
        # Having artifacts available to sacrifice/count is not an entry
        # trigger. Conflating these told the Cube advisor that Portable Hole
        # triggered Legion Extruder, whose actual synergy is sacrifice fodder.
        if _ARTIFACT_PRESENCE_PAYOFF.search(line):
            payoffs.add("artifact_presence")
        if re.search(r"\bartifact tokens would be created\b", line):
            payoffs.add("artifact_tokens")
        if _EQUIPMENT_CARD_PAYOFF.search(effect):
            payoffs.add("equipment_card")
        if _SPELL_RECURSION_PAYOFF.search(effect):
            payoffs.add("spell_recursion")
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
            enables.update({"artifact_entry", "artifact_presence", "artifact_tokens"})
        # Arena omits these keywords' reminder text. Each creates a creature
        # token and attaches this Equipment to it, supplying a body without
        # making the Equipment itself a creature or a creature spell.
        if re.fullmatch(r"living weapon|for mirrodin!", line):
            enables.add("creature_entry")
            body = unconditional_body = True
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


def _synergy_cautions(source: dict, payoff: dict, mechanic: str) -> list[dict]:
    """Known costs of using this source for a supported link, not pick bans.

    An artifact-count payoff does not consume its enabler. Nor does sacrificing
    a token made by a nonartifact card sacrifice that card. Keep the facts tied
    specifically to sacrificing the artifact card itself.
    """
    if mechanic != "artifact_presence":
        return []
    if "artifact" not in str(source.get("type_line") or "").lower().split("//")[0]:
        return []
    payoff_text = _plain_rules(str(payoff.get("oracle_text") or ""))
    if not re.search(r"\bsacrifice another artifact\b", payoff_text):
        return []
    source_text = _plain_rules(str(source.get("oracle_text") or ""))
    self_names = [r"this artifact", r"this permanent"]
    if name := str(source.get("name") or "").strip():
        self_names.append(re.escape(_plain_rules(name)))
    cautions = []
    if re.search(
        r"\bexile\b[^.\n]*\buntil (?:" + "|".join(self_names) + r") leaves the battlefield\b",
        source_text,
    ):
        cautions.append(
            {
                "code": "sacrifice_ends_exile_until_source_leaves",
                "kind": "rules_fact",
                "detail": "If this artifact itself is sacrificed while it still holds a permanent card "
                "in exile, the until-this-leaves exile ends and that card returns. Evaluate the returned "
                "permanent when weighing the sacrifice; exiled tokens do not return.",
            }
        )
    if re.search(r"(?:^|\n)(?:\{[^}\n]+\}[^:\n.]*|sacrifice this artifact)\s*:\s*add\b", source_text):
        cautions.append(
            {
                "code": "sacrifice_spends_mana_source",
                "kind": "opportunity_cost",
                "detail": "This artifact has an activated mana ability. Sacrificing it spends a mana "
                "source; compare its remaining mana uses with the sacrifice payoff. This does not "
                "make the sacrifice illegal or always incorrect.",
            }
        )
    return cautions


def synergy_evidence(first: dict, second: dict) -> list[dict]:
    evidence = []
    for source, payoff in ((first, second), (second, first)):
        source_roles, payoff_roles = rules_profile(source), rules_profile(payoff)
        for mechanic in set(source_roles["enables"]) & set(payoff_roles["payoffs"]):
            edge = {"source_id": source["grp_id"], "payoff_id": payoff["grp_id"], "mechanic": mechanic}
            if cautions := _synergy_cautions(source, payoff, mechanic):
                edge["cautions"] = cautions
            evidence.append(edge)
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
