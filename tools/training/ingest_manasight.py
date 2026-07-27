"""Ingest `manasight/manasight-corpus` into the replay menu-groundtruth schema.

What this is
------------
`manasight/manasight-corpus` is 55 gzipped, PII-sanitized MTGA `Player.log`
captures (43 MB) published as a parser test corpus. It is the only public
dataset that records BOTH sides of an in-game decision:

    GREMessageType_ActionsAvailableReq  ->  actionsAvailableReq.actions[]
                                            (the menu MTGA presented)
    ClientMessageType_PerformActionResp ->  performActionResp.actions[]
                                            (what the player picked)

joined by ``respId == msgId``.  Every other public source (17lands
`replay_data`, MTGO logs, tracker fixtures) records what was DONE but never
what was AVAILABLE — see `docs/rl-training-status.md` §18.

Relationship to `tools/eval/replay/menu_groundtruth.py`
------------------------------------------------------
That module extracts the same shape from MTGA's own `.rply` TimedReplay
files.  This module emits **the same record schema** by importing its
normalisers (`normalize_menu`, `menu_key`, `resolve_pick`, `card_facts`,
`_battlefield_split`) and the same state reconstructor
(`tools.eval.replay.state`), so the two corpora concatenate directly.

Two extra keys are added that the `.rply` extractor does not emit
(``source_corpus``, ``source_license``, plus match provenance).  They are
additive: a consumer reading both files should default ``source_corpus`` to
``"mtgacoach_rply"`` when absent.

Format differences the reader has to bridge
-------------------------------------------
A `.rply` is one `DIR-TIMESTAMP:{json}` line per GRE message.  A `Player.log`
instead interleaves:

  * ``Match to <player>: GreToClientEvent`` followed by ONE line of JSON
    carrying ``greToClientEvent.greToClientMessages[]`` (many GRE messages
    per blob) — these are the IN direction;
  * ``<player> to Match: ClientToGremessage`` followed by a PRETTY-PRINTED,
    multi-line JSON blob whose ``payload`` is the ClientToGRE message —
    the OUT direction;
  * one session log contains MANY matches, and ``msgId`` restarts inside
    each.  Requests are therefore paired to the *first response after the
    request* carrying that ``respId``, not through a global map (which the
    `.rply` extractor can safely use because a `.rply` is one match).

Caveats that materially reduce the useful yield
-----------------------------------------------
* **~46% of picks are ``ActionType_Pass``.**  A menu whose answer is "pass"
  teaches priority discipline, not card evaluation; the summary reports
  ``pick_action_types`` and several "meaningful option" counts so the honest
  denominator is visible rather than the raw menu count.
* **Player skill is LOW and it IS determinable** — see ``--rank-probe-only``.
  The logs carry ``RankGetCombinedRankInfo`` responses with
  ``constructedClass`` / ``limitedClass``.

All decision types (``--all-decision-types``)
---------------------------------------------
``ActionsAvailableReq`` is only ONE of the request types MTGA asks. Restricting
extraction to it is why an earlier corpus came out 69% land drops: the main-phase
menu is mostly about the land drop, while the genuinely strategic decisions —
who attacks, what blocks what, where the removal spell points, what the tutor
finds — arrive as ``DeclareAttackersReq`` / ``DeclareBlockersReq`` /
``SelectTargetsReq`` / ``SelectNReq`` / ``SearchReq``, which were being
discarded by SCOPE, not absence.

``--all-decision-types`` extracts all of them, projected onto the same
option/pick record shape (see ``normalize_decision`` in
`tools/eval/replay/menu_groundtruth.py`). Mulligans are never extracted:
"should I mulligan" is a generic decision, and the requirement is specifically
"out of THIS board, what is the next best action".

The strategic filter (``classify_strategic``) then keeps only decisions where a
competent player could genuinely have chosen otherwise, and the
``STRATEGIC_FUNNEL`` block in the summary shows every record dropped and why.
Quote ``stage_4_strategic_after_filter`` — never ``records_emitted``.

Usage
-----
    # download (43 MB) to ~/.arenamcp/corpora, then ingest
    python -m tools.training.ingest_manasight --download \
        --out tools/training/data/manasight_menu_groundtruth.jsonl

    # every decision type, strategic slices, Pass capped at 10%
    python -m tools.training.ingest_manasight --all-decision-types \
        --max-pass-share 0.10 \
        --out tools/training/data/manasight_all_decisions.jsonl \
        --strategic-out tools/training/data/manasight_strategic.jsonl \
        --strategic-action-out tools/training/data/manasight_strategic_action.jsonl \
        --summary-out tools/training/data/manasight_all_decisions_summary.json

    # just report what rank the contributor was, no extraction
    python -m tools.training.ingest_manasight --rank-probe-only

The corpus is stored OUTSIDE the repo by default (``~/.arenamcp/corpora``);
outputs land under ``tools/training/data/`` which is gitignored.  Do not
commit either.

License / attribution (recorded into every output record and the sidecar)
-------------------------------------------------------------------------
manasight-corpus is dual licensed **MIT OR Apache-2.0**, at the user's
option.  Both require the copyright notice and license text to travel with
redistributed copies or derivative works; Apache-2.0 additionally requires
stating that files were changed.  Derived training data counts.  The corpus
is not affiliated with or endorsed by Wizards of the Coast.

CPU only.  No GPU, no network beyond the optional corpus download.
"""

from __future__ import annotations

import argparse
import bisect
import gzip
import hashlib
import io
import json
import sys
import zipfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.eval.replay.menu_groundtruth import (  # noqa: E402
    AT_DECLARE_ATTACKER,
    AT_DECLARE_BLOCKER,
    AT_MODAL_OPTION,
    AT_NO_ATTACKS,
    AT_NO_BLOCKS,
    AT_OPTIONAL_NO,
    AT_OPTIONAL_YES,
    AT_SEARCH_FAIL,
    AT_SEARCH_FIND,
    AT_SELECT_N,
    AT_SELECT_TARGET,
    DECISION_TYPE_BY_MSG,
    DECISION_TYPES_EXCLUDED,
    MANA_LEVEL_ACTION_TYPES,
    _battlefield_split,
    _iter_state_messages,
    card_facts,
    menu_key,
    normalize_decision,
)
from tools.eval.replay.reader import ReplayMessage  # noqa: E402
from tools.eval.replay.state import (  # noqa: E402
    GameStateSnapshot,
    _apply_state_message,
    opponent_seat,
)

# ---------------------------------------------------------------------------
# Provenance / licensing — stamped into every record and the sidecar file.
# ---------------------------------------------------------------------------

SOURCE_CORPUS = "manasight"
SOURCE_LICENSE = "MIT OR Apache-2.0"
SOURCE_REPO = "https://github.com/manasight/manasight-corpus"
SOURCE_ZIP = "https://github.com/manasight/manasight-corpus/archive/refs/heads/main.zip"

ATTRIBUTION = (
    "Derived from manasight-corpus (https://github.com/manasight/manasight-corpus), "
    "sanitized MTG Arena Player.log captures published by the Manasight project "
    "(https://manasight.gg). Dual-licensed MIT OR Apache-2.0 at the user's option. "
    "Redistribution of these records or anything derived from them must carry the "
    "upstream copyright notice and license text (LICENSE-MIT / LICENSE-APACHE); "
    "Apache-2.0 additionally requires stating that the files were changed — they "
    "were: MTGA GRE messages were re-serialized into the mtgacoach menu-groundtruth "
    "record schema by tools/training/ingest_manasight.py. "
    "manasight-corpus is not affiliated with, endorsed by, or associated with "
    "Wizards of the Coast, Hasbro, or Magic: The Gathering Arena."
)

