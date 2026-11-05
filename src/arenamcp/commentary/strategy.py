"""Small, rule-grounded analyst observations when generation cannot run.

These are conditional strategic implications of supplied cards, not outcomes,
recommendations, guessed hidden cards, or a target number of spoken lines.
"""

import re

from arenamcp.player_names import actor_name


def strategic_observation(observation, event, *, private=False):
    if event.kind not in {"action_observed", "combat", "life_change", "outcome", "turn_start"}:
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
        if text in observation.recent_commentary or text in observation.recent_analysis:
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

    # Broaden the fallback beyond card text. These observations compare the
    # actual position and remain conditional about combat and future choices.
    def position(text, scope="public"):
        if text not in observation.recent_analysis and text not in observation.recent_commentary:
            return text, scope
        return None

    seats = [p.get("seat_id") for p in observation.public_state.get("players", ())]
    known_board = all(not c.get("face_down") and c.get("type_line") for c in zones.get("battlefield", ()))
    if known_board and len(seats) == 2 and all(type(s) is int for s in seats):
        creatures = {
            s: [
                c
                for c in board
                if c.get("controller_seat_id") == s and "creature" in c.get("type_line", "").lower()
            ]
            for s in seats
        }
        if all(creatures.values()):
            found = position(
                "Both sides have creatures committed; an attack could open a return attack, so the race depends on blocks as well as life totals."
            )
            if found:
                return found
        for s in seats:
            other = next(v for v in seats if v != s)
            if creatures[s] and not creatures[other]:
                found = position(
                    f"{actor_name(observation.public_state, other)} {'have' if actor_name(observation.public_state, other) == 'You' else 'has'} no creatures in play; rebuilding blockers could change the race against {actor_name(observation.public_state, s)}."
                )
                if found:
                    return found
        lands = {
            s: sum(
                c.get("controller_seat_id") == s and "land" in c.get("type_line", "").lower() for c in board
            )
            for s in seats
        }
        leader = max(seats, key=lands.get)
        if lands[leader] >= min(lands.values()) + 2:
            found = position(
                f"{actor_name(observation.public_state, leader)} {'have' if actor_name(observation.public_state, leader) == 'You' else 'has'} more lands in play; that could support developing threats while keeping interaction available."
            )
            if found:
                return found
    hand = observation.private_state.get("hand") if private else None
    if isinstance(hand, list) and len(hand) == 1:
        return position(
            "With one card in your hand, another exchange could leave the next turn dependent on a draw.", ""
        )
    return None
