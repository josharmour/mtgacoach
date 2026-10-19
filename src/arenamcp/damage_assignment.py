"""Combat damage assignment for Arena's AssignDamageRequest (ledger #102).

Arena sends one ``DamageAssigner`` per attacker: ``totalDamage`` and one entry
per receiver, in Arena's order. Each blocker carries ``minDamage``, the damage
Arena counts as lethal (damage already marked and deathtouch included). The
attack quarry (the player or planeswalker attacked) is listed last, and only
when damage may go past the blockers (trample). Its ``maxDamage`` is what is
left after lethal damage to every blocker. Player.log, 2026-10-19 12:22:18:

    {"instanceId": 344, "totalDamage": 4, "assignments": [
        {"instanceId": 350, "minDamage": 2, "assignedDamage": 2},
        {"instanceId": 1, "maxDamage": 2, "assignedDamage": 2}]}

Arena's client checks an answer with ``MtgDamageAssigner.CanSubmit``
(re-output/Core/.../AssignDamage). All the damage must be assigned. With
trample, the quarry may get damage only once every blocker has lethal damage.
Its Done button sends every entry, including those with no damage.

The deterministic split is Arena's Auto Allocate (``AssignPrioritizedDamage``).
Each blocker gets lethal damage in Arena's order. The rest goes to the quarry,
or onto the last blocker when there is no quarry entry. When the damage cannot
kill every blocker, the attacker kills the most blockers it can, then the
biggest (``combat_solver._damage_order``, the rule the combat model predicts
with). The damage left over goes onto a blocker that survives, as Arena's own
auto allocation does (2026-10-18 pro game: Dina put 2 on Campus Crier, 3 on Zimone).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ActionPlan.fallback_reason when no usable model answer was given and the split is ours.
FALLBACK_DAMAGE_ASSIGNMENT = "planner_damage_assignment"

_ID_SUFFIX = re.compile(r"[\[#(]\s*(?:id:)?\s*(\d+)\s*[\])]?\s*$", re.I)
_PLAYER_WORDS = frozenset(
    {"opponent", "the opponent", "player", "the player", "defending player", "face", "their face", "them"}
)


@dataclass(frozen=True)
class Receiver:
    """One entry of Arena's assignment list."""

    instance_id: int
    lethal: int  # Arena's minDamage; 0 for the quarry
    max_damage: int  # 0 = no cap (Arena reads it as totalDamage)
    quarry: bool  # the player or planeswalker the attacker attacks
    card: dict | None


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _cards(battlefield: list[dict] | None) -> dict[int, dict]:
    cards: dict[int, dict] = {}
    for card in battlefield or []:
        if isinstance(card, dict) and _int(card.get("instance_id")):
            cards[_int(card.get("instance_id"))] = card
    return cards


def _player_seats(players: list[dict] | None) -> set[int]:
    seats = {_int(p.get("seat_id") or p.get("system_seat_id")) for p in players or [] if isinstance(p, dict)}
    seats.discard(0)
    return seats or {1, 2}


def _local_seat(game_state: dict[str, Any]) -> int:
    seat = _int(game_state.get("local_seat_id"))
    if seat:
        return seat
    for player in game_state.get("players") or []:
        if isinstance(player, dict) and player.get("is_local"):
            return _int(player.get("seat_id") or player.get("system_seat_id"))
    return 0


def _is_creature(card: dict) -> bool:
    if "creature" in str(card.get("type_line") or "").lower():
        return True
    return any(str(kind).lower().endswith("creature") for kind in card.get("card_types") or ())


def receivers(
    assignments: list[dict] | None,
    battlefield: list[dict] | None,
    *,
    attacker: dict | None = None,
    players: list[dict] | None = None,
) -> list[Receiver]:
    """Arena's entries, in Arena's order, split into blockers and the quarry.

    A creature on the battlefield is a blocker; when Arena sent no minDamage
    for it, its lethal damage is its toughness less the damage marked on it
    (1 for a deathtouch attacker). An entry with no card is a blocker only when
    Arena sent a lethal amount and it is not a player seat.
    """
    from arenamcp.combat_solver import _has

    cards = _cards(battlefield)
    seats = _player_seats(players)
    deathtouch = bool(attacker) and _has(attacker, "deathtouch")
    out: list[Receiver] = []
    for entry in assignments or []:
        if not isinstance(entry, dict):
            continue
        rid = _int(entry.get("instanceId"))
        if not rid:
            continue
        card = cards.get(rid)
        lethal = _int(entry.get("minDamage"))
        if card is not None and _is_creature(card):
            if lethal <= 0:
                lethal = 1 if deathtouch else max(1, _int(card.get("toughness")) - _int(card.get("damage")))
            blocker = True
        else:
            blocker = card is None and lethal > 0 and rid not in seats
        out.append(
            Receiver(rid, lethal if blocker else 0, max(0, _int(entry.get("maxDamage"))), not blocker, card)
        )
    return out