DEFAULT_CORPUS_ROOT = Path.home() / ".arenamcp" / "corpora"
DEFAULT_CORPUS_DIR = DEFAULT_CORPUS_ROOT / "manasight-corpus-main" / "corpus"
DEFAULT_OUT = REPO / "tools" / "training" / "data" / "manasight_menu_groundtruth.jsonl"

# A pick of one of these teaches priority discipline / mana plumbing, not card
# choice. They are counted separately so the honest yield is visible.
TRIVIAL_ACTION_TYPES = frozenset(MANA_LEVEL_ACTION_TYPES) | {"ActionType_Pass"}

# Matches against MTGA's built-in bot. Both seats are simulated locally, so the
# client logs the BOT's ActionsAvailableReq too (they are addressed to the
# non-local seat and dropped), and the human's own picks were made against an
# opponent that does not play well. 35% of this corpus is bot practice.
BOT_EVENT_MARKERS = ("AIBotMatch", "Sparky", "NPE_", "Tutorial", "ColorChallenge", "Precon", "PVE")

# MTGA's ranked constructed queues all end in `Ladder`
# (Ladder / Traditional_Ladder / Explorer_Ladder / Timeless_Ladder / ...).
RANKED_LADDER_MARKER = "Ladder"


def classify_event(event: Optional[str]) -> tuple[bool, bool]:
    """(is_bot_match, is_ranked_ladder) for a match's `eventId`."""
    name = event or ""
    is_bot = any(m.lower() in name.lower() for m in BOT_EVENT_MARKERS)
    is_ranked = RANKED_LADDER_MARKER.lower() in name.lower() and not is_bot
    return is_bot, is_ranked


# Cheap pre-filter: only JSON blobs containing one of these substrings are
# handed to json.loads. A 31 MB session log is mostly telemetry we never want.
_BLOB_MARKERS = (
    '"greToClientEvent"',
    '"clientToMatchServiceMessageType"',
    '"matchGameRoomStateChangedEvent"',
    '"constructedSeasonOrdinal"',
)


# ---------------------------------------------------------------------------
# Player.log blob reader
# ---------------------------------------------------------------------------


def _open_log(path: Path) -> io.TextIOBase:
    """Open a corpus log, transparently handling `.gz`."""
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, encoding="utf-8", errors="replace")


def iter_json_blobs(path: Path, *, max_blob_lines: int = 200_000) -> Iterator[dict]:
    """Yield every top-level JSON object in a `Player.log`.

    MTGA writes its interesting payloads as JSON starting at column 0 —
    either the whole object on one line (``GreToClientEvent``) or
    pretty-printed across many lines (``ClientToGremessage``).  Everything
    else in the log is prefixed (``[UnityCrossThreadLogger]``, ``<==``, ...),
    so "line starts with ``{``" is a reliable blob start.

    Braces are counted with string/escape awareness so a ``{`` inside a
    quoted value cannot desynchronise the scanner.  Blobs that never close
    (truncated log, log rotation mid-write) are abandoned after
    ``max_blob_lines``.
    """
    buf: list[str] = []
    depth = 0
    in_str = False
    esc = False
    with _open_log(path) as fh:
        for line in fh:
            if depth == 0:
                if not line.startswith("{"):
                    continue
                buf = []
                in_str = False
                esc = False
            buf.append(line)
            for ch in line:
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                elif ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
            if depth <= 0:
                depth = 0
                text = "".join(buf)
                buf = []
                if any(m in text for m in _BLOB_MARKERS):
                    try:
                        obj = json.loads(text)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if isinstance(obj, dict):
                        yield obj
            elif len(buf) > max_blob_lines:
                # Unterminated blob: give up on it rather than eat the file.
                depth = 0
                in_str = False
                esc = False
                buf = []


def _coerce_payload(payload) -> Optional[dict]:
    """`ClientToGREMessage.payload` is normally a dict, sometimes a JSON string."""
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            obj = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return None
        return obj if isinstance(obj, dict) else None
    return None


@dataclass
class LogMatch:
    """One match's worth of GRE traffic carved out of a session log."""

    index: int
    match_id: Optional[str] = None
    event_names: list[str] = field(default_factory=list)
    messages: list[ReplayMessage] = field(default_factory=list)
    seat_team: dict[int, int] = field(default_factory=dict)  # systemSeatId -> teamId
    winning_team_ids: list[int] = field(default_factory=list)
    completed: bool = False


@dataclass
class RankSnapshot:
    """One `RankGetCombinedRankInfo` response observed in a session log."""

    log_file: str
    constructed_class: Optional[str]
    constructed_level: Optional[int]
    constructed_won: Optional[int]
    constructed_lost: Optional[int]
    limited_class: Optional[str]
    limited_level: Optional[int]
    season: Optional[int]


def _room_event_info(ev: dict) -> tuple[Optional[str], list[str], dict[int, int], list[int], bool]:
    """Pull matchId / event names / seat->team / winners out of a room event."""
    gri = ev.get("gameRoomInfo") or {}
    cfg = gri.get("gameRoomConfig") or {}
    match_id = cfg.get("matchId")
    events: list[str] = []
    seat_team: dict[int, int] = {}
    for p in cfg.get("reservedPlayers") or []:
        name = p.get("eventId")
        if name and name not in events:
            events.append(str(name))
        sid, tid = p.get("systemSeatId"), p.get("teamId")
        if sid is not None and tid is not None:
            seat_team[int(sid)] = int(tid)
    winners: list[int] = []
    final = gri.get("finalMatchResult") or {}
    for r in final.get("resultList") or []:
        wt = r.get("winningTeamId")
        if wt is not None:
            winners.append(int(wt))
    completed = str(gri.get("stateType") or "") == "MatchGameRoomStateType_MatchCompleted"
    return match_id, events, seat_team, winners, completed


def parse_player_log(path: Path) -> tuple[list[LogMatch], list[RankSnapshot]]:
    """Split a session log into per-match GRE message streams.

    A session log is many matches back to back and ``msgId`` restarts in each,
    so the stream MUST be segmented before anything is paired or replayed.
    Segment boundaries come from ``matchGameRoomStateChangedEvent`` (a new
    ``matchId``); GRE traffic seen before the first such event (log rotation
    mid-match) is kept in a leading segment with ``match_id=None`` rather than
    dropped.
    """
    matches: list[LogMatch] = []
    ranks: list[RankSnapshot] = []
    current = LogMatch(index=0)

    def _flush() -> None:
        if current.messages or current.match_id:
            matches.append(current)

    for obj in iter_json_blobs(path):
        if "matchGameRoomStateChangedEvent" in obj:
            mid, events, seat_team, winners, completed = _room_event_info(
                obj["matchGameRoomStateChangedEvent"] or {}
            )
            if mid is not None and mid != current.match_id:
                if current.messages or current.match_id:
                    _flush()
                    current = LogMatch(index=len(matches))
                current.match_id = mid
            if current.match_id is None:
                current.match_id = mid
            for e in events:
                if e not in current.event_names:
                    current.event_names.append(e)
            current.seat_team.update(seat_team)
            for w in winners:
                current.winning_team_ids.append(w)
            current.completed = current.completed or completed
            continue

        if "greToClientEvent" in obj:
            gte = obj.get("greToClientEvent") or {}
            for m in gte.get("greToClientMessages") or []:
                if isinstance(m, dict):
                    current.messages.append(ReplayMessage(direction="IN", timestamp_us=0, payload=m))
            continue

        if obj.get("clientToMatchServiceMessageType"):
            payload = _coerce_payload(obj.get("payload"))
            if payload is not None:
                current.messages.append(ReplayMessage(direction="OUT", timestamp_us=0, payload=payload))
            continue

        if "constructedSeasonOrdinal" in obj:
            ranks.append(
                RankSnapshot(
                    log_file=path.name,
                    # constructedClass is omitted by MTGA when it is the default
                    # (lowest) tier, so absent != unknown — it is reported as
                    # "<omitted>" and read as Bronze-or-unranked.
                    constructed_class=obj.get("constructedClass"),
                    constructed_level=obj.get("constructedLevel"),
                    constructed_won=obj.get("constructedMatchesWon"),
                    constructed_lost=obj.get("constructedMatchesLost"),
                    limited_class=obj.get("limitedClass"),
                    limited_level=obj.get("limitedLevel"),
                    season=obj.get("constructedSeasonOrdinal"),
                )
            )

    _flush()
    return matches, ranks


