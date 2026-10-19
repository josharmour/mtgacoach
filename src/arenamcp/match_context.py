"""Shared deck knowledge for tactical advice and background strategy.

Resolve rules locally, cache card lookups, and put immutable deck facts before
the changing decision. No LLM call, network lookup, or history replay is needed
to prepare a request. Library composition is an estimate, never a known order.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterable
from functools import lru_cache
from typing import Any

from arenamcp.library_counts import observed_library_count
from arenamcp.situational_rules import SUBJECTS as SITUATIONAL_SUBJECTS
from arenamcp.situational_rules import compose as compose_rules

logger = logging.getLogger(__name__)
_ZONES = ("hand", "battlefield", "graveyard", "exile", "stack", "command")
_resolved_cards: OrderedDict[tuple[int, int], tuple[Any, dict]] = OrderedDict()
_cache_lock = threading.Lock()

# 2026-10-10 Brawl: the deck reference quoted the rules of all 99 cards in every
# call (~7k tokens of a 24k-token plan prompt, ~15k-token decision prompts).
# The reference now lists the whole deck compactly (name, cost, type) and quotes
# full rules only for cards seen this game. The seen list is kept per match in
# first-seen order, so the rules section only ever grows: within a turn every
# call shares the same prefix for the server's prefix cache.
_seen_lock = threading.Lock()
_seen_rules: tuple[Any, list[int]] = (None, [])


def reset_seen_rules() -> None:
    """Forget which cards' rules have been quoted (a test hook: a new match id resets it anyway)."""
    global _seen_rules
    with _seen_lock:
        _seen_rules = (None, [])


# Subject-tagged like the decision rules (situational_rules): the tutor and
# fetch-land sentences need a library effect in play.
STRATEGIC_POLICY_PARTS: tuple[tuple[str | None, str], ...] = (
    (
        None,
        "Follow the deck's win conditions and current game plan, but reassess every decision against "
        "the live board, stack, mana and legal options. A plan is conditional, not a forced script. "
        "Survival, a changed threat, new information or an immediate winning line can override it. "
        "For removal, compare acting now with holding interaction, the target's actual impact, protection, "
        "and the resource trade. ",
    ),
    (
        "library",
        "For tutors, choose an eligible card still available for the current "
        "need: survival/interaction, mana, engine, protection, or closing the game. Account for the "
        "tutor's destination, restrictions, X and the cost/timing of using the found card. Recheck actual "
        "search options when they arrive. ",
    ),
    (
        None,
        "Do not tutor a habitual favorite or spend removal merely "
        "because it is payable. Library composition does not reveal draw order or guarantee a draw. "
        "An unknown library count or missing deck inventory does not mean the library is empty; "
        "never reject a fetch/tutor or predict decking based on missing observations. ",
    ),
    (
        "library",
        "A fetch land that sacrifices itself for one land replaces a land; it is not net land ramp. "
        "Evaluate its life cost, needed colors/mana, timing and actual synergies; deck thinning alone "
        "is not a reason to crack it immediately. ",
    ),
    (
        None,
        "Apply the deck playbook's relevant mechanisms and conditions to each choice, including combat, "
        "sacrifices, discards, commander zones and mana use. Equal combat stats do not imply equal "
        "strategic value. Compare preserving an ongoing engine with spending a recoverable resource "
        "to replay a supported trigger; account for the surviving board, colored mana, commander tax, "
        "timing, summoning sickness and opportunity cost. Do not count a sacrificed source toward its "
        "own recovery or assume a token has the original's cast/enter/zone options. Neither commanders "
        "nor tokens are automatically expendable. Before choosing, compare the strongest legal "
        "alternative and name the relevant mechanism or the live constraint that makes deviating better. "
        "Use only effects supported by the supplied Oracle text: cast, enter, attack, death and zone "
        "changes are different events. A static material/combat score does not price these strategic effects.",
    ),
)
STRATEGIC_POLICY = compose_rules(STRATEGIC_POLICY_PARTS)


def strategic_policy(subjects: Iterable[str] = SITUATIONAL_SUBJECTS) -> str:
    """The strategic policy for the rule subjects in play (situational_rules.subjects_in_play)."""
    return compose_rules(STRATEGIC_POLICY_PARTS, subjects)


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


