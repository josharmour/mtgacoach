"""The match ledger: what the plan said each turn, what we did, and how the board moved.

The game plan is re-formed in the background every turn or two, and until
2026-10-10 each reform saw only the previous plan text. It could not tell
whether last turn's step was executed, whether the opponent's life moved,
or that a win condition had been stalling for three turns, so a plan could
be rewritten around the same failing line indefinitely.

The ledger is deterministic. At every turn change it closes the previous
turn with a board snapshot (life, creatures, power, hand) and the typed
decisions made during it, and opens the new turn with the plan's step for
it. It renders the last few turns for the reform prompt and derives plain
lessons (no damage dealt for two of our turns, a planned cast never made,
a large life loss) that the plan carries forward as its own ``lessons``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

LEDGER_TURNS = 8

SNAPSHOT_KEYS = (
    "our_life",
    "opp_life",
    "our_creatures",
    "our_power",
    "their_creatures",
    "their_power",
    "our_hand",
)


def snapshot_from_assessment(assessment: Any) -> dict[str, int]:
    """The ledger's board snapshot from a BoardAssessment (missing fields read as 0), plus the outlook."""
    if assessment is None:
        return {}
    snapshot = {key: int(getattr(assessment, key, 0) or 0) for key in SNAPSHOT_KEYS}
    lines = getattr(assessment, "lines", None) or []
    best = lines[0] if lines else None
    if best is not None and getattr(best, "win_turn", None):
        snapshot["lethal_turn"] = int(best.win_turn)
    try:
        from arenamcp.outlook import outlook

        snapshot["outlook"] = int(outlook(assessment).get("pct") or 0)
    except Exception:  # the outlook is decoration; the ledger stands without it
        pass
    return snapshot


@dataclass
class TurnEntry:
    turn: int
    ours: bool
    role: str = ""
    plan_formed: int = 0
    planned: str = ""  # the plan's step text for this turn, "" when none
    planned_casts: list[str] = field(default_factory=list)
    did: list[str] = field(default_factory=list)  # decision lines made during the turn
    start: dict[str, int] = field(default_factory=dict)
    end: dict[str, int] = field(default_factory=dict)

    def executed(self) -> tuple[list[str], list[str]]:
        """(planned casts we made, planned casts we did not) by name match in the decision lines."""
        done = " ".join(self.did).casefold()
        made = [name for name in self.planned_casts if name.casefold() in done]
        missed = [name for name in self.planned_casts if name.casefold() not in done]
        return made, missed

    def swing(self) -> str:
        if not self.start or not self.end:
            return ""
        s, e = self.start, self.end

        def pair(key_a: str, key_b: str) -> str:
            return f"{s.get(key_a, 0)}/{s.get(key_b, 0)}->{e.get(key_a, 0)}/{e.get(key_b, 0)}"

        return (
            f"life us {s.get('our_life', 0)}->{e.get('our_life', 0)}, them {s.get('opp_life', 0)}->{e.get('opp_life', 0)}; "
            f"creatures/power ours {pair('our_creatures', 'our_power')}, theirs {pair('their_creatures', 'their_power')}"
        )

    def line(self) -> str:
        who = "our turn" if self.ours else "their turn"
        head = f"T{self.turn} ({who}{', ' + self.role if self.role else ''})"
        parts = []
        if self.planned:
            parts.append(f"plan said: {self.planned}")
            made, missed = self.executed()
            if self.planned_casts:
                status = "executed" if not missed else ("partly executed" if made else "NOT executed")
                parts.append(f"{status}" + (f" (missing {', '.join(missed)})" if missed else ""))
        if self.did:
            parts.append("did: " + " | ".join(self.did[-6:]))
        elif self.ours:
            parts.append("did: nothing recorded")
        swing = self.swing()
        if swing:
            parts.append(swing)
        return f"  - {head}: " + "; ".join(parts)