def detect_local_seat_player_log(messages: list[ReplayMessage]) -> tuple[Optional[int], str]:
    """Which seat is the recording player? Returns ``(seat, method)``.

    **This deliberately does NOT reuse ``state.detect_local_seat``.** That
    function trusts ``GREMessageType_ConnectResp.systemSeatIds`` first, which
    is correct for `.rply` files but **wrong for Player.log**: measured over
    this corpus's 145 match segments, ConnectResp named the wrong seat in
    **14 of the 70** segments that carry one (20%), while the seat that GRE
    *requests* were addressed to was right **106/106** times, cross-checked
    against hand visibility (the client can only see grpIds in its own hand).

    Getting this wrong is not a small error: it silently reconstructs the
    OPPONENT's hand and board and scores the opponent's menu, which looks
    exactly like a bad model downstream. So the order is inverted here:
    request-addressee first, ConnectResp only as a fallback.

    No segment in this corpus addresses requests to both seats, so a split
    vote means the segmentation is wrong; the caller records the method used
    and a hand-visibility cross-check flags the rest.
    """
    counts: Counter[int] = Counter()
    for m in messages:
        if m.direction != "IN" or not m.is_request:
            continue
        ids = m.payload.get("systemSeatIds") or []
        if len(ids) == 1:
            counts[int(ids[0])] += 1
    if len(counts) == 1:
        return next(iter(counts)), "request_addressee"
    if counts:
        return counts.most_common(1)[0][0], "request_addressee_split"
    for m in messages:
        if m.msg_type == "GREMessageType_ConnectResp":
            ids = m.payload.get("systemSeatIds") or []
            if len(ids) == 1:
                return int(ids[0]), "connect_resp_fallback"
    return None, "undetermined"


def pair_requests_ordered(messages: list[ReplayMessage]) -> dict[int, ReplayMessage]:
    """Map ``request list-index -> response message``.

    `menu_groundtruth` can use a flat ``{msgId: response}`` map because a
    `.rply` holds exactly one match.  A `Player.log` segment can still contain
    several *games* of a Bo3 whose ``msgId`` restarts, so a flat map would
    silently pair game 1's request with game 3's answer.  Here each request is
    paired with the FIRST response occurring after it that carries the
    matching ``respId``, which is correct regardless of id reuse.
    """
    out_positions: dict[int, list[int]] = {}
    for i, m in enumerate(messages):
        if m.direction != "OUT":
            continue
        rid = m.payload.get("respId")
        if rid is None:
            continue
        try:
            out_positions.setdefault(int(rid), []).append(i)
        except (TypeError, ValueError):
            continue

    pairs: dict[int, ReplayMessage] = {}
    for i, m in enumerate(messages):
        if m.direction != "IN" or m.msg_id is None:
            continue
        cands = out_positions.get(m.msg_id)
        if not cands:
            continue
        j = bisect.bisect_right(cands, i)
        if j < len(cands):
            pairs[i] = messages[cands[j]]
    return pairs


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def meaningful_options(menu: list[dict]) -> list[dict]:
    """Menu entries that represent a real card decision.

    Drops ``Pass`` and the per-land mana plumbing (``Activate_Mana`` /
    ``FloatMana``).  What remains is "things you could actually choose to do".
    """
    return [e for e in menu if e["action_type"] not in TRIVIAL_ACTION_TYPES]


# ---------------------------------------------------------------------------
# Strategic filter
# ---------------------------------------------------------------------------
#
# The requirement: the model must answer "out of THIS board, what is the next
# best action specifically". A corpus that is 69% land drops teaches a reflex,
# not that skill — the previous LoRA scored identically to the untuned base on
# strategic decisions (13/63 both) while getting MORE land-biased (played the
# first land in 84% of strategic decisions vs 60% for the base).
#
# So "is this decision genuinely strategic?" is the primary constraint. A
# record survives only if a competent player could have chosen differently
# AND the choice is about cards, not plumbing.

# Casting a spell, in all its MTGA spellings.
CAST_ACTION_TYPES = frozenset(
    {
        "ActionType_Cast",
        "ActionType_CastLeft",
        "ActionType_CastRight",
        "ActionType_CastAdventure",
        "ActionType_CastOmen",
        "ActionType_CastRightRoom",
        "ActionType_CastLeftRoom",
        "ActionType_CastAsThoughSorcery",
    }
)

# Playing a permanent from hand that is NOT a land (MDFC creature side, etc.).
PLAY_ACTION_TYPES = frozenset({"ActionType_Play", "ActionType_PlayMDFC"})

ACTIVATE_ACTION_TYPES = frozenset({"ActionType_Activate", "ActionType_Special", "ActionType_Special_TurnFaceUp"})

COMBAT_ACTION_TYPES = frozenset({AT_DECLARE_ATTACKER, AT_DECLARE_BLOCKER})

TARGETING_ACTION_TYPES = frozenset({AT_SELECT_TARGET, AT_SELECT_N, AT_SEARCH_FIND, AT_MODAL_OPTION})

# "at least one cast / attack / block / activate / target" — the requirement's
# own wording, made executable.
STRATEGIC_ACTION_TYPES = (
    CAST_ACTION_TYPES | ACTIVATE_ACTION_TYPES | COMBAT_ACTION_TYPES | TARGETING_ACTION_TYPES
)

# Rows that are choices but carry no card identity on their own. They count
# toward "could have chosen differently" but never satisfy the "at least one
# strategic action" test by themselves — otherwise "attack with nobody" alone
# would qualify a menu with no creatures.
NEUTRAL_OPTION_TYPES = frozenset({AT_NO_ATTACKS, AT_NO_BLOCKS, AT_SEARCH_FAIL, AT_OPTIONAL_NO, AT_OPTIONAL_YES})

# Decision types that are mana/plumbing by construction.
NON_STRATEGIC_DECISION_TYPES = frozenset({"pay_costs"})

# Pick statuses that mean the CLIENT answered, not the player.
AUTO_PICK_STATUSES = frozenset({"auto_declared", "arbitrary_auto", "autotap_auto"})


def _is_land_row(opt: dict) -> bool:
    return "Land" in (opt.get("type_line") or "")


def _is_unnamed(opt: dict) -> bool:
    """True when the row cannot be described to a model.

    ``card_facts`` falls back to ``"#<grpId>"`` for ids the card database does
    not know (ability ids, mode ids), and to ``None`` for rows with no id at
    all. Neutral rows ("attack with nobody") are legitimately nameless and are
    not counted here — they are handled by the caller.
    """
    if opt["action_type"] in NEUTRAL_OPTION_TYPES:
        return False
    name = opt.get("name")
    return name is None or (isinstance(name, str) and name.startswith("#"))


# Fields that make two rows DIFFERENT plays even when the card is the same.
# Without these, "block attacker A" and "block attacker B" with the same
# creature collapse to one option and the whole decision looks trivial.
_OPTION_DISCRIMINATORS = (
    "blocks_instance_id",
    "damage_recipient_seat",
    "damage_recipient_instance_id",
    "target_idx",
    "group_index",
    "assign_to_instance_id",
    "raw_id",
)


def option_identity(opt: dict) -> tuple:
    """Identity of a menu row for 'is there really more than one choice?'.

    Two copies of the same basic land in hand are two rows but one choice.
    Two different block assignments for one creature are one card but two
    choices.
    """
    return (opt["action_type"], opt["grp_id"]) + tuple(opt.get(k) for k in _OPTION_DISCRIMINATORS)


