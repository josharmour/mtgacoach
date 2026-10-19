"""The autopilot's memory of its own recent decisions, and which decisions deserve more thought.

2026-10-10 FRA quick draft, game 1: at 14:57:36 the typed decision passed,
saying "discarding Proft (our threshold closer) to kill a 2/1 is a bad trade";
five seconds later, after a land drop, it discarded Proft for one mana to kill
that creature. At 14:59:18 it kept Jace at 1 loyalty ("his -1 would sacrifice
him for little") and three seconds later used that loyalty for a surveil.
Each typed decision was a fresh prompt with no memory of the previous answer,
and every one ran at low reasoning effort.

This module is deterministic. It keeps the last few decisions of the match
(what was chosen, why, and what the board looked like), renders them for the
next decision prompt and for the background game-plan reform, detects a
reversal (taking an action the planner declined moments ago on the same
board, unless that pass kept the card for this later step), and classifies
the few decisions that should run at high reasoning effort: removal that can
kill something, spending a card the game plan names, and target selection
for a removal effect.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Decisions kept per match; the prompt shows only the current turn's.
MEMORY_LIMIT = 48
# Recent actions shown to the game-plan reform.
REFORM_ACTIONS = 8

_REMOVAL_RE = re.compile(
    r"(?:destroy|exile) (?:target|each|all|another target|up to \w+ target) "
    r"(?:creature|permanent|artifact|enchantment|planeswalker|nonland)"
    r"|deals? (?:\d+|x) damage to (?:any target|target creature|target planeswalker|each creature)"
    r"|target creature (?:an opponent controls )?gets [-−]\d+/[-−]\d+"
    r"|target creature[^.]{0,40}gets [-−]\d+/[-−]\d+",
    re.IGNORECASE,
)


# The steps of a turn in order; ``turn_window`` indexes into it.
_TURN_STEPS = (
    "upkeep",
    "draw",
    "main1",
    "begincombat",
    "declareattack",
    "declareblock",
    "firststrikedamage",
    "combatdamage",
    "endcombat",
    "main2",
    "end",
    "cleanup",
)

# A pass that keeps a card for a later point of the turn ("holding Shock as
# post-combat reach", "Shock face after combat").
_LATER_IN_TURN_RE = re.compile(
    r"post-?combat|after (?:combat|blocks|blockers|the attack|attacking|combat damage|damage)"
    r"|second main|main ?2|end of (?:the |their )?turn|end step|their turn"
    r"|\b(?:as|for) (?:[\w-]+ )?reach\b|\blater\b",
    re.IGNORECASE,
)


def turn_window(state: dict | None) -> int | None:
    """Where in the turn ``state`` is, as an index into the turn's steps; None when unknown."""
    if not isinstance(state, dict):
        return None
    turn = state.get("turn") or {}

    def norm(value: Any) -> str:
        return re.sub(r"^(?:phase|step)_", "", str(value or "").strip().lower()).replace("_", "")

    phase, step = norm(turn.get("phase")), norm(turn.get("step"))
    if phase in ("main1", "main2"):
        return _TURN_STEPS.index(phase)  # a main phase has no step (an old one may linger)
    for name in (step, phase):
        if name in _TURN_STEPS:
            return _TURN_STEPS.index(name)
    if "combat" in phase:
        return _TURN_STEPS.index("begincombat")
    if "ending" in phase:
        return _TURN_STEPS.index("end")
    if "beginning" in phase:
        return _TURN_STEPS.index("upkeep")
    return None


@dataclass
class DecisionRecord:
    turn: int
    request_type: str
    source: str
    chosen: list[str]
    chosen_labels: list[str]
    reasoning: str
    option_labels: dict[str, str] = field(default_factory=dict)
    signature: tuple = ()
    window: int | None = None

    def line(self) -> str:
        what = ", ".join(self.chosen_labels) or ", ".join(self.chosen) or "(nothing)"
        head = f"{self.request_type}" + (f" ({self.source})" if self.source else "")
        why = f" — {self.reasoning}" if self.reasoning else ""
        return f"T{self.turn} {head}: {what}{why}"


