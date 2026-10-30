"""Reuse displayed action explanations, never raw model traces or planned outcomes.

Only a successfully submitted cast can contribute, and only after a matching
public GRE cast is observed. A bounded vocabulary extracts the stated tactical
purpose; numbers, hidden card identities and predicted results are not copied.
"""

import re
import time
from dataclasses import replace


def remember_explanation(engine, decision, option_ids, explanation, snapshot):
    if decision.request_type != "ActionsAvailable" or len(option_ids) != 1:
        return
    option = decision.find(option_ids[0])
    if not option or option.meta.get("actionType") != "ActionType_Cast":
        return
    iid, gid = option.meta.get("instanceId"), option.meta.get("grpId")
    if type(iid) is not int or not iid or type(gid) is not int or not gid:
        return
    if not isinstance(explanation, str) or not explanation.strip():
        return
    turn = snapshot.get("turn_info") or snapshot.get("turn") or {}
    record = {
        "match_id": snapshot.get("match_id"),
        "game_number": snapshot.get("game_number"),
        "seat": snapshot.get("local_seat_id"),
        "instance_id": iid,
        "grp_id": gid,
        "turn": turn.get("turn_number"),
        "received_at": time.monotonic(),
        "sequence": snapshot.get("commentary_source_sequence", 0),
        "explanation": explanation[:1200],
    }
    prior = getattr(engine, "_commentary_explanations", ())

    # Same typed submission retry must not create ambiguous duplicate candidates.
    def key(r):
        return (r["match_id"], r["game_number"], r["turn"], r["instance_id"])

    engine._commentary_explanations = tuple(r for r in prior[-15:] if key(r) != key(record)) + (record,)


def purpose(explanation, public_cards=()):
    """Conservative, explicit intent paraphrase, not a fresh strategic judgment."""
    text = explanation.lower()
    # The action label alone contributes nothing; use only the displayed rationale.
    text = re.sub(r"^casting [^.]+\.\s*", "", text)
    if not text or re.search(r"\b(?:do not|don't|should not|shouldn't|mistake|misplay)\b", text):
        return ""
    purposes = []
    if re.search(r"\b(?:prowess|pumps?|pump spell)\b", text):
        # Keep a concrete, already-public synergy from the explanation. Do not
        # copy its predicted power, damage total, or unobserved resolution.
        threat = next(
            (
                c.get("name")
                for c in public_cards
                if c.get("name")
                and not c.get("face_down")
                and re.search(
                    r"prowess|whenever you cast a noncreature spell", str(c.get("oracle_text") or ""), re.I
                )
                and any(
                    re.search(r"\b" + re.escape(word.lower()) + r"\b", text)
                    for word in c["name"].split()
                    if len(word) >= 6
                )
            ),
            None,
        )
        purposes.append(f"build up {threat or 'a threat'} while advancing the race")
    elif re.search(r"\b(?:race|pressure|reach|chip|chips|burn)\b", text):
        purposes.append("keep pressure on the opponent")
    if re.search(r"\b(?:block|blocker|blocking|defend|defense|stabilize)\b", text):
        purposes.append("strengthen the defense")
    if re.search(r"\b(?:no.lifegain|prevent.{0,15}lifegain|shut.{0,15}lifegain)\b", text):
        purposes.append("deny lifegain")
    if re.search(r"\b(?:protect|protection|save|shield)\b", text):
        purposes.append("protect an important permanent")
    if re.search(r"\b(?:card advantage|draw cards|refill|replenish)\b", text):
        purposes.append("rebuild resources for later turns")
    if re.search(r"\b(?:ramp|accelerate|develop mana)\b", text):
        purposes.append("develop mana for bigger plays")
    if re.search(r"\b(?:remove|removal|answer)\b", text) and not purposes:
        purposes.append("answer an opposing threat")
    if not purposes:
        return ""
    return "The plan behind that play was to " + " and ".join(purposes[:2]) + "."


def attach_rationales(events, snapshot, private, now):
    """No private explanation crosses the explicit own-hand/deck consent gate."""
    if not private:
        return tuple(events)
    records = snapshot.get("commentary_action_explanations", ())[-16:]
    result = []
    for event in events:
        if event.kind != "action_observed" or event.seat != snapshot.get("local_seat_id"):
            result.append(event)
            continue
        candidates = [
            r
            for r in records
            if r.get("match_id") == snapshot.get("match_id")
            and r.get("game_number") == snapshot.get("game_number")
            and r.get("seat") == event.seat
            and r.get("turn") == event.turn_number
            and r.get("grp_id")
            and r.get("grp_id") == event.payload.get("grp_id")
            and 0 <= now - r.get("received_at", 0) < 30
            # The callback follows submission; the GRE reader can win that race.
            and -1 <= event.received_at - r.get("received_at", 0) < 15
            and event.source_sequence > r.get("sequence", 0)
        ]
        exact = [r for r in candidates if r.get("instance_id") == event.payload.get("instance_id")]
        candidates = exact or candidates
        if len(candidates) == 1:
            from .observations import _public_card

            cards = [_public_card(c) for c in (snapshot.get("zones") or {}).get("battlefield", ())]
            intent = purpose(candidates[0].get("explanation", ""), cards)
            if intent:
                event = replace(event, payload={**event.payload, "coach_intent": intent})
        result.append(event)
    return tuple(result)
