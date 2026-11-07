"""Compare a land drop with a concrete commander line using only visible cards."""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import SimpleNamespace

from arenamcp.board_model import our_commanders
from arenamcp.land_mana import basic_types, enters_tapped, land_colors
from arenamcp.mana import get_local_seat_id
from arenamcp.mulligan_policy import _pip_matching
from arenamcp.play_safety import find_source
from arenamcp.rules_engine import _normalize_mana_symbols

_TYPES = {"Plains": "W", "Island": "U", "Swamp": "B", "Mountain": "R", "Forest": "G"}
_LANDFALL = re.compile(
    r"whenever a land (?:you control enters(?: the battlefield)?|enters the battlefield under your control), "
    r"(?:you may )?add one mana of any colou?r\.",
    re.I,
)


def _rules(card):
    return _normalize_mana_symbols(re.sub(r"<[^>]*>", "", card.get("oracle_text") or ""))


def _engine(card):
    return bool(_LANDFALL.search(_rules(card)))


def _produces(card, board):
    """One ordinary, unrestricted tap per land. Exert, sacrifice and filters aren't counted."""
    rules = _rules(card)
    result = {_TYPES[t] for t in _TYPES if t.lower() in basic_types(card)}
    if re.search(r"\b(?:spend|only to cast|chosen color)\b", rules, re.I):
        return result
    if "activate only if you control" in rules.lower():
        return land_colors(card, board)
    for m in re.finditer(r"\{T\}(?:, Pay 1 life)?: Add ([^.]+)\.", rules, re.I):
        # An activation with another cost before the tap symbol is not a plain tap.
        if rules[: m.start()].rstrip().endswith(","):
            continue
        effect = m[1]
        if effect.lower() == "one mana of any color":
            result.update("WUBRG")
        elif re.fullmatch(r"\{[WUBRGC]\}(?:(?:,? or |, )\{[WUBRGC]\})*", effect, re.I):
            result.update(re.findall(r"\{([WUBRGC])\}", effect.upper()))
    return result


def _pays(cost, sources):
    cost = _normalize_mana_symbols(cost or "").upper()
    if not re.fullmatch(r"(?:\{(?:[0-9]+|[WUBRGC])\})+", cost):
        return False
    symbols = re.findall(r"\{([^}]+)\}", cost)
    count = sum(int(s) if s.isdecimal() else 1 for s in symbols)
    usable = [SimpleNamespace(produces=s) for s in sources if s]
    return count <= len(usable) and _pip_matching(
        tuple(frozenset(s) for s in symbols if not s.isdecimal()), usable
    )


def _variants(land, board, *, life):
    rules = _rules(land)
    choice = "choose a basic land type" in rules.lower() and "is the chosen type" in rules.lower()
    payment = re.search(r"you may pay (\d+) life\. If you don't, it enters tapped", rules, re.I)
    for kind in _TYPES if choice else ("",):
        card = dict(land, subtypes=[kind]) if kind else land
        if payment:
            yield card, True, 0, kind
            if life - int(payment[1]) >= 5:
                yield card, False, int(payment[1]), kind
        else:
            yield card, enters_tapped(card, board), 0, kind


@dataclass(frozen=True)
class LandLine:
    option_id: str
    land: dict
    kind: str
    life: int
    now: bool
    fixing: int
    target_id: int
    reason: str


