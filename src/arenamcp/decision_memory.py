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
for a removal effect. Burn counts as removal when it can pay for its target's
ward, or kills together with other burn what no one spell kills
(:func:`burn_combo`). Guard holds and cancelled casts are remembered as such.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

# Decisions kept per match; the prompt shows only the current turn's.
MEMORY_LIMIT = 48
# Recent actions shown to the game-plan reform.
REFORM_ACTIONS = 8

# "each opponent and each creature they control": Tectonic Hazard (2026-10-19
# match abde2a07, ledger #77) is burn that reaches creatures too.
_REMOVAL_RE = re.compile(
    r"(?:destroy|exile) (?:target|each|all|another target|up to \w+ target) "
    r"(?:creature|permanent|artifact|enchantment|planeswalker|nonland)"
    r"|deals? (?:\d+|x) damage to (?:any target|target creature|target planeswalker"
    r"|(?:each opponent and )?each creature)"
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
    # What became of it when that differs from what was chosen ("cancelled — no effect").
    outcome: str = ""

    def line(self) -> str:
        what = ", ".join(self.chosen_labels) or ", ".join(self.chosen) or "(nothing)"
        head = f"{self.request_type}" + (f" ({self.source})" if self.source else "")
        why = f" — {self.reasoning}" if self.reasoning else ""
        outcome = f" [{self.outcome}]" if self.outcome else ""
        return f"T{self.turn} {head}: {what}{why}{outcome}"


# The planner's sentinel for "take no option" (``action_planner.DECLINE_DECISION``):
# the autopilot cancels the request, which for a cast's sub-decision cancels the cast.
_DECLINE = "__decline__"
CANCELLED = "cancelled — no effect"
COUNTERED = "countered — no effect"


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
        self._countered_keys: set[tuple[Any, ...]] = set()

    def reset(self) -> None:
        self._records = []
        self._countered_keys = set()

    def _sync_match(self, state: dict) -> None:
        match_id = state.get("match_id")
        if match_id != self._match_id:
            self._match_id = match_id
            self._records = []
            self._countered_keys = set()

    def record(self, state: dict, decision: Any, chosen: list[str], reasoning: str) -> DecisionRecord | None:
        if not isinstance(state, dict) or decision is None:
            return None
        self._sync_match(state)
        labels = {option.option_id: option.label for option in getattr(decision, "options", ()) or ()}
        chosen = [str(c) for c in (chosen or [])]
        declined = chosen == [_DECLINE]
        record = DecisionRecord(
            turn=int(((state.get("turn") or {}).get("turn_number")) or 0),
            request_type=str(getattr(decision, "request_type", "") or ""),
            source=str(getattr(decision, "source_label", "") or ""),
            chosen=chosen,
            chosen_labels=[CANCELLED] if declined else [labels.get(c, c) for c in chosen],
            reasoning=" ".join(str(reasoning or "").split()[:40]),
            option_labels=labels,
            signature=board_signature(state),
            window=turn_window(state),
        )
        self._records.append(record)
        del self._records[: -self._limit]
        if declined and record.request_type != "ActionsAvailable" and record.source:
            self.mark_cancelled(state, record.source)
        self.observe_countered(state)
        return record

    def note(self, state: dict, request_type: str, what: str, reasoning: str = "") -> DecisionRecord | None:
        """Remember something the planner's own guards decided, outside a typed decision.

        2026-10-19 10:56:24 (match 15e0eb20 T9): the losing-attack guard held
        Emberheart Challenger back; nothing recorded it, and at 10:56:26 the
        next decision still said "proceed to attack ... for lethal". A repeat of
        the same note (a re-planned window) is not added twice.
        """
        if not isinstance(state, dict) or not what:
            return None
        self._sync_match(state)
        turn = int(((state.get("turn") or {}).get("turn_number")) or 0)
        reasoning = " ".join(str(reasoning or "").split()[:40])
        last = self._records[-1] if self._records else None
        if (
            last is not None
            and last.turn == turn
            and last.request_type == request_type
            and last.chosen_labels == [what]
            and last.reasoning == reasoning
        ):
            return last
        record = DecisionRecord(
            turn=turn,
            request_type=request_type,
            source="guard",
            chosen=[],
            chosen_labels=[what],
            reasoning=reasoning,
            signature=board_signature(state),
            window=turn_window(state),
        )
        self._records.append(record)
        del self._records[: -self._limit]
        return record

    def mark_cancelled(self, state: dict | None, source: str) -> list[DecisionRecord]:
        """Mark this turn's latest cast of ``source`` (and its steps since) as cancelled — no effect.

        2026-10-19 11:13 (match 115944ce T8, ledger #68): Iroh's Demonstration was
        cast, its target declined (the cast cancelled) and cast again, six
        times; the memory listed six casts and targets, and the model went on
        "two prior 4-damage hits already landed". Returns the records marked.
        """
        name = str(source or "").strip().casefold()
        if not name:
            return []
        marked: list[DecisionRecord] = []
        for record in reversed(self.this_turn(state)):
            if record.chosen_labels == [CANCELLED]:
                continue  # the decline itself already says so
            if record.outcome:
                break  # an earlier cast, already accounted for
            if record.request_type == "ActionsAvailable":
                label = (record.chosen_labels or [""])[0].split(" [", 1)[0].strip().casefold()
                if label == f"cast {name}" or label.startswith(f"cast {name} "):
                    record.outcome = CANCELLED
                    marked.append(record)
                break  # the cast that opened this chain, or another priority choice
            if record.source.strip().casefold() == name:
                record.outcome = record.outcome or CANCELLED
                marked.append(record)
        return marked

    def observe_countered(self, state: dict | None) -> list[DecisionRecord]:
        """Mark casts whose stack ZoneTransfer had ``category == 'Countered'`` as countered — no effect.

        Uses only Arena's own ``AnnotationType_ZoneTransfer`` ``category == 'Countered'``
        (never ``action_history`` counterspell heuristics, which misclassify resolved spells
        whose Spell Pierce tax was paid — ledger #106).
        """
        if not isinstance(state, dict):
            return []
        if self._match_id is not None and state.get("match_id") != self._match_id:
            return []
        if not self._records:
            return []
        local = _local_seat(state)
        current_turn = int(((state.get("turn") or {}).get("turn_number")) or 0)
        seen = getattr(self, "_countered_keys", None)
        if seen is None:
            seen = self._countered_keys = set()
        marked: list[DecisionRecord] = []
        for idx, ev in enumerate(state.get("recent_events") or []):
            if not isinstance(ev, dict) or ev.get("type") != "zone_transfer":
                continue
            if str(ev.get("category") or "") != "Countered":
                continue
            owner = ev.get("controller_seat_id") or ev.get("owner_seat_id")
            if local is not None and owner is not None and int(owner) != int(local):
                continue
            card = str(ev.get("card") or "").strip()
            if not card or card == "Unknown":
                continue
            ev_turn = int(ev.get("turn") or current_turn or 0)
            iid = int(ev.get("instance_id") or ev.get("orig_instance_id") or 0)
            key = (ev_turn, card.casefold(), iid if iid else f"idx:{idx}")
            if key in seen:
                continue
            seen.add(key)
            marked.extend(self._mark_countered_cast(ev_turn, card))
        return marked

    def _mark_countered_cast(self, turn: int, card: str) -> list[DecisionRecord]:
        name = card.strip().casefold()
        if not name:
            return []
        turn_records = [r for r in self._records if r.turn == turn] if turn > 0 else list(self._records)
        target_idx: int | None = None
        for idx in range(len(turn_records) - 1, -1, -1):
            rec = turn_records[idx]
            if rec.outcome or rec.request_type != "ActionsAvailable":
                continue
            label = (rec.chosen_labels or [""])[0].split(" [", 1)[0].strip().casefold()
            if label == f"cast {name}" or label.startswith(f"cast {name} "):
                target_idx = idx
                break
        if target_idx is None:
            return []
        marked: list[DecisionRecord] = []
        cast_rec = turn_records[target_idx]
        cast_rec.outcome = COUNTERED
        marked.append(cast_rec)
        for rec in turn_records[target_idx + 1 :]:
            if rec.request_type == "ActionsAvailable":
                break
            if rec.source.strip().casefold() == name and not rec.outcome:
                rec.outcome = COUNTERED
                marked.append(rec)
        return marked

    def this_turn(self, state: dict | None) -> list[DecisionRecord]:
        if not isinstance(state, dict):
            return []
        if state.get("match_id") != self._match_id:
            return []
        self.observe_countered(state)
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
        if _their_end_step_instant(state, decision, option_id):
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

    def lines_for_turn(self, turn: int, state: dict | None = None) -> list[str]:
        """The decisions recorded for one turn of the current match, oldest first."""
        if state is not None:
            self.observe_countered(state)
        return [r.line() for r in self._records if r.turn == turn]

    def recent_actions_text(self, limit: int = REFORM_ACTIONS) -> str:
        """The latest decisions, one per line, for the game-plan reform prompt."""
        return "\n".join(f"  - {record.line()}" for record in self._records[-limit:])


def _their_end_step_instant(state: dict, decision: Any, option_id: str) -> bool:
    """An instant-speed cast at the opponent's end step (or cleanup): never a reversal of an earlier pass.

    Ledger #153 (2026-10-19 14:02:30, f5bc17dc T4): Burst Lightning at their
    end step "reversed" a pass from their main phase on the same board; the
    re-ask passed on a false premise ("saving the single red mana for my own
    curve-out next turn") and the held Burst and Shock were stranded. This is
    the last window for that mana, which untaps at our next turn either way.
    """
    local = _local_seat(state)
    active = (state.get("turn") or {}).get("active_player")
    if local is None or active is None or active == local:
        return False
    if turn_window(state) not in (_TURN_STEPS.index("end"), _TURN_STEPS.index("cleanup")):
        return False
    option = next((o for o in getattr(decision, "options", ()) or () if o.option_id == option_id), None)
    if option is None or (getattr(option, "meta", None) or {}).get("actionType") != "ActionType_Cast":
        return False
    card = _source_card(option, state)
    if not card:
        return False
    from arenamcp.mulligan_policy import hand_card

    try:
        return bool(hand_card(card).instant_speed)
    except Exception:
        return False


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
    r"deals? (\d+|x) damage to (any target|target creature|target planeswalker|(?:each opponent and )?each creature)",
    re.IGNORECASE,
)
# Burst Lightning: "If this spell was kicked, it deals 4 damage instead."
_DAMAGE_INSTEAD_RE = re.compile(r"deals? (\d+) damage instead", re.IGNORECASE)
_KICKER_RE = re.compile(r"\bkicker ((?:\{[^}]+\})+)", re.IGNORECASE)


