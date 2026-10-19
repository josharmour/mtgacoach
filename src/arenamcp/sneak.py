"""Sneak: a creature cast in our declare-blockers step by returning an unblocked attacker.

"Sneak {cost}" (Teenage Mutant Ninja Turtles): during the declare blockers
step of our turn we may cast the card for its Sneak cost by also returning an
unblocked attacking creature we control to its owner's hand; a creature cast
this way enters tapped and attacking. A commander cast from the command zone
this way still pays the commander tax ({2} per earlier cast).

Leonardo, Big Brother (Arena grp 100471, "Sneak {oW}" and "Leonardo gets
+1/+0 for each other creature you control.") is the Brawl case that cost
match be1a945f (2026-10-19 T12, ledger #156): with 15 attackers into four
blockers and their life at 17, keeping {2}{W} for a Sneak Leonardo after
blocks was 26 to 32 damage; the planner spent the mana on a second Hare
Apparent instead, because nothing in the coach knew the mechanic existed.

What this module answers, all from planner snapshots (log-first, the bridge
only names the request):

- ``sneak_cost`` / ``sneak_casts``: which cards in hand or our command zone
  have Sneak and what it costs now (tax included).
- ``per_creature_bonus`` / ``scaled_power``: "+N/+0 for each other creature
  you control" recomputed for a board (the line search uses it at each node).
- ``sneak_now``: in our declare-blockers step, with blocks known, the damage a
  Sneak cast adds and whether it is lethal, or beats casting the card
  normally by half their life (``beats_hard_cast``, ledger #179).
- ``sneak_main_hold``: before combat, whether attacking with everything plus a
  Sneak after their best blocks is lethal (or beats the hard cast by half
  their life), and how much mana to keep for it.
- ``return_choice``: which unblocked attacker the Sneak cost returns
  (a recastable card over a token, which just vanishes; never the one lethal
  needs).

Pure and deterministic; nothing here calls a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from arenamcp.mulligan_policy import _mana_value, _symbols

# "Sneak {oW}" (Arena) / "Sneak {W}" (Scryfall); the cost runs to the end of its brace groups.
_SNEAK = re.compile(r"\bsneak\s+((?:\{[^}]*\}\s*)+)", re.IGNORECASE)
# "Leonardo gets +1/+0 for each other creature you control." ("~" after board_assessment._text).
_PER_CREATURE = re.compile(
    r"(?:(?<!\w)~|\bthis creature|\bit)\s+gets?\s+\+(?P<p>\d+)/\+(?P<t>\d+)\s+for each (?P<other>other )?"
    r"creature you control\b"
)
# Arena's prompt 8580 for the Sneak cost: "Return an unblocked attacking creature you control to
# its owner's hand."
RETURN_PROMPT = re.compile(r"\breturn an unblocked attacking creature\b", re.IGNORECASE)
SNEAK_PROMPT_ID = 8580


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, dict) and "value" in value:
        value = value.get("value")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _oracle(card: dict | None) -> str:
    return str((card or {}).get("oracle_text") or "")


def _types(card: dict) -> str:
    return f"{card.get('type_line') or ''} {' '.join(str(t) for t in card.get('card_types') or [])}".lower()


def _is_creature(card: dict) -> bool:
    return "creature" in _types(card)


def _is_land(card: dict) -> bool:
    return bool(re.search(r"\bland\b", _types(card).replace("cardtype_land", "land")))


def _controller(card: dict) -> int | None:
    return card.get("controller_seat_id") or card.get("owner_seat_id")


def _is_token(card: dict) -> bool:
    return "token" in str(card.get("object_kind") or "").lower() or "token" in _types(card)


def braced(cost: str) -> str:
    """Arena's packed symbols as plain braces: "{oW}" -> "{W}", "{o2oW}" -> "{2}{W}"."""
    symbols = _symbols(cost)
    generic = sum(int(s) for s in symbols if s.isdigit())
    rest = "".join(f"{{{s}}}" for s in symbols if not s.isdigit())
    return (f"{{{generic}}}" if generic else "") + rest


def sneak_cost(card: dict | None) -> str:
    """The card's printed Sneak cost, braced ("{W}"); "" when it has no Sneak."""
    match = _SNEAK.search(_oracle(card))
    return braced(match.group(1)) if match else ""


def with_tax(cost: str, casts: int | None) -> str:
    """``cost`` plus {2} per earlier cast from the command zone."""
    tax = 2 * max(0, casts or 0)
    if not tax:
        return cost
    symbols = _symbols(cost)
    generic = sum(int(s) for s in symbols if s.isdigit()) + tax
    return f"{{{generic}}}" + "".join(f"{{{s}}}" for s in symbols if not s.isdigit())


