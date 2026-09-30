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
    return "\nATTACK RECIPIENTS (choose separately for each attacker):\n" + "\n".join(lines)


def choose_recipient(target: str, legal: list[dict], state: dict) -> dict[str, Any]:
    if not target:
        if len(legal) == 1:
            return legal[0]
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