def _kill_order(blockers: list[Receiver], total: int) -> list[Receiver]:
    """The blockers ``total`` damage kills: the most, then the biggest (combat_solver)."""
    from arenamcp.combat_solver import _damage_order, _has

    shaped = [{**(b.card or {}), "toughness": max(1, b.lethal), "_receiver": b} for b in blockers]
    killed: list[Receiver] = []
    left = total
    for blocker in _damage_order(total, shaped, deathtouch=False):
        if left < blocker["toughness"] or _has(blocker, "indestructible"):
            break
        killed.append(blocker["_receiver"])
        left -= blocker["toughness"]
    return killed


def deterministic_split(
    assignments: list[dict] | None,
    battlefield: list[dict] | None,
    total: int,
    *,
    attacker: dict | None = None,
    players: list[dict] | None = None,
) -> list[dict[str, int]]:
    """Lethal damage to the blockers, the rest past them with trample.

    Returns the receivers that get damage as ``{"instanceId", "damage"}``. The
    bridge fills in Arena's other entries with 0. Returns [] when Arena's list
    cannot be read.
    """
    from arenamcp.combat_solver import _has

    recs = receivers(assignments, battlefield, attacker=attacker, players=players)
    total = _int(total)
    if total <= 0 or not recs:
        return []
    blockers = [r for r in recs if not r.quarry]
    quarries = [r for r in recs if r.quarry]
    if not blockers:
        return [{"instanceId": quarries[0].instance_id, "damage": total}]
    damage: dict[int, int] = {}
    if total >= sum(b.lethal for b in blockers):
        # Every blocker dies (Arena's Auto Allocate). The rest goes to the quarry, which
        # Arena lists only when trample lets damage past the blockers, else on the last blocker.
        for blocker in blockers:
            damage[blocker.instance_id] = blocker.lethal
        left = total - sum(damage.values())
        if left and quarries:
            quarry = quarries[0]
            give = left if quarry.max_damage <= 0 else min(left, quarry.max_damage)
            damage[quarry.instance_id] = give
            left -= give
        if left:
            damage[blockers[-1].instance_id] += left
    else:
        killed = _kill_order(blockers, total)
        for blocker in killed:
            damage[blocker.instance_id] = blocker.lethal
        left = total - sum(damage.values())
        if left:
            spared = [b for b in blockers if b not in killed]
            soft = [b for b in spared if not (b.card and _has(b.card, "indestructible"))]
            target = (soft or spared or killed)[0]
            damage[target.instance_id] = damage.get(target.instance_id, 0) + left
    return [{"instanceId": rid, "damage": amount} for rid, amount in damage.items() if amount > 0]


def illegal_reason(recs: list[Receiver], damage: dict[int, int], total: int) -> str:
    """Why Arena would refuse this split ("" when it is legal): MtgDamageAssigner.CanSubmit."""
    by_id = {r.instance_id: r for r in recs}
    for rid, amount in damage.items():
        if rid not in by_id:
            return f"#{rid} is not one of Arena's receivers"
        if amount < 0:
            return f"negative damage to #{rid}"
        cap = by_id[rid].max_damage
        if cap and amount > cap:
            return f"{amount} to #{rid}, more than Arena's {cap}"
    assigned = sum(damage.values())
    if assigned != total:
        return f"assigns {assigned} of {total} damage"
    if any(damage.get(r.instance_id, 0) > 0 for r in recs if r.quarry) and any(
        damage.get(r.instance_id, 0) < r.lethal for r in recs if not r.quarry
    ):
        return "damage past a blocker that has not been assigned lethal damage"
    return ""