def is_sneak_ability(text: str) -> bool:
    """An alternative-cost ability text that is Sneak ("Sneak {oW}")."""
    return bool(_SNEAK.match(str(text or "").strip()))


@dataclass(frozen=True)
class SneakCast:
    """A card we could cast with Sneak this turn."""

    card: dict
    name: str
    printed: str  # the printed Sneak cost ("{W}")
    cost: str  # what it costs now, commander tax included ("{2}{W}")
    zone: str  # "hand" or "command"
    tax: int
    # What casting it normally costs now ("{2}{W}" printed, the command zone's with tax, Arena's
    # own cast action when the snapshot has one: discounts included); "" when unknown.
    hard_cost: str = ""

    @property
    def mana_value(self) -> int:
        return _mana_value(self.cost)

    @property
    def hard_mana(self) -> int | None:
        return _mana_value(self.hard_cost) if self.hard_cost else None


def sneak_casts(state: dict | None) -> list[SneakCast]:
    """Our Sneak cards: in hand, and our commanders in the command zone (tax included)."""
    if not isinstance(state, dict):
        return []
    found: list[SneakCast] = []
    for card in state.get("hand") or []:
        if isinstance(card, dict) and _is_creature(card):
            printed = sneak_cost(card)
            if printed:
                hard = str(card.get("mana_cost") or "")
                found.append(SneakCast(card, str(card.get("name") or ""), printed, printed, "hand", 0, hard))
    try:
        from arenamcp.board_model import our_commanders

        commanders = our_commanders(state)
    except Exception:
        commanders = []
    for commander in commanders:
        printed = sneak_cost(commander.card)
        if printed and _is_creature(commander.card):
            casts = commander.casts or 0
            found.append(
                SneakCast(
                    commander.card, commander.name, printed, with_tax(printed, casts), "command", 2 * casts,
                    commander.cost,
                )
            )
    return found


def per_creature_bonus(card: dict | None) -> tuple[int, int, bool] | None:
    """(+power, +toughness, counts only *other* creatures) per creature we control, or None.

    "Leonardo gets +1/+0 for each other creature you control." -> (1, 0, True).
    """
    if not isinstance(card, dict):
        return None
    text = re.sub(r"<[^>]*>", "", _oracle(card)).lower()
    name = str(card.get("name") or "").lower()
    if name:
        text = text.replace(name, "~")
        short = name.split(",")[0].strip()
        if short and short != name:
            text = text.replace(short, "~")
    match = _PER_CREATURE.search(text)
    if not match:
        return None
    return int(match.group("p")), int(match.group("t")), bool(match.group("other"))


def scaled_stats(card: dict, base: tuple[int, int], creatures: int) -> tuple[int, int]:
    """(power, toughness) of ``card`` with ``creatures`` creatures under our control, itself
    included when it is on the battlefield; ``base`` its stats without the bonus."""
    bonus = per_creature_bonus(card)
    if bonus is None:
        return base
    per_p, per_t, other = bonus
    counted = max(0, creatures - (1 if other else 0))
    return base[0] + per_p * counted, base[1] + per_t * counted


def entry_power(card: dict, creatures_after: int) -> int:
    """The power ``card`` has once it is on the battlefield with ``creatures_after`` creatures of
    ours, itself included (its printed power when the snapshot's is unknown)."""
    power = _int(card.get("power"))
    toughness = _int(card.get("toughness"))
    base = (power if power is not None else 0, toughness if toughness is not None else 0)
    return max(0, scaled_stats(card, base, creatures_after)[0])


# --- the snapshot -------------------------------------------------------------------------


def _seats(state: dict) -> tuple[int | None, int | None]:
    players = [p for p in state.get("players") or [] if isinstance(p, dict)]
    local = state.get("local_seat_id") or next((p.get("seat_id") for p in players if p.get("is_local")), None)
    opponent = next(
        (p.get("seat_id") for p in players if p.get("seat_id") is not None and p.get("seat_id") != local),
        None,
    )
    return local, opponent


def _life(state: dict, seat: int | None) -> int | None:
    for player in state.get("players") or []:
        if isinstance(player, dict) and player.get("seat_id") == seat:
            life = player.get("life_total", player.get("life"))
            return life if isinstance(life, int) and not isinstance(life, bool) else None
    return None