def _cmc(cost: Any) -> int:
    """Mana value of a cost, Arena's dialect ({o4}, {oR}) included."""
    from arenamcp.mana import mana_cost_to_cmc
    from arenamcp.rules_engine import _normalize_mana_symbols

    return mana_cost_to_cmc(_normalize_mana_symbols(str(cost or "")))


def mana_available(state: dict) -> int:
    """Mana we can spend now: our untapped lands, or in our main phase the turn's
    mana potential (land drop and mana creatures counted) when that is more."""
    local = _local_seat(state)
    have = sum(
        1
        for c in state.get("battlefield") or []
        if c.get("controller_seat_id") == local and _is_land(c) and not c.get("is_tapped")
    )
    try:
        from arenamcp.mana_potential import turn_mana_potential

        potential = turn_mana_potential(state)
    except Exception:
        potential = None
    if potential is not None:
        have = max(have, potential.total_with_enabler)
    return have


def _kicker_affordable(card: dict, oracle: str, state: dict) -> bool:
    """Whether the mana for this spell plus its kicker can be there this turn (yes when it cannot be told).

    Our untapped lands, or in our main phase the turn's mana potential (land
    drop and mana creatures counted) when that is more. 2026-10-19 11:39:57
    (match a358cce5 T7, ledger #83): Arena's text reads "Kicker {o4}", which
    counted as one mana, so Burst Lightning's kicked 4 damage "killed" a 4/3
    with two lands and the decision ran at high effort; costs are read in
    both dialects now.
    """
    kicker = _KICKER_RE.search(oracle)
    if kicker is None:
        return True
    try:
        need = _cmc(card.get("mana_cost")) + _cmc(kicker.group(1))
        have = mana_available(state)
    except Exception:
        return True
    return have >= need


