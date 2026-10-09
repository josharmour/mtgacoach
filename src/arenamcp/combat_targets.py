"""Legal combat recipients shared by the planner and bridge submission."""

from typing import Any


def attack_candidates(state: dict, pending: dict | None = None) -> list[dict] | None:
    if pending is not None:
        if isinstance(pending.get("attackers"), list):
            return pending["attackers"]
        payload = pending.get("request_payload") or {}
        for key in ("qualifiedAttackers", "attackers"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return (state.get("decision_context") or {}).get("raw_attackers")


def recipient_key(recipient: dict) -> tuple[str, int]:
    for kind, member in (
        ("player", "playerSystemSeatId"),
        ("planeswalker", "planeswalkerInstanceId"),
        ("battle", "battleInstanceId"),
    ):
        value = recipient.get(member)
        if value is not None and int(value) > 0:
            return kind, int(value)
    raise ValueError("Combat recipient has no supported identity")


def recipient_label(recipient: dict, state: dict) -> str:
    kind, identity = recipient_key(recipient)
    if kind == "player":
        local = next(
            (player.get("seat_id") for player in state.get("players", []) if player.get("is_local")), None
        )
        return "You" if identity == local else "Opponent"
    card = next((card for card in state.get("battlefield", []) if card.get("instance_id") == identity), {})
    return f"{card.get('name') or kind.title()} [{identity}]"


def attack_target_prompt(state: dict, context: dict) -> str:
    raw = context.get("raw_attackers") or []
    names = {card.get("instance_id"): card.get("name") for card in state.get("battlefield", [])}
    lines = []
    for attacker in raw:
        labels = []
        for recipient in attacker.get("legalDamageRecipients") or []:
            try:
                labels.append(recipient_label(recipient, state))
            except (ValueError, TypeError):
                continue
        if labels:
            identity = attacker.get("attackerInstanceId")
            lines.append(f"{names.get(identity) or identity}: {'; '.join(labels)}")
    if not lines:
        return ""
    return (
        "\nATTACK RECIPIENTS (choose separately for each attacker; an attacker you name without a "
        "recipient attacks the opponent player):\n" + "\n".join(lines)
    )


def default_recipient(legal: list[dict], state: dict, attacker: dict | None = None) -> dict[str, Any] | None:
    """The recipient an unnamed attacker hits when several are legal, or None.

    bug_20261008_172415 (game 2, T18): the planner named Screeching
    Soulbreaker with no recipient while the opponent controlled a Jace, so
    submission raised and the window went MANUAL REQUIRED. The opponent
    player is the default; a planeswalker only when this attacker's damage
    kills it outright (power >= loyalty) and no untapped opposing creature
    can block the attacker, so the hit is certain.
    """
    players = [recipient for recipient in legal if _kind(recipient) == "player"]
    walkers = [recipient for recipient in legal if _kind(recipient) == "planeswalker"]
    if attacker and walkers:
        from arenamcp.combat_solver import _can_block
        from arenamcp.combat_strategy import loyalty

        try:
            power = int(attacker.get("power"))
        except (TypeError, ValueError):
            power = 0
        local = next((p.get("seat_id") for p in state.get("players", []) if p.get("is_local")), None)
        blockers = [
            card
            for card in state.get("battlefield", [])
            if (card.get("controller_seat_id") or card.get("owner_seat_id")) not in (None, local)
            and (
                "creature" in str(card.get("type_line", "")).lower()
                or "CardType_Creature" in (card.get("card_types") or [])
            )
            and not card.get("is_tapped")
        ]
        cards = {card.get("instance_id"): card for card in state.get("battlefield", [])}
        if power > 0 and not any(_can_block(attacker, blocker) for blocker in blockers):
            killable = []
            for recipient in walkers:
                walker = cards.get(recipient_key(recipient)[1]) or {}
                remaining = loyalty(walker)
                if remaining is not None and 0 < remaining <= power:
                    killable.append((remaining, recipient))
            if killable:
                return max(killable, key=lambda item: item[0])[1]
    if players:
        return players[0]
    if walkers:
        cards = {card.get("instance_id"): card for card in state.get("battlefield", [])}
        from arenamcp.combat_strategy import loyalty

        return min(walkers, key=lambda r: loyalty(cards.get(recipient_key(r)[1]) or {}) or 0)
    return None


def _kind(recipient: dict) -> str:
    try:
        return recipient_key(recipient)[0]
    except (TypeError, ValueError):
        return ""


def choose_recipient(
    target: str, legal: list[dict], state: dict, attacker: dict | None = None
) -> dict[str, Any]:
    if not target:
        if len(legal) == 1:
            return legal[0]
        picked = default_recipient(legal, state, attacker)
        if picked is not None:
            return picked
        raise ValueError("Choose an explicit player or planeswalker recipient for each attacker")
    query = target.strip().casefold()
    matches = []
    for recipient in legal:
        kind, identity = recipient_key(recipient)
        label = recipient_label(recipient, state).casefold()
        aliases = {label}
        if kind == "player" and label == "opponent":
            aliases.update({"opponent player", "opponent's face", "face"})
        elif kind != "player":
            aliases.add(label.rsplit(" [", 1)[0])
        if query in aliases:
            matches.append(recipient)
    if len(matches) != 1:
        raise ValueError(f"Attack recipient {target!r} is unavailable or ambiguous")
    return matches[0]