def _norm(value: Any) -> str:
    return re.sub(r"^(?:phase|step)_", "", str(value or "").strip().lower()).replace("_", "")


def _our_creatures(state: dict, local: int | None) -> list[dict]:
    return [
        c
        for c in state.get("battlefield") or []
        if isinstance(c, dict) and _controller(c) == local and _is_creature(c) and not _is_land(c)
    ]


def _untapped_mana(state: dict, local: int | None) -> int:
    return sum(
        1
        for c in state.get("battlefield") or []
        if isinstance(c, dict) and _controller(c) == local and _is_land(c) and not c.get("is_tapped")
    )


def beats_hard_cast(cast: SneakCast, gain: int, life: int, hard_mana: int | None = None) -> bool:
    """A Sneak that adds ``gain`` damage this turn beats casting the card normally (it would come
    in summoning sick: no damage this turn) by a large margin: at least half their ``life``, at the
    same or a lower cost (``hard_mana``, default ``cast.hard_mana``; unknown: no). Ledger #179
    (c0af235f T10): a Sneak Leonardo after blocks was ~20 of their 27; master hard-cast him."""
    hard = cast.hard_mana if hard_mana is None else hard_mana
    return hard is not None and cast.mana_value <= hard and life > 0 and 2 * gain >= life


@dataclass(frozen=True)
class SneakNow:
    """A Sneak cast in our declare-blockers step, blocks known."""

    cast: SneakCast
    unblocked: tuple[dict, ...]  # our unblocked attackers (at the player)
    returned: dict  # the attacker the cost returns (``return_choice`` order)
    through: int  # damage the unblocked attackers deal without the Sneak
    power: int  # the Sneak creature's power once it is in
    total: int  # damage to the player with the Sneak
    life: int  # their life
    affordable: bool | None  # mana for it (None: unknown)

    @property
    def lethal(self) -> bool:
        return self.total >= self.life

    @property
    def gain(self) -> int:
        return self.total - self.through

    @property
    def beats_hard_cast(self) -> bool:
        return beats_hard_cast(self.cast, self.gain, self.life)

    def text(self) -> str:
        return (
            f"Sneak {self.cast.name} ({self.cast.cost}) returning {self.returned.get('name')}: "
            f"{self.through} unblocked - {_power(self.returned)} + {self.power} = {self.total} "
            f"against their {self.life} life"
        )


def _power(card: dict) -> int:
    return max(0, _int(card.get("power")) or 0)


def _attacks_player(card: dict, opponent: int | None) -> bool:
    target = card.get("attack_target_id")
    return target in (None, 0, opponent)


def unblocked_attackers(state: dict) -> list[dict] | None:
    """Our attackers no creature blocks, at the player; None when blocks are unknown.

    Attackers carry ``is_blocked`` (Player.log attackers' BlockState_Blocked /
    _Unblocked) once blocks are declared. Without it, our attackers are all
    unblocked only when nothing of theirs is blocking.
    """
    local, opponent = _seats(state)
    attackers = [c for c in _our_creatures(state, local) if c.get("is_attacking")]
    if not attackers:
        return []
    if any("is_blocked" in c for c in attackers):
        return [c for c in attackers if c.get("is_blocked") is False and _attacks_player(c, opponent)]
    theirs_blocking = any(
        c.get("is_blocking")
        for c in state.get("battlefield") or []
        if isinstance(c, dict) and _controller(c) == opponent
    )
    if theirs_blocking:
        return None
    return [c for c in attackers if _attacks_player(c, opponent)]


def _choice_key(card: dict, keeps_lethal: bool) -> tuple:
    """Sort key, best first: keeps lethal, a card (back to hand to recast) over a token (gone),
    the cheaper recast, then the least power lost."""
    cost = _mana_value(str(card.get("mana_cost") or ""))
    return (not keeps_lethal, _is_token(card), cost, _power(card), int(card.get("instance_id") or 0))


def return_choice(candidates: list[dict], *, through: int, power: int, life: int | None) -> dict | None:
    """The attacker to return for the Sneak cost: one that keeps the attack lethal when any does,
    then a recastable card over a token (a returned token just vanishes), the cheapest to recast,
    the least power lost."""
    if not candidates:
        return None

    def keeps(card: dict) -> bool:
        return life is not None and through - _power(card) + power >= life

    return min(candidates, key=lambda card: _choice_key(card, keeps(card)))


