#!/usr/bin/env python3
"""fleet_review.py — W1: mine shared match bundles for recurring misplays.

Reads the shared match-bundle corpus (default ~/.arenamcp/match_bundles,
overridable with --dir) and produces the W1 rankings:

  1. coverage: matches per (format, local archetype) with win rates and the
     M1 metric aggregates (decision answers, line search, inference per call
     class, plans, mulligan keeps, decision prompt size). Games fold per
     match_id like M1: the LAST game of a match carries the match result,
     so wins are counted once per match, never per game.
  2. cards most often left unevaluated (the line-search unmodelled counter,
     aggregated per archetype and overall)
  3. decision types with the most watchdog defaults and fallbacks:
     hop-limit hand-offs from the coach log, and packet-recorded fallbacks
     (``trace.fallback``) grouped by ``pending_decision.request_type``
  4. other code-layer flags (typed-decision set-asides, mulligan guards,
     plan-validation rejections, kicker guard, backend error sentinels)
  5. win rate by opposing commander — identified generically from card
     facts (a legendary Creature or Planeswalker in the opponent's
     end-of-game command zone); anything else (Dungeons, emblems) is not a
     commander and reports "unknown"
  6. early-end suspects: games that ended by turn EARLY_TURN_MAX with both
     players near starting life and no executed concede, no ERROR-level
     coach-log line and no "timed out" line (those explain an early end).
     Both seats must be recorded in final_state (local_seat_id and
     opponent_seat_id, each with a life total); with only one seat known
     the game cannot qualify.

The per-bundle metric definitions are the same as tools/upgrade_metrics.py
(M1). They are reimplemented here rather than imported so this tool runs on
any checkout, including ones that do not carry M1's file; the W1 report
documents the equivalence check.

Usage:
    python tools/fleet_review.py [--dir DIR] [--since YYYY-MM-DD] [--json]
"""

from __future__ import annotations

import argparse
import ast
import collections
import gzip
import json
import os
import re
import sys
from datetime import datetime, timezone

DEFAULT_BUNDLES_DIR = os.path.expanduser("~/.arenamcp/match_bundles")

# ---------------------------------------------------------------------------
# Tunables (rationale documented in fable-november-reports/W1.md)
# ---------------------------------------------------------------------------

# A game ending at or before this turn number counts as "ended early".
EARLY_TURN_MAX = 8

# "Near starting life": both players' final life totals must be at least
# this much below their format's starting life.
NEAR_STARTING_LIFE_MARGIN = 10

# Arena starting life by format family (verified against the corpus:
# every Ladder seat peaks at 20, every Brawl Ladder seat at 25).
STARTING_LIFE_DEFAULT = 20
STARTING_LIFE_BRAWL = 25

# Coach-log substrings that mean a concession was actually executed.
# The "[CONCEDE] loss confidence ..." musings are advisory only and do
# NOT count — see fable-november-reports/W1.md for the distinction.
CONCEDE_EXECUTED_MARKERS = (
    "verified before conceding",
    "[CONCEDE] countdown",
    "offer shown",
)

# Any ERROR-level coach-log line (e.g. the backend error sentinel, a proxy
# "API error: Request timed out.") marks an abnormal ending path, so the
# game is not an unexplained early end.
ERROR_LEVEL_RE = re.compile(r"\| ERROR\s+\|")

# "... timed out ..." (with a space) in the coach log — an LLM/API timeout —
# explains an early end. Routine lines such as "Auto-clearing stale decision
# ... timeout=15s" carry "timeout=" without a space and do not match.
TIMEOUT_RE = re.compile(r"\btimed out\b", re.IGNORECASE)

# Commander eligibility as card facts (the Brawl/Commander rule): a commander
# is a legendary Creature or Planeswalker. Keyed on card types only, so it
# works for every card — Dungeons (CardType_Dungeon) and emblems (no card
# types) are game objects, not commanders.
COMMANDER_CARD_TYPES = frozenset({"CardType_Creature", "CardType_Planeswalker"})
COMMANDER_SUPER_TYPES = frozenset({"SuperType_Legendary"})

