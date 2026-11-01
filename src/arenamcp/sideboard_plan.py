"""Bounded Traditional Limited sideboard plans, using only this match's pool."""

from __future__ import annotations

import concurrent.futures
import json
import logging
from collections import Counter
from typing import Any

from arenamcp.draft_advisor import DraftAdvisor

logger = logging.getLogger(__name__)
MAX_POOL = 600


def opponent_ids_for_sideboard(game, request_id: list) -> list[int]:
    """Read opponent evidence and its identity atomically, including the reset stash."""
    expected = tuple(request_id[:2])
    with game._state_lock:
        if (game.match_id, game.game_number) == expected:
            current = list(game.get_opponent_played_cards())
            if current:
                return current
        ended = game.last_ended_game() or {}
        if (ended.get("match_id"), ended.get("game_number")) == expected:
            return list(game.get_last_game_opponent_played_cards())
    return []


def card_ids(value: Any) -> list[int]:
    if not isinstance(value, list) or len(value) > MAX_POOL:
        raise ValueError("Incomplete or oversized card list")
    if any(type(g) is not int or not 0 < g < 2**32 for g in value):
        raise ValueError("Card ids must be positive uints")
    return list(value)


def validate_plan(state: dict, plan: dict) -> dict:
    """Pool conservation plus the GRE's actual constraints; never invent format limits."""
    if state.get("super_format") != "Limited":
        raise ValueError("Automatic sideboarding currently supports Traditional Limited only")
    source = state["deck"]
    main, side = card_ids(plan.get("main")), card_ids(plan.get("sideboard"))
    original_main, original_side = card_ids(source.get("main")), card_ids(source.get("sideboard"))
    commander = card_ids(source.get("commander"))
    companion = source.get("companion")
    if type(companion) is not int or companion < 0:
        raise ValueError("Companion identity unavailable")
    if len(main) != len(original_main) or len(side) != len(original_side):
        raise ValueError("Sideboard plans must preserve both pile sizes")
    if Counter(main + side) != Counter(original_main + original_side):
        raise ValueError("Sideboard plan changes the registered pool")
    constraints = state.get("constraints") or {}
    for pile, count in (("deck", len(main)), ("sideboard", len(side)), ("commander", len(commander))):
        low, high = constraints.get("min_" + pile), constraints.get("max_" + pile)
        if type(low) is not int or type(high) is not int or not 0 <= low <= count <= high:
            raise ValueError(f"Unknown or violated {pile} size constraint")
    changed = Counter(main) != Counter(original_main)
    if changed and (commander or companion):
        raise ValueError("Companion/commander restrictions require manual sideboarding")
    return {"main": sorted(main), "sideboard": sorted(side)}


def unchanged_plan(state: dict) -> dict:
    return validate_plan(state, state["deck"])


def apply_swaps(state: dict, proposal: dict, cards: list[dict]) -> dict:
    """A model can exchange up to five known nonlands; it cannot rebuild the deck."""
    known = {c["grp_id"]: c for c in cards if type(c.get("grp_id")) is int}
    main, side = Counter(state["deck"]["main"]), Counter(state["deck"]["sideboard"])
    swaps = proposal.get("swaps")
    if not isinstance(swaps, list) or len(swaps) > 5:
        raise ValueError("Expected at most five swaps")
    touched: set[int] = set()
    total = 0
    for swap in swaps:
        if not isinstance(swap, dict):
            raise ValueError("Invalid swap")
        out, into, count = swap.get("out"), swap.get("in"), swap.get("count")
        if any(type(n) is not int or n <= 0 for n in (out, into, count)):
            raise ValueError("Swap ids/count must be positive integers")
        if out == into or out in touched or into in touched:
            raise ValueError("Repeated or circular swap")
        touched.update((out, into))
        total += count
        if total > 5 or main[out] < count or side[into] < count:
            raise ValueError("Swap exceeds available cards or five-card budget")
        for gid in (out, into):
            card = known.get(gid, {})
            if (
                not card.get("type_line")
                or not card.get("oracle_text")
                or "land" in card["type_line"].lower()
            ):
                raise ValueError("Only known nonlands can be swapped automatically")
        main[out] -= count
        side[out] += count
        side[into] -= count
        main[into] += count
    return validate_plan(state, {"main": list(main.elements()), "sideboard": list(side.elements())})


SIDEBOARD_PROMPT = """Choose conservative sideboard swaps for this Traditional Limited match.
Use only supplied card rules and opponent cards actually seen; do not invent their deck.
Preserve the mana base and main colors, curve, creature density, and existing deck plan.
Do not bring in uncastable cards. Swap only for a concrete matchup improvement.
Keep the deck unchanged if evidence is weak. Never exchange lands or unknown cards.
Return only JSON: {"swaps":[{"out":123,"in":456,"count":1}],"reason":"brief rationale"}.
Use exact grp_id values from our main and sideboard respectively. At most five cards,
no repeated ids, equal cards in/out. Empty swaps is an appropriate choice.
"""


class SideboardAdvisor(DraftAdvisor):
    """One time-bounded call; late/invalid answers never become a later game's plan."""

    def choose(self, state: dict, cards: list[dict], opponent: list[dict]) -> tuple[dict, str]:
        fallback = unchanged_plan(state)
        if (
            not opponent
            or not state["deck"]["sideboard"]
            or state["deck"].get("companion")
            or state["deck"].get("commander")
        ):
            return fallback, "Keeping the deck: no supported matchup change."
        if self._backend is None or (self._pending is not None and not self._pending.done()):
            return fallback, "Keeping the deck: sideboard reasoning unavailable."
        main, side = Counter(state["deck"]["main"]), Counter(state["deck"]["sideboard"])
        fields = ("grp_id", "name", "type_line", "oracle_text", "mana_cost", "related_faces")
        details = [
            {
                **{k: c[k] for k in fields if k in c},
                "main_count": main[c["grp_id"]],
                "sideboard_count": side[c["grp_id"]],
            }
            for c in cards
            if c.get("grp_id") in main + side
        ]
        prompt = json.dumps({"cards": details, "opponent_seen": opponent}, ensure_ascii=False)
        self._pending = self._executor.submit(
            self._complete, prompt, SIDEBOARD_PROMPT, 1200, {"type": "json_object"}, "coach.sideboard"
        )
        try:
            reply = self._pending.result(timeout=self._timeout)
            proposal = json.loads(reply)
            if not isinstance(proposal, dict):
                raise ValueError("Expected sideboard object")
            plan = apply_swaps(state, proposal, cards)
            reason = proposal.get("reason")
            return plan, (
                " ".join(reason.split()[:50]) if isinstance(reason, str) else "Sideboard plan selected."
            )
        except (concurrent.futures.TimeoutError, ValueError, TypeError, KeyError) as exc:
            logger.info("Sideboard proposal discarded; keeping registered deck: %s", exc)
        except Exception as exc:
            logger.warning("Sideboard reasoning failed; keeping registered deck: %s", exc)
        return fallback, "Keeping the deck: no validated sideboard changes."