def sneak_now(state: dict | None, cast: SneakCast | None = None) -> SneakNow | None:
    """The best Sneak cast now (our declare-blockers step, blocks known), or None."""
    if not isinstance(state, dict):
        return None
    turn = state.get("turn") or {}
    local, opponent = _seats(state)
    if local is None or turn.get("active_player") != local:
        return None
    if _norm(turn.get("step")) not in ("declareblock", "declareblockers"):
        return None
    life = _life(state, opponent)
    if life is None or life <= 0:
        return None
    unblocked = unblocked_attackers(state)
    if not unblocked:
        return None
    casts = [cast] if cast is not None else sneak_casts(state)
    if not casts:
        return None
    creatures = len(_our_creatures(state, local))
    through = sum(_power(c) for c in unblocked)
    mana = _untapped_mana(state, local)
    best: SneakNow | None = None
    for candidate in casts:
        # One attacker leaves, the Sneak creature arrives: the count of creatures is unchanged.
        power = entry_power(candidate.card, creatures)
        returned = return_choice(unblocked, through=through, power=power, life=life)
        if returned is None:
            continue
        total = through - _power(returned) + power
        found = SneakNow(
            candidate, tuple(unblocked), returned, through, power, total, life,
            candidate.mana_value <= mana if mana or candidate.mana_value else None,
        )  # fmt: skip
        if best is None or found.total > best.total:
            best = found
    return best


@dataclass(frozen=True)
class SneakHold:
    """Before combat: attacking with everything, then a Sneak after their best blocks."""

    cast: SneakCast
    through: int  # damage through their best blocks without the Sneak
    power: int  # the Sneak creature's power (current board)
    total: int
    life: int
    keep: int  # mana to keep for it

    @property
    def lethal(self) -> bool:
        return self.total >= self.life

    @property
    def gain(self) -> int:
        return self.total - self.through

    def text(self) -> str:
        text = (
            f"attack with everything, then Sneak {self.cast.name} ({self.cast.cost}) after blocks: "
            f"{self.through} through their best blocks - the returned attacker + {self.power} = "
            f"{self.total} against their {self.life} life"
        )
        if self.lethal:
            return text
        hard = f" ({self.cast.hard_cost})" if self.cast.hard_cost else ""
        return f"{text}, {self.gain} more than casting it normally{hard}, which comes in summoning sick"


def sneak_attack(attackers: list[dict], blockers: list[dict], life: int, sneak_power: int) -> tuple[int, int]:
    """(damage without, damage with) the Sneak for this attack into their best blocks (solver
    bodies); the Sneak needs an attacker they leave unblocked and returns the weakest one."""
    from arenamcp.board_assessment import _combat_life

    if not attackers:
        return 0, 0
    damage, _dead, _killed, _gain, healed, through, _used = _combat_life(
        attackers, blockers, max(1, life), detail=True
    )
    net = damage - healed
    open_ = [a for a in attackers if a["power"] > 0 and through.get(a["instance_id"], 0) >= a["power"]]
    if not open_:
        return net, net
    weakest = min(a["power"] for a in open_)
    return net, net - weakest + sneak_power


def sneak_main_hold(state: dict | None) -> SneakHold | None:
    """Before our attack: the Sneak line (attack with every able creature, Sneak after blocks)
    when it is lethal on the current board, or beats casting the card normally by at least half
    their life at the same or a lower cost (``beats_hard_cast``, ledger #179), else None. Main
    phase 1 of our turn only, and never when the attack is lethal without it."""
    if not isinstance(state, dict):
        return None
    turn = state.get("turn") or {}
    local, opponent = _seats(state)
    if local is None or turn.get("active_player") != local:
        return None
    phase, step = _norm(turn.get("phase")), _norm(turn.get("step"))
    if not (phase in ("main1", "beginning") or (phase == "combat" and step in ("", "begincombat"))):
        return None
    casts = sneak_casts(state)
    if not casts:
        return None
    life = _life(state, opponent)
    if life is None or life <= 0:
        return None
    try:
        from arenamcp.board_model import build_board_model

        model = build_board_model(state)
    except Exception:
        model = None
    if model is None or not model.our_attack_pending:
        return None
    attackers = list(model.first_our_attackers or ())
    if not attackers:
        return None
    blockers = [b for b in model.theirs if not b["_tapped"]]
    creatures = len(model.ours)
    best: SneakHold | None = None
    for cast in casts:
        power = entry_power(cast.card, creatures)
        without, with_sneak = sneak_attack(attackers, blockers, life, power)
        if with_sneak <= without:
            continue
        hold = SneakHold(cast, without, power, with_sneak, life, cast.mana_value)
        if not hold.lethal and not beats_hard_cast(cast, hold.gain, life):
            continue
        if best is None or (hold.lethal, hold.total) > (best.lethal, best.total):
            best = hold
    return best if best is not None and best.through < life else None


