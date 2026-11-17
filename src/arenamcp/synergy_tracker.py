"""Live deck synergy and combo state tracking (Fable Nov G4).

Evaluates a deck study's (or legacy playbook's) synergy entries deterministically
against the current zones, remaining library inventory, turn number, and available
mana — classifying each interaction into ``live``, ``one_step_away``, or
``out_of_reach`` without any LLM calls.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from arenamcp.card_facts.prompt_render import candidate_fact_hashes
from arenamcp.card_facts.reviewed_notes import corpus, reviewed_print
from arenamcp.mana import get_local_seat_id

_GRAVEYARD_RECOVERABLE_RE = re.compile(
    r"\b(?:flashback|escape|unearth|disturb|retrace|jump-start|embalm|eternalize|encore|harmonize|dredge)\b"
    r"|\bfrom your graveyard\b|\bcast [^.]+ from your graveyard\b|\bplay lands? from your graveyard\b",
    re.I,
)
_EXILE_PLAYABLE_RE = re.compile(
    r"\b(?:adventure|foretell|plot|airbend|warp)\b|\byou may (?:play|cast)\b[^.]*\bfrom exile\b",
    re.I,
)
_RECURSION_SOURCE_RE = re.compile(
    r"\breturn target [^.]+ from your graveyard\b|\bcast [^.]+ from your graveyard\b",
    re.I,
)
_TUTOR_WINDOW_RE = re.compile(
    r"\b(?:search|tutor|scry|surveil|mulligan|discard|select_n|selectn|select_from_groups|searchfromgroups|group|london)\b",
    re.I,
)


@dataclass(frozen=True)
class SynergyEntry:
    """Machine-checkable interaction extracted from a deck study or fallback playbook."""

    id: str
    kind: str
    pieces: tuple[tuple[int, str], ...]
    mana_generic: int = 0
    mana_colors: dict[str, int] = field(default_factory=dict)
    first_turn: int = 1
    outcome: str = ""
    stops: str = ""
    piece_names: dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SynergySnapshot:
    """Evaluated live / one-step-away / out-of-reach breakdown for one game state."""

    live: tuple[dict[str, Any], ...] = ()
    one_step_away: tuple[dict[str, Any], ...] = ()
    out_of_reach: tuple[dict[str, Any], ...] = ()


def _zone_cards(state: dict[str, Any], name: str) -> list[dict[str, Any]]:
    if name in state and isinstance(state[name], list):
        return [c for c in state[name] if isinstance(c, dict)]
    zones = state.get("zones")
    if isinstance(zones, dict):
        if name == "hand":
            raw = zones.get("hand") or zones.get("my_hand") or ()
        else:
            raw = zones.get(name) or ()
        if isinstance(raw, list):
            return [c for c in raw if isinstance(c, dict)]
    return []


def _local_seat(state: dict[str, Any]) -> int | None:
    seat = state.get("local_seat_id")
    if type(seat) is int:
        return seat
    return get_local_seat_id(state) if isinstance(state.get("players"), list) else None


def _is_ours(card: dict[str, Any], local: int | None) -> bool:
    if local is None:
        return True
    side = card.get("owner_seat_id")
    ctrl = card.get("controller_seat_id")
    if ctrl is not None:
        return ctrl == local
    return side in (None, local)


def _owned_by_us(card: dict[str, Any], local: int | None) -> bool:
    if local is None:
        return True
    side = card.get("owner_seat_id")
    return side in (None, local)


def _parse_mana_cost_simple(mana_cost: str) -> tuple[int, dict[str, int]]:
    generic = 0
    colors: dict[str, int] = {}
    for sym in re.findall(r"\{([^{}]+)\}", str(mana_cost or "").upper()):
        if sym.isdigit():
            generic += int(sym)
        elif sym in ("W", "U", "B", "R", "G", "C"):
            colors[sym] = colors.get(sym, 0) + 1
    return generic, colors


def _available_mana_estimate(state: dict[str, Any], local: int | None) -> tuple[int, Counter[str]]:
    pool = Counter()
    explicit_pool = state.get("mana_pool")
    if isinstance(explicit_pool, dict) and any(isinstance(v, int) and v > 0 for v in explicit_pool.values()):
        total = 0
        for color, amt in explicit_pool.items():
            if isinstance(amt, int) and amt > 0:
                total += amt
                if str(color).upper() in ("W", "U", "B", "R", "G", "C", "ANY"):
                    pool[str(color).upper()] += amt
        return total, pool

    total = 0
    turn_num = ((state.get("turn") or {}).get("turn_number")) or (
        (state.get("turn_info") or {}).get("turn_number")
    )
    for card in _zone_cards(state, "battlefield"):
        if not _is_ours(card, local) or card.get("is_tapped"):
            continue
        tline = str(card.get("type_line") or "").lower()
        oracle = str(card.get("oracle_text") or "").lower()
        if "land" in tline:
            is_fetch_only = (
                "sacrifice" in oracle
                and "search your library" in oracle
                and not re.search(r"\{o?T\}[^:]*:\s*add\b", oracle, re.I)
            )
            if is_fetch_only:
                continue
            total += 1
            cols_from_type: set[str] = set()
            for name_part, col in (
                ("plains", "W"),
                ("island", "U"),
                ("swamp", "B"),
                ("mountain", "R"),
                ("forest", "G"),
            ):
                if name_part in tline or name_part in str(card.get("name") or "").lower():
                    cols_from_type.add(col)
                    pool[col] += 1
            if "any color" in oracle:
                pool["ANY"] += 1
            if not cols_from_type:
                for col in ("W", "U", "B", "R", "G", "C"):
                    if f"{{{col.lower()}}}" in oracle or f"{{o{col.lower()}}}" in oracle:
                        pool[col] += 1
        elif ("creature" in tline or "artifact" in tline) and re.search(
            r"\{o?T\}[^:]*:\s*add\b", oracle, re.I
        ):
            if (
                "creature" in tline
                and turn_num
                and card.get("turn_entered_battlefield") == turn_num
                and "haste" not in oracle
            ):
                continue
            total += 1
            if "any color" in oracle:
                pool["ANY"] += 1
            for col in ("W", "U", "B", "R", "G", "C"):
                if f"{{{col.lower()}}}" in oracle or f"{{o{col.lower()}}}" in oracle:
                    pool[col] += 1
    return total, pool


def _can_pay(
    req_generic: int, req_colors: dict[str, int], avail_total: int, avail_pool: Counter[str]
) -> tuple[bool, int]:
    needed_total = max(0, req_generic) + sum(max(0, v) for v in req_colors.values())
    if needed_total <= 0:
        return True, 0
    shortfall = max(0, needed_total - avail_total)
    if shortfall > 0:
        return False, shortfall
    if not avail_pool:
        # Total mana available known without per-color breakdown -> allow based on total.
        return True, 0
    any_mana = avail_pool.get("ANY", 0)
    deficit = 0
    for col, amt in req_colors.items():
        if amt <= 0:
            continue
        have = avail_pool.get(col, 0)
        if have < amt:
            gap = amt - have
            used_any = min(any_mana, gap)
            any_mana -= used_any
            deficit += gap - used_any
    return deficit == 0, deficit


def entries_from_playbook(
    playbook: Any,
    *,
    deck_cards: Iterable[int] = (),
    card_facts_map: dict[str, dict[str, Any]] | None = None,
) -> list[SynergyEntry]:
    """Extract SynergyEntry list from a DeckPlaybook (study interactions, else mechanisms + links)."""
    if playbook is None:
        return []
    raw_data = getattr(playbook, "data", playbook)
    if not isinstance(raw_data, dict):
        return []
    data = raw_data
    raw_cat = getattr(playbook, "catalog", None)
    catalog = raw_cat if isinstance(raw_cat, dict) else {}
    entries: list[SynergyEntry] = []
    seen_sigs: set[tuple[int, ...]] = set()

    def _name(gid: int) -> str:
        info = catalog.get(gid) or catalog.get(str(gid)) or {}
        return str(info.get("name") or f"Card {gid}")

    study = data.get("study") if isinstance(data, dict) else None
    if isinstance(study, dict):
        for idx, item in enumerate(study.get("interactions") or (), 1):
            if not isinstance(item, dict):
                continue
            conds = item.get("conditions") or {}
            pieces_raw = conds.get("pieces") or ()
            pieces = tuple(
                (int(p["card"]), str(p.get("zone") or "battlefield").lower())
                for p in pieces_raw
                if isinstance(p, dict) and type(p.get("card")) is int
            )
            if len(pieces) < 2:
                continue
            sig = tuple(sorted(gid for gid, _ in pieces))
            seen_sigs.add(sig)
            mana = conds.get("mana") or {}
            generic = max(0, int(mana.get("generic", 0))) if isinstance(mana, dict) else 0
            colors = {
                k: int(v)
                for k, v in (mana.items() if isinstance(mana, dict) else ())
                if k in ("W", "U", "B", "R", "G", "C") and isinstance(v, int) and v > 0
            }
            entries.append(
                SynergyEntry(
                    id=str(item.get("id") or f"study_{idx}"),
                    kind=str(item.get("kind") or "synergy"),
                    pieces=pieces,
                    mana_generic=generic,
                    mana_colors=colors,
                    first_turn=max(1, int(conds.get("first_turn") or 1)),
                    outcome=str(item.get("outcome") or ""),
                    stops=str(item.get("stops") or ""),
                    piece_names={gid: _name(gid) for gid, _ in pieces},
                )
            )

    if entries:
        return entries

    # Fallback when only legacy playbook / mechanisms + reviewed notes / CardFacts exist.
    deck_iter = deck_cards if isinstance(deck_cards, (list, tuple, set, dict)) else ()
    deck_set = {int(g) for g in deck_iter if type(g) is int and g > 4} or {
        int(g) for g in catalog if isinstance(g, int) or str(g).isdigit()
    }
    raw_cmds = getattr(playbook, "commanders", ())
    commanders = {
        int(c) for c in (raw_cmds if isinstance(raw_cmds, (list, tuple, set)) else ()) if type(c) is int
    }
    for idx, mech in enumerate(data.get("mechanisms") or (), 1):
        if not isinstance(mech, dict):
            continue
        cards = [int(g) for g in (mech.get("cards") or ()) if type(g) is int and int(g) in deck_set]
        unique_cards = list(dict.fromkeys(cards))
        if len(unique_cards) < 2:
            continue
        sig = tuple(sorted(unique_cards))
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)
        pieces = tuple((gid, "command" if gid in commanders else "battlefield") for gid in unique_cards)
        entries.append(
            SynergyEntry(
                id=str(mech.get("id") or f"mech_{idx}"),
                kind="synergy",
                pieces=pieces,
                mana_generic=0,
                mana_colors={},
                first_turn=1,
                outcome=str(mech.get("effect") or ""),
                stops="; ".join(str(x) for x in (mech.get("avoid") or ())[:2]),
                piece_names={gid: _name(gid) for gid, _ in pieces},
            )
        )

    # Also add reviewed/CardFacts relationships inside this deck not yet listed.
    if catalog:
        _, records, _, _ = corpus()
        by_name: dict[str, list[int]] = {}
        for gid, info in catalog.items():
            if isinstance(info, dict) and info.get("name"):
                igid = int(gid)
                by_name.setdefault(str(info["name"]).strip().casefold(), []).append(igid)
        for gid, info in sorted(catalog.items()):
            igid = int(gid)
            if igid not in deck_set or not isinstance(info, dict):
                continue
            links: list[dict[str, Any]] = []
            rev = reviewed_print({**info, "grp_id": igid})
            if rev is not None and rev.name in records:
                links.extend(records[rev.name].get("relationships", {}).get("links") or ())
            if card_facts_map:
                for digest in candidate_fact_hashes(info):
                    rec = card_facts_map.get(digest)
                    if isinstance(rec, dict):
                        links.extend((rec.get("relationships") or {}).get("links") or ())
                        break
            for ln in links:
                if not isinstance(ln, dict):
                    continue
                target_name = str(ln.get("card") or ln.get("target") or "").strip().casefold()
                for tgid in by_name.get(target_name, ()):
                    if tgid == igid or tgid not in deck_set:
                        continue
                    sig = tuple(sorted((igid, tgid)))
                    if sig in seen_sigs:
                        continue
                    seen_sigs.add(sig)
                    claim = ""
                    if isinstance(ln.get("claim"), dict):
                        claim = str(ln["claim"].get("text") or "")
                    entries.append(
                        SynergyEntry(
                            id=f"rel_{sig[0]}_{sig[1]}",
                            kind="combo" if str(ln.get("role") or "").lower() == "combo_piece" else "synergy",
                            pieces=((igid, "battlefield"), (tgid, "battlefield")),
                            outcome=claim or f"{_name(igid)} + {_name(tgid)} ({ln.get('role', 'synergy')})",
                            stops="Removal or disruption on key piece",
                            piece_names={igid: _name(igid), tgid: _name(tgid)},
                        )
                    )
    return entries


def evaluate_synergies(
    entries: Iterable[SynergyEntry],
    game_state: dict[str, Any],
    *,
    catalog: dict[int, dict[str, Any]] | None = None,
    deck_list: Iterable[int] | None = None,
    available_mana: int | None = None,
) -> SynergySnapshot:
    """Classify each synergy entry into ``live``, ``one_step_away``, or ``out_of_reach``."""
    entry_list = list(entries or ())
    if not entry_list or not isinstance(game_state, dict):
        return SynergySnapshot()

    local = _local_seat(game_state)
    turn_num = (
        ((game_state.get("turn") or {}).get("turn_number"))
        or ((game_state.get("turn_info") or {}).get("turn_number"))
        or 1
    )
    if type(turn_num) is not int or turn_num < 1:
        turn_num = 1

    raw_cat = catalog if isinstance(catalog, dict) else game_state.get("deck_catalog")
    cat = raw_cat if isinstance(raw_cat, dict) else {}

    def _card_info(gid: int) -> dict[str, Any]:
        return cat.get(gid) or cat.get(str(gid)) or {}

    zone_gids: dict[str, set[int]] = {
        "battlefield": set(),
        "hand": set(),
        "command": set(),
        "stack": set(),
        "graveyard": set(),
        "exile": set(),
    }
    recoverable_gy_gids: set[int] = set()
    playable_exile_gids: set[int] = set()
    commanders = set(game_state.get("commander_grp_ids") or ())
    seen_non_token_copies: Counter[int] = Counter()
    seen_instance_ids: set[int] = set()

    gy_recursion_types: set[str] = set()
    has_unrestricted_gy_recursion = False
    for zone_name in ("battlefield", "hand", "command", "stack"):
        for card in _zone_cards(game_state, zone_name):
            src_oracle = str(card.get("oracle_text") or "")
            if _is_ours(card, local) and _RECURSION_SOURCE_RE.search(src_oracle):
                lower = src_oracle.lower()
                if "land card" in lower or "lands from your graveyard" in lower:
                    gy_recursion_types.add("land")
                elif "creature card" in lower or "creatures from your graveyard" in lower:
                    gy_recursion_types.add("creature")
                elif "instant or sorcery" in lower:
                    gy_recursion_types.update(("instant", "sorcery"))
                elif "artifact" in lower or "enchantment" in lower:
                    if "artifact" in lower:
                        gy_recursion_types.add("artifact")
                    if "enchantment" in lower:
                        gy_recursion_types.add("enchantment")
                else:
                    has_unrestricted_gy_recursion = True

    for zone_name in ("battlefield", "hand", "command", "stack", "graveyard", "exile"):
        for card in _zone_cards(game_state, zone_name):
            gid = card.get("original_grp_id") or card.get("base_grp_id") or card.get("grp_id")
            if type(gid) is not int or gid <= 4:
                continue
            belongs = (
                _is_ours(card, local) if zone_name in ("battlefield", "stack") else _owned_by_us(card, local)
            )
            if not belongs:
                continue
            zone_gids[zone_name].add(gid)
            if zone_name == "graveyard":
                info = _card_info(gid)
                oracle = str(card.get("oracle_text") or info.get("oracle_text") or "")
                tline = str(card.get("type_line") or info.get("type_line") or "").lower()
                typed_match = any(t in tline for t in gy_recursion_types)
                if (
                    _GRAVEYARD_RECOVERABLE_RE.search(oracle)
                    or gid in commanders
                    or has_unrestricted_gy_recursion
                    or typed_match
                ):
                    recoverable_gy_gids.add(gid)
            elif zone_name == "exile":
                oracle = str(card.get("oracle_text") or _card_info(gid).get("oracle_text") or "")
                if not card.get("face_down") and (
                    _EXILE_PLAYABLE_RE.search(oracle) or gid in commanders or card.get("castable_from_exile")
                ):
                    playable_exile_gids.add(gid)
            kind = str(card.get("object_kind") or "").lower()
            if not card.get("is_token") and "token" not in kind and "ability" not in kind:
                iid = card.get("instance_id")
                if iid is None or iid not in seen_instance_ids:
                    if iid is not None:
                        seen_instance_ids.add(iid)
                    seen_non_token_copies[gid] += 1

    raw_deck = list(deck_list if deck_list is not None else (game_state.get("deck_cards") or ()))
    remaining_lib: Counter[int] = Counter()
    if raw_deck:
        deck_counter = Counter(int(g) for g in raw_deck if type(g) is int and g > 4)
        for gid, count in deck_counter.items():
            remaining_lib[gid] = max(0, count - seen_non_token_copies.get(gid, 0))
    else:
        for row in game_state.get("commentary_library_inventory") or ():
            if isinstance(row, dict) and type(row.get("grp_id")) is int and type(row.get("count")) is int:
                remaining_lib[row["grp_id"]] = max(0, row["count"])

    avail_total, avail_pool = _available_mana_estimate(game_state, local)
    if available_mana is not None:
        avail_total = int(available_mana)
        avail_pool = Counter()  # explicit numeric budget supplied by caller/planner

    def _name_for(entry: SynergyEntry, gid: int) -> str:
        return entry.piece_names.get(gid) or str(_card_info(gid).get("name") or f"Card {gid}")

    def _piece_present(gid: int, req_zone: str) -> tuple[bool, bool, tuple[int, dict[str, int]]]:
        """Return (present_locally, can_ever_reach, deploy_cost_needed)."""
        info = _card_info(gid)
        cost_g, cost_c = _parse_mana_cost_simple(str(info.get("mana_cost") or ""))
        in_lib = remaining_lib.get(gid, 0) > 0
        # If no deck list is given at all, assume library might hold un-exiled copies unless singleton exiled/dead
        lib_possible = in_lib if raw_deck or game_state.get("commentary_library_inventory") else True

        if req_zone not in ("graveyard", "exile", "library") and (
            gid in zone_gids["battlefield"] or gid in zone_gids["stack"]
        ):
            return True, True, (0, {})
        if req_zone == "graveyard":
            if gid in zone_gids["graveyard"]:
                return True, True, (0, {})
            # Can move from hand/battlefield/library to graveyard unless all exiled
            can_reach = (
                gid in zone_gids["hand"]
                or gid in zone_gids["battlefield"]
                or gid in zone_gids["command"]
                or lib_possible
            )
            return False, can_reach, (0, {})
        if req_zone == "exile":
            if gid in zone_gids["exile"]:
                return True, True, (0, {})
            can_reach = gid in zone_gids["hand"] or gid in zone_gids["battlefield"] or lib_possible
            return False, can_reach, (0, {})
        if req_zone == "library":
            return in_lib, in_lib, (0, {})
        # battlefield / hand / command:
        if gid in zone_gids["hand"]:
            return True, True, (cost_g, cost_c)
        if gid in zone_gids["command"]:
            return True, True, (cost_g, cost_c)
        if gid in recoverable_gy_gids or gid in playable_exile_gids:
            return True, True, (cost_g, cost_c)
        # Missing from castable/play zones right now
        return False, bool(lib_possible), (cost_g, cost_c)

    live: list[dict[str, Any]] = []
    one_step: list[dict[str, Any]] = []
    out_of_reach: list[dict[str, Any]] = []

    for entry in entry_list:
        missing_gids: list[int] = []
        dead_gids: list[int] = []
        present_gids: list[int] = []
        piece_deploy_generic = 0
        piece_deploy_colors: dict[str, int] = {}

        for gid, req_zone in entry.pieces:
            present, reachable, (dg, dc) = _piece_present(gid, req_zone)
            if present:
                present_gids.append(gid)
                piece_deploy_generic += dg
                for c, amt in dc.items():
                    piece_deploy_colors[c] = piece_deploy_colors.get(c, 0) + amt
            else:
                missing_gids.append(gid)
                if not reachable:
                    dead_gids.append(gid)

        if entry.mana_generic > 0 or entry.mana_colors:
            # explicit activation / combo mana requirement applies even when all pieces are in play;
            # when pieces must still be deployed from hand, include deployment mana too.
            req_gen = entry.mana_generic + piece_deploy_generic
            req_cols = dict(entry.mana_colors)
            for c, amt in piece_deploy_colors.items():
                req_cols[c] = req_cols.get(c, 0) + amt
        else:
            req_gen = piece_deploy_generic
            req_cols = piece_deploy_colors

        names = [_name_for(entry, gid) for gid, _ in entry.pieces]

        if dead_gids:
            out_of_reach.append(
                {
                    "id": entry.id,
                    "kind": entry.kind,
                    "pieces": [gid for gid, _ in entry.pieces],
                    "names": names,
                    "dead_grp_ids": dead_gids,
                    "dead_names": [_name_for(entry, gid) for gid in dead_gids],
                    "reason": "piece_gone",
                    "outcome": entry.outcome,
                }
            )
            continue

        if not missing_gids:
            affordable, mana_shortfall = _can_pay(req_gen, req_cols, avail_total, avail_pool)
            if affordable and turn_num >= entry.first_turn:
                live.append(
                    {
                        "id": entry.id,
                        "kind": entry.kind,
                        "pieces": [gid for gid, _ in entry.pieces],
                        "names": names,
                        "outcome": entry.outcome,
                        "stops": entry.stops,
                        "mana_needed": max(0, req_gen + sum(req_cols.values())),
                    }
                )
            elif mana_shortfall <= 3 or turn_num < entry.first_turn:
                one_step.append(
                    {
                        "id": entry.id,
                        "kind": entry.kind,
                        "pieces": [gid for gid, _ in entry.pieces],
                        "names": names,
                        "missing_grp_ids": [],
                        "missing_names": [],
                        "in_library": False,
                        "copies_in_library": 0,
                        "mana_shortfall": max(1, mana_shortfall) if not affordable else 0,
                        "turn_wait": max(0, entry.first_turn - turn_num),
                        "outcome": entry.outcome,
                    }
                )
        elif len(missing_gids) == 1:
            mgid = missing_gids[0]
            copies = int(remaining_lib.get(mgid, 0))
            one_step.append(
                {
                    "id": entry.id,
                    "kind": entry.kind,
                    "pieces": [gid for gid, _ in entry.pieces],
                    "names": names,
                    "missing_grp_ids": [mgid],
                    "missing_names": [_name_for(entry, mgid)],
                    "in_library": copies > 0,
                    "copies_in_library": copies,
                    "mana_shortfall": 0,
                    "turn_wait": max(0, entry.first_turn - turn_num),
                    "outcome": entry.outcome,
                }
            )

    return SynergySnapshot(
        live=tuple(live),
        one_step_away=tuple(one_step),
        out_of_reach=tuple(out_of_reach),
    )


def tutor_missing_piece_hints(
    snapshot: SynergySnapshot,
    state: dict[str, Any],
    *,
    request_type: str | None = None,
) -> list[str]:
    """Return tutor / search / selection / discard hints for missing 1-step synergy pieces."""
    if not snapshot.one_step_away and not snapshot.live:
        return []
    context = state.get("decision_context") or {}
    text = " ".join(
        str(v or "")
        for v in (
            request_type,
            context.get("type"),
            context.get("context"),
            state.get("pending_decision"),
            state.get("_bridge_request_type"),
        )
    )
    if not _TUTOR_WINDOW_RE.search(text):
        return []
    hints: list[str] = []
    for item in snapshot.one_step_away[:4]:
        missing = item.get("missing_names") or ()
        if missing:
            copies = int(item.get("copies_in_library") or 0)
            where = f"{copies} copy/copies in library" if copies > 0 else "need mana/source"
            held = [n for n in item.get("names", ()) if n not in missing]
            hints.append(
                f"Missing synergy piece for {' + '.join(held)}: {', '.join(missing)} "
                f"({where}) -> {item.get('outcome', '')}"
            )
    if re.search(r"\b(?:discard|mulligan|bottom)\b", text, re.I):
        protect = sorted(
            {
                name
                for item in (*snapshot.live, *snapshot.one_step_away)
                for name in item.get("names", ())
                if name not in (item.get("missing_names") or ())
            }
        )
        if protect:
            hints.append("Protect held synergy piece(s) from discard/bottoming: " + ", ".join(protect[:6]))
    return hints


def player_synergy_facts(coach: Any, state: dict[str, Any]) -> dict[str, Any] | None:
    """Build privacy-scoped live / 1-step synergy facts for commentary when allowed."""
    if coach is None or not isinstance(state, dict):
        return None
    try:
        playbook = None
        for holder in (coach, getattr(coach, "_coach", None)):
            if holder is None:
                continue
            raw_pb = getattr(holder, "_deck_playbook", None)
            cand = raw_pb() if callable(raw_pb) else raw_pb
            if cand is not None and isinstance(getattr(cand, "data", cand), dict):
                playbook = cand
                break
        if playbook is None:
            return None
        raw_cat = getattr(playbook, "catalog", None)
        cat = raw_cat if isinstance(raw_cat, dict) else None
        entries = entries_from_playbook(playbook, deck_cards=state.get("deck_cards") or ())
        if not entries:
            return None
        snap = evaluate_synergies(entries, state, catalog=cat)
    except Exception:
        return None
    if not snap.live and not snap.one_step_away:
        return None
    return {
        "source": "local_study_synergy_tracker",
        "live": [
            {"id": item["id"], "kind": item["kind"], "names": list(item["names"]), "outcome": item["outcome"]}
            for item in snap.live[:4]
        ],
        "one_step_away": [
            {
                "id": item["id"],
                "kind": item["kind"],
                "names": list(item["names"]),
                "missing": list(item["missing_names"]),
                "in_library": bool(item["in_library"]),
                "copies_in_library": int(item["copies_in_library"]),
            }
            for item in snap.one_step_away[:4]
        ],
    }
