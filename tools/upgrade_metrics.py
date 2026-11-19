#!/usr/bin/env python3
"""Baseline metrics for the Fable November upgrades (plan milestone M1).

Replays the match bundles that the coach writes under ``~/.arenamcp/match_bundles``
and reproduces the baseline numbers documented in section 1 of
``fable-november-upgrades.md`` (repo root):

* matches / wins / losses, split by ``settings_subset.autopilot_enabled``
* LLM inference per call class (calls, input tokens, cached input tokens,
  thinking tokens, seconds — per match and per call), from the
  ``[PROXY] Request metrics: {...}`` lines in ``coach_log``
* decision answers: total, ``pass`` answers, answers resolved by code without
  a model call, and menus that offered a single option
* line search: decisions that ran one, decisions with an unevaluated option,
  chosen options that were unevaluated, and the most frequent unevaluated cards
* game plans formed, and plans whose ``path`` was corrected
  (``GamePlan validation ... path: corrected ...``)
* mulligan-guard keeps grouped by number of lands kept, with win rate
* decision prompt size (``typed-decision: big prompt (N chars)``): median / p90

Usage::

    python tools/upgrade_metrics.py                       # ~/.arenamcp/match_bundles
    python tools/upgrade_metrics.py --dir DIR --since 2026-11-09
    python tools/upgrade_metrics.py --json                # machine-readable

A "match" is a unique ``match_id``; a bundle is one game of that match.
Decisions with no ``chosen_options`` (e.g. superseded by an executed action)
are not counted as answers.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import median
from types import SimpleNamespace

BASELINE_WIN_RATE = 0.484
TARGET_WIN_RATE = 0.520
MIN_GATE_MATCHES = 30


def wilson_interval(
    wins: int,
    decided: int,
    *,
    z: float = 1.959963984540054,
) -> tuple[float, float]:
    """Two-sided Wilson binomial confidence interval in [0.0, 1.0]."""
    if decided <= 0:
        return 0.0, 0.0
    w = max(0, min(int(wins), int(decided)))
    n = int(decided)
    p = w / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    half = (z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * n)) / n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def evaluate_promotion_gate(
    wins: int,
    losses: int,
    *,
    baseline: float = BASELINE_WIN_RATE,
    target: float = TARGET_WIN_RATE,
    min_matches: int = MIN_GATE_MATCHES,
    watchdog_hop_limit_rate: float | None = None,
    max_hop_limit_rate: float = 0.05,
    b3_v2_blunder_rate: float | None = None,
    max_b3_v2_blunder_rate: float = 0.05,
) -> dict:
    """Evaluate W1/M1 promotion gate against >52% target and 48.4% pre-Neon baseline."""
    wins_i = max(0, int(wins))
    losses_i = max(0, int(losses))
    decided = wins_i + losses_i
    win_rate = (wins_i / decided) if decided > 0 else None
    win_rate_pct = (100.0 * win_rate) if win_rate is not None else None
    low, high = wilson_interval(wins_i, decided)
    delta = (win_rate - baseline) if win_rate is not None else None
    min_matches_met = decided >= min_matches
    win_rate_met = win_rate is not None and win_rate > target
    clock_safety_met = (
        True if watchdog_hop_limit_rate is None else (float(watchdog_hop_limit_rate) <= max_hop_limit_rate)
    )
    b3_v2_met = True if b3_v2_blunder_rate is None else (float(b3_v2_blunder_rate) <= max_b3_v2_blunder_rate)

    reasons: list[str] = []
    if not min_matches_met:
        reasons.append(f"insufficient decided matches: {decided} < {min_matches}")
    if not win_rate_met:
        if win_rate is None:
            reasons.append("no decided matches to compute win rate")
        else:
            reasons.append(f"win rate {100.0 * win_rate:.1f}% <= target {100.0 * target:.1f}%")
    if not clock_safety_met:
        reasons.append(
            f"watchdog_hop_limit_rate {float(watchdog_hop_limit_rate):.4f} > {max_hop_limit_rate:.4f}"
        )
    if not b3_v2_met:
        reasons.append(f"b3_v2_blunder_rate {float(b3_v2_blunder_rate):.4f} > {max_b3_v2_blunder_rate:.4f}")

    passed = min_matches_met and win_rate_met and clock_safety_met and b3_v2_met
    if not min_matches_met:
        status = "insufficient_matches"
    elif passed:
        status = "passed"
    else:
        status = "failed"

    return {
        "status": status,
        "passed": passed,
        "wins": wins_i,
        "losses": losses_i,
        "decided_matches": decided,
        "win_rate": win_rate,
        "win_rate_pct": win_rate_pct,
        "wilson_95_low": low,
        "wilson_95_high": high,
        "baseline_win_rate": baseline,
        "target_win_rate": target,
        "delta_vs_baseline": delta,
        "min_matches": min_matches,
        "min_matches_met": min_matches_met,
        "win_rate_met": win_rate_met,
        "clock_safety_met": clock_safety_met,
        "b3_v2_met": b3_v2_met,
        "watchdog_hop_limit_rate": float(watchdog_hop_limit_rate)
        if watchdog_hop_limit_rate is not None
        else None,
        "max_hop_limit_rate": max_hop_limit_rate,
        "b3_v2_blunder_rate": float(b3_v2_blunder_rate) if b3_v2_blunder_rate is not None else None,
        "max_b3_v2_blunder_rate": max_b3_v2_blunder_rate,
        "reasons": reasons,
    }


_REPO_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _REPO_SRC not in sys.path:
    sys.path.insert(0, _REPO_SRC)

DEFAULT_BUNDLES_DIR = os.path.expanduser("~/.arenamcp/match_bundles")
DEFAULT_PLAYBOOK_DIRS = (
    os.path.expanduser("~/.arenamcp/evidence/g2-g4-e3-1117/playbooks"),
    os.path.expanduser("~/.arenamcp/cache/playbooks"),
)

_PLAYBOOK_SYNERGY_CACHE: dict[tuple[tuple[str, ...], str], tuple[list, dict]] = {}
_CARD_FACTS_DISK_CACHE: dict[str, dict[str, dict]] = {}


def _load_disk_card_facts(facts_dir: str | None = None) -> dict[str, dict]:
    target = facts_dir or os.path.expanduser("~/.arenamcp/cache/card_facts")
    if target in _CARD_FACTS_DISK_CACHE:
        return _CARD_FACTS_DISK_CACHE[target]
    out: dict[str, dict] = {}
    if os.path.isdir(target):
        for fname in os.listdir(target):
            if not fname.endswith(".json"):
                continue
            digest = fname[:-5]
            try:
                with open(os.path.join(target, fname), encoding="utf-8") as fh:
                    rec = json.load(fh)
                if isinstance(rec, dict):
                    out[digest] = rec
            except (OSError, ValueError, TypeError):
                continue
    _CARD_FACTS_DISK_CACHE[target] = out
    return out


def _playbook_search_dirs() -> tuple[str, ...]:
    env_dirs = os.environ.get("ARENAMCP_PLAYBOOK_DIRS", "").strip()
    if env_dirs:
        return tuple(d for d in env_dirs.split(os.pathsep) if d)
    return DEFAULT_PLAYBOOK_DIRS


def _synergy_entries_for_state(state: dict) -> tuple[list, dict]:
    """Load cached/studied playbook entries (or CardFacts links) for a bundle snapshot."""
    if not isinstance(state, dict) or not state.get("deck_cards"):
        return [], {}
    from arenamcp.deck_strategy import deck_identity
    from arenamcp.synergy_tracker import entries_from_playbook

    identity = deck_identity(state)
    dirs = _playbook_search_dirs()
    cache_key = (dirs, identity)
    if cache_key in _PLAYBOOK_SYNERGY_CACHE:
        return _PLAYBOOK_SYNERGY_CACHE[cache_key]

    saved = None
    for directory in dirs:
        candidate = os.path.join(directory, f"{identity}.json")
        if os.path.isfile(candidate):
            try:
                with open(candidate, encoding="utf-8") as fh:
                    parsed = json.load(fh)
            except (OSError, ValueError, TypeError):
                continue
            if isinstance(parsed, dict) and (
                isinstance(parsed.get("data"), dict)
                or isinstance((parsed.get("playbook") or {}).get("data"), dict)
            ):
                saved = parsed
                break
    if isinstance(saved, dict) and isinstance(saved.get("playbook"), dict):
        saved = saved["playbook"]
    if not isinstance(saved, dict) or not isinstance(saved.get("data"), dict):
        _PLAYBOOK_SYNERGY_CACHE[cache_key] = ([], {})
        return [], {}
    raw_catalog = saved.get("catalog") or {}
    catalog = {int(k): v for k, v in raw_catalog.items() if str(k).isdigit() and isinstance(v, dict)}
    pb = SimpleNamespace(
        data=saved["data"],
        catalog=catalog,
        commanders=list(saved.get("commanders") or ()),
    )
    facts_map = _load_disk_card_facts()
    entries = entries_from_playbook(
        pb,
        deck_cards=state.get("deck_cards") or (),
        card_facts_map=facts_map or None,
    )
    _PLAYBOOK_SYNERGY_CACHE[cache_key] = (entries, catalog)
    return entries, catalog


def _unwrap_decision_state(decision: dict) -> dict | None:
    raw = decision.get("state_snapshot")
    if not isinstance(raw, dict):
        return None
    inner = raw.get("state")
    return inner if isinstance(inner, dict) else raw


def _option_grp_id_map(options: list, state: dict, catalog: dict) -> dict[str, int]:
    """Resolve option_id -> canonical grp_id across option metadata and state zones."""
    by_iid: dict[int, int] = {}
    for zone_name in ("hand", "battlefield", "command", "graveyard", "exile", "stack"):
        for card in state.get(zone_name) or (state.get("zones") or {}).get(zone_name) or ():
            if not isinstance(card, dict):
                continue
            iid = card.get("instance_id")
            gid = card.get("original_grp_id") or card.get("base_grp_id") or card.get("grp_id")
            if type(iid) is int and type(gid) is int and gid > 4:
                by_iid[iid] = gid
    by_name: dict[str, int] = {}
    for gid, info in catalog.items():
        if isinstance(info, dict) and info.get("name"):
            by_name.setdefault(str(info["name"]).strip().casefold(), int(gid))

    resolved: dict[str, int] = {}
    for opt in options:
        if not isinstance(opt, dict) or not opt.get("option_id"):
            continue
        oid = str(opt["option_id"])
        meta = opt.get("meta") if isinstance(opt.get("meta"), dict) else {}
        gid = meta.get("grpId") or meta.get("grp_id")
        if type(gid) is not int or gid <= 4:
            iid = meta.get("instanceId") or meta.get("instance_id")
            if type(iid) is int and iid in by_iid:
                gid = by_iid[iid]
        if (type(gid) is not int or gid <= 4) and opt.get("label"):
            label = str(opt["label"]).strip()
            for prefix in ("Cast ", "Play land: ", "Activate ", "Play "):
                if label.startswith(prefix):
                    label = label[len(prefix) :].strip()
                    break
            gid = by_name.get(label.casefold())
        if type(gid) is int and gid > 4:
            resolved[oid] = gid
    return resolved


REQUEST_METRICS_RE = re.compile(r"Request metrics:?\s*(\{.*\})\s*$")
KEEP_LINE_RE = re.compile(r"Mulligan guard: KEEP")
LANDS_RE = re.compile(r"(\d+) lands?")
BIG_PROMPT_RE = re.compile(r"typed-decision: big prompt \((\d+) chars\)")
PLAN_FORMED_RE = re.compile(r"Game plan reform \(")
# Real logged form: "GamePlan validation (turn 12): path: plan claim corrected: ..."
PATH_CORRECTED_RE = re.compile(r"GamePlan validation.*\bpath:.*corrected")

# --- T1: time lost on the Arena clock (plan section "T1") -------------------
#
# Measures, per event type, the seconds between Arena asking and the coach
# answering, and how much of Arena's turn timer was left at the answer.
# Four waste sources are quantified because plan section T1 names them as
# likely candidates:
#   * surfaced priority requests answered slowly (ask -> answer)
#   * watchdog waits after a hop-limit yield
#   * planning thrown away when the request changed mid-plan
#   * candidate full model calls spent only to conclude "pass again" on an
#     unchanged no-op priority window (equal window-change signatures mark
#     these as candidate waste, not proven-redundant — see the caveat the
#     output prints beside the figure; the waste that drained the clock in
#     match 9e858d62 turn 11 on 2026-11-08)

COACH_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})")
GRE_HEADER_TS_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4}) (\d{1,2}):(\d{2}):(\d{2}) (AM|PM)")
SURFACED_REQUEST_RE = re.compile(r"post-cast priority: new (\w+) request surfaced")
HOP_LIMIT_RE = re.compile(r"post-cast priority: hop limit \(\d+\) reached; leaving \w+ to the watchdog")
PLAN_TRIGGER_RE = re.compile(r"Autopilot planning: trigger=")
REQUEST_CHANGED_RE = re.compile(r"request changed during planning")
ANY_SUBMIT_RE = re.compile(r"mac bridge submit_\w+:|GRE bridge submitted ")
PASS_SUBMIT_RE = re.compile(r"mac bridge submit_pass:|GRE bridge submitted pass\b")
# Rope timers are deadline timers (decision_clock._DEADLINE_BEHAVIORS); the
# side map mirrors decision_clock._ROPE_TYPES.
_DEADLINE_BEHAVIORS = frozenset({"TimerBehavior_TakeControl", "TimerBehavior_Timeout"})
_ROPE_SIDES = {"TimerType_ActivePlayer": "active", "TimerType_NonActivePlayer": "nonactive"}

_ANSWER_SEARCH_LINES = 80  # surface -> answer lookahead (log lines)
_ANSWER_WINDOW_S = 120.0  # ...and max seconds between ask and answer
_HOP_WAIT_WINDOW_S = 90.0  # hop-limit yield -> watchdog answer window
_SUBMIT_DEDUPE_S = 0.5  # mac + GRE bridges log one submission twice

# "Clearing stale decision 'Priority' (window change to turn=9 active=1
# priority=2 phase=Phase_Main1 step=-)" — the game-window identity the
# redundant-pass detector keys on (real logged form, match 9e858d62).
WINDOW_CHANGE_RE = re.compile(
    r"window change to turn=(-?\d+) active=(-?\d+) priority=(-?\d+) "
    r"phase=(\S+) step=([^\s)]+)"
)


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


def coach_line_epoch(line):
    """Epoch seconds of a coach_log line's leading timestamp, or None."""
    m = COACH_TS_RE.match(line)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f").timestamp()


