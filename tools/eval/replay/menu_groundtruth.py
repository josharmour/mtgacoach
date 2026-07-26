"""Ground-truth extractor: REAL MTGA legal-action menus at start of own Main 1.

Purpose
-------
This module exists for one measurement: *how close is a legal-action menu
synthesised from 17lands `replay_data` to a menu MTGA actually presented?*
A nearly-right menu is worse than no menu — it teaches a model to pick
actions that do not exist — so the fidelity gap has to be measured against
real menus before any training run.

The real menus come from MTGA's own `.rply` TimedReplay files, which record
every `GREMessageType_ActionsAvailableReq` verbatim, including
`actionsAvailableReq.actions[]` (THE MENU) and the player's answer via a
`performActionResp` joined on `respId == msgId`.

Scope: start-of-own-Main-1 only
------------------------------
17lands `replay_data` is **turn granular**: it records what a player had and
did during turn N, but not the order of decisions inside the turn and not
which mana was tapped when. The only decision point a turn-granular source
can honestly reconstruct is therefore the *first* priority window of the
player's own precombat main phase:

  * all permanents untapped during the untap step, so available mana is a
    function of lands-in-play alone (no tapped/untapped ambiguity),
  * nothing has been cast yet this turn, so hand == start-of-turn hand,
  * the land drop is still available.

Every other decision point (Main 2, combat, opponent's turn, second and
later priority windows in Main 1) depends on within-turn ordering that
17lands does not record. Those are emitted only when ``--all-decisions`` is
passed, and are excluded from the headline corpus.

Output
------
One JSONL record per selected decision. Each record carries:

  * provenance      — replay file, decision index, GRE msgId, game format
  * timing          — GRE global turn number, the player's own turn index,
                      phase, main-1 priority-window ordinal
  * ``real_menu``   — the verbatim menu, normalised: one entry per action
                      with actionType, grpId, resolved card name, mana cost
  * ``real_menu_key``        — comparable set of "ActionType:grpId" strings
  * ``real_menu_key_spell``  — same, minus per-land mana taps / float-mana,
                      which a turn-granular source can never reconstruct
  * ``real_pick``   — index into ``real_menu`` + actionType/grpId/name
  * ``state``       — the reconstructable fields a 17lands synthesiser would
                      have to reproduce: hand grpIds, lands in play, creatures
                      in play, other permanents, life totals, tapped counts

Usage
-----
    python -m tools.eval.replay.menu_groundtruth \
        --replay-dir /path/to/StreamingAssets/Tests \
        --out tools/eval/data/replay_menu_groundtruth.jsonl

CPU only. No network beyond the card database's own local caches.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from arenamcp.card_db import require_card_database  # noqa: E402

from .decisions import pair_request_response  # noqa: E402
from .reader import ReplayMessage, parse_replay_path  # noqa: E402
from .state import GameStateSnapshot, _apply_state_message  # noqa: E402

# Actions that exist per-land / per-mana-source. A turn-granular source can
# never reconstruct these individually (it does not know which land was
# tapped for what), so they are excluded from the "spell level" comparison
# key. They are still recorded verbatim in ``real_menu``.
MANA_LEVEL_ACTION_TYPES = frozenset(
    {
        "ActionType_Activate_Mana",
        "ActionType_FloatMana",
    }
)

DEFAULT_OUT = REPO / "tools" / "eval" / "data" / "replay_menu_groundtruth.jsonl"


# ---------------------------------------------------------------------------
# Card resolution
# ---------------------------------------------------------------------------

_CARD_DB = None
_CARD_CACHE: dict[int, dict] = {}


def _cdb():
    """The card database, **fully loaded**, or an exception.

    This corpus is the input every downstream play-decision corpus is built
    from, so a card fact that is missing here is missing everywhere. It used
    to call ``get_card_database()``, whose MTGJSON source loads in a
    background daemon thread — records extracted in the first seconds got
    empty type lines, and a permanent with no type line yields no mana. Fail
    closed instead: an absent database is an error, never a silent downgrade.
    """
    global _CARD_DB
    if _CARD_DB is None:
        _CARD_DB = require_card_database()
    return _CARD_DB


def card_facts(grp_id: Optional[int]) -> dict:
    """Resolve a grpId to {name, mana_cost, cmc, type_line} via the repo card DB.

    Cached per process; unresolvable ids come back with a ``#<id>`` name so
    downstream analysis can count them rather than crash.
    """
    if grp_id is None:
        return {"name": None, "mana_cost": "", "cmc": None, "type_line": ""}
    gid = int(grp_id)
    hit = _CARD_CACHE.get(gid)
    if hit is not None:
        return hit
    info = None
    try:
        info = _cdb().get_card_by_arena_id(gid)
    except Exception:  # noqa: BLE001 - a DB miss must not abort the walk
        info = None
    if info is None:
        out = {"name": f"#{gid}", "mana_cost": "", "cmc": None, "type_line": "", "resolved": False}
    else:
        out = {
            "name": getattr(info, "name", None) or f"#{gid}",
            "mana_cost": getattr(info, "mana_cost", "") or "",
            "cmc": getattr(info, "cmc", None),
            "type_line": getattr(info, "type_line", "") or "",
            "resolved": True,
        }
    _CARD_CACHE[gid] = out
    return out


# Colour-symbol rendering for the GRE's structured manaCost list. Hybrid
# entries carry more than one colour; we join them with '/' inside one brace
# so `{W/U}` survives the round trip.
_COLOR_SHORT = {
    "ManaColor_White": "W",
    "ManaColor_Blue": "U",
    "ManaColor_Black": "B",
    "ManaColor_Red": "R",
    "ManaColor_Green": "G",
    "ManaColor_Colorless": "C",
    "ManaColor_Generic": None,
}


def format_gre_mana_cost(cost: list[dict]) -> str:
    """Render an `actionsAvailableReq` manaCost list as `{1}{W}` style text."""
    if not cost:
        return ""
    parts: list[str] = []
    for entry in cost:
        n = int(entry.get("count") or 0)
        colors = entry.get("color") or []
        symbols = [_COLOR_SHORT.get(c) for c in colors]
        symbols = [s for s in symbols if s]
        if not symbols:
            if n > 0:
                parts.append("{" + str(n) + "}")
            continue
        sym = "/".join(symbols)
        parts.extend(["{" + sym + "}"] * n)
    return "".join(parts)


def gre_mana_value(cost: list[dict]) -> int:
    """Total pip count of a GRE manaCost list (generic counts as its number)."""
    return sum(int(e.get("count") or 0) for e in (cost or []))


# ---------------------------------------------------------------------------
# Replay walking
# ---------------------------------------------------------------------------


def detect_local_seat(messages: list[ReplayMessage]) -> Optional[int]:
    """Which seat is the recording player?

    `GREMessageType_ConnectResp` is addressed to exactly one seat — the
    client that recorded the replay. Measured across the 104-file corpus
    this is seat 1 for 55 files and seat 2 for 49, so it must be detected
    per replay rather than assumed (``state.py`` defaults to 2).

    Falls back to the modal `systemSeatIds` of single-seat request messages.
    """
    for m in messages:
        if m.msg_type == "GREMessageType_ConnectResp":
            ids = m.payload.get("systemSeatIds") or []
            if len(ids) == 1:
                return int(ids[0])
    counts: Counter[int] = Counter()
    for m in messages:
        if m.direction != "IN" or not m.is_request:
            continue
        ids = m.payload.get("systemSeatIds") or []
        if len(ids) == 1:
            counts[int(ids[0])] += 1
    return counts.most_common(1)[0][0] if counts else None


def _iter_state_messages(m: ReplayMessage):
    """Yield every gameStateMessage carried by an IN message."""
    if m.msg_type == "GREMessageType_GameStateMessage":
        gsm = m.payload.get("gameStateMessage")
        if gsm:
            yield gsm
    elif m.msg_type == "GREMessageType_QueuedGameStateMessage":
        qg = m.payload.get("queuedGameStateMessage") or {}
        for gsm in qg.get("gameStateMessages") or []:
            yield gsm


@dataclass
class WalkStats:
    """Counters describing one replay's contribution to the corpus."""

    file: str = ""
    local_seat: Optional[int] = None
    super_format: str = ""
    variant: str = ""
    actions_available_total: int = 0
    actions_available_local: int = 0
    own_main1_windows: int = 0
    start_of_main1: int = 0
    start_of_main1_with_pick: int = 0
    unresolved_pick: int = 0
    no_response: int = 0
    errors: list[str] = field(default_factory=list)