def commander_land_line(decision, state):
    if decision.request_type != "ActionsAvailable" or state.get("stack"):
        return None
    local = get_local_seat_id(state)
    turn = state.get("turn") or {}
    if local is None or turn.get("active_player") != local or "main" not in str(turn.get("phase")).lower():
        return None
    board = state.get("battlefield") or []
    # Sequencing advice for developing on a quiet board. Defer combat, cost
    # modifiers and missed priority windows to the normal tactical planner.
    if any(
        "creature" in str(c.get("type_line")).lower() and c.get("controller_seat_id") != local for c in board
    ):
        return None
    if any(
        re.search(
            r"spells?[^.]*cost[^.]*\b(?:more|less)\b|lands?[^.]*your opponents control enter tapped",
            _rules(c),
            re.I,
        )
        for c in board
    ):
        return None
    own = [c for c in board if c.get("controller_seat_id") == local and not c.get("is_phased_out")]
    lands = [c for c in own if "land" in str(c.get("type_line")).lower()]
    untapped = [c for c in lands if not c.get("is_tapped")]
    hand = state.get("hand") or []
    engines = [c for c in hand if _engine(c)]
    triggers = sum(_engine(c) for c in own)
    if not engines and not triggers:
        return None
    commanders = [c for c in our_commanders(state) if not c.blocked_by and c.cost]
    life = next(
        (p.get("life_total") or 0 for p in state.get("players") or [] if p.get("seat_id") == local), 0
    )
    if not commanders or life < 6 or len(lands) > 6:
        return None
    offered = []
    for o in decision.options:
        if o.meta.get("actionType") == "ActionType_Play":
            card = find_source(state, o.meta)
            if card and "land" in str(card.get("type_line")).lower():
                offered.append((o.option_id, card))
        if o.meta.get("actionType") == "ActionType_Cast" and o.payable is True:
            card = find_source(state, o.meta)
            if _engine(card) or any(
                c.card.get("instance_id") == card.get("instance_id") for c in commanders if card
            ):
                return None
    lines = []
    for oid, land in offered:
        for first, tapped, pay, kind in _variants(land, lands, life=life):
            field = [*lands, first]
            sources = [_produces(c, field) for c in untapped + ([] if tapped else [first])]
            sources += [set("WUBRG")] * triggers
            label = land["name"] + (f" as a {kind}" if kind else "") + (f" (pay {pay} life)" if pay else "")
            # Break ties between basic land types by which spells in the
            # actual hand/command zone need the color and which lands it enables.
            symbol = _TYPES.get(kind, "")
            costs = [c.get("mana_cost") or "" for c in hand] + [c.cost for c in commanders]
            fixing = (
                (
                    sum(_normalize_mana_symbols(cost).count("{" + symbol + "}") for cost in costs)
                    + sum(len(_produces(c, field) - _produces(c, lands)) for c in lands)
                )
                if kind
                else 0
            )
            for cmd in commanders:
                if triggers and _pays(cmd.cost, sources):
                    why = f"Play {label}. Landfall plus available lands can pay {cmd.cost} for {cmd.name} this turn."
                    lines.append(
                        LandLine(oid, land, kind, pay, True, fixing, cmd.card.get("instance_id"), why)
                    )
                    continue
                for engine in engines:
                    if not _pays(engine.get("mana_cost"), sources):
                        continue
                    for _, follow in offered:
                        if follow is land:
                            continue
                        for second, tapped2, pay2, kind2 in _variants(follow, field, life=life - pay):
                            after = [*field, second]
                            next_sources = [
                                _produces(c, after) for c in field + ([] if tapped2 else [second])
                            ]
                            if _pays(cmd.cost, next_sources + [set("WUBRG")] * (triggers + 1)):
                                why = (
                                    f"Play {label} and cast {engine['name']}. On our next turn, "
                                    f"{follow['name']}{' as a ' + kind2 if kind2 else ''} triggers landfall "
                                    f"to pay {cmd.cost} for {cmd.name} if the engine survives."
                                )
                                lines.append(
                                    LandLine(
                                        oid, land, kind, pay, False, fixing, engine.get("instance_id"), why
                                    )
                                )
    return min(lines, key=lambda line: (not line.now, line.life, -line.fixing), default=None)