def gre_event_epoch(event):
    """Epoch seconds of a GRE player_log event's header timestamp, or None."""
    m = GRE_HEADER_TS_RE.match(str((event or {}).get("header_ts") or ""))
    if not m:
        return None
    month, day, year, hour, minute, second, ampm = m.groups()
    hour = int(hour) % 12 + (12 if ampm == "PM" else 0)
    return datetime(int(year), int(month), int(day), hour, int(minute), int(second)).timestamp()


def gre_event_epoch_precise(event):
    """Header-local clock coordinate with the payload's subsecond precision.

    Coach log timestamps and GRE headers are local wall time; payload epochs
    are UTC (eight hours apart in the retained example). Do not mix axes.
    Only the fractional second is applied, so timezone offsets cannot distort
    ask/answer joins. Absent payload precision retains the header coordinate.
    """
    from decimal import Decimal, InvalidOperation

    header = gre_event_epoch(event)
    if header is None:
        return None
    try:
        raw = str(json.loads(str((event or {}).get("json") or "{}")).get("timestamp") or "")
        if not raw.isdigit():
            return header
        stamp = Decimal(raw)
        scale = Decimal(10000000 if len(raw) >= 17 else 1000)
        if len(raw) < 12:
            return header
        return header + float((stamp % scale) / scale)
    except (ValueError, InvalidOperation):
        return header


def _timer_seconds(timer, key_ms, key_s):
    """Seconds from a timer field pair, preferring the millisecond key.

    Mirrors decision_clock._seconds so the metric reads timers exactly as
    the live coach does.
    """
    for key, scale in ((key_ms, 1000.0), (key_s, 1.0)):
        value = timer.get(key)
        if value is not None:
            try:
                return float(value) / scale
            except (TypeError, ValueError):
                continue
    return None


def _rope_report(timer, reported_at):
    """Frozen reading of one rope timer, mirroring decision_clock.seat_entry.

    Only deadline-behaviour rope timers count (TimerBehavior_TakeControl /
    TimerBehavior_Timeout on TimerType_ActivePlayer / NonActivePlayer).
    Returns {"duration_s", "elapsed_s", "running", "reported_at"}: the time
    left at ``reported_at`` is ``duration_s - elapsed_s``, decaying by wall
    time since only while ``running`` — a paused report freezes until the
    next one (review round 4: the previous revision derived remaining time
    from wall time since turn start, ignoring elapsedMs and paused state,
    so a paused 90 s rope with 30 s elapsed read 80 s ten seconds later
    instead of 60 s).
    """
    if timer.get("behavior") not in _DEADLINE_BEHAVIORS:
        return None
    side = _ROPE_SIDES.get(timer.get("type"))
    if side is None:
        return None
    duration = _timer_seconds(timer, "durationMs", "durationSec")
    if duration is None or duration <= 0:
        return None
    elapsed = _timer_seconds(timer, "elapsedMs", "elapsedSec") or 0.0
    return {
        "duration_s": duration,
        "elapsed_s": elapsed,
        "running": bool(timer.get("running")),
        "reported_at": reported_at,
    }


