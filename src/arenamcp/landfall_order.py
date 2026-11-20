"""Cast a landfall permanent before the land drop that would trigger it.

2026-10-12 turn 11 (live, Mac): with Terrasymbiosis out, the model played
Ba Sing Se ("a free land drop ... keeping my 5 mana intact to cast Evolution
Sage") and cast Evolution Sage four seconds later, so its "Landfall — Whenever
a land you control enters, proliferate" missed the land drop, and so did the
Terrasymbiosis draw. The game plan's T11 step cast Sage that turn; the line
search cannot value the trigger, and every land-first default (the planner
preflight, the board-math fallback) played the land without looking.

Casting first costs nothing when the card is cast this turn anyway: Arena's
payable flag says the current mana covers it, and the land's mana arrives
after. So the reorder applies only to a payable, non-X permanent whose
"whenever a land you control enters" trigger works from the battlefield, and
only when this turn's plan casts it (the model's own reasoning without hold
language, the strategy block's THIS TURN line, or the searched line's casts).
Everything here is log-derivable.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from arenamcp.mana import mana_cost_to_cmc
from arenamcp.play_safety import find_source

logger = logging.getLogger(__name__)

# "Landfall — Whenever a land you control enters", the older "Whenever a land
# enters the battlefield under your control", and "one or more lands". Not
# "if you had a land enter this turn" (Searing Blaze wants the land first) and
# not lands an opponent controls.
_TRIGGER = re.compile(
    r"whenever (?:a|one or more|another) (?:basic )?lands? "
    r"(?:you control enters?|enters?(?: the battlefield)? under your control)"
)
_HOLD = re.compile(r"\b(?:hold|holding|held|save|saving|sandbag\w*|next turn|later|wait|waiting)\b")
_THIS_TURN_CASTS = re.compile(r"\bcasts? ([^;|\n\[]*)")
_HOLD_WINDOW = 30
_PRE_LAND_CAST_RE = re.compile(
    r"\b(?:draw|scry|surveil|search your library|look at the top|reveal the top|"
    r"exile the top|discard|cards? in (?:your )?hand|no cards in hand|hellbent|"
    r"more lands than you|fewer lands)\b",
    re.IGNORECASE,
)
_OPP_LAND_PUNISHER_RE = re.compile(
    r"whenever (?:a |an )?(?:opponent )?plays? a land|whenever a land enters",
    re.IGNORECASE,
)
_UNSAFE_PRECAST_LAND_RE = re.compile(
    r"\b(?:enters?(?: the battlefield)? tapped|channel|cycling|bloodrush|forecast|"
    r"sacrifice|return a land|pay \d+ life|lose \d+ life|deals? \d+ damage to you|"
    r"unless you)\b",
    re.IGNORECASE,
)
_BLUFF_LAND_DISQUALIFIER_RE = re.compile(
    r"\b(?:channel|cycling|basic landcycling|landcycling|bloodrush|forecast|"
    r"becomes? a|when(?:ever)?|search|draw|scry|surveil|create|token|counter|"
    r"mill|gains?|lose|deals?|damage|sacrifice|return|destroy|exile)\b",
    re.IGNORECASE,
)
_LAND_PAYOFF_RE = re.compile(
    r"\b(?:lands? you control|number of lands|domain|for each (?:basic |nonbasic )?"
    r"(?:land|plains|island|swamp|mountain|forest))\b",
    re.IGNORECASE,
)
_ALT_CAST_COST_RE = re.compile(
    r"\b(?:flashback|escape|plot|foretell|disturb|unearth|encore|harmonize)\b"
    r"[^\n{(]*((?:\{[^}]+\})+)",
    re.IGNORECASE,
)
_JUMP_START_RE = re.compile(r"\bjump-?start\b", re.IGNORECASE)
_PLAY_ACTION_TYPES = frozenset({"actiontype_play", "actiontype_playmdfc"})


@dataclass(frozen=True)
class LandfallFirst:
    option_id: str
    card_name: str
    land_name: str
    reason: str


@dataclass(frozen=True)
class LandBeforeCast:
    option_id: str
    land_name: str
    cast_name: str
    reason: str


def landfall_trigger(card: dict[str, Any]) -> bool:
    """A permanent whose own trigger fires on our land drops while it is on the battlefield."""
    types = str(card.get("type_line") or "").lower()
    if not types.strip() or re.search(r"\b(?:instant|sorcery)\b", types):
        return False
    text = str(card.get("oracle_text") or "").lower()
    for match in _TRIGGER.finditer(text):
        end = text.find(".", match.end())
        # Bloodghast: the trigger only returns it from the graveyard.
        if "from your graveyard" not in text[match.start() : end if end >= 0 else len(text)]:
            return True
    return False


def _named_without_hold(name: str, reasoning: str) -> bool:
    text, needle = reasoning.casefold(), name.casefold()
    start = text.find(needle)
    while start >= 0:
        window = text[max(0, start - _HOLD_WINDOW) : start + len(needle) + _HOLD_WINDOW]
        if not _HOLD.search(window):
            return True
        start = text.find(needle, start + 1)
    return False


def _in_this_turn_line(name: str, plan_block: str) -> bool:
    needle = name.casefold()
    for line in plan_block.splitlines():
        if "THIS TURN" in line:
            return any(needle in found.casefold() for found in _THIS_TURN_CASTS.findall(line))
    return False


def planned_this_turn(
    name: str, *, reasoning: str = "", plan_block: str = "", planned_names: Iterable[str] = ()
) -> bool:
    """Whether this turn's plan casts ``name`` (so casting it now spends nothing extra)."""
    if any(str(n).casefold() == name.casefold() for n in planned_names):
        return True
    return _named_without_hold(name, reasoning) or _in_this_turn_line(name, plan_block)