def _battlefield_split(snap: GameStateSnapshot, seat: int) -> dict:
    """Split a seat's battlefield into lands / creatures / other.

    Uses the GRE object's own ``cardTypes`` (authoritative: it reflects
    animated lands, transformed permanents, tokens) and falls back to the
    card database's type line when the object omits cardTypes.
    """
    lands: list[dict] = []
    creatures: list[dict] = []
    other: list[dict] = []
    for obj in snap.battlefield(seat):
        gid = obj.get("grpId")
        ctypes = obj.get("cardTypes") or []
        if not ctypes:
            tl = card_facts(gid)["type_line"].lower()
            ctypes = []
            if "land" in tl:
                ctypes.append("CardType_Land")
            if "creature" in tl:
                ctypes.append("CardType_Creature")
        entry = {
            "grp_id": int(gid) if gid is not None else None,
            "instance_id": int(obj["instanceId"]) if obj.get("instanceId") is not None else None,
            "name": card_facts(gid)["name"],
            "is_tapped": bool(obj.get("isTapped")),
            "card_types": list(ctypes),
        }
        if "CardType_Land" in ctypes:
            lands.append(entry)
        elif "CardType_Creature" in ctypes:
            creatures.append(entry)
        else:
            other.append(entry)
    return {"lands": lands, "creatures": creatures, "other": other}