def _resolve_receiver(
    key: str, recs: list[Receiver], used: dict[int, int], local_seat: int, opponent_name: str
) -> int | None:
    """The entry a model's distribution key names: "Name [350]", "Opponent [1]", "Opponent", a card name."""
    text = str(key or "").strip()
    by_id = {r.instance_id for r in recs}
    match = _ID_SUFFIX.search(text)
    if match:
        rid = int(match.group(1))
        return rid if rid in by_id else None
    name = text.lower()
    bare = re.sub(r"\s*\([^)]*\)\s*$", "", name)  # "Opponent Zac (6 life)", the prompt's player label
    rival = opponent_name.strip().lower()
    if (
        bare in _PLAYER_WORDS
        or bare.startswith("opponent")
        or (rival and bare in (rival, f"opponent {rival}"))
    ):
        players = [r for r in recs if r.quarry and r.card is None and r.instance_id != local_seat]
        return players[0].instance_id if players else None
    named = [r for r in recs if r.card and str(r.card.get("name") or "").strip().lower() == name]
    fresh = [r for r in named if r.instance_id not in used]
    pick = (fresh or named or [None])[0]
    return pick.instance_id if pick else None


def model_split(
    assignments: list[dict] | None,
    distribution: dict[str, Any] | None,
    battlefield: list[dict] | None,
    total: int,
    *,
    attacker: dict | None = None,
    players: list[dict] | None = None,
    local_seat: int = 0,
    opponent_name: str = "",
) -> list[dict[str, int]] | None:
    """The model's split when Arena would accept it, else None.

    Amounts past the attacker's damage are cut off in the model's order. An
    unknown receiver, damage left unassigned or an illegal split (damage
    tramples past a blocker without lethal damage) returns None.
    """
    if not distribution or not isinstance(distribution, dict):
        return None
    recs = receivers(assignments, battlefield, attacker=attacker, players=players)
    total = _int(total)
    damage: dict[int, int] = {}
    left = total
    for key, amount in distribution.items():
        want = _int(amount)
        if want <= 0:
            continue
        rid = _resolve_receiver(key, recs, damage, local_seat, opponent_name)
        if rid is None:
            logger.info("assign_damage: model split names %r, which is not in Arena's request", key)
            return None
        give = min(want, left)
        if give <= 0:
            break
        damage[rid] = damage.get(rid, 0) + give
        left -= give
    reason = illegal_reason(recs, damage, total)
    if not reason and any(r.quarry for r in recs):
        # Trample damage kept on a blocker that already has lethal damage is
        # wasted (and 2026-10-18 18:41's refused answer had that shape).
        wasted = [r.instance_id for r in recs if not r.quarry and damage.get(r.instance_id, 0) > r.lethal]
        if wasted:
            reason = f"more than lethal damage on {wasted} while trample could reach the quarry"
    if reason:
        logger.info("assign_damage: model split %s not used: %s", distribution, reason)
        return None
    return [{"instanceId": rid, "damage": amount} for rid, amount in damage.items() if amount > 0]


def arena_default(assignments: list[dict] | None, total: int) -> list[dict[str, int]]:
    """Arena's own assignment from the request (what its Done button submits untouched)."""
    split = [
        {"instanceId": _int(e.get("instanceId")), "damage": _int(e.get("assignedDamage"))}
        for e in assignments or []
        if isinstance(e, dict) and _int(e.get("instanceId")) and _int(e.get("assignedDamage")) > 0
    ]
    return split if split and sum(e["damage"] for e in split) == _int(total) else []