def _own_main_phase_with_empty_stack(state: dict[str, Any]) -> bool:
    turn = state.get("turn") or {}
    local = _local_seat(state)
    if local is None or turn.get("active_player") != local:
        return False
    return "main" in str(turn.get("phase") or "").lower() and not (state.get("stack") or [])


def landfall_first(
    decision: Any,
    game_state: dict[str, Any],
    picked: str,
    *,
    reasoning: str = "",
    plan_block: str = "",
    planned_names: Iterable[str] = (),
) -> LandfallFirst | None:
    """The planned landfall cast to make before the land ``picked`` names, or None."""
    if decision.request_type != "ActionsAvailable" or not _own_main_phase_with_empty_stack(game_state):
        return None
    land = decision.find(picked)
    if land is None or (land.meta or {}).get("actionType") != "ActionType_Play":
        return None
    land_name = str(find_source(game_state, land.meta or {}).get("name") or "") or "the land"
    planned_names = list(planned_names)
    for option in decision.options:
        meta = option.meta or {}
        if option.payable is not True or meta.get("actionType") != "ActionType_Cast":
            continue
        card = find_source(game_state, meta)
        name = str(card.get("name") or "")
        # An X spell trades the land's extra mana for the trigger: the model's call.
        if not name or "{x}" in str(card.get("mana_cost") or "").lower() or not landfall_trigger(card):
            continue
        if not planned_this_turn(
            name, reasoning=reasoning, plan_block=plan_block, planned_names=planned_names
        ):
            continue
        return LandfallFirst(
            option_id=option.option_id,
            card_name=name,
            land_name=land_name,
            reason=f"Cast {name} before playing {land_name} so its landfall trigger sees the land drop.",
        )
    return None


def landfall_cast_pending(game_state: dict[str, Any], legal_actions: Iterable[str]) -> str:
    """Name of a payable ("[OK]") landfall permanent in a legacy legal-action list, else ''."""
    hand = {
        str(c.get("name") or "").casefold(): c for c in game_state.get("hand") or [] if isinstance(c, dict)
    }
    for legal in legal_actions:
        text = str(legal)
        if not text.lower().startswith("cast ") or "[OK]" not in text:
            continue
        name = text[5:].split("[OK]", 1)[0].strip()
        card = hand.get(name.casefold())
        if card and "{x}" not in str(card.get("mana_cost") or "").lower() and landfall_trigger(card):
            return name
    return ""