def _is_land(card: dict) -> bool:
    types = [str(t) for t in (card.get("card_types") or card.get("types") or [])]
    return any("Land" in t for t in types) or "Land" in str(card.get("type_line") or "")


def _local_seat(state: dict) -> int | None:
    seat = state.get("local_seat_id")
    if seat is None:
        for player in state.get("players") or []:
            if player.get("is_local"):
                return player.get("seat_id")
    return seat


def board_signature(state: dict | None) -> tuple:
    """What must be unchanged for a later choice to count as reversing an earlier one.

    Turn, life totals, the nonland permanents on both sides and the stack.
    A land drop or a mana float between two decisions does not change it;
    a creature entering, dying, or a spell on the stack does.
    """
    if not isinstance(state, dict):
        return ()
    turn = int(((state.get("turn") or {}).get("turn_number")) or 0)
    life = tuple(
        (player.get("seat_id"), player.get("life") if "life" in player else player.get("life_total"))
        for player in sorted(state.get("players") or [], key=lambda p: p.get("seat_id") or 0)
    )
    permanents = tuple(
        sorted(
            int(card.get("instance_id") or 0) for card in state.get("battlefield") or [] if not _is_land(card)
        )
    )
    return (turn, life, permanents, len(state.get("stack") or []))


class DecisionMemory:
    """Bounded per-match log of the planner's own typed decisions."""

    def __init__(self, limit: int = MEMORY_LIMIT) -> None:
        self._limit = limit
        self._match_id: Any = None
        self._records: list[DecisionRecord] = []

    def reset(self) -> None:
        self._records = []

    def _sync_match(self, state: dict) -> None:
        match_id = state.get("match_id")
        if match_id != self._match_id:
            self._match_id = match_id
            self._records = []

    def record(self, state: dict, decision: Any, chosen: list[str], reasoning: str) -> DecisionRecord | None:
        if not isinstance(state, dict) or decision is None:
            return None
        self._sync_match(state)
        labels = {option.option_id: option.label for option in getattr(decision, "options", ()) or ()}
        chosen = [str(c) for c in (chosen or [])]
        record = DecisionRecord(
            turn=int(((state.get("turn") or {}).get("turn_number")) or 0),
            request_type=str(getattr(decision, "request_type", "") or ""),
            source=str(getattr(decision, "source_label", "") or ""),
            chosen=chosen,
            chosen_labels=[labels.get(c, c) for c in chosen],
            reasoning=" ".join(str(reasoning or "").split()[:40]),
            option_labels=labels,
            signature=board_signature(state),
            window=turn_window(state),
        )
        self._records.append(record)
        del self._records[: -self._limit]
        return record

    def this_turn(self, state: dict | None) -> list[DecisionRecord]:
        if not isinstance(state, dict):
            return []
        if state.get("match_id") != self._match_id:
            return []
        turn = int(((state.get("turn") or {}).get("turn_number")) or 0)
        return [r for r in self._records if r.turn == turn]

    def prompt_block(self, state: dict | None) -> str:
        """The current turn's decisions for the typed-decision prompt ("" when none)."""
        records = self.this_turn(state)
        if not records:
            return ""
        lines = ["DECISIONS YOU ALREADY MADE THIS TURN (oldest first):"]
        lines.extend(f"  - {record.line()}" for record in records[-6:])
        lines.append(
            "  Stay consistent with these: do not take an action you declined above unless the "
            "board changed (new permanent, spell on the stack, life change, mana that was missing "
            "is now available) and your reasoning names that change. A reason you gave for holding "
            "a card or keeping a planeswalker alive still applies now."
        )
        return "\n".join(lines)

    def reversal(self, state: dict | None, decision: Any, chosen: list[str]) -> DecisionRecord | None:
        """The earlier pass this turn that ``chosen`` reverses on an unchanged board, else None."""
        if not isinstance(state, dict) or len(chosen or []) != 1:
            return None
        if str(getattr(decision, "request_type", "")) != "ActionsAvailable":
            return None
        option_id = str(chosen[0])
        if option_id == "pass":
            return None
        labels = {option.option_id: option.label for option in getattr(decision, "options", ()) or ()}
        label = labels.get(option_id)
        if not label:
            return None
        signature = board_signature(state)
        for record in reversed(self.this_turn(state)):
            if record.request_type != "ActionsAvailable" or record.chosen != ["pass"]:
                continue
            if record.signature != signature:
                continue
            if label in record.option_labels.values():
                return record
        return None

    @staticmethod
    def stated_plan_followed(
        record: DecisionRecord | None, state: dict | None, decision: Any, chosen: list[str]
    ) -> str:
        """The phrase by which ``record``'s pass kept the chosen card for later, once the turn got there; else "".

        2026-10-19 08:04:11 (Mono-Red Burn): the autopilot passed in the
        declare-attackers step "holding Shock as post-combat reach"; at 08:04:23,
        with blockers declared, it chose Shock, and the reversal check called that
        a contradiction ("nothing on the board has changed") and asked again at
        high effort (another 12.7 s; "Coaching loop stalled ... ~23s"). Using a
        card in a later step of the turn the pass itself named for it is that
        pass's plan, not a reversal. A pass that rejected the card ("a bad
        trade") names no later point, and the same step is never later.
        """
        if record is None or not isinstance(state, dict) or len(chosen or []) != 1:
            return ""
        now = turn_window(state)
        if now is None or record.window is None or now <= record.window:
            return ""
        option = next(
            (o for o in getattr(decision, "options", ()) or () if o.option_id == str(chosen[0])), None
        )
        if option is None:
            return ""
        name = str(_source_card(option, state).get("name") or "")
        if not name:
            label = str(option.label or "").split(" [", 1)[0]
            name = re.sub(r"^(?:cast|activate(?: ability)?:?|play land:)\s*", "", label, flags=re.IGNORECASE)
        if not _names_in_plan(name.strip(), record.reasoning):
            return ""
        match = _LATER_IN_TURN_RE.search(record.reasoning)
        return match.group(0) if match else ""

    def lines_for_turn(self, turn: int) -> list[str]:
        """The decisions recorded for one turn of the current match, oldest first."""
        return [r.line() for r in self._records if r.turn == turn]

    def recent_actions_text(self, limit: int = REFORM_ACTIONS) -> str:
        """The latest decisions, one per line, for the game-plan reform prompt."""
        return "\n".join(f"  - {record.line()}" for record in self._records[-limit:])


