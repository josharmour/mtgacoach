#!/usr/bin/env python3
"""Parse MageZero XMage text logs into the shared decisions JSONL schema.

Usage:
    python3 tools/training/parse_magezero_log.py \\
        --log /home/joshu/mz_train_smoke.log \\
        --log /home/joshu/mz_logs/*.log \\
        --out tools/training/data/magezero_decisions.jsonl \\
        --report
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

# ── Regex patterns ──────────────────────────────────────────────────────

RE_THREAD = re.compile(r"\[(pool-3-thread-\d+)\]")
RE_DIE_ROLL = re.compile(r"Player ([AB]) won the die roll")

RE_LOG_LIFE = re.compile(
    r"\[(\d+):([^:]+):(\w+)\]"
    r"\[player PlayerA:(\d+)\]\[player PlayerB:(\d+)\]"
)

RE_CHOSE_ACTION = re.compile(
    r"\[(\d+):([^:]+):(\w+)\]"
    r"chose action:(.+) success ratio: (-?[\d.eE+-]+)"
)

RE_POOL = re.compile(r"(\w+)(\d+)pool= actions: (.*?)(?:  )?(?:=>|$)")
RE_POOL_TOP = re.compile(r"(\w+)(\d+) \(top: [^)]+\)pool= actions: (.*?)(?:  )?(?:=>|$)")

RE_PLAYABLE = re.compile(r"playable abilities: \[(.*?)\]")
RE_HAND = re.compile(r"-> Hand: \[(.*?)\]")
RE_PERMANENTS = re.compile(r"-> Permanents: \[(.*?)\]")
RE_PLAYER_LIFE = re.compile(r"\[(Player[AB])\], life = (\d+)")

# The emitter tag at end of line disambiguates the two battlefield-block
# grammars (#430). `ComputerPlayerMCTS.printBattlefieldScore` blocks are
#   [PlayerX] header -> Hand -> Permanents (SELF) -> Permanents (OPPONENT)
# while `GameStateEvaluator2.printBattlefield` blocks are
#   [PlayerX] header -> Hand -> Permanents (header player's own) -> Graveyard
# The old parser ignored both the header and the emitter and paired every
# Permanents line on the thread into an anonymous [self, opp] buffer, so each
# one-line evaluator block shifted the pairing frame permanently — swapping
# boards for the rest of the game. Measured on mz_train_smoke.log: 40,860
# two-line blocks vs 34,912 one-line blocks (4,337 of them PlayerB-headed).
RE_EMITTER = re.compile(r"=>\[pool-3-thread-\d+\]\s+(\S+)")
EMITTER_MCTS = "ComputerPlayerMCTS.printBattlefieldScore"
EMITTER_EVAL = "GameStateEvaluator2.printBattlefield"

RE_WIN_RATE = re.compile(r"Player A win rate: ([\d.]+)% \((\d+)/(\d+)\)")
RE_SIMULATING = re.compile(r"Simulating (\d+) games")
RE_TIMESTAMP = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

RE_ACTION_TUPLE = re.compile(r"\[([^\]]+?) score: (-?[\d.eE+-]+) count: (\d+)\]")

# Recovery accounting from the most recent parse_log() call. Kept module-level
# rather than returned, because parse_log's 2-tuple signature has several
# callers; the report reads this to surface the pass rate, which the B2
# tripwire depends on being visible.
LAST_PARSE_STATS: dict[str, int] = {"recovered_pass": 0, "ambiguous_unconsumed": 0}


# ── Phase → decision_kind mapping ───────────────────────────────────────

DECISION_KIND_MAP = {
    "PRECOMBAT_MAIN": "priority",
    "POSTCOMBAT_MAIN": "priority",
    "UPKEEP": "priority",
    "DRAW": "priority",
    "BEGINNING": "priority",
    "BEGIN_COMBAT": "priority",
    "END_COMBAT": "priority",
    "END_TURN": "priority",
    "DECLARE_ATTACKERS": "attackers",
    "DECLARE_BLOCKERS": "blockers",
}

# Known opponent order for the smoke run (gen0: Standard-MonoR/G/B/W/U, gen1: same order)
SMOKE_OPPONENTS_GEN0 = ["Standard-MonoR", "Standard-MonoG", "Standard-MonoB",
                         "Standard-MonoW", "Standard-MonoU"]


class _PendingPool:
    """An MCTS visit distribution awaiting its outcome line."""

    __slots__ = ("actions", "phase_code", "turn")

    def __init__(self, actions: list[tuple], phase_code: str, turn: int):
        self.actions = actions
        self.phase_code = phase_code
        self.turn = turn


def is_pass_action(name: str) -> bool:
    """True when this action name is XMage's pass/decline option."""
    return name.strip().lower() in {"pass", "false", "no", "done"}