# Code-layer flag patterns. Each pattern is independent; a line may match
# several. Group(1), when present, is a sub-category (the decision type a
# watchdog hand-off leaves behind, or keep-vs-mulligan).
FLAG_PATTERNS = (
    (
        "watchdog_hop_limit",
        re.compile(
            r"post-cast priority: hop limit \(\d+\) reached; "
            r"leaving (\S+) to the watchdog"
        ),
    ),
    ("model_pick_set_aside", re.compile(r"typed-decision fallback \(the model's pick was set aside\)")),
    ("controller_target_fallback", re.compile(r"plan_decision_options: controller-aware target fallback")),
    ("mulligan_guard", re.compile(r"Mulligan guard: (KEEP|MULLIGAN)")),
    ("gameplan_validation_rejected", re.compile(r"GamePlan validation \([^)]*\): .*rejected")),
    ("kicker_affordability_guard", re.compile(r"Kicker affordability guard")),
    ("backend_error_sentinel", re.compile(r"Backend returned error sentinel")),
)

DECK_PLAYBOOK_PREFIX_RE = re.compile(r"^DECK PLAYBOOK v\d+:\s*(.*)$")

# M1 metric line patterns (same definitions as tools/upgrade_metrics.py).
REQUEST_METRICS_RE = re.compile(r"Request metrics:?\s*(\{.*\})\s*$")
KEEP_LINE_RE = re.compile(r"Mulligan guard: KEEP")
LANDS_RE = re.compile(r"(\d+) lands?")
BIG_PROMPT_RE = re.compile(r"typed-decision: big prompt \((\d+) chars\)")
PLAN_FORMED_RE = re.compile(r"Game plan reform \(")
PATH_CORRECTED_RE = re.compile(r"GamePlan validation.*\bpath:.*corrected")

UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Bundle loading (same semantics as M1)
# ---------------------------------------------------------------------------


def load_bundle(path):
    """Load one bundle file; transparently handles gzip and plain JSON."""
    with open(path, "rb") as fh:
        magic = fh.read(2)
    opener = gzip.open if magic == b"\x1f\x8b" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def iter_bundle_paths(directory):
    """Sorted bundle paths (*.json and *.json.gz) under ``directory``."""
    names = sorted(n for n in os.listdir(directory) if n.endswith(".json") or n.endswith(".json.gz"))
    return [os.path.join(directory, n) for n in names]


def _bundle_day(bundle):
    """UTC calendar date (YYYY-MM-DD) of the bundle's ``started_at``."""
    started = bundle.get("started_at")
    if isinstance(started, (int, float)):
        return datetime.fromtimestamp(started, tz=timezone.utc).strftime("%Y-%m-%d")
    return str(started or "")[:10]


def is_match_bundle(bundle):
    """True when the file is a match bundle, not e.g. a live_context.json
    pointer (which carries only paths/offsets and no coach_log or packet)."""
    return "coach_log" in bundle or "packet" in bundle


def parse_request_metrics(line):
    """Return the metrics dict from a ``[PROXY] Request metrics:`` log line.

    The payload is a Python repr (single quotes), not JSON, so it is parsed
    with ``ast.literal_eval``. Returns None when the line does not carry a
    parsable payload.
    """
    m = REQUEST_METRICS_RE.search(line)
    if not m:
        return None
    try:
        payload = ast.literal_eval(m.group(1))
    except (ValueError, SyntaxError):
        return None
    return payload if isinstance(payload, dict) else None


def parse_keep_lands(line):
    """Return the number of lands stated in a mulligan-guard KEEP line.

    Lines that justify a keep qualitatively (e.g. "Mountains cast Laelia ...")
    state no count and yield None; those games are excluded from the per-land
    win-rate table.
    """
    if not KEEP_LINE_RE.search(line):
        return None
    m = LANDS_RE.search(line)
    return int(m.group(1)) if m else None


def unmodelled_card_name(value):
    """Reduce an unevaluated-option description to the card name.

    Real logged values look like ``"Laelia, the Blade Reforged (its triggered
    ability)"``; the parenthetical qualifier is stripped.
    """
    text = str(value).strip()
    if text.startswith("T: "):
        text = text[3:]
    return text.split(" (")[0].strip()


# ---------------------------------------------------------------------------
# Card-name lookup and deck identity
# ---------------------------------------------------------------------------


def _bootstrap_src_path():
    """Make arenamcp.card_db importable when running from tools/."""
    src = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "src"))
    if os.path.isdir(src) and src not in sys.path:
        sys.path.insert(0, src)


_CARD_DB = None


