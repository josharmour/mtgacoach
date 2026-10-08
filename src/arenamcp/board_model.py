"""Per-board facts shared by the board assessment and the line search.

``build_board_model`` computes once what ``board_assessment._assess`` computed
inline (seats, turn, phase/step, life, creature bodies, attack timing, mana,
land drops, the hand's spells). Pure: no I/O, no LLM, no logging; it reads
only planner-snapshot fields. Phase/step come back in log form
("Phase_Main1"): bridge snapshots carry ``CurrentPhase.ToString()`` ("Main1",
step "None"), which the timing rules never matched.

Commander games (Brawl): our commanders in the command zone (``our_commanders``)
are spells like the hand's (``_Spell.zone`` "command"), priced at what casting
them costs now: Arena's own cast action when the snapshot has one (its
``manaCost`` includes the tax), else the printed cost plus {2} per previous cast
from the command zone (``commander_casts``; unknown counts as none, noted in
``unknowns``). One whose cost is unknown is not a spell (``commanders`` keeps
it). Without command-zone cards the model is what it was.

The model is frozen, but the cards, bodies and ``_Spell`` objects it holds are
shared and mutable: copy before changing them. ``board_assessment._schedule``
overwrites ``_Spell.value`` on every run, so never rely on it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType, SimpleNamespace
from typing import Any

from arenamcp.board_assessment import (
    _body,
    _controller,
    _int,
    _is_creature,
    _is_land,
    _life,
    _mana_source,
    _name,
    _opponent_hand,
    _player,
    _seats,
    _side_rules,
    _Spell,
    _text,
    _types,
    card_role,
    extra_mana_cost,
)
from arenamcp.combat_keywords import has_combat_keyword, printed_combat_keywords
from arenamcp.mulligan_policy import _land_colors, _mana_value, _symbols, hand_card

_PHASES = {name.lower(): name for name in ("Beginning", "Main1", "Combat", "Main2", "Ending")}
_STEPS = {
    name.lower(): name
    for name in (
        "Untap", "Upkeep", "Draw", "BeginCombat", "DeclareAttack", "DeclareBlock",
        "FirstStrikeDamage", "CombatDamage", "EndCombat", "End", "Cleanup",
    )
}  # fmt: skip


def _canonical(value: Any, prefix: str, known: dict[str, str]) -> str:
    text = str(value or "").strip()
    if text[: len(prefix)].lower() == prefix.lower():
        text = text[len(prefix) :]
    if not text or text.lower() == "none":
        return ""
    return prefix + known.get(text.lower(), text)


def canonical_phase_step(turn_info: Mapping[str, Any] | None) -> tuple[str, str]:
    """``(phase, step)`` in log form: 'Main1' -> 'Phase_Main1', 'None'/'' -> ''.

    Idempotent on log names, so it composes with ``concede.normalize_phases``.
    """
    turn_info = turn_info if isinstance(turn_info, Mapping) else {}
    return (
        _canonical(turn_info.get("phase"), "Phase_", _PHASES),
        _canonical(turn_info.get("step"), "Step_", _STEPS),
    )


def able_now(bodies: tuple[dict, ...] | list[dict]) -> list[dict]:
    """The creatures attacking now, else those untapped and not summoning sick."""
    if any(b["_attacking"] for b in bodies):
        return [b for b in bodies if b["_attacking"]]
    return [b for b in bodies if not b["_tapped"] and not b["_sick"]]


_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}  # fmt: skip
_GRAVEYARD_GATE = re.compile(r"can't cast this spell unless there are (\w+) or more cards in your graveyard")


def _cast_gate_unmet(card: dict, graveyard: int) -> bool:
    """A Threshold-style "You can't cast this spell unless ..." condition the snapshot shows unmet."""
    match = _GRAVEYARD_GATE.search(_text(card))
    if not match:
        return False
    word = match.group(1)
    needed = _NUMBERS.get(word, _int(word) or 0)
    return graveyard < needed


_STRIKES = frozenset({"first strike", "double strike"})
_STRIKE_TEXT = re.compile(r"\b(?:first|double) strike\b", re.IGNORECASE)


def _regular_damage_part(attackers: list[dict]) -> list[dict]:
    """The part of an attack still to deal damage after its first-strike step.

    That damage is already in our life total and the blockers it killed are
    off the board. First strikers are done; a double striker deals its damage
    once more, as a copy with neither strike keyword (``has_combat_keyword``
    reads ``keywords`` and the rules text, so both lose them).
    """
    rest = []
    for body in attackers:
        if has_combat_keyword(body, "double strike"):
            kept = [k for k in body["keywords"] if str(k).lower() not in _STRIKES]
            printed = printed_combat_keywords(body["oracle_text"]) - _STRIKES - {str(k).lower() for k in kept}
            text = _STRIKE_TEXT.sub("", body["oracle_text"])
            rest.append({**body, "keywords": kept + sorted(printed), "oracle_text": text})
        elif not has_combat_keyword(body, "first strike"):
            rest.append(body)
    return rest


# --- commanders in the command zone -------------------------------------------------------

_MANA_COLORS = {
    "ManaColor_White": "W", "ManaColor_Blue": "U", "ManaColor_Black": "B", "ManaColor_Red": "R",
    "ManaColor_Green": "G", "ManaColor_Colorless": "C",
}  # fmt: skip
# Command-zone objects that are never cast: emblems, dungeons and other non-card objects.
_NOT_CAST_KINDS = ("ability", "emblem", "token", "boon", "trigger")
_NOT_CAST_TYPES = re.compile(r"\b(?:emblem|dungeon|plane|phenomenon|scheme|conspiracy|vanguard)\b")
_CASTABLE_TYPES = re.compile(r"\b(?:creature|planeswalker|artifact|enchantment|instant|sorcery|battle)\b")


@dataclass(frozen=True)
class CommandCast:
    """One of our commanders in the command zone, priced for a cast now."""

    card: dict  # the command-zone card (a copy when Arena's cast action supplied its mana cost)
    name: str
    cost: str  # what casting it costs now, tax included ("{5}{G}{G}"); "" when unknown
    printed: str  # the printed mana cost; "" when unknown
    casts: int | None  # previous casts from the command zone; None when unknown
    from_action: bool  # ``cost`` is Arena's own (the cast action's manaCost, tax included)
    text_unknown: bool  # no card data: its rules text (and printed cost) are unknown

    @property
    def tax(self) -> int | None:
        return None if self.casts is None else 2 * self.casts

    @property
    def mana_value(self) -> int | None:
        """Mana to cast it now, the additional mana cost included; None when the cost is unknown."""
        return _mana_value(self.cost) + extra_mana_cost(self.card) if self.cost else None


def _action_cost(entries: Any) -> str:
    """A GRE ``manaCost`` array as a braced cost: [{color: [Generic], count: 3}, {color: [Green],
    count: 2}] -> "{3}{G}{G}" (as ``gamestate_decisions._braced_mana_cost``). The Mac bridge
    writes ``color`` as one enum name or a '[ "ManaColor_White", ... ]' string."""
    generic, colored = 0, []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        count = _int(entry.get("count"))
        count = 1 if count is None else count
        if count <= 0:
            continue
        colors = entry.get("color") or []
        names = colors if isinstance(colors, list) else re.findall(r"ManaColor_\w+", str(colors))
        symbols = [_MANA_COLORS[c] for c in names if c in _MANA_COLORS]
        if not symbols:
            generic += count
        else:
            colored += [f"{{{'/'.join(symbols)}}}"] * count
    return (f"{{{generic}}}" if generic else "") + "".join(colored)


def _with_generic(cost: str, amount: int) -> str:
    """``cost`` with ``amount`` more generic mana (the tax; negative: Arena's taxed cost back to
    the printed one); "" when that would leave less than none."""
    symbols = _symbols(cost)
    generic = sum(int(s) for s in symbols if s.isdigit()) + amount
    if generic < 0:
        return ""
    rest = "".join(f"{{{s}}}" for s in symbols if not s.isdigit())
    return (f"{{{generic}}}" if generic else "") + rest


def _cast_action_cost(state: dict, instance_id: int | None) -> str:
    """The braced manaCost of Arena's cast action for this card ("" without one)."""
    if instance_id is None:
        return ""
    for key in ("_bridge_actions", "legal_actions_raw"):
        for action in state.get(key) or []:
            if (
                isinstance(action, dict)
                and str(action.get("actionType") or "").removeprefix("ActionType_").lower() == "cast"
                and _int(action.get("instanceId")) == instance_id
            ):
                cost = _action_cost(action.get("manaCost"))
                if cost:
                    return cost
    return ""