_WARD_MANA_RE = re.compile(r"\bward\s*((?:\{[^}]+\})+)", re.IGNORECASE)
_GRANTED_RE = re.compile(r"\b(?:enchanted|equipped) creature\b[^.\n]*", re.IGNORECASE)
_REMINDER_RE = re.compile(r"\([^)]*\)")


def _ward_costs(text: Any) -> list[int]:
    return [_cmc(match.group(1)) for match in _WARD_MANA_RE.finditer(_REMINDER_RE.sub(" ", str(text or "")))]


def ward_tax(card: dict, state: dict) -> int:
    """Mana a spell aimed at this permanent must add for its ward, every instance counted.

    Its own ward once (Arena's text can repeat a line in its markup), plus the
    ward each Aura or Equipment attached to it grants. 2026-10-19 (match
    a358cce5, ledger #83): T5 "removal available: Shock" against Skyward
    Spider (Ward {2}) with two lands; T7 the Spider also wore Sheltered by
    Ghosts ("... has lifelink and ward {2}"), so a spell at it cost {4} more.
    The ward module's own sum (ledger #51) should replace this when it lands.
    """
    own = _ward_costs(card.get("oracle_text"))
    total = max(own) if own else 0
    iid = card.get("instance_id")
    if iid is None:
        return total
    for other in state.get("battlefield") or []:
        if other is card or other.get("attached_to_id") != iid:
            continue
        for sentence in _GRANTED_RE.findall(_REMINDER_RE.sub(" ", str(other.get("oracle_text") or ""))):
            total += sum(_ward_costs(sentence))
    return total


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


@dataclass(frozen=True, eq=False)
class Burn:
    """How one damage-only removal spell reaches a single opposing creature or planeswalker.

    Compared by identity: two Shocks in hand are two spells.
    """

    card: dict
    name: str
    # (mana it needs, damage to one creature): the base cost, and the kicked one when it can count.
    variants: tuple[tuple[int, int], ...]
    creatures: bool
    walkers: bool