def _local_seat(state: dict[str, Any]) -> Any:
    seat = state.get("local_seat_id")
    if seat is not None:
        return seat
    return next(
        (p.get("seat_id") for p in state.get("players") or [] if isinstance(p, dict) and p.get("is_local")),
        None,
    )


def _controller(card: dict[str, Any]) -> Any:
    ctrl = card.get("controller_seat_id")
    return ctrl if ctrl is not None else card.get("owner_seat_id")


def _is_land(card: dict[str, Any]) -> bool:
    types = " ".join(str(t) for t in (card.get("card_types") or card.get("types") or []))
    return "land" in f"{card.get('type_line') or ''} {types}".lower()


def _rules_text(card: dict[str, Any]) -> str:
    from arenamcp.rules_engine import _normalize_mana_symbols

    text = re.sub(r"<[^>]*>", " ", str(card.get("oracle_text") or ""))
    text = re.sub(r"\([^)]*\)", " ", text)
    return _normalize_mana_symbols(text)


def _is_mdfc_or_split_land(card: dict[str, Any]) -> bool:
    if " // " in str(card.get("name") or ""):
        return True
    for face in card.get("related_faces") or ():
        if isinstance(face, dict):
            tl = str(face.get("type_line") or "").lower()
            if tl and "land" not in tl:
                return True
    return False


def _lands_played_this_turn(state: dict[str, Any], local: Any) -> int:
    for player in state.get("players") or []:
        if not isinstance(player, dict):
            continue
        if player.get("seat_id") == local or player.get("is_local"):
            return int(player.get("lands_played") or 0)
    return 0


def _cost_exceeds_lands(cost_text: str, n_lands: int) -> bool:
    from arenamcp.rules_engine import _normalize_mana_symbols

    normalized = _normalize_mana_symbols(str(cost_text or ""))
    tokens = re.findall(r"\{([^}]+)\}", normalized)
    if not tokens:
        return False
    mana_tokens: list[str] = []
    for tok in tokens:
        low = tok.strip().lower()
        if low in ("t", "q", "e"):
            continue
        if low == "x":
            return True
        mana_tokens.append(f"{{{tok.strip()}}}")
    if not mana_tokens:
        return False
    return mana_cost_to_cmc("".join(mana_tokens)) > n_lands


def _plain_bluff_land(card: dict[str, Any]) -> bool:
    """True only for a plain mana land without triggers, utility abilities, or alt faces."""
    if not _is_land(card) or _is_mdfc_or_split_land(card):
        return False
    rules = _rules_text(card)
    if _BLUFF_LAND_DISQUALIFIER_RE.search(rules):
        return False
    type_low = str(card.get("type_line") or "").lower()
    subtypes_low = " ".join(str(s).lower() for s in (card.get("subtypes") or ()))
    has_basic_hint = "basic" in type_low or any(
        b in f"{type_low} {subtypes_low}" for b in ("plains", "island", "swamp", "mountain", "forest")
    )
    if not rules.strip() and not has_basic_hint:
        from arenamcp.mulligan_policy import hand_card

        if not hand_card(card).produces:
            return False
    for raw_line in rules.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if ":" in line:
            if not re.fullmatch(r"\{t\}:\s*add\b[^.]*\.?", line, re.IGNORECASE):
                return False
        elif not re.fullmatch(r"[^.:]*\benters(?: the battlefield)? tapped\.?", line, re.IGNORECASE):
            return False
    return True


