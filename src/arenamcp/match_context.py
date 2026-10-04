"""Shared deck knowledge for tactical advice and background strategy.

Resolve rules locally, cache card lookups, and put immutable deck facts before
the changing decision. No LLM call, network lookup, or history replay is needed
to prepare a request. Library composition is an estimate, never a known order.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter, OrderedDict
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from arenamcp.library_counts import observed_library_count

logger = logging.getLogger(__name__)
_ZONES = ("hand", "battlefield", "graveyard", "exile", "stack", "command")
_resolved_cards: OrderedDict[tuple[int, int], tuple[Any, dict]] = OrderedDict()
_cache_lock = threading.Lock()

STRATEGIC_POLICY = (
    "Follow the deck's win conditions and current game plan, but reassess every decision against "
    "the live board, stack, mana and legal options. A plan is conditional, not a forced script. "
    "Survival, a changed threat, new information or an immediate winning line can override it. "
    "For removal, compare acting now with holding interaction, the target's actual impact, protection, "
    "and the resource trade. For tutors, choose an eligible card still available for the current "
    "need: survival/interaction, mana, engine, protection, or closing the game. Account for the "
    "tutor's destination, restrictions, X and the cost/timing of using the found card. Recheck actual "
    "search options when they arrive. Do not tutor a habitual favorite or spend removal merely "
    "because it is payable. Library composition does not reveal draw order or guarantee a draw. "
    "An unknown library count or missing deck inventory does not mean the library is empty; "
    "never reject a fetch/tutor or predict decking based on missing observations. "
    "A fetch land that sacrifices itself for one land replaces a land; it is not net land ramp. "
    "Evaluate its life cost, needed colors/mana, timing and actual synergies; deck thinning alone "
    "is not a reason to crack it immediately. "
    "Apply the deck playbook's relevant mechanisms and conditions to each choice, including combat, "
    "sacrifices, discards, commander zones and mana use. Equal combat stats do not imply equal "
    "strategic value. Compare preserving an ongoing engine with spending a recoverable resource "
    "to replay a supported trigger; account for the surviving board, colored mana, commander tax, "
    "timing, summoning sickness and opportunity cost. Do not count a sacrificed source toward its "
    "own recovery or assume a token has the original's cast/enter/zone options. Neither commanders "
    "nor tokens are automatically expendable. Before choosing, compare the strongest legal "
    "alternative and name the relevant mechanism or the live constraint that makes deviating better. "
    "Use only effects supported by the supplied Oracle text: cast, enter, attack, death and zone "
    "changes are different events. A static material/combat score does not price these strategic effects."
)


@lru_cache(maxsize=4096)
def _local_card(grp_id: int, epoch: int) -> dict[str, Any]:
    # Refresh periodically so startup's asynchronously loaded databases can
    # replace unresolved entries; warm decisions reuse the same local facts.
    from arenamcp.card_db import FallbackCardDatabase, ScryfallAdapter, get_card_database

    source_database = get_card_database()
    cache_key = (id(source_database), grp_id)
    with _cache_lock:
        if cache_key in _resolved_cards:
            _resolved_cards.move_to_end(cache_key)
            return _resolved_cards[cache_key][1]
    database = FallbackCardDatabase(
        [source for source in source_database.sources if not isinstance(source, ScryfallAdapter)]
    )
    card = database.get_card_by_arena_id(grp_id)
    if card is None:
        return {"name": f"Unknown({grp_id})"}
    result = {
        key: getattr(card, key, "")
        for key in ("name", "type_line", "mana_cost", "cmc", "power", "toughness", "oracle_text")
    }
    result["related_faces"] = getattr(card, "related_faces", [])
    if card.name and card.type_line and card.oracle_text:
        with _cache_lock:
            # Retain the source object so its identity cannot be reused while cached.
            _resolved_cards[cache_key] = (source_database, result)
            if len(_resolved_cards) > 2048:
                _resolved_cards.popitem(last=False)
    return result


def _ids(values: Any) -> list[int]:
    return [value for value in values or [] if isinstance(value, int) and value > 4]


def _zone(state: dict, name: str) -> list[dict]:
    if name in state:
        return state[name] or []
    zones = state.get("zones") or {}
    return zones.get(name) or (zones.get("my_hand") if name == "hand" else []) or []


def _card_line(info: dict, count: int, grp_id: int) -> str:
    name = info.get("name") or f"Unknown({grp_id})"
    fields = [
        f"{count}x {name} [grp:{grp_id}]",
        str(info.get("mana_cost") or ""),
        str(info.get("type_line") or ""),
    ]
    if info.get("cmc") is not None and info.get("cmc") != "":
        fields.append(f"MV {info['cmc']}")
    if info.get("power") not in (None, "") and info.get("toughness") not in (None, ""):
        fields.append(f"printed {info['power']}/{info['toughness']}")
    fields.append(str(info.get("oracle_text") or "Rules text unavailable; do not invent abilities."))
    for face in info.get("related_faces") or []:
        if isinstance(face, dict) and face.get("oracle_text"):
            fields.append(
                f"Other face {face.get('name', '?')}: {face.get('mana_cost', '')} "
                f"{face.get('type_line', '')} — {face['oracle_text']}"
            )
    return " | ".join(" ".join(part.split()) for part in fields if part)


def prepare_match_context(state: dict, *, card_lookup: Callable[[int], Any] | None = None) -> dict:
    """Return a shallow snapshot with full deck rules and remaining inventory.

    Ownership (not controller) determines library membership. Tokens/abilities
    and duplicated instance records do not consume deck copies. Hidden, copied,
    transformed or generated cards can make this estimate incomplete; report
    that uncertainty instead of pretending the draw probabilities are exact.
    ``card_lookup`` is a local resolver hook for offline renders and tests.
    """
    result = dict(state)
    observed = observed_library_count(state)
    if observed is None:
        # Some consumers send structured state alongside the rendered prompt.
        # Do not leave a contradictory legacy zero in that JSON representation.
        if "library_count" in result:
            result["library_count"] = "?"
            result["library_count_source"] = "unknown"
        zones = state.get("zones")
        if isinstance(zones, dict) and "library_count" in zones:
            result["zones"] = {**zones, "library_count": "?", "library_count_source": "unknown"}
    deck = Counter(_ids(state.get("deck_cards")))
    if not deck:
        count_text = f"Arena library count {observed}" if observed is not None else "count UNKNOWN"
        result["library_summary"] = (
            f"MY LIBRARY ({count_text}): starting deck inventory unavailable; composition unknown. "
            "Missing inventory does not mean empty. Actual search options are authoritative."
        )
        return result
    local = state.get("local_seat_id") or next(
        (p.get("seat_id") for p in state.get("players", []) if p.get("is_local")), None
    )
    from arenamcp.deck_strategy import commander_ids

    commanders = Counter(commander_ids(state))
    lookup = card_lookup or (lambda grp_id: _local_card(grp_id, int(time.monotonic() // 30)))
    catalog: dict[int, dict] = {}
    for grp_id in sorted(deck.keys() | commanders.keys()):
        try:
            info = lookup(grp_id)
            catalog[grp_id] = dict(info) if isinstance(info, dict) else vars(info).copy() if info else {}
        except Exception as exc:
            logger.debug("Local deck lookup failed for %s: %s", grp_id, exc)
            catalog[grp_id] = {}
    reference = [
        "DECK REFERENCE (starting composition; current locations and modified stats are below)",
        "Printed rules are reference facts, not evidence that a card is in hand or currently castable.",
    ]
    reference.extend(_card_line(catalog[gid], count, gid) for gid, count in sorted(deck.items()))
    if commanders:
        reference.append("COMMANDER REFERENCE (availability and command tax depend on live state)")
        reference.extend(_card_line(catalog[gid], count, gid) for gid, count in sorted(commanders.items()))
    result["deck_reference"] = "\n".join(reference)
    result["deck_catalog"] = catalog

    remaining = deck.copy()
    seen: set[int] = set()
    uncertain = local is None
    for zone_name in _ZONES:
        for card in _zone(state, zone_name):
            if card.get("owner_seat_id") is None:
                uncertain = True
            if local is None or card.get("owner_seat_id") != local:
                continue
            kind = str(card.get("object_kind") or card.get("object_type") or "").lower()
            if card.get("is_token") or "token" in kind or "ability" in kind:
                continue
            identity = card.get("instance_id")
            if identity is not None:
                if identity in seen:
                    continue
                seen.add(identity)
            if card.get("face_down"):
                uncertain = True
                continue
            grp_id = card.get("original_grp_id") or card.get("base_grp_id") or card.get("grp_id")
            if card.get("is_copy") or card.get("copied_from_grp_id"):
                if not card.get("original_grp_id") and not card.get("base_grp_id"):
                    uncertain = True
                    continue
            if remaining[grp_id] > 0:
                remaining[grp_id] -= 1
            elif grp_id not in commanders:
                uncertain = True
    total = sum(remaining.values())
    if observed is None or observed != total:
        uncertain = True
    label = f"MY LIBRARY ({total} cards left by starting-deck-minus-visible estimate"
    if isinstance(observed, int):
        label += f"; Arena library count {observed}"
    else:
        label += "; observed count UNKNOWN, not evidence of an empty library"
    label += "):"
    lines = [label]
    if uncertain:
        lines.append(
            "Composition uncertain: hidden/transformed/copied/generated cards or count mismatch; "
            "do not treat these as exact draw odds. Actual search options are authoritative."
        )
    else:
        lines.append(
            "Draw chances below assume this inventory is complete and the library is randomized; "
            "order is unknown. Conjure, seek, transforms and hidden moves may change the estimate."
        )
    for gid, count in sorted(remaining.items()):
        if count <= 0:
            continue
        odds = f" ({count / total:.1%} per random draw)" if total and not uncertain else ""
        lines.append(f"{count}x {catalog[gid].get('name') or f'Unknown({gid})'} [grp:{gid}]{odds}")
    if not total:
        lines.append("No original-deck cards remain in this estimate.")
    result["library_summary"] = "\n".join(lines)
    return result


def with_deck_reference(prompt: str, state: dict) -> str:
    """Keep stable deck text before volatile board/options for prefix caching."""
    reference = state.get("deck_reference")
    return f"{reference}\n\n{prompt}" if reference else prompt
