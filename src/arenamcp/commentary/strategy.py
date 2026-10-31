"""Small, rule-grounded analyst observations when generation cannot run.

These are conditional strategic implications of supplied cards, not outcomes,
recommendations, guessed hidden cards, or a target number of spoken lines.
"""

import re


def strategic_observation(observation, event):
    if event.kind not in {"action_observed", "combat", "life_change", "outcome"}:
        return None
    zones = observation.public_state.get("zones", {})
    board = [c for c in zones.get("battlefield", []) if c.get("name") and not c.get("face_down")]
    local = observation.public_state.get("local_seat_id")
    # Projection adapters may put local identity on players instead.
    local = local or next(
        (p.get("seat_id") for p in observation.public_state.get("players", []) if p.get("is_local")), None
    )
    seat = event.seat or local
    own = [c for c in board if c.get("controller_seat_id") == seat]

    def rules(c):
        return str(c.get("oracle_text") or "").lower().replace("’", "'")

    def candidate(text, cards):
        if text in observation.recent_commentary:
            return None
        ids = sorted({c.get("instance_id") for c in cards if type(c.get("instance_id")) is int})
        return (text, "cards:" + ",".join(map(str, ids))) if ids else None

    mobilizers = [c for c in own if re.search(r"\bmobilize\s+[1-9]", rules(c))]
    token_engines = [
        c
        for c in own
        if "whenever a token you control enters" in rules(c)
        and "draw a card" in rules(c)
        and "gain" in rules(c)
    ]
    if mobilizers and token_engines:
        a, b = mobilizers[0], token_engines[0]
        found = candidate(
            f"{a['name']}'s Mobilize can feed {b['name']}'s token-entry rewards; keeping that engine alive matters beyond this combat.",
            [a, b],
        )
        if found:
            return found
    leave_engines = [
        c
        for c in own
        if "whenever another permanent you control leaves the battlefield during your turn" in rules(c)
        and "create a 1/1" in rules(c)
    ]
    if mobilizers and leave_engines:
        a, b = mobilizers[0], leave_engines[0]
        found = candidate(
            f"{a['name']}'s temporary Warriors can leave lasting value through {b['name']}; protecting that engine matters even when the attack stalls.",
            [a, b],
        )
        if found:
            return found
    entry_engines = [
        c
        for c in own
        if "whenever a creature you control enters" in rules(c)
        and "plan counter" in rules(c)
        and "draw a card" in rules(c)
    ]
    if mobilizers and entry_engines:
        a, b = mobilizers[0], entry_engines[0]
        found = candidate(
            f"{a['name']}'s Mobilize can advance {b['name']} toward its card and team-counter payoff, so an attack can develop resources beyond damage.",
            [a, b],
        )
        if found:
            return found
    prowess = [c for c in own if re.search(r"\bprowess\b|whenever you cast a noncreature spell", rules(c))]
    if prowess:
        a = prowess[0]
        found = candidate(
            f"With {a['name']} established, noncreature spells can build attacking power before combat; spending them afterward gives up that pressure.",
            [a],
        )
        if found:
            return found
    if mobilizers:
        a = mobilizers[0]
        found = candidate(
            f"{a['name']}'s Mobilize rewards attacking even when it cannot get through itself; the extra bodies can force different blocks.",
            [a],
        )
        if found:
            return found
    for c in own:
        if "opponent" in rules(c) and "can't gain life" in rules(c):
            found = candidate(
                f"{c['name']} shuts off opposing lifegain while it remains in play, making it harder to recover from an aggressive start.",
                [c],
            )
            if found:
                return found
    return None