def is_land_drop_pick(rec: dict) -> bool:
    """Did the player's pick amount to 'play a land'? (explicitly rejected)"""
    pick = rec.get("real_pick") or {}
    if pick.get("action_type") not in PLAY_ACTION_TYPES:
        return False
    idx = pick.get("menu_index")
    menu = rec.get("real_menu") or []
    if idx is None or idx >= len(menu):
        # No row to inspect; a bare Play is a land drop far more often than not.
        return True
    return _is_land_row(menu[idx])


def classify_strategic(rec: dict) -> tuple[bool, str]:
    """Apply the strategic filter. Returns ``(keep, reason_if_dropped)``.

    The rules, in the order the requirement states them:
      1. drop mulligans                    (never emitted; asserted here)
      2. drop single-option menus          (<2 distinct choices)
      3. drop land-only / pass-only menus  (nothing but lands and/or Pass)
      4. keep >=2 meaningful options with >=1 cast/attack/block/activate/target

    Plus two rules the measured data forced:
      5. drop client-auto answers  (autoDeclare / arbitrary ordering / autotap)
         — those are solver output, not a player decision
      6. drop unresolved picks     (no response, or pick not in menu)
    """
    dtype = rec.get("decision_type")
    if dtype in NON_STRATEGIC_DECISION_TYPES:
        return False, "non_strategic_decision_type"

    pick = rec.get("real_pick") or {}
    status = pick.get("status")
    if status in AUTO_PICK_STATUSES:
        return False, f"client_auto_answer:{status}"
    if status not in ("ok", "implicit_pass"):
        return False, f"unresolved_pick:{status}"

    # Tapping a land for mana is never the answer to "what is the next best
    # action". MTGA emits it as its own priority window; the spell it pays for
    # shows up as a separate Cast decision, which is the one worth learning.
    if pick.get("action_type") in MANA_LEVEL_ACTION_TYPES:
        return False, "mana_ability_pick"

    menu = rec.get("real_menu") or []
    meaningful = [e for e in menu if e["action_type"] not in TRIVIAL_ACTION_TYPES]

    # Rule 2 — a menu with one real choice is not a decision.
    if len({option_identity(e) for e in meaningful}) < 2:
        return False, "single_option"

    # Rule 3 — nothing but lands (and Pass, already excluded above).
    non_land = [e for e in meaningful if not (_is_land_row(e) and e["action_type"] in PLAY_ACTION_TYPES)]
    if not non_land:
        return False, "land_only_menu"

    # Rule 4 — at least one genuinely strategic row.
    if not any(e["action_type"] in STRATEGIC_ACTION_TYPES for e in non_land):
        return False, "no_strategic_action"

    # Rule 6 — a lone-attacker combat is a reflex teacher, not a decision.
    # MEASURED on this corpus: with exactly one qualified attacker the player
    # declined 165 times and attacked 24 (87% "no"). Training on that produces
    # a "never attack" reflex the same way a 69%-land-drop corpus produced a
    # "always play the land" reflex. With >=2 qualified attackers the split is
    # 171 declined / 129 attacked, which is a real decision.
    if dtype == "declare_attackers":
        if (rec.get("decision_context") or {}).get("qualified_attacker_count", 0) < 2:
            return False, "single_attacker_combat"

    # Rule 7 — the choice has to be describable. Some requests identify their
    # options by ids the card database cannot name (abstract SelectN pickers;
    # modal option grpIds, which are mode/ability ids rather than cards). A
    # prompt listing unnamed rows asks the model to choose blind, so those are
    # not trainable no matter how strategic the underlying decision was.
    nameable = [e for e in meaningful if not _is_unnamed(e)]
    if len({option_identity(e) for e in nameable}) < 2:
        return False, "options_not_nameable"

    return True, ""


@dataclass
class MatchStats:
    """Per-match counters used for the corpus summary."""

    log_file: str = ""
    match_id: Optional[str] = None
    local_seat: Optional[int] = None
    seat_method: str = ""
    hand_visible_local: int = 0
    hand_visible_opp: int = 0
    super_format: str = ""
    variant: str = ""
    event_names: list[str] = field(default_factory=list)
    gre_messages: int = 0
    actions_available_total: int = 0
    actions_available_local: int = 0
    actions_available_non_local: int = 0
    emitted: int = 0
    no_response: int = 0
    pick_not_in_menu: int = 0
    is_bot_match: bool = False
    is_ranked_ladder: bool = False
    other_requests: Counter = field(default_factory=Counter)
    other_paired: Counter = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)
    # all-decision-types mode
    requests_by_type: Counter = field(default_factory=Counter)
    requests_local_by_type: Counter = field(default_factory=Counter)
    requests_nonlocal_by_type: Counter = field(default_factory=Counter)
    emitted_by_type: Counter = field(default_factory=Counter)
    excluded_by_type: Counter = field(default_factory=Counter)
    unsupported_requests: Counter = field(default_factory=Counter)