def plan_assigners(
    bridge_assigners: list[dict] | None,
    game_state: dict[str, Any],
    distribution: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """The submission for every attacker, and where it came from.

    The model's distribution counts only with a single attacker (its keys name
    receivers, and the player is a receiver of every trampling attacker) and
    only when it is legal. Otherwise the deterministic split, and Arena's own
    default when the request cannot be read.
    """
    battlefield = game_state.get("battlefield") or []
    players = game_state.get("players") or []
    cards = _cards(battlefield)
    readable = [
        a
        for a in bridge_assigners or []
        if isinstance(a, dict) and _int(a.get("instanceId")) and _int(a.get("totalDamage")) > 0
    ]
    sources: list[str] = []
    out: list[dict[str, Any]] = []
    for assigner in readable:
        attacker_id, total = _int(assigner.get("instanceId")), _int(assigner.get("totalDamage"))
        entries = assigner.get("assignments") or []
        attacker = cards.get(attacker_id)
        recs = receivers(entries, battlefield, attacker=attacker, players=players)
        split = None
        if isinstance(distribution, dict) and distribution and len(readable) == 1:
            split = model_split(
                entries,
                distribution,
                battlefield,
                total,
                attacker=attacker,
                players=players,
                local_seat=_local_seat(game_state),
                opponent_name=str(game_state.get("opponent_name") or ""),
            )
            if split:
                sources.append("model")
        if not split:
            split = deterministic_split(entries, battlefield, total, attacker=attacker, players=players)
            reason = (
                illegal_reason(recs, {e["instanceId"]: e["damage"] for e in split}, total)
                if split
                else "empty"
            )
            if reason:
                logger.warning(
                    "assign_damage: deterministic split %s unusable (%s); Arena's default", split, reason
                )
                split = arena_default(entries, total)
                if split:
                    sources.append("arena default")
            else:
                sources.append("deterministic")
        if split:
            out.append({"instanceId": attacker_id, "assignments": split})
    return out, ", ".join(dict.fromkeys(sources))


def receiver_label(instance_id: int, game_state: dict[str, Any]) -> str:
    """ "Pyrewood Gearhulk [350]" / "Opponent [1]": names a receiver for narration and the planner."""
    card = _cards(game_state.get("battlefield")).get(instance_id)
    if card is not None:
        return f"{card.get('name') or 'creature'} [{instance_id}]"
    local = _local_seat(game_state)
    if local and instance_id == local:
        return f"You [{instance_id}]"
    return f"Opponent [{instance_id}]"


def submitted_distribution(assigners: list[dict[str, Any]], game_state: dict[str, Any]) -> dict[str, int]:
    """Receiver label -> damage over every attacker (what the narration says was assigned)."""
    out: dict[str, int] = {}
    for assigner in assigners:
        for entry in assigner.get("assignments") or []:
            label = receiver_label(_int(entry.get("instanceId")), game_state)
            out[label] = out.get(label, 0) + _int(entry.get("damage"))
    return out


def spoken_split(distribution: Any) -> str:
    """ "2 to Pyrewood Gearhulk and 2 to the opponent" ("" when nothing is assigned)."""
    from arenamcp.narration import spoken_list, spoken_name

    if not isinstance(distribution, dict):
        return ""
    parts = [f"{_int(n)} to {spoken_name(str(name))}" for name, n in distribution.items() if _int(n) > 0]
    return spoken_list(parts)


def is_assign_damage_window(
    game_state: dict[str, Any], decision_context: dict[str, Any] | None, legal_actions: list[str] | None
) -> bool:
    """Arena is asking for a combat damage assignment."""
    context = decision_context or game_state.get("decision_context") or {}
    if str(context.get("type") or "").lower() == "assign_damage":
        return True
    bridge = f"{game_state.get('_bridge_request_type') or ''} {game_state.get('_bridge_request_class') or ''}"
    if "AssignDamage" in bridge:
        return True
    return any(str(entry).strip().lower().startswith("assign damage") for entry in legal_actions or [])


def log_assigners(decision_context: dict[str, Any] | None) -> list[dict]:
    """The request's assigners from the log's decision context (raw AssignDamageReq), if present."""
    context = decision_context or {}
    raw = context.get("raw") if isinstance(context.get("raw"), dict) else {}
    assigners = raw.get("damageAssigners") or context.get("assigners") or []
    return [a for a in assigners if isinstance(a, dict)]


def fallback_plan(game_state: dict[str, Any], decision_context: dict[str, Any] | None, trigger: str = ""):
    """An ActionPlan that assigns the deterministic split (no usable model answer, #102).

    The action carries no distribution: the bridge executor splits the live
    request. The log's AssignDamageReq, when present, only words the reasoning.
    """
    from arenamcp.action_planner import ActionPlan, ActionType, GameAction

    context = decision_context or game_state.get("decision_context") or {}
    planned, _ = plan_assigners(log_assigners(context), game_state)
    split = submitted_distribution(planned, game_state) if planned else {}
    reasoning = "Lethal damage to each blocker first, the rest past them with trample"
    if split:
        reasoning += ": " + spoken_split(split)
    plan = ActionPlan(
        actions=[GameAction(action_type=ActionType.ASSIGN_DAMAGE, reasoning=reasoning + ".")],
        overall_strategy="[damage-assignment] lethal damage to each blocker first",
        trigger=trigger,
        turn_number=_int((game_state.get("turn") or {}).get("turn_number")),
        fallback_reason=FALLBACK_DAMAGE_ASSIGNMENT,
    )
    plan.voice_advice = plan.spoken_actions()
    return plan