def option_mana(meta: dict[str, Any]) -> int:
    """Mana an ActionsAvailable cast option spends (its GRE manaCost), 0 for a land play."""
    if str(meta.get("actionType") or "").removeprefix("ActionType_") != "Cast":
        return 0
    try:
        from arenamcp.board_model import _action_cost

        return _mana_value(_action_cost(meta.get("manaCost")))
    except Exception:
        return 0


def is_sneak_option(meta: dict[str, Any] | None) -> bool:
    """A cast option Arena offers through Sneak (``decisions`` marks it ``alternative: Sneak``)."""
    return str((meta or {}).get("alternative") or "").lower() == "sneak"


# --- typed-decision policies (``ActionPlanner._plan_decision_options``) ---------------------


def _by_id(state: dict) -> dict[int, dict]:
    return {
        int(c.get("instance_id") or 0): c
        for zone in ("battlefield", "hand", "command", "stack")
        for c in state.get(zone) or []
        if isinstance(c, dict)
    }


def _option_card(option: Any, state: dict) -> dict:
    meta = getattr(option, "meta", {}) or {}
    return _by_id(state).get(int(meta.get("instanceId") or 0), {})


def sneak_cast_pick(decision: Any, state: dict | None) -> tuple[list[str], str]:
    """In our declare-blockers step: the Sneak cast option when it is lethal, or beats casting
    the card normally by at least half their life at the same or a lower cost, with why; else
    ([], '').

    Ledger #156 (match be1a945f): T6 the model cast Leonardo through Sneak
    believing it an ordinary cast; T12 the mana for it was gone. A lethal
    Sneak is a counting job, not a judgement call. Ledger #179 (match
    c0af235f T10): a Sneak Leonardo after blocks was ~20 damage of their 27
    while casting him normally dealt none that turn; a margin that large is
    counting too.
    """
    if not isinstance(state, dict) or getattr(decision, "request_type", "") != "ActionsAvailable":
        return [], ""
    casts = {c.name: c for c in sneak_casts(state)}
    best: tuple[SneakNow, str] | None = None
    for option in getattr(decision, "options", ()) or ():
        meta = getattr(option, "meta", {}) or {}
        if not is_sneak_option(meta) or getattr(option, "payable", None) is False:
            continue
        card = _option_card(option, state)
        name = str(card.get("name") or "")
        cast = casts.get(name)
        if cast is None and card:
            printed = sneak_cost(card)
            hard = str(card.get("mana_cost") or "")
            cast = SneakCast(card, name, printed, printed, "hand", 0, hard) if printed else None
        if cast is None:
            continue
        found = sneak_now(state, cast)
        if found is None or not (found.lethal or found.beats_hard_cast):
            continue
        if best is None or (found.lethal, found.total) > (best[0].lethal, best[0].total):
            best = (found, option.option_id)
    if best is None:
        return [], ""
    found, option_id = best
    if found.lethal:
        return [option_id], f"Lethal: {found.text()}."
    hard = f" ({found.cast.hard_cost})" if found.cast.hard_cost else ""
    return [option_id], (
        f"Sneak: {found.text()}; {found.gain} more damage than casting it normally{hard}, at least half "
        f"their life."
    )


def is_sneak_return(decision: Any) -> bool:
    """The SelectN that pays Sneak's cost: return an unblocked attacking creature to hand."""
    if getattr(decision, "request_type", "") != "SelectN":
        return False
    text = " ".join(str(getattr(decision, name, "") or "") for name in ("prompt_text", "source_label"))
    return bool(RETURN_PROMPT.search(text))


def _looks_like_sneak_payment(decision: Any, state: dict) -> bool:
    """A non-mana payment without Arena's prompt text whose choices are all our attacking
    creatures while a Sneak creature of ours is on the stack (the prompt may not reach us)."""
    if getattr(decision, "request_type", "") != "SelectN":
        return False
    if not str(getattr(decision, "source_label", "") or "").lower().startswith("pay non-mana cost"):
        return False
    local, _opponent = _seats(state)
    attacking = {
        int(c.get("instance_id") or 0) for c in _our_creatures(state, local) if c.get("is_attacking")
    }
    ids = [_int(str(o.option_id)[4:]) for o in getattr(decision, "options", ()) or ()]
    stack = [c for c in state.get("stack") or [] if isinstance(c, dict) and _controller(c) == local]
    return bool(ids) and all(i in attacking for i in ids) and any(sneak_cost(c) for c in stack)