def burn_reach(oracle: str, card: dict | None, state: dict, *, check_kicker: bool = True) -> Burn | None:
    """``oracle``'s damage as removal (None for anything but fixed damage to creatures/planeswalkers).

    The kicked amount counts when the mana for it can be there this turn
    (``check_kicker``); a spell already on the stack may have been kicked, so
    without the check it always counts.
    """
    walkers = creatures = False
    amounts: list[int] = []
    for match in _REMOVAL_RE.finditer(oracle):
        damage = _DAMAGE_CLAUSE_RE.match(match.group(0))
        if damage is None or damage.group(1).lower() == "x":
            return None
        amounts.append(int(damage.group(1)))
        scope = damage.group(2).lower()
        creatures = creatures or "creature" in scope or scope == "any target"
        walkers = (
            walkers
            or scope in ("any target", "target planeswalker")
            or oracle[match.end() : match.end() + 17].lower().startswith(" or planeswalker")
        )
    if not amounts:
        return None
    card = card or {}
    cost = _cmc(card.get("mana_cost"))
    variants = [(cost, max(amounts))]
    kicked = [int(n) for n in _DAMAGE_INSTEAD_RE.findall(oracle)]
    if kicked:
        kicker = _KICKER_RE.search(oracle)
        if not check_kicker:
            variants.append((cost, max(kicked)))
        elif _kicker_affordable(card, oracle, state):
            variants.append((cost + (_cmc(kicker.group(1)) if kicker else 0), max(kicked)))
    return Burn(card, str(card.get("name") or ""), tuple(variants), creatures, walkers)


def _theirs(state: dict) -> list[dict]:
    local = _local_seat(state)
    return [
        card
        for card in state.get("battlefield") or []
        if local is None or card.get("controller_seat_id") not in (None, local)
    ]


def _reachable(burn: Burn, target: dict, state: dict, mana: int) -> list[int]:
    """The damage ``burn`` can bring to ``target`` with its ward paid (any variant when it has no ward)."""
    tax = ward_tax(target, state)
    if not tax:
        return [damage for _need, damage in burn.variants]
    return [damage for need, damage in burn.variants if need + tax <= mana]


def _doomed(state: dict, source: dict | None = None) -> frozenset:
    """Their creatures our own spells on the stack kill, ``source`` (a stack spell) left out."""
    from arenamcp.play_safety import doomed_by_our_stack

    return doomed_by_our_stack(state, exclude=(source or {}).get("instance_id"))


def burn_kills_nothing(
    oracle: str, state: dict, castable: dict | None = None, *, source: dict | None = None
) -> bool:
    """Damage-only removal that can kill no opposing creature or planeswalker now: it can only go face.

    2026-10-19 Mono-Red Burn (07:15-09:08): Shock, Burst Lightning and
    Lightning Strike ("deals N damage to any target") were in hand almost
    every window, so 151 typed decisions ran the high-effort hedge, many of
    them against a 4/4 Bristly Bill or a 4-toughness Dawnwing Marshal the burn
    could not kill. Burning face is the race the board math and the game plan
    already count, not removal. Destroy, exile and -N/-N effects, X damage,
    an opposing planeswalker the damage kills (its loyalty, or unknown
    loyalty), a creature the damage kills (marked damage
    counted) or one in combat keep the decision critical. The kicked amount
    counts unless ``castable`` (the card about to be cast) shows the mana
    for the kicker cannot be there this turn; a spell already on the stack
    (``source``, its card) may have been kicked, so it always counts there.
    A warded target counts only when the spell's cost plus every ward on it
    fits our mana (ledger #83; Arena takes a spell's cost after its targets,
    so a spell on the stack still owes it). A creature our own pending
    damage on the stack already kills (:func:`play_safety.doomed_by_our_stack`,
    ``source`` itself left out) is no kill for it (ledger #122, 17a32d51 T5:
    Lightning Strike "could kill" the Emberheart our Shock was aimed at, so
    the #80 hold let it go face at low effort). Together with other burn,
    see :func:`burn_combo`.
    """
    burn = burn_reach(
        oracle, castable if castable is not None else source, state, check_kicker=castable is not None
    )
    if burn is None:
        return False
    mana = mana_available(state)
    from arenamcp.combat_strategy import loyalty

    doomed = _doomed(state, source)
    for card in _theirs(state):
        if card.get("instance_id") in doomed:
            continue
        types = _types(card)
        if burn.walkers and "planeswalker" in types:
            # Ledger #139 (d9a33232 T4): Shock's 2 at a 3-loyalty Jace killed nothing.
            left = loyalty(card)
            if any(left is None or damage >= left for damage in _reachable(burn, card, state, mana)):
                return False
        if burn.creatures and "creature" in types:
            if any(_damage_may_kill(card, damage) for damage in _reachable(burn, card, state, mana)):
                return False
    return True


@dataclass(frozen=True)
class BurnCombo:
    """Two or more burn spells that together kill one opposing creature none of them kills alone."""

    spells: tuple[str, ...]
    damages: tuple[int, ...]
    victim: str
    body: str
    mana: int
    ward: int = 0

    def short(self) -> str:
        return f"{' + '.join(self.spells)} kill {self.victim}"

    def text(self) -> str:
        parts = " + ".join(
            f"{name} ({damage})" for name, damage in zip(self.spells, self.damages, strict=True)
        )
        ward = f", its ward {{{self.ward}}} paid for each" if self.ward else ""
        return (
            f"{parts} = {sum(self.damages)} damage kills {self.victim} ({self.body}) this turn for "
            f"{self.mana} mana{ward}; no one of them kills it alone"
        )


_UNTARGETABLE_RE = re.compile(r"\b(?:hexproof|shroud)\b", re.IGNORECASE)


