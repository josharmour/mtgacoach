"""Life that changes hands when a creature dies (ledger #184).

Match 4d934402 T11 (2026-10-19 16:07:42): Vein Ripper ("Whenever a creature
dies, target opponent loses 2 life and you gain 2 life.") was on their side
while Lagomos's Elemental and Urabrask's Forge's Phyrexian Horror attacked.
The block solver returned "Rabbit blocks Elemental, Rabbit blocks Phyrexian
Horror (2 dmg through; kills Elemental, Phyrexian Horror; loses Rabbit,
Rabbit)", the model followed the "Computed optimal blocks" line, and the four
deaths drained us 8 and gained them 8: 22-4 became 12-12, out of our lethal
range. Nothing in combat priced a death.

``death_triggers`` reads every permanent's "whenever a creature [you control |
an opponent controls] dies" triggers (and a creature's own "when this dies")
that make a player lose life, deal damage to a player or gain its controller
life. ``annotate_death_swings`` marks each creature on the battlefield with
``death_swing`` = [its controller's life change, the other player's life
change] when it dies; the combat solver charges that per death a plan causes.
A token sacrificed at the next end step (``token_lifetime``) dies anyway, so
killing it in combat causes no death that would not happen: it carries no
swing. Batch triggers ("one or more creatures die") and conditional effects
are not read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from arenamcp.token_lifetime import dies_anyway

# "Whenever a creature dies, ...", "Whenever Zulaport Cutthroat or another creature you control
# dies, ...", "When this creature dies, ..."; an ability word may lead.
_HEAD = re.compile(
    r"^(?:[a-z' ]+\s*[—–-]\s*)?(?P<when>when|whenever) (?P<subject>[^,]+?) "
    r"(?:dies|die|is put into a graveyard from the battlefield),\s*(?P<effect>.+)$"
)
_SUBJECT = re.compile(
    r"^(?:(?P<self>~|this [a-z]+) or )?(?P<det>a|an|another) (?P<nontoken>nontoken )?creature"
    r"(?P<token> token)?(?: or (?:planeswalker|artifact))?"
    r"(?: (?P<whose>you control|an opponent controls|your opponents control|you don't control))?$"
)
_OWN = re.compile(r"^(?:~|this [a-z]+|it)$")
_EACH_PLAYER = re.compile(
    r"\beach player loses (?P<n>\d+) life\b|\bdeals? (?P<m>\d+) damage to each player\b"
)
_TO_OPPONENT = re.compile(
    r"\b(?:target|each) opponent loses (?P<n>\d+) life\b|\btarget player loses (?P<p>\d+) life\b"
    r"|\bdeals? (?P<m>\d+) damage to (?:any (?:other )?target|target (?:opponent|player)"
    r"(?: or (?:planeswalker|battle))?|each opponent|target creature or player)\b"
)
_GAIN = re.compile(r"\byou gain (?P<n>\d+) life\b")
_GAIN_LOST = re.compile(r"\byou gain life equal to the life lost this way\b")
_SELF_LOSS = re.compile(r"\byou lose (?P<n>\d+) life\b|\bdeals? (?P<m>\d+) damage to you\b")
_CONDITION = re.compile(r"\b(?:if|unless|otherwise|instead|for each|equal to|where x|as long as|you may)\b")


@dataclass(frozen=True)
class DeathTrigger:
    """One "whenever ... dies" trigger and the life it moves per death."""

    source_id: int | None
    source: str  # the permanent's name
    seat: int | None  # its controller
    own_only: bool  # "When this creature dies": only its own death
    includes_self: bool  # its own death counts too ("a creature", "~ or another creature")
    whose: str  # "" any, "yours" its controller's creatures, "theirs" the other player's
    nontoken: bool
    token_only: bool
    loss: int  # each opponent of its controller loses (drain or damage)
    gain: int  # its controller gains
    self_loss: int  # its controller loses ("each player loses", "deals N damage to you")

    def counts(self, creature: dict) -> bool:
        """Whether ``creature``'s death fires this trigger."""
        own = self.source_id is not None and _int(creature.get("instance_id")) == self.source_id
        if own:
            return self.own_only or self.includes_self
        if self.own_only:
            return False
        token = _is_token(creature)
        if (self.nontoken and token) or (self.token_only and not token):
            return False
        controller = _controller(creature)
        if self.whose == "yours":
            return controller is not None and controller == self.seat
        if self.whose == "theirs":
            return controller is not None and controller != self.seat
        return True

    def describe(self, local_seat: int | None) -> str:
        """'Each creature death drains you 2 and gains them 2 (Vein Ripper)' from our side."""
        ours = self.seat is not None and self.seat == local_seat
        if self.own_only:
            who = f"{self.source}'s own death"
        else:
            kind = (
                "nontoken creature" if self.nontoken else "creature token" if self.token_only else "creature"
            )
            if self.whose == "yours":
                side = "your" if ours else "their"
                who = f"Each death of {side} {'other ' if not self.includes_self else ''}{kind}s"
            elif self.whose == "theirs":
                side = "their" if ours else "your"
                who = f"Each death of {side} {kind}s"
            else:
                who = f"Each {'other ' if not self.includes_self else ''}{kind} death"
        effects = []
        if self.loss:
            effects.append(f"drains {'them' if ours else 'you'} {self.loss}")
        if self.gain:
            effects.append(f"gains {'you' if ours else 'them'} {self.gain}")
        if self.self_loss:
            effects.append(f"costs {'you' if ours else 'them'} {self.self_loss}")
        owner = "yours" if ours else "theirs"
        return f"{who} {' and '.join(effects)} ({self.source}, {owner})"


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _controller(card: dict) -> int | None:
    return _int(
        card.get("controller_seat_id")
        if card.get("controller_seat_id") is not None
        else card.get("owner_seat_id")
    )