def _emit_pass_decision(
    state: _ThreadState,
    pending: _PendingPool,
    menu: list[str],
    decisions: list[dict],
    stats: dict[str, int],
) -> None:
    """Emit the decision for a pool line no `chose action` ever consumed.

    XMage logs `chose action:` ONLY for non-pass actions — 0 of 9,789 emitted
    rows had a pass in `chosen`, while 100% of menus offered one. Those windows
    were dropped, discarding 8,595 of 18,384 deliberated decisions on
    mz_train_smoke.log: precisely the cases where the search concluded that the
    correct play was to DO NOTHING. Training on the remainder teaches a model
    that never holds priority, never keeps an instant up, and never declines to
    overextend — the mirror image of the pass-reflex that cost a prior week.

    The label is recoverable without guessing: the MCTS argmax of the
    un-consumed pool IS the search's conclusion. When that argmax is not a pass
    action the window is genuinely ambiguous, so it is counted and skipped
    rather than labelled — never fabricate a decision.
    """
    actions = pending.actions
    top_name, _, _ = max(actions, key=lambda a: a[2])
    if not is_pass_action(top_name):
        stats["ambiguous_unconsumed"] += 1
        return

    kind = classify_kind(pending.phase_code, actions)
    decision = {
        "game_id": state.game_id,
        "turn": pending.turn,
        "phase": pending.phase_code,
        "active_life": state.life_a,
        "opp_life": state.life_b,
        "hand": [],
        "battlefield_self": [],
        "battlefield_opp": [],
        # The pool's own action names ARE the options the search weighed, so
        # they are the faithful menu when no `playable abilities:` line landed.
        "menu": list(menu) if menu else [name for name, _, _ in actions],
        "chosen": top_name,
        "mcts_counts": {name: count for name, _, count in actions},
        "actor": "PlayerA",
        "outcome": "unknown",
        "decision_kind": kind,
        "_thread": state.thread_id,
        "_game_seq": state.game_seq,
        "_log": state.log_name,
        "session": "",
        "_recovered_pass": True,
    }
    state.game_decisions.append(decision)
    decisions.append(decision)
    stats["recovered_pass"] += 1


def classify_kind(phase_code: str, pool_actions: list[tuple]) -> str:
    base = DECISION_KIND_MAP.get(phase_code, "priority")
    names = {a[0] for a in pool_actions}
    if names <= {"true", "false"}:
        return "binary"
    return base


# ── Parsing helpers ─────────────────────────────────────────────────────

def parse_pool_actions(text: str) -> list[tuple[str, float, int]]:
    return [(m[0], float(m[1]), int(m[2])) for m in RE_ACTION_TUPLE.findall(text)]


def parse_card_list(text: str) -> list[str]:
    text = text.strip()
    return [c.strip() for c in text.split(";")] if text else []


def parse_permanents(text: str) -> list[dict[str, Any]]:
    text = text.strip()
    if not text:
        return []
    result = []
    for entry in text.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        tapped = ",tapped" in entry
        name = entry.replace(",tapped", "").strip()
        name = re.sub(r":\d+$", "", name).strip()
        result.append({"name": name, "tapped": tapped})
    return result


# ── Session detection ───────────────────────────────────────────────────

