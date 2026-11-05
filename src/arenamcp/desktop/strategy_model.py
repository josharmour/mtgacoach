"""Presentation of observed forecasts; never computes or executes game actions."""

from __future__ import annotations

from typing import Any

from arenamcp.forecast_identity import forecast_key


def number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def is_current(plan: dict, state: dict) -> bool:
    key = forecast_key(state)
    return bool(
        key
        and plan.get("snapshot_key") == key
        and not state.get("game_over")
        and not state.get("result")
        and (number((state.get("turn") or {}).get("turn_number")) or 0) > 0
    )


def step_text(step: dict) -> str:
    parts = []
    if step.get("land"):
        parts.append(f"Play {step['land']}")
    modes = step.get("modes") or {}
    targets = step.get("targets") or {}
    for name in step.get("casts") or []:
        mode = modes.get(name) if isinstance(modes, dict) else None
        target = targets.get(name) if isinstance(targets, dict) else None
        parts.append(f"Cast {name}" + (f" ({mode})" if mode else "") + (f" → {target}" if target else ""))
    if step.get("cycles"):
        parts.append("Cycle " + ", ".join(map(str, step["cycles"])))
    if step.get("attack"):
        parts.append("Attack with " + ", ".join(map(str, step["attack"])))
    if step.get("held"):
        parts.append("Hold " + ", ".join(map(str, step["held"])))
    return " · ".join(parts) or "No play in this projection"


def line_outcome(line: dict) -> str:
    kind = line.get("outcome")
    if line.get("unsupported") or kind not in {"win", "alive", "dead"}:
        return "Outcome uncertain"
    steps = [step for step in line.get("steps") or [] if isinstance(step, dict)]
    if kind == "dead":
        turn = next(
            (
                number(step.get("turn")) + 1
                for step in steps
                if number(step.get("life_after")) is not None
                and number(step["life_after"]) <= 0
                and number(step.get("turn")) is not None
            ),
            None,
        )
        now = number(line.get("now_life"))
        timing = " · current combat" if now is not None and now <= 0 else f" · T{turn}" if turn else ""
        return "Projected loss" + timing + " · best available may still lose"
    if kind == "win":
        turn = next(
            (
                number(step.get("turn"))
                for step in steps
                if number(step.get("opp_life_after")) is not None and number(step["opp_life_after"]) <= 0
            ),
            None,
        )
        return (
            "Projected win"
            + (f" · T{turn}" if turn else "")
            + " · conditional"
            + (" · distant" if line.get("distant") else "")
        )
    return "Survives modeled attacks · win not established"


def visible_lines(plan: dict, state: dict) -> list[dict]:
    facts = plan.get("facts") or {}
    turn = number((state.get("turn") or {}).get("turn_number")) or 0
    result = []
    for index, raw in enumerate(facts.get("lines") or []):
        if not isinstance(raw, dict):
            continue
        steps = [s for s in raw.get("steps") or [] if isinstance(s, dict)]
        # An elapsed first action means the entire sequence needs replanning;
        # dropping its first step would imply the remaining actions are feasible.
        if steps and (number(steps[0].get("turn")) or 0) < turn:
            if index == 0:
                return []
            continue
        if steps or raw.get("now_life") is not None:
            result.append(raw)
    return result[:3]


def decision_heading(state: dict, plan: dict, autoplay: str = "") -> tuple[str, str]:
    turn = state.get("turn") or {}
    local = state.get("local_seat_id")
    priority = turn.get("priority_player")
    playing = (
        "Autoplay paused"
        if "PAUSED" in autoplay
        else ("Autoplay is playing" if "ON" in autoplay else "You are playing")
    )
    if not state.get("match_id") or not turn:
        return "Waiting for a game", playing
    if state.get("game_over") or state.get("result"):
        return "Game complete", playing
    pending = str(state.get("pending_decision") or "").lower()
    decision_seat = state.get("decision_seat_id")
    ours = local is not None and (decision_seat == local if pending and decision_seat else priority == local)
    ownership = (
        "Your response" if ours else "Opponent has priority" if priority and local else "Priority unknown"
    )
    detail = f"{ownership} · {playing}"
    if not ours and priority is not None and local is not None and priority != local:
        return "Waiting on opponent", detail
    if ours and pending:
        for word, title in (
            ("mulligan", "Keep this hand or mulligan"),
            ("block", "Choose blockers"),
            ("attack", "Choose attackers"),
            ("target", "Choose a target"),
            ("discard", "Choose what to discard"),
            ("sacrifice", "Choose a sacrifice"),
            ("select", "Make your selection"),
        ):
            if word in pending:
                return title, detail
    if not is_current(plan, state):
        return "Board changed · rechecking" if plan else "Assessing this position", detail
    search = (plan.get("facts") or {}).get("search") or {}
    lines = visible_lines(plan, state)
    if (
        ours
        and lines
        and not search.get("truncated")
        and not lines[0].get("unsupported")
        and lines[0].get("outcome") in {"win", "alive", "dead"}
    ):
        steps = lines[0].get("steps") or []
        if steps and turn.get("active_player") == local:
            return step_text(steps[0]).split(" · ")[0], detail
    return "Your decision" if ours else "Watching the board", detail