def _source_card(option: Any, state: dict) -> dict:
    meta = getattr(option, "meta", {}) or {}
    instance_id = meta.get("instanceId")
    for zone in ("hand", "battlefield", "command", "graveyard", "exile", "stack"):
        for card in state.get(zone) or []:
            if instance_id and card.get("instance_id") == instance_id:
                return dict(card, _zone=zone)
    return {}


_TARGET_WORDS = ("creature", "planeswalker", "artifact", "enchantment", "permanent", "nonland", "any target")


def _removal_has_target(oracle: str, state: dict) -> bool:
    """Whether the removal text's target type exists on the opponent's side (2026-10-10 Brawl).

    Chomping Changeling ("destroy target artifact or enchantment") escalated
    every priority window of a game where the opponent had neither.
    """
    local = _local_seat(state)
    theirs = [
        card
        for card in state.get("battlefield") or []
        if local is None or card.get("controller_seat_id") not in (None, local)
    ]
    wanted: set[str] = set()
    for match in _REMOVAL_RE.finditer(oracle):
        clause = match.group(0).lower()
        for word in _TARGET_WORDS:
            if word in clause:
                wanted.add(word)
        # "destroy target artifact or enchantment": the second noun follows "or".
        for word in re.findall(
            r"\bor (creature|planeswalker|artifact|enchantment|permanent)\b",
            oracle[match.end() : match.end() + 40].lower(),
        ):
            wanted.add(word)
    if not wanted or "any target" in wanted or "permanent" in wanted or "nonland" in wanted:
        return True
    for card in theirs:
        types = " ".join(str(t) for t in (card.get("card_types") or card.get("types") or [])).lower()
        types += " " + str(card.get("type_line") or "").lower()
        if any(word in types for word in wanted):
            return True
    return False