def _card_db():
    """Lazy card-name lookup; returns None when the DB is unavailable."""
    global _CARD_DB
    if _CARD_DB is None:
        try:
            _bootstrap_src_path()
            from arenamcp.card_db import get_card_database

            _CARD_DB = get_card_database()
        except Exception:
            _CARD_DB = False  # sentinel: tried and failed
    return _CARD_DB or None


def grp_id_to_name(grp_id):
    """Resolve an arena grp_id to a card name, or 'grp:<id>' as fallback."""
    db = _card_db()
    if db is not None:
        try:
            info = db.get_card_by_arena_id(grp_id)
            if info is not None and getattr(info, "name", None):
                return info.name
        except Exception:
            pass
    return "grp:%s" % grp_id


def archetype_label(bundle):
    """Local deck archetype from the playbook header line.

    Real form (bundle 00651f77): "DECK PLAYBOOK v2: Mono-red Laelia
    cascade/discover aggro-tempo: ..." -> "Mono-red Laelia
    cascade/discover aggro-tempo".
    """
    strategy = ((bundle.get("packet") or {}).get("deck_strategy")) or ""
    for line in strategy.splitlines():
        line = line.strip()
        if not line:
            continue
        m = DECK_PLAYBOOK_PREFIX_RE.match(line)
        text = m.group(1) if m else line
        return text.split(":")[0].strip() or UNKNOWN
    return UNKNOWN


def opponent_commanders(bundle):
    """Opponent commander names from their end-of-game command zone.

    A commander is identified generically from card facts: an object owned by
    the opponent whose card types include Creature or Planeswalker and whose
    super types include Legendary. Everything else in the command zone —
    Dungeons (real case: match 346239cd's grp_id 78770,
    card_types=[CardType_Dungeon]) and typeless emblem objects (grp_id 2 in
    matches 44bc39ca/988f05f1/d28cfaa9) — is not a commander; those games
    report ["unknown"].
    """
    final_state = bundle.get("final_state") or {}
    opp_seat = final_state.get("opponent_seat_id")
    zones = final_state.get("zones") or {}
    names = []
    for card in zones.get("command") or []:
        if not isinstance(card, dict):
            continue
        if card.get("owner_seat_id") != opp_seat:
            continue
        card_types = set(card.get("card_types") or [])
        super_types = set(card.get("super_types") or [])
        if not (card_types & COMMANDER_CARD_TYPES):
            continue
        if not (super_types & COMMANDER_SUPER_TYPES):
            continue
        grp_id = card.get("grp_id")
        if grp_id is None:
            continue
        names.append(grp_id_to_name(grp_id))
    return names or [UNKNOWN]


def starting_life_for(format_name):
    """Arena starting life for a format name (corpus-verified constants)."""
    if "brawl" in (format_name or "").lower():
        return STARTING_LIFE_BRAWL
    return STARTING_LIFE_DEFAULT


def _definite_concede(coach_lines):
    lowered = [line.lower() for line in coach_lines]
    return any(marker.lower() in line for line in lowered for marker in CONCEDE_EXECUTED_MARKERS)


# ---------------------------------------------------------------------------
# Per-bundle review
# ---------------------------------------------------------------------------