def _is_token(card: dict) -> bool:
    kind = str(card.get("object_kind") or "").lower()
    return (
        bool(card.get("is_token")) or "token" in kind or "token" in str(card.get("type_line") or "").lower()
    )


def _is_creature(card: dict) -> bool:
    types = f"{card.get('type_line') or ''} {' '.join(str(t) for t in card.get('card_types') or [])}".lower()
    return "creature" in types


@lru_cache(maxsize=2048)
def _parse(name: str, oracle: str) -> tuple[tuple[bool, bool, str, bool, bool, int, int, int], ...]:
    """(own only, includes itself, whose, nontoken, token only, loss, gain, self loss) per trigger."""
    text = re.sub(r"<[^>]*>", "", oracle).lower()
    while re.search(r"\([^()]*\)", text):
        text = re.sub(r"\([^()]*\)", "", text)
    lowered = name.lower()
    for variant in (lowered, lowered.split(",")[0].strip()):
        if variant and len(variant) > 2:
            text = text.replace(variant, "~")
    text = text.replace("cardname", "~")
    found = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line or line in seen:
            continue
        seen.add(line)
        head = _HEAD.match(line)
        if head is None:
            continue
        subject = head.group("subject").strip()
        effect = _GAIN_LOST.sub(lambda _m: "you gain {lost} life", head.group("effect"))
        if _CONDITION.search(effect):
            continue
        if _OWN.match(subject):
            own_only, includes_self, whose, nontoken, token_only = True, True, "", False, False
        else:
            match = _SUBJECT.match(subject)
            if match is None:
                continue  # "one or more ...", "enchanted creature", "a creature dealt damage by ~"
            own_only = False
            includes_self = bool(match.group("self")) or match.group("det") in ("a", "an")
            whose_text = match.group("whose") or ""
            whose = "yours" if whose_text == "you control" else "theirs" if whose_text else ""
            nontoken, token_only = bool(match.group("nontoken")), bool(match.group("token"))
        each = _EACH_PLAYER.search(effect)
        both = int(each.group("n") or each.group("m")) if each else 0
        to_opponent = _TO_OPPONENT.search(_EACH_PLAYER.sub("", effect))
        loss = (
            int(to_opponent.group("n") or to_opponent.group("p") or to_opponent.group("m"))
            if to_opponent
            else 0
        )
        if "{lost}" in effect:
            effect = effect.replace("{lost}", str(loss))
        gain_match = _GAIN.search(effect)
        gain = int(gain_match.group("n")) if gain_match else 0
        self_match = _SELF_LOSS.search(effect)
        self_loss = int(self_match.group("n") or self_match.group("m")) if self_match else 0
        loss, self_loss = loss + both, self_loss + both
        if loss or gain or self_loss:
            found.append((own_only, includes_self, whose, nontoken, token_only, loss, gain, self_loss))
    return tuple(found)