_DAMAGE_CLAUSE_RE = re.compile(
    r"deals? (\d+|x) damage to (any target|target creature|target planeswalker|each creature)", re.IGNORECASE
)
# Burst Lightning: "If this spell was kicked, it deals 4 damage instead."
_DAMAGE_INSTEAD_RE = re.compile(r"deals? (\d+) damage instead", re.IGNORECASE)
_KICKER_RE = re.compile(r"\bkicker ((?:\{[^}]+\})+)", re.IGNORECASE)


def _kicker_affordable(card: dict, oracle: str, state: dict) -> bool:
    """Whether the mana for this spell plus its kicker can be there this turn (yes when it cannot be told).

    Our untapped lands, or in our main phase the turn's mana potential (land
    drop and mana creatures counted) when that is more.
    """
    kicker = _KICKER_RE.search(oracle)
    if kicker is None:
        return True
    try:
        from arenamcp.mana import mana_cost_to_cmc
        from arenamcp.mana_potential import turn_mana_potential

        need = mana_cost_to_cmc(card.get("mana_cost")) + mana_cost_to_cmc(kicker.group(1))
        potential = turn_mana_potential(state)
    except Exception:
        return True
    local = _local_seat(state)
    have = sum(
        1
        for c in state.get("battlefield") or []
        if c.get("controller_seat_id") == local and _is_land(c) and not c.get("is_tapped")
    )
    if potential is not None:
        have = max(have, potential.total_with_enabler)
    return have >= need


def _types(card: dict) -> str:
    types = " ".join(str(t) for t in (card.get("card_types") or card.get("types") or []))
    return f"{types} {card.get('type_line') or ''}".lower()


def _damage_may_kill(creature: dict, amount: int) -> bool:
    """Whether ``amount`` damage may kill this creature now: unknown toughness and a combat in progress count."""
    toughness = creature.get("toughness")
    if type(toughness) is not int:
        return True
    marked = creature.get("damage") if type(creature.get("damage")) is int else 0
    if toughness - marked <= amount:
        return True
    return bool(
        creature.get("is_attacking") or creature.get("is_blocking") or creature.get("damaged_this_turn")
    )


def burn_kills_nothing(oracle: str, state: dict, castable: dict | None = None) -> bool:
    """Damage-only removal that can kill no opposing creature or planeswalker now: it can only go face.

    2026-10-19 Mono-Red Burn (07:15-09:08): Shock, Burst Lightning and
    Lightning Strike ("deals N damage to any target") were in hand almost
    every window, so 151 typed decisions ran the high-effort hedge, many of
    them against a 4/4 Bristly Bill or a 4-toughness Dawnwing Marshal the burn
    could not kill. Burning face is the race the board math and the game plan
    already count, not removal. Destroy, exile and -N/-N effects, X damage,
    an opposing planeswalker, a creature the damage kills (marked damage
    counted) or one in combat keep the decision critical. The kicked amount
    counts unless ``castable`` (the card about to be cast) shows the mana
    for the kicker cannot be there this turn; a spell already on the stack
    may have been kicked, so it always counts there.
    """
    walkers = creatures = False
    amounts: list[int] = []
    for match in _REMOVAL_RE.finditer(oracle):
        damage = _DAMAGE_CLAUSE_RE.match(match.group(0))
        if damage is None or damage.group(1).lower() == "x":
            return False
        amounts.append(int(damage.group(1)))
        scope = damage.group(2).lower()
        creatures = creatures or "creature" in scope or scope == "any target"
        walkers = (
            walkers
            or scope in ("any target", "target planeswalker")
            or oracle[match.end() : match.end() + 17].lower().startswith(" or planeswalker")
        )
    if not amounts:
        return False
    kicked = [int(n) for n in _DAMAGE_INSTEAD_RE.findall(oracle)]
    if kicked and castable is not None and not _kicker_affordable(castable, oracle, state):
        kicked = []
    amount = max(amounts + kicked)
    local = _local_seat(state)
    for card in state.get("battlefield") or []:
        if not (local is None or card.get("controller_seat_id") not in (None, local)):
            continue
        types = _types(card)
        if walkers and "planeswalker" in types:
            return False
        if creatures and "creature" in types and _damage_may_kill(card, amount):
            return False
    return True