def summarize_m1(bundle):
    """This bundle's M1 metrics — same definitions as upgrade_metrics.py.

    Reimplemented (not imported) so fleet_review runs on any checkout; see
    the module docstring.
    """
    settings = bundle.get("settings_subset") or {}
    final_state = bundle.get("final_state") or {}
    coach_lines = ((bundle.get("coach_log") or {}).get("lines")) or []

    out = {
        "match_id": bundle.get("match_id"),
        "day": _bundle_day(bundle),
        "autopilot": bool(settings.get("autopilot_enabled")),
        # Match-level result: bundle["result"] is the match outcome string
        # ("win"/"loss"); final_state.last_game_result is the last game's.
        "result": bundle.get("result") or final_state.get("last_game_result"),
        # decision stats
        "answers": 0,
        "pass_answers": 0,
        "code_answers": 0,
        "single_option_menus": 0,
        "decisions_with_line_search": 0,
        "decisions_with_unmodelled": 0,
        "chosen_unmodelled": 0,
        "unmodelled_cards": collections.Counter(),
        # inference per call class: {class: {calls,input,cached,think,sec}}
        "call_classes": collections.defaultdict(
            lambda: {"calls": 0, "input": 0, "cached": 0, "think": 0, "sec": 0.0}
        ),
        # coach-log counters
        "plans_formed": 0,
        "path_corrected": 0,
        "big_prompts": [],
        # (lands_kept, result) pairs for parseable KEEP lines
        "keeps": [],
    }

    for line in coach_lines:
        metrics = parse_request_metrics(line)
        if metrics is not None:
            bucket = out["call_classes"][metrics.get("call_class")]
            bucket["calls"] += 1
            bucket["input"] += metrics.get("input_tokens") or 0
            bucket["cached"] += metrics.get("cached_input_tokens") or 0
            bucket["think"] += metrics.get("reasoning_tokens") or 0
            bucket["sec"] += (metrics.get("total_ms") or 0) / 1000.0
        if PLAN_FORMED_RE.search(line):
            out["plans_formed"] += 1
        if PATH_CORRECTED_RE.search(line):
            out["path_corrected"] += 1
        m = BIG_PROMPT_RE.search(line)
        if m:
            out["big_prompts"].append(int(m.group(1)))
        lands = parse_keep_lands(line)
        if lands is not None:
            out["keeps"].append((lands, out["result"]))

    for decision in (bundle.get("packet") or {}).get("decisions") or []:
        pending = decision.get("pending_decision") or {}
        options = pending.get("options") or []
        chosen = decision.get("chosen_options") or []
        trace = decision.get("trace") or {}

        if not chosen:
            continue  # no answer recorded (e.g. superseded by an executed action)
        out["answers"] += 1
        if len(options) == 1:
            out["single_option_menus"] += 1
        if chosen == ["pass"]:
            out["pass_answers"] += 1
        if "llm_usage" not in trace:
            out["code_answers"] += 1

        lines = trace.get("lines")
        if isinstance(lines, dict) and lines:
            out["decisions_with_line_search"] += 1
            unmodelled = lines.get("unmodelled") or {}
            if unmodelled:
                out["decisions_with_unmodelled"] += 1
                chosen_set = set(chosen)
                for key, value in unmodelled.items():
                    out["unmodelled_cards"][unmodelled_card_name(value)] += 1
                    if key in chosen_set:
                        out["chosen_unmodelled"] += 1

    return out


def fallback_tally(bundle):
    """Packet-recorded fallbacks grouped by decision type.

    Real case (match 0e07798d): a decision whose ``pending_decision.request_type``
    is "ActionsAvailable" and whose ``trace.fallback`` is "board_math".
    Returns a Counter keyed by ``(request_type, fallback)``.
    """
    tally = collections.Counter()
    for decision in (bundle.get("packet") or {}).get("decisions") or []:
        trace = decision.get("trace") or {}
        fallback = trace.get("fallback")
        if not fallback:
            continue
        pending = decision.get("pending_decision") or {}
        request_type = pending.get("request_type") or UNKNOWN
        tally[(request_type, fallback)] += 1
    return tally