def normalize_menu(request: ReplayMessage) -> tuple[list[dict], list[dict]]:
    """Return (active_menu, inactive_menu) as normalised action dicts.

    ``active_menu`` is ``actionsAvailableReq.actions[]`` — the actions MTGA
    presented as takeable. ``inactive_menu`` is ``inactiveActions[]``, the
    actions MTGA knew about but ruled out (land drop already used, aura with
    no legal target, ...). Both are kept: a synthesiser that emits an
    inactive action has produced an illegal menu entry, and we want that
    distinguishable from an entry MTGA never mentioned at all.
    """
    aa = request.payload.get("actionsAvailableReq") or {}

    def _norm(a: dict) -> dict:
        gid = a.get("grpId")
        cost = a.get("manaCost") or []
        facts = card_facts(gid)
        return {
            "action_type": a.get("actionType") or "?",
            "grp_id": int(gid) if gid is not None else None,
            "instance_id": int(a["instanceId"]) if a.get("instanceId") is not None else None,
            "ability_grp_id": a.get("abilityGrpId"),
            "name": facts["name"],
            "type_line": facts["type_line"],
            "mana_cost": format_gre_mana_cost(cost) or facts["mana_cost"],
            "mana_cost_from_gre": bool(cost),
            "mana_value": gre_mana_value(cost) if cost else facts["cmc"],
        }

    active = [_norm(a) for a in (aa.get("actions") or [])]
    inactive = [_norm(a) for a in (aa.get("inactiveActions") or [])]
    return active, inactive


def menu_key(menu: list[dict], *, spell_level: bool = False) -> list[str]:
    """Order-independent comparable form: sorted unique ``Type:grpId`` strings.

    Ordering and numbering differ between MTGA's menu and any synthesised
    one, so comparison has to be set-based. ``spell_level=True`` drops
    per-land mana taps and float-mana, which no turn-granular source can
    reconstruct.
    """
    keys = set()
    for e in menu:
        atype = e["action_type"]
        if spell_level and atype in MANA_LEVEL_ACTION_TYPES:
            continue
        short = atype.replace("ActionType_", "")
        gid = e["grp_id"]
        keys.add(f"{short}:{gid}" if gid is not None else short)
    return sorted(keys)