def _names_in_plan(name: str, plan_text: str) -> bool:
    if not name or not plan_text:
        return False
    text = plan_text.casefold()
    full = name.casefold()
    if full in text:
        return True
    short = full.split(",")[0].strip()
    return bool(short) and len(short) > 3 and re.search(rf"\b{re.escape(short)}\b", text) is not None


def critical_decision(
    decision: Any, state: dict | None, plan_text: str = "", skipped: list[str] | None = None
) -> str:
    """Why this decision deserves high reasoning effort, or "" for the routine path.

    - a removal spell or ability is castable now (ActionsAvailable);
    - an option spends a card the game plan names (an activation from hand,
      which is a discard cost, or a SelectN discard/sacrifice of that card);
    - the targets of a removal effect are being chosen (SelectTargets).

    Burn that can kill nothing on the opposing side (:func:`burn_kills_nothing`)
    is not removal here; ``skipped`` (when given) collects why such cards were
    passed over, for the log.
    """
    if decision is None or not isinstance(state, dict):
        return ""
    if skipped is None:
        skipped = []
    request_type = str(getattr(decision, "request_type", "") or "")
    options = list(getattr(decision, "options", ()) or ())
    if request_type == "ActionsAvailable":
        for option in options:
            if getattr(option, "payable", None) is False:
                continue
            meta = getattr(option, "meta", {}) or {}
            action = str(meta.get("actionType") or "")
            if action not in ("ActionType_Cast", "ActionType_Activate"):
                continue
            card = _source_card(option, state)
            oracle = str(card.get("oracle_text") or "")
            name = str(card.get("name") or "")
            if (
                action == "ActionType_Activate"
                and card.get("_zone") == "hand"
                and _names_in_plan(name, plan_text)
            ):
                return f"spends a plan card from hand: {name}"
            if _REMOVAL_RE.search(oracle) and _removal_has_target(oracle, state):
                if burn_kills_nothing(oracle, state, castable=card):
                    skipped.append(f"{name or option.label} can kill nothing on their side")
                    continue
                return f"removal available: {name or option.label}"
        return ""
    if request_type == "SelectN":
        source = str(getattr(decision, "source_label", "") or "")
        for option in options:
            card = _source_card(option, state)
            name = str(card.get("name") or option.label or "")
            if card.get("_zone") == "hand" and _names_in_plan(name, plan_text):
                return f"may discard a plan card: {name}" + (f" ({source})" if source else "")
        return ""
    if request_type == "SelectTargets":
        source_id = None
        for card in state.get("stack") or []:
            source_id = card.get("instance_id")
            oracle = str(card.get("oracle_text") or "")
            if _REMOVAL_RE.search(oracle):
                if burn_kills_nothing(oracle, state):
                    skipped.append(f"{card.get('name') or source_id} can kill nothing on their side")
                    continue
                return f"removal target: {card.get('name') or source_id}"
        source = str(getattr(decision, "source_label", "") or "")
        for zone in ("battlefield", "hand", "graveyard"):
            for card in state.get(zone) or []:
                oracle = str(card.get("oracle_text") or "")
                if source and str(card.get("name") or "") == source and _REMOVAL_RE.search(oracle):
                    if burn_kills_nothing(oracle, state):
                        continue
                    return f"removal target: {source}"
        return ""
    if request_type == "CastingTimeOptions":
        # The mode of a removal spell on the stack (#87, 2026-10-19 c8ea2b6c T14: Iroh's
        # Demonstration's 4 to one creature at low effort over the sweep that killed three).
        modal = [o for o in options if (getattr(o, "meta", {}) or {}).get("choiceKind") == "modal"]
        source_id = (getattr(modal[0], "meta", {}) or {}).get("sourceId") if modal else None
        for card in state.get("stack") or []:
            oracle = str(card.get("oracle_text") or "")
            if source_id and card.get("instance_id") == source_id and _REMOVAL_RE.search(oracle):
                if not burn_kills_nothing(oracle, state):
                    return f"removal mode: {card.get('name') or source_id}"
        return ""
    return ""
