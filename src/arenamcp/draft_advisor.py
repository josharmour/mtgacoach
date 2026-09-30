"""Explain draft choices from current card rules and the actual drafted pool."""

from __future__ import annotations

import concurrent.futures
import html
import json
import logging
import re
from collections import Counter
from typing import Any

from arenamcp.draft_guidance import FormatContext, analyze_pool, compute_lane, normalize_card
from arenamcp.limited_rules import rules_profile, synergy_evidence, synergy_graph

logger = logging.getLogger(__name__)

DRAFT_SYSTEM_PROMPT = """You are an expert Limited draft coach. Build a playable, coherent deck
from the cards the player ACTUALLY picked. Card rules in the supplied pack and pool
are authoritative; do not invent abilities or rely on memorized card names.

Choose exactly picks_required cards from this pack. In PickTwo, evaluate the TWO
cards together: their interaction, colors, curve, and combined contribution matter.
Explain each pick's actual function and, where relevant, how it works with a NAMED
card already in the pool. Shared creature types or words alone are not synergy:
identify an enabler, its payoff, the triggering condition, and sufficient support.
Examples: scry/surveil enables a Thopter payoff; artifact tokens enable a Dragon
replacement effect; cheap creatures give counters and combat tricks useful targets.
Do not imply that every card that draws a card enables scry or surveil.
Adding loyalty counters is not activating a loyalty ability. Bounce does not
destroy a creature or trigger its death abilities. Different counter types are
not interchangeable. A hybrid symbol can be paid with either of its colors;
it does not inherently require a splash. Tricks are not early creature plays.
Read related_faces too: prepared spells, Adventures, and alternate faces have
their own costs and conditions. Do not treat a linked spell as freely repeatable.

Early picks should preserve flexibility. As the actual pool develops, settle on
supported main colors and a specific game plan. Continue that plan unless card
quality, real pool support, or mana justify changing it; explain a change.
Previous advice is provisional: the user may have picked different cards. Never
count a previous recommendation as owned unless it appears in the actual pool.
Prefer early creatures, interaction, fixing, and missing enablers over more expensive
payoffs or combat tricks when the deck lacks bodies. Track cheap plays, creature
density, removal, mana requirements, and top-end saturation. Avoid a deck of five-mana
creatures and buffs with no early board. Off-color cards need realistic fixing.
Do not spend picks on ordinary basic lands while useful spells remain in the pack.

Heuristic scores and win rates are supporting evidence, not instructions. Missing
17lands ratings are UNKNOWN, not zero strength. Do not claim win-rate evidence
when none is supplied. If card rules are missing, say so and avoid invented synergy.

Return ONLY JSON:
{"picks": [{"grp_id": 123, "reason": "1-2 concise sentences explaining why",
"synergy_with": [456]}], "plan": "one sentence describing the deck's supported theme",
"needs": ["up to three concrete remaining needs"],
"alternative": {"grp_id": 789, "reason": "why it loses to the chosen pick(s)"}}
synergy_with must contain only ids of actual pool cards or the other chosen card.
Only assert named synergies present in supported_synergies; it lists conservative
rules-text enabler/payoff links, not guaranteed trigger frequency. Explain the
actual condition and costs. For unmodeled interactions, explain card roles instead
of claiming a verified synergy. Loyalty counters never substitute for +1/+1 counters.
An alternative is optional. Keep each explanation under 40 words.
"""


def _text(value: Any, words: int = 40) -> str:
    return " ".join(value.split()[:words]) if isinstance(value, str) else ""