def extract_match(
    lm: LogMatch, log_file: str, *, start_of_main1_only: bool = False, all_decision_types: bool = False
) -> tuple[list[dict], MatchStats]:
    """Emit menu-groundtruth records for one match segment.

    ``all_decision_types`` extends extraction beyond ``ActionsAvailableReq``
    to every GRE request type in ``DECISION_TYPE_BY_MSG`` — combat, targeting,
    tutoring, modal choices. Those are where the strategic decisions live;
    restricting to ActionsAvailable is what made the previous corpus 69% land
    drops.
    """
    event_name = lm.event_names[0] if lm.event_names else None
    is_bot, is_ranked = classify_event(event_name)
    stats = MatchStats(
        log_file=log_file,
        match_id=lm.match_id,
        event_names=list(lm.event_names),
        gre_messages=len(lm.messages),
        is_bot_match=is_bot,
        is_ranked_ladder=is_ranked,
    )
    if not lm.messages:
        return [], stats

    seat, method = detect_local_seat_player_log(lm.messages)
    stats.local_seat = seat
    stats.seat_method = method
    if seat is None:
        stats.errors.append("could not determine local seat")
        return [], stats
    opp = opponent_seat(seat)

    local_team = lm.seat_team.get(seat)
    match_won: Optional[bool] = None
    if local_team is not None and lm.winning_team_ids:
        wins = sum(1 for t in lm.winning_team_ids if t == local_team)
        match_won = wins * 2 > len(lm.winning_team_ids)

    pairs = pair_requests_ordered(lm.messages)
    snap = GameStateSnapshot(local_seat_id=seat)
    step = ""
    game_number: Optional[int] = None
    main1_windows: Counter[int] = Counter()
    own_turn_seen: list[int] = []

    records: list[dict] = []
    decision_index = -1

    for i, m in enumerate(lm.messages):
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

        is_aa = m.msg_type == "GREMessageType_ActionsAvailableReq"
        if m.is_request and not is_aa:
            kind = m.msg_type.replace("GREMessageType_", "")
            stats.other_requests[kind] += 1
            if i in pairs:
                stats.other_paired[kind] += 1

        if not is_aa and not all_decision_types:
            continue
        if not is_aa:
            if m.msg_type in DECISION_TYPES_EXCLUDED:
                stats.excluded_by_type[m.msg_type.replace("GREMessageType_", "")] += 1
                continue
            if m.msg_type not in DECISION_TYPE_BY_MSG:
                if m.is_request:
                    stats.unsupported_requests[m.msg_type.replace("GREMessageType_", "")] += 1
                continue

        decision_index += 1
        dtype_name = DECISION_TYPE_BY_MSG[m.msg_type]
        stats.requests_by_type[dtype_name] += 1
        if is_aa:
            stats.actions_available_total += 1

        addressed = [int(x) for x in (m.payload.get("systemSeatIds") or [])]
        if addressed and seat not in addressed:
            # In a bot match the client simulates BOTH seats, so it logs the
            # bot's menus too. Emitting them would train on Sparky's play.
            stats.requests_nonlocal_by_type[dtype_name] += 1
            if is_aa:
                stats.actions_available_non_local += 1
            continue
        stats.requests_local_by_type[dtype_name] += 1
        if is_aa:
            stats.actions_available_local += 1

        is_own_turn = snap.active_player == seat
        is_main1 = snap.phase == "Phase_Main1"
        if is_own_turn and snap.turn_number not in own_turn_seen:
            own_turn_seen.append(snap.turn_number)

        window_ordinal = None
        if is_own_turn and is_main1 and is_aa:
            main1_windows[snap.turn_number] += 1
            window_ordinal = main1_windows[snap.turn_number]
        is_start_of_main1 = window_ordinal == 1
        if start_of_main1_only and not is_start_of_main1:
            continue

        resp = pairs.get(i)
        normalized = normalize_decision(m, resp, snap)
        if normalized is None:
            continue
        dtype_name, active, inactive, pick, decision_extra = normalized
        if is_aa:
            if pick.get("status") == "no_response":
                stats.no_response += 1
            elif pick.get("menu_index") is None:
                stats.pick_not_in_menu += 1

        # Cross-check the seat choice: only the recording client can see grpIds
        # in its own hand, so if the "opponent's" hand is the visible one the
        # seat is wrong and every record from this match is the wrong player's.
        stats.hand_visible_local += sum(1 for o in snap.hand(seat) if o.get("grpId"))
        stats.hand_visible_opp += sum(1 for o in snap.hand(opp) if o.get("grpId"))

        you = _battlefield_split(snap, seat)
        opp_board = _battlefield_split(snap, opp)
        hand = []
        for o in snap.hand(seat):
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
        meaningful = meaningful_options(active)

        rec = {
            # provenance (schema-compatible with menu_groundtruth + extras)
            "source_corpus": SOURCE_CORPUS,
            "source_license": SOURCE_LICENSE,
            "replay_file": log_file,
            "match_id": lm.match_id,
            "match_index": lm.index,
            "match_event": event_name,
            "is_bot_match": is_bot,
            "is_ranked_ladder": is_ranked,
            "match_won": match_won,
            "decision_type": dtype_name,
            "gre_request_type": m.msg_type.replace("GREMessageType_", ""),
            "decision_index": decision_index,
            "msg_id": m.msg_id,
            "game_state_id": m.payload.get("gameStateId"),
            "game_number": game_number,
            "super_format": stats.super_format,
            "variant": stats.variant,
            "local_seat": seat,
            "local_seat_method": method,
            "local_screen_name": "",  # scrubbed upstream for PII
            # timing
            "turn_number": snap.turn_number,
            "player_turn_index": player_turn_index,
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
            "meaningful_option_count": len(meaningful),
            # THE REAL PICK
            "real_pick": pick,
            # reconstructable state
            "state": {
                "life": {"you": snap.life(seat), "opp": snap.life(opp)},
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
                "opp_lands_grp_ids": [c["grp_id"] for c in opp_board["lands"]],
                "opp_creatures_grp_ids": [c["grp_id"] for c in opp_board["creatures"]],
                "opp_other_grp_ids": [c["grp_id"] for c in opp_board["other"]],
                "opp_hand_size": snap.hand_size(opp),
            },
        }
        if decision_extra:
            rec["decision_context"] = decision_extra
        keep, drop_reason = classify_strategic(rec)
        rec["is_strategic"] = keep
        rec["strategic_drop_reason"] = drop_reason or None
        rec["is_land_drop_pick"] = is_land_drop_pick(rec)
        records.append(rec)
        stats.emitted += 1
        stats.emitted_by_type[dtype_name] += 1

    return records, stats


# ---------------------------------------------------------------------------
# Rank / skill probe
# ---------------------------------------------------------------------------

_RANK_ORDER = ["Bronze", "Silver", "Gold", "Platinum", "Diamond", "Mythic"]


def summarize_ranks(ranks: list[RankSnapshot]) -> dict:
    """Aggregate every rank snapshot seen across the corpus.

    Answers "is player skill determinable at all?" — it is: MTGA logs a
    ``RankGetCombinedRankInfo`` response containing the account's constructed
    and limited tier.  ``constructedClass`` is *omitted* by MTGA when it holds
    its default (lowest) value, so an absent class is reported explicitly as
    ``"<omitted>"`` rather than silently dropped.
    """

    def _cls(v):
        return v if v else "<omitted>"

    constructed = Counter(_cls(r.constructed_class) for r in ranks)
    limited = Counter(_cls(r.limited_class) for r in ranks)
    per_file: dict[str, dict] = {}
    for r in ranks:
        cur = per_file.setdefault(r.log_file, {"constructed": set(), "limited": set(), "seasons": set()})
        cur["constructed"].add(_cls(r.constructed_class))
        cur["limited"].add(_cls(r.limited_class))
        if r.season is not None:
            cur["seasons"].add(int(r.season))

    def _best(names) -> Optional[str]:
        ranked = [n for n in names if n in _RANK_ORDER]
        if not ranked:
            return None
        return max(ranked, key=_RANK_ORDER.index)

    files_constructed = Counter()
    files_limited = Counter()
    for f, v in per_file.items():
        files_constructed[_best(v["constructed"]) or "<omitted>"] += 1
        files_limited[_best(v["limited"]) or "<omitted>"] += 1

    wins = sum(r.constructed_won or 0 for r in ranks if r.constructed_won is not None)
    losses = sum(r.constructed_lost or 0 for r in ranks if r.constructed_lost is not None)

    return {
        "determinable": bool(ranks),
        "how": (
            "MTGA logs a RankGetCombinedRankInfo response carrying "
            "constructedClass/constructedLevel and limitedClass/limitedLevel; "
            "the manasight scrubber does not strip it."
        ),
        "snapshots_seen": len(ranks),
        "files_with_rank_data": len(per_file),
        "constructed_class_snapshots": dict(constructed.most_common()),
        "limited_class_snapshots": dict(limited.most_common()),
        "best_constructed_class_per_file": dict(files_constructed.most_common()),
        "best_limited_class_per_file": dict(files_limited.most_common()),
        "seasons_seen": sorted({r.season for r in ranks if r.season is not None}),
        "constructed_matches_won_max": max(
            (r.constructed_won for r in ranks if r.constructed_won is not None), default=None
        ),
        "constructed_matches_lost_max": max(
            (r.constructed_lost for r in ranks if r.constructed_lost is not None), default=None
        ),
        "constructed_win_loss_sum_note": (f"sum over snapshots (double counts repeats): {wins}W/{losses}L"),
        "caveat": (
            "Rank is per-ACCOUNT-and-season, not per-match: it dates the "
            "session, it does not label an individual decision. "
            "constructedClass absent == MTGA default (lowest tier)."
        ),
    }


# ---------------------------------------------------------------------------
# Corpus summary
# ---------------------------------------------------------------------------


def _median(xs: list) -> Optional[float]:
    return xs[len(xs) // 2] if xs else None


def _write_attribution(path: Path, n: int) -> Path:
    """Every derived file must carry the upstream licence notice."""
    p = path.with_suffix(".ATTRIBUTION.txt")
    p.write_text(
        f"{ATTRIBUTION}\n\nSource: {SOURCE_REPO}\nLicense: {SOURCE_LICENSE}\n"
        f"Records: {n} in {path.name}\n",
        encoding="utf-8",
    )
    return p


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 2) if d else 0.0


