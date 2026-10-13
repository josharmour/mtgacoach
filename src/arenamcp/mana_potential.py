"""Mana reachable this turn by sequencing the land drop, enablers and creature taps.

2026-10-11 turn 10 (The Notary Hobbits vs. angels): Arena's legal list
offered "Cast Emrakul, the World Anew" without [OK] because only ten mana
could be tapped at that moment. Playing the Forest, casting Badgermole Cub
first ({G} more per creature tapped for mana; its earthbend turns a land
into a creature) and tapping the Hobbits last reached thirteen, and
Emrakul's cast trigger takes every creature the opponent controls. The
planner, reading the missing [OK] as "unavailable", cast Vaultborn Tyrant
and the Cub instead and the game was lost to the angels next turn.

The [OK] tag counts only mana tappable right now. This note states the
total that ordering unlocks and the order that reaches it, from the same
Oracle-derived tap abilities the combat recovery forecast uses. It is a
lower bound (costly, restricted and conditional activations are omitted)
and it never makes a spell legal: Arena still has to confirm the cast.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from arenamcp.combat_recovery import _cost, _payable, _text, mana_options
from arenamcp.rules_engine import card_has_haste

_COLOUR_INDEX = {"W": 0, "U": 1, "B": 2, "R": 3, "G": 4, "C": 5}
_COLOUR_NAMES = {
    "white": "W",
    "blue": "U",
    "black": "B",
    "red": "R",
    "green": "G",
    "colorless": "C",
}
_SAC_MANA_RE = re.compile(
    r"^Sacrifice (?:this (?:token|creature|artifact|permanent)|~): Add ((?:\{[WUBRGC]\})+)\.?$", re.I | re.M
)
_TAP_BONUS_RE = re.compile(
    r"Whenever (?:you tap a creature(?: you control)?|a creature you control is tapped|you tap a creature) "
    r"for mana, add an additional ((?:\{[WUBRGC]\})+)",
    re.I,
)
_AURA_BONUS_RE = re.compile(
    r"Whenever enchanted \w+ is tapped for mana, its controller adds an additional "
    r"(one mana of the chosen color|one mana of any color|(?:\{[WUBRGC]\})+)",
    re.I,
)
_EXTRA_LAND_RE = re.compile(r"you may play an additional land", re.I)
_EARTHBEND_RE = re.compile(r"\bearthbend\b", re.I)

Options = tuple[tuple[int, ...], ...]


def _symbols(text: str) -> tuple[int, ...]:
    out = [0] * 6
    for symbol in re.findall(r"\{([WUBRGC])\}", text.upper()):
        out[_COLOUR_INDEX[symbol]] += 1
    return tuple(out)


def _colour_options(names: list | None) -> Options:
    options = set()
    for name in names or []:
        symbol = _COLOUR_NAMES.get(str(name).lower())
        if symbol:
            option = [0] * 6
            option[_COLOUR_INDEX[symbol]] = 1
            options.add(tuple(option))
    return tuple(sorted(options))


def _is_land(card: dict) -> bool:
    return (
        "Land" in (card.get("card_types") or [])
        or "land" in str(card.get("type_line", "")).lower().split("—")[0]
    )


def _is_creature(card: dict) -> bool:
    return (
        "Creature" in (card.get("card_types") or []) or "creature" in str(card.get("type_line", "")).lower()
    )


def _land_options(card: dict, own: list[dict]) -> Options:
    return mana_options(card, own) or _colour_options(card.get("color_production"))


def _bump(options: Options, bonus: tuple[int, ...]) -> Options:
    return tuple(tuple(a + b for a, b in zip(option, bonus, strict=True)) for option in options)


def _max(options: Options) -> int:
    return max((sum(option) for option in options), default=0)


def _ready(card: dict, turn: int) -> bool:
    entered = card.get("turn_entered_battlefield")
    if isinstance(entered, int) and turn:
        return entered < turn or card_has_haste(card)
    return not card.get("summoning_sickness") or card_has_haste(card)


@dataclass
class ManaPotential:
    base_total: int
    base_sources: list[Options]
    land_sources: list[Options]
    parts: list[str]
    steps: list[str]
    enabler: dict | None = None
    enabler_cost: tuple[int, tuple[int, ...]] | None = None
    enabler_sources: list[Options] = field(default_factory=list)
    enabler_part: str = ""
    enabler_steps: list[str] = field(default_factory=list)

    @property
    def total_with_enabler(self) -> int:
        if self.enabler_cost is None:
            return self.base_total
        return self.base_total + sum(_max(s) for s in self.enabler_sources) - self.enabler_cost[0]


def _our_main_phase(state: dict) -> int | None:
    turn = state.get("turn") or {}
    local = state.get("local_seat_id")
    if local is None:
        local = next((p.get("seat_id") for p in state.get("players", []) if p.get("is_local")), None)
    if local is None or turn.get("active_player") != local:
        return None
    phase = str(turn.get("phase") or "").lower()
    if "main" not in phase:
        return None
    return int(turn.get("turn_number") or 0)


def turn_mana_potential(state: dict) -> ManaPotential | None:
    """Supported mana this turn if the land drop and tap sources are sequenced, or None off our main phase."""
    turn = _our_main_phase(state)
    if turn is None:
        return None
    local = next((p for p in state.get("players", []) if p.get("is_local")), {})
    seat = local.get("seat_id") or state.get("local_seat_id")
    own = [
        c
        for c in state.get("battlefield", [])
        if (c.get("controller_seat_id") or c.get("owner_seat_id")) == seat
    ]
    hand = [c for c in state.get("hand") or [] if isinstance(c, dict)]
    parts: list[str] = []
    steps: list[str] = []

    # Lands on the battlefield, plus auras that add mana when their land is tapped.
    aura_bonus: dict[int, tuple[int, ...]] = {}
    for card in own:
        match = _AURA_BONUS_RE.search(_text(card))
        if match and card.get("attached_to_id"):
            bonus = _symbols(match[1]) if match[1].startswith("{") else (0, 0, 0, 0, 0, 1)
            aura_bonus[int(card["attached_to_id"])] = bonus
    land_sources: list[Options] = []
    untapped_lands = [c for c in own if _is_land(c) and not c.get("is_tapped")]
    aura_total = 0
    for card in untapped_lands:
        options = _land_options(card, own)
        if not options:
            continue
        bonus = aura_bonus.get(card.get("instance_id"))
        if bonus:
            options = _bump(options, bonus)
            aura_total += sum(bonus)
        land_sources.append(options)
    land_mana = sum(_max(s) for s in land_sources)

    # The land drop(s) still available from hand.
    allowed = 1 + sum(1 for c in own if _EXTRA_LAND_RE.search(_text(c)))
    drops = max(0, allowed - int(local.get("lands_played") or 0))
    hand_lands = sorted(
        ((c, _land_options(c, own)) for c in hand if _is_land(c)),
        key=lambda pair: -_max(pair[1]),
    )
    dropped = []
    for card, options in hand_lands[:drops]:
        if not options:
            continue
        land_sources.append(options)
        land_mana += _max(options)
        dropped.append(str(card.get("name") or "land"))
        steps.append(f"Play Land: {card.get('name')}")
    land_part = f"lands {land_mana}"
    details = []
    if dropped:
        details.append("incl. " + ", ".join(f"Play Land: {name}" for name in dropped))
    if aura_total:
        details.append(f"aura bonus +{aura_total}")
    if details:
        land_part += f" ({'; '.join(details)})"
    if land_mana:
        parts.append(land_part)

    # Creatures that can tap for mana now (not summoning sick), with per-type scaling.
    creature_sources: list[Options] = []
    creature_names: list[str] = []
    for card in own:
        if not _is_creature(card) or _is_land(card) or card.get("is_tapped") or not _ready(card, turn):
            continue
        options = mana_options(card, own)
        if options:
            creature_sources.append(options)
            creature_names.append(f"{card.get('name', 'creature')} {_max(options)}")
    if creature_sources:
        parts.append(f"creature taps {sum(_max(s) for s in creature_sources)} ({', '.join(creature_names)})")

    # Tokens and permanents that sacrifice for mana.
    sac_sources: list[Options] = []
    sac_names: list[str] = []
    for card in own:
        match = _SAC_MANA_RE.search(_text(card))
        if match:
            sac_sources.append((_symbols(match[1]),))
            sac_names.append(str(card.get("name") or "token"))
    if sac_sources:
        parts.append(f"sacrifice {', '.join(sac_names)} +{sum(_max(s) for s in sac_sources)}")

    # A creature-tap bonus already on the battlefield applies to every creature tap.
    board_bonus = [_symbols(m[1]) for c in own for m in [_TAP_BONUS_RE.search(_text(c))] if m]
    bonus_sources: list[Options] = []
    for bonus in board_bonus:
        bonus_sources.extend((bonus,) for _ in creature_sources)
    if bonus_sources:
        parts.append(f"tap bonus +{sum(_max(s) for s in bonus_sources)} (one per creature tapped for mana)")

    pool_sources: list[Options] = []
    pool = local.get("mana_pool") or {}
    if isinstance(pool, dict):
        floating = [0] * 6
        for key, value in pool.items():
            symbol = _COLOUR_NAMES.get(str(key).lower(), str(key).upper())
            if symbol in _COLOUR_INDEX and isinstance(value, int) and value > 0:
                floating[_COLOUR_INDEX[symbol]] += value
        if sum(floating):
            pool_sources.append((tuple(floating),))
            parts.append(f"floating {sum(floating)}")

    base_sources = land_sources + creature_sources + sac_sources + bonus_sources + pool_sources
    potential = ManaPotential(
        base_total=sum(_max(s) for s in base_sources),
        base_sources=base_sources,
        land_sources=land_sources + pool_sources,
        parts=parts,
        steps=steps,
    )

    # An enabler in hand: a creature-tap bonus cast first from land mana.
    best_gain = 0
    for card in hand:
        match = _TAP_BONUS_RE.search(_text(card))
        if not match or _is_land(card):
            continue
        cost = _cost(card, 0)
        if cost is None or not _payable(potential.land_sources, cost):
            continue
        bonus = _symbols(match[1])
        taps = len(creature_sources)
        earthbend = bool(_EARTHBEND_RE.search(_text(card))) and len(untapped_lands) + len(dropped) > cost[0]
        extra = [(bonus,) for _ in range(taps + (1 if earthbend else 0))]
        gain = sum(_max(s) for s in extra) - cost[0]
        if gain <= best_gain:
            continue
        best_gain = gain
        potential.enabler = card
        potential.enabler_cost = cost
        potential.enabler_sources = extra
        potential.enabler_part = (
            f"{card.get('name')} cast first ({card.get('mana_cost')} from land mana): +{sum(_max(s) for s in extra)}, "
            "one per creature tapped for mana"
            + (
                ", plus one if it earthbends an UNTAPPED land (it then taps as a creature)"
                if earthbend
                else ""
            )
        )
        potential.enabler_steps = [f"Cast {card.get('name')} (pay with lands)"] + (
            ["earthbend an untapped land"] if earthbend else []
        )
    return potential


def _joint(a: tuple[int, tuple[int, ...]], b: tuple[int, tuple[int, ...]]) -> tuple[int, tuple[int, ...]]:
    return a[0] + b[0], tuple(x + y for x, y in zip(a[1], b[1], strict=True))


def mana_reach_note(state: dict, payable: dict[int, bool | None] | None = None) -> str:
    """The sequencing note when a hand card Arena does not yet mark [OK] is reachable this turn, else ''.

    ``payable`` maps hand instance ids to Arena's autotap verdict for this
    window; cards already [OK] need no note.
    """
    potential = turn_mana_potential(state)
    if potential is None or not potential.base_sources:
        return ""
    payable = payable or {}
    plain: list[str] = []
    enabled: list[str] = []
    for card in state.get("hand") or []:
        if not isinstance(card, dict) or _is_land(card) or payable.get(card.get("instance_id")) is True:
            continue
        if potential.enabler is not None and card is potential.enabler:
            continue
        cost = _cost(card, 0)
        if cost is None or cost[0] == 0:
            continue
        label = f"{card.get('name')} {card.get('mana_cost')}"
        if _payable(potential.base_sources, cost):
            # Arena said not now: the land drop, creature taps or sacrifices close the gap.
            plain.append(label)
        elif potential.enabler is not None and _payable(
            potential.base_sources + potential.enabler_sources, _joint(cost, potential.enabler_cost)
        ):
            enabled.append(label)
    if not plain and not enabled:
        return ""
    with_enabler = bool(enabled)
    total = potential.total_with_enabler if with_enabler else potential.base_total
    parts = list(potential.parts) + ([potential.enabler_part] if with_enabler else [])
    steps = list(potential.steps) + (potential.enabler_steps if with_enabler else [])
    lines = [
        "MANA REACHABLE THIS TURN (deterministic sequencing; Arena's [OK] tags count only mana tappable "
        f"right now): up to {total} = " + " + ".join(parts) + ".",
    ]
    unlocked = plain + enabled
    lines.append("  Unlocks (not [OK] in this window): " + "; ".join(unlocked) + ".")
    order = (
        steps
        + ["tap creatures and sacrifice last"]
        + [f"Cast {name.rsplit(' {', 1)[0]}" for name in unlocked[:1]]
    )
    lines.append("  Order: " + " -> ".join(order) + ". Spending mana on any other spell first forfeits this.")
    return "\n".join(lines)