def review_bundle(bundle):
    """Pure per-bundle review: identity, M1 metrics, flags, early-end.

    The tests exercise this directly with fixtures whose values come from
    real logged cases (match ids named in the test docstrings).
    """
    final_state = bundle.get("final_state") or {}
    coach_lines = ((bundle.get("coach_log") or {}).get("lines")) or []

    final_state = bundle.get("final_state") or {}
    coach_lines = ((bundle.get("coach_log") or {}).get("lines")) or []

    m1 = summarize_m1(bundle)

    players = final_state.get("players") or []
    lives = [p.get("life_total") for p in players]
    local_seat = final_state.get("local_seat_id")
    opp_seat = final_state.get("opponent_seat_id")
    lives_by_seat = {}
    for player in players:
        seat = player.get("seat_id")
        if seat is not None:
            lives_by_seat[seat] = player.get("life_total")
    # Both seats must be recorded before life totals can be judged: with
    # only one seat known there is no way to establish that BOTH players
    # were near starting life at the end.
    both_seats_recorded = (
        local_seat is not None
        and opp_seat is not None
        and local_seat in lives_by_seat
        and opp_seat in lives_by_seat
    )
    seat_lives = [lives_by_seat[local_seat], lives_by_seat[opp_seat]] if both_seats_recorded else []

    turn_number = (final_state.get("turn_info") or {}).get("turn_number")
    format_name = final_state.get("format_name")

    flags = collections.Counter()
    watchdog_types = collections.Counter()
    flag_examples = collections.defaultdict(list)
    match_id = bundle.get("match_id")

    for line in coach_lines:
        for category, pattern in FLAG_PATTERNS:
            m = pattern.search(line)
            if m is None:
                continue
            flags[category] += 1
            if category == "watchdog_hop_limit":
                watchdog_types[m.group(1)] += 1
            if match_id and len(flag_examples[category]) < 3:
                flag_examples[category].append(match_id)

    has_concede = _definite_concede(coach_lines)
    has_error_line = any(ERROR_LEVEL_RE.search(line) for line in coach_lines)
    has_timeout_line = any(TIMEOUT_RE.search(line) for line in coach_lines)

    starting_life = starting_life_for(format_name)
    near_starting = (
        both_seats_recorded
        and all(isinstance(l, int) for l in seat_lives)
        and min(seat_lives) >= starting_life - NEAR_STARTING_LIFE_MARGIN
    )
    early_end = bool(
        isinstance(turn_number, int)
        and turn_number <= EARLY_TURN_MAX
        and near_starting
        and not has_concede
        and not has_error_line
        and not has_timeout_line
    )

    return {
        "match_id": match_id,
        "day": m1["day"],
        "format_name": format_name,
        "archetype": archetype_label(bundle),
        "result": m1["result"],
        "turn_number": turn_number,
        "lives": lives,
        "starting_life": starting_life,
        "opponent_commanders": opponent_commanders(bundle),
        # M1 metric passthrough (same definitions as upgrade_metrics.py)
        "m1": m1,
        # W1 additions
        "flags": flags,
        "watchdog_types": watchdog_types,
        "fallbacks": fallback_tally(bundle),
        "flag_examples": {k: list(v) for k, v in flag_examples.items()},
        "early_end": early_end,
        "has_concede_marker": has_concede,
        "has_error_line": has_error_line,
        "has_timeout_line": has_timeout_line,
    }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

_MATCH_METRIC_KEYS = (
    "answers",
    "pass_answers",
    "code_answers",
    "single_option_menus",
    "decisions_with_line_search",
    "decisions_with_unmodelled",
    "chosen_unmodelled",
    "plans_formed",
    "path_corrected",
)

_NEW_DECK_BUCKET = {
    "matches": 0,
    "games": 0,
    "wins": 0,
    **dict.fromkeys(_MATCH_METRIC_KEYS, 0),
}


def _new_deck_bucket():
    bucket = dict(_NEW_DECK_BUCKET)
    bucket["unmodelled_cards"] = collections.Counter()
    bucket["unmodelled_examples"] = collections.defaultdict(list)
    bucket["call_classes"] = collections.defaultdict(
        lambda: {"calls": 0, "input": 0, "cached": 0, "think": 0, "sec": 0.0}
    )
    bucket["big_prompts"] = []
    bucket["keeps"] = []
    return bucket


def _new_match_fold():
    fold: dict = {"games": 0}
    for key in _MATCH_METRIC_KEYS:
        fold[key] = 0
    fold["unmodelled_cards"] = collections.Counter()
    fold["unmodelled_examples"] = collections.defaultdict(list)
    fold["call_classes"] = collections.defaultdict(
        lambda: {"calls": 0, "input": 0, "cached": 0, "think": 0, "sec": 0.0}
    )
    fold["big_prompts"] = []
    fold["keeps"] = []
    fold["_latest"] = None
    return fold


def _merge_review_into_match(fold, rv):
    """Merge one game bundle's review into its match fold."""
    m1 = rv["m1"]
    fold["games"] += 1
    for key in _MATCH_METRIC_KEYS:
        fold[key] += m1[key]
    fold["unmodelled_cards"].update(m1["unmodelled_cards"])
    for card in m1["unmodelled_cards"]:
        ex = fold["unmodelled_examples"][card]
        if rv["match_id"] and rv["match_id"] not in ex and len(ex) < 3:
            ex.append(rv["match_id"])
    fold["big_prompts"].extend(m1["big_prompts"])
    fold["keeps"].extend(m1["keeps"])
    for cls, bucket in m1["call_classes"].items():
        acc = fold["call_classes"][cls]
        for field in ("calls", "input", "cached", "think"):
            acc[field] += bucket[field]
        acc["sec"] += bucket["sec"]
    fold["_latest"] = rv