def strategic_funnel(all_stats: list[MatchStats], records: list[dict]) -> dict:
    """The funnel the requirement asks for, stage by stage, with the Pass audit.

    Every number here is a count of records at a named stage, so the drop from
    "requests seen" to "genuinely strategic" is auditable rather than asserted.
    """
    req_by_type: Counter = Counter()
    local_by_type: Counter = Counter()
    nonlocal_by_type: Counter = Counter()
    excluded: Counter = Counter()
    unsupported: Counter = Counter()
    for s in all_stats:
        req_by_type.update(s.requests_by_type)
        local_by_type.update(s.requests_local_by_type)
        nonlocal_by_type.update(s.requests_nonlocal_by_type)
        excluded.update(s.excluded_by_type)
        unsupported.update(s.unsupported_requests)

    total = len(records)
    strategic = [r for r in records if r.get("is_strategic")]
    dropped = Counter(r.get("strategic_drop_reason") for r in records if not r.get("is_strategic"))

    by_type_emitted = Counter(r.get("decision_type") for r in records)
    by_type_strategic = Counter(r.get("decision_type") for r in strategic)

    # --- the Pass audit ----------------------------------------------------
    def _is_pass(r):
        return (r.get("real_pick") or {}).get("action_type") == "ActionType_Pass"

    pass_all = [r for r in records if _is_pass(r)]
    pass_kept = [r for r in strategic if _is_pass(r)]
    # A Pass that survived the single-option filter had real alternatives, so
    # it was a genuine "hold priority / do nothing now" choice, not a forced one.
    pass_kept_menu_sizes = sorted(r["meaningful_option_count"] for r in pass_kept)

    land_pick_all = sum(1 for r in records if r.get("is_land_drop_pick"))
    land_pick_strategic = sum(1 for r in strategic if r.get("is_land_drop_pick"))
    nonland_strategic = [r for r in strategic if not r.get("is_land_drop_pick")]

    return {
        "READ_THIS_FIRST": (
            "The requirement is 'out of this board, what is the next best action "
            "specifically'. Quote strategic_after_filter (or, strictest, "
            "strategic_nonland_pick) as the corpus size — NEVER records_emitted. "
            "The previous run's 69%-land-drop corpus came from quoting the wrong "
            "number."
        ),
        "stage_0_gre_requests_by_type": dict(req_by_type.most_common()),
        "stage_1_addressed_to_local_seat": dict(local_by_type.most_common()),
        "stage_1_dropped_other_seat": dict(nonlocal_by_type.most_common()),
        "stage_1_excluded_by_design": {
            "counts": dict(excluded.most_common()),
            "why": DECISION_TYPES_EXCLUDED,
        },
        "stage_1_unsupported_request_types": dict(unsupported.most_common()),
        "stage_2_records_emitted": total,
        "stage_2_emitted_by_decision_type": dict(by_type_emitted.most_common()),
        "stage_3_dropped_by_filter": dict(dropped.most_common()),
        "stage_3_filter_rules": [
            "drop mulligans (never extracted at all)",
            "drop client-auto answers (autoDeclare / arbitrary ordering / autotap)",
            "drop unresolved picks (no response, pick not in menu)",
            "drop single-option menus (<2 distinct meaningful choices)",
            "drop land-only / pass-only menus",
            "keep >=2 meaningful options with >=1 cast/attack/block/activate/target",
        ],
        "stage_4_strategic_after_filter": len(strategic),
        "stage_4_strategic_share_of_emitted_pct": _pct(len(strategic), total),
        "stage_4_strategic_by_decision_type": dict(by_type_strategic.most_common()),
        "stage_5_strategic_nonland_pick": len(nonland_strategic),
        "land_drops": {
            "land_drop_picks_all_records": land_pick_all,
            "land_drop_pct_all_records": _pct(land_pick_all, total),
            "land_drop_picks_surviving_filter": land_pick_strategic,
            "land_drop_pct_of_strategic": _pct(land_pick_strategic, len(strategic)),
            "note": (
                "The previous corpus was 69% land drops. A land drop survives this "
                "filter only when the menu ALSO offered a real spell/attack/activation "
                "— i.e. the player genuinely chose land over action. Use "
                "stage_5_strategic_nonland_pick to exclude even those."
            ),
        },
        "PASS_AUDIT": {
            "pass_picks_before_filter": len(pass_all),
            "pass_pct_before_filter": _pct(len(pass_all), total),
            "pass_picks_after_filter": len(pass_kept),
            "pass_pct_of_strategic": _pct(len(pass_kept), len(strategic)),
            "surviving_pass_meaningful_options_median": _median(pass_kept_menu_sizes),
            "surviving_pass_meaningful_options_min": (
                pass_kept_menu_sizes[0] if pass_kept_menu_sizes else None
            ),
            "justification": (
                "A Pass survives only if the menu held >=2 distinct meaningful options "
                "including a real cast/activate — the player COULD have acted and chose "
                "not to. That is 'hold up removal / do not overextend', a genuine "
                "strategic call. Forced passes (nothing else legal) are removed by the "
                "single-option rule. If the surviving Pass share is still high enough to "
                "teach a pass reflex, train on stage_5_strategic_nonland_pick with passes "
                "downsampled."
            ),
        },
        "strategic_pick_action_types": dict(
            Counter((r.get("real_pick") or {}).get("action_type") for r in strategic).most_common()
        ),
        "strategic_by_match_kind": {
            "vs_human": sum(1 for r in strategic if not r.get("is_bot_match")),
            "vs_builtin_bot": sum(1 for r in strategic if r.get("is_bot_match")),
            "ranked_ladder": sum(1 for r in strategic if r.get("is_ranked_ladder")),
        },
    }


def cap_pass_share(records: list[dict], max_share: float) -> tuple[list[dict], dict]:
    """Drop the easiest Pass records until Pass is at most ``max_share``.

    Passes are ranked by how many meaningful options the player declined —
    a pass with 8 live options is a real restraint decision, a pass with 2 is
    nearly forced. The easy ones go first, so the corpus keeps the passes that
    actually carry signal.
    """
    passes = [r for r in records if (r.get("real_pick") or {}).get("action_type") == "ActionType_Pass"]
    others = [r for r in records if (r.get("real_pick") or {}).get("action_type") != "ActionType_Pass"]
    if not passes:
        return records, {"applied": False, "reason": "no pass records"}
    # keep = max_share * (keep + len(others))  ->  solve for keep
    keep_n = int(len(others) * max_share / (1.0 - max_share)) if max_share < 1.0 else len(passes)
    keep_n = min(keep_n, len(passes))
    passes.sort(key=lambda r: r.get("meaningful_option_count", 0), reverse=True)
    kept = passes[:keep_n]
    out = others + kept
    out.sort(key=lambda r: (r.get("replay_file", ""), r.get("match_index", 0), r.get("decision_index", 0)))
    return out, {
        "applied": True,
        "max_pass_share": max_share,
        "pass_before": len(passes),
        "pass_kept": len(kept),
        "pass_dropped": len(passes) - len(kept),
        "records_after": len(out),
        "resulting_pass_share_pct": _pct(len(kept), len(out)),
        "kept_by": "highest meaningful_option_count first (hardest passes)",
    }


