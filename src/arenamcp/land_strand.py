"""Compare the mana left by land choices (Fable November L1).

Mana forecasts compare the legal choices in one request. Each spell is priced
independently. Targets, future draws and unknown effects stay with the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from arenamcp.board_model import our_commanders
from arenamcp.combat_recovery import _cost, _payable, mana_options
from arenamcp.decisions import asking_card
from arenamcp.land_mana import basic_types, enters_tapped, land_colors
from arenamcp.mana_potential import _ready
from arenamcp.mulligan_policy import _mana_value
from arenamcp.play_safety import _local_seat, find_source
from arenamcp.rules_engine import _normalize_mana_symbols

COLORS = "WUBRGC"
TYPES = {"plains": "W", "island": "U", "swamp": "B", "mountain": "R", "forest": "G"}


def _rules(card):
    return _normalize_mana_symbols(re.sub(r"<[^>]*>", "", str(card.get("oracle_text") or "")))


def _land(card):
    return bool(re.search(r"\bland\b", str(card.get("type_line") or "").split("—")[0], re.I))


def _vector(symbols):
    return tuple(symbols.count("{" + color + "}") for color in COLORS)


def _outputs(text):
    if text.lower() == "one mana of any color":
        return tuple(_vector("{" + color + "}") for color in "WUBRG")
    choices = re.split(r",? or |, ", text)
    if all(re.fullmatch(r"(?:\{[WUBRGC]\})+", choice) for choice in choices):
        return tuple(_vector(choice) for choice in choices)
    return ()


def _facts_mana(card, facts):
    """An attached record may corroborate a production line on the printed card."""
    from arenamcp.card_facts.client import hash_for_card

    if (
        not isinstance(facts, dict)
        or type(facts.get("schema_version")) is not int
        or facts["schema_version"] != 1
    ):
        return None
    digest = hash_for_card(card)
    if (
        not digest
        or facts.get("rules_hash") != digest
        or facts.get("name") != card.get("name")
        or facts.get("type_line") != card.get("type_line")
        or facts.get("confidence") != "high"
    ):
        return None
    entries = facts.get("mana")
    if not isinstance(entries, (list, tuple)) or not entries:
        return None
    result = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("condition"):
            return None
        outputs = _outputs(_normalize_mana_symbols(str(entry.get("mana") or "")))
        if not outputs:
            return None
        result.update(outputs)
    return tuple(sorted(result))


def _typed_board(board, external=()):
    # Public static basic-type additions, not a card-name rule. Recompute
    # after a sacrifice/drop so an enabling permanent is not left behind.
    types = set()
    for card in [*board, *external]:
        for line in _rules(card).splitlines():
            grant = re.fullmatch(
                r"Each land is a (Plains|Island|Swamp|Mountain|Forest) in addition to its other land types\.",
                line.strip(),
                re.I,
            )
            if grant:
                types.add(grant[1])
    if not types:
        return board
    return [
        dict(card, subtypes=[*(card.get("subtypes") or []), *types]) if _land(card) else card
        for card in board
    ]


def _life_charge(card):
    cost = re.findall(r"(?:pay (\d+) life|deals? (\d+) damage to you)", _rules(card), re.I)
    return max((int(n) for pair in cost for n in pair if n), default=0)


def _source(card, board, state):
    """One mutually exclusive output per source; never count a dual twice."""
    if card.get("is_phased_out"):
        return (), True
    if not card.get("type_line"):
        return (), False
    text = _rules(card)
    counters = card.get("counters") or {}
    if not isinstance(counters, dict) or any(type(n) is not int or n < 0 for n in counters.values()):
        return (), False
    if any("stun" in str(k).lower() and n for k, n in counters.items()):
        return (), False
    if re.search(r"(?:doesn't|don't|cannot|can't) untap|loses? all abilities", text, re.I):
        return (), False
    if not _land(card):
        options = mana_options(card, board)
        if re.search(r"spend .*only|activate only", text, re.I) and re.search(r"\badd\b", text, re.I):
            return options, False
        return options, bool(options) or not bool(re.search(r"\badd\b.*mana|\badd\s*\{", text, re.I))
    outputs = {_vector("{" + TYPES[t] + "}") for t in basic_types(card) if t in TYPES}
    known = bool(outputs)
    unsupported = False
    for line in text.strip().splitlines():
        line = line.strip()
        if not re.search(r"\badd\b", line, re.I):
            if re.search(r"^(?:Spend|Activate only|Use this mana)", line, re.I):
                unsupported = True
            continue
        clause = _mana_clause(line, card)
        if not clause:
            unsupported = True
            continue
        rider = clause[3] or ""
        if condition := _TYPE_CONDITION.fullmatch(rider):
            kinds = {t.lower() for t in re.findall(_BASIC_TYPE, condition[0], re.I)}
            if not any(basic_types(p) & kinds for p in board):
                continue
        parsed = _outputs(clause[2])
        if replacement := _COUNTER_REPLACEMENT.fullmatch(rider):
            kind = replacement[2].lower()
            has_counter = any(
                str(k).lower().removeprefix("countertype_") == kind and type(count) is int and count > 0
                for k, count in counters.items()
            )
            if has_counter:
                parsed = _outputs("one mana of any color")
        outputs.update(parsed)
        known = True
    # A structured production list is useful for older snapshots, but is not
    # authoritative over a printed conditional/restricted ability.
    if not text and card.get("color_production"):
        outputs.update(_vector("{" + c + "}") for c in land_colors(card, board))
        known = bool(outputs)
    if re.search(r"spend .*only", text, re.I) or ("search your library" in text.lower() and not outputs):
        known = False
    life = next(
        (p.get("life_total") for p in state.get("players") or [] if p.get("seat_id") == _local_seat(state)),
        None,
    )
    charge = _life_charge(card)
    if charge and (type(life) is not int or life <= 2 * charge):
        known = False
    # A fresh hash binds the printed input, not the compiler's interpretation.
    # A record alone cannot turn a conditional or costly activation into free mana.
    record = card.get("card_facts") or state.get("_land_mana_records", {}).get(card.get("instance_id"))
    facts = _facts_mana(card, record) if record else None
    if facts and set(facts) != outputs:
        known = False
    return tuple(sorted(outputs)), known and not unsupported


def _sources(board, state, *, now):
    external = [
        c
        for c in state.get("battlefield") or []
        if c.get("controller_seat_id", c.get("owner_seat_id")) != _local_seat(state)
    ]
    uncertain_type_grant = any(
        re.search(r"\b(?:each|all|other) lands? (?:you control )?(?:is|are|have|has|lose)\b", line, re.I)
        and not re.fullmatch(
            r"Each land is a (Plains|Island|Swamp|Mountain|Forest) in addition to its other land types\.",
            line.strip(),
            re.I,
        )
        for c in [*board, *external]
        for line in _rules(c).splitlines()
    )
    board = _typed_board(board, external)
    charges = sum(_life_charge(c) for c in board if _land(c))
    local = next((p for p in state.get("players") or [] if p.get("seat_id") == _local_seat(state)), {})
    life = local.get("life_total")
    # The two-turn forecast prices spells independently. Keep the choices when
    # using pain sources on both turns could exhaust the shared life payment.
    sources, certain = (
        [],
        not uncertain_type_grant and (not charges or (type(life) is int and life > 2 * charges)),
    )
    turn = int((state.get("turn") or {}).get("turn_number") or 0)
    for card in board:
        options, known = _source(card, board, state)
        certain &= known
        if now and (
            card.get("is_tapped")
            or ("creature" in str(card.get("type_line") or "").lower() and not _ready(card, turn))
        ):
            continue
        if options:
            sources.append(options)
    if now:
        player = next((p for p in state.get("players") or [] if p.get("seat_id") == _local_seat(state)), {})
        pool = player.get("mana_pool") or {}
        names = {"white": "W", "blue": "U", "black": "B", "red": "R", "green": "G", "colorless": "C"}
        if isinstance(pool, dict):
            floating = [0] * 6
            for key, count in pool.items():
                color = names.get(str(key).lower(), str(key).upper())
                if color in COLORS and isinstance(count, int) and count > 0:
                    floating[COLORS.index(color)] += count
            if any(floating):
                sources.append((tuple(floating),))
    return sources, certain


def _capacity(sources):
    return sum(max(map(sum, source), default=0) for source in sources)


def _card_for(option, state):
    card = find_source(state, option.meta)
    if not card and option.option_id.startswith("sel:"):
        try:
            iid = int(option.option_id.split(":")[1])
        except ValueError:
            iid = 0
        card = find_source(state, {"instanceId": iid}) if iid else {}
    embedded = option.meta.get("card")
    if isinstance(embedded, dict):
        result = {**embedded, **card}
        if not result.get("instance_id") and option.option_id.startswith("sel:"):
            try:
                result["instance_id"] = int(option.option_id.split(":")[1])
            except ValueError:
                pass
        return result
    return card


def _destination(decision, state):
    if decision.request_type != "Search":
        return None
    _, text = asking_card(decision, state)
    if not text:
        # Some old Search requests omit sourceId; one resolving search spell
        # on the stack is still an unambiguous source.
        candidates = [
            _rules(c) for c in state.get("stack") or [] if "search your library" in _rules(c).lower()
        ]
        if len(candidates) == 1:
            text = candidates[0]
    text = _normalize_mana_symbols(text)
    if re.search(r"the rest|one of them|the other|instead|you may (?:have|put)", text, re.I):
        return None
    if re.search(r"put (?:it|that card|them) (?:onto|on) the battlefield", text, re.I):
        return "tapped" if re.search(r"battlefield tapped", text, re.I) else "battlefield"
    if re.search(r"put (?:it|that card|them) into your hand", text, re.I):
        return "hand"
    # Unknown destination, top-of-library and mixed-zone searches get facts
    # only, never a made-up immediate mana source.
    return None


def _dropped(card, board, *, force_tapped=False):
    return dict(card, is_tapped=force_tapped or enters_tapped(card, board))


def _followups(card, board, state, library):
    """Known fetch targets and selectable basic types, never hypothetical draws."""
    from arenamcp.land_sequence import _variants

    text = _rules(card)
    local = _local_seat(state)
    life = next(
        (p.get("life_total") or 0 for p in state.get("players") or [] if p.get("seat_id") == local), 0
    )
    # Other fetches may require mana, specific types, cycling, delayed triggers
    # or additional payments. Their eventual target menu is authoritative.
    subject = r"(?:this land|" + re.escape(str(card.get("name") or "?")) + ")"
    type_pattern = r"(?:Plains|Island|Swamp|Mountain|Forest)"
    fetch = re.fullmatch(
        r"\{T\}(?:, Pay (1) life)?, Sacrifice "
        + subject
        + r": Search your library for (?:an? )?(basic land|"
        + type_pattern
        + r"(?: or "
        + type_pattern
        + r")?) card, put (?:it|that card) onto the battlefield( tapped)?, then shuffle\.",
        text,
        re.I,
    )
    if fetch and not enters_tapped(card, board) and (not fetch[1] or life > 1):
        kinds = set(re.findall(type_pattern, fetch[2].lower(), re.I))
        used = {c.get("instance_id") for c in board}
        targets = [
            c
            for c in library
            if _land(c)
            and c.get("instance_id") is not None
            and c["instance_id"] not in used
            and (basic_types(c) & kinds if kinds else "basic" in str(c.get("type_line") or "").lower())
        ]
        if targets:
            return [
                dict(
                    _dropped(c, board, force_tapped=bool(fetch[3])),
                    _played_land_instance=card.get("instance_id"),
                )
                for c in targets
            ]
    return [dict(c, is_tapped=tapped) for c, tapped, _pay, _kind in _variants(card, board, life=life)]


def _remove(board, card):
    iid = card.get("instance_id")
    if iid is None or sum(c.get("instance_id") == iid for c in board) != 1:
        return None
    return [c for c in board if c.get("instance_id") != iid]


_BASIC_TYPE = r"(?:Plains|Island|Swamp|Mountain|Forest)"
_PRINTED_MANA = re.compile(r"\{T\}(?:, Pay (\d+) life)?: Add ([^.]+)\.(?: (.*))?", re.I)
_SELF_DAMAGE = re.compile(r"(This land|It|[^.:]+) deals (\d+) damage to you\.", re.I)
_TYPE_CONDITION = re.compile(
    rf"Activate only if you control (?:a|an) {_BASIC_TYPE} or (?:a|an) {_BASIC_TYPE}\.", re.I
)
_COUNTER_REPLACEMENT = re.compile(
    r"If (this land|[^.:]+) has a ([a-z]+) counter on it, instead add one mana of any color\.", re.I
)


def _mana_clause(line, card):
    """Return the complete printed tap ability, including its entire rider."""
    match = _PRINTED_MANA.fullmatch(line.strip("()"))
    if not match:
        return None
    payout, rider = match[2], match[3] or ""
    if not _outputs(payout):
        return None
    if rider and not (
        _SELF_DAMAGE.fullmatch(rider)
        or _TYPE_CONDITION.fullmatch(rider)
        or _COUNTER_REPLACEMENT.fullmatch(rider)
    ):
        return None
    if subject := _SELF_DAMAGE.fullmatch(rider) or _COUNTER_REPLACEMENT.fullmatch(rider):
        self_names = {"it", "this land", str(card.get("name") or "").casefold()}
        # A card can have a changed display name. The opening-hand rule explicitly
        # binds the named card beginning on the battlefield to "this card".
        opening = re.match(
            r"If this card is in your opening hand and you're not the starting player, "
            r"you may begin the game with (.+?) on the battlefield with an? [a-z]+ counter on it\.",
            _rules(card),
            re.I,
        )
        if opening:
            self_names.add(opening[1].casefold())
        if subject[1].casefold() not in self_names:
            return None
    return match


def _has_unpriced_payoff(card, state):
    """Require a full account of every line before removing a land from a menu."""
    text = _rules(card).strip()
    if not text:
        return not (basic_types(card) and "basic" in str(card.get("type_line") or "").lower())
    for line in text.splitlines():
        line = line.strip()
        if _mana_clause(line, card):
            continue
        # Unconditional tapped entry and the two native, modelled land checks.
        if re.fullmatch(
            rf"(?:this land|{re.escape(str(card.get('name') or '?'))}) enters (?:the battlefield )?tapped"
            rf"(?: unless you control (?:two or more other lands|(?:a|an) {_BASIC_TYPE}(?: or (?:a|an) {_BASIC_TYPE})?))?\.",
            line,
            re.I,
        ):
            continue
        if ":" in line:
            cost_text, effect = line.split(":", 1)
            if re.fullmatch(
                rf"(?:[\w ]+ — )?(?:\{{(?:[0-9]+|[WUBRGC])\}}, )*\{{T\}}, Sacrifice (?:this land|{re.escape(str(card.get('name') or '?'))})",
                cost_text,
                re.I,
            ) and not _damage_tradeoff(effect, state):
                continue
        # Unknown static, entry, payment, trigger, and alternative-use text
        # all preserve the original option. Future rules cannot add a free benefit.
        return True
    return False


def _damage_tradeoff(effect, state):
    """A pure fixed damage ability can be priced narrowly on an empty board."""
    # Every other effect, including damage to a creature, damage scaled by X
    # and life loss with a rider, has a payoff outside this mana forecast.
    match = re.fullmatch(
        r"[^.:]+ deals (\d+) damage to (?:any target|target (?:opponent|player))\."
        r"(?: Activate only if [^.]+\.)?",
        effect.strip(),
        re.I,
    )
    if not match or state.get("stack"):
        return True
    local = _local_seat(state)
    opponent = [p for p in state.get("players") or [] if p.get("seat_id") != local]
    if len(opponent) != 1 or type(opponent[0].get("life_total")) is not int:
        return True
    if int(match[1]) >= opponent[0]["life_total"]:
        return True
    from arenamcp.board_assessment import harms_players

    # Do not suppress the last points of a multi-spell kill. Count possible
    # face effects even when their cost or magnitude is not modelled.
    if any(not _land(c) and harms_players(c) for c in state.get("hand") or []):
        return True
    # Other permanents can supply recipients, modify the damage or trigger
    # on gaining/losing life. A land creature is also a real recipient.
    return any(
        not _land(c)
        or re.search(r"\b(?:creature|planeswalker|battle)\b", str(c.get("type_line") or ""), re.I)
        or re.search(
            r"\b(?:whenever|double|becomes a creature)\b|(?:damage|life)[^.]*\binstead\b", _rules(c), re.I
        )
        for c in state.get("battlefield") or []
    )


@dataclass(frozen=True)
class LandChoice:
    option_id: str
    name: str
    now: frozenset[int]
    later: frozenset[int]
    covered: frozenset[int]
    lost: frozenset[str]
    certain: bool
    kind: str
    life_cost: int = 0
    unpriced_payoff: bool = False
    preserves_all_colours: bool = False
    mana_certain: bool = False


def _spells(state):
    spells = [(str(c.get("name") or "?"), c) for c in state.get("hand") or [] if not _land(c)]
    spells += [(c.name, dict(c.card, mana_cost=c.cost)) for c in our_commanders(state) if not c.blocked_by]
    return [(name, cost) for name, card in spells if (cost := _cost(card, 0)) is not None]


def land_choices(decision, state):
    """Mana-only forecasts for each offered land; unknown lines remain legal."""
    if len(decision.options) > 128 or decision.request_type not in {"ActionsAvailable", "Search", "SelectN"}:
        return [], []
    local = _local_seat(state)
    if local is None or "battlefield" not in state:
        return [], []
    board = [
        c
        for c in state.get("battlefield") or []
        if c.get("controller_seat_id", c.get("owner_seat_id")) == local and not c.get("is_phased_out")
    ]
    if any(
        c.get("controller_seat_id", c.get("owner_seat_id")) is None for c in state.get("battlefield") or []
    ):
        return [], []
    destination = _destination(decision, state)
    if len(state.get("hand") or []) > 24 or len(board) > 40:
        return [], []
    sacrifice = (
        decision.request_type == "SelectN"
        and "sacrific" in (decision.prompt_text + " " + decision.source_label).lower()
    )
    if decision.request_type == "SelectN" and not sacrifice:
        return [], []
    player = next((p for p in state.get("players") or [] if p.get("seat_id") == local), {})
    turn = state.get("turn") or {}
    drop_available = turn.get("active_player") == local and player.get("lands_played") == 0
    spells = _spells(state)
    base, base_known = _sources(board, state, now=False)
    # A choice may itself add more than one mana (e.g. the logged Tomb).
    # The horizon selects relevant costs; actual payment uses exact outputs.
    offered = [_card_for(o, state) for o in decision.options]
    potential = max(
        (_capacity([_source(c, _typed_board([*board, c]), state)[0]]) for c in offered if _land(c)), default=0
    )
    horizon = _capacity(base) + max(1, potential) + 2
    spells = [(name, cost) for name, cost in spells if cost[0] <= horizon]
    needed = {c for _, cost in spells for c, n in zip(COLORS, cost[1], strict=True) if n}
    base_colors = {
        c for source in base for output in source for c, n in zip(COLORS, output, strict=True) if n
    }
    uncertain_effect = any(
        re.search(
            r"spells?[^.]*cost[^.]*\b(?:less|more)\b|lands?[^.]*enter[^.]*tapped|"
            r"(?:additional|twice|double)[^.]*mana|mana[^.]*instead|nonbasic lands are",
            _rules(c),
            re.I,
        )
        for c in state.get("battlefield") or []
        if not _land(c)
    )
    # An unsupported affordable cost (hybrid, Phyrexian, X, etc.) may be
    # the very trade-off a different colour enables. Do not erase it.
    uncertain_cost = any(
        not _land(card)
        and card.get("mana_cost")
        and _cost(card, 0) is None
        and _mana_value(str(card["mana_cost"])) <= horizon
        for card in state.get("hand") or []
    )
    choices = []
    for option in decision.options:
        card = _card_for(option, state)
        if (
            not card
            or not _land(card)
            or option.payable is False
            or option.meta.get("identity_known") is False
        ):
            continue
        if decision.request_type == "ActionsAvailable" and option.meta.get("actionType") != "ActionType_Play":
            continue  # MDFC face selection and fetch resolution get their own menu.
        kind = "sacrifice" if sacrifice else "search" if decision.request_type == "Search" else "drop"
        hand = [
            c for c in state.get("hand") or [] if _land(c) and c.get("instance_id") != card.get("instance_id")
        ]
        if sacrifice:
            after = _remove(board, card)
            if after is None:
                continue
        elif kind == "search" and destination in (None, "hand"):
            after = list(board)
            if destination == "hand":
                hand.append(card)
        else:
            after = [*board, _dropped(card, board, force_tapped=destination == "tapped")]
        library = [c for c in offered if _land(c) and c.get("instance_id") != card.get("instance_id")]
        if decision.request_type != "Search":
            library = [
                c
                for c in state.get("library") or []
                if isinstance(c, dict) and c.get("owner_seat_id") == local
            ]
        current_boards = [after]
        if kind != "drop" and drop_available:
            current_boards += [
                [*after, variant] for c in hand for variant in _followups(c, after, state, library)
            ]
        # This turn and our next turn, using only lands already visible in
        # hand. Each spell is evaluated separately, not cast simultaneously.
        now_sources = [_sources(b, state, now=True) for b in current_boards]
        future_boards = []
        for b in current_boards:
            future_boards.append([dict(c, is_tapped=False, summoning_sickness=False) for c in b])
            used = {c.get("_played_land_instance", c.get("instance_id")) for c in b}
            for follow in hand:
                if follow.get("instance_id") is not None and follow.get("instance_id") in used:
                    continue
                if len(future_boards) > 256:
                    return [], []
                future_boards.extend(
                    [*[dict(c, is_tapped=False, summoning_sickness=False) for c in b], variant]
                    for variant in _followups(follow, b, state, library)
                )
        # On our next turn the new land's entry state still matters. Floating
        # mana empties, life does not; creatures cease being summoning sick.
        later_state = dict(
            state,
            players=[dict(p, mana_pool={}) for p in state.get("players") or []],
            turn=dict(turn, turn_number=int(turn.get("turn_number") or 0) + 2),
        )
        if len(future_boards) > 256:
            return [], []
        later_sources = [_sources(b, later_state, now=True) for b in future_boards]
        permanent_sources, permanent_known = _sources(after, state, now=False)
        colors = {
            c
            for source in permanent_sources
            for output in source
            for c, n in zip(COLORS, output, strict=True)
            if n
        }
        now = frozenset(
            i
            for i, (_, cost) in enumerate(spells)
            if cost[0] == 0 or any(_payable(s, cost) for s, _ in now_sources)
        )
        later = frozenset(
            i
            for i, (_, cost) in enumerate(spells)
            if cost[0] == 0 or any(_payable(s, cost) for s, _ in later_sources)
        )
        covered = frozenset(
            i
            for i, (_, cost) in enumerate(spells)
            if all(not n or c in colors for c, n in zip(COLORS, cost[1], strict=True))
        )
        certain = permanent_known and all(k for _, k in now_sources + later_sources)
        certain &= (
            not uncertain_effect and not uncertain_cost and not (kind == "search" and destination is None)
        )
        choices.append(
            LandChoice(
                option.option_id,
                str(card.get("name") or "?"),
                now,
                later,
                covered,
                frozenset(needed & (base_colors - colors)) if sacrifice else frozenset(),
                certain,
                kind,
                _life_charge(card),
                unpriced_payoff=_has_unpriced_payoff(card, state),
                preserves_all_colours=base_colors <= colors,
                mana_certain=base_known and permanent_known and not uncertain_effect,
            )
        )
    return choices, [name for name, _ in spells]


def _better(a, b):
    if a.kind != b.kind or a.unpriced_payoff or b.unpriced_payoff:
        return False
    if a.kind == "sacrifice" and a.life_cost != b.life_cost:
        return False
    if a.mana_certain and b.mana_certain and b.lost and a.preserves_all_colours:
        return True
    if not a.certain or not b.certain:
        return False
    if not a.lost <= b.lost or a.life_cost > b.life_cost:
        return False
    return (
        a.now >= b.now
        and a.later >= b.later
        and a.covered >= b.covered
        and (a.now != b.now or a.later != b.later or a.covered != b.covered)
    )


def _activation_losses(decision, state):
    """Land activations that sacrifice their own sole required colour source."""
    from arenamcp.play_safety import _offered_ability

    if decision.request_type != "ActionsAvailable" or not any(
        o.meta.get("actionType") == "ActionType_Activate" for o in decision.options
    ):
        return {}
    local = _local_seat(state)
    board = [
        c
        for c in state.get("battlefield") or []
        if c.get("controller_seat_id", c.get("owner_seat_id")) == local
    ]
    if local is None:
        return {}
    before, known = _sources(board, state, now=False)
    if not known:
        return {}
    have = {
        color for source in before for output in source for color, n in zip(COLORS, output, strict=True) if n
    }
    needed = {
        color
        for _, cost in _spells(state)
        if cost[0] <= _capacity(before) + 2
        for color, n in zip(COLORS, cost[1], strict=True)
        if n
    }
    losses = {}
    for option in decision.options:
        if option.meta.get("actionType") != "ActionType_Activate":
            continue
        card = _card_for(option, state)
        if not _land(card):
            continue
        cost, effect = _offered_ability(card, option.meta)
        subject = "(?:this land|this permanent|~|" + re.escape(str(card.get("name") or "?")) + ")"
        if not re.search(r"\bsacrifice " + subject + r"(?:\b|$)", cost, re.I):
            continue
        # Fetches replace the land; their actual target menu is compared when
        # it arrives. Unknown mana payoffs cannot be priced as a plain loss.
        if re.search(r"search your library|\badd\b|return .*land", effect, re.I):
            continue
        after = _remove(board, card)
        if after is None:
            continue
        sources, known = _sources(after, state, now=False)
        available = {
            color
            for source in sources
            for output in source
            for color, n in zip(COLORS, output, strict=True)
            if n
        }
        missing = needed & (have - available)
        if known and missing:
            from arenamcp.board_assessment import face_damage

            aimed, each = face_damage({"name": card.get("name"), "oracle_text": effect})
            opponent_life = [
                p.get("life_total") for p in state.get("players") or [] if p.get("seat_id") != local
            ]
            possible_lethal = any(
                type(life) is int and 0 < life <= max(aimed, each) for life in opponent_life
            )
            note = (
                f"{option.option_id} {card.get('name')}: sacrificing this land loses the only "
                + "/".join(sorted(missing))
                + " source the hand/commander needs."
                + (
                    " Printed damage can reach the opponent's life total."
                    if possible_lethal
                    else " Weigh the ability's payoff."
                )
            )
            losses[option.option_id] = (note, option.payable is False or not _damage_tradeoff(effect, state))
    return losses


def filter_land_choices(decision, state, *, lethal=False):
    """Withhold strictly worse colour choices before every planner/fallback path.

    Multi-card selections are retained: removing a single option cannot prove
    that a whole count/weight-constrained combination remains legal.
    """
    if decision.request_type == "ActionsAvailable" and not any(
        option.meta.get("actionType") in ("ActionType_Play", "ActionType_Activate")
        and _land(_card_for(option, state))
        for option in decision.options
    ):
        return decision, []
    if decision.request_type not in {"ActionsAvailable", "Search", "SelectN"}:
        return decision, []
    if (
        decision.request_type == "SelectN"
        and "sacrific" not in (decision.prompt_text + " " + decision.source_label).lower()
    ):
        return decision, []
    if len(decision.options) > 128:
        return decision, []

    from arenamcp.card_facts.client import facts_for, hash_for_card

    cards = [
        c
        for c in [
            *(state.get("battlefield") or []),
            *(state.get("hand") or []),
            *(_card_for(o, state) for o in decision.options),
        ]
        if isinstance(c, dict) and _land(c) and type(c.get("instance_id")) is int
    ]
    records = facts_for(cards, memory_only=True) if cards else {}
    if records:
        state = dict(
            state,
            _land_mana_records={
                c["instance_id"]: records[digest] for c in cards if (digest := hash_for_card(c)) in records
            },
        )
    choices, names = land_choices(decision, state)
    activations = _activation_losses(decision, state)

    if not choices and not activations:
        return decision, []
    notes = [
        "LAND COLOURS: mana-only forecast, each spell separately; this turn and our next "
        "turn use visible lands only. Unknown effects and real trade-offs need judgment."
    ]
    notes.extend(note for note, _withhold in activations.values())
    for row in choices:
        now = ", ".join(names[i] for i in sorted(row.now)) or "none"
        later = ", ".join(names[i] for i in sorted(row.later)) or "none"
        lost = " loses the only " + "/".join(sorted(row.lost)) + " source needed" if row.lost else ""
        notes.append(
            f"{row.option_id} {row.name}: mana pays now: {now}; next turn: {later};"
            f"{lost or ' no required colour removed'}"
            + (" (incomplete forecast; keep for judgment)." if not row.certain else ".")
        )
    if decision.max_select != 1:
        return decision, notes
    # Do not compare a land tutor against an unknown/nonland option's value.
    safe = {r.option_id for r in choices if decision.selection_is_valid([r.option_id])}
    withheld = {
        r.option_id
        for r in choices
        if r.option_id in safe and any(better.option_id in safe and _better(better, r) for better in choices)
    }
    withheld.update(oid for oid, (_note, withhold) in activations.items() if withhold)
    kept = tuple(o for o in decision.options if o.option_id not in withheld)
    if withheld and any(o.payable is not False and decision.selection_is_valid([o.option_id]) for o in kept):
        if (lethal() if callable(lethal) else lethal) is not False:
            return decision, notes
        notes.append(
            "WITHHELD land choices that strand or delay required colours without a "
            "supported mana benefit: " + ", ".join(sorted(withheld)) + "."
        )
        return replace(decision, options=kept), notes
    return decision, notes


def guard_land_sacrifice(decision, state, chosen, *, lethal=False):
    """Check a complete land-cost selection, including repeated-colour sources.

    Search is bounded; large/unknown requests keep their colour facts for the
    model rather than silently inventing a legal payment.
    """
    from itertools import combinations, islice

    if (
        decision.request_type != "SelectN"
        or not decision.selection_is_valid(chosen)
        or "sacrific" not in (decision.prompt_text + " " + decision.source_label).lower()
    ):
        return chosen, ""
    cards = {o.option_id: _card_for(o, state) for o in decision.options}
    if not cards or any(not _land(c) for c in cards.values()):
        return chosen, ""
    local = _local_seat(state)
    board = [
        c
        for c in state.get("battlefield") or []
        if c.get("controller_seat_id", c.get("owner_seat_id")) == local
    ]
    sources, certain = _sources(board, state, now=False)
    if not certain:
        return chosen, ""
    horizon = _capacity(sources) + 2
    needed = {
        color
        for _, cost in _spells(state)
        if cost[0] <= horizon
        for color, count in zip(COLORS, cost[1], strict=True)
        if count
    }

    def available(source_list):
        return {
            color
            for source in source_list
            for output in source
            for color, count in zip(COLORS, output, strict=True)
            if count
        }

    have = available(sources)

    def lost(selection):
        after = board
        for oid in selection:
            after = _remove(after, cards[oid])
            if after is None:
                return None
        remaining, known = _sources(after, state, now=False)
        return needed & (have - available(remaining)) if known else None

    missing = lost(chosen)
    if not missing or (lethal() if callable(lethal) else lethal) is not False:
        return chosen, ""
    # Optional extra sacrifices can scale the payoff. Preserve the selected count.
    eligible = (o.option_id for o in decision.options if o.payable is not False)
    candidates = (list(combo) for combo in combinations(eligible, len(chosen)))
    for candidate in islice(candidates, 256):
        newly_sacrificed = set(candidate) - set(chosen)
        if any(_has_unpriced_payoff(cards[oid], state) for oid in newly_sacrificed):
            continue
        if decision.selection_is_valid(candidate) and lost(candidate) == set():
            reason = (
                "Preserve the only "
                + "/".join(sorted(missing))
                + " source needed by the hand/commander; sacrifice "
                + ", ".join(str(cards[oid].get("name") or oid) for oid in candidate)
                + " instead."
            )
            return candidate, reason
    return chosen, ""
