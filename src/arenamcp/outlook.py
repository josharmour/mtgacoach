"""A deterministic "how is the game going" number for the sidebar and the match ledger.

Not a calibrated win probability: a 0-100 outlook from the board facts the
strategic layer already computes (life, clocks, board and card advantage,
lethal flags). It exists so the player can see at a glance whether the game
is going well and which way it is trending, and so the ledger can record a
trend across turns. The weights are hand-set and documented here.
"""

from __future__ import annotations

from typing import Any

LABELS = (
    (70, "winning", "good"),
    (55, "ahead", "good"),
    (45, "even", "muted"),
    (30, "behind", "warn"),
    (0, "losing", "bad"),
)


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def outlook(assessment: Any) -> dict[str, Any]:
    """{"pct": 0-100, "label": winning/ahead/even/behind/losing, "tone": theme tone, "why": [...]}"""
    if assessment is None:
        return {}
    why: list[str] = []
    score = 50.0
    our_life = int(getattr(assessment, "our_life", 0) or 0)
    opp_life = int(getattr(assessment, "opp_life", 0) or 0)
    life = _clip(our_life - opp_life, -20, 20)
    if life:
        score += 1.2 * life
        why.append(f"life {our_life} vs {opp_life}")
    board = _clip(int(getattr(assessment, "board_advantage", 0) or 0), -4, 4)
    if board:
        score += 4 * board
        why.append(f"board {int(board):+d}")
    cards = getattr(assessment, "card_advantage", None)
    if cards is not None and cards:
        score += 3 * _clip(int(cards), -3, 3)
        why.append(f"cards {int(cards):+d}")
    ours, theirs = getattr(assessment, "our_clock", None), getattr(assessment, "their_clock", None)
    if ours and theirs:
        score += 5 * _clip(theirs - ours, -3, 3)
        why.append(f"clocks {ours} vs {theirs}")
    elif ours and not theirs:
        score += 8
        why.append(f"we have a clock ({ours}), they have none")
    elif theirs and not ours:
        score -= 8
        why.append(f"they have a clock ({theirs}), we have none")
    if getattr(assessment, "lethal_now", False):
        score = max(score, 95)
        why.append("lethal on board")
    elif getattr(assessment, "lethal_next_turn", False):
        score = max(score, 80)
        why.append("lethal next turn")
    if getattr(assessment, "opp_lethal_on_board", False):
        score = min(score, 8)
        why.append("they have lethal on board")
    dead_in = getattr(assessment, "dead_in", None)
    if dead_in == 1:
        score = min(score, 12)
        why.append("dead next attack")
    elif dead_in == 2:
        score = min(score, 30)
        why.append("dead in 2 attacks")
    pct = int(round(_clip(score, 3, 97)))
    label, tone = next((name, tone) for floor, name, tone in LABELS if pct >= floor)
    return {"pct": pct, "label": label, "tone": tone, "why": why[:4]}