def land_before_cast_order(
    decision: Any,
    game_state: dict[str, Any],
    chosen_id: str,
    *,
    land_drop_first: bool = True,
) -> LandBeforeCast | None:
    """Play a unique plain untapped land before a cast when opponent holds open mana."""
    from arenamcp.ward import untapped_land_drop

    if (
        not land_drop_first
        or getattr(decision, "request_type", "") != "ActionsAvailable"
        or not _own_main_phase_with_empty_stack(game_state)
    ):
        return None
    local = _local_seat(game_state)
    if local is None or _lands_played_this_turn(game_state, local) > 0:
        return None
    battlefield = [c for c in game_state.get("battlefield") or [] if isinstance(c, dict)]
    if not any(
        _controller(c) not in (None, local) and _is_land(c) and not c.get("is_tapped") for c in battlefield
    ):
        return None
    for perm in battlefield:
        if _controller(perm) not in (None, local) and _OPP_LAND_PUNISHER_RE.search(_rules_text(perm)):
            return None

    chosen_opt = decision.find(chosen_id) if hasattr(decision, "find") else None
    chosen_meta = getattr(chosen_opt, "meta", None) or {}
    if chosen_meta.get("actionType") != "ActionType_Cast":
        return None
    chosen_card = find_source(game_state, chosen_meta)
    cast_name = str(chosen_card.get("name") or "")
    if not cast_name or landfall_trigger(chosen_card) or _PRE_LAND_CAST_RE.search(_rules_text(chosen_card)):
        return None
    from arenamcp.line_search_moves import unmodelled_effect

    if unmodelled_effect(chosen_card):
        return None

    land_opts: list[tuple[Any, dict[str, Any]]] = []
    for opt in getattr(decision, "options", ()) or ():
        meta = getattr(opt, "meta", None) or {}
        action_type = str(meta.get("actionType") or "")
        if action_type == "ActionType_Cast" and getattr(opt, "payable", None) is True:
            other_card = find_source(game_state, meta)
            if other_card and "{x}" not in str(other_card.get("mana_cost") or "").lower():
                if landfall_trigger(other_card):
                    return None
        elif action_type.lower() in _PLAY_ACTION_TYPES and getattr(opt, "payable", None) is not False:
            card = find_source(game_state, meta)
            if card and _is_land(card):
                land_opts.append((opt, card))

    if not land_opts:
        return None
    names = {str(card.get("name") or "").casefold() for _opt, card in land_opts}
    if len(names) != 1:
        return None
    land_opt, land_card = land_opts[0]
    if str((getattr(land_opt, "meta", None) or {}).get("actionType") or "").lower() != "actiontype_play":
        return None
    land_name = str(land_card.get("name") or "")
    if not land_name or _is_mdfc_or_split_land(land_card):
        return None
    if re.search(r"\blegendary\b", str(land_card.get("type_line") or ""), re.IGNORECASE) and any(
        _controller(perm) == local and str(perm.get("name") or "").casefold() == land_name.casefold()
        for perm in battlefield
    ):
        return None
    if not untapped_land_drop(game_state, [land_opt]):
        return None
    if _UNSAFE_PRECAST_LAND_RE.search(_rules_text(land_card)):
        return None
    return LandBeforeCast(
        option_id=land_opt.option_id,
        land_name=land_name,
        cast_name=cast_name,
        reason=(
            f"Play {land_name} before casting {cast_name}: an untapped land drop first gives "
            f"extra mana against the opponent's open mana and leaves {cast_name} castable afterward."
        ),
    )