def resolve_pick(request: ReplayMessage, response: Optional[ReplayMessage], active: list[dict]) -> dict:
    """Map the player's `performActionResp` back to an index in the menu.

    Matching is by (actionType, grpId, instanceId) — the triple that
    uniquely identifies a menu row, since the same card can appear twice
    (two copies of a basic land in hand each get their own instanceId).
    Falls back to (actionType, grpId) then actionType alone.
    """
    if response is None:
        return {"status": "no_response"}
    par = response.payload.get("performActionResp") or {}
    actions = par.get("actions") or []
    if not actions:
        # A bare PerformActionResp with no actions is MTGA's auto-pass ack.
        idx = next((i for i, e in enumerate(active) if e["action_type"] == "ActionType_Pass"), None)
        return {
            "status": "implicit_pass",
            "menu_index": idx,
            "action_type": "ActionType_Pass",
            "grp_id": None,
            "instance_id": None,
            "name": None,
            "auto_pass_priority": par.get("autoPassPriority"),
            "n_actions_submitted": 0,
        }
    primary = actions[0]
    atype = primary.get("actionType")
    gid = primary.get("grpId")
    iid = primary.get("instanceId")
    gid_i = int(gid) if gid is not None else None
    iid_i = int(iid) if iid is not None else None

    idx = next(
        (
            i
            for i, e in enumerate(active)
            if e["action_type"] == atype and e["grp_id"] == gid_i and e["instance_id"] == iid_i
        ),
        None,
    )
    match = "exact"
    if idx is None:
        idx = next(
            (i for i, e in enumerate(active) if e["action_type"] == atype and e["grp_id"] == gid_i), None
        )
        match = "by_grp_id"
    if idx is None:
        idx = next((i for i, e in enumerate(active) if e["action_type"] == atype), None)
        match = "by_action_type"
    if idx is None:
        match = "not_in_menu"
    return {
        "status": "ok" if idx is not None else "not_in_menu",
        "match": match,
        "menu_index": idx,
        "action_type": atype,
        "grp_id": gid_i,
        "instance_id": iid_i,
        "name": card_facts(gid_i)["name"] if gid_i is not None else None,
        "auto_pass_priority": par.get("autoPassPriority"),
        "n_actions_submitted": len(actions),
    }


def extract_replay(path: Path, *, all_decisions: bool = False) -> tuple[list[dict], WalkStats]:
    """Walk one `.rply` and emit ground-truth records + per-file stats."""
    stats = WalkStats(file=path.name)
    try:
        meta, messages = parse_replay_path(path)
    except Exception as exc:  # noqa: BLE001
        stats.errors.append(f"parse: {exc}")
        return [], stats

    seat = detect_local_seat(messages)
    stats.local_seat = seat
    if seat is None:
        stats.errors.append("could not determine local seat")
        return [], stats
    opp_seat = 1 if seat == 2 else 2

    by_resp = pair_request_response(messages)
    snap = GameStateSnapshot(local_seat_id=seat)
    step = ""
    game_number: Optional[int] = None

    # (turn_number -> how many own-Main-1 priority windows seen so far). Reset
    # on a new game so a Bo3 replay would not collide (this corpus is 1
    # game/file, but the guard is cheap).
    main1_windows: Counter[int] = Counter()
    own_turn_seen: list[int] = []  # ordered distinct turn numbers of own turns

    records: list[dict] = []
    decision_index = -1

    for m in messages:
        if m.direction != "IN":
            continue

        for gsm in _iter_state_messages(m):
            gi = gsm.get("gameInfo")
            if gi:
                if gi.get("superFormat"):
                    stats.super_format = gi["superFormat"]
                if gi.get("variant"):
                    stats.variant = gi["variant"]
                gn = gi.get("gameNumber")
                if gn is not None and gn != game_number:
                    game_number = gn
                    main1_windows.clear()
                    own_turn_seen.clear()
            _apply_state_message(snap, gsm)
            ti = gsm.get("turnInfo")
            if ti and ("step" in ti or "phase" in ti):
                step = ti.get("step") or ""

        if m.msg_type != "GREMessageType_ActionsAvailableReq":
            continue

        decision_index += 1
        stats.actions_available_total += 1

        addressed = [int(x) for x in (m.payload.get("systemSeatIds") or [])]
        if addressed and seat not in addressed:
            continue
        stats.actions_available_local += 1

        is_own_turn = snap.active_player == seat
        is_main1 = snap.phase == "Phase_Main1"

        if is_own_turn and snap.turn_number not in own_turn_seen:
            own_turn_seen.append(snap.turn_number)

        window_ordinal = None
        if is_own_turn and is_main1:
            main1_windows[snap.turn_number] += 1
            window_ordinal = main1_windows[snap.turn_number]
            stats.own_main1_windows += 1

        is_start_of_main1 = window_ordinal == 1
        if is_start_of_main1:
            stats.start_of_main1 += 1
        if not is_start_of_main1 and not all_decisions:
            continue

        active, inactive = normalize_menu(m)
        resp = by_resp.get(m.msg_id) if m.msg_id is not None else None
        pick = resolve_pick(m, resp, active)
        if is_start_of_main1:
            if pick.get("status") == "no_response":
                stats.no_response += 1
            elif pick.get("menu_index") is None:
                stats.unresolved_pick += 1
            else:
                stats.start_of_main1_with_pick += 1

        you = _battlefield_split(snap, seat)
        opp = _battlefield_split(snap, opp_seat)
        hand_objs = snap.hand(seat)
        hand = []
        for o in hand_objs:
            gid = o.get("grpId")
            facts = card_facts(gid)
            hand.append(
                {
                    "grp_id": int(gid) if gid is not None else None,
                    "instance_id": int(o["instanceId"]) if o.get("instanceId") is not None else None,
                    "name": facts["name"],
                    "mana_cost": facts["mana_cost"],
                    "cmc": facts["cmc"],
                    "type_line": facts["type_line"],
                }
            )

        player_turn_index = (
            own_turn_seen.index(snap.turn_number) + 1 if snap.turn_number in own_turn_seen else None
        )

        rec = {
            # provenance
            "replay_file": path.name,
            "decision_index": decision_index,
            "msg_id": m.msg_id,
            "game_state_id": m.payload.get("gameStateId"),
            "game_number": game_number,
            "super_format": stats.super_format,
            "variant": stats.variant,
            "local_seat": seat,
            "local_screen_name": meta.local_screen_name,
            # timing
            "turn_number": snap.turn_number,  # GRE global turn counter
            "player_turn_index": player_turn_index,  # 1-based own-turn index
            "phase": snap.phase,
            "step": step,
            "active_player": snap.active_player,
            "priority_player": snap.priority_player,
            "is_own_turn": is_own_turn,
            "is_start_of_main1": bool(is_start_of_main1),
            "main1_window_ordinal": window_ordinal,
            # THE REAL MENU
            "real_menu": active,
            "real_menu_size": len(active),
            "real_menu_key": menu_key(active),
            "real_menu_key_spell": menu_key(active, spell_level=True),
            "real_menu_inactive": inactive,
            "real_menu_inactive_key": menu_key(inactive),
            # THE REAL PICK
            "real_pick": pick,
            # reconstructable state (what a 17lands synthesiser must reproduce)
            "state": {
                "life": {"you": snap.life(seat), "opp": snap.life(opp_seat)},
                "hand": hand,
                "hand_grp_ids": [c["grp_id"] for c in hand],
                "hand_size": len(hand),
                "lands_in_play": you["lands"],
                "lands_in_play_grp_ids": [c["grp_id"] for c in you["lands"]],
                "lands_tapped": sum(1 for c in you["lands"] if c["is_tapped"]),
                "creatures_in_play": you["creatures"],
                "creatures_in_play_grp_ids": [c["grp_id"] for c in you["creatures"]],
                "other_permanents": you["other"],
                "other_permanents_grp_ids": [c["grp_id"] for c in you["other"]],
                "opp_lands_grp_ids": [c["grp_id"] for c in opp["lands"]],
                "opp_creatures_grp_ids": [c["grp_id"] for c in opp["creatures"]],
                "opp_other_grp_ids": [c["grp_id"] for c in opp["other"]],
                "opp_hand_size": snap.hand_size(opp_seat),
            },
        }
        records.append(rec)

    return records, stats