def card_death_triggers(card: dict) -> list[DeathTrigger]:
    """The death triggers printed on one permanent."""
    if not isinstance(card, dict) or card.get("is_phased_out") or card.get("face_down"):
        return []
    name = str(card.get("name") or "")
    return [
        DeathTrigger(
            source_id=_int(card.get("instance_id")),
            source=name or "a permanent",
            seat=_controller(card),
            own_only=own_only,
            includes_self=includes_self,
            whose=whose,
            nontoken=nontoken,
            token_only=token_only,
            loss=loss,
            gain=gain,
            self_loss=self_loss,
        )
        for own_only, includes_self, whose, nontoken, token_only, loss, gain, self_loss in _parse(
            name, str(card.get("oracle_text") or "")
        )
    ]


def death_triggers(battlefield: list | None) -> list[DeathTrigger]:
    """Every death trigger on the battlefield (both sides)."""
    return [t for card in battlefield or [] if isinstance(card, dict) for t in card_death_triggers(card)]


def death_swing(creature: dict, triggers: list[DeathTrigger]) -> tuple[int, int]:
    """(its controller's life change, the other player's) when ``creature`` dies now.

    0, 0 for a token sacrificed at the next end step: its death comes anyway.
    """
    if dies_anyway(creature):
        return 0, 0
    controller = _controller(creature)
    ours = theirs = 0
    for trigger in triggers:
        if trigger.seat is None or controller is None or not trigger.counts(creature):
            continue
        if trigger.seat == controller:
            ours += trigger.gain - trigger.self_loss
            theirs -= trigger.loss
        else:
            ours -= trigger.loss
            theirs += trigger.gain - trigger.self_loss
    return ours, theirs


def annotate_death_swings(battlefield: list | None) -> list[DeathTrigger]:
    """Mark each creature with ``death_swing`` (see ``death_swing``); return the triggers.

    Removes a stale mark, so it is safe to call again on the same board.
    """
    cards = [card for card in battlefield or [] if isinstance(card, dict)]
    triggers = death_triggers(cards)
    for card in cards:
        swing = death_swing(card, triggers) if triggers and _is_creature(card) else (0, 0)
        if swing != (0, 0):
            card["death_swing"] = list(swing)
        else:
            card.pop("death_swing", None)
    return triggers


def swing_of(card: Any) -> tuple[int, int]:
    """A creature's ``death_swing`` mark (a planner body's ``_card`` too), or (0, 0)."""
    if not isinstance(card, dict):
        return 0, 0
    value = card.get("death_swing")
    if value is None and isinstance(card.get("_card"), dict):
        value = card["_card"].get("death_swing")
    if not value or len(value) != 2:
        return 0, 0
    return int(value[0] or 0), int(value[1] or 0)


def death_trigger_notes(battlefield: list | None, local_seat: int | None) -> list[str]:
    """One prompt line per death trigger on the board, from our side ('drains you 2')."""
    notes = []
    for trigger in death_triggers(battlefield):
        note = trigger.describe(local_seat)
        if note not in notes:
            notes.append(note)
    return notes