def _casts_so_far(state: dict, card: dict) -> int | None:
    """Previous casts from the command zone: the snapshot's ``commander_casts`` (by grp id, int or
    str keys), else the card's own; None when neither says."""
    casts = state.get("commander_casts")
    if isinstance(casts, Mapping):
        for grp in (card.get("grp_id"), card.get("base_grp_id")):
            for key in (grp, str(grp)) if grp is not None else ():
                value = _int(casts.get(key))
                if value is not None and value >= 0:
                    return value
    value = _int(card.get("commander_casts"))
    return value if value is not None and value >= 0 else None


def _command_cards(state: dict) -> list[dict]:
    cards = state.get("command")
    if cards is None:
        zones = state.get("zones") if isinstance(state.get("zones"), dict) else {}
        cards = zones.get("command")
    return [c for c in cards or [] if isinstance(c, dict)]


def our_commanders(state: dict, local: int | None = None) -> list[CommandCast]:
    """Our commanders in the command zone (``state['command']``), priced for a cast now.

    Ours: owned (or controlled) by our seat; a card without a seat (Mac bridge
    snapshots) when our player's ``commander_ids`` (instance ids) or the
    match's ``commander_grp_ids`` name it and the opponent's ``commander_ids``
    do not. A commander: one ``commander_grp_ids`` / ``commander_ids`` name, or,
    when neither is known, any card of ours there of a type that is cast
    (emblems, dungeons, abilities and tokens never are). The cost: Arena's
    cast action when the snapshot has one (its manaCost includes the tax),
    else the printed cost plus {2} per previous cast (``commander_casts``;
    unknown is assumed none). Without card data (no printed cost and no rules
    text) the printed cost is the action's less the tax, and ``text_unknown``
    is set: the card copy carries ``_card_unknown`` so ``unmodelled_effect``
    can say so.
    """
    cards = _command_cards(state)
    if not cards:
        return []
    if local is None:
        local, _opponent = _seats(state)
    if local is None:
        return []
    players = [p for p in state.get("players") or [] if isinstance(p, dict)]
    ours_ids = {
        _int(i) for p in players if p.get("seat_id") == local for i in p.get("commander_ids") or []
    } - {None}
    theirs_ids = {
        _int(i) for p in players if p.get("seat_id") != local for i in p.get("commander_ids") or []
    } - {None}
    grp_ids = {_int(g) for g in state.get("commander_grp_ids") or []} - {None}
    designated_known = bool(ours_ids or grp_ids)
    found: list[CommandCast] = []
    for card in cards:
        kind = str(card.get("object_kind") or "").lower()
        if any(word in kind for word in _NOT_CAST_KINDS) or _NOT_CAST_TYPES.search(_types(card)):
            continue
        iid = _int(card.get("instance_id"))
        grps = {_int(card.get("grp_id")), _int(card.get("base_grp_id"))} - {None}
        designated = (iid is not None and iid in ours_ids) or bool(grps & grp_ids)
        owner = _int(card.get("owner_seat_id")) or _int(card.get("controller_seat_id"))
        if owner is not None:
            if owner != local:
                continue
        elif not designated or (iid is not None and iid in theirs_ids):
            continue
        if designated_known and not designated:
            continue
        printed = str(card.get("mana_cost") or "")
        action = _cast_action_cost(state, iid)
        text_unknown = not printed and not str(card.get("oracle_text") or "").strip()
        if not designated and not (printed or action) and not _CASTABLE_TYPES.search(_types(card)):
            continue  # not a designated commander, nor a card we could cast
        casts = _casts_so_far(state, card)
        if action:
            cost = action
            if not printed and casts is not None:
                printed = _with_generic(action, -2 * casts)
        elif printed:
            cost = _with_generic(printed, 2 * casts) if casts else printed
        else:
            cost = ""
        if text_unknown or (printed and not card.get("mana_cost")):
            card = {**card, "mana_cost": printed}
            if text_unknown:
                card["_card_unknown"] = True
        found.append(
            CommandCast(
                card=card, name=_name(card), cost=cost, printed=printed, casts=casts,
                from_action=bool(action), text_unknown=text_unknown,
            )
        )  # fmt: skip
    return found


