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

import re
from collections import Counter
from dataclasses import dataclass
from itertools import combinations

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

MULLIGAN_POLICY = (
    "MULLIGAN POLICY (London): Arena shows 7 cards; after N mulligans you keep 7-N and bottom N. "
    "Each mulligan costs a card, so compare this hand with a random hand ONE CARD SMALLER, "
    "and lower the bar as the hand shrinks. "
    "Keeping 7: demand a functional hand: 3-4 lands with spells your lands can cast early keep; "
    "2 lands keep on the draw with cheap plays; 5 lands with castable spells is usually a keep, "
    "especially on the draw; mulligan 0-1 or 6-7 lands. "
    "Keeping 6: keep any 2-4 lander with a play castable by turn 3. "
    "Keeping 5: keep unless unplayable (0 lands, all lands, or 1 land with no cheap plays). "
    "Never go to 4 except for 0-land or near-all-land hands. "
    "Read each card's real mana value below; landcycling cards are extra land drops once you have "
    "the mana (count them as about half a land); cheap instants are early plays. "
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


def _land_colors(card: dict) -> frozenset[str]:
    colors = set()
    for name in card.get("color_production") or []:
        color = _COLOR_NAMES.get(str(name).strip().lower())
        if color:
            colors.add(color)
    words = " ".join([str(card.get("type_line") or ""), *[str(s) for s in card.get("subtypes") or []]])
    for word in re.findall(r"[a-z]+", words.lower()):
        if word in _BASIC_TYPES:
            colors.add(_BASIC_TYPES[word])
    text = _clean(card.get("oracle_text") or "").lower()
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
    return HandCard(
        instance_id=int(card.get("instance_id") or 0),
        name=str(card.get("name") or "?"),
        is_land=is_land,
        produces=_land_colors(card) if is_land else frozenset(),
        mana_value=0 if is_land else _mana_value(cost),
        pips=() if is_land else _pips(cost),
        landcycling=_mana_value(cycling.group(1)) if cycling else None,
        instant_speed="instant" in types or bool(flash),
        type_line=type_line,
        mana_cost=cost,
        rarity=_rarity(card),
    )


def _pip_matching(pips: tuple[frozenset[str], ...], lands: list[HandCard]) -> bool:
    """Distinct lands can pay every colored pip (one land per pip)."""
    order = sorted(pips, key=len)

    def assign(index: int, used: frozenset[int]) -> bool:
        if index == len(order):
            return True
        for position, land in enumerate(lands):
            if position not in used and land.produces & order[index]:
                if assign(index + 1, used | {position}):
                    return True
        return False

    return assign(0, frozenset())


def colors_reachable(card: HandCard, lands: list[HandCard]) -> bool:
    """Every colored pip has at least one source among these lands."""
    sources = frozenset().union(*(land.produces for land in lands)) if lands else frozenset()
    return all(pip & sources for pip in card.pips)


def castable_with(card: HandCard, lands: list[HandCard]) -> bool:
    """Castable using only these lands (land drops on curve, no draws)."""
    return card.mana_value <= len(lands) and _pip_matching(card.pips, lands)


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
    elif not _pip_matching(card.pips, lands):
        value -= 0.3
    if card.landcycling is not None:
        value = max(value, 1.5 if count >= 2 else 1.0)
    if card.rarity in ("rare", "mythic"):
        value += 1.5
    if copies and card.mana_value >= 4:
        value -= 0.5
    return value


def hand_score(kept: list[HandCard]) -> float:
    lands = [card for card in kept if card.is_land]
    score = sum(_LAND_VALUES[index] for index in range(min(len(lands), len(_LAND_VALUES))))
    seen: Counter = Counter()
    for card in kept:
        if card.is_land:
            continue
        score += _card_value(card, lands, seen[card.name])
        seen[card.name] += 1
    return score


def best_keep(cards: list[HandCard], keep: int) -> tuple[list[HandCard], list[HandCard]]:
    """The keep-card subset with the best land count and curve; ties keep cheaper cards."""
    keep = max(0, min(keep, len(cards)))
    best_key, best = None, None
    for chosen in combinations(range(len(cards)), keep):
        kept = [cards[i] for i in chosen]
        key = (hand_score(kept), -sum(card.mana_value for card in kept), [-i for i in chosen])
        if best_key is None or key > best_key:
            best_key, best = key, chosen
    kept = [cards[i] for i in best or ()]
    return kept, [card for i, card in enumerate(cards) if i not in set(best or ())]


@dataclass
class MulliganSituation:
    cards: list[HandCard]
    mulligans: int | None
    on_play: bool | None

    @property
    def keep_size(self) -> int | None:
        return None if self.mulligans is None else max(0, len(self.cards) - self.mulligans)


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
    if isinstance(value, bool):
        return None
    try:
        return max(0, int(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def on_the_play(state: dict) -> bool | None:
    """During mulligans the GRE's active player is the starting player."""
    seat = local_seat(state)
    active = (state.get("turn") or {}).get("active_player")
    if not seat or not active:
        return None
    return active == seat


def situation(state: dict, mulligans: int | None = None) -> MulliganSituation:
    hand = [card for card in state.get("hand") or [] if isinstance(card, dict)]
    return MulliganSituation(
        cards=[hand_card(card) for card in hand],
        mulligans=mulligans if mulligans is not None else mulligans_from_state(state),
        on_play=on_the_play(state),
    )


def _plays(kept: list[HandCard]) -> tuple[list[HandCard], list[HandCard], list[HandCard]]:
    lands = [card for card in kept if card.is_land]
    spells = [card for card in kept if not card.is_land]
    early = [card for card in spells if card.mana_value <= 3 and castable_with(card, lands)]
    cheap = [card for card in spells if card.mana_value <= 2 and colors_reachable(card, lands)]
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
    kept, bottom = best_keep(cards, keep_size or 7)
    kept_lands = [card for card in kept if card.is_land]
    early, cheap, covered = _plays(kept)
    landish = len(kept_lands) + 0.5 * sum(card.landcycling is not None for card in kept if not card.is_land)
    bottoming = f" (bottom {_names(bottom)})" if bottom else ""

    if keep_size in (7, None):
        if 3 <= lands <= 4 and early and len(covered) >= 2:
            return "keep", f"{lands} lands with early plays ({_names(early)}) on 7, {where}"
        if lands == 2 and found.on_play is False and cheap:
            return "keep", f"2 lands on the draw with cheap plays ({_names(cheap)})"
        if keep_size is None:
            return None
        if lands <= 1:
            return "mulligan", f"{lands} land(s) in 7 cards; a 6-card hand is better"
        if lands >= 6:
            return "mulligan", f"{lands} lands in 7 cards; a 6-card hand is better"
        return None
    if keep_size == 6:
        if lands in (0, 7):
            return "mulligan", f"{lands} lands in the 7 shown: unkeepable even at 6"
        if 2 <= len(kept_lands) <= 4 and landish <= 4.5 and early:
            return (
                "keep",
                f"keeping 6 with {len(kept_lands)} lands and early plays ({_names(early)}){bottoming}; "
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
    return "keep", f"keeping 5 with {len(kept_lands)} land(s){bottoming}; a random 4 is worse"


def _turn_note(card: HandCard, lands: list[HandCard]) -> str:
    if castable_with(card, lands):
        return f"castable turn {max(1, card.mana_value)} with these lands"
    if not colors_reachable(card, lands):
        missing = sorted(
            {color for pip in card.pips for color in pip} - {c for land in lands for c in land.produces}
        )
        return "needs " + "/".join(missing) + " mana this hand cannot make yet"
    if not _pip_matching(card.pips, lands):
        return "needs more sources of its color than this hand has"
    return f"needs {card.mana_value - len(lands)} more land(s)"


def describe(state: dict, mulligans: int | None = None) -> list[str]:
    """Prompt lines: hand size consequences, play/draw, and per-card castability."""
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
        keep_size = max(0, len(found.cards) - n)
        lines.append(
            f"MULLIGAN STATUS: London mulligan; mulligans taken this game: {n}. "
            f"KEEP → you keep {keep_size} of these {len(found.cards)}"
            + (f" and bottom {n}" if n else "")
            + f". MULLIGAN → you will see a new 7, keep {max(0, keep_size - 1)} and bottom {n + 1}."
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
    for card in found.cards:
        if card.is_land:
            lines.append(
                f"  - {card.name}: land, taps for {'/'.join(sorted(card.produces, key=COLORS.find)) or '?'}"
            )
            continue
        tags = []
        if card.instant_speed:
            tags.append("instant speed")
        if card.instant_speed and card.mana_value <= 2:
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
    if n:
        kept, bottom = best_keep(found.cards, max(0, len(found.cards) - n))
        lines.append(
            f"If you keep: best {len(kept)} by lands and curve keeps {_names(kept)}; bottom {_names(bottom)}."
        )
    return lines


def bottom_choice(state: dict, instance_ids: list[int], count: int) -> list[int]:
    """Instance ids to bottom: keep lands toward 2-3, cheap plays and rares; bottom the expensive."""
    by_id = {
        int(card.get("instance_id") or 0): card for card in state.get("hand") or [] if isinstance(card, dict)
    }
    cards = [hand_card(by_id[i]) if i in by_id else None for i in instance_ids]
    if any(card is None for card in cards) or not 0 < count < len(cards):
        return []
    kept, bottom = best_keep(cards, len(cards) - count)
    return [card.instance_id for card in bottom]


def kept_score(state: dict, instance_ids: list[int], bottomed: list[int]) -> float | None:
    by_id = {
        int(card.get("instance_id") or 0): card for card in state.get("hand") or [] if isinstance(card, dict)
    }
    if any(i not in by_id for i in instance_ids):
        return None
    return hand_score([hand_card(by_id[i]) for i in instance_ids if i not in set(bottomed)])


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