def aggregate(reviews):
    """Fold per-bundle reviews into the W1 ranked tables.

    A match can span several game bundles (one per game). Like M1
    (upgrade_metrics.py), games are folded per ``match_id`` and the LAST
    game's review carries the match-level result: wins are counted once
    per MATCH, never per game. Event-level tallies (flags, watchdog
    hand-offs, fallbacks, early-end suspects) stay per game bundle.
    """
    matches = {}  # match_id -> fold (insertion order; last game wins)
    agg = {
        "bundles": len(reviews),
        "matches": 0,
        "days": set(),
        "by_deck": collections.defaultdict(_new_deck_bucket),
        "flags": collections.Counter(),
        "flag_matches": collections.defaultdict(set),
        "watchdog_types": collections.Counter(),
        "watchdog_examples": collections.defaultdict(list),
        "fallbacks_by_decision_type": collections.Counter(),
        "fallback_examples": collections.defaultdict(list),
        "opponent_commanders": collections.defaultdict(lambda: {"matches": 0, "wins": 0}),
        "early_ends": [],
        "flag_examples": collections.defaultdict(list),
    }

    for rv in reviews:
        _merge_review_into_match(matches.setdefault(rv["match_id"], _new_match_fold()), rv)

        if rv["day"]:
            agg["days"].add(rv["day"])

        for category in rv["flags"]:
            agg["flags"][category] += rv["flags"][category]
            if rv["match_id"]:
                agg["flag_matches"][category].add(rv["match_id"])
                examples = agg["flag_examples"][category]
                if rv["match_id"] not in examples and len(examples) < 3:
                    examples.append(rv["match_id"])
        for dtype, n in rv["watchdog_types"].items():
            agg["watchdog_types"][dtype] += n
            ex = agg["watchdog_examples"][dtype]
            if rv["match_id"] and rv["match_id"] not in ex and len(ex) < 3:
                ex.append(rv["match_id"])
        for pair, n in rv["fallbacks"].items():
            agg["fallbacks_by_decision_type"][pair] += n
            ex = agg["fallback_examples"][pair]
            if rv["match_id"] and rv["match_id"] not in ex and len(ex) < 3:
                ex.append(rv["match_id"])

        if rv["early_end"]:
            agg["early_ends"].append(
                {
                    "match_id": rv["match_id"],
                    "format_name": rv["format_name"],
                    "turn_number": rv["turn_number"],
                    "lives": rv["lives"],
                    "starting_life": rv["starting_life"],
                }
            )

    # Match-level roll-up: one row contribution per match, keyed by the
    # deck of the last game (whose review also carries the match result).
    for fold in matches.values():
        latest = fold["_latest"]
        deck_key = "%s | %s" % (latest["format_name"] or UNKNOWN, latest["archetype"])
        deck = agg["by_deck"][deck_key]
        deck["matches"] += 1
        deck["games"] += fold["games"]
        if latest["result"] == "win":
            deck["wins"] += 1
        for key in _MATCH_METRIC_KEYS:
            deck[key] += fold[key]
        deck["unmodelled_cards"].update(fold["unmodelled_cards"])
        for card in fold["unmodelled_cards"]:
            ex = deck["unmodelled_examples"][card]
            for example in fold["unmodelled_examples"].get(card, ()):
                if example not in ex and len(ex) < 3:
                    ex.append(example)
        deck["big_prompts"].extend(fold["big_prompts"])
        deck["keeps"].extend(fold["keeps"])
        for cls, bucket in fold["call_classes"].items():
            acc = deck["call_classes"][cls]
            for field in ("calls", "input", "cached", "think"):
                acc[field] += bucket[field]
            acc["sec"] += bucket["sec"]

        # Opposing-commander win rates are match-level too.
        for cmd in latest["opponent_commanders"]:
            cbucket = agg["opponent_commanders"][cmd]
            cbucket["matches"] += 1
            if latest["result"] == "win":
                cbucket["wins"] += 1

    agg["matches"] = len(matches)
    agg["days"] = sorted(agg["days"])
    return agg


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _examples(examples):
    return ", ".join(examples[:3]) if examples else "(none)"


def _percentile(sorted_values, q):
    if not sorted_values:
        return None
    idx = min(int(len(sorted_values) * q), len(sorted_values) - 1)
    return sorted_values[idx]


def _prompt_stats(big_prompts):
    values = sorted(big_prompts)
    return _percentile(values, 0.5), _percentile(values, 0.9)


