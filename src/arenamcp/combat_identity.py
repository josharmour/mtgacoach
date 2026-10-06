"""Resolve combat labels against legal instance IDs without guessing a copy."""

import re
from collections import Counter


def plain_combat_name(label: str) -> str:
    label = re.sub(r"\s*\[(?:id:)?\d+\]\s*$", "", label.strip(), flags=re.I)
    return label.lstrip("*").strip()


def _local_seat(state: dict):
    return state.get("local_seat_id") or next(
        (p.get("seat_id") for p in state.get("players", []) if p.get("is_local")), None
    )


def _board(state: dict, local_side: bool) -> list[dict]:
    local = _local_seat(state)
    return [
        card
        for card in state.get("battlefield", [])
        if isinstance(card, dict)
        and card.get("instance_id")
        and str(card.get("type_line", "")).lower() != "ability"
        and (
            local is None
            or not (card.get("controller_seat_id") or card.get("owner_seat_id"))
            or ((card.get("controller_seat_id") or card.get("owner_seat_id")) == local) == local_side
        )
    ]


def _labels(cards: list[dict]) -> dict[int, str]:
    counts = Counter(str(c.get("name") or "Unknown").casefold() for c in cards)
    seen = Counter()
    labels = {}
    for card in cards:
        name = str(card.get("name") or "Unknown")
        seen[name.casefold()] += 1
        labels[int(card["instance_id"])] = (
            f"{name} #{seen[name.casefold()]}" if counts[name.casefold()] > 1 else name
        )
    return labels


def resolve_combatant(label: str, state: dict, eligible: list[int], *, local_side: bool) -> int:
    """Prefer explicit IDs; accept names only when board/menu meanings agree.

    Board ordinals include tapped/ineligible copies; menu ordinals only include
    legal candidates. A token '*' is the board's annotation, not its name.
    """
    eligible = list(dict.fromkeys(int(i) for i in eligible))
    explicit = re.search(r"\[(?:id:)?(\d+)\]\s*$", label, re.I)
    if explicit is None:
        explicit = re.fullmatch(r"Creature\s*(\d+)", label.strip(), re.I)
    if explicit:
        identity = int(explicit.group(1))
        if identity in eligible:
            return identity
        raise ValueError(f"Combat instance {identity} is not eligible")
    query = plain_combat_name(label).casefold()
    if not query:
        raise ValueError("Combat creature name is missing")
    cards = _board(state, local_side)
    by_id = {int(c["instance_id"]): c for c in cards}
    board_labels = _labels(cards)
    menu_labels = _labels([by_id[i] for i in eligible if i in by_id])
    token_marker = label.strip().startswith("*")
    matches = {
        identity
        for identity, name in board_labels.items()
        if name.casefold() == query
        and (
            not token_marker
            or by_id[identity].get("is_token")
            or "token" in str(by_id[identity].get("object_kind", "")).lower()
        )
    }
    if not token_marker:
        matches.update(identity for identity, name in menu_labels.items() if name.casefold() == query)
    if not matches and "#" not in query:
        matches = {
            identity
            for identity in eligible
            if identity in by_id
            and query == str(by_id[identity].get("name") or "").casefold()
            and (
                not token_marker
                or by_id[identity].get("is_token")
                or "token" in str(by_id[identity].get("object_kind", "")).lower()
            )
        }
    if len(matches) > 1 and not local_side and matches <= set(eligible):
        # 2026-10-06 15:23:16: "block Void Extrapolator" while two identical
        # hasty 3/3 copies attacked was rejected as ambiguous, the planner
        # produced no block and the autopilot went MANUAL REQUIRED. Blocking
        # either of two interchangeable attackers is the same block.
        copies = [by_id.get(identity) for identity in matches]
        if _interchangeable(copies, state):
            return min(matches)
    if len(matches) != 1 or next(iter(matches)) not in eligible:
        raise ValueError(f"Combat creature {label!r} is unavailable or ambiguous; use its instance ID")
    return next(iter(matches))


def _interchangeable(cards: list, state: dict) -> bool:
    """Identical copies: same name, known stats, text, keywords, counters, status; nothing attached.

    A token copy and the original card are not interchangeable (killing the
    original is worth more), and unknown power/toughness never counts as equal.
    """
    if len(cards) < 2 or any(not isinstance(card, dict) for card in cards):
        return False
    identities = {card.get("instance_id") for card in cards}
    for permanent in state.get("battlefield", []) or []:
        if isinstance(permanent, dict) and permanent.get("attached_to_id") in identities:
            return False

    def key(card: dict):
        power, toughness = card.get("power"), card.get("toughness")
        if not isinstance(power, int) or not isinstance(toughness, int) or card.get("attached_with_ids"):
            return None
        return (
            str(card.get("name") or "").casefold(),
            power,
            toughness,
            card.get("oracle_text") or "",
            tuple(sorted(str(k) for k in card.get("keywords") or [])),
            tuple(sorted((str(k), str(v)) for k, v in (card.get("counters") or {}).items())),
            bool(card.get("is_tapped")),
            bool(card.get("is_attacking")),
            card.get("damage") or 0,
            str(card.get("object_kind") or ""),
            bool(card.get("is_token")),
            tuple(sorted(str(a) for a in card.get("granted_abilities") or [])),
        )

    keys = {key(card) for card in cards}
    return len(keys) == 1 and None not in keys


def blocker_id_assignments(assignments: dict[str, str], state: dict, blockers: list[dict]) -> dict[int, int]:
    by_id = {int(b["blockerInstanceId"]): b for b in blockers}
    result = {}
    for blocker_name, attacker_name in assignments.items():
        blocker_id = resolve_combatant(blocker_name, state, list(by_id), local_side=True)
        attacker_id = resolve_combatant(
            attacker_name, state, by_id[blocker_id].get("attackerInstanceIds") or [], local_side=False
        )
        if blocker_id in result:
            raise ValueError(f"Blocker {blocker_id} was assigned twice")
        result[blocker_id] = attacker_id
    return result


def combat_identity_prompt(state: dict, context: dict) -> str:
    blockers = context.get("raw_blockers") or []
    attackers = context.get("raw_attackers") or []
    cards = {int(c["instance_id"]): c for c in state.get("battlefield", []) if c.get("instance_id")}

    def label(identity):
        card = cards.get(int(identity), {})
        token = "*" if card.get("is_token") or "token" in str(card.get("object_kind", "")).lower() else ""
        return f"{token}{card.get('name') or 'Creature'} [id:{identity}]"

    lines = [
        f"{label(b['blockerInstanceId'])} may block: "
        + "; ".join(label(i) for i in b.get("attackerInstanceIds") or [])
        for b in blockers
        if b.get("blockerInstanceId")
    ]
    lines.extend(label(a["attackerInstanceId"]) for a in attackers if a.get("attackerInstanceId"))
    if not lines:
        return ""
    return (
        "\nCOMBAT IDENTITIES: use these exact labels including [id:N] in assignments/attacker_names. "
        "The * marks a token; board and legal-menu #numbers can differ.\n" + "\n".join(lines)
    )