def _walk_payload_nodes(node):
    """Every dict in a parsed GRE event payload, in document order."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_payload_nodes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_payload_nodes(value)


def gre_clock_timeline(events):
    """Chronological clock snapshots from GRE player_log events.

    Each snapshot freezes what the log knew AT that event, so a rope-left
    value computed for a past timestamp never moves when later events are
    appended (2026-11-11 review round 3), and it reads Arena's own timer
    reports instead of deriving time from wall offsets (review round 4):

        [{"epoch": sec, "turn": int|None, "active_seat": int|None,
          "ropes": {seat: {"active": report|None, "nonactive": report|None}}}]

    Every message in an event payload is walked, so GameStateMessage /
    QueuedGameStateMessage timer updates count alongside TimerStateMessage
    ones: rope timers are owned via ``players[]`` ``systemSeatNumber``/
    ``timerIds`` pairs (unknown ownership is not guessed from timer numbering), and ``turn``/``active_seat`` come from whichever ``turnInfo``
    the payload carries. A rope report keeps the exact
    ``durationSec``/``elapsedMs``/``running`` the log stated; see
    _rope_report for how remaining time is read from it.
    """
    snaps = []
    cur_turn = None
    active_seat = None
    timer_seats: dict = {}
    ropes: dict = {}
    prev_epoch = None

    def owner_of(timer_id):
        if timer_id in timer_seats:
            return timer_seats[timer_id]
        return None

    for event in events or []:
        epoch = gre_event_epoch_precise(event)
        payload = str((event or {}).get("json") or "")
        if epoch is None or not payload:
            continue
        # Keep document order for occasional out-of-order header timestamps;
        # payload fractional precision distinguishes events within one second.
        if prev_epoch is not None and epoch < prev_epoch:
            epoch = prev_epoch
        prev_epoch = epoch
        try:
            root = json.loads(payload)
        except ValueError:
            continue
        nodes = list(_walk_payload_nodes(root))
        changed = False
        # Ownership first: a GameStateMessage may list its players after its
        # timers, so learn every timerId -> seat pair in this event before
        # attributing any of its timer reports.
        for node in nodes:
            for player in node.get("players") or []:
                if not isinstance(player, dict):
                    continue
                owner = player.get("systemSeatNumber")
                if owner is None:
                    continue
                for timer_id in player.get("timerIds") or []:
                    try:
                        timer_seats[int(timer_id)] = int(owner)
                    except (TypeError, ValueError):
                        continue
        for node in nodes:
            info = node.get("turnInfo")
            if isinstance(info, dict):
                try:
                    turn = int(info["turnNumber"])
                except (KeyError, TypeError, ValueError):
                    turn = None
                if turn is not None and turn != cur_turn:
                    cur_turn = turn
                    changed = True
                try:
                    seen_seat = int(info["activePlayer"])
                except (KeyError, TypeError, ValueError):
                    seen_seat = None
                if seen_seat is not None and seen_seat != active_seat:
                    active_seat = seen_seat
                    changed = True
            timers = node.get("timers")
            if not isinstance(timers, list):
                continue
            seat_hint = node.get("seatId")
            for timer in timers:
                if not isinstance(timer, dict):
                    continue
                # Stamp using this event coordinate; a query at arrival has
                # zero decay, and later queries decay only running reports.
                report = _rope_report(timer, epoch)
                if report is None:
                    continue
                try:
                    timer_id = int(timer.get("timerId") or 0)
                except (TypeError, ValueError):
                    timer_id = 0
                try:
                    owner = int(seat_hint) if seat_hint is not None else owner_of(timer_id)
                except (TypeError, ValueError):
                    owner = 0
                if not owner:
                    continue
                slot = ropes.setdefault(owner, {"active": None, "nonactive": None})
                side = _ROPE_SIDES[timer["type"]]
                if slot[side] != report:
                    slot[side] = report
                    changed = True
        if changed:
            snaps.append(
                {
                    "epoch": epoch,
                    "turn": cur_turn,
                    "active_seat": active_seat,
                    "ropes": {seat: dict(sides) for seat, sides in ropes.items()},
                }
            )
    return snaps


def rope_seconds_left(clock, epoch, local_seat=None):
    """Seconds of the local player's Arena decision rope left at ``epoch``.

    Reads the rope report visible at ``epoch`` exactly the way the live
    coach's decision_clock.seat_entry consumes per-event timer broadcasts:
    time left at the report is duration minus elapsed, decaying by wall time
    since the report only while that report said ``running`` — a paused
    reading freezes until the next one (review round 4: wall-time-since-
    turn-start ignored elapsedMs, paused state and GameStateMessage updates).
    The side follows the active seat as of ``epoch``.

    Queries use header-local coordinates with payload fractional precision.
    A report is visible only at or before the query; later same-second events
    cannot influence earlier measurements. No claim of matching all corpus
    timer events is made without an explicit per-event comparison.
    """
    if epoch is None or local_seat is None:
        return None
    snap = None
    for candidate in clock or []:
        if candidate["epoch"] <= epoch:
            snap = candidate
        else:
            break
    if snap is None:
        return None
    if snap["active_seat"] is None:
        return None
    side = "active" if snap["active_seat"] == local_seat else "nonactive"
    report = ((snap.get("ropes") or {}).get(local_seat) or {}).get(side)
    if report is None:
        return None
    remaining = report["duration_s"] - report["elapsed_s"]
    if report["running"]:
        remaining -= max(epoch - report["reported_at"], 0.0)
    return max(remaining, 0.0)


def final_rope_seconds(clock):
    """Each seat's rope time left as of the timeline's last snapshot."""
    if not clock:
        return {}
    end = clock[-1]["epoch"]
    out: dict = {}
    for candidate in clock:
        for seat, sides in (candidate.get("ropes") or {}).items():
            for side, rep in sides.items():
                if rep is None:
                    continue
                remaining = rep["duration_s"] - rep["elapsed_s"]
                if rep["running"]:
                    remaining -= max(end - rep["reported_at"], 0.0)
                out.setdefault(seat, {})[side] = max(remaining, 0.0)
    return out


def successful_submit(line):
    """Only an explicit successful bridge result is an observed answer."""
    if "GRE bridge submitted " in line:
        return True
    match = re.search(r"mac bridge submit_\w+:\s*(.*)$", line)
    if not match:
        return False
    try:
        result = ast.literal_eval(match.group(1))
    except (ValueError, SyntaxError):
        return False
    return isinstance(result, dict) and result.get("ok") is True


def surfaced_request_events(coach_lines, clock, local_seat=None):
    """Ask->answer seconds and rope left for each surfaced priority request.

    Returns (events, unanswered_by_type) where each event is
    {"event_type", "ask_to_ans_s", "rope_left_s"}. Attribution is bounded
    log-marker matching, not proof of every Arena request identity: these
    markers carry no request IDs. Supersession, cancellation and failed or
    unproven mac submissions cannot establish an answer.
    """
    events = []
    unanswered = Counter()
    for i, line in enumerate(coach_lines):
        m = SURFACED_REQUEST_RE.search(line)
        if not m:
            continue
        ask = coach_line_epoch(line)
        if ask is None:
            continue
        answer_epoch = None
        for j in range(i + 1, min(len(coach_lines), i + _ANSWER_SEARCH_LINES)):
            later = coach_line_epoch(coach_lines[j])
            if later is not None and later - ask > _ANSWER_WINDOW_S:
                break
            # A new surfaced request or cancellation closes this ask. Never
            # borrow a later request's answer, including undated boundaries.
            if SURFACED_REQUEST_RE.search(coach_lines[j]) or REQUEST_CHANGED_RE.search(coach_lines[j]):
                break
            if successful_submit(coach_lines[j]):
                if later is not None and later < ask:
                    break
                answer_epoch = later  # may be None if the submit line is undated
                break
        if answer_epoch is None:
            unanswered[m.group(1)] += 1
            continue
        events.append(
            {
                "event_type": m.group(1),
                "ask_to_ans_s": round(answer_epoch - ask, 3),
                "rope_left_s": rope_seconds_left(clock, answer_epoch, local_seat),
            }
        )
    return events, unanswered


HANDOFF_PICKUP_RE = re.compile(r"Triggering decision:|Triggers detected:|\| TRIGGER: ")


def hop_limit_waits(coach_lines):
    """Seconds each hop-limit yield added before the follow-up was picked up.

    When the post-cast chain hits its hop limit it leaves the follow-up
    request to the watchdog; the clock cost of that handover is only the
    delay until something picks the request up again. The normal trigger
    path usually does so within tens of milliseconds (2026-11-10
    evidence: median 0.18 s, p90 0.26 s over 72 yields in standalone.log)
    — the model call that then ANSWERS the follow-up is legitimate
    decision time, not waiting, and counting it (as an earlier revision
    did, yield -> submit) inflated this class ~10x. Measures yield -> the
    next pickup marker (``Triggering decision:``, ``Triggers detected:``
    or ``TRIGGER:`` — different trigger kinds log different lines); a
    yield followed by no pickup inside the window (the window closed with
    no follow-up) adds nothing.
    """
    waits = []
    for i, line in enumerate(coach_lines):
        if not HOP_LIMIT_RE.search(line):
            continue
        hop = coach_line_epoch(line)
        if hop is None:
            continue
        for j in range(i + 1, min(len(coach_lines), i + _ANSWER_SEARCH_LINES)):
            later = coach_line_epoch(coach_lines[j])
            if later is not None and later - hop > _HOP_WAIT_WINDOW_S:
                break
            if HANDOFF_PICKUP_RE.search(coach_lines[j]):
                if later is not None and later >= hop:
                    waits.append(round(later - hop, 3))
                break
    return waits


def cancellation_marker_families(coach_lines):
    """Count log markers, not independent requests or attributable elapsed time.

    Layered notices lack stable request identity; nearest preceding start cannot
    establish causal pairing or additive waste, even after deduplication.
    """
    families = []
    for line in coach_lines:
        if not REQUEST_CHANGED_RE.search(line):
            continue
        if "typed-decision:" in line:
            family = "typed_decision"
        elif "Backend returned error sentinel" in line:
            family = "backend_error_sentinel"
        elif "Autopilot:" in line:
            family = "autopilot"
        elif "LLM path failed" in line:
            family = "planner_path_failure"
        else:
            family = "other"
        families.append(family)
    return families


def cancellation_summary(families):
    return {
        "marker_count": len(families),
        "families": dict(sorted(Counter(families).items())),
        "unique_requests": None,
        "attributable_duration_s": None,
        "causal_time_rank_eligible": False,
        "interpretation": "layered log markers; request identity and causal duration unproven",
    }


def redundant_pass_cycles(coach_lines):
    """Decision-model seconds likely burned re-answering one unchanged pass window.

    A cycle is two consecutive pass submissions between which every
    ``Clearing stale decision ... (window change to ...)`` line carries
    the same turn/active/priority/phase/step signature the first pass was
    answered under, and no non-pass submission intervened (match 9e858d62
    turn 11, 2026-11-08: ~10 such cycles in ~79 s, each paying a full
    decision call that concluded "pass again" while the rope ran out).
    Only decision-class model seconds count — a background plan call
    inside the same interval is not waste attributable to the unchanged
    window. Submissions the mac and GRE bridges log twice within
    _SUBMIT_DEDUPE_S count once.

    Caveat (2026-11-11 review): equality of the logged turn/active/
    priority/phase/step signature does not by itself prove the stack,
    menu or material state were unchanged — the log line is a summary,
    not the state. Each counted cycle is therefore a *candidate* waste
    interval identified from the log, not a proven-redundant one; the
    runtime reuse guard additionally checks the menu fingerprint and the
    full material-state digest before it re-answers anything.
    """
    wasted_s = []
    last_pass_sig = None  # window signature the last counted pass saw
    sigs_since_pass = set()  # signatures observed since that pass
    cur_sig = None  # most recent window-change signature
    decision_s = 0.0  # decision-class model seconds since last submit
    last_submit_epoch = None
    last_submit_was_pass = False
    for line in coach_lines:
        metrics = parse_request_metrics(line)
        if metrics is not None:
            if str(metrics.get("call_class") or "").startswith("decision"):
                decision_s += (metrics.get("total_ms") or 0) / 1000.0
            continue
        m = WINDOW_CHANGE_RE.search(line)
        if m:
            cur_sig = m.groups()
            sigs_since_pass.add(cur_sig)
            continue
        if not ANY_SUBMIT_RE.search(line):
            continue
        epoch = coach_line_epoch(line)
        if epoch is None:
            continue
        is_pass = bool(PASS_SUBMIT_RE.search(line))
        if (
            last_submit_epoch is not None
            and is_pass == last_submit_was_pass
            and 0.0 < epoch - last_submit_epoch <= _SUBMIT_DEDUPE_S
        ):
            last_submit_epoch = epoch  # same submission logged by both bridges
            continue
        if (
            is_pass
            and last_pass_sig is not None
            and cur_sig == last_pass_sig
            and sigs_since_pass <= {last_pass_sig}
            and decision_s > 0.0
        ):
            wasted_s.append(round(decision_s, 3))
        last_pass_sig = cur_sig if is_pass else None
        sigs_since_pass = set()
        decision_s = 0.0
        last_submit_epoch = epoch
        last_submit_was_pass = is_pass
    return wasted_s


def _proxy_cancelled_record(line: str, metrics: dict) -> dict | None:
    """Extract bounded cancellation/drop fields from a proxy Request metrics row."""
    status = metrics.get("status")
    if status not in {"cancelled", "dropped"}:
        return None
    call_id = metrics.get("call_id")
    if isinstance(call_id, str) and call_id:
        receipt = hashlib.sha256(f"call_id:{call_id}".encode()).hexdigest()
    elif COACH_TS_RE.match(line) or metrics.get("response_id") or metrics.get("request_started_unix_s"):
        receipt = hashlib.sha256(line.encode()).hexdigest()
    else:
        receipt = None
    corr = metrics.get("correlation_id")
    if not (isinstance(corr, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", corr)):
        corr = None
    total_ms = metrics.get("total_ms")
    if type(total_ms) not in (int, float) or not math.isfinite(total_ms) or total_ms < 0:
        total_ms = None
    started = metrics.get("request_started_unix_s")
    if type(started) not in (int, float) or not math.isfinite(started) or started < 0:
        started = None
    src = metrics.get("cancel_source")
    if src not in {"request_changed", "hedge_loser", "timeout"}:
        src = None
    return {
        "receipt": receipt,
        "call_id": call_id if isinstance(call_id, str) and call_id else None,
        "call_class": str(metrics.get("call_class") or "unknown"),
        "status": str(status),
        "cancel_source": src,
        "correlation_id": corr,
        "request_started_unix_s": float(started) if started is not None else None,
        "total_ms": float(total_ms) if total_ms is not None else None,
    }


def _merged_interval_seconds(records: list[dict]) -> float:
    """Wall-clock union duration in seconds across overlapping calls in one request."""
    intervals: list[tuple[float, float]] = []
    untimed_s: list[float] = []
    for rec in records:
        ms = rec.get("total_ms")
        dur_s = (ms / 1000.0) if ms is not None else None
        start = rec.get("request_started_unix_s")
        if start is not None and dur_s is not None:
            intervals.append((start, start + dur_s))
        elif dur_s is not None:
            untimed_s.append(dur_s)
    if not intervals:
        return max(untimed_s, default=0.0)
    intervals.sort()
    merged: list[list[float]] = []
    for start, end in intervals:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        elif end > merged[-1][1]:
            merged[-1][1] = end
    return sum(end - start for start, end in merged)


def summarize_correlated_cancellations(records: list[dict]) -> dict:
    """Attribute proxy cancellations via per-request correlation IDs without double-counting hedges."""
    deduped: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()
    for rec in records:
        if rec.get("call_id"):
            key = ("call_id", rec["call_id"])
        elif rec.get("receipt"):
            key = ("receipt", rec["receipt"])
        else:
            deduped.append(rec)
            continue
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(rec)

    by_corr: dict[str, list[dict]] = defaultdict(list)
    request_changed_calls = 0
    request_changed_uncorrelated = 0
    hedge_loser_calls = 0
    hedge_loser_s = 0.0
    timeout_cancelled_calls = 0
    uncorrelated_by_class: dict[str, dict] = {}

    for rec in deduped:
        src = rec.get("cancel_source")
        corr = rec.get("correlation_id")
        cc = rec.get("call_class") or "unknown"
        dur_s = (rec["total_ms"] / 1000.0) if rec.get("total_ms") is not None else 0.0
        if src == "request_changed" and cc.startswith("decision."):
            if corr is not None:
                request_changed_calls += 1
                by_corr[corr].append(rec)
            else:
                request_changed_uncorrelated += 1
        elif src == "hedge_loser":
            hedge_loser_calls += 1
            hedge_loser_s += dur_s
        elif src == "timeout":
            timeout_cancelled_calls += 1
        elif src is None:
            bucket = uncorrelated_by_class.setdefault(cc, {"calls": 0, "total_s": 0.0})
            bucket["calls"] += 1
            bucket["total_s"] += dur_s

    attributable_s = (
        round(sum(_merged_interval_seconds(group) for group in by_corr.values()), 3) if by_corr else None
    )
    return {
        "unique_requests": len(by_corr),
        "request_changed_calls": request_changed_calls,
        "request_changed_uncorrelated_calls": request_changed_uncorrelated,
        "attributable_duration_s": attributable_s,
        "hedge_loser_calls": hedge_loser_calls,
        "hedge_loser_total_s": round(hedge_loser_s, 3),
        "timeout_cancelled_calls": timeout_cancelled_calls,
        "uncorrelated_proxy_cancelled_by_class": {
            cls: {"calls": row["calls"], "total_s": round(row["total_s"], 3)}
            for cls, row in sorted(uncorrelated_by_class.items())
        },
    }


def t1_bundle_metrics(bundle):
    """T1 clock measurements for one bundle (plan section T1, item 1)."""
    coach_lines = ((bundle.get("coach_log") or {}).get("lines")) or []
    final_state = bundle.get("final_state") or {}
    local_seat = final_state.get("local_seat_id")
    clock = gre_clock_timeline(((bundle.get("player_log") or {}).get("events")) or [])
    events, unanswered = surfaced_request_events(coach_lines, clock, local_seat)
    cancelled_proxy = []
    for line in coach_lines:
        metrics = parse_request_metrics(line)
        if metrics is None:
            continue
        rec = _proxy_cancelled_record(line, metrics)
        if rec is not None:
            cancelled_proxy.append(rec)
    return {
        "events": events,
        "unanswered_by_type": dict(unanswered),
        "hop_waits_s": hop_limit_waits(coach_lines),
        "cancellation_marker_families": cancellation_marker_families(coach_lines),
        "redundant_pass_cycles_s": redundant_pass_cycles(coach_lines),
        "cancelled_proxy_calls": cancelled_proxy,
        "local_seat": local_seat,
        "final_timers_by_seat": final_rope_seconds(clock),
    }


def summarize_bundle(bundle):
    """Compute this bundle's contribution to every metric.

    Pure function of the bundle dict — the tests exercise it directly.
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
        "unmodelled_cards": Counter(),
        "requests": [],
        "call_classes": defaultdict(_inference_bucket),
        # coach-log counters
        "plans_formed": 0,
        "path_corrected": 0,
        "big_prompts": [],
        # (lands_kept, result) pairs for parseable KEEP lines
        "keeps": [],
        # T1 clock measurements (plan section T1)
        "t1": {
            "events": [],
            "unanswered_by_type": Counter(),
            "hop_waits_s": [],
            "cancellation_marker_families": [],
            "redundant_pass_cycles_s": [],
            "cancelled_proxy_calls": [],
        },
        # G4 synergy execution tracking
        "synergy_execution": {
            "decisions_evaluated": 0,
            "live_decisions": 0,
            "executed_live": 0,
            "chose_other": 0,
            "line_tagged_executed": 0,
            "line_tagged_other": 0,
            "samples": [],
        },
    }

    for line in coach_lines:
        metrics = parse_request_metrics(line)
        if metrics is not None:
            # Overlapping archive tails can contain the same timestamped request.
            # Without an identifying call_id/timestamp/receipt, count independent observations.
            call_id = metrics.get("call_id")
            if isinstance(call_id, str) and call_id:
                receipt = hashlib.sha256(f"call_id:{call_id}".encode()).digest()
            elif (
                COACH_TS_RE.match(line) or metrics.get("response_id") or metrics.get("request_started_unix_s")
            ):
                receipt = hashlib.sha256(line.encode()).digest()
            else:
                receipt = None
            record = {
                k: metrics.get(k)
                for k in (
                    "call_class",
                    "input_tokens",
                    "cached_input_tokens",
                    "ttft_ms",
                    "reasoning_tokens",
                    "total_ms",
                )
            }
            out["requests"].append((receipt, record))
            _record_request(out["call_classes"][record["call_class"] or "unknown"], record)
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

        state = _unwrap_decision_state(decision)
        if state is not None:
            entries, catalog = _synergy_entries_for_state(state)
            if entries:
                from arenamcp.synergy_tracker import evaluate_synergies

                syn = out["synergy_execution"]
                syn["decisions_evaluated"] += 1
                snap = evaluate_synergies(entries, state, catalog=catalog)
                if snap.live:
                    syn["live_decisions"] += 1
                    option_gids = _option_grp_id_map(options, state, catalog)
                    live_pieces = {gid for item in snap.live for gid in item["pieces"]}
                    chosen_touches = any(option_gids.get(oid) in live_pieces for oid in chosen)
                    offered_touches = any(
                        option_gids.get(o.get("option_id")) in live_pieces
                        for o in options
                        if isinstance(o, dict) and o.get("payable") is not False
                    )
                    tags = (lines or {}).get("tags") if isinstance(lines, dict) else {}
                    if not isinstance(tags, dict):
                        tags = {}
                    chosen_tag = tags.get(chosen[0]) if chosen else None
                    synergy_tags = [
                        tags.get(o.get("option_id"))
                        for o in options
                        if isinstance(o, dict)
                        and option_gids.get(o.get("option_id")) in live_pieces
                        and tags.get(o.get("option_id"))
                    ]
                    if chosen_touches:
                        syn["executed_live"] += 1
                        if chosen_tag:
                            syn["line_tagged_executed"] += 1
                    elif offered_touches:
                        syn["chose_other"] += 1
                        if chosen_tag:
                            syn["line_tagged_other"] += 1
                    if len(syn["samples"]) < 5 and (chosen_touches or offered_touches):
                        syn["samples"].append(
                            {
                                "turn": decision.get("turn"),
                                "chosen": list(chosen),
                                "executed": chosen_touches,
                                "live_ids": [item["id"] for item in snap.live[:4]],
                                "chosen_tag": chosen_tag,
                                "synergy_tags": synergy_tags[:4],
                            }
                        )

    out["t1"] = t1_bundle_metrics(bundle)

    return out


