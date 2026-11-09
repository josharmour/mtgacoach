"""Evidence-only session narrative. No model call, score, or diagnostic upload."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


def read_packets(game_ids: set[str], directory: Path) -> dict[str, list[dict]]:
    """Use exact game identities, never attach a BO3 match's packet to every game."""
    found = {}
    for path in sorted(directory.glob("packet_*.json"), reverse=True):
        # Filter filenames before reading potentially large, unrelated packets.
        if not any(path.name.endswith("_" + mid + ".json") for mid in game_ids):
            continue
        try:
            if path.stat().st_size > 16 * 1024 * 1024:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            mid = data.get("match_id")
            entries = data.get("decisions")
            if mid in game_ids and mid not in found and isinstance(entries, list):
                found[mid] = [x for x in entries if isinstance(x, dict)]
        except (OSError, ValueError, AttributeError):
            continue
    return found


def _choice(entry: dict) -> str:
    action = entry.get("executed_action")
    if isinstance(action, dict):
        return " ".join(str(action.get(k) or "") for k in ("action_type", "card_name")).strip()[:160]
    pending = entry.get("pending_decision") or {}
    if not isinstance(pending, dict):
        return ""
    selected = entry.get("chosen_options") or []
    options = pending.get("options") or []
    if not isinstance(selected, list) or not isinstance(options, list):
        return ""
    labels = [
        str(o.get("label") or o.get("option_id") or "")
        for o in options
        if isinstance(o, dict) and o.get("option_id") in selected
    ]
    return ", ".join(labels)[:160]


def written_review(games, packets: dict[str, list[dict]]) -> str:
    counts = Counter(r.result for r in games)
    lines = [
        f"You finished with {counts['win']} wins and {counts['loss']} losses across "
        f"{len(games)} recorded games."
    ]
    if counts["draw"] or counts["unknown"] or counts[""]:
        lines.append(
            f"There were also {counts['draw']} draws and {counts['unknown'] + counts['']} unknown results."
        )
    lines.append(
        "These are games, not necessarily complete best-of-three matches. "
        "The results alone do not tell us whether the deck or autoplay needs changing."
    )
    streak = []
    for r in reversed(games):
        if r.result not in ("win", "loss") or (streak and r.result != streak[0]):
            break
        streak.append(r.result)
    if len(streak) >= 2:
        lines.append(f"You ended with {len(streak)} consecutive {streak[0]}s.")
    covered = [r for r in games if packets.get(r.match_id)]
    entries = [e for r in covered for e in packets[r.match_id]]
    outcomes = Counter(str(e.get("outcome") or "unknown").upper() for e in entries)
    lines.append(
        f"I found saved autoplay decision/action entries for {len(covered)} of "
        f"{len(games)} games. These are action records, not grades of the Coach's advice."
    )
    if entries:
        lines.append(
            f"The records mark {outcomes['ADVANCED']} requests as advanced and "
            f"{outcomes['EXECUTED']} action entries as executed. These counts can overlap "
            "and do not establish strategic correctness."
        )
        unresolved = sum(
            n for state, n in outcomes.items() if state not in ("ADVANCED", "EXECUTED", "REJECTED")
        )
        if outcomes["REJECTED"]:
            lines.append(
                f"{outcomes['REJECTED']} requests were marked rejected. "
                "Those are the first places I would review, without assuming they caused a loss."
            )
        if unresolved:
            lines.append(
                f"{unresolved} entries have no confirmed execution outcome. "
                "A pending entry is an evidence gap, not proof of a stall or failed play."
            )
        examples = []
        # Prefer rejected requests, then decisions from losses, then other games.
        candidates = [(i, r, e) for i, r in enumerate(games, 1) for e in packets.get(r.match_id, [])]
        candidates.sort(
            key=lambda x: (
                str(x[2].get("outcome")).upper() != "REJECTED",
                x[1].result != "loss",
                not any(word in _choice(x[2]).lower() for word in ("cast", "attack", "block", "target")),
            )
        )
        seen = set()
        seen_games = set()
        for i, r, entry in candidates:
            choice = _choice(entry)
            if not choice or (i, choice) in seen or i in seen_games:
                continue
            seen.add((i, choice))
            seen_games.add(i)
            turn = entry.get("turn") or {}
            number = turn.get("turn_number") if isinstance(turn, dict) else None
            where = f", turn {number}" if isinstance(number, int) else ""
            outcome = str(entry.get("outcome") or "unknown")[:32]
            examples.append(f"Game {i} ({r.result}){where}: {choice}. Recorded outcome: {outcome}.")
            rationale = entry.get("explanation")
            if isinstance(rationale, str) and rationale.strip():
                rationale = " ".join(rationale.split())[:240]
                examples[-1] += f' Recorded rationale (not independently verified): "{rationale}"'
            if len(examples) == 3:
                break
        if examples:
            lines.append("A few concrete moments to review:\n" + "\n".join(examples))
        lines.append(
            "Next, compare those choices with the board and legal alternatives in Match History. "
            "I cannot tell from these records alone whether a different choice would have won. "
            "I have not verified timing or stalls across the session."
        )
    else:
        lines.append(
            "I do not have enough saved decision evidence to identify a turning point, "
            "diagnose a stall, or recommend a deck change. Review an individual game in "
            "Match History before drawing a gameplay conclusion."
        )
    actions = review_actions(games, packets)
    next_steps = [
        "Review games: start with the recorded moments above and check the board, "
        "available actions and execution result."
    ]
    if "support" in actions:
        next_steps.append(
            "Prepare a support note: if you saw a stall or wrong action, add what you "
            "expected and the game/turn to these recording gaps or rejections. "
            "You can review a draft for support@mtgacoach.com before sending anything."
        )
    if "deck" in actions:
        next_steps.append(
            "Compare deck options: review the losses before choosing a stronger build "
            "or changing cards. Check whether threats went unanswered while playable "
            "removal was unavailable. These records do not establish that removal is "
            "missing; review the actual deck list and board before proposing specific swaps. "
            "Opening the builder does not change your deck or spend wildcards."
        )
    lines.append("What would you like to work on next?\n" + "\n\n".join(next_steps))
    return "\n\n".join(lines)


def review_actions(games, packets: dict[str, list[dict]]) -> list[str]:
    """Offer investigation routes, never a diagnosis or an automatic external action."""
    actions = ["history"]
    entries = [e for r in games for e in packets.get(r.match_id, [])]
    if any(str(e.get("outcome") or "").upper() not in ("ADVANCED", "EXECUTED") for e in entries) or any(
        not packets.get(r.match_id) for r in games
    ):
        actions.append("support")
    if any(r.result == "loss" for r in games):
        actions.append("deck")
    return actions