def burn_combo(
    state: dict,
    burns: list[Burn],
    *,
    required: Burn | None = None,
    mana: int | None = None,
    victim_id: int | None = None,
) -> BurnCombo | None:
    """The best kill two or more of ``burns`` make together within this turn's mana, or None.

    2026-10-19 11:11:29 (match 115944ce T6, ledger #64): Shock (2) and Iroh's
    Demonstration (4) were each "can kill nothing" against a 5/5 Trostani, so
    the window ran at low effort ("burn only goes face"); together, for three
    mana, they kill it. Every spell's cost and the victim's ward for each
    spell count against ``mana`` (default :func:`mana_available`); hexproof
    or shroud creatures are skipped, and so are those our other pending
    damage already kills (ledger #122). ``required`` (a spell on the stack)
    must be part of the kill. The biggest victim wins, then the fewest
    spells and the least mana; ``victim_id`` limits it to that creature.
    """
    from itertools import combinations, product

    from arenamcp.play_safety import lethal_damage

    pool = [burn for burn in burns if burn.creatures][:7]
    if required is not None and required not in pool:
        return None
    if len(pool) < 2:
        return None
    mana = mana_available(state) if mana is None else mana
    doomed = _doomed(state, required.card if required is not None else None)
    best: tuple[tuple, BurnCombo] | None = None
    for victim in _theirs(state):
        if "creature" not in _types(victim) or victim.get("instance_id") in doomed:
            continue
        if victim_id is not None and victim.get("instance_id") != victim_id:
            continue
        text = f"{victim.get('oracle_text') or ''} {' '.join(str(k) for k in victim.get('keywords') or [])}"
        if _UNTARGETABLE_RE.search(_REMINDER_RE.sub(" ", text)):
            continue
        needed = lethal_damage(victim)
        if needed is None:
            continue
        tax = ward_tax(victim, state)
        if any(damage >= needed for burn in pool for damage in _reachable(burn, victim, state, mana)):
            continue  # one spell kills it alone: plain removal, not a combo
        found = None
        for size in range(2, min(4, len(pool)) + 1):
            for subset in combinations(pool, size):
                if required is not None and required not in subset:
                    continue
                for variants in product(*(burn.variants for burn in subset)):
                    spent = sum(need for need, _damage in variants) + tax * size
                    damage = sum(damage for _need, damage in variants)
                    if damage < needed or spent > mana:
                        continue
                    if found is None or spent < found[0]:
                        found = (spent, subset, variants)
            if found is not None:
                break
        if found is None:
            continue
        spent, subset, variants = found
        power = victim.get("power") if type(victim.get("power")) is int else 0
        combo = BurnCombo(
            spells=tuple(burn.name or "burn" for burn in subset),
            damages=tuple(damage for _need, damage in variants),
            victim=str(victim.get("name") or victim.get("instance_id")),
            body=f"{victim.get('power')}/{victim.get('toughness')}",
            mana=spent,
            ward=tax,
        )
        key = (-power, len(subset), spent)
        if best is None or key < best[0]:
            best = (key, combo)
    return best[1] if best is not None else None


def _castable_burns(decision: Any, state: dict) -> list[Burn]:
    """The payable burn casts on an ActionsAvailable menu, one per card."""
    burns: list[Burn] = []
    seen: set = set()
    for option in getattr(decision, "options", ()) or ():
        meta = getattr(option, "meta", {}) or {}
        if getattr(option, "payable", None) is False or meta.get("actionType") != "ActionType_Cast":
            continue
        card = _source_card(option, state)
        if not card or card.get("instance_id") in seen:
            continue
        burn = burn_reach(str(card.get("oracle_text") or ""), card, state)
        if burn is not None:
            seen.add(card.get("instance_id"))
            burns.append(burn)
    return burns


def _stack_burn_combo(
    card: dict, state: dict, *, victim_id: int | None = None, damage: int | None = None
) -> BurnCombo | None:
    """A kill the burn on the stack (``card``) makes with burn still castable from hand.

    ``damage``: what the stack spell deals in the mode chosen for it (its
    largest amount otherwise); ``victim_id``: only a kill of that creature.
    """
    required = burn_reach(str(card.get("oracle_text") or ""), card, state, check_kicker=False)
    if required is None:
        return None
    if damage is not None:
        required = replace(required, variants=((required.variants[0][0], damage),))
    active, local = (state.get("turn") or {}).get("active_player"), _local_seat(state)
    sorcery_ok = turn_window(state) in (_TURN_STEPS.index("main1"), _TURN_STEPS.index("main2")) and (
        active is None or local is None or active == local
    )
    hand: list[Burn] = []
    for other in state.get("hand") or []:
        if not isinstance(other, dict) or other.get("instance_id") == card.get("instance_id"):
            continue
        types = _types(other)
        if "instant" not in types and not ("sorcery" in types and sorcery_ok):
            continue
        burn = burn_reach(str(other.get("oracle_text") or ""), other, state)
        if burn is not None:
            hand.append(burn)
    return burn_combo(state, [required, *hand], required=required, victim_id=victim_id) if hand else None