def _deck_inference_line(deck):
    calls = sum(b["calls"] for b in deck["call_classes"].values())
    think = sum(b["think"] for b in deck["call_classes"].values())
    sec = sum(b["sec"] for b in deck["call_classes"].values())
    classes = ", ".join(
        "%s:%d" % (cls, b["calls"])
        for cls, b in sorted(deck["call_classes"].items(), key=lambda kv: -kv[1]["calls"])
    )
    return "      inference: %d calls (%s), %d thinking tokens, %.1fs" % (
        calls,
        classes or "(none)",
        think,
        sec,
    )


def _deck_keeps_line(deck):
    by_lands = collections.defaultdict(lambda: {"games": 0, "wins": 0})
    for lands, result in deck["keeps"]:
        row = by_lands[lands]
        row["games"] += 1
        if result == "win":
            row["wins"] += 1
    parts = ["%d-land:%d/%d" % (lands, row["wins"], row["games"]) for lands, row in sorted(by_lands.items())]
    return ", ".join(parts)


def render_report(agg):
    """Human-readable W1 mining report."""
    out = []
    w = out.append
    w(
        "W1 fleet review — %d bundles / %d matches, days: %s"
        % (agg["bundles"], agg["matches"], ", ".join(agg["days"]) or UNKNOWN)
    )
    if agg.get("skipped_non_bundle_files"):
        w("(skipped %d non-bundle file(s), e.g. live_context.json)" % agg["skipped_non_bundle_files"])
    w("")
    w("== Coverage and M1 numbers by (format | archetype)")
    for deck_key in sorted(agg["by_deck"]):
        d = agg["by_deck"][deck_key]
        wr = (100.0 * d["wins"] / d["matches"]) if d["matches"] else 0.0
        w(
            "  %s: %d matches (%d games), %d wins (%.0f%%), answers=%d pass=%d code=%d "
            "line_search=%d unmodelled=%d chosen_unmodelled=%d"
            % (
                deck_key,
                d["matches"],
                d["games"],
                d["wins"],
                wr,
                d["answers"],
                d["pass_answers"],
                d["code_answers"],
                d["decisions_with_line_search"],
                d["decisions_with_unmodelled"],
                d["chosen_unmodelled"],
            )
        )
        w(_deck_inference_line(d))
        median, p90 = _prompt_stats(d["big_prompts"])
        if median is not None:
            w(
                "      decision prompt: median %d chars, p90 %d chars; "
                "plans formed=%d path corrected=%d" % (median, p90, d["plans_formed"], d["path_corrected"])
            )
        else:
            w(
                "      decision prompt: no 'typed-decision: big prompt' lines; "
                "plans formed=%d path corrected=%d" % (d["plans_formed"], d["path_corrected"])
            )
        keeps = _deck_keeps_line(d)
        if keeps:
            w("      mulligan-guard keeps (wins/games by lands kept): %s" % keeps)
    w("")
    w("== Cards most often left unevaluated (overall top 15)")
    total_cards = collections.Counter()
    for d in agg["by_deck"].values():
        total_cards.update(d["unmodelled_cards"])
    for card, n in total_cards.most_common(15):
        examples = []
        for d in agg["by_deck"].values():
            examples.extend(d["unmodelled_examples"].get(card, []))
        w("  %4d  %-40s e.g. %s" % (n, card[:40], _examples(examples)))
    w("")
    w("== Per-archetype top unevaluated cards")
    for deck_key in sorted(agg["by_deck"]):
        d = agg["by_deck"][deck_key]
        tops = ", ".join("%s(%d)" % (c, n) for c, n in d["unmodelled_cards"].most_common(5))
        w("  %s: %s" % (deck_key, tops or "(none)"))
    w("")
    w("== Code-layer flags (count / matches affected / example match IDs)")
    for category, n in agg["flags"].most_common():
        w(
            "  %-32s %4d across %d matches e.g. %s"
            % (
                category,
                n,
                len(agg["flag_matches"][category]),
                _examples(sorted(agg["flag_examples"][category])),
            )
        )
    w("")
    w("== Watchdog hop-limit hand-offs by decision type (count / example match IDs)")
    for dtype, n in agg["watchdog_types"].most_common():
        w("  %-28s %4d e.g. %s" % (dtype, n, _examples(agg["watchdog_examples"][dtype])))
    w("")
    w(
        "== Packet-recorded fallbacks by decision type "
        "(request_type / fallback mechanism; count / example match IDs)"
    )
    for (rtype, fallback), n in agg["fallbacks_by_decision_type"].most_common():
        w(
            "  %-22s %-14s %4d e.g. %s"
            % (rtype, fallback, n, _examples(agg["fallback_examples"][(rtype, fallback)]))
        )
    w("")
    w(
        "== Win rate by opposing commander (legendary Creature/Planeswalker "
        "in the opponent's end-of-game command zone; sparse by nature)"
    )
    rows = sorted(agg["opponent_commanders"].items(), key=lambda kv: (-kv[1]["matches"], kv[0]))
    for cmd, b in rows:
        wr = (100.0 * b["wins"] / b["matches"]) if b["matches"] else 0.0
        w("  %-36s n=%2d wins=%2d (%.0f%%)" % (cmd[:36], b["matches"], b["wins"], wr))
    w("")
    w(
        "== Early-end suspects (turn<=%d, both players within %d of starting "
        "life, no executed concede / ERROR-level line / timed-out line)"
        % (EARLY_TURN_MAX, NEAR_STARTING_LIFE_MARGIN)
    )
    if not agg["early_ends"]:
        w("  (none)")
    for e in agg["early_ends"]:
        w(
            "  %s [%s] turn=%s lives=%s (start=%d)"
            % (e["match_id"], e["format_name"], e["turn_number"], e["lives"], e["starting_life"])
        )
    return "\n".join(out)