def _merge(dst, src):
    """Fold one bundle summary into a running total."""
    for key in (
        "answers",
        "pass_answers",
        "code_answers",
        "single_option_menus",
        "decisions_with_line_search",
        "decisions_with_unmodelled",
        "chosen_unmodelled",
        "plans_formed",
        "path_corrected",
    ):
        dst[key] += src[key]
    dst["unmodelled_cards"].update(src["unmodelled_cards"])
    dst["big_prompts"].extend(src["big_prompts"])
    dst["keeps"].extend(src["keeps"])
    t1_dst, t1_src = dst["t1"], src["t1"]
    t1_dst["events"].extend(t1_src["events"])
    t1_dst["unanswered_by_type"].update(t1_src["unanswered_by_type"])
    for key in (
        "hop_waits_s",
        "cancellation_marker_families",
        "redundant_pass_cycles_s",
        "cancelled_proxy_calls",
    ):
        t1_dst[key].extend(t1_src.get(key) or [])
    syn_dst = dst["synergy_execution"]
    syn_src = src.get("synergy_execution") or {}
    for key in (
        "decisions_evaluated",
        "live_decisions",
        "executed_live",
        "chose_other",
        "line_tagged_executed",
        "line_tagged_other",
    ):
        syn_dst[key] += int(syn_src.get(key) or 0)
    for sample in syn_src.get("samples") or ():
        if len(syn_dst["samples"]) < 10:
            syn_dst["samples"].append(sample)
    dst["games"] += 1


