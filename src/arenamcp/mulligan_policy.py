"""London-mulligan policy shared by the autopilot planner and the coach prompt.

2026-10-06, FRA traditional draft, match 3da54de9: both games began on five
cards. The typed-decision LLM mulliganed two clear six-card keeps (three lands
with Unsummon and Tam's Resistance; three lands with Living Library and Sphinx
of False Conclusions) because its prompt had no mulligan policy, no mulligan
count or resulting hand size, and no per-card costs ("two redundant 4-drops"
were five-drops with Basic landcycling {2}).

Arena always shows seven cards. After N mulligans you keep 7-N and put N on
the bottom, so every mulligan costs a card and the bar for a keep drops:
seven cards must be functional, six need a reasonable land count and a play,
five are kept unless unplayable, and four are always kept.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, replace
from itertools import combinations
from typing import Any

from arenamcp import opening_fetches

COLORS = "WUBRG"
_COLOR_NAMES = {
    "white": "W",
    "blue": "U",
    "black": "B",
    "red": "R",
    "green": "G",
    "w": "W",
    "u": "U",
    "b": "B",
    "r": "R",
    "g": "G",
}
_BASIC_TYPES = {"plains": "W", "island": "U", "swamp": "B", "mountain": "R", "forest": "G"}
# The i-th kept land's worth when choosing which cards to bottom.
_LAND_VALUES = (3.0, 3.0, 2.0, 1.0, 0.3, 0.0, 0.0)
# Keeping 6, the third land outweighs any spell (at most 4.0: a cheap castable rare), as the
# bottom prompt says ("3 when keeping 6"). Ledger #101 (match 18b1601a, 2026-10-19): the
# suggestion bottomed the third Mountain of Slickshot x2, Burst, Iroh's Demonstration and
# three Mountains, and the 20-land deck sat on three lands T5-T9.
_SIX_CARD_LAND_VALUES = (3.0, 3.0, 4.5, 1.0, 0.3, 0.0, 0.0)

MULLIGAN_POLICY = (
    "MULLIGAN POLICY (London): Arena shows 7 cards; after N mulligans you keep 7-N and bottom N. "
    "Each mulligan costs a card, so compare this hand with a random hand ONE CARD SMALLER, "
    "and lower the bar as the hand shrinks. "
    "Keeping 7: demand a functional hand: 3-4 lands with spells your lands can cast early keep; "
    "2 lands keep on the draw only when they can develop (a mana maker, a land finder, or two cheap "
    "permanents); 5 lands with castable spells is usually a keep, "
    "especially on the draw; mulligan 0-1 or 6-7 lands. "
    "Keeping 6: keep any 2-4 lander with a play castable by turn 3. "
    "Keeping 5: keep unless unplayable (0 lands, all lands, or 1 land with no cheap plays). "
    "Never go to 4 except for 0-land or near-all-land hands. "
    "Read each card's real mana value below; landcycling cards are extra land drops once you have "
    "the mana (count them as about half a land). Auras and protective tricks need their targets; "
    "count them as development only when this hand can deploy a compatible creature early. "
    "Do not mulligan a functional hand for a better curve or for one awkward color."
)


@dataclass(frozen=True)
class HandCard:
    instance_id: int
    name: str
    is_land: bool
    produces: frozenset[str]
    mana_value: int
    # Each colored pip as the set of colors that can pay it.
    pips: tuple[frozenset[str], ...]
    landcycling: int | None
    instant_speed: bool
    type_line: str
    mana_cost: str
    rarity: str
    # What the spell does, for ties the land/curve score leaves (see _role_value).
    role: float = 0.0
    needs_carrier: bool = False
    makes_carrier: bool = False
    # Q1 2026-11-08/09 (matches b55e160b, a4ca1d46): grows this hand's mana —
    # taps for mana without eating another hand card, or puts a land onto the
    # battlefield. Reactive burn does not qualify.
    makes_mana_or_finds_land: bool = False
    fetch_targets: tuple[opening_fetches.FetchTarget, ...] | None = None
    fetch_life: int = 0
    entry_life: int | None = 0
    fetch_pool: tuple[opening_fetches.FetchTarget, ...] = ()
    alt_costs: tuple[tuple[int, tuple[frozenset[str], ...]], ...] = ()
    fetches_basic: bool = False
    one_drop_needs_carrier: bool = False


# A spell that can kill or stop a creature: damage it can aim at one (or at
# each creature), destroy/exile, -N/-N, fight, or a counterspell.
_HITS_CREATURES = re.compile(
    r"\bdeals? (?:\d+|x|twice x) damage to (?:any target|each (?:other )?creature"
    r"|(?:up to \w+ )?(?:another |other )?target (?:[\w-]+ )*?(?:creature|planeswalker))"
    r"|\b(?:destroy|exile) (?:up to \w+ )?(?:another )?target (?:[\w-]+ )*?"
    r"(?:creature|permanent|planeswalker)\b"
    r"|\btarget creature[^.]*\bgets? [-−]\d+/[-−][1-9]"
    r"|\bfights?\b|\bcounter target\b"
)
# Burn that only reaches players ("deals 3 damage to each opponent").
_FACE_ONLY = re.compile(r"\bdeals? \d+ damage to (?:each opponent|target (?:player|opponent))\b")


def _role_value(text: str, type_line: str, instant_speed: bool) -> float:
    """Tie-break worth of a spell for bottoming: what it does, not what it costs.

    2026-10-19 09:30 (match 92c09c7a): keeping Shock or Boltwave both scored
    19.0, and list position bottomed Shock. Instant-speed removal (3) beats
    creatures and sorcery-speed removal (2), other spells (1.5) and burn that
    only hits players (1); a kicker that scales the effect adds 0.25 (Burst
    Lightning over Shock: 08:47, match 7efad315, the model bottomed a Burst).
    """
    types = type_line.lower()
    if "creature" in types or "planeswalker" in types:
        value = 2.0
    elif _HITS_CREATURES.search(text):
        value = 3.0 if instant_speed else 2.0
    elif _FACE_ONLY.search(text):
        value = 1.0
    else:
        value = 1.5
    if re.search(r"\b(?:multi)?kicker\b", text):
        value += 0.25
    return value


def _symbols(cost: str) -> list[str]:
    """Mana symbols; Arena may pack several in one brace ("{o1o(U/R)o(U/R)}")."""
    out = []
    for group in re.findall(r"\{([^}]*)\}", cost or ""):
        for token in re.split(r"o", group) if re.search(r"o", group) else [group]:
            token = token.strip().strip("()").upper()
            if token:
                out.append(token)
    return out


def _mana_value(cost: str) -> int:
    value = 0
    for symbol in _symbols(cost):
        if symbol.isdigit():
            value += int(symbol)
        elif symbol in ("X", "Y", "Z", "T", "Q"):
            continue
        elif "/" in symbol and symbol.split("/")[0].isdigit():
            value += int(symbol.split("/")[0])
        else:
            value += 1
    return value


def _pips(cost: str) -> tuple[frozenset[str], ...]:
    pips = []
    for symbol in _symbols(cost):
        parts = symbol.split("/")
        if "P" in parts or any(part.isdigit() for part in parts):
            continue  # Phyrexian or 2/W hybrid: payable without that color
        colors = frozenset(part for part in parts if part in COLORS)
        if colors:
            pips.append(colors)
    return tuple(pips)


def _clean(text: str) -> str:
    return re.sub(r"<[^>]*>", "", text or "")


# "If this land has a luck counter on it, instead add one mana of any color."
_COUNTER_MANA = re.compile(r"\bif [^.]*\bhas an? (\w+) counter on it, instead add [^.]*\.")

# Q1 2026-11-08/09 (matches b55e160b, a4ca1d46): a two-land keep needs a way to
# develop its mana, not just cheap reactive spells — those keeps won 38% vs 60%
# for comparable three-landers. A spell develops when it taps for mana itself
# without consuming another hand card (Chrome Mox exiles one), or puts a land
# onto the battlefield (ramp and land-fetch spells).
_TAPS_FOR_MANA = re.compile(r"\{o?t\}:\s*add\b|:\s*add\b", re.IGNORECASE)
_EATS_A_HAND_CARD = re.compile(r"exile [^.]*card from your hand")
_FINDS_A_LAND = re.compile(
    r"search your library[^.]*\bland\b|\bland\b[^.]*onto the battlefield", re.IGNORECASE
)


def _develops_mana(text: str) -> bool:
    return bool(
        (_TAPS_FOR_MANA.search(text) and not _EATS_A_HAND_CARD.search(text)) or _FINDS_A_LAND.search(text)
    )


def _is_permanent(card: HandCard) -> bool:
    """Stays on the battlefield: not an instant or sorcery."""
    return not re.search(r"\binstant\b|\bsorcery\b", card.type_line.lower())


_BASIC_FETCH_LAND = re.compile(
    r"(?:^|\n)\s*(?:\{o?t\}\s*,\s*)?(?:pay \d+ life\s*,\s*)?sacrifice [^:\n]*:\s*"
    r"search your library for a basic land card[^.\n]*onto the battlefield",
    re.IGNORECASE,
)

_OWN_CREATURE_TARGET_RE = re.compile(r"\btarget creature you control\b", re.IGNORECASE)
_PUMP_TRICK_RE = re.compile(
    r"\btarget creature\b[^.]*?(?:\bgets? \+|\bgains? "
    r"(?:hexproof|indestructible|flying|lifelink|vigilance|trample|haste|first strike|double strike|deathtouch|menace)\b"
    r"|\bput [^.]*\+1/\+1 counter)"
    r"|\bput [^.]*\+1/\+1 counters? (?:on|onto) [^.]*\btarget creature\b",
    re.IGNORECASE,
)


def _text_needs_carrier(text: str) -> bool:
    """True when spell rules text mandates a friendly or pumpable creature target."""
    from arenamcp.target_effects import has_mandatory_target_pattern, target_effect_is_harmful

    if has_mandatory_target_pattern(text, _OWN_CREATURE_TARGET_RE):
        return True
    stripped = re.sub(r"\([^()]*\)", " ", str(text or ""))
    return bool(
        has_mandatory_target_pattern(text, _PUMP_TRICK_RE) and target_effect_is_harmful(stripped) is False
    )


def _spell_needs_carrier(card: dict, types: str, text: str) -> bool:
    """True when an instant/sorcery spell requires a friendly or pumpable creature to do anything."""
    if "instant" not in types and "sorcery" not in types:
        return False
    from arenamcp.target_effects import resolves_to_creature

    if resolves_to_creature(card):
        return False
    name = str(card.get("name") or "")
    cost = str(card.get("mana_cost") or "")
    if "//" in name or " // " in cost:
        spell_faces = [
            face
            for face in (card.get("related_faces") or ())
            if isinstance(face, dict)
            and (
                "instant" in str(face.get("type_line") or "").lower()
                or "sorcery" in str(face.get("type_line") or "").lower()
            )
            and (resolves_to_creature(face) or str(face.get("oracle_text") or "").strip())
        ]
        if len(spell_faces) >= 2:
            return all(
                not resolves_to_creature(face) and _text_needs_carrier(str(face.get("oracle_text") or ""))
                for face in spell_faces
            )
    return _text_needs_carrier(text)


def _cast_costs(
    card: dict, cost: str, type_line: str
) -> tuple[int, tuple[frozenset[str], ...], tuple[tuple[int, tuple[frozenset[str], ...]], ...]]:
    """Return ``(primary_mv, primary_pips, alt_costs)`` from castable faces.

    Split/Room cards concatenate both halves' costs at top level in MTGA DB
    (e.g., ``{1}{R}{3}{U}{U}``); drop the concatenated cost when both faces are
    explicit (`` // `` in cost, or >=2 split/Room faces in ``related_faces``).
    Adventure/Omen and MDFC cards preserve the front face ``cost`` as primary
    while including alternate castable faces in ``alt_costs``. Empty-cost
    transforming back faces are ignored.
    """
    name = str(card.get("name") or "")
    types_lower = type_line.lower()
    faces = [f for f in (card.get("related_faces") or ()) if isinstance(f, dict)]

    if " // " in cost:
        slash_costs = [
            (_mana_value(p.strip()), _pips(p.strip())) for p in cost.split(" // ") if _symbols(p.strip())
        ]
        if slash_costs:
            deduped = tuple(dict.fromkeys(slash_costs))
            primary_mv, primary_pips = min(deduped, key=lambda item: (item[0], len(item[1])))
            return primary_mv, primary_pips, deduped

    primary_mv, primary_pips = _mana_value(cost), _pips(cost)
    is_adventure_or_omen = any(
        (
            "adventure" in str(f.get("type_line") or "").lower()
            or "omen" in str(f.get("type_line") or "").lower()
        )
        for f in faces
    )

    if not is_adventure_or_omen and ("//" in name or "room" in types_lower):
        split_names = {p.strip().lower() for p in re.split(r"\s*///?\s*", name) if p.strip()}
        face_costs: list[tuple[int, tuple[frozenset[str], ...]]] = []
        for face in faces:
            fc = str(face.get("mana_cost") or "")
            if not _symbols(fc):
                continue
            ftl = str(face.get("type_line") or "").lower()
            fname = str(face.get("name") or "").strip().lower()
            if "room" in types_lower or "room" in ftl or (split_names and fname in split_names):
                face_costs.append((_mana_value(fc), _pips(fc)))
        if len(face_costs) >= 2 or (
            "room" in types_lower and face_costs and primary_mv > max(mv for mv, _ in face_costs)
        ):
            deduped = tuple(dict.fromkeys(face_costs))
            split_mv, split_pips = min(deduped, key=lambda item: (item[0], len(item[1])))
            return split_mv, split_pips, deduped
        if face_costs:
            deduped = tuple(dict.fromkeys([(primary_mv, primary_pips), *face_costs]))
            return primary_mv, primary_pips, deduped if len(deduped) > 1 else ()

    alts: list[tuple[int, tuple[frozenset[str], ...]]] = [(primary_mv, primary_pips)]
    for face in faces:
        ftl = str(face.get("type_line") or "").lower()
        if "adventure" not in ftl and "omen" not in ftl:
            continue
        fc = str(face.get("mana_cost") or "")
        if _symbols(fc):
            alts.append((_mana_value(fc), _pips(fc)))
    deduped = tuple(dict.fromkeys(alts))
    return primary_mv, primary_pips, deduped if len(deduped) > 1 else ()


def _without_unmet_counter_mana(card: dict, text: str) -> tuple[str, bool]:
    """Rules text minus mana that needs a counter the card lacks, and whether any was cut.

    2026-11-08 21:26 (match b55e160b): Gemstone Caverns, played as a land drop with no
    luck counter, counted as a five-colour source. The line search's best line was
    "Gemstone Caverns + Chrome Mox + Delighted Halfling" with a Mountain as the only
    other land, the model played it "to cast Delighted Halfling now", and the Halfling
    stayed in hand.
    """
    held = {
        str(kind).lower().removeprefix("countertype_")
        for kind, count in (card.get("counters") or {}).items()
        if isinstance(count, (int, float)) and count > 0
    }
    cut = False

    def drop(match: re.Match) -> str:
        nonlocal cut
        if match.group(1) in held:
            return match.group(0)
        cut = True
        return ""

    return _COUNTER_MANA.sub(drop, text), cut


def _land_colors(card: dict) -> frozenset[str]:
    colors = set()
    text, conditional = _without_unmet_counter_mana(card, _clean(card.get("oracle_text") or "").lower())
    # Card data lists every colour the card can ever make; with its counter missing that overstates.
    for name in () if conditional else card.get("color_production") or []:
        color = _COLOR_NAMES.get(str(name).strip().lower())
        if color:
            colors.add(color)
    words = " ".join([str(card.get("type_line") or ""), *[str(s) for s in card.get("subtypes") or []]])
    for word in re.findall(r"[a-z]+", words.lower()):
        if word in _BASIC_TYPES:
            colors.add(_BASIC_TYPES[word])
    if re.search(r"mana of any (?:one )?colou?r|mana of the chosen colou?r|any colou?r", text):
        colors.update(COLORS)
    for clause in re.findall(r"\badd\b([^.]*)", text):
        for symbol in _symbols(clause.upper().replace("O", "o")):
            colors.update(part for part in symbol.split("/") if part in COLORS)
    return frozenset(colors)


def _rarity(card: dict) -> str:
    rarity = str(card.get("rarity") or "").lower()
    if rarity:
        return rarity
    try:  # only an already-initialized card DB: never load one inside a timed window
        from arenamcp import card_db

        db = card_db._card_db
        info = db.get_card_by_arena_id(int(card.get("grp_id") or 0)) if db is not None else None
        return str(getattr(info, "rarity", "") or "").lower()
    except Exception:
        return ""


def hand_card(card: dict) -> HandCard:
    type_line = str(card.get("type_line") or "")
    card_types = {str(t).lower() for t in card.get("card_types") or []}
    supertypes = re.split(r"\s[—-]\s", type_line.lower())[0]
    text = _clean(card.get("oracle_text") or "")
    cycling = re.search(
        r"\b(?:basic landcycling|landcycling|plainscycling|islandcycling|swampcycling|mountaincycling"
        r"|forestcycling|wastescycling)\s*(\{[^}]*\}(?:\{[^}]*\})*)",
        text,
        re.I,
    )
    flash = "flash" in (card.get("keywords") or ()) or re.search(r"(?mi)^\s*flash\b", text)
    is_land = "land" in card_types or bool(re.search(r"\bland\b", supertypes))
    types = f"{type_line} {' '.join(card_types)}".lower()
    cost = str(card.get("mana_cost") or "")
    from arenamcp.target_effects import (
        creature_aura_scope,
        enchant_effect_text,
        resolves_to_creature,
        target_effect_is_harmful,
    )

    instant_speed = "instant" in types or bool(flash)
    scope = creature_aura_scope(card)
    needs_carrier = (
        scope == "self"
        or (
            scope == "either"
            and target_effect_is_harmful(enchant_effect_text("Enchant creature", text)) is False
        )
        or (not is_land and _spell_needs_carrier(card, types, text))
    )
    fetches_basic = is_land and bool(_BASIC_FETCH_LAND.search(text))
    mana_value, pips, alt_costs = (0, (), ()) if is_land else _cast_costs(card, cost, type_line)
    one_drop_needs_carrier = False
    if not is_land and alt_costs and mana_value > 1:
        adv_faces = [
            face
            for face in (card.get("related_faces") or ())
            if isinstance(face, dict)
            and (
                "adventure" in str(face.get("type_line") or "").lower()
                or "omen" in str(face.get("type_line") or "").lower()
            )
            and _symbols(str(face.get("mana_cost") or ""))
            and _mana_value(str(face.get("mana_cost") or "")) <= 1
        ]
        if adv_faces and all(
            _spell_needs_carrier(
                face,
                str(face.get("type_line") or "").lower(),
                _clean(face.get("oracle_text") or ""),
            )
            for face in adv_faces
        ):
            one_drop_needs_carrier = True
    return HandCard(
        instance_id=int(card.get("instance_id") or 0),
        name=str(card.get("name") or "?"),
        is_land=is_land,
        produces=_land_colors(card) if is_land else frozenset(),
        mana_value=mana_value,
        pips=pips,
        landcycling=_mana_value(cycling.group(1)) if cycling else None,
        instant_speed=instant_speed,
        type_line=type_line,
        mana_cost=cost,
        rarity=_rarity(card),
        role=0.0 if is_land else _role_value(text.lower(), types, instant_speed),
        needs_carrier=needs_carrier,
        makes_carrier=resolves_to_creature(card),
        makes_mana_or_finds_land=False if is_land else _develops_mana(text),
        alt_costs=alt_costs,
        fetches_basic=fetches_basic,
        one_drop_needs_carrier=one_drop_needs_carrier,
    )


def _card_cost_options(card: HandCard) -> tuple[tuple[int, tuple[frozenset[str], ...]], ...]:
    """Castable (mana_value, pips) options for ``card``."""
    return card.alt_costs or ((card.mana_value, card.pips),)


def _mulligan_produces(land: Any) -> frozenset[str]:
    """Colors this land can provide in mulligan evaluation (including basic fetchlands)."""
    if getattr(land, "fetches_basic", False):
        return frozenset(COLORS)
    return land.produces


def _pip_matching(pips: tuple[frozenset[str], ...], lands: list[HandCard]) -> bool:
    """Distinct lands can pay every colored pip (one land per pip)."""
    order = sorted(pips, key=len)

    def assign(index: int, used: frozenset[int]) -> bool:
        if index == len(order):
            return True
        for position, land in enumerate(lands):
            if position not in used and _mulligan_produces(land) & order[index]:
                if assign(index + 1, used | {position}):
                    return True
        return False

    return assign(0, frozenset())


def colors_reachable(card: HandCard, lands: list[HandCard]) -> bool:
    """Every colored pip of at least one castable face has a source among these lands."""
    sources = frozenset().union(*(_mulligan_produces(land) for land in lands)) if lands else frozenset()
    return any(all(pip & sources for pip in opt_pips) for _mv, opt_pips in _card_cost_options(card))


def _cost_castable(mv: int, pips: tuple[frozenset[str], ...], lands: list[HandCard]) -> bool:
    if mv > len(lands):
        return False
    if any(getattr(land, "fetch_targets", None) is not None for land in lands):
        norm_lands = [
            replace(land, produces=frozenset(COLORS))
            if getattr(land, "fetches_basic", False) and getattr(land, "fetch_targets", None) is None
            else land
            for land in lands
        ]
        return opening_fetches.can_pay(pips, mv, norm_lands)
    return _pip_matching(pips, lands)


def castable_with(card: HandCard, lands: list[HandCard]) -> bool:
    """Castable using only these lands (land drops on curve, no draws)."""
    return any(_cost_castable(mv, pips, lands) for mv, pips in _card_cost_options(card))


def _card_value(card: HandCard, lands: list[HandCard], copies: int) -> float:
    count = len(lands)
    value = 1.0
    if card.mana_value <= 2:
        value += 0.5
    if castable_with(card, lands):
        value += 1.0
    elif card.mana_value <= count + 1 and colors_reachable(card, lands):
        value += 0.5
    if card.mana_value > 4:
        value -= 0.3 * (card.mana_value - 4)
    if not colors_reachable(card, lands):
        value -= 1.0
    elif not any(_pip_matching(opt_pips, lands) for _mv, opt_pips in _card_cost_options(card)):
        value -= 0.3
    if card.landcycling is not None:
        value = max(value, 1.5 if count >= 2 else 1.0)
    if card.rarity in ("rare", "mythic"):
        value += 1.5
    if copies and card.mana_value >= 4:
        value -= 0.5
    return value


def _keep_land_values(keep: int) -> tuple[float, ...]:
    """The kept lands' worths (``hand_score``) for an opening hand of ``keep`` cards."""
    return _SIX_CARD_LAND_VALUES if keep == 6 else _LAND_VALUES


def hand_score(kept: list[HandCard], land_values: tuple[float, ...] = _LAND_VALUES) -> float:
    lands = [card for card in kept if card.is_land]
    score = sum(land_values[index] for index in range(min(len(lands), len(land_values))))
    carrier = any(card.makes_carrier and card.mana_value <= 3 and castable_with(card, lands) for card in kept)
    seen: Counter = Counter()
    for card in kept:
        if card.is_land:
            continue
        value = _card_value(card, lands, seen[card.name])
        score += min(value, 1.0) if card.needs_carrier and not carrier else value
        seen[card.name] += 1
    return score


def best_keep(cards: list[HandCard], keep: int) -> tuple[list[HandCard], list[HandCard]]:
    """The keep-card subset with the best land count and curve; ties keep the better
    role (:func:`_role_value`), then cheaper cards, and only then the earlier ones."""
    keep = max(0, min(keep, len(cards)))
    best_key, best = None, None
    for chosen in combinations(range(len(cards)), keep):
        kept = [cards[i] for i in chosen]
        kept = opening_fetches.after_bottom(
            kept, {card.instance_id for i, card in enumerate(cards) if i not in chosen}
        )
        key = (
            round(hand_score(kept, _keep_land_values(keep)), 6),
            sum(card.role for card in kept),
            -sum(card.mana_value for card in kept),
            [-i for i in chosen],
        )
        if best_key is None or key > best_key:
            best_key, best = key, chosen
    kept = [cards[i] for i in best or ()]
    bottom = [card for i, card in enumerate(cards) if i not in set(best or ())]
    return opening_fetches.after_bottom(kept, {card.instance_id for card in bottom}), bottom


def dominated_bottom(state: dict, instance_ids: list[int], bottomed: list[int]) -> tuple[str, str] | None:
    """(bottomed spell, kept spell) when a bottomed spell does what a kept one does and more:
    the same kind of card (role class, speed, creature or not), at least as good on every count
    the policy has (land/curve value with the kept lands, role, mana value) and better on one.
    Keeping it instead is strictly better. None otherwise; trading one kind of card for
    another stays a judgement call.

    2026-10-19 08:47 (match 7efad315): the model bottomed a Burst Lightning and
    kept Shock, which Burst does everything of; within the score tolerance it
    stood.
    """
    by_id = {
        int(card.get("instance_id") or 0): card for card in state.get("hand") or [] if isinstance(card, dict)
    }
    if any(i not in by_id for i in instance_ids):
        return None
    out = set(bottomed)
    cards = {c.instance_id: c for c in _opening_cards(state, list(by_id.values()))}
    lands = opening_fetches.after_bottom(
        [card for i, card in cards.items() if card.is_land and i not in out], out
    )

    def worth(card: HandCard) -> tuple[float, float, int]:
        return (round(_card_value(card, lands, 0), 6), card.role, -card.mana_value)

    def kind(card: HandCard) -> tuple[int, bool, bool]:
        types = card.type_line.lower()
        return int(card.role), card.instant_speed, "creature" in types or "planeswalker" in types

    gone = [cards[i] for i in bottomed if i in cards and not cards[i].is_land]
    kept = [card for i, card in cards.items() if i not in out and not card.is_land]
    for bottom in gone:
        for keeper in kept:
            if bottom.name == keeper.name or kind(bottom) != kind(keeper):
                continue
            ours, theirs = worth(bottom), worth(keeper)
            if ours != theirs and all(a >= b for a, b in zip(ours, theirs, strict=True)):
                return bottom.name, keeper.name
    return None


@dataclass
class MulliganSituation:
    cards: list[HandCard]
    mulligans: int | None
    on_play: bool | None
    free_mulligans: int = 0
    exile_enabler: str = ""

    @property
    def bottom_count(self) -> int | None:
        if self.mulligans is None:
            return None
        return max(0, self.mulligans - max(0, self.free_mulligans))

    @property
    def keep_size(self) -> int | None:
        return None if self.bottom_count is None else max(0, len(self.cards) - self.bottom_count)


def local_seat(state: dict) -> int | None:
    seat = state.get("local_seat_id")
    if seat is None:
        seat = next((p.get("seat_id") for p in state.get("players") or [] if p.get("is_local")), None)
    return seat


def mulligans_from_state(state: dict) -> int | None:
    """Mulligans already taken this game, when the state carries them."""
    value = state.get("_mulligans_taken")
    if value is None:
        seat = local_seat(state)
        player = next((p for p in state.get("players") or [] if p.get("seat_id") == seat), {})
        value = player.get("mulligan_count")
    if value is None:
        # Log-only or bridge request path: MulliganReq's mulliganCount.
        ctx = state.get("decision_context") or {}
        value = ctx.get("mulligan_count")
        if value is None:
            value = ctx.get("mulliganCount")
    if value is None:
        value = (state.get("_bridge_request_payload") or {}).get("mulliganCount")
    if isinstance(value, bool):
        return None
    try:
        return max(0, int(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def free_mulligans_from_state(state: dict) -> int:
    """Use explicit game evidence; a format name does not grant free mulligans."""
    value = state.get("_free_mulligans")
    if value is None:
        ctx = state.get("decision_context") or {}
        value = ctx.get("free_mulligan_count")
        if value is None:
            value = ctx.get("freeMulliganCount")
    if value is None:
        value = (state.get("_bridge_request_payload") or {}).get("freeMulliganCount")
    if value is None:
        seat = local_seat(state)
        player = next((p for p in state.get("players") or [] if p.get("seat_id") == seat), {})
        value = player.get("free_mulligan_count")
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value)) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def on_the_play(state: dict) -> bool | None:
    """During mulligans the GRE's active player is the starting player."""
    seat = local_seat(state)
    active = (state.get("turn") or {}).get("active_player")
    if not seat or not active:
        return None
    return active == seat


_LAELIA_PAYOFF_NAMES = {
    "Etali's Favor",
    "Meteoric Mace",
    "Throes of Chaos",
    "Volcanic Torrent",
    "Daring Discovery",
    "Trumpeting Carnosaur",
    "Hidden Volcano",
    "Geological Appraiser",
    "A-Geological Appraiser",
}


def _laelia_enabler(state: dict, hand: list[dict]) -> str:
    """Recognize the land-heavy Laelia plan from the starting list and this hand."""
    from arenamcp.deck_strategy import commander_ids

    # Arena group 83081 is Laelia, the Blade Reforged.
    if 83081 not in commander_ids(state):
        return ""
    deck = state.get("deck_cards") or []
    mountains = [c for c in hand if c.get("name") == "Mountain"]
    if len(mountains) < 3:
        return ""
    # The opening snapshot can have names before it has Oracle text.
    from arenamcp.laelia_strategy import is_laelia_list

    if is_laelia_list(state):
        return next((c["name"] for c in hand if c.get("name") in _LAELIA_PAYOFF_NAMES), "")
    basic_ids = {c.get("grp_id") for c in mountains if c.get("grp_id")}
    if len(deck) < 60 or sum(gid in basic_ids for gid in deck) < len(deck) * 0.8:
        return ""
    for card in hand:
        if re.search(r"\b(?:cascade|discover \d+)\b", str(card.get("oracle_text") or ""), re.I):
            if colors_reachable(hand_card(card), [hand_card(land) for land in mountains]):
                return str(card.get("name") or "")
    return ""


def _opening_cards(state, hand):
    return opening_fetches.opening_cards(state, hand, [hand_card(card) for card in hand])


def situation(state: dict, mulligans: int | None = None) -> MulliganSituation:
    hand = [card for card in state.get("hand") or [] if isinstance(card, dict)]
    return MulliganSituation(
        cards=_opening_cards(state, hand),
        mulligans=mulligans if mulligans is not None else mulligans_from_state(state),
        on_play=on_the_play(state),
        free_mulligans=free_mulligans_from_state(state),
        exile_enabler=_laelia_enabler(state, hand),
    )


def _plays(kept: list[HandCard]) -> tuple[list[HandCard], list[HandCard], list[HandCard]]:
    lands = [card for card in kept if card.is_land]
    spells = [card for card in kept if not card.is_land]
    sources = frozenset().union(*(_mulligan_produces(land) for land in lands)) if lands else frozenset()
    carrier = any(
        card.makes_carrier
        and any(mv <= 3 and _cost_castable(mv, pips, lands) for mv, pips in _card_cost_options(card))
        for card in spells
    )
    spells = [card for card in spells if not card.needs_carrier or carrier]
    early = [
        card
        for card in spells
        if any(mv <= 3 and _cost_castable(mv, pips, lands) for mv, pips in _card_cost_options(card))
    ]
    cheap = [
        card
        for card in spells
        if any(mv <= 2 and all(pip & sources for pip in pips) for mv, pips in _card_cost_options(card))
    ]
    covered = [card for card in spells if colors_reachable(card, lands)]
    return early, cheap, covered


def _names(cards: list[HandCard]) -> str:
    return ", ".join(card.name for card in cards) or "none"


def mulligan_verdict(found: MulliganSituation) -> tuple[str, str] | None:
    """("keep" | "mulligan", reason) for clear cases, else None for the LLM.

    Unknown mulligan count only ever keeps: the keeps below are keeps at any
    hand size, while a forced mulligan needs to know what it leads to.
    """
    cards = found.cards
    if len(cards) != 7:
        return None
    lands = sum(card.is_land for card in cards)
    keep_size = found.keep_size
    where = {True: "on the play", False: "on the draw", None: "play/draw unknown"}[found.on_play]
    if keep_size is not None and keep_size <= 4:
        return "keep", f"keeping {keep_size}: never mulligan to {keep_size - 1}"
    if found.exile_enabler:
        return "keep", f"Mountains cast Laelia and {found.exile_enabler} supplies the exile payoff"
    kept, _bottom = best_keep(cards, keep_size or 7)
    kept_lands = [card for card in kept if card.is_land]
    early, cheap, covered = _plays(kept)
    landish = len(kept_lands) + 0.5 * sum(card.landcycling is not None for card in kept if not card.is_land)
    # The keep never names the bottom card: the bottom step decides it with
    # the whole hand in view. 2026-10-19 09:30 (match 92c09c7a): "(bottom
    # Shock)" here, from a tie broken by list position, and the bottom step's
    # model followed it "consistent with my earlier keep decision".

    if keep_size == 7:
        next_bottom = max(0, (found.mulligans or 0) + 1 - max(0, found.free_mulligans))
        next_keep = max(0, len(cards) - next_bottom)
        if lands <= 1:
            return "mulligan", f"{lands} land(s) in 7 cards; a {next_keep}-card hand is better"
        if lands >= 6:
            return "mulligan", f"{lands} lands in 7 cards; a {next_keep}-card hand is better"
    if keep_size == 6 and lands in (0, 7):
        return "mulligan", f"{lands} lands in the 7 shown: unkeepable even at 6"

    if (
        keep_size in (7, 6, None)
        and sum(card.needs_carrier for card in kept) >= 2
        and not any(card.makes_carrier for card in early)
    ):
        return None  # dependent payoffs need strategic evaluation, not a mana-only auto-keep
    if keep_size in (7, None):
        if 3 <= lands <= 4 and early and len(covered) >= 2:
            return "keep", f"{lands} lands with early plays ({_names(early)}) on 7, {where}"
        if lands == 2 and found.on_play is False and cheap:
            # Q1 2026-11-08/09 (matches b55e160b, a4ca1d46): reactive burn alone
            # stalls on two lands (logged keeps won 38% vs 60% for comparable
            # three-landers). Keep only hands that can develop: a spell that
            # makes mana or finds a land, or two cheap castable permanents.
            castable = [card for card in kept if not card.is_land and castable_with(card, kept_lands)]
            if (
                any(card.makes_mana_or_finds_land for card in castable)
                or sum(card.mana_value <= 2 and _is_permanent(card) for card in castable) >= 2
            ):
                return "keep", f"2 lands on the draw with cheap plays ({_names(cheap)})"
        return None
    if keep_size == 6:
        if 2 <= len(kept_lands) <= 4 and landish <= 4.5 and early:
            return (
                "keep",
                f"keeping 6 with {len(kept_lands)} lands and early plays ({_names(early)}); "
                "a random 5 is worse",
            )
        return None
    # keep_size == 5
    if lands == 0:
        return "mulligan", "0 lands: unplayable even at 5"
    if lands >= 6:
        return "mulligan", f"{lands} lands of 7: keeping 5 leaves at most one spell"
    if len(kept_lands) == 1 and not cheap:
        return "mulligan", f"1 land and no cheap plays among {_names(kept)}"
    return "keep", f"keeping 5 with {len(kept_lands)} land(s); a random 4 is worse"


def mulligan_fallback_choice(found: MulliganSituation) -> tuple[str, str]:
    """Fallback keep/mulligan verdict when ``mulligan_verdict`` left it to the model and the model failed."""
    if found.keep_size == 7 and len(found.cards) == 7:
        kept, _bottom = best_keep(found.cards, 7)
        kept_lands = [card for card in kept if card.is_land]
        early, _cheap, covered = _plays(kept)
        has_basic_fetch = any(land.fetches_basic for land in kept_lands)
        has_affordable_landcycler = any(
            card.landcycling is not None and card.landcycling <= len(kept_lands)
            for card in kept
            if not card.is_land
        )
        if not kept_lands or (
            not early and len(covered) <= 1 and not has_affordable_landcycler and not has_basic_fetch
        ):
            return "mulligan", "no early plays or color coverage in 7 cards; a 6-card hand is better"
    elif found.keep_size == 6 and len(found.cards) == 7:
        kept, _bottom = best_keep(found.cards, 6)
        kept_lands = [card for card in kept if card.is_land]
        if not kept_lands:
            return "mulligan", "0 lands in 6 cards; a 5-card hand is better"
        if len(kept_lands) == 1:
            has_one_drop = any(
                not card.is_land
                and not card.needs_carrier
                and not card.one_drop_needs_carrier
                and castable_with(card, kept_lands)
                for card in kept
            )
            has_basic_fetch = any(land.fetches_basic for land in kept_lands)
            has_affordable_landcycler = any(
                card.landcycling is not None and card.landcycling <= 1 for card in kept if not card.is_land
            )
            if not has_one_drop and not has_basic_fetch and not has_affordable_landcycler:
                return "mulligan", "1 land with no castable 1-drop in 6 cards; a 5-card hand is better"
    return "keep", "fallback keep"


def _turn_note(card: HandCard, lands: list[HandCard]) -> str:
    if castable_with(card, lands):
        return f"castable turn {max(1, card.mana_value)} with these lands"
    if not colors_reachable(card, lands):
        sources = frozenset().union(*(_mulligan_produces(land) for land in lands)) if lands else frozenset()
        missing = sorted(
            {color for _mv, opt_pips in _card_cost_options(card) for pip in opt_pips for color in pip}
            - sources
        )
        if any(land.fetch_targets is not None for land in lands):
            return "no verified opening route to " + "/".join(missing)
        return "needs " + "/".join(missing) + " mana this hand cannot make yet"
    if any(land.fetch_targets is not None for land in lands) and card.mana_value <= len(lands):
        return "no verified payment with distinct available land sources"
    if not any(_pip_matching(opt_pips, lands) for _mv, opt_pips in _card_cost_options(card)):
        return "needs more sources of its color than this hand has"
    return f"needs {card.mana_value - len(lands)} more land(s)"


def _cached_playbook_present(state: dict, coach=None, playbook_fn=None) -> bool:
    """True when a strategic playbook is already available for this deck.

    The playbook supersedes the static deck profile, so the profile's keep
    guide is only seeded before the first playbook arrives (playbook wins).

    Availability mirrors what the coach actually serves, not just the
    disposable on-disk cache file: a playbook the coach still holds in
    memory (or a live playbook source provides) stays valid after the cache
    file is cleared, and an unreadable cache file must not mask the profile.
    Without a coach or playbook source, fall back to the on-disk cache path.
    """
    try:
        if playbook_fn is not None:
            return playbook_fn() is not None
        from arenamcp.deck_strategy import deck_identity

        identity = deck_identity(state)
        if coach is not None and callable(getattr(coach, "_cached_playbook", None)):
            return coach._cached_playbook(identity, state) is not None
        from arenamcp.coach import CoachEngine

        path = CoachEngine._playbook_cache_path(identity)
        return bool(path and path.exists())
    except Exception:  # the guide is advisory
        return False


def _deck_keep_guide(state: dict, coach=None, playbook_fn=None) -> list[str]:
    """The deck profile's keep criteria, when the deck list can be profiled.

    Suppressed once a playbook is available for the deck: the playbook's own
    guidance wins and the static profile must not contradict it.

    Deferred import: deck_profile imports this module's helpers, so the
    dependency is resolved at call time to keep module load acyclic.
    """
    if _cached_playbook_present(state, coach=coach, playbook_fn=playbook_fn):
        return []
    try:
        from arenamcp.deck_profile import profile_for_state

        profile = profile_for_state(state)
    except Exception as error:  # the guide is advisory
        logging.getLogger(__name__).debug("deck keep guide unavailable: %s", error)
        return []
    if profile is None:
        return []
    return profile.keep_criteria_lines()


def describe(
    state: dict,
    mulligans: int | None = None,
    coach=None,
    playbook_fn=None,
) -> list[str]:
    """Prompt lines: hand size consequences, play/draw, and per-card castability.

    ``coach``/``playbook_fn`` let the caller declare where a saved playbook
    lives, so the static profile keep guide yields to it (playbook wins).
    """
    found = situation(state, mulligans)
    if not found.cards:
        return ["Waiting for hand..."]
    n = found.mulligans
    lines = []
    if n is None:
        lines.append(
            "MULLIGAN STATUS: London mulligan; mulligans taken this game unknown (if this is your first "
            "look, KEEP keeps all 7). Each mulligan bottoms one more card."
        )
    else:
        bottom = found.bottom_count or 0
        keep_size = max(0, len(found.cards) - bottom)
        next_bottom = max(0, n + 1 - max(0, found.free_mulligans))
        next_keep = max(0, len(found.cards) - next_bottom)
        free_note = f" ({found.free_mulligans} free)" if found.free_mulligans > 0 else ""
        lines.append(
            f"MULLIGAN STATUS: London mulligan; mulligans taken this game: {n}{free_note}. "
            f"KEEP → you keep {keep_size} of these {len(found.cards)}"
            + (f" and bottom {bottom}" if bottom else "")
            + f". MULLIGAN → you will see a new 7, keep {next_keep}"
            + (f" and bottom {next_bottom}." if next_bottom else " (free mulligan, bottom 0).")
        )
    lines.append(
        "You are "
        + {
            True: "ON THE PLAY (no draw on turn 1)",
            False: "ON THE DRAW (extra card turn 1)",
            None: "play/draw unknown",
        }[found.on_play]
        + "."
    )
    lands = [card for card in found.cards if card.is_land]
    sources = sorted({color for land in lands for color in land.produces}, key=COLORS.find)
    cyclers = [card for card in found.cards if card.landcycling is not None]
    lines.append(
        f"HAND: {len(lands)} lands (colors: {', '.join(sources) or 'none'}), "
        f"{len(found.cards) - len(lands)} spells; land-ish total {len(lands) + 0.5 * len(cyclers):g} "
        "(each landcycling card counts as half a land)."
    )
    lines.extend(_deck_keep_guide(state, coach=coach, playbook_fn=playbook_fn))
    early, _, _ = _plays(found.cards)
    dependent = [c for c in found.cards if c.needs_carrier]
    if dependent and not any(c.makes_carrier for c in early):
        lines.append(
            f"No early carrier in this hand for {_names(dependent)}. Compare a mulligan with its independent plays and any available commander."
        )
    for card in found.cards:
        if card.fetch_targets is not None:
            routes = []
            for t in card.fetch_targets:
                entry = {0: "untapped", 2: "tapped or pay 2 more life for untapped", None: "tapped"}[
                    t.entry_life
                ]
                routes.append(
                    f"{t.name} ({'/'.join(sorted(t.colors, key=COLORS.find))}, {t.count} in library, {entry})"
                )
            lines.append(
                f"  - {card.name}: pay 1 life to fetch; verified choices: "
                + (", ".join(routes) or "none verified")
                + ". Each physical target can be fetched only once."
            )
            continue
        if card.is_land:
            lines.append(
                f"  - {card.name}: land, taps for {'/'.join(sorted(card.produces, key=COLORS.find)) or '?'}"
            )
            continue
        tags = []
        if card.instant_speed:
            tags.append("instant speed")
        if card.instant_speed and card.mana_value <= 2 and not card.needs_carrier:
            tags.append("cheap interaction")
        if card.landcycling is not None:
            tags.append(
                f"landcycling {{{card.landcycling}}}: fetches a land once you have {card.landcycling} mana"
            )
        if card.rarity in ("rare", "mythic"):
            tags.append(card.rarity)
        lines.append(
            f"  - {card.name} {card.mana_cost or '(no cost)'}: {card.type_line or 'spell'}, "
            f"mana value {card.mana_value}, {_turn_note(card, lands)}"
            + (f" [{'; '.join(tags)}]" if tags else "")
        )
    if found.bottom_count:
        kept, bottom = best_keep(found.cards, max(0, len(found.cards) - found.bottom_count))
        lines.append(
            f"If you keep: best {len(kept)} by lands and curve keeps {_names(kept)}; bottom {_names(bottom)}."
        )
    return lines


def laelia_bottom_choice(state: dict, instance_ids: list[int], count: int, offered_names=None) -> list[int]:
    if not 0 < count < len(instance_ids):
        return []
    hand = state.get("hand") or []
    names = {card.get("instance_id"): card.get("name") for card in hand}
    names.update({iid: name for iid, name in (offered_names or {}).items() if name})
    if not _laelia_enabler(state, hand):
        from arenamcp.laelia_strategy import is_laelia_list

        if not is_laelia_list(state):
            return []
        # Every non-Mountain card in this exact starting library is an enabler.
        # Unknown or incomplete group data cannot prove a payoff.
        if not any(names.get(i) in _LAELIA_PAYOFF_NAMES for i in instance_ids):
            return []
    eligible = [i for i in instance_ids if names.get(i) == "Mountain"]
    return eligible[-count:] if len(eligible) - count >= 2 else []


def bottom_choice(state: dict, instance_ids: list[int], count: int) -> list[int]:
    """Instance ids to bottom: keep lands toward 2-3, cheap plays and rares; bottom the expensive."""
    by_id = {
        int(card.get("instance_id") or 0): card for card in state.get("hand") or [] if isinstance(card, dict)
    }
    priced = {c.instance_id: c for c in _opening_cards(state, list(by_id.values()))}
    cards = [priced.get(i) for i in instance_ids]
    if any(card is None for card in cards) or not 0 < count < len(cards):
        return []
    protected = laelia_bottom_choice(state, instance_ids, count)
    if protected:
        return protected
    kept, bottom = best_keep(cards, len(cards) - count)
    return [card.instance_id for card in bottom]


# Subsets tried when choosing several cards to lose at once (C(10, 3) = 120).
_MAX_LOSS_SUBSETS = 2000


def hand_loss_choice(state: dict, instance_ids: list[int], count: int) -> list[int]:
    """Which ``count`` of these hand cards to give up mid-game (discard, exile, put on the bottom).

    The bottoming score (``hand_score``) of what we keep, counted with our
    lands in play: an extra land beyond what the hand can use goes first, a
    land we still need stays. Ties give up a second copy before a card's last
    one, then the weaker role (``_role_value``), then the costlier card.
    Instance ids, [] when a card is unknown or the count does not fit.

    Ledger #79 (match 772233af, 2026-10-19 11:35:23): Unscrupulous Agent made
    us exile a card from Hazard, Strike, Hazard, Mountain, Shock, Effigy with
    one land in play; the model exiled Shock, the second Hazard was the card
    to lose.
    """
    hand_dicts = [card for card in state.get("hand") or [] if isinstance(card, dict)]
    by_id = {int(card.get("instance_id") or 0): card for card in hand_dicts}
    wanted = list(dict.fromkeys(int(i) for i in instance_ids))
    if not wanted or any(i not in by_id for i in wanted) or not 0 < count <= len(wanted):
        return []
    if count == len(wanted):
        return wanted
    hand = [hand_card(card) for card in hand_dicts]
    seat = local_seat(state)
    in_play = [
        hand_card(card)
        for card in state.get("battlefield") or []
        if isinstance(card, dict) and (card.get("controller_seat_id") or card.get("owner_seat_id")) == seat
    ]
    lands_in_play = [card for card in in_play if card.is_land]
    options = [card for card in hand if card.instance_id in set(wanted)]
    subsets = list(combinations(range(len(options)), count))
    if len(subsets) > _MAX_LOSS_SUBSETS:
        return []
    best_key, best = None, None
    for gone in subsets:
        lost = {options[i].instance_id for i in gone}
        kept = [card for card in hand if card.instance_id not in lost]
        key = (
            round(hand_score(kept + lands_in_play), 6),
            len({card.name for card in kept}),
            sum(card.role for card in kept),
            -sum(card.mana_value for card in kept),
            [-i for i in range(len(options)) if i not in gone],
        )
        if best_key is None or key > best_key:
            best_key, best = key, gone
    return [options[i].instance_id for i in best or ()]


def kept_score(state: dict, instance_ids: list[int], bottomed: list[int]) -> float | None:
    by_id = {
        int(card.get("instance_id") or 0): card for card in state.get("hand") or [] if isinstance(card, dict)
    }
    if any(i not in by_id for i in instance_ids):
        return None
    priced = {c.instance_id: c for c in _opening_cards(state, list(by_id.values()))}
    kept = [priced[i] for i in instance_ids if i not in set(bottomed)]
    return hand_score(opening_fetches.after_bottom(kept, set(bottomed)), _keep_land_values(len(kept)))


def kept_land_count(state: dict, instance_ids: list[int], bottomed: list[int]) -> int:
    by_id = {
        int(card.get("instance_id") or 0): card for card in state.get("hand") or [] if isinstance(card, dict)
    }
    return sum(hand_card(by_id[i]).is_land for i in instance_ids if i in by_id and i not in set(bottomed))


def card_names(state: dict, instance_ids: list[int]) -> str:
    by_id = {
        int(card.get("instance_id") or 0): card for card in state.get("hand") or [] if isinstance(card, dict)
    }
    return ", ".join(str(by_id.get(i, {}).get("name") or i) for i in instance_ids) or "none"