_MARKUP = re.compile(r"<[^>]*>")


def oracle_lines(text: Any) -> list[str]:
    """Rules text as prompt lines: markup stripped, Arena's repeated renderings dropped.

    MTGA's card data stores many abilities two or three times (plain, then
    ``<nobr>``/``<i>``/``<indent>`` variants), and a modal ability repeats with
    its "•" modes: 2026-10-18 prompts quoted Yuriko's and Divining Duelist's
    triggers three times each, and the cached Brawl playbooks quoted every
    "+1/+1 counter" rule three times. An ability (a line plus the "•" lines
    under it) equal to an earlier one is dropped; two different abilities keep
    their own modes even when a mode reads the same.
    """
    lines = [plain_rule(raw) for raw in str(text or "").replace("\r", "\n").split("\n")]
    return [lines[index] for index in unique_abilities(lines)]


def plain_rule(line: Any) -> str:
    """One rules line without markup or runs of whitespace."""
    return " ".join(_MARKUP.sub("", str(line or "")).split())


def unique_abilities(lines: list[str]) -> list[int]:
    """Indices of the non-empty ``lines`` (markup-free) that are not a repeat of an earlier ability."""
    blocks: list[list[int]] = []
    for index, line in enumerate(lines):
        if not line:
            continue
        if line.startswith("•") and blocks:
            blocks[-1].append(index)
        else:
            blocks.append([index])
    seen: set[tuple[str, ...]] = set()
    keep: list[int] = []
    for block in blocks:
        key = tuple(lines[index] for index in block)
        if key not in seen:
            seen.add(key)
            keep.extend(block)
    return keep


def _ids(values: Any) -> list[int]:
    return [value for value in values or [] if isinstance(value, int) and value > 4]


def _zone(state: dict, name: str) -> list[dict]:
    if name in state:
        return state[name] or []
    zones = state.get("zones") or {}
    return zones.get(name) or (zones.get("my_hand") if name == "hand" else []) or []


def _compact_line(info: dict, count: int, grp_id: int) -> str:
    """One line per deck card without rules: count, name, cost, type, printed stats."""
    name = info.get("name") or f"Unknown({grp_id})"
    fields = [
        f"{count}x {name} [grp:{grp_id}]",
        str(info.get("mana_cost") or ""),
        str(info.get("type_line") or ""),
    ]
    if info.get("power") not in (None, "") and info.get("toughness") not in (None, ""):
        fields.append(f"{info['power']}/{info['toughness']}")
    return " ".join(" ".join(part.split()) for part in fields if part)


def _visible_grp_ids(state: dict, catalog: dict[int, dict]) -> list[int]:
    """Deck cards in a visible zone now (either side's copy), in zone order."""
    out: list[int] = []
    for zone_name in _ZONES:
        for card in _zone(state, zone_name):
            for key in ("grp_id", "original_grp_id", "base_grp_id"):
                gid = card.get(key)
                if isinstance(gid, int) and gid in catalog and gid not in out:
                    out.append(gid)
    return out


def _rules_order(state: dict, catalog: dict[int, dict], visible: list[int]) -> list[int]:
    """Cards whose rules the reference quotes: everything seen so far this match, first-seen first."""
    match_id = state.get("match_id")
    if match_id is None:
        return sorted(visible)
    global _seen_rules
    with _seen_lock:
        key, seen = _seen_rules
        if key != match_id:
            seen = []
        seen = seen + [gid for gid in visible if gid not in seen]
        _seen_rules = (match_id, seen)
    return [gid for gid in seen if gid in catalog]


_TUTOR_RE = re.compile(r"search your library for", re.I)
_LAND_SEARCH_RE = re.compile(
    r"search your library for (?:a |an |up to \w+ |two |three )?(?:basic )?(?:land|forest|plains|island|swamp|mountain)",
    re.I,
)