def _stat_block(values):
    """n / median / p90 / total for a list of second-values."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return {"n": 0, "median_s": None, "p90_s": None, "total_s": 0.0}
    return {
        "n": len(vals),
        "median_s": round(vals[min(int(len(vals) * 0.5), len(vals) - 1)], 3),
        "p90_s": round(vals[min(int(len(vals) * 0.9), len(vals) - 1)], 3),
        "total_s": round(sum(vals), 3),
    }


def aggregate_t1(match_folds):
    """Aggregate the per-match T1 folds into the report's clock section."""
    by_type = defaultdict(lambda: {"answered": 0, "unanswered": 0, "ask_to_ans_s": [], "rope_left_s": []})
    hop, cancels, cycles, proxy_cancels = [], [], [], []
    per_match = {}
    for match_id, fold in match_folds.items():
        t1 = fold["t1"]
        pm = per_match.setdefault(
            match_id,
            {
                "surfaced_events": 0,
                "events_by_type": defaultdict(list),
                "unanswered_by_type": Counter(),
                "hop_waits_s": [],
                "cancellation_marker_families": [],
                "redundant_pass_cycles_s": [],
                "cancelled_proxy_calls": [],
            },
        )
        for ev in t1["events"]:
            row = by_type[ev["event_type"]]
            row["answered"] += 1
            row["ask_to_ans_s"].append(ev["ask_to_ans_s"])
            if ev["rope_left_s"] is not None:
                row["rope_left_s"].append(ev["rope_left_s"])
            pm["surfaced_events"] += 1
            pm["events_by_type"][ev["event_type"]].append(ev)
        pm["unanswered_by_type"].update(t1["unanswered_by_type"])
        for event_type, count in t1["unanswered_by_type"].items():
            by_type[event_type]["unanswered"] += count
        for key, dst_list in (
            ("hop_waits_s", hop),
            ("cancellation_marker_families", cancels),
            ("redundant_pass_cycles_s", cycles),
            ("cancelled_proxy_calls", proxy_cancels),
        ):
            rows = t1.get(key) or []
            dst_list.extend(rows)
            pm[key].extend(rows)

    by_type_out = {}
    for etype, row in by_type.items():
        ropes = sorted(row["rope_left_s"])
        by_type_out[etype] = {
            "answered": row["answered"],
            "unanswered": row["unanswered"],
            "ask_to_ans": _stat_block(row["ask_to_ans_s"]),
            "rope_left_at_answer": (
                {
                    "n": len(ropes),
                    "median_s": round(ropes[min(int(len(ropes) * 0.5), len(ropes) - 1)], 3),
                    "min_s": round(ropes[0], 3),
                }
                if ropes
                else {"n": 0, "median_s": None, "min_s": None}
            ),
        }

    per_match_out = {}
    for match_id, pm in per_match.items():
        entry = {
            "surfaced_events": pm["surfaced_events"],
            "unanswered_by_type": dict(pm["unanswered_by_type"]),
            "events_by_type": {},
            "hop_waits": _stat_block(pm["hop_waits_s"]),
            "cancellation_markers": cancellation_summary(pm["cancellation_marker_families"]),
            "correlated_cancellations": summarize_correlated_cancellations(pm["cancelled_proxy_calls"]),
            "redundant_pass_cycles": _stat_block(pm["redundant_pass_cycles_s"]),
        }
        for etype, evs in pm["events_by_type"].items():
            ropes = [e["rope_left_s"] for e in evs if e["rope_left_s"] is not None]
            asks = sorted(e["ask_to_ans_s"] for e in evs)
            entry["events_by_type"][etype] = {
                "n": len(evs),
                "median_ask_to_ans_s": round(asks[min(int(len(asks) * 0.5), len(asks) - 1)], 3),
                "min_rope_left_s": round(min(ropes), 3) if ropes else None,
            }
        per_match_out[match_id] = entry

    cycles_block = _stat_block(cycles)
    cycles_block["interpretation"] = (
        "candidate waste intervals identified from equal window-change "
        "signatures; signature equality does not prove the stack, menu or "
        "material state was unchanged, and a static rerun of old bundles "
        "cannot demonstrate live savings"
    )
    return {
        "by_event_type": by_type_out,
        "hop_limit_watchdog_waits": _stat_block(hop),
        "cancellation_markers": cancellation_summary(cancels),
        "correlated_cancellations": summarize_correlated_cancellations(proxy_cancels),
        "redundant_pass_cycles": cycles_block,
        "per_match": per_match_out,
    }