def summarize(
    all_stats: list[MatchStats], records: list[dict], rank_summary: dict, files: list[Path]
) -> dict:
    """Corpus-level counts, with the Pass caveat made unmissable."""
    menu_sizes = sorted(r["real_menu_size"] for r in records)
    spell_sizes = sorted(len(r["real_menu_key_spell"]) for r in records)
    meaningful_counts = sorted(r["meaningful_option_count"] for r in records)

    pick_types = Counter(r["real_pick"].get("action_type") for r in records)
    pick_status = Counter(r["real_pick"].get("status") for r in records)
    total = len(records)
    pass_picks = pick_types.get("ActionType_Pass", 0)

    ge2_options = sum(1 for r in records if r["real_menu_size"] >= 2)
    ge2_spell = sum(1 for r in records if len(r["real_menu_key_spell"]) >= 2)
    ge1_meaningful = sum(1 for r in records if r["meaningful_option_count"] >= 1)
    ge2_meaningful = sum(1 for r in records if r["meaningful_option_count"] >= 2)
    ge2_meaningful_nonpass = sum(
        1
        for r in records
        if r["meaningful_option_count"] >= 2 and r["real_pick"].get("action_type") != "ActionType_Pass"
    )

    unresolved_names = 0
    total_named = 0
    for r in records:
        for e in r["real_menu"]:
            if e["grp_id"] is None:
                continue
            total_named += 1
            if str(e["name"]).startswith("#"):
                unresolved_names += 1

    other_requests: Counter = Counter()
    other_paired: Counter = Counter()
    for s in all_stats:
        other_requests.update(s.other_requests)
        other_paired.update(s.other_paired)

    fmt = Counter(f"{s.super_format}/{s.variant}" for s in all_stats if s.super_format)
    events = Counter(e for s in all_stats for e in s.event_names)

    # Per-queue yield. This is the number that actually matters: a third of
    # the corpus is practice against MTGA's built-in bot.
    by_event: dict[str, dict] = {}
    for r in records:
        e = r.get("match_event") or "<unknown>"
        row = by_event.setdefault(
            e,
            {
                "menus": 0,
                "pass_picks": 0,
                "useful": 0,
                "is_bot_match": bool(r.get("is_bot_match")),
                "is_ranked_ladder": bool(r.get("is_ranked_ladder")),
            },
        )
        row["menus"] += 1
        if r["real_pick"].get("action_type") == "ActionType_Pass":
            row["pass_picks"] += 1
        elif r["meaningful_option_count"] >= 2:
            row["useful"] += 1
    by_event = dict(sorted(by_event.items(), key=lambda kv: -kv[1]["menus"]))

    def _slice(pred) -> dict:
        sub = [r for r in records if pred(r)]
        useful = sum(
            1
            for r in sub
            if r["meaningful_option_count"] >= 2 and r["real_pick"].get("action_type") != "ActionType_Pass"
        )
        return {"menus": len(sub), "useful_decisions": useful}

    return {
        "source": {
            "corpus": SOURCE_CORPUS,
            "repo": SOURCE_REPO,
            "license": SOURCE_LICENSE,
            "license_files": ["LICENSE-MIT", "LICENSE-APACHE"],
            "attribution_required": True,
            "attribution": ATTRIBUTION,
        },
        "files_scanned": len(files),
        "matches_found": len(all_stats),
        "matches_with_menus": sum(1 for s in all_stats if s.emitted),
        "matches_with_errors": sum(1 for s in all_stats if s.errors),
        "gre_messages": sum(s.gre_messages for s in all_stats),
        "format_by_match": dict(fmt.most_common()),
        "event_by_match": dict(events.most_common()),
        "matches_vs_bot": sum(1 for s in all_stats if s.is_bot_match),
        "matches_ranked_ladder": sum(1 for s in all_stats if s.is_ranked_ladder),
        "local_seat_detection": {
            "method_by_match": dict(Counter(s.seat_method for s in all_stats if s.seat_method).most_common()),
            "matches_where_opponent_hand_was_more_visible": sum(
                1 for s in all_stats if s.hand_visible_opp > s.hand_visible_local
            ),
            "note": (
                "Cross-check: only the recording client sees grpIds in its own "
                "hand. A non-zero count means the seat detector picked the "
                "wrong player for that match and its records are the "
                "OPPONENT's decisions. ConnectResp is NOT used as the primary "
                "signal here — it named the wrong seat in 14 of the 70 "
                "segments that carry one."
            ),
        },
        "actions_available_total": sum(s.actions_available_total for s in all_stats),
        "actions_available_addressed_to_local": sum(s.actions_available_local for s in all_stats),
        "actions_available_dropped_not_local_seat": sum(s.actions_available_non_local for s in all_stats),
        "menus_emitted": total,
        "menus_with_resolved_pick": sum(1 for r in records if r["real_pick"].get("menu_index") is not None),
        "menus_no_response": sum(s.no_response for s in all_stats),
        "menus_pick_not_in_menu": sum(s.pick_not_in_menu for s in all_stats),
        "option_counts": {
            "menus_with_ge2_options": ge2_options,
            "menus_with_ge2_spell_level_options": ge2_spell,
            "menus_with_ge1_meaningful_option": ge1_meaningful,
            "menus_with_ge2_meaningful_options": ge2_meaningful,
            "menus_with_ge2_meaningful_options_and_nonpass_pick": ge2_meaningful_nonpass,
            "definition": (
                "'meaningful' excludes ActionType_Pass, "
                "ActionType_Activate_Mana and ActionType_FloatMana — "
                "i.e. entries that represent an actual card decision."
            ),
        },
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
        "meaningful_option_count": {
            "min": meaningful_counts[0] if meaningful_counts else None,
            "median": _median(meaningful_counts),
            "max": meaningful_counts[-1] if meaningful_counts else None,
            "mean": round(sum(meaningful_counts) / len(meaningful_counts), 2) if meaningful_counts else None,
        },
        "pick_action_types": dict(pick_types.most_common()),
        "pick_status": dict(pick_status.most_common()),
        "pick_match_quality": dict(Counter(r["real_pick"].get("match") for r in records).most_common()),
        "yield_by_event": by_event,
        "BOT_MATCH_CAVEAT": {
            "vs_builtin_bot": _slice(lambda r: r.get("is_bot_match")),
            "vs_human": _slice(lambda r: not r.get("is_bot_match")),
            "ranked_ladder_only": _slice(lambda r: r.get("is_ranked_ladder")),
            "note": (
                "MTGA's AIBotMatch queues are practice against Sparky. Both "
                "seats run on the client, so the bot's own menus appear in "
                "the log (dropped here via the local-seat filter), and the "
                "human's decisions were made against an opponent that does "
                "not punish mistakes. Filter on is_bot_match=false for any "
                "imitation-training use."
            ),
        },
        "PASS_CAVEAT": {
            "pass_picks": pass_picks,
            "pass_pct_of_menus": round(100.0 * pass_picks / total, 2) if total else None,
            "note": (
                "A menu answered with Pass teaches priority discipline, not "
                "card evaluation. Use menus_with_ge2_meaningful_options_and_"
                "nonpass_pick as the honest denominator for imitation "
                "training, NOT menus_emitted."
            ),
        },
        "other_paired_decisions": {
            "requests_by_type": dict(other_requests.most_common()),
            "paired_with_response_by_type": dict(other_paired.most_common()),
            "total_requests": sum(other_requests.values()),
            "total_paired": sum(other_paired.values()),
        },
        "card_name_resolution": {
            "menu_entries_with_grp_id": total_named,
            "unresolved": unresolved_names,
            "resolved_pct": round(100.0 * (total_named - unresolved_names) / total_named, 2)
            if total_named
            else None,
        },
        "player_skill": rank_summary,
    }


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def download_corpus(root: Path = DEFAULT_CORPUS_ROOT) -> Path:
    """Fetch the corpus zip and extract it under ``root``.

    Returns the directory holding the ``*.log.gz`` files.  Kept out of the
    repo on purpose: 43 MB of logs must never be committed.
    """
    import urllib.request

    root.mkdir(parents=True, exist_ok=True)
    zip_path = root / "manasight-corpus.zip"
    print(f"downloading {SOURCE_ZIP} -> {zip_path}", file=sys.stderr)
    with urllib.request.urlopen(SOURCE_ZIP, timeout=300) as resp:  # noqa: S310
        data = resp.read()
    zip_path.write_bytes(data)
    print(f"  {len(data) / 1e6:.1f} MB", file=sys.stderr)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(root)
    for cand in sorted(root.glob("manasight-corpus*/corpus")):
        return cand
    raise RuntimeError(f"no corpus/ directory found under {root} after extraction")