def stack_burn_combo_on(
    state: dict | None,
    victim_id: int,
    *,
    source_id: int = 0,
    source_name: str = "",
    damage: int | None = None,
) -> BurnCombo | None:
    """The kill of ``victim_id`` our burn on the stack makes with burn still castable from hand.

    Ledger #110 (2026-10-19 12:29:01): Iroh's Demonstration's 4 at a 4/5 Hearth
    Elemental, with Tectonic Hazard (1 to each of their creatures) castable
    for the mana left, was declined as "kills no legal opposing target" because
    the target-step reasoning did not name the Hazard. The stack spell is
    found by instance id, else by name; every spell's cost, the victim's ward
    and the timing (sorceries only in our main phase) count. Never raises.
    """
    if not isinstance(state, dict) or not victim_id:
        return None
    try:
        stack = [card for card in state.get("stack") or [] if isinstance(card, dict)]
        card = next((c for c in stack if source_id and c.get("instance_id") == source_id), None)
        if card is None and source_name:
            wanted = source_name.strip().lower()
            card = next((c for c in stack if str(c.get("name") or "").strip().lower() == wanted), None)
        if card is None:
            return None
        return _stack_burn_combo(card, state, victim_id=victim_id, damage=damage)
    except Exception:  # a hint, never a reason to fail the decision
        return None


def burn_combo_for(decision: Any, state: dict | None) -> BurnCombo | None:
    """The burn kill this decision can start: castable burn together (ActionsAvailable), or the
    burn whose targets are being chosen with burn from hand (SelectTargets)."""
    if decision is None or not isinstance(state, dict):
        return None
    try:
        request_type = str(getattr(decision, "request_type", "") or "")
        if request_type == "ActionsAvailable":
            burns = _castable_burns(decision, state)
            return burn_combo(state, burns) if len(burns) >= 2 else None
        if request_type == "SelectTargets":
            for card in state.get("stack") or []:
                if _REMOVAL_RE.search(str(card.get("oracle_text") or "")):
                    return _stack_burn_combo(card, state)
    except Exception:  # the hint never blocks a decision
        return None
    return None


def burn_combo_note(decision: Any, state: dict | None) -> str:
    """The typed-decision prompt's line for :func:`burn_combo_for` ('' when there is none)."""
    combo = burn_combo_for(decision, state)
    if combo is None:
        return ""
    if str(getattr(decision, "request_type", "")) == "SelectTargets":
        return f"BURN COMBO: {combo.text()}. Aim this spell at {combo.victim} and finish it with the rest."
    return f"BURN COMBO: {combo.text()}. Cast them at {combo.victim} this turn unless something better is lethal."


# Our permanents that reward casting a noncreature spell this turn: casting
# burn in our own main phase feeds them. (Hired Claw's "activate only if an
# opponent lost life this turn" is not one: its own attack ping enables it.)
_CAST_PAYOFF_RE = re.compile(
    r"\bprowess\b|\bmagecraft\b|\bwhenever you cast (?:a|an|your first|your second) "
    r"(?:noncreature|instant|sorcery|instant or sorcery)\b|\bwhenever you cast a spell\b",
    re.IGNORECASE,
)


def cast_payoffs(state: dict) -> list[str]:
    """Names of our battlefield permanents that pay off a noncreature cast now.

    Prowess (Emberheart Challenger), Slickshot Show-Off's +2/+0.
    """
    local = _local_seat(state)
    found: list[str] = []
    for card in state.get("battlefield") or []:
        if local is not None and card.get("controller_seat_id") != local:
            continue
        keywords = " ".join(str(k) for k in card.get("keywords") or [])
        if _CAST_PAYOFF_RE.search(f"{card.get('oracle_text') or ''} {keywords}"):
            found.append(str(card.get("name") or card.get("instance_id")))
    return found