def _inference_bucket():
    return {
        "calls": 0,
        "input": 0,
        "cached": 0,
        "think": 0,
        "sec": 0.0,
        "cache_reports": 0,
        "cache_reported_input": 0,
        "first_token_ms": [],
    }


def _record_request(bucket, metrics):
    bucket["calls"] += 1
    tokens, cached = metrics["input_tokens"], metrics["cached_input_tokens"]
    if type(tokens) is int and tokens >= 0:
        bucket["input"] += tokens
        if type(cached) is int and 0 <= cached <= tokens:
            bucket["cached"] += cached
            bucket["cache_reports"] += 1
            bucket["cache_reported_input"] += tokens
    ttft = metrics["ttft_ms"]
    if type(ttft) in (int, float) and math.isfinite(ttft) and ttft >= 0:
        bucket["first_token_ms"].append(ttft)
    bucket["think"] += metrics["reasoning_tokens"] or 0
    bucket["sec"] += (metrics["total_ms"] or 0) / 1000.0


def aggregate(summaries):
    """Roll per-bundle summaries up into the report structure."""
    per_class = defaultdict(_inference_bucket)
    totals = _inference_bucket()
    seen_receipts = set()
    matches = {}  # match_id -> folded totals (a match may span several games)

    for summary in summaries:
        fold = matches.setdefault(
            summary["match_id"],
            {
                "games": 0,
                "answers": 0,
                "pass_answers": 0,
                "code_answers": 0,
                "single_option_menus": 0,
                "decisions_with_line_search": 0,
                "decisions_with_unmodelled": 0,
                "chosen_unmodelled": 0,
                "unmodelled_cards": Counter(),
                "plans_formed": 0,
                "path_corrected": 0,
                "big_prompts": [],
                "keeps": [],
                "t1": {
                    "events": [],
                    "unanswered_by_type": Counter(),
                    "hop_waits_s": [],
                    "cancellation_marker_families": [],
                    "redundant_pass_cycles_s": [],
                    "cancelled_proxy_calls": [],
                },
                "synergy_execution": {
                    "decisions_evaluated": 0,
                    "live_decisions": 0,
                    "executed_live": 0,
                    "chose_other": 0,
                    "line_tagged_executed": 0,
                    "line_tagged_other": 0,
                    "samples": [],
                },
                "_latest": None,
            },
        )
        _merge(fold, summary)
        for receipt, metrics in summary["requests"]:
            if receipt is not None:
                if receipt in seen_receipts:
                    continue
                seen_receipts.add(receipt)
            _record_request(per_class[metrics["call_class"] or "unknown"], metrics)
            _record_request(totals, metrics)
        fold["_latest"] = summary  # last game carries the match result

    n_matches = len(matches)

    # Wins/losses are match-level: count them once per match, from the last
    # game's summary (a best-of-three counted per game would inflate both).
    wl_by_autopilot = {
        "on": {"matches": 0, "wins": 0, "losses": 0},
        "off": {"matches": 0, "wins": 0, "losses": 0},
    }
    for fold in matches.values():
        latest = fold["_latest"]
        side = wl_by_autopilot["on" if latest["autopilot"] else "off"]
        side["matches"] += 1
        if latest["result"] == "win":
            side["wins"] += 1
        elif latest["result"] == "loss":
            side["losses"] += 1

    for agg in list(per_class.values()) + [totals]:
        agg["calls_per_match"] = agg["calls"] / n_matches if n_matches else 0.0
        agg["input_per_call"] = agg["input"] / agg["calls"] if agg["calls"] else 0.0
        agg["cached_per_call"] = agg["cached"] / agg["cache_reports"] if agg["cache_reports"] else None
        known_input = agg["cache_reported_input"]
        agg["cache_share"] = agg["cached"] / known_input if known_input else None
        ttft = sorted(agg.pop("first_token_ms"))
        agg["ttft_reports"] = len(ttft)
        agg["median_ttft_ms"] = median(ttft) if ttft else None
        agg["p90_ttft_ms"] = ttft[math.ceil(0.9 * len(ttft)) - 1] if ttft else None
        agg["think_per_call"] = agg["think"] / agg["calls"] if agg["calls"] else 0.0
        agg["sec_per_call"] = agg["sec"] / agg["calls"] if agg["calls"] else 0.0

    answers_total = sum(fold["answers"] for fold in matches.values())
    pass_answers = sum(fold["pass_answers"] for fold in matches.values())
    code_answers = sum(fold["code_answers"] for fold in matches.values())
    single_option_menus = sum(fold["single_option_menus"] for fold in matches.values())

    line_decisions = sum(fold["decisions_with_line_search"] for fold in matches.values())
    unmodelled_decisions = sum(fold["decisions_with_unmodelled"] for fold in matches.values())
    chosen_unmodelled = sum(fold["chosen_unmodelled"] for fold in matches.values())
    card_counter = Counter()
    for fold in matches.values():
        card_counter.update(fold["unmodelled_cards"])

    plans_formed = sum(fold["plans_formed"] for fold in matches.values())
    path_corrected = sum(fold["path_corrected"] for fold in matches.values())

    keeps_by_lands = defaultdict(lambda: {"games": 0, "wins": 0})
    for fold in matches.values():
        for lands, result in fold["keeps"]:
            row = keeps_by_lands[lands]
            row["games"] += 1
            if result == "win":
                row["wins"] += 1

    all_prompts = sorted(p for fold in matches.values() for p in fold["big_prompts"])

    def pct(num, den):
        return (100.0 * num / den) if den else None

    def percentile(sorted_values, q):
        if not sorted_values:
            return None
        idx = min(int(len(sorted_values) * q), len(sorted_values) - 1)
        return sorted_values[idx]

    syn_evaluated = sum(fold["synergy_execution"]["decisions_evaluated"] for fold in matches.values())
    syn_live = sum(fold["synergy_execution"]["live_decisions"] for fold in matches.values())
    syn_exec = sum(fold["synergy_execution"]["executed_live"] for fold in matches.values())
    syn_other = sum(fold["synergy_execution"]["chose_other"] for fold in matches.values())
    syn_tag_exec = sum(fold["synergy_execution"]["line_tagged_executed"] for fold in matches.values())
    syn_tag_other = sum(fold["synergy_execution"]["line_tagged_other"] for fold in matches.values())
    syn_per_match = {
        mid: {k: v for k, v in fold["synergy_execution"].items() if k != "samples" or v}
        for mid, fold in matches.items()
        if fold["synergy_execution"]["decisions_evaluated"] > 0
    }
    t1 = aggregate_t1(matches)
    ap_on = wl_by_autopilot["on"]
    ap_on_matches = {mid: fold for mid, fold in matches.items() if fold["_latest"].get("autopilot")}
    t1_ap_on = aggregate_t1(ap_on_matches)
    ap_on_answers = sum(fold["answers"] for fold in ap_on_matches.values())
    hop_waits = int((t1_ap_on.get("hop_limit_watchdog_waits") or {}).get("n") or 0)
    hop_rate = (hop_waits / ap_on_answers) if ap_on_answers > 0 else None

    return {
        "matches": n_matches,
        "days_covered": sorted({s["day"] for s in summaries}),
        "autopilot": {
            key: {
                "matches": side["matches"],
                "wins": side["wins"],
                "losses": side["losses"],
                "win_rate_pct": pct(side["wins"], side["wins"] + side["losses"]),
            }
            for key, side in wl_by_autopilot.items()
        },
        "inference_per_class": per_class,
        "inference_totals": totals,
        "input_tokens_per_match": totals["input"] / n_matches if n_matches else 0.0,
        "answers": {
            "total": answers_total,
            "pass": pass_answers,
            "pass_pct": pct(pass_answers, answers_total),
            "code_answered_no_model_call": code_answers,
            "code_answered_pct": pct(code_answers, answers_total),
            "single_option_menus": single_option_menus,
            "per_match": answers_total / n_matches if n_matches else 0.0,
        },
        "line_search": {
            "decisions_with_line_search": line_decisions,
            "with_unevaluated_option": unmodelled_decisions,
            "with_unevaluated_pct": pct(unmodelled_decisions, line_decisions),
            "chosen_was_unevaluated": chosen_unmodelled,
            "top_unevaluated_cards": card_counter.most_common(10),
        },
        "plans_formed": plans_formed,
        "plans_path_corrected": path_corrected,
        "keeps_by_lands": {
            lands: {
                "games": row["games"],
                "wins": row["wins"],
                "win_rate_pct": pct(row["wins"], row["games"]),
            }
            for lands, row in sorted(keeps_by_lands.items())
        },
        "prompt_size_median_chars": percentile(all_prompts, 0.5),
        "prompt_size_p90_chars": percentile(all_prompts, 0.9),
        "t1": t1,
        "promotion_gate": evaluate_promotion_gate(
            ap_on["wins"],
            ap_on["losses"],
            watchdog_hop_limit_rate=hop_rate,
        ),
        "synergy_execution": {
            "matches_with_decisions": len(syn_per_match),
            "decisions_evaluated": syn_evaluated,
            "live_synergy_decisions": syn_live,
            "executed_live_synergy": syn_exec,
            "chose_different_play": syn_other,
            "execution_rate_pct": pct(syn_exec, syn_exec + syn_other),
            "line_tagged_executed": syn_tag_exec,
            "line_tagged_other": syn_tag_other,
            "per_match": syn_per_match,
        },
    }