def corpus_file_digests(files: list[Path]) -> list[dict]:
    """sha256 + size per corpus file, so the output is reproducible/auditable."""
    out = []
    for f in files:
        h = hashlib.sha256()
        with open(f, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        out.append({"file": f.name, "sha256": h.hexdigest(), "size_bytes": f.stat().st_size})
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Ingest manasight-corpus into the menu-groundtruth schema")
    p.add_argument(
        "--corpus-dir",
        type=Path,
        default=DEFAULT_CORPUS_DIR,
        help=f"Directory of *.log.gz files (default: {DEFAULT_CORPUS_DIR})",
    )
    p.add_argument(
        "--download",
        action="store_true",
        help="Download + extract the corpus first (43 MB, kept outside the repo)",
    )
    p.add_argument(
        "--out", type=Path, default=DEFAULT_OUT, help=f"Output JSONL path (default: {DEFAULT_OUT})"
    )
    p.add_argument("--summary-out", type=Path, default=None, help="Optional JSON path for the summary block")
    p.add_argument(
        "--start-of-main1-only",
        action="store_true",
        help="Emit only start-of-own-Main-1 windows (menu_groundtruth's headline slice)",
    )
    p.add_argument(
        "--all-decision-types",
        action="store_true",
        help="Extract EVERY supported GRE request type (combat, targeting, tutoring, "
        "modal choices), not just ActionsAvailableReq. This is where the strategic "
        "decisions live; ActionsAvailable-only is what made the corpus 69%% land drops.",
    )
    p.add_argument(
        "--strategic-out",
        type=Path,
        default=None,
        help="Also write the strategic-filtered subset here (is_strategic == true)",
    )
    p.add_argument(
        "--strategic-nonland-out",
        type=Path,
        default=None,
        help="Also write the strategic subset whose PICK is not a land drop",
    )
    p.add_argument(
        "--strategic-action-out",
        type=Path,
        default=None,
        help="Also write the strictest slice: strategic, pick is neither a land drop "
        "NOR a Pass — every record answers 'what action do I take' with an action. "
        "This is the anti-reflex corpus.",
    )
    p.add_argument(
        "--max-pass-share",
        type=float,
        default=None,
        help="Cap Pass picks at this share (0-1) of --strategic-out by dropping the "
        "excess (lowest-option-count passes first, keeping the hardest ones). "
        "Guards against training a pass reflex the way 69%% land drops trained a "
        "land reflex.",
    )
    p.add_argument("--limit", type=int, default=None, help="Only process the first N log files")
    p.add_argument(
        "--rank-probe-only",
        action="store_true",
        help="Report player rank/skill determinability and exit; no extraction",
    )
    p.add_argument(
        "--no-digests", action="store_true", help="Skip sha256 hashing of the source logs (faster)"
    )
    args = p.parse_args(argv)

    corpus_dir = args.corpus_dir
    if args.download:
        corpus_dir = download_corpus()
    if not corpus_dir.is_dir():
        print(
            f"corpus dir not found: {corpus_dir}\nrun with --download to fetch it ({SOURCE_REPO})",
            file=sys.stderr,
        )
        return 2

    files = sorted(corpus_dir.glob("*.log.gz")) + sorted(corpus_dir.glob("*.log"))
    if args.limit:
        files = files[: args.limit]
    if not files:
        print(f"no *.log.gz files under {corpus_dir}", file=sys.stderr)
        return 2

    all_records: list[dict] = []
    all_stats: list[MatchStats] = []
    all_ranks: list[RankSnapshot] = []

    for i, f in enumerate(files, 1):
        try:
            matches, ranks = parse_player_log(f)
        except Exception as exc:  # noqa: BLE001 - one bad log must not abort the corpus
            print(f"  [{i}/{len(files)}] {f.name}: PARSE FAILED: {exc}", file=sys.stderr)
            continue
        all_ranks.extend(ranks)
        if args.rank_probe_only:
            print(f"  [{i}/{len(files)}] {f.name}: {len(ranks)} rank snapshots", file=sys.stderr)
            continue
        n_before = len(all_records)
        for lm in matches:
            recs, stats = extract_match(
                lm,
                f.name,
                start_of_main1_only=args.start_of_main1_only,
                all_decision_types=args.all_decision_types,
            )
            all_records.extend(recs)
            all_stats.append(stats)
        print(
            f"  [{i}/{len(files)}] {f.name}: {len(matches)} match(es), {len(all_records) - n_before} menus",
            file=sys.stderr,
        )

    rank_summary = summarize_ranks(all_ranks)
    if args.rank_probe_only:
        print(json.dumps({"player_skill": rank_summary}, indent=2))
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        for r in all_records:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")

    summary = summarize(all_stats, all_records, rank_summary, files)
    summary["output_jsonl"] = str(args.out)
    summary["records_written"] = len(all_records)
    summary["start_of_main1_only"] = bool(args.start_of_main1_only)
    summary["all_decision_types"] = bool(args.all_decision_types)
    summary["STRATEGIC_FUNNEL"] = strategic_funnel(all_stats, all_records)

    strategic = [r for r in all_records if r.get("is_strategic")]
    if args.max_pass_share is not None:
        strategic, capped = cap_pass_share(strategic, args.max_pass_share)
        summary["STRATEGIC_FUNNEL"]["pass_cap"] = capped
    if args.strategic_out:
        args.strategic_out.parent.mkdir(parents=True, exist_ok=True)
        with open(args.strategic_out, "w", encoding="utf-8") as fh:
            for r in strategic:
                fh.write(json.dumps(r, separators=(",", ":")) + "\n")
        summary["strategic_jsonl"] = str(args.strategic_out)
        summary["strategic_records_written"] = len(strategic)
        _write_attribution(args.strategic_out, len(strategic))
    if args.strategic_nonland_out:
        nonland = [r for r in strategic if not r.get("is_land_drop_pick")]
        args.strategic_nonland_out.parent.mkdir(parents=True, exist_ok=True)
        with open(args.strategic_nonland_out, "w", encoding="utf-8") as fh:
            for r in nonland:
                fh.write(json.dumps(r, separators=(",", ":")) + "\n")
        summary["strategic_nonland_jsonl"] = str(args.strategic_nonland_out)
        summary["strategic_nonland_records_written"] = len(nonland)
        _write_attribution(args.strategic_nonland_out, len(nonland))
    if args.strategic_action_out:
        action_only = [
            r
            for r in strategic
            if not r.get("is_land_drop_pick")
            and (r.get("real_pick") or {}).get("action_type") != "ActionType_Pass"
        ]
        args.strategic_action_out.parent.mkdir(parents=True, exist_ok=True)
        with open(args.strategic_action_out, "w", encoding="utf-8") as fh:
            for r in action_only:
                fh.write(json.dumps(r, separators=(",", ":")) + "\n")
        summary["strategic_action_jsonl"] = str(args.strategic_action_out)
        summary["strategic_action_records_written"] = len(action_only)
        summary["STRATEGIC_FUNNEL"]["stage_6_strategic_action_only"] = len(action_only)
        _write_attribution(args.strategic_action_out, len(action_only))

    if not args.no_digests:
        summary["source_files"] = corpus_file_digests(files)

    attrib_path = args.out.with_suffix(".ATTRIBUTION.txt")
    attrib_path.write_text(
        f"{ATTRIBUTION}\n\nSource: {SOURCE_REPO}\nLicense: {SOURCE_LICENSE}\n"
        f"Records: {len(all_records)} in {args.out.name}\n",
        encoding="utf-8",
    )

    print("\n" + json.dumps(summary, indent=2, default=str))
    if args.summary_out:
        args.summary_out.parent.mkdir(parents=True, exist_ok=True)
        args.summary_out.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        print(f"\nsummary -> {args.summary_out}")
    print(f"\nJSONL -> {args.out}  ({len(all_records)} records)")
    print(f"attribution -> {attrib_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