@dataclass(frozen=True)
class BoardModel:
    """One snapshot's board facts; see the module docstring for the mutation rule."""

    local: int
    opponent: int
    turn: int
    our_turn: bool
    phase: str  # log form, "" when unknown
    step: str  # log form, "" when unknown
    our_life: int
    opp_life: int
    battlefield: tuple[dict, ...]
    hand: tuple[dict, ...]
    attached: Mapping[int, tuple[dict, ...]]  # host instance id -> attachments
    our_rules: Mapping[str, Any]
    their_rules: Mapping[str, Any]
    ours: tuple[dict, ...]
    theirs: tuple[dict, ...]
    unknown_bodies: tuple[str, ...]  # names of creatures with unknown P/T
    any_ours_attacking: bool
    any_theirs_attacking: bool
    pre_combat: bool
    in_combat_before_damage: bool
    our_attack_pending: bool
    their_attack_pending: bool
    first_our_attackers: tuple[dict, ...] | None  # able_now(ours) when our attack is pending
    # able_now(theirs) when their attack is pending; at Step_FirstStrikeDamage
    # only its regular-damage part (see _regular_damage_part)
    first_their_attackers: tuple[dict, ...] | None
    untapped_ours: tuple[dict, ...]
    untapped_theirs: tuple[dict, ...]
    our_first_blockers: tuple[dict, ...]
    our_permanents: tuple[dict, ...]
    sources_now: tuple[SimpleNamespace, ...]
    sources_all: tuple[SimpleNamespace, ...]
    our_lands: int
    their_lands: int
    hand_lands: tuple[dict, ...]
    lands_played: int
    land_drop_now: bool
    land_drop_available: bool
    colors_all: frozenset[str]
    spells: tuple[_Spell, ...]
    missing_colors: tuple[str, ...]  # sorted
    has_x_spells: bool
    their_hand: int | None
    stack_nonempty: bool
    our_graveyard: int  # cards in our graveyard (Threshold-style cast conditions)
    # Our creature spells on the stack, as bodies: they enter before T's attack.
    our_stack_bodies: tuple[dict, ...]
    t_casts_pre_combat: bool  # this turn's casts can come before our attack
    t_casts_post_combat: bool  # our attack is over: casts come after it
    t_instant_only: bool  # our ending phase: instant-speed plays only
    # Our commanders in the command zone (``our_commanders``); those with a known cost are
    # also in ``spells`` (zone "command"). Empty outside commander games.
    commanders: tuple[CommandCast, ...] = ()

    @property
    def unknowns(self) -> list[str]:
        """The ``unknowns`` entries ``_assess`` derives from these facts, in order."""
        notes = [f"{name} has unknown power/toughness" for name in self.unknown_bodies]
        if self.has_x_spells:
            notes.append("X spells are not scheduled")
        for commander in self.commanders:
            if not commander.cost:
                notes.append(f"our commander {commander.name}: cost unknown (not cast in the lines)")
            elif commander.casts is None and not commander.from_action:
                notes.append(
                    f"our commander {commander.name}: commander tax unknown (no previous casts assumed)"
                )
            if commander.text_unknown:
                notes.append(f"our commander {commander.name}: rules text unknown")
        return notes