def render_report(agg):
    """Human-readable report mirroring the baseline tables in plan section 1."""
    out = []
    ap = agg["autopilot"]
    out.append(f"Matches: {agg['matches']} (days covered: {', '.join(agg['days_covered']) or 'n/a'})")
    for key in ("on", "off"):
        side = ap[key]
        wr = side["win_rate_pct"]
        wr_s = f"{wr:.0f}%" if wr is not None else "-"
        out.append(
            f"  autopilot {key}: {side['matches']} matches "
            f"({side['wins']}W/{side['losses']}L, win rate {wr_s})"
        )

    out.append("")
    out.append("Inference per call class:")
    out.append(
        f"{'call_class':<28}{'calls/m':>9}{'in_tok/call':>13}"
        f"{'cached/call':>13}{'think/call':>12}{'s/call':>8}{'cache share':>13}{'reports/calls':>16}{'TTFT p50/p90 ms':>20}{'TTFT reports':>14}"
    )
    order = sorted(agg["inference_per_class"].items(), key=lambda kv: -kv[1]["calls"]) + [
        ("<ALL>", agg["inference_totals"])
    ]
    for cls, row in order:
        cached = f"{row['cached_per_call']:.0f}" if row["cached_per_call"] is not None else "n/a"
        share = f"{row['cache_share']:.1%}" if row["cache_share"] is not None else "n/a"
        receipts = f"{row['cache_reports']}/{row['calls']}"
        timings = f"{row['median_ttft_ms']:.0f}/{row['p90_ttft_ms']:.0f}" if row["ttft_reports"] else "n/a"
        out.append(
            f"{cls:<28}{row['calls_per_match']:>9.1f}"
            f"{row['input_per_call']:>13.0f}{cached:>13}"
            f"{row['think_per_call']:>12.1f}{row['sec_per_call']:>8.2f}{share:>13}{receipts:>16}{timings:>20}{row['ttft_reports']:>14}"
        )
    out.append(f"Input tokens per match: {agg['input_tokens_per_match']:.0f}")

    ans = agg["answers"]
    out.append("")
    out.append(f"Decision answers: {ans['total']} ({ans['per_match']:.1f}/match)")
    pass_pct = f"{ans['pass_pct']:.1f}%" if ans["pass_pct"] is not None else "-"
    code_pct = f"{ans['code_answered_pct']:.1f}%" if ans["code_answered_pct"] is not None else "-"
    out.append(f"  pass answers: {ans['pass']} ({pass_pct})")
    out.append(f"  answered by code without a model call: {ans['code_answered_no_model_call']} ({code_pct})")
    out.append(f"  single-option menus: {ans['single_option_menus']}")

    ls = agg["line_search"]
    uneval_pct = f"{ls['with_unevaluated_pct']:.1f}%" if ls["with_unevaluated_pct"] is not None else "-"
    out.append("")
    out.append(
        f"Line search: {ls['decisions_with_line_search']} decisions ran one; "
        f"{ls['with_unevaluated_option']} had an unevaluated option ({uneval_pct}); "
        f"a chosen option was unevaluated {ls['chosen_was_unevaluated']} times"
    )
    out.append("Most frequent unevaluated cards:")
    for name, count in ls["top_unevaluated_cards"]:
        out.append(f"  {count:>4}x {name}")

    pc_pct = (
        f"{100.0 * agg['plans_path_corrected'] / agg['plans_formed']:.1f}%" if agg["plans_formed"] else "-"
    )
    out.append("")
    out.append(
        f"Plans formed: {agg['plans_formed']}; path corrected: {agg['plans_path_corrected']} ({pc_pct})"
    )

    out.append("")
    out.append("Mulligan-guard keeps by lands kept:")
    out.append(f"{'lands':>6}{'games':>7}{'wins':>6}{'win rate':>10}")
    for lands, row in agg["keeps_by_lands"].items():
        wr = row["win_rate_pct"]
        wr_s = f"{wr:.0f}%" if wr is not None else "-"
        out.append(f"{lands:>6}{row['games']:>7}{row['wins']:>6}{wr_s:>10}")

    median = agg["prompt_size_median_chars"]
    p90 = agg["prompt_size_p90_chars"]
    out.append("")
    if median is not None:
        out.append(
            f"Decision prompt size (typed-decision big prompt lines): median {median} chars, p90 {p90} chars"
        )
    else:
        out.append("Decision prompt size: no 'typed-decision: big prompt' lines found")

    t1 = agg.get("t1") or {}

    def _secs(value):
        return f"{value:.2f}" if value is not None else "-"

    out.append("")
    out.append("Time on the Arena clock (T1):")
    out.append("Surfaced priority requests (Arena ask -> coach answer):")
    out.append(
        f"{'event_type':<22}{'answered':>9}{'med s':>8}{'p90 s':>8}"
        f"{'rope med s':>12}{'rope min s':>12}{'unans':>7}"
    )
    for etype, row in sorted(t1.get("by_event_type", {}).items()):
        ask = row["ask_to_ans"]
        rope = row["rope_left_at_answer"]
        out.append(
            f"{etype:<22}{row['answered']:>9}{_secs(ask['median_s']):>8}"
            f"{_secs(ask['p90_s']):>8}{_secs(rope['median_s']):>12}"
            f"{_secs(rope['min_s']):>12}{row['unanswered']:>7}"
        )
    hop = t1.get("hop_limit_watchdog_waits") or {}
    cancels = t1.get("cancellation_markers") or {}
    cycles = t1.get("redundant_pass_cycles") or {}
    out.append(
        f"Watchdog waits after hop-limit yield: {hop.get('n', 0)} events, "
        f"{hop.get('total_s', 0.0):.1f} s total "
        f"(median {_secs(hop.get('median_s'))} s)"
    )
    out.append(
        f"Request-change cancellation: {cancels.get('marker_count', 0)} log markers; "
        "unique requests and attributable duration unproven (excluded from causal-time ranking)"
    )
    out.append(
        "  marker families: "
        + ", ".join(f"{name}={count}" for name, count in sorted(cancels.get("families", {}).items()))
    )
    corr = t1.get("correlated_cancellations") or {}
    if (
        corr.get("unique_requests")
        or corr.get("hedge_loser_calls")
        or corr.get("timeout_cancelled_calls")
        or corr.get("request_changed_uncorrelated_calls")
    ):
        dur_str = (
            f"{corr['attributable_duration_s']:.1f} s wall-clock union"
            if corr.get("attributable_duration_s") is not None
            else "n/a"
        )
        out.append(
            f"  correlated request-change cancellations: {corr.get('unique_requests', 0)} unique requests "
            f"({corr.get('request_changed_calls', 0)} calls, {dur_str}); "
            f"hedge loser: {corr.get('hedge_loser_calls', 0)} calls ({corr.get('hedge_loser_total_s', 0.0):.1f} s); "
            f"timeout: {corr.get('timeout_cancelled_calls', 0)} calls"
        )
    uncorr_cls = corr.get("uncorrelated_proxy_cancelled_by_class") or {}
    if uncorr_cls:
        out.append(
            "  uncorrelated proxy cancelled/dropped by class: "
            + ", ".join(
                f"{cls}={row['calls']} ({row['total_s']:.1f}s)" for cls, row in sorted(uncorr_cls.items())
            )
        )
    out.append(
        f"Candidate redundant model calls on unchanged pass windows: "
        f"{cycles.get('n', 0)} cycles / "
        f"{cycles.get('total_s', 0.0):.1f} s "
        f"(median {_secs(cycles.get('median_s'))} s)"
    )
    out.append(
        "  caveat: equal window-change signatures mark candidate waste only —"
        " they do not prove the stack/menu/material state was unchanged, and"
        " a static rerun cannot demonstrate live savings"
    )

    per_match = t1.get("per_match") or {}
    interesting = {
        mid: entry
        for mid, entry in per_match.items()
        if entry.get("surfaced_events")
        or entry["hop_waits"]["n"]
        or entry["cancellation_markers"]["marker_count"]
        or entry["redundant_pass_cycles"]["n"]
    }
    if interesting:
        out.append("")
        out.append(
            f"Per-match clock detail ({len(interesting)} of {len(per_match)} "
            f"matches had measurable clock events):"
        )
        out.append(
            f"{'match':<40}{'surf n/med s':>14}{'rope min s':>12}{'hop s':>8}{'cancel marks':>12}{'cyc n/s':>14}"
        )
        for mid, entry in sorted(interesting.items()):
            types = entry.get("events_by_type") or {}
            surf_n = sum(v["n"] for v in types.values())
            meds = sorted(
                v["median_ask_to_ans_s"] for v in types.values() if v["median_ask_to_ans_s"] is not None
            )
            surf_med = meds[len(meds) // 2] if meds else None
            rope_mins = [v["min_rope_left_s"] for v in types.values() if v["min_rope_left_s"] is not None]
            rope_min = min(rope_mins) if rope_mins else None
            cyc = entry["redundant_pass_cycles"]
            cyc_cell = f"{cyc['n']}/{cyc['total_s']:.1f}" if cyc["n"] else "-"
            surf_cell = f"{surf_n}/{surf_med:.2f}" if surf_n else "-"
            hop_cell = f"{entry['hop_waits']['total_s']:.1f}" if entry["hop_waits"]["n"] else "-"
            cancel_cell = str(entry["cancellation_markers"]["marker_count"])
            out.append(
                f"{mid:<40}{surf_cell:>14}{_secs(rope_min):>12}{hop_cell:>8}{cancel_cell:>12}{cyc_cell:>14}"
            )

    syn = agg.get("synergy_execution") or {}
    if syn.get("decisions_evaluated"):
        rate = f"{syn['execution_rate_pct']:.1f}%" if syn.get("execution_rate_pct") is not None else "-"
        out.append("")
        out.append(
            f"Synergy execution (G4): {syn['matches_with_decisions']} matches, "
            f"{syn['decisions_evaluated']} evaluated decisions, "
            f"{syn['live_synergy_decisions']} with live synergy "
            f"(executed={syn['executed_live_synergy']}, other={syn['chose_different_play']}, rate={rate}; "
            f"line-tagged executed={syn.get('line_tagged_executed', 0)}, other={syn.get('line_tagged_other', 0)})"
        )

    gate = agg.get("promotion_gate")
    if isinstance(gate, dict):
        out.append("")
        if gate.get("win_rate_pct") is None:
            out.append(
                f"Promotion gate (autopilot on): {gate.get('status')} (0 decided matches vs target > {100.0 * gate['target_win_rate']:.1f}%)"
            )
        else:
            out.append(
                f"Promotion gate (autopilot on): {gate['status']} — {gate['wins']}W/{gate['losses']}L "
                f"({gate['win_rate_pct']:.1f}%, Wilson 95% [{100.0 * gate['wilson_95_low']:.1f}%, {100.0 * gate['wilson_95_high']:.1f}%], "
                f"delta vs {100.0 * gate['baseline_win_rate']:.1f}% baseline: {100.0 * gate['delta_vs_baseline']:+.1f} pp, "
                f"target > {100.0 * gate['target_win_rate']:.1f}%)"
            )

    return "\n".join(out)


def pivotal_thinking_summary(bundles):
    """E4: observed per-decision thinking, never requested budgets or missing=0.

    Uses final decision traces so a low-first unknown choice later promoted to
    pivotal contributes its entire thinking total to pivotal, not two cohorts.
    Failure-rate acceptance is intentionally unavailable without comparable
    complete watchdog/default telemetry; token evidence alone cannot pass E4.
    """
    categories = {
        k: {"decisions": 0, "complete_decisions": 0, "reasoning_tokens": 0} for k in ("pivotal", "routine")
    }
    matches = set()
    fingerprints = {}
    duplicates = 0
    for bundle in bundles:
        match = bundle.get("match_id")
        if not match:
            continue
        if type(match) is not str:
            raise ValueError("invalid match identity in E4 evidence")
        # Full-bundle equality only: never guess which conflicting copy is
        # authoritative or collapse distinct BO3 decisions within one bundle.
        fingerprint = hashlib.sha256(
            json.dumps(bundle, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).digest()
        previous = fingerprints.get(match)
        if previous is not None:
            if previous != fingerprint:
                raise ValueError("conflicting copies of a match in E4 evidence")
            duplicates += 1
            continue
        fingerprints[match] = fingerprint
        observations = [
            (row.get("trace") or {}).get("pivotal_thinking") or {}
            for row in (bundle.get("packet") or {}).get("decisions") or []
        ]
        for line in (bundle.get("coach_log") or {}).get("lines") or []:
            if "E4 declaration metrics: " not in line:
                continue
            try:
                event = json.loads(line.split("E4 declaration metrics: ", 1)[1])
                if isinstance(event, dict):
                    observations.append(event)
            except (ValueError, TypeError):
                continue
        for evidence in observations:
            category = evidence.get("category")
            if category not in categories:
                continue
            matches.add(match)
            bucket = categories[category]
            bucket["decisions"] += 1
            calls = evidence.get("call_usage") or []
            count = evidence.get("calls")
            values = [c.get("reasoning_tokens") for c in calls]
            if not calls or count != len(calls) or any(type(v) is not int or v < 0 for v in values):
                continue
            bucket["complete_decisions"] += 1
            bucket["reasoning_tokens"] += sum(values)
    for bucket in categories.values():
        bucket["average_thinking_tokens"] = (
            bucket["reasoning_tokens"] / bucket["complete_decisions"]
            if bucket["complete_decisions"] == bucket["decisions"] and bucket["complete_decisions"]
            else None
        )
    pivotal, routine = (categories[k]["average_thinking_tokens"] for k in ("pivotal", "routine"))
    ratio = pivotal / routine if pivotal is not None and routine is not None and routine > 0 else None
    return {
        "observed_matches": len(matches),
        "duplicate_bundles_ignored": duplicates,
        "categories": categories,
        "pivotal_routine_ratio": ratio,
        "tenfold_token_gate_met": ratio is not None and ratio >= 10,
        "timeout_watchdog_fallback_gate_met": None,
        "overall_e4_gate_met": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Reproduce the Fable November baseline metrics from match bundles."
    )
    parser.add_argument(
        "--dir",
        "--bundles",
        dest="bundles_dir",
        default=DEFAULT_BUNDLES_DIR,
        help=f"match_bundles directory (default: {DEFAULT_BUNDLES_DIR})",
    )
    parser.add_argument(
        "--since",
        default=None,
        metavar="YYYY-MM-DD",
        help="only count bundles started on/after this UTC date",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the aggregated metrics as JSON instead of text"
    )
    parser.add_argument(
        "--pivotal",
        action="store_true",
        help="E4 observed per-decision thinking evidence (JSON; does not waive failure-rate gate)",
    )
    args = parser.parse_args(argv)

    if not os.path.isdir(args.bundles_dir):
        parser.error(f"bundles directory not found: {args.bundles_dir}")

    summaries = []
    pivotal_bundles = []
    for path in iter_bundle_paths(args.bundles_dir):
        bundle = load_bundle(path)
        if not is_match_bundle(bundle):
            continue
        summary = summarize_bundle(bundle)
        if args.since and summary["day"] < args.since:
            continue
        summaries.append(summary)
        if args.pivotal:
            pivotal_bundles.append(bundle)

    if not summaries:
        print(
            f"No bundles found in {args.bundles_dir}" + (f" on/after {args.since}" if args.since else ""),
            file=sys.stderr,
        )
        return 1

    if args.pivotal:
        try:
            result = pivotal_thinking_summary(pivotal_bundles)
        except (ValueError, TypeError):
            parser.exit(2, "invalid or conflicting E4 evidence\n")
        print(json.dumps(result, indent=2))
        return 0

    agg = aggregate(summaries)
    if args.json:

        def _plain(obj):
            if isinstance(obj, Counter):
                return dict(obj)
            raise TypeError(repr(type(obj)))

        print(json.dumps(agg, indent=2, default=_plain))
    else:
        print(render_report(agg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