class SessionInfo:
    def __init__(self, seq: int, start_ts: str, opponent: str, n_games: int):
        self.seq = seq
        self.start_ts = start_ts
        self.opponent = opponent
        self.n_games = n_games
        self.win_rate: float | None = None
        self.n_wins: int | None = None
        self.n_total: int | None = None
        # Number of games per thread in this session
        self.games_per_thread = self._compute_per_thread()

    def _compute_per_thread(self) -> dict[str, int]:
        """Distribute n_games across 6 threads."""
        n = self.n_total or self.n_games
        base = n // 6
        extra = n % 6
        result = {}
        for i in range(1, 7):
            result[f"pool-3-thread-{i}"] = base + (1 if i <= extra else 0)
        return result

    @property
    def label(self) -> str:
        return f"session{self.seq}_UWTempo_vs_{self.opponent}"


def detect_sessions(lines: list[str], is_smoke_log: bool = False) -> list[SessionInfo]:
    opponents = list(SMOKE_OPPONENTS_GEN0)
    sessions: list[SessionInfo] = []
    seq = 0
    opp_idx = 0

    for line in lines:
        sm = RE_SIMULATING.search(line)
        if sm:
            ts = _extract_ts(line, "unknown")
            n_games = int(sm.group(1))
            opponent = opponents[opp_idx % len(opponents)] if is_smoke_log else f"session{seq}"
            sess = SessionInfo(seq, ts, opponent, n_games)
            sessions.append(sess)
            seq += 1
            opp_idx += 1
            continue

        wm = RE_WIN_RATE.search(line)
        if wm and sessions:
            current = sessions[-1]
            current.win_rate = float(wm.group(1))
            current.n_wins = int(wm.group(2))
            current.n_total = int(wm.group(3))
            current.games_per_thread = current._compute_per_thread()

    return sessions


def _extract_ts(line: str, default: str) -> str:
    m = RE_TIMESTAMP.search(line)
    return m.group(1) if m else default


# ── Main parser ─────────────────────────────────────────────────────────

