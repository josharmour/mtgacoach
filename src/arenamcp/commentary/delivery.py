"""Preserve ArenaOnAir delivery and give observed big moments an expressive voice."""

from .contracts import SpeechCategory
from .timeline import event_priority


def speech_delivery(item, observation, persona="classic"):
    level = item.excitement
    if level is None and item.category == SpeechCategory.COMMENTARY and observation is not None:
        events = [event for event in observation.events if event.event_id in item.evidence_ids]
        priorities = [event_priority(e, observation) for e in events if e.kind != "deck_overview"]
        priority = max(priorities, default=0)
        level = "electric" if priority >= 20 else "tense" if priority >= 10 else "normal"
        if events and all(
            e.kind in {"land_drop", "game_start", "turn_start", "deck_overview"} for e in events
        ):
            level = "calm"
    level = level or "normal"
    # Same salience lift and persona ceilings as ArenaOnAir's speech engine.
    if level == "normal":
        level = {0: "calm", 2: "tense", 3: "electric"}.get(item.salience, "normal")
    if persona in {"test_match", "pro_tour"} and level == "electric":
        level = "tense"
    return {
        "excitement": level,
        "energy": {"calm": 0.3, "normal": 0.5, "tense": 0.65, "electric": 0.8}[level],
    }