def landfall_color(decision, state):
    """For a transient mana trigger, check what the offered color can cast now."""
    from arenamcp.color_choice import ColorPick, is_color_choice

    if not is_color_choice(decision):
        return None
    local = get_local_seat_id(state)
    own = [
        c
        for c in state.get("battlefield") or []
        if c.get("controller_seat_id") == local and not c.get("is_phased_out")
    ]
    if not any(c.get("name") == decision.source_label and _engine(c) for c in own):
        return None
    player = next((p for p in state.get("players") or [] if p.get("seat_id") == local), {})
    if (player.get("life_total") or 0) < 6:
        return None
    lands = [c for c in own if "land" in str(c.get("type_line")).lower()]
    mana = [_produces(c, lands) for c in lands if not c.get("is_tapped")]
    spells = [(c.name, c.cost, True) for c in our_commanders(state) if not c.blocked_by]
    spells += [
        (c.get("name"), c.get("mana_cost"), False)
        for c in state.get("hand") or []
        if re.search(r"creature|planeswalker|artifact|enchantment", str(c.get("type_line")), re.I)
    ]
    pool = player.get("mana_pool") or {}
    floated = [
        {k} for k, n in pool.items() if k in "WUBRGC" and isinstance(n, int) for _ in range(min(n, 12))
    ]
    options = []
    for opt in decision.options:
        viable = [
            (is_cmd, name)
            for name, cost, is_cmd in spells
            if name and not _pays(cost, mana) and _pays(cost, mana + [{opt.meta["color"]}])
        ]
        with_pool = [
            (cmd, name)
            for name, cost, cmd in spells
            if name and _pays(cost, mana + floated + [{opt.meta["color"]}])
        ]
        options.append(
            (
                (
                    any(cmd for cmd, _ in with_pool),
                    len(with_pool),
                    any(cmd for cmd, _ in viable),
                    len(viable),
                ),
                opt,
                viable or with_pool,
            )
        )
    options.sort(key=lambda row: row[0], reverse=True)
    score, opt, viable = options[0]
    if not viable or len(options) < 2 or score == options[1][0]:
        return None
    return ColorPick(
        opt.option_id,
        opt.label,
        f"{opt.label} from landfall makes {', '.join(name for _, name in viable)} castable now.",
        True,
    )


def choose_land_sequence(decision, state, pending):
    """Carry a verified land line through its short entry/landfall request chain."""
    from arenamcp.basic_land_choice import is_basic_land_choice
    from arenamcp.color_choice import is_color_choice
    from arenamcp.optional_prompts import prompt_cards

    key = (state.get("match_id"), (state.get("turn") or {}).get("turn_number"))
    if pending and key == pending[0] and 0 < decision.request_id[0] - pending[1] <= 12:
        line = pending[2]
        if is_basic_land_choice(decision) and decision.request_id[0] - pending[1] <= 3:
            chosen = next((o for o in decision.options if o.meta.get("name") == line.kind), None)
            if chosen:
                return (chosen.option_id, line.reason), pending
        if decision.request_type == "OptionalAction" and decision.find("optional:accept"):
            cards = prompt_cards(decision, state)
            if (
                len(cards) == 1
                and cards[0].get("name") == line.land["name"]
                and line.life
                and decision.prompt_text == f"Pay {line.life} life?"
            ):
                return ("optional:accept", line.reason), pending
        if is_color_choice(decision):
            return None, pending
        if (
            decision.request_type == "ActionsAvailable"
            and not state.get("stack")
            and any(
                c.get("name") == line.land["name"] and c.get("controller_seat_id") == get_local_seat_id(state)
                for c in state.get("battlefield") or []
            )
        ):
            cast = next(
                (
                    o
                    for o in decision.options
                    if o.meta.get("actionType") == "ActionType_Cast"
                    and o.meta.get("instanceId") == line.target_id
                    and o.payable is True
                    and not o.meta.get("alternativeCost")
                ),
                None,
            )
            if cast:
                return (cast.option_id, f"{cast.label} with the mana from the planned land drop."), None
    line = commander_land_line(decision, state)
    if line:
        return (line.option_id, line.reason), (key, decision.request_id[0], line) if all(key) else None
    return None, None