def return_pick(decision: Any, state: dict | None) -> tuple[list[str], str]:
    """Which unblocked attacker the Sneak cost returns, with why; ([], '') when not that request.

    A card goes back to hand and can be cast again (a Hare Apparent makes its
    Rabbits again); a token just vanishes. When the attack is lethal only
    without the bigger one, the lethal choice wins.
    """
    if not isinstance(state, dict) or not (
        is_sneak_return(decision) or _looks_like_sneak_payment(decision, state)
    ):
        return [], ""
    by_id = _by_id(state)
    candidates = []
    for option in getattr(decision, "options", ()) or ():
        if not str(option.option_id).startswith("sel:"):
            continue
        card = by_id.get(_int(str(option.option_id)[4:]) or 0)
        if card is not None:
            candidates.append({**card, "_option_id": option.option_id})
    if not candidates or len(candidates) < (getattr(decision, "min_select", 1) or 1):
        return [], ""
    local, opponent = _seats(state)
    life = _life(state, opponent)
    # The Sneak creature is on the stack now: its power once in (one attacker leaves, it arrives).
    stack = [c for c in state.get("stack") or [] if isinstance(c, dict) and _controller(c) == local]
    spell = next((c for c in stack if sneak_cost(c)), None)
    creatures = len(_our_creatures(state, local))
    power = entry_power(spell, creatures) if spell is not None else 0
    unblocked = unblocked_attackers(state) or candidates
    through = sum(_power(c) for c in unblocked)
    chosen = return_choice(candidates, through=through, power=power, life=life)
    if chosen is None:
        return [], ""
    kind = "a token, gone for good" if _is_token(chosen) else "back to hand to cast again"
    why = f"Return {chosen.get('name')} for the Sneak cost ({kind})"
    if life is not None and power:
        total = through - _power(chosen) + power
        why += f"; {through} unblocked - {_power(chosen)} + {power} = {total} against their {life} life"
    return [chosen["_option_id"]], why + "."


def hold_filter(decision: Any, state: dict | None) -> tuple[Any, str]:
    """Before our attack, when the Sneak line is lethal or beats the hard cast by half their life
    (``sneak_main_hold``): the decision without the casts that would leave less mana than the
    Sneak costs, nor the Sneak card's own normal cast, and why; (decision, '') otherwise.

    Ledger #156 (be1a945f T12): six mana, two Hare Apparents in hand and a Sneak
    Leonardo for {2}{W} lethal after blocks; the second Hare took the mana.
    A cast that leaves the Sneak mana open (the first Hare, which also grows
    Leonardo) stays on the menu; so do land drops and passing.
    """
    if not isinstance(state, dict) or getattr(decision, "request_type", "") != "ActionsAvailable":
        return decision, ""
    hold = sneak_main_hold(state)
    if hold is None:
        return decision, ""
    try:
        from arenamcp.decision_memory import mana_available

        mana = mana_available(state)
    except Exception:
        return decision, ""
    if mana < hold.keep:
        return decision, ""  # the Sneak is not affordable this turn anyway
    sneak_id = _int(hold.cast.card.get("instance_id"))

    def hard_cast(meta: dict) -> bool:  # the Sneak card cast normally: no Sneak left after it
        return sneak_id is not None and _int(meta.get("instanceId")) == sneak_id and not is_sneak_option(meta)

    if not hold.lethal:
        # Ledger #179: the margin is against Arena's own price for the normal cast when it is offered.
        offered = [
            option_mana(getattr(o, "meta", {}) or {})
            for o in decision.options
            if hard_cast(getattr(o, "meta", {}) or {})
        ]
        if offered and not beats_hard_cast(hold.cast, hold.gain, hold.life, min(offered)):
            return decision, ""
    kept, dropped = [], []
    for option in decision.options:
        meta = getattr(option, "meta", {}) or {}
        spend = option_mana(meta)
        (dropped if spend and (mana - spend < hold.keep or hard_cast(meta)) else kept).append(option)
    if not dropped or not kept:
        return decision, ""
    import dataclasses

    names = ", ".join(sorted({str(o.label).split(" [")[0] for o in dropped}))
    why = f"keep {hold.cast.cost} open for the Sneak ({hold.text()}); withholding {names}"
    return dataclasses.replace(decision, options=tuple(kept)), why