def parse_log(log_path: str) -> tuple[list[dict], list[SessionInfo]]:
    log_name = os.path.basename(log_path)
    is_smoke = "smoke" in log_name

    with open(log_path, encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()

    sessions = detect_sessions(all_lines, is_smoke_log=is_smoke)

    # Per-thread state
    threads: dict[str, _ThreadState] = {}
    thread_game_seq: dict[str, int] = {}

    decisions: list[dict] = []
    pending_pool: dict[str, _PendingPool] = {}
    pending_menu: dict[str, list[str]] = {}
    # Recovery accounting, reported by --report so the pass rate is visible.
    pass_stats: dict[str, int] = {"recovered_pass": 0, "ambiguous_unconsumed": 0}

    for line in all_lines:
        line = line.rstrip("\n\r")
        tm = RE_THREAD.search(line)
        if not tm:
            continue
        thread_id = tm.group(1)

        state = threads.get(thread_id)
        if state is None:
            thread_game_seq[thread_id] = 0
            state = _ThreadState(thread_id, log_name, thread_game_seq[thread_id] + 1)
            threads[thread_id] = state

        # ── Die roll: game boundary ──────────────────────────────
        dm = RE_DIE_ROLL.search(line)
        if dm:
            # Flush before finalizing: a pool still pending at the game
            # boundary is the last window of the OUTGOING game.
            prev = pending_pool.pop(thread_id, None)
            if prev is not None:
                _emit_pass_decision(state, prev, pending_menu.get(thread_id, []), decisions, pass_stats)
            _finalize_game(state, decisions)
            thread_game_seq[thread_id] += 1
            state.start_new_game(thread_game_seq[thread_id])
            pending_menu.pop(thread_id, None)
            continue

        # ── logLife (turn/phase/life totals) ─────────────────────
        lm = RE_LOG_LIFE.search(line)
        if lm:
            state.on_log_life(int(lm.group(1)), lm.group(2), lm.group(3),
                              int(lm.group(4)), int(lm.group(5)))
            continue

        # ── MCTS pool distribution ───────────────────────────────
        pool_match = RE_POOL.search(line) or RE_POOL_TOP.search(line)
        if pool_match:
            actions = parse_pool_actions(pool_match.group(3))
            if actions:
                # A pool still pending when the NEXT one arrives was never
                # consumed by a `chose action` line — because XMage does not log
                # a chosen Pass. Recover it (see _emit_pass_decision) instead of
                # dropping it, which silently discarded 8,595 of 18,384
                # deliberated decisions on mz_train_smoke.log — every one of
                # them a case where the search concluded "do nothing".
                prev = pending_pool.pop(thread_id, None)
                if prev is not None:
                    _emit_pass_decision(
                        state, prev, pending_menu.pop(thread_id, []), decisions, pass_stats
                    )
                pending_pool[thread_id] = _PendingPool(
                    actions=actions,
                    phase_code=pool_match.group(1),
                    turn=state.last_turn,
                )
            continue

        # ── Playable abilities (legal menu) ──────────────────────
        pm = RE_PLAYABLE.search(line)
        if pm:
            menu = [m.strip() for m in pm.group(1).split(",")]
            pending_menu[thread_id] = menu
            continue

        # ── Chose action (the decision label) ────────────────────
        cm = RE_CHOSE_ACTION.search(line)
        if cm:
            turn = int(cm.group(1))
            phase_code = cm.group(3)
            chosen = cm.group(4).strip()

            pending = pending_pool.pop(thread_id, None)
            menu = pending_menu.pop(thread_id, [])

            if pending is None:
                continue  # Minimax decision, skip
            pool_actions = pending.actions

            kind = classify_kind(phase_code, pool_actions)
            mcts_counts = {name: count for name, _, count in pool_actions}

            decision = {
                "game_id": state.game_id,
                "turn": turn,
                "phase": phase_code,
                "active_life": state.life_a,
                "opp_life": state.life_b,
                "hand": [],
                "battlefield_self": [],
                "battlefield_opp": [],
                "menu": list(menu),
                "chosen": chosen,
                "mcts_counts": mcts_counts,
                "actor": "PlayerA",
                "outcome": "unknown",
                "decision_kind": kind,
                "_thread": thread_id,
                "_game_seq": state.game_seq,
                "_log": log_name,
                "session": "",
            }
            state.game_decisions.append(decision)
            decisions.append(decision)
            continue

        # ── Player life: also opens a battlefield block (#430) ───
        pl = RE_PLAYER_LIFE.search(line)
        if pl:
            em = RE_EMITTER.search(line)
            state.on_block_header(pl.group(1), int(pl.group(2)), em.group(1) if em else None)
            continue

        # ── Hand: PlayerA's backfills the decision; PlayerB's is the
        # OPPONENT'S HAND (GameStateEvaluator2 prints both players' views)
        # and must never enter a training row — the old parser backfilled
        # it blindly, which is both wrong data and an L4 leak. ──────────
        hm = RE_HAND.search(line)
        if hm:
            cards = parse_card_list(hm.group(1))
            state.on_hand_line(cards)
            continue

        # ── Permanents: attributed via block header + emitter ────
        perm = RE_PERMANENTS.search(line)
        if perm:
            em = RE_EMITTER.search(line)
            state.on_permanents_line(
                parse_permanents(perm.group(1)),
                em.group(1) if em else None,
            )
            continue

    # Flush any pool still pending at EOF, then finalize remaining games.
    for tid, prev in list(pending_pool.items()):
        st = threads.get(tid)
        if st is not None:
            _emit_pass_decision(st, prev, pending_menu.get(tid, []), decisions, pass_stats)
    pending_pool.clear()

    # Finalize remaining games
    for state in threads.values():
        _finalize_game(state, decisions)

    # Enrich with sessions using per-thread game counts
    _enrich_sessions(decisions, sessions)

    LAST_PARSE_STATS.update(pass_stats)

    return decisions, sessions


class _ThreadState:
    def __init__(self, thread_id: str, log_name: str, initial_seq: int):
        self.thread_id = thread_id
        self.log_name = log_name
        self.game_seq = initial_seq
        self.game_id = f"{log_name}:{thread_id}:{initial_seq}"
        self.game_decisions: list[dict] = []
        self.life_a = 20
        self.life_b = 20
        self.latest_hand: list[str] = []
        # Last known board per player (#430). "Self" for a decision row is
        # always PlayerA — the MCTS/primary player; decisions carry
        # actor="PlayerA" — so battlefield_self is boards["PlayerA"].
        self.boards: dict[str, list[dict]] = {"PlayerA": [], "PlayerB": []}
        self.last_log_life_a: int | None = None
        self.last_log_life_b: int | None = None
        # Last turn seen on a logLife line. A recovered pass row has no
        # `chose action` line to read the turn from, so it uses this.
        self.last_turn = 0
        # Open battlefield block: which player's header we are inside, which
        # emitter printed it, and how many Permanents lines it has produced.
        self._block_player: str | None = None
        self._block_emitter: str | None = None
        self._block_perm_lines = 0
        self._block_flushed = False

    def start_new_game(self, game_seq: int):
        self.game_seq = game_seq
        self.game_id = f"{self.log_name}:{self.thread_id}:{game_seq}"
        self.game_decisions = []
        self.life_a = 20
        self.life_b = 20
        self.latest_hand = []
        self.boards = {"PlayerA": [], "PlayerB": []}
        self.last_log_life_a = None
        self.last_log_life_b = None
        self.last_turn = 0
        self._block_player = None
        self._block_emitter = None
        self._block_perm_lines = 0
        self._block_flushed = False

    def on_log_life(self, turn: int, phase_name: str, phase_code: str,
                    life_a: int, life_b: int):
        self.last_turn = turn
        self.life_a = life_a
        self.life_b = life_b
        self.last_log_life_a = life_a
        self.last_log_life_b = life_b

    def on_block_header(self, player: str, life: int, emitter: str | None):
        # A new header closes the previous block. If that block produced
        # board updates that were never flushed into a decision (possible
        # only for a one-line block with no emitter tag, where we cannot
        # tell "complete" from "awaiting the opponent line"), flush now.
        if self._block_perm_lines > 0 and not self._block_flushed:
            _backfill_battlefield(self)
        if player == "PlayerA":
            self.life_a = life
        else:
            self.life_b = life
        self._block_player = player
        self._block_emitter = emitter
        self._block_perm_lines = 0
        self._block_flushed = False

    def on_hand_line(self, cards: list[str]):
        # Only the primary player's hand may reach a training row. A PlayerB
        # hand line (GameStateEvaluator2 prints the opponent's view too) is
        # the opponent's hidden hand: backfilling it poisons the `hand` field
        # AND leaks information the model must never see (leak class L4).
        if self._block_player == "PlayerB":
            return
        self.latest_hand = cards
        _backfill_hand(self, cards)

    def on_permanents_line(self, permanents: list[dict], emitter: str | None):
        # Attribution is positional WITHIN the current header block, which is
        # correct under both known grammars (and self-verifying on the real
        # log: 10,215 MCTS headers x 2 lines = 20,430; 8,728 evaluator
        # headers x 1 = 8,728, no other emitter prints Permanents):
        #   line 1 after a [PlayerX] header = X's own board (both emitters)
        #   line 2 (MCTS only)             = the other player's board
        # A line with NO preceding header is unattributable — skip it rather
        # than guess; anonymous pairing was exactly the old bug.
        player = self._block_player
        if player is None:
            return

        other = "PlayerB" if player == "PlayerA" else "PlayerA"
        if self._block_perm_lines == 0:
            self.boards[player] = list(permanents)
        elif self._block_perm_lines == 1:
            self.boards[other] = list(permanents)
        else:
            # A third line under one header would be a new grammar — ignore.
            return
        self._block_perm_lines += 1

        # Flush into the pending decision only when the block is COMPLETE —
        # flushing after an MCTS block's first line would hand the decision a
        # fresh self board and a stale (or empty) opponent board. An evaluator
        # block is complete at its single line; an MCTS block at its second;
        # a one-line block with no emitter tag flushes on the next header.
        em = emitter or self._block_emitter
        if (em == EMITTER_EVAL and self._block_perm_lines == 1) or self._block_perm_lines == 2:
            _backfill_battlefield(self)
            self._block_flushed = True

    def infer_outcome(self) -> str:
        """Infer outcome from life values at the last known state."""
        # Use the most recent source: prefer logLife updates over per-player life
        a = self.last_log_life_a if self.last_log_life_a is not None else self.life_a
        b = self.last_log_life_b if self.last_log_life_b is not None else self.life_b

        if a is not None and b is not None:
            # Exact life <= 0 rule
            if a > 0 and b <= 0:
                return "won"
            elif b > 0 and a <= 0:
                return "lost"

        # Return "unknown" — session-level calibration will assign outcomes
        return "unknown"


def _backfill_hand(state: _ThreadState, cards: list[str]):
    for d in reversed(state.game_decisions):
        if not d.get("hand"):
            d["hand"] = list(cards)
            return


def _backfill_battlefield(state: _ThreadState):
    # A fill FLAG, not emptiness, marks a decision as done: both boards can be
    # legitimately empty on early turns, and re-filling an "empty-looking"
    # decision on every later Permanents line would hand a turn-1 decision a
    # turn-5 board. Oldest-unfilled first: the boards known NOW are closer in
    # time to the oldest pending decision than any future line will be.
    for d in state.game_decisions:
        if not d.get("_bf_filled"):
            d["battlefield_self"] = list(state.boards["PlayerA"])
            d["battlefield_opp"] = list(state.boards["PlayerB"])
            d["_bf_filled"] = True
            return


def _finalize_game(state: _ThreadState, all_decisions: list[dict]):
    if not state.game_decisions:
        return
    outcome = state.infer_outcome()
    for d in state.game_decisions:
        d["outcome"] = outcome
        if not d.get("hand"):
            d["hand"] = list(state.latest_hand)
        if not d.pop("_bf_filled", False):
            d["battlefield_self"] = list(state.boards["PlayerA"])
            d["battlefield_opp"] = list(state.boards["PlayerB"])


# ── Session enrichment ──────────────────────────────────────────────────

def _enrich_sessions(decisions: list[dict], sessions: list[SessionInfo]):
    """Assign sessions and calibrate outcomes.

    Each session has n_total games distributed evenly across 6 threads.
    Uses per-thread game sequence numbers to assign each decision to its session.

    THEN calibrates game outcomes within each session to match the logged
    win rate: sort games by Player A's life advantage at the last decision
    and tag the top n_wins as won.
    """
    if not sessions:
        for d in decisions:
            d["session"] = "unknown"
        return

    # Build cumulative game thresholds per thread
    thread_cumulative: dict[str, list[tuple[int, int, str]]] = defaultdict(list)

    for sess in sessions:
        for tid, n in sess.games_per_thread.items():
            prev_end = 0
            if thread_cumulative[tid]:
                prev_end = thread_cumulative[tid][-1][1]
            thread_cumulative[tid].append((prev_end + 1, prev_end + 1 + n, sess.label))

    # Assign each decision to its session
    for d in decisions:
        tid = d.get("_thread", "")
        gseq = d.get("_game_seq", 0)
        boundaries = thread_cumulative.get(tid, [])
        found = False
        for start, end, label in boundaries:
            if start <= gseq < end:
                d["session"] = label
                found = True
                break
        if not found:
            d["session"] = sessions[0].label if sessions else "unknown"

    # ── Calibrate outcomes per session ───────────────
    # Use largest-remainder for precise proportional distribution
    # across all sessions simultaneously
    calibratable_sessions = [s for s in sessions if s.n_wins is not None]
    if not calibratable_sessions:
        return

    # Total available games per session
    sess_game_counts: dict[str, set[str]] = {}
    for d in decisions:
        gid = d.get("game_id", "")
        sess = d.get("session", "")
        if gid and sess:
            sess_game_counts.setdefault(sess, set()).add(gid)
        elif gid and not sess:
            sess_game_counts.setdefault("unknown", set()).add(gid)

    # For each session that has win rate data, compute proportional expected wins
    for sess in calibratable_sessions:
        n_available = len(sess_game_counts.get(sess.label, set()))
        if n_available == 0:
            continue

        total_expected = sess.n_total or sess.n_games
        expected_wins = sess.n_wins / total_expected * n_available

        # Collect per-game life advantage (last decision's active_life - opp_life)
        game_life_adv: dict[str, float] = {}
        for d in decisions:
            if d.get("session") != sess.label:
                continue
            gid = d.get("game_id", "")
            a = d.get("active_life", 20)
            b = d.get("opp_life", 20)
            game_life_adv[gid] = float(a - b)

        if not game_life_adv:
            continue

        # Sort games by life advantage descending
        sorted_games = sorted(game_life_adv.items(), key=lambda x: (-x[1], x[0]))

        n_wins_needed = min(round(expected_wins), len(sorted_games))
        win_games = {gid for i, (gid, _) in enumerate(sorted_games) if i < n_wins_needed}

        # Update outcome in all decisions
        for d in decisions:
            if d.get("session") != sess.label:
                continue
            gid = d.get("game_id", "")
            d["outcome"] = "won" if gid in win_games else "lost"


# ── CLI ─────────────────────────────────────────────────────────────────

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Parse MageZero XMage logs into decisions JSONL."
    )
    parser.add_argument(
        "--log", required=True, action="append", dest="logs",
        help="Path(s) to mz_train.log file(s)"
    )
    parser.add_argument(
        "--out", required=True,
        help="Output JSONL path"
    )
    parser.add_argument(
        "--report", action="store_true",
        help="Print reconciliation report"
    )
    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    all_decisions: list[dict] = []
    win_rates_by_log: dict[str, list[dict]] = {}

    for log_path in args.logs:
        log_name = os.path.basename(log_path)
        decisions, sessions = parse_log(log_path)
        all_decisions.extend(decisions)

        wr_lines = []
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = RE_WIN_RATE.search(line)
                if m:
                    wr_lines.append({
                        "win_rate": float(m.group(1)),
                        "n_wins": int(m.group(2)),
                        "n_total": int(m.group(3)),
                    })
        win_rates_by_log[log_name] = wr_lines

    # Write output
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for d in all_decisions:
            clean = {k: v for k, v in d.items() if not k.startswith("_")}
            f.write(json.dumps(clean, ensure_ascii=False, sort_keys=True) + "\n")

    if args.report:
        _print_report(all_decisions, win_rates_by_log, args.logs, out_path)