def superfluous_bluff_land(
    option: Any,
    state: dict[str, Any],
    *,
    land_name: str = "",
) -> bool:
    """True when playing a plain land as the sole card in hand only drops a bluff shield."""
    if not isinstance(state, dict) or not _own_main_phase_with_empty_stack(state):
        return False
    local = _local_seat(state)
    if local is None:
        return False
    hand = [c for c in state.get("hand") or [] if isinstance(c, dict)]
    if len(hand) != 1:
        return False
    sole = hand[0]
    if not _plain_bluff_land(sole):
        return False
    sole_name = str(sole.get("name") or "")
    if option is not None:
        meta = getattr(option, "meta", None) or {}
        if meta.get("actionType") != "ActionType_Play":
            return False
        meta_iid = meta.get("instanceId")
        sole_iid = sole.get("instance_id")
        if meta_iid is not None and sole_iid is not None and meta_iid != sole_iid:
            return False
        card = find_source(state, meta)
        if not card:
            return False
        if (
            sole_iid is not None
            and card.get("instance_id") is not None
            and card.get("instance_id") != sole_iid
        ):
            return False
        if str(card.get("name") or "").casefold() != sole_name.casefold():
            return False
    elif land_name and sole_name.casefold() != land_name.casefold():
        return False

    battlefield = [c for c in state.get("battlefield") or [] if isinstance(c, dict)]
    our_perms = [c for c in battlefield if _controller(c) == local]
    our_lands = [c for c in our_perms if _is_land(c)]
    n_lands = len(our_lands)
    if n_lands < 6:
        return False

    from arenamcp.mulligan_policy import hand_card

    sole_colors = hand_card(sole).produces
    existing_colors = frozenset().union(*(hand_card(land).produces for land in our_lands))
    if not (sole_colors <= existing_colors):
        return False

    for perm in our_perms:
        if landfall_trigger(perm):
            return False
        rules = _rules_text(perm)
        if _LAND_PAYOFF_RE.search(rules):
            return False
        for raw_line in rules.splitlines():
            if ":" in raw_line and _cost_exceeds_lands(raw_line.split(":", 1)[0], n_lands):
                return False

    if state.get("command") or (isinstance(state.get("zones"), dict) and state["zones"].get("command")):
        try:
            from arenamcp.board_model import our_commanders

            for cmd in our_commanders(state, local):
                if _cost_exceeds_lands(str(getattr(cmd, "cost", "") or ""), n_lands):
                    return False
                cmd_card = getattr(cmd, "card", None)
                if isinstance(cmd_card, dict) and _cost_exceeds_lands(
                    str(cmd_card.get("mana_cost") or ""), n_lands
                ):
                    return False
        except Exception as error:
            logger.debug("commander tax check skipped in superfluous_bluff_land: %s", error)

    for zone_name in ("graveyard", "exile", "command"):
        for card in state.get(zone_name) or []:
            if not isinstance(card, dict):
                continue
            owner = card.get("owner_seat_id")
            if owner is None:
                owner = card.get("controller_seat_id")
            if owner not in (None, local):
                continue
            rules = _rules_text(card)
            if zone_name == "command":
                if _cost_exceeds_lands(str(card.get("mana_cost") or ""), n_lands):
                    return False
            elif zone_name == "graveyard" and _JUMP_START_RE.search(rules):
                return False
            elif _ALT_CAST_COST_RE.search(rules):
                for match in _ALT_CAST_COST_RE.finditer(rules):
                    if _cost_exceeds_lands(match.group(1), n_lands):
                        return False
                if _cost_exceeds_lands(str(card.get("mana_cost") or ""), n_lands):
                    return False

    return True


def bluff_land_notes(decision: Any, state: dict[str, Any]) -> list[str]:
    """Prompt hint when passing preserves a hidden card instead of exposing hellbent."""
    if getattr(decision, "request_type", "") != "ActionsAvailable":
        return []
    if not hasattr(decision, "find") or decision.find("pass") is None:
        return []
    battlefield = [c for c in state.get("battlefield") or [] if isinstance(c, dict)]
    local = _local_seat(state)
    n_lands = sum(1 for c in battlefield if local is not None and _controller(c) == local and _is_land(c))
    for opt in getattr(decision, "options", ()) or ():
        if getattr(opt, "payable", None) is False:
            continue
        if superfluous_bluff_land(opt, state):
            card = find_source(state, getattr(opt, "meta", None) or {})
            name = str(card.get("name") or "the land")
            return [
                f"BLUFF LAND: {name} is your only card in hand with {n_lands} lands already out "
                f"and no landfall or higher mana sink; passing keeps a hidden card in hand to "
                f"bluff interaction instead of revealing an empty hand."
            ]
    return []