def _tutor_in_hand(state: dict) -> bool:
    """A nonland tutor in our hand can find any library card, so every card's rules matter."""
    local = state.get("local_seat_id") or next(
        (p.get("seat_id") for p in state.get("players", []) if p.get("is_local")), None
    )
    for card in _zone(state, "hand"):
        if local is not None and card.get("owner_seat_id") not in (None, local):
            continue
        text = str(card.get("oracle_text") or "")
        if _TUTOR_RE.search(text) and not _LAND_SEARCH_RE.search(text):
            return True
    return False


# Effects whose outcome depends on what is left in the library: searches
# (tutors, fetches, ramp, typecycling), looking at or revealing its top
# (scry, surveil, explore, seek, discover, cascade) and milling.
_COUNT = r"(?:a|an|one|two|three|four|five|six|seven|x|\d+)"
_LIBRARY_EFFECT_RE = re.compile(
    r"search (?:your|their|its owner's|that player's) library|\b\w+cycling\b"
    rf"|\b(?:scry|surveil|discover) (?:\d+|x)\b|\bmills? {_COUNT}\b|\bseeks? {_COUNT}\b"
    r"|\bexplores?\b|\bcascade\b"
    r"|top (?:\w+ )?cards? of (?:your|their|his or her|its owner's) library"
    r"|(?:look at|reveal|exile) the top",
    re.I,
)
LIBRARY_DECISIONS = frozenset({"search", "scry", "surveil", "mill", "explore"})
LIBRARY_POINTER = (
    "MY LIBRARY: the DECK REFERENCE minus the cards seen this game; its inventory is listed when a "
    "search, scry, surveil, mill or top-of-library effect is in play."
)


def library_in_play(state: dict) -> bool:
    """Whether the remaining library's composition can matter right now.

    True for a library decision (search, scry, surveil, mill, explore), and
    when one of our cards that could act now reaches the library: a card in
    hand or the command zone, our object on the stack, or a permanent of ours
    with a library ability that is not a one-time enters trigger. Otherwise
    the inventory (up to ~1.2k tokens in Brawl) only restates the deck
    reference minus the visible zones (2026-10-18 prompt audit). A snapshot
    it cannot read lists the inventory.
    """
    try:
        return _library_in_play(state)
    except Exception as error:
        logger.debug("library relevance unknown, listing the inventory: %s", error)
        return True


def _library_in_play(state: dict) -> bool:
    context = state.get("decision_context")
    context = context if isinstance(context, dict) else {}
    request = f"{context.get('type') or ''} {state.get('_bridge_request_type') or ''}".lower()
    if any(kind in request for kind in LIBRARY_DECISIONS):
        return True
    local = state.get("local_seat_id") or next(
        (p.get("seat_id") for p in state.get("players", []) if isinstance(p, dict) and p.get("is_local")),
        None,
    )

    def ours(card: Any) -> bool:
        if not isinstance(card, dict):
            return False
        side = card.get("controller_seat_id") or card.get("owner_seat_id")
        return local is None or side in (None, local)

    for zone_name in ("hand", "command", "stack"):
        for card in _zone(state, zone_name):
            if ours(card) and _LIBRARY_EFFECT_RE.search(" ".join(oracle_lines(card.get("oracle_text")))):
                return True
    for card in _zone(state, "battlefield"):
        if not ours(card):
            continue
        resolved = False
        for line in oracle_lines(card.get("oracle_text")):
            if not line.startswith("•"):  # a "•" mode belongs to the ability above it
                resolved = _one_time_enters(line)
            if not resolved and _LIBRARY_EFFECT_RE.search(line):
                return True
    return False


def _one_time_enters(line: str) -> bool:
    """A permanent's "When ... enters" trigger: it resolved when the permanent arrived."""
    head = line.split(",", 1)[0].lower()
    return head.startswith(("when ", "as ")) and "enter" in head and not re.search(r"\b(?:or|and)\b", head)


def _mana_value(value: Any) -> str:
    return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)