def json_report(agg):
    """JSON-serialisable version of the aggregate."""
    by_deck = {}
    for key, d in agg["by_deck"].items():
        entry = {f: d[f] for f in _NEW_DECK_BUCKET}
        median, p90 = _prompt_stats(d["big_prompts"])
        entry.update(
            {
                "unmodelled_cards": dict(d["unmodelled_cards"]),
                "unmodelled_examples": {c: ex for c, ex in d["unmodelled_examples"].items()},
                "call_classes": {cls: dict(b) for cls, b in d["call_classes"].items()},
                "big_prompts": list(d["big_prompts"]),
                "keeps": [list(k) for k in d["keeps"]],
                "prompt_median_chars": median,
                "prompt_p90_chars": p90,
            }
        )
        by_deck[key] = entry
    return {
        "bundles": agg["bundles"],
        "matches": agg["matches"],
        "days": agg["days"],
        "by_deck": by_deck,
        "flags": dict(agg["flags"]),
        "flag_matches": {k: sorted(v) for k, v in agg["flag_matches"].items()},
        "flag_examples": dict(agg["flag_examples"]),
        "watchdog_types": dict(agg["watchdog_types"]),
        "watchdog_examples": dict(agg["watchdog_examples"]),
        "fallbacks_by_decision_type": {
            "%s/%s" % pair: n for pair, n in agg["fallbacks_by_decision_type"].items()
        },
        "fallback_examples": {"%s/%s" % pair: ex for pair, ex in agg["fallback_examples"].items()},
        "opponent_commanders": {k: dict(v) for k, v in agg["opponent_commanders"].items()},
        "early_ends": agg["early_ends"],
    }


def collect_reviews(directory, since=None):
    """Review every match bundle in *directory*.

    Files that are not match bundles (no ``coach_log``/``packet``, e.g. the
    ``live_context.json`` the live coach keeps beside the bundles) are
    skipped and counted, not silently folded into an "unknown" row.
    Returns ``(reviews, skipped_count)``.
    """
    reviews = []
    skipped = 0
    for path in iter_bundle_paths(directory):
        bundle = load_bundle(path)
        if not is_match_bundle(bundle):
            skipped += 1
            continue
        if since:
            day = _bundle_day(bundle)
            if day and day < since:
                continue
        reviews.append(review_bundle(bundle))
    return reviews, skipped


def main(argv=None):
    parser = argparse.ArgumentParser(description="W1: mine shared match bundles for recurring misplays.")
    parser.add_argument(
        "--dir", default=DEFAULT_BUNDLES_DIR, help="directory of *.json.gz / *.json match bundles"
    )
    parser.add_argument(
        "--since", default=None, metavar="YYYY-MM-DD", help="only bundles created on/after this date"
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json", help="emit JSON instead of the human report"
    )
    args = parser.parse_args(argv)

    if not os.path.isdir(args.dir):
        parser.error("bundles directory not found: %s" % args.dir)

    reviews, skipped = collect_reviews(args.dir, since=args.since)

    agg = aggregate(reviews)
    agg["skipped_non_bundle_files"] = skipped
    if args.as_json:
        print(json.dumps(json_report(agg), indent=2, sort_keys=True))
    else:
        print(render_report(agg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