# ---------------------------------------------------------------------------
# Corpus driver
# ---------------------------------------------------------------------------


def summarize(all_stats: list[WalkStats], records: list[dict]) -> dict:
    """Aggregate corpus-level counts for the spike's headline numbers."""
    aa_total = sum(s.actions_available_total for s in all_stats)
    aa_local = sum(s.actions_available_local for s in all_stats)
    own_main1 = sum(s.own_main1_windows for s in all_stats)
    start = sum(s.start_of_main1 for s in all_stats)
    with_pick = sum(s.start_of_main1_with_pick for s in all_stats)

    start_recs = [r for r in records if r["is_start_of_main1"]]
    menu_sizes = sorted(r["real_menu_size"] for r in start_recs)
    spell_sizes = sorted(len(r["real_menu_key_spell"]) for r in start_recs)

    def _median(xs):
        return xs[len(xs) // 2] if xs else None

    pick_types = Counter(r["real_pick"].get("action_type") for r in start_recs)
    pick_status = Counter(r["real_pick"].get("status") for r in start_recs)
    fmt_files = Counter((s.super_format, s.variant) for s in all_stats)
    seat_files = Counter(s.local_seat for s in all_stats)

    unresolved_names = 0
    total_named = 0
    for r in start_recs:
        for e in r["real_menu"]:
            if e["grp_id"] is None:
                continue
            total_named += 1
            if str(e["name"]).startswith("#"):
                unresolved_names += 1

    tapped_at_start = Counter(r["state"]["lands_tapped"] for r in start_recs)

    return {
        "replays_parsed": len(all_stats),
        "replays_with_errors": sum(1 for s in all_stats if s.errors),
        "format_by_file": {f"{k[0]}/{k[1]}": v for k, v in fmt_files.most_common()},
        "local_seat_by_file": {str(k): v for k, v in seat_files.most_common()},
        "actions_available_total": aa_total,
        "actions_available_addressed_to_local": aa_local,
        "own_main1_priority_windows": own_main1,
        "start_of_own_main1_decisions": start,
        "start_of_own_main1_with_resolved_pick": with_pick,
        "start_of_own_main1_no_response": sum(s.no_response for s in all_stats),
        "start_of_own_main1_pick_not_in_menu": sum(s.unresolved_pick for s in all_stats),
        "fraction_of_all_actions_available": round(start / aa_total, 4) if aa_total else None,
        "fraction_of_local_actions_available": round(start / aa_local, 4) if aa_local else None,
        "menu_size": {
            "min": menu_sizes[0] if menu_sizes else None,
            "median": _median(menu_sizes),
            "max": menu_sizes[-1] if menu_sizes else None,
            "mean": round(sum(menu_sizes) / len(menu_sizes), 2) if menu_sizes else None,
        },
        "menu_size_spell_level": {
            "min": spell_sizes[0] if spell_sizes else None,
            "median": _median(spell_sizes),
            "max": spell_sizes[-1] if spell_sizes else None,
            "mean": round(sum(spell_sizes) / len(spell_sizes), 2) if spell_sizes else None,
        },
        "pick_action_types": dict(pick_types.most_common()),
        "pick_status": dict(pick_status.most_common()),
        "card_name_resolution": {
            "menu_entries_with_grp_id": total_named,
            "unresolved": unresolved_names,
            "resolved_pct": round(100.0 * (total_named - unresolved_names) / total_named, 2)
            if total_named
            else None,
        },
        "lands_tapped_at_start_of_main1": {str(k): v for k, v in sorted(tapped_at_start.items())},
    }


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Extract REAL MTGA legal-action menus at start of own Main 1")
    p.add_argument("--replay-dir", type=Path, required=True, help="Directory containing .rply files")
    p.add_argument(
        "--out", type=Path, default=DEFAULT_OUT, help=f"Output JSONL path (default: {DEFAULT_OUT})"
    )
    p.add_argument("--summary-out", type=Path, default=None, help="Optional JSON path for the summary block")
    p.add_argument(
        "--all-decisions",
        action="store_true",
        help="Emit every ActionsAvailable decision, not just start-of-own-Main-1",
    )
    p.add_argument("--limit", type=int, default=None, help="Only process the first N replays")
    args = p.parse_args(argv)

    files = sorted(args.replay_dir.glob("*.rply"))
    if args.limit:
        files = files[: args.limit]
    if not files:
        print(f"no .rply files under {args.replay_dir}", file=sys.stderr)
        return 2

    all_records: list[dict] = []
    all_stats: list[WalkStats] = []
    for i, f in enumerate(files, 1):
        recs, stats = extract_replay(f, all_decisions=args.all_decisions)
        all_records.extend(recs)
        all_stats.append(stats)
        if i % 10 == 0 or i == len(files):
            print(
                f"  [{i}/{len(files)}] {f.name}: "
                f"{stats.start_of_main1} start-of-main1 / "
                f"{stats.actions_available_total} ActionsAvailable",
                file=sys.stderr,
            )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        for r in all_records:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")

    summary = summarize(all_stats, all_records)
    summary["output_jsonl"] = str(args.out)
    summary["records_written"] = len(all_records)
    summary["emitted_all_decisions"] = bool(args.all_decisions)

    print("\n" + json.dumps(summary, indent=2))
    if args.summary_out:
        args.summary_out.parent.mkdir(parents=True, exist_ok=True)
        args.summary_out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"\nsummary -> {args.summary_out}")
    print(f"\nJSONL -> {args.out}  ({len(all_records)} records)")

    errored = [s for s in all_stats if s.errors]
    if errored:
        print(f"\n{len(errored)} replay(s) had errors:", file=sys.stderr)
        for s in errored[:10]:
            print(f"  {s.file}: {s.errors}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