def _card_line(info: dict, count: int, grp_id: int, *, listed: bool = False) -> str:
    """A card's rules line. ``listed``: the deck list above already gives its count, cost, type and stats."""
    name = info.get("name") or f"Unknown({grp_id})"
    if listed:
        fields = [f"{name} [grp:{grp_id}]"]
    else:
        fields = [
            f"{count}x {name} [grp:{grp_id}]",
            str(info.get("mana_cost") or ""),
            str(info.get("type_line") or ""),
        ]
    if info.get("cmc") is not None and info.get("cmc") != "":
        fields.append(f"MV {_mana_value(info['cmc'])}")
    if not listed and info.get("power") not in (None, "") and info.get("toughness") not in (None, ""):
        fields.append(f"printed {info['power']}/{info['toughness']}")
    fields.append(
        " ".join(oracle_lines(info.get("oracle_text"))) or "Rules text unavailable; do not invent abilities."
    )
    for face in info.get("related_faces") or []:
        if isinstance(face, dict) and face.get("oracle_text"):
            fields.append(
                f"Other face {face.get('name', '?')}: {face.get('mana_cost', '')} "
                f"{face.get('type_line', '')} — {' '.join(oracle_lines(face['oracle_text']))}"
            )
    return " | ".join(" ".join(part.split()) for part in fields if part)


def prepare_match_context(
    state: dict,
    *,
    card_lookup: Callable[[int], Any] | None = None,
    reveal: Iterable[int] = (),
    full_rules: bool = False,
    library: bool | None = None,
) -> dict:
    """Return a shallow snapshot with the deck reference, catalog and remaining inventory.

    The reference lists the whole deck compactly and quotes rules for the
    cards seen this game (plus ``reveal``: grp ids a pending decision offers,
    such as tutor candidates, quoted for this call only). ``full_rules=True``
    quotes every card (the library inspection and win-in-N paths).
    ``library_listed`` on the result says whether a prompt lists the
    remaining inventory (``library_summary``) or only points to the deck
    reference: ``library=True`` always lists it (strategy prompts),
    ``None`` lists it when :func:`library_in_play` (tactical prompts).
    Ownership (not controller) determines library membership. Tokens/abilities
    and duplicated instance records do not consume deck copies. Hidden, copied,
    transformed or generated cards can make this estimate incomplete; report
    that uncertainty instead of pretending the draw probabilities are exact.
    ``card_lookup`` is a local resolver hook for offline renders and tests.
    ``deck_rules_grp_ids`` on the result names the cards whose rules are quoted.
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
        "DECK REFERENCE (starting composition: count, name, cost, type; current locations and modified stats are below)",
        "Printed facts, not evidence that a card is in hand or currently castable.",
    ]
    reference.extend(_compact_line(catalog[gid], count, gid) for gid, count in sorted(deck.items()))
    if commanders:
        reference.append("COMMANDER REFERENCE (availability and command tax depend on live state)")
        reference.extend(_card_line(catalog[gid], count, gid) for gid, count in sorted(commanders.items()))
    full_rules = full_rules or _tutor_in_hand(state)
    if full_rules:
        quoted = sorted(deck)
    else:
        quoted = _rules_order(state, catalog, _visible_grp_ids(state, catalog))
        quoted += sorted(
            gid for gid in {int(g) for g in reveal if isinstance(g, int)} if gid in deck and gid not in quoted
        )
    quoted = [gid for gid in quoted if gid not in commanders]
    if quoted:
        # Each card's count, cost, type and printed stats are on its deck-list
        # line above; its rules line adds only MV and rules (2026-10-18: every
        # seen card's facts were sent twice in the same reference).
        reference.append(
            "RULES OF THE CARDS SEEN THIS GAME (hand, battlefield, graveyards, exile, stack"
            + (", and the pending decision's candidates" if reveal else "")
            + "; cost, type and printed stats as listed above; the rest of the library is listed above "
            "by name only: do not invent its rules)"
            if not full_rules
            else "RULES (every card in the deck: a tutor in hand or a library inspection can reach any of them; "
            "cost, type and printed stats as listed above)"
        )
        reference.extend(_card_line(catalog[gid], deck[gid], gid, listed=True) for gid in quoted)
    result["deck_reference"] = "\n".join(reference)
    result["deck_catalog"] = catalog
    result["deck_rules_grp_ids"] = sorted(set(quoted) | set(commanders))
    result["library_listed"] = bool(library or full_rules or library_in_play(state))

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