_FACE_DAMAGE_RE = re.compile(
    r"deals? (\d+) damage to (?:any target|each opponent|target player|target opponent)", re.IGNORECASE
)
_CAST_PUMP_RE = re.compile(
    r"whenever you cast a noncreature spell, (?:this creature|it|~|[^,.]{1,40}) gets \+(\d+)/\+\d+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LethalReach:
    """The most damage we might deal this turn, when it reaches their life (a cheap overestimate)."""

    damage: int
    life: int
    attack: int
    burn: int
    pumps: int
    pumpers: tuple[str, ...]
    sneak: int = 0  # a Sneak swap after blocks: its creature's power less the attacker it returns
    sneak_name: str = ""

    def text(self) -> str:
        parts = [f"attackers {self.attack}"] if self.attack else []
        if self.burn:
            parts.append(f"burn {self.burn}")
        if self.pumps:
            parts.append(f"cast pumps +{self.pumps} ({', '.join(self.pumpers)})")
        if self.sneak:
            parts.append(f"Sneak {self.sneak_name} after blocks +{self.sneak}")
        return f"up to {self.damage} damage this turn ({', '.join(parts)}) against their {self.life} life"


def _sneak_reach(
    state: dict, attackers: list[dict], mana: int, window: int | None
) -> tuple[int, str, int | None]:
    """(+damage, card, unblocked damage or None) a Sneak cast adds this turn: in our
    declare-blockers step the real swap (``sneak.sneak_now``) and the unblocked attackers'
    damage; before it, the Sneak creature's power less our weakest attacker's.

    Ledger #156 (match be1a945f T12): the reach read 20 against their 17 and
    never counted the Sneak Leonardo worth 15 more.
    """
    from arenamcp.sneak import entry_power, sneak_casts, sneak_now

    if window == _TURN_STEPS.index("declareblock"):
        found = sneak_now(state)
        if found is None or found.affordable is False:
            return 0, "", None
        return max(0, found.total - found.through), found.cast.name, found.through
    if not attackers or window is None or window > _TURN_STEPS.index("declareattack"):
        return 0, "", None
    local = _local_seat(state)
    creatures = sum(
        1
        for c in state.get("battlefield") or []
        if c.get("controller_seat_id") == local and "creature" in _types(c) and not _is_land(c)
    )
    weakest = min((c["power"] for c in attackers if type(c.get("power")) is int), default=0)
    best, name = 0, ""
    for cast in sneak_casts(state):
        if cast.mana_value > mana:
            continue
        gain = entry_power(cast.card, creatures) - max(0, weakest)
        if gain > best:
            best, name = gain, cast.name
    return best, name, None


def _menu_can_change_combat(decision: Any, state: dict) -> bool:
    """A payable activation, or a payable cast of a card that is not a creature (or unknown)."""
    hand = {
        int(card.get("instance_id") or 0): card for card in state.get("hand") or [] if isinstance(card, dict)
    }
    for option in getattr(decision, "options", ()) or ():
        if getattr(option, "payable", None) is False:
            continue
        meta = getattr(option, "meta", {}) or {}
        kind = meta.get("actionType")
        if kind == "ActionType_Activate":
            return True
        if kind != "ActionType_Cast":
            continue
        try:
            card = hand.get(int(meta.get("instanceId") or 0))
        except (TypeError, ValueError):
            card = None
        if card is None:
            return True
        kinds = (
            " ".join(str(k) for k in card.get("card_types") or []) + " " + str(card.get("type_line") or "")
        )
        if "creature" not in kinds.lower():
            return True
    return False


def lethal_reach(decision: Any, state: dict | None) -> LethalReach | None:
    """Their life is within our most damage this turn, or None (ActionsAvailable only).

    2026-10-19 12:05:06 (match 18b1601a T9, ledger #100): they were at 7 and
    Tectonic Hazard (1) made both airborne Slickshot Show-Offs 3/2: exactly 7.
    Nothing in the window counted as removal, so it ran at low effort and
    followed the line tag that put Fleeting Effigy first (the line search did
    not count the cast pumps, ledger #94). The bound: our creatures that can
    attack before combat on our turn (their power), plus the most the payable
    noncreature casts on the menu deal to their face within our mana, plus
    per such cast +N for each attacker with "whenever you cast a noncreature
    spell, ... gets +N/+0" and +1 for each with prowess. Tapped lands of
    theirs and ordering are ignored: it only gates effort.

    Ledger #175: a menu with no payable cast or activation (land and pass
    only) is never within reach, and when nothing on the menu can change the
    combat (no activation, no noncreature cast) their best blocks cap the
    attack. A noncreature cast or an activation (a tap effect, a pump, removal)
    keeps the blocker-free bound (review 2026-10-20).
    """
    if not isinstance(state, dict) or str(getattr(decision, "request_type", "")) != "ActionsAvailable":
        return None
    if not any(
        getattr(o, "payable", None) is not False
        and (getattr(o, "meta", {}) or {}).get("actionType") in ("ActionType_Cast", "ActionType_Activate")
        for o in getattr(decision, "options", ()) or ()
    ):
        return None
    life = next(
        (
            p.get("life_total")
            for p in state.get("players") or []
            if not p.get("is_local") and type(p.get("life_total")) is int
        ),
        None,
    )
    if life is None or life <= 0:
        return None
    local = _local_seat(state)
    our_turn = local is not None and (state.get("turn") or {}).get("active_player") == local
    window = turn_window(state)
    attackers: list[dict] = []
    if our_turn and window is not None and window <= _TURN_STEPS.index("declareattack"):
        from arenamcp.combat_strategy import able_attackers

        attackers = able_attackers(state)
    attack = sum(card["power"] for card in attackers if type(card.get("power")) is int and card["power"] > 0)
    per_spell = 0
    pumpers: list[str] = []
    for card in attackers:
        # Without reminder text: prowess's own "(Whenever you cast a noncreature spell, ...)" counts once.
        text = _REMINDER_RE.sub(
            " ", f"{card.get('oracle_text') or ''} {' '.join(map(str, card.get('keywords') or []))}"
        )
        bonus = sum(int(n) for n in _CAST_PUMP_RE.findall(text)) + (
            1 if re.search(r"\bprowess\b", text, re.I) else 0
        )
        if bonus:
            per_spell += bonus
            pumpers.append(str(card.get("name") or card.get("instance_id")))
    mana = mana_available(state)
    # For each amount of mana: (face damage + pumps, face damage, spells cast), most first.
    best = [(0, 0, 0)] * (mana + 1)
    seen: set = set()
    for option in getattr(decision, "options", ()) or ():
        meta = getattr(option, "meta", {}) or {}
        if getattr(option, "payable", None) is False or meta.get("actionType") != "ActionType_Cast":
            continue
        card = _source_card(option, state)
        if not card or card.get("instance_id") in seen or "creature" in _types(card):
            continue
        seen.add(card.get("instance_id"))
        oracle = str(card.get("oracle_text") or "")
        cost = _cmc(card.get("mana_cost"))
        variants = [(cost, sum(int(n) for n in _FACE_DAMAGE_RE.findall(oracle)))]
        kicked = _DAMAGE_INSTEAD_RE.findall(oracle)
        kicker = _KICKER_RE.search(oracle)
        if kicked and kicker:
            variants.append((cost + _cmc(kicker.group(1)), max(int(n) for n in kicked)))
        before = list(best)
        for spend in range(mana + 1):
            for need, damage in variants:
                if need <= spend:
                    total, face, spells = before[spend - need]
                    candidate = (total + damage + per_spell, face + damage, spells + 1)
                    if candidate[0] > best[spend][0]:
                        best[spend] = candidate
    extra, face, spells = best[mana]
    sneak, sneak_name = 0, ""
    if our_turn:
        try:
            sneak, sneak_name, unblocked = _sneak_reach(state, attackers, mana, window)
        except Exception:  # a reading aid: the bound never fails on it
            sneak, sneak_name, unblocked = 0, "", None
        if sneak and unblocked is not None:
            attack = unblocked  # blocks are known: the unblocked attackers' damage
        elif not sneak:
            sneak_name = ""
    if extra == 0 and not sneak and attackers and not _menu_can_change_combat(decision, state):
        from arenamcp.combat_solver import optimal_blocks
        from arenamcp.combat_strategy import _eligible_blockers

        opponent = next((p.get("seat_id") for p in state.get("players") or [] if not p.get("is_local")), None)
        blockers = _eligible_blockers(state, opponent)
        if blockers:
            plan = optimal_blocks(attackers, blockers, life)
            if plan is not None:
                attack = plan.damage_through
    if attack + extra + sneak < life:
        return None
    return LethalReach(
        attack + extra + sneak, life, attack, face, spells * per_spell, tuple(pumpers), sneak, sneak_name
    )


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
    - the targets of a removal effect are being chosen (SelectTargets);
    - their life is within our most damage this turn (:func:`lethal_reach`).

    Burn that can kill nothing on the opposing side (:func:`burn_kills_nothing`)
    is not removal here; ``skipped`` (when given) collects why such cards were
    passed over, for the log. Unless burn kills together what no one spell
    kills (:func:`burn_combo`, ledger #64): then it is removal again.
    """
    if decision is None or not isinstance(state, dict):
        return ""
    if skipped is None:
        skipped = []
    request_type = str(getattr(decision, "request_type", "") or "")
    options = list(getattr(decision, "options", ()) or ())
    if request_type == "ActionsAvailable":
        # First: a possible kill outweighs any removal or plan-card reason (ledger #100).
        reach = lethal_reach(decision, state)
        if reach is not None:
            return f"within lethal reach: {reach.text()}"
        face_only = 0
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
                    face_only += 1
                    continue
                return f"removal available: {name or option.label}"
        if face_only:
            combo = burn_combo_for(decision, state)
            if combo is not None:
                return f"removal available: {combo.short()}"
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
        stack = [card for card in state.get("stack") or [] if isinstance(card, dict)]
        label = str(getattr(decision, "source_label", "") or "")
        named = [card for card in stack if label and str(card.get("name") or "") == label]
        if named:
            # The request's own spell, by name: the copy whose targets are not chosen yet. Our
            # Shock already aimed below it is not the spell being aimed (ledger #122).
            stack = sorted(named, key=lambda card: bool(card.get("targeting")))[:1]
        for card in stack:
            source_id = card.get("instance_id")
            oracle = str(card.get("oracle_text") or "")
            if _REMOVAL_RE.search(oracle):
                if burn_kills_nothing(oracle, state, source=card):
                    combo = _stack_burn_combo(card, state)
                    if combo is not None:
                        return f"removal target: {card.get('name') or source_id} ({combo.short()})"
                    skipped.append(f"{card.get('name') or source_id} can kill nothing on their side")
                    continue
                return f"removal target: {card.get('name') or source_id}"
        source = str(getattr(decision, "source_label", "") or "")
        for zone in ("battlefield", "hand", "graveyard"):
            for card in state.get(zone) or []:
                oracle = str(card.get("oracle_text") or "")
                if source and str(card.get("name") or "") == source and _REMOVAL_RE.search(oracle):
                    if burn_kills_nothing(oracle, state, source=card):
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
                if not burn_kills_nothing(oracle, state, source=card):
                    return f"removal mode: {card.get('name') or source_id}"
        return ""
    return ""