def build_board_model(state: dict) -> BoardModel | None:
    """The board facts of a planner-shape snapshot.

    None when the seats or the turn are unknown, or a creature's controller is:
    the Mac bridge's snapshots can come without any card's seat (14 of 59
    bridge-named bug reports by 2026-10-06), and an unseated creature would be
    counted as the opponent's, ours included.
    """
    local, opponent = _seats(state)
    turn_info = state.get("turn") or {}
    turn = _int(turn_info.get("turn_number")) or 0
    if local is None or opponent is None or turn <= 0:
        return None
    our_turn = _int(turn_info.get("active_player")) == local
    phase, step = canonical_phase_step(turn_info)
    battlefield = [c for c in state.get("battlefield") or [] if isinstance(c, dict)]
    hand = [c for c in state.get("hand") or [] if isinstance(c, dict)]
    unseated = [c for c in battlefield if _controller(c) is None]
    if any(_is_creature(c) for c in unseated) or (battlefield and len(unseated) == len(battlefield)):
        return None

    attached: dict[int, list[dict]] = {}
    for card in battlefield:
        target = _int(card.get("attached_to_id"))
        if target:
            attached.setdefault(target, []).append(card)

    our_rules, their_rules = _side_rules(battlefield, local), _side_rules(battlefield, opponent)
    ours: list[dict] = []
    theirs: list[dict] = []
    unknown_bodies: list[str] = []
    for card in battlefield:
        if not _is_creature(card):
            continue
        body = _body(card, turn, our_rules if _controller(card) == local else their_rules, attached=attached)
        if body is None:
            unknown_bodies.append(_name(card))
            continue
        (ours if _controller(card) == local else theirs).append(body)

    any_ours_attacking = any(b["_attacking"] for b in ours)
    any_theirs_attacking = any(b["_attacking"] for b in theirs)
    pre_combat = phase in ("Phase_Beginning", "Phase_Main1") or (
        phase == "Phase_Combat" and step in ("", "Step_BeginCombat", "Step_DeclareAttack")
    )
    in_combat_before_damage = phase == "Phase_Combat" and step not in ("Step_CombatDamage", "Step_EndCombat")
    our_attack_pending = our_turn and pre_combat and not any_ours_attacking
    their_attack_pending = (not our_turn) and (
        pre_combat or (in_combat_before_damage and any_theirs_attacking)
    )
    first_their_attackers = able_now(theirs) if their_attack_pending else None
    if first_their_attackers is not None and step == "Step_FirstStrikeDamage":
        first_their_attackers = _regular_damage_part(first_their_attackers)
        if not first_their_attackers:  # only first strikers: their damage is done
            their_attack_pending, first_their_attackers = False, None
    untapped_ours = [b for b in ours if not b["_tapped"]]
    untapped_theirs = [b for b in theirs if not b["_tapped"]]
    # Our blockers for their next attack: what is untapped now when that
    # attack comes before our untap step, otherwise everything.
    our_first_blockers = untapped_ours if (our_turn or their_attack_pending) else list(ours)

    our_permanents = [c for c in battlefield if _controller(c) == local]
    sources_all = [s for c in our_permanents if (s := _mana_source(c, turn + 1))]
    sources_now = [s for c in our_permanents if not c.get("is_tapped") and (s := _mana_source(c, turn))]
    hand_lands = [c for c in hand if _is_land(c) and not _is_creature(c)]
    lands_played = _int(_player(state, local).get("lands_played")) or 0
    land_drop_now = (not our_turn) or lands_played == 0
    colors_all = set().union(*(s.produces for s in sources_all)) if sources_all else set()
    for land in hand_lands:
        colors_all |= set(_land_colors(land))

    our_graveyard = sum(
        1
        for c in state.get("graveyard") or []
        if isinstance(c, dict) and c.get("owner_seat_id", c.get("controller_seat_id")) == local
    )
    spells: list[_Spell] = []
    for card in hand:
        if _is_land(card) and not _is_creature(card):
            continue
        info = hand_card(card)
        cost = str(card.get("mana_cost") or "")
        if not cost and not _is_creature(card):
            continue
        spells.append(
            _Spell(
                card=card,
                name=_name(card),
                role=card_role(card),
                # "As an additional cost ..., pay {3}": the mana is part of what it costs.
                mana_value=info.mana_value + extra_mana_cost(card),
                pips=info.pips,
                has_x="x" in cost.lower(),
                uncastable=_cast_gate_unmet(card, our_graveyard),
            )
        )
    # Our commanders in the command zone: cast like hand cards, at their current cost.
    commanders = our_commanders(state, local)
    for commander in commanders:
        if not commander.cost:
            continue  # unknown cost: not cast in the lines (``unknowns`` says so)
        card = commander.card
        info = hand_card({**card, "mana_cost": commander.cost, "rarity": card.get("rarity") or "-"})
        spells.append(
            _Spell(
                card=card,
                name=commander.name,
                role=card_role(card),
                mana_value=info.mana_value + extra_mana_cost(card),
                pips=info.pips,
                has_x="x" in (commander.printed or commander.cost).lower(),
                uncastable=_cast_gate_unmet(card, our_graveyard),
                zone="command",
            )
        )
    our_stack_bodies = []
    for card in state.get("stack") or []:
        if not isinstance(card, dict) or _controller(card) != local or not _is_creature(card):
            continue
        if "ability" in str(card.get("object_kind") or card.get("type_line") or "").lower():
            continue
        body = _body(card, turn, our_rules)
        if body is not None:
            our_stack_bodies.append({**body, "_sick": True, "_tapped": False, "_attacking": False})
    missing = sorted({c for s in spells for pip in s.pips for c in pip if not (pip & colors_all)})

    their_lands = sum(
        1 for c in battlefield if _controller(c) == opponent and _is_land(c) and not _is_creature(c)
    )
    return BoardModel(
        local=local, opponent=opponent, turn=turn, our_turn=our_turn, phase=phase, step=step,
        our_life=_life(state, local), opp_life=_life(state, opponent),
        battlefield=tuple(battlefield), hand=tuple(hand),
        attached=MappingProxyType({k: tuple(v) for k, v in attached.items()}),
        our_rules=MappingProxyType(our_rules), their_rules=MappingProxyType(their_rules),
        ours=tuple(ours), theirs=tuple(theirs), unknown_bodies=tuple(unknown_bodies),
        any_ours_attacking=any_ours_attacking, any_theirs_attacking=any_theirs_attacking,
        pre_combat=pre_combat, in_combat_before_damage=in_combat_before_damage,
        our_attack_pending=our_attack_pending, their_attack_pending=their_attack_pending,
        first_our_attackers=tuple(able_now(ours)) if our_attack_pending else None,
        first_their_attackers=None if first_their_attackers is None else tuple(first_their_attackers),
        untapped_ours=tuple(untapped_ours), untapped_theirs=tuple(untapped_theirs),
        our_first_blockers=tuple(our_first_blockers), our_permanents=tuple(our_permanents),
        sources_now=tuple(sources_now), sources_all=tuple(sources_all),
        our_lands=sum(1 for c in our_permanents if _is_land(c) and not _is_creature(c)),
        their_lands=their_lands, hand_lands=tuple(hand_lands), lands_played=lands_played,
        land_drop_now=land_drop_now, land_drop_available=bool(hand_lands) and land_drop_now,
        colors_all=frozenset(colors_all), spells=tuple(spells), missing_colors=tuple(missing),
        has_x_spells=any(s.has_x for s in spells),
        their_hand=_opponent_hand(state), stack_nonempty=bool(state.get("stack")),
        our_graveyard=our_graveyard, our_stack_bodies=tuple(our_stack_bodies),
        t_casts_pre_combat=(not our_turn) or phase in ("Phase_Beginning", "Phase_Main1"),
        t_casts_post_combat=our_turn and phase in ("Phase_Combat", "Phase_Main2"),
        t_instant_only=our_turn and phase == "Phase_Ending",
        commanders=tuple(commanders),
    )  # fmt: skip