class PlanLedger:
    """Bounded per-match record of turns, fed to every game-plan reform."""

    def __init__(self, limit: int = LEDGER_TURNS) -> None:
        self._limit = limit
        self._entries: list[TurnEntry] = []
        self._turn = 0

    def reset(self) -> None:
        self._entries = []
        self._turn = 0

    @property
    def entries(self) -> list[TurnEntry]:
        return list(self._entries)

    def turn_started(
        self,
        turn: int,
        *,
        ours: bool,
        snapshot: dict[str, int],
        role: str = "",
        plan: Any = None,
        decisions_for_turn: Callable[[int], list[str]] | None = None,
    ) -> None:
        """Close the previous turn with ``snapshot`` and open ``turn``; repeated calls for a turn are no-ops."""
        if turn <= 0 or turn == self._turn:
            return
        if turn < self._turn:
            self.reset()
        if self._entries:
            last = self._entries[-1]
            last.end = dict(snapshot)
            if decisions_for_turn is not None:
                try:
                    last.did = list(decisions_for_turn(last.turn) or [])
                except Exception:  # the feed never blocks the ledger
                    pass
        entry = TurnEntry(turn=turn, ours=ours, role=role, start=dict(snapshot))
        step = plan.step_for(turn) if plan is not None and hasattr(plan, "step_for") else None
        if isinstance(step, dict) and ours:
            from arenamcp.game_plan import step_text

            entry.planned = step_text(step)
            entry.planned_casts = [str(c) for c in (step.get("cast") or []) if c]
            entry.plan_formed = int(getattr(plan, "turn_formed", 0) or 0)
        self._entries.append(entry)
        del self._entries[: -self._limit]
        self._turn = turn

    def lessons(self) -> list[str]:
        """Deterministic lessons from the closed turns: stalls, missed casts, big life swings."""
        closed = [e for e in self._entries if e.end]
        ours = [e for e in closed if e.ours]
        out: list[str] = []
        no_damage = [e.turn for e in ours if e.start.get("opp_life", 0) <= e.end.get("opp_life", 0)]
        if len(ours) >= 2 and len(no_damage) >= 2 and ours[-1].turn in no_damage:
            out.append(
                f"No damage dealt on our turns {', '.join(f'T{t}' for t in no_damage[-3:])}: the pressure line is not working"
            )
        missed_turns = [e for e in ours if e.planned_casts and e.executed()[1]]
        if len(missed_turns) >= 2:
            names = sorted({name for e in missed_turns for name in e.executed()[1]})
            out.append(
                f"Planned casts not made on {len(missed_turns)} of our turns ({', '.join(names[:4])}): plan only what the mana and board allow"
            )
        # 2026-10-10 game 3: the search said lethal on T34, then T36, then T38
        # while a recurring chump blocker and lifegain ate a swing a turn; the
        # plan raced at 4 life on a clock that never arrived.
        projected = [
            (e.turn, e.start["lethal_turn"]) for e in self._entries if e.ours and "lethal_turn" in e.start
        ]
        if len(projected) >= 3:
            last = projected[-3:]
            if last[0][1] < last[1][1] < last[2][1]:
                path = " -> ".join(f"T{t}" for _turn, t in last)
                out.append(
                    f"Projected lethal turn slipped {path}: the clock is not real (a recurring blocker or lifegain "
                    "is eating a swing each turn); do not race at low life on it, keep blockers home"
                )
        for e in closed[-4:]:
            lost = e.start.get("our_life", 0) - e.end.get("our_life", 0)
            if lost >= 5:
                out.append(f"We lost {lost} life across T{e.turn}: their board is outpacing our blocks")
            board_before = e.start.get("our_power", 0)
            board_after = e.end.get("our_power", 0)
            if e.ours and board_after < board_before and board_before > 0:
                out.append(
                    f"Our board shrank on T{e.turn} ({board_before}->{board_after} power) while executing the plan"
                )
        return out[:4]

    def outlook_trend(self) -> list[tuple[int, int]]:
        """(turn, outlook pct) at the start of each recorded turn that has one."""
        return [(e.turn, int(e.start["outlook"])) for e in self._entries if "outlook" in e.start]

    def prompt_block(self) -> str:
        if not self._entries:
            return ""
        lines = ["MATCH LEDGER (what the plan said, what we did, how the board moved; oldest first):"]
        lines.extend(entry.line() for entry in self._entries)
        lessons = self.lessons()
        if lessons:
            lines.append("  Lessons from the ledger (deterministic):")
            lines.extend(f"    * {lesson}" for lesson in lessons)
        return "\n".join(lines)