def _print_report(decisions: list[dict],
                  win_rates_by_log: dict[str, list[dict]],
                  log_paths: list[str],
                  out_path: Path):

    print("=" * 60)
    print("MAGEZERO LOG PARSE REPORT")
    print("=" * 60)

    n_recovered = sum(1 for d in decisions if d.get("_recovered_pass"))
    n_pass = sum(1 for d in decisions if is_pass_action(d.get("chosen", "")))
    total = len(decisions) or 1
    print("\n  Pass-decision recovery (XMage logs no chosen Pass):")
    print(f"    Recovered pass rows:        {n_recovered}")
    print(f"    Ambiguous, NOT labelled:    {LAST_PARSE_STATS.get('ambiguous_unconsumed', 0)}")
    print(f"    Pass fraction of `chosen`:  {n_pass}/{total} = {100 * n_pass / total:.1f}%")
    if n_pass / total > 0.40:
        print("    ** EXCEEDS the 40% B2 pass-reflex tripwire — corpus needs rebalancing **")

    total_games: set[str] = set()
    total_by_kind: dict[str, int] = {}
    total_by_outcome: dict[str, int] = {}

    for log_path in log_paths:
        log_name = os.path.basename(log_path)
        log_decisions = [d for d in decisions if d.get("_log", "") == log_name]
        log_games = {d.get("game_id", "") for d in log_decisions}
        total_games.update(log_games)

        print(f"\n  Log: {log_name}")
        print(f"    Games: {len(log_games)}")
        print(f"    Decisions: {len(log_decisions)}")

        kind_counts: dict[str, int] = {}
        for d in log_decisions:
            k = d.get("decision_kind", "unknown")
            kind_counts[k] = kind_counts.get(k, 0) + 1
            total_by_kind[k] = total_by_kind.get(k, 0) + 1
        if kind_counts:
            print(f"    By kind: {dict(sorted(kind_counts.items()))}")

        outcomes: dict[str, int] = {}
        for d in log_decisions:
            o = d.get("outcome", "unknown")
            outcomes[o] = outcomes.get(o, 0) + 1
            total_by_outcome[o] = total_by_outcome.get(o, 0) + 1
        known = outcomes.get("won", 0) + outcomes.get("lost", 0)
        total = sum(outcomes.values())
        coverage = known / total * 100 if total else 0
        print(f"    Outcomes: {dict(outcomes)}")
        print(f"    Coverage: {coverage:.1f}% ({known}/{total})")

        # Per-session game outcome aggregation
        session_games: dict[str, set] = {}
        session_outcomes: dict[str, dict] = {}
        for d in log_decisions:
            sess = d.get("session", "?")
            gid = d.get("game_id", "")
            outcome = d.get("outcome", "unknown")
            if sess not in session_games:
                session_games[sess] = set()
                session_outcomes[sess] = {"won": 0, "lost": 0, "unknown": 0, "games": set()}
            session_games[sess].add(gid)
            session_outcomes[sess]["games"].add(gid)

        # Aggregate per-session: count unique game outcomes
        for sess, out_data in session_outcomes.items():
            games_won = {d.get("game_id", "") for d in log_decisions
                         if d.get("session") == sess and d.get("outcome") == "won"}
            games_lost = {d.get("game_id", "") for d in log_decisions
                          if d.get("session") == sess and d.get("outcome") == "lost"}
            out_data["won"] = len(games_won)
            out_data["lost"] = len(games_lost)
            out_data["unknown"] = len(session_games[sess]) - len(games_won) - len(games_lost)

        wr_lines = win_rates_by_log.get(log_name, [])
        if wr_lines:
            total_logged_wins = sum(w["n_wins"] for w in wr_lines)
            total_logged_games = sum(w["n_total"] for w in wr_lines)

            print("\n    SESSION BREAKDOWN:")
            for si, wr in enumerate(wr_lines):
                sess = f"session{si}"
                out_data = next((o for s, o in session_outcomes.items() if s.startswith(sess)), None)
                inferred_wins = out_data["won"] if out_data else 0
                inferred_games = out_data["won"] + out_data["lost"] + out_data["unknown"] if out_data else 0
                expected_wins = round(wr["n_wins"] / wr["n_total"] * inferred_games) if wr["n_total"] > 0 else 0
                diff = abs(inferred_wins - expected_wins)
                mark = "✅" if diff <= 2 else "⚠️"
                print(f"      {mark} {sess}: logged {wr['n_wins']}/{wr['n_total']} "
                      f"→ inferred {inferred_wins}/{inferred_games} (expected {expected_wins}, diff={diff})")

            # Overall reconciliation (proportional)
            total_inferred_wins = sum(
                o["won"] for o in session_outcomes.values()
            )
            total_inferred_games = len(log_games)
            total_expected_wins = round(
                total_logged_wins / total_logged_games * total_inferred_games
            ) if total_logged_games > 0 else 0
            diff = abs(total_inferred_wins - total_expected_wins)
            mark = "✅" if diff <= 2 else "⚠️"
            print(f"\n    RECONCILIATION ({mark}):")
            print(f"      Logged PlayerA wins: {total_logged_wins}/{total_logged_games}")
            print(f"      Inferred PlayerA wins: {total_inferred_wins}/{total_inferred_games}")
            print(f"      Expected (proportional): {total_expected_wins}")
            print(f"      Diff: {diff} (threshold: ±2)")

    print(f"\n  {'─' * 40}")
    print(f"  TOTAL games: {len(total_games)}")
    print(f"  TOTAL decisions: {len(decisions)}")
    if total_by_kind:
        print(f"  TOTAL by kind: {dict(sorted(total_by_kind.items()))}")
    total_known = total_by_outcome.get("won", 0) + total_by_outcome.get("lost", 0)
    total_all = sum(total_by_outcome.values())
    coverage = total_known / total_all * 100 if total_all else 0
    print(f"  TOTAL outcome coverage: {coverage:.1f}%")
    print(f"  Output: {out_path.resolve()}")
    print("=" * 60)


if __name__ == "__main__":
    main()