def _card_details(card: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_card(card)
    oracle = html.unescape(re.sub(r"<[^>]+>", "", str(card.get("oracle_text") or "")))
    lines = list(dict.fromkeys(line.strip() for line in oracle.splitlines() if line.strip()))
    return {
        "grp_id": card["grp_id"],
        "name": card.get("name", "Unknown"),
        "mana_cost": normalized.mana_cost,
        "mana_value": normalized.cmc,
        "colors": list(normalized.colors),
        "type_line": card.get("type_line", ""),
        "oracle_text": "\n".join(lines),
        "gih_wr": card.get("gih_wr"),
        "power": card.get("power", ""),
        "toughness": card.get("toughness", ""),
        "related_faces": card.get("related_faces") or [],
        "rules_roles": rules_profile(card),
    }


def pool_summary(cards: list[dict[str, Any]]) -> dict[str, Any]:
    normalized = [normalize_card(card) for card in cards]
    lane = compute_lane(normalized, FormatContext())
    needs = analyze_pool(normalized, FormatContext(), lane)
    return {
        "main_colors": list(lane.main_colors[:2]),
        "creatures": needs.creatures,
        "early_plays": needs.early_plays,
        "removal": needs.removal,
        "five_plus_mana": needs.heavy_drops,
        "fixing": needs.fixing,
        "pool_size": len(cards),
    }


class DraftAdvisor:
    """One bounded model call per pick, with a validated deterministic fallback."""

    def __init__(self, backend: Any, timeout: float = 8.0) -> None:
        self._backend = backend
        self._timeout = timeout
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._pending: concurrent.futures.Future | None = None
        self._event_name = ""
        self._pool_size = 0
        self._plan = ""

    def _complete(
        self, message: str, system_prompt: str = DRAFT_SYSTEM_PROMPT, max_tokens: int = 1200
    ) -> str:
        try:
            return self._backend.complete(
                system_prompt,
                message,
                max_tokens,
                temperature=0.0,
                request_timeout_s=self._timeout,
            )
        except TypeError:
            return self._backend.complete(system_prompt, message)

    def recommend_deck(self, fallback: dict[str, Any]) -> dict[str, Any]:
        from arenamcp.limited_deck import DECK_SYSTEM_PROMPT, validate_deck

        pool = fallback.get("pool_cards") or []
        if not pool or (self._pending is not None and not self._pending.done()):
            return fallback
        counts = Counter(card["grp_id"] for card in pool)
        unique = {card["grp_id"]: card for card in pool}
        message = json.dumps(
            {
                "target_size": 40,
                "previous_plan": self._plan,
                "supported_synergies": synergy_graph(pool),
                "pool": [{**_card_details(card), "count": counts[grp_id]} for grp_id, card in unique.items()],
            },
            ensure_ascii=False,
        )
        self._pending = self._executor.submit(self._complete, message, DECK_SYSTEM_PROMPT, 2400)
        try:
            response = self._pending.result(timeout=self._timeout)
            start = response.find("{")
            if start < 0:
                raise ValueError("Deck response has no JSON object")
            payload, _end = json.JSONDecoder().raw_decode(response[start:])
            result = validate_deck(payload, pool, source="card_rules")
            return {**fallback, **result}
        except Exception as exc:
            logger.warning("Deck reasoning unavailable; keeping counted fallback build: %s", exc)
            return fallback

    def recommend(self, pack: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
        cards = pack.get("cards") or []
        pool = pack.get("picked_cards") or []
        if not cards:
            return fallback
        event_name = str(pack.get("event_name") or pack.get("set_code") or "")
        if event_name != self._event_name or len(pool) < self._pool_size:
            self._plan = ""
        self._event_name = event_name
        self._pool_size = len(pool)
        if self._pending is not None and not self._pending.done():
            return fallback
        required = min(max(1, int(pack.get("picks_per_pack") or 1)), len(cards))
        counted_pool: dict[int, dict[str, Any]] = {}
        for card in pool:
            grp_id = card["grp_id"]
            if grp_id not in counted_pool:
                counted_pool[grp_id] = {**_card_details(card), "copies": 0}
            counted_pool[grp_id]["copies"] += 1
        message = json.dumps(
            {
                "event": event_name,
                "pack_number": pack.get("pack_number"),
                "pick_number": pack.get("pick_number"),
                "picks_required": required,
                "supported_synergies": synergy_graph(cards + pool),
                "previous_plan": self._plan,
                "actual_pool": list(counted_pool.values()),
                "pool_summary": pool_summary(pool),
                "pack": [_card_details(card) for card in cards],
                "heuristic_rankings": fallback.get("evaluations") or [],
            },
            ensure_ascii=False,
        )
        self._pending = self._executor.submit(self._complete, message)
        try:
            response = self._pending.result(timeout=self._timeout)
            start = response.find("{")
            if start < 0:
                raise ValueError("Draft response has no JSON object")
            payload, _end = json.JSONDecoder().raw_decode(response[start:])
            result = self._validate(payload, cards, pool, required)
        except Exception as exc:
            logger.warning("Draft reasoning unavailable; using card-text guidance: %s", exc)
            return {**fallback, "reasoning_source": "heuristic"}
        self._plan = result["plan"]
        advice = f"Pack {pack.get('pack_number')}, Pick {pack.get('pick_number')}. "
        advice += " ".join(f"Take {pick['name']}. {pick['reason']}" for pick in result["recommendations"])
        advice += f" Plan: {result['plan']}"
        detailed = advice
        if result["needs"]:
            detailed += "\nStill need: " + "; ".join(result["needs"])
        if result.get("alternative"):
            alternative = result["alternative"]
            detailed += f"\nAlternative: {alternative['name']}. {alternative['reason']}"
        return {
            **fallback,
            **result,
            "spoken_advice": advice,
            "detailed_advice": detailed,
            "reasoning_source": "card_rules",
        }

    @staticmethod
    def _validate(payload: Any, cards: list[dict], pool: list[dict], required: int) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Draft response must be an object")
        picks = payload.get("picks")
        if not isinstance(picks, list) or len(picks) != required:
            raise ValueError("Draft response has the wrong number of picks")
        available = Counter(card["grp_id"] for card in cards)
        names = {card["grp_id"]: card["name"] for card in cards}
        selected = []
        for pick in picks:
            if not isinstance(pick, dict) or type(pick.get("grp_id")) is not int:
                raise ValueError("Draft pick must identify a pack card")
            grp_id = pick["grp_id"]
            if available[grp_id] <= 0:
                raise ValueError("Draft pick is absent from the pack")
            available[grp_id] -= 1
            reason = _text(pick.get("reason"))
            if not reason:
                raise ValueError("Draft pick needs an explanation")
            selected.append({"grp_id": grp_id, "name": names[grp_id], "reason": reason})
        owned = {card["grp_id"] for card in pool} | {pick["grp_id"] for pick in selected}
        by_id = {card["grp_id"]: card for card in cards + pool}
        for pick, raw in zip(selected, picks, strict=True):
            synergy = raw.get("synergy_with") or []
            if not isinstance(synergy, list) or any(
                type(grp_id) is not int or grp_id not in owned or grp_id == pick["grp_id"]
                for grp_id in synergy
            ):
                raise ValueError("Draft synergy references a card outside the actual pool or chosen pair")
            pick["synergy_with"] = synergy
            evidence = [
                edge for grp_id in synergy for edge in synergy_evidence(by_id[pick["grp_id"]], by_id[grp_id])
            ]
            if any(not synergy_evidence(by_id[pick["grp_id"]], by_id[grp_id]) for grp_id in synergy):
                raise ValueError("Draft synergy lacks a supported rules-text enabler/payoff link")
            pick["synergy_evidence"] = evidence
        plan = _text(payload.get("plan"))
        if not plan:
            raise ValueError("Draft response needs a deck plan")
        needs = payload.get("needs") or []
        if not isinstance(needs, list):
            raise ValueError("Draft needs must be a list")
        result = {"recommendations": selected, "plan": plan, "needs": [_text(need, 16) for need in needs[:3]]}
        alternative = payload.get("alternative")
        if isinstance(alternative, dict):
            grp_id = alternative.get("grp_id")
            reason = _text(alternative.get("reason"))
            if type(grp_id) is int and available[grp_id] > 0 and reason:
                result["alternative"] = {"grp_id": grp_id, "name": names[grp_id], "reason": reason}
        return result
