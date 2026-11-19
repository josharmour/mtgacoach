"""Preserve ArenaOnAir delivery and give observed big moments an expressive voice."""

from .config import normalize_persona
from .contracts import SpeechCategory
from .timeline import event_priority

LEVELS = ("calm", "normal", "tense", "electric")
ENERGY = {"calm": 0.3, "normal": 0.5, "tense": 0.65, "electric": 0.8}


def delivery_level(persona, role, excitement):
    level = LEVELS.index(excitement) if excitement in LEVELS else 1
    ceiling = 1 if normalize_persona(persona) == "technical" else 2 if role == "color_analyst" else 3
    return LEVELS[min(level, ceiling)]


def event_excitement(event):
    if event.kind == "game_end":
        return "electric"
    if event.kind == "life_change":
        after, delta = event.payload.get("to"), event.payload.get("delta")
        if type(after) is int and after <= 3:
            return "electric"
        if type(delta) is int and abs(delta) >= 5:
            return "tense"
    if event.kind in {"combat", "outcome"}:
        return "tense"
    if event.kind in {"deck_overview", "game_start", "turn_start", "land_drop"}:
        return "calm"
    return "normal"


def kokoro_delivery(text, speed, excitement):
    # Kokoro has no emotion input. Modest cadence and punctuation changes use
    # its supported controls; Chatterbox receives the actual emotion separately.
    cadence = {"calm": 0.97, "normal": 1.0, "tense": 1.03, "electric": 1.06}[excitement]
    if excitement == "electric" and text.endswith("."):
        text = text[:-1] + "!"
    return text, max(0.5, min(2.0, speed * cadence))


_ELECTRIC_CUES = (
    "lethal",
    "for the win",
    "win the game",
    "wins the game",
    "swing all",
    "attack all",
    "alpha strike",
)
_TENSE_CUES = (
    "watch out",
    "be careful",
    "careful",
    "must block",
    "have to block",
    "need to block",
    "must answer",
    "counterspell",
    "hold up",
    "open mana",
    "danger",
    "warning",
    "don't block",
    "do not block",
    "don't attack",
    "do not attack",
)
_CALM_CUES = (
    "no attacks",
    "no blocks",
    "easy keep",
    "clear keep",
    "pass the turn",
    "pass turn",
    "nothing to do",
    "end step",
)


def infer_advice_excitement(text: str) -> str:
    """Classify coach advice urgency into a delivery excitement level.

    Returns one of ``"calm"``, ``"normal"``, ``"tense"``, or ``"electric"``.
    """
    cleaned = str(text or "").strip()
    if not cleaned:
        return "normal"
    lowered = cleaned.lower()
    if "!" in cleaned or any(cue in lowered for cue in _ELECTRIC_CUES):
        return "electric"
    if any(cue in lowered for cue in _TENSE_CUES):
        return "tense"
    if any(cue in lowered for cue in _CALM_CUES):
        return "calm"
    return "normal"


def speech_delivery(item, observation, persona="technical"):
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
    # Preserve source salience, then apply the selected style and caster role.
    if level == "normal":
        level = {0: "calm", 2: "tense", 3: "electric"}.get(item.salience, "normal")
    level = delivery_level(persona, item.role, level)
    return {
        "excitement": level,
        "energy": ENERGY[level],
    }
