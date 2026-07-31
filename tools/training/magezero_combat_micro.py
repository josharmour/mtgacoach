#!/usr/bin/env python3
"""MageZero COMBAT micro-decision adapter: XMage text logs -> training records.

Why this file exists
--------------------
#453 found that the previous combat converter (build_magezero_combat.py)
reverse-engineered declarations from stale board markers and produced 89
fabricated labels out of 90 records; it now fails closed to zero. The
retraction in rl-pipeline-fix.md ("COMBAT IS BLOCKED UPSTREAM -- RETRACTED")
established that the MCTS agent's combat decisions ARE in the log, at
per-creature granularity, under emitters nobody had grepped for:

Attack micro-decision (one per available attacker; the pool= line immediately
precedes the resolution line ON THE SAME THREAD):

    ... DECLARE_ATTACKERS0pool= actions: [false score: 0.013 count: 104]
        [true score: 0.147 count: 867]           =>[pool-3-thread-1] MCTSNode.bestChild
    ... use attack with: Skrelv, Defector Mite?: true
                                                 =>[pool-3-thread-1] ComputerPlayerMCTS.chooseUse

('base choose use attack with: <name>?' opens the deliberation ~2s earlier on
the same thread.)

Block micro-decision (one per available blocker):

    ... base choose target choose which creature to block for Malcolm, ...
                                                 =>[pool-3-thread-5] ComputerPlayerMCTS.makeChoice
    ... possible targets: 2                      =>[...same thread] ComputerPlayerMCTS.makeChoice
    ... DECLARE_BLOCKERS0pool= actions: [Stop Choosing score: -0.976 count: 250] ...
                                                 =>[...] MCTSNode.bestChild
    ... Targeting Stop Choosing                  =>[...same thread] ComputerPlayerMCTS.makeChoice

'Targeting <attacker>' = blocked that attacker; 'Targeting Stop Choosing' =
declined to block with this creature. A 'Targeting ...' line ALSO appears for
ordinary spell targeting, so a Targeting line only counts as a block decision
when the same thread has an open 'choose which creature to block for' context
with a DECLARE_BLOCKERS pool= attached.

Fail-closed rules (the #452/#453 lessons, mechanised)
-----------------------------------------------------
* All pairing is done per THREAD TAG. Nothing is ever paired across threads.
* A resolution pairs with a pool only if that pool is still pending on the
  same thread (any intervening decision-class event abandons it, with a
  counted reason) and the same-thread line distance is within
  MAX_POOL_GAP_THREAD_LINES.
* Board/hand context comes from the thread's own battlefield state machine
  (same grammar handling as parse_magezero_log, #430) and is only accepted if
  a COMPLETE ComputerPlayerMCTS.printBattlefieldScore block was seen on that
  thread, in the SAME game, within MAX_CTX_GAP_THREAD_LINES same-thread lines.
* Every combat line seen (opener, pool, resolution, Targeting) ends up either
  consumed into a record or counted under an explicit reason;
  :func:`verify_accounting` asserts the identities and is run on every scan.
* Nothing is ever guessed: an unpaired / ambiguous / out-of-window line is a
  counted drop, never a labelled record.

Record shape
------------
Scanner output rows use the shared WP-3 decisions schema (same field names as
parse_magezero_log) with decision_kind 'attack_commit' / 'block_assign', plus
combat extras (creature, candidates, mcts_scores, provenance). The renderer
turns a row into a production-shaped training record via the SAME functions
build_magezero_bridge uses (gate_play_decisions.build_user_message ->
ActionPlanner._build_action_prompt), with the production 'combat_attackers' /
'combat_blockers' triggers and a {"pick": N} answer. MCTS counts/scores stay
in meta and never enter prompt text (the pipeline leak scan re-checks this).

Usage
-----
    python3 tools/training/magezero_combat_micro.py \
        --log /home/joshu/mz_train_smoke.log \
        --out tools/training/data/wp3/combat_decisions.jsonl --report

Wired into run_wp3_pipeline.py behind --include-combat (default OFF).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
for _p in (str(SRC), str(REPO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.training import parse_magezero_log as PARSER  # noqa: E402

# ---------------------------------------------------------------------------
# Regexes (shared ones come straight from the reviewed parser, #430)
# ---------------------------------------------------------------------------

RE_THREAD = PARSER.RE_THREAD
RE_DIE_ROLL = PARSER.RE_DIE_ROLL
RE_LOG_LIFE = PARSER.RE_LOG_LIFE
RE_CHOSE_ACTION = PARSER.RE_CHOSE_ACTION
RE_POOL = PARSER.RE_POOL
RE_POOL_TOP = PARSER.RE_POOL_TOP
RE_PLAYER_LIFE = PARSER.RE_PLAYER_LIFE
RE_HAND = PARSER.RE_HAND
RE_PERMANENTS = PARSER.RE_PERMANENTS
RE_EMITTER = PARSER.RE_EMITTER
EMITTER_MCTS = PARSER.EMITTER_MCTS

# Combat emitters (verified verbatim against mz_train_smoke.log 2026-07-30).
# The resolution line has '?: true|false'; the opener does not, so the two
# cannot be confused by these patterns.
RE_ATTACK_OPEN = re.compile(r"base choose use attack with: (.+)\?\s*=>")
RE_ATTACK_RES = re.compile(r"use attack with: (.+)\?: (true|false)\s*=>")
RE_BLOCK_OPEN = re.compile(
    r"base choose target choose which creature to block for (.+?)\s*=>"
)
RE_TARGET_OPEN = re.compile(r"base choose target ")
RE_TARGETING = re.compile(r"Targeting (.+?)\s*=>")
RE_POSSIBLE = re.compile(r"possible targets: (\d+)")

PHASE_ATTACK = "DECLARE_ATTACKERS"
PHASE_BLOCK = "DECLARE_BLOCKERS"

# Pairing windows, in SAME-THREAD line counts. Measured on mz_train_smoke.log:
# pool -> resolution is same-thread-adjacent in 3213/3213 clean attack pairs
# and 788/788 block pairs (only battlefield-print / 'possible targets' lines
# ever sit between); 12 allows a full 4-line battlefield block plus slack
# without ever reaching back to a stale pool from a previous window.
MAX_POOL_GAP_THREAD_LINES = 12
# Context window: distance from the last complete printBattlefieldScore block
# to the decision. Measured p95 on the smoke log is ~16 extracted events
# (a few hundred raw thread lines across 2s searches); 800 bounds it while
# the same-game check does the semantic work.
MAX_CTX_GAP_THREAD_LINES = 800

# Pre-registered trivial-policy floors (tools/training/gate_combat_decisions.py,
# measured on combat_gate_test.jsonl n=6,852):
#   always_no_block accuracy 0.6983  -> the "69% no-block" floor
#   best combined reflex (always_all_in + always_no_block) 0.3859 -> "38.6%"
NO_BLOCK_FLOOR = 0.69
ALL_IN_FLOOR = 0.386

STOP_CHOOSING = "Stop Choosing"

ATTRIBUTION = "MageZero self-play (XMage-based AlphaZero engine)"
SOURCE = "magezero_combat_micro"

_COMBAT_SUFFIXES = (",attacking", ",blocking")


def _strip_combat_suffix(name: str) -> tuple[str, str | None]:
    for suffix in _COMBAT_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)], suffix[1:]
    return name, None


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


class _Pending:
    """Per-thread pending combat context."""

    __slots__ = (
        "attack_open_name",
        "attack_open_line",
        "attack_pool",  # (actions, global_line, thread_line)
        "block_open_name",
        "block_open_line",
        "block_possible",
        "block_pool",  # (actions, global_line, thread_line)
    )

    def __init__(self):
        self.attack_open_name = None
        self.attack_open_line = 0
        self.attack_pool = None
        self.block_open_name = None
        self.block_open_line = 0
        self.block_possible = None
        self.block_pool = None

    # -- counted state transitions (nothing vanishes uncounted) -----------

    def consume_attack(self, acct: Counter) -> None:
        """A resolution line takes whatever attack context is pending."""
        if self.attack_open_name is not None:
            acct["opener_attack_consumed_at_resolution"] += 1
        if self.attack_pool is not None:
            acct["pool_attack_consumed_at_resolution"] += 1
        self.attack_open_name = None
        self.attack_open_line = 0
        self.attack_pool = None

    def abandon_attack(self, acct: Counter, why: str) -> None:
        if self.attack_open_name is not None:
            acct[f"opener_attack_{why}"] += 1
        if self.attack_pool is not None:
            acct[f"pool_attack_{why}"] += 1
        self.attack_open_name = None
        self.attack_open_line = 0
        self.attack_pool = None

    def consume_block(self, acct: Counter) -> None:
        if self.block_open_name is not None:
            acct["opener_block_consumed_at_targeting"] += 1
        if self.block_pool is not None:
            acct["pool_block_consumed_at_targeting"] += 1
        self.block_open_name = None
        self.block_open_line = 0
        self.block_possible = None
        self.block_pool = None

    def abandon_block(self, acct: Counter, why: str) -> None:
        if self.block_open_name is not None:
            acct[f"opener_block_{why}"] += 1
        if self.block_pool is not None:
            acct[f"pool_block_{why}"] += 1
        self.block_open_name = None
        self.block_open_line = 0
        self.block_possible = None
        self.block_pool = None


class _Ctx:
    """Last complete printBattlefieldScore block on a thread."""

    __slots__ = ("global_line", "thread_line", "game_seq", "turn")

    def __init__(self, global_line: int, thread_line: int, game_seq: int, turn: int):
        self.global_line = global_line
        self.thread_line = thread_line
        self.game_seq = game_seq
        self.turn = turn


def parse_combat_log(log_path: str) -> tuple[list[dict], dict[str, int]]:
    """One streaming pass -> (combat rows, accounting dict).

    The accounting dict contains drop reasons (``drop_*``), abandoned-context
    counters (``opener_*`` / ``pool_*``) and seen counters (``seen_*``);
    :func:`verify_accounting` asserts the closed-ledger identities.
    """
    log_name = os.path.basename(log_path)

    threads: dict[str, PARSER._ThreadState] = {}
    thread_game_seq: dict[str, int] = {}
    pendings: dict[str, _Pending] = {}
    thread_lines: dict[str, int] = {}
    # printBattlefieldScore block tracking (context anchor)
    mcts_block_open: dict[str, int] = {}  # thread -> perm lines seen in open block
    mcts_ctx: dict[str, _Ctx] = {}
    # attack chain id per thread (increments whenever the combat window closes)
    chain_seq: dict[str, int] = {}
    chain_open: dict[str, bool] = {}
    declared_before: dict[str, list] = {}
    block_assignments_before: dict[str, dict] = {}

    rows: list[dict] = []
    game_rows: dict[str, list[dict]] = {}  # thread -> rows of current game
    acct: Counter = Counter()

    def _reset_chain(tid: str):
        if chain_open.get(tid):
            chain_seq[tid] = chain_seq.get(tid, 0) + 1
            chain_open[tid] = False
        declared_before[tid] = []
        block_assignments_before[tid] = {}

    def _finalize_game(tid: str):
        state = threads.get(tid)
        if state is None:
            return
        outcome = state.infer_outcome()
        for r in game_rows.get(tid, []):
            r["outcome"] = outcome
        game_rows[tid] = []

    with open(log_path, encoding="utf-8", errors="replace") as f:
        for global_line, raw in enumerate(f, start=1):
            line = raw.rstrip("\n\r")
            tm = RE_THREAD.search(line)
            if not tm:
                continue
            tid = tm.group(1)
            thread_lines[tid] = thread_lines.get(tid, 0) + 1
            tline = thread_lines[tid]

            state = threads.get(tid)
            if state is None:
                thread_game_seq[tid] = 0
                # game_seq numbering identical to parse_magezero_log so that
                # game_ids line up with the priority corpus for outcome joins
                # and game-level splits.
                state = PARSER._ThreadState(tid, log_name, thread_game_seq[tid] + 1)
                threads[tid] = state
                pendings[tid] = _Pending()
                chain_seq[tid] = 0
                chain_open[tid] = False
                declared_before[tid] = []
                block_assignments_before[tid] = {}
                game_rows[tid] = []
            p = pendings[tid]

            em_m = RE_EMITTER.search(line)
            emitter = em_m.group(1) if em_m else None

            # -- game boundary --------------------------------------------
            if RE_DIE_ROLL.search(line):
                _finalize_game(tid)
                p.abandon_attack(acct, "abandoned_at_game_end")
                p.abandon_block(acct, "abandoned_at_game_end")
                _reset_chain(tid)
                thread_game_seq[tid] += 1
                state.start_new_game(thread_game_seq[tid])
                mcts_ctx.pop(tid, None)
                mcts_block_open.pop(tid, None)
                continue

            # -- turn/phase/life ------------------------------------------
            lm = RE_LOG_LIFE.search(line)
            if lm:
                state.on_log_life(
                    int(lm.group(1)), lm.group(2), lm.group(3),
                    int(lm.group(4)), int(lm.group(5)),
                )
                continue

            # -- MCTS pool ------------------------------------------------
            pool_match = RE_POOL.search(line) or RE_POOL_TOP.search(line)
            if pool_match:
                actions = PARSER.parse_pool_actions(pool_match.group(3))
                if not actions:
                    continue
                phase = pool_match.group(1)
                names = {a[0] for a in actions}
                if phase == PHASE_ATTACK and names == {"true", "false"}:
                    acct["seen_attack_pools_binary"] += 1
                    if p.attack_pool is not None:
                        p.abandon_attack(acct, "abandoned_replaced_by_new_pool")
                    p.attack_pool = (actions, global_line, tline)
                elif phase == PHASE_BLOCK and p.block_open_name is not None:
                    acct["seen_block_pools_gated"] += 1
                    if p.block_pool is not None:
                        acct["pool_block_abandoned_replaced_by_new_pool"] += 1
                        p.block_pool = None
                    p.block_pool = (actions, global_line, tline)
                else:
                    # A priority-window pool (any phase). Not a combat
                    # micro-decision, but a decision-class event: any pending
                    # combat context is now stale, and the combat window is
                    # closed for chain purposes.
                    acct["seen_pools_noncombat"] += 1
                    p.abandon_attack(acct, "abandoned_by_noncombat_pool")
                    p.abandon_block(acct, "abandoned_by_noncombat_pool")
                    _reset_chain(tid)
                continue

            # -- chose action: a priority decision closes combat windows --
            if RE_CHOSE_ACTION.search(line):
                p.abandon_attack(acct, "abandoned_at_priority_decision")
                p.abandon_block(acct, "abandoned_at_priority_decision")
                _reset_chain(tid)
                continue

            # -- attack deliberation opener -------------------------------
            am = RE_ATTACK_OPEN.search(line)
            if am:
                acct["seen_attack_openers"] += 1
                if p.attack_open_name is not None or p.attack_pool is not None:
                    p.abandon_attack(acct, "abandoned_by_new_attack_opener")
                p.abandon_block(acct, "abandoned_by_attack_opener")
                p.attack_open_name = am.group(1)
                p.attack_open_line = global_line
                continue

            # -- attack resolution ----------------------------------------
            rm = RE_ATTACK_RES.search(line)
            if rm:
                acct["seen_attack_resolutions"] += 1
                name, verdict = rm.group(1), rm.group(2)
                row = _try_emit_attack(
                    state, p, acct, name, verdict, global_line, tline,
                    mcts_ctx.get(tid), log_name,
                    chain_seq.get(tid, 0), declared_before[tid],
                )
                if row is not None:
                    chain_open[tid] = True
                    if verdict == "true":
                        declared_before[tid] = declared_before[tid] + [name]
                    rows.append(row)
                    game_rows[tid].append(row)
                p.consume_attack(acct)
                continue

            # -- block deliberation opener --------------------------------
            bm = RE_BLOCK_OPEN.search(line)
            if bm:
                acct["seen_block_openers"] += 1
                if p.block_open_name is not None:
                    p.abandon_block(acct, "abandoned_by_new_block_opener")
                p.abandon_attack(acct, "abandoned_by_block_opener")
                p.block_open_name = bm.group(1)
                p.block_open_line = global_line
                p.block_possible = None
                p.block_pool = None
                continue

            # -- other target opener (spell targeting) closes block window -
            if RE_TARGET_OPEN.search(line):
                p.abandon_block(acct, "abandoned_by_spell_targeting")
                continue

            pmatch = RE_POSSIBLE.search(line)
            if pmatch:
                if p.block_open_name is not None and p.block_pool is None:
                    p.block_possible = int(pmatch.group(1))
                continue

            # -- Targeting resolution -------------------------------------
            tg = RE_TARGETING.search(line)
            if tg:
                acct["seen_targeting_total"] += 1
                if p.block_open_name is None:
                    acct["seen_targeting_nonblock"] += 1
                    continue
                acct["seen_targeting_block_gated"] += 1
                blocker = p.block_open_name
                row = _try_emit_block(
                    state, p, acct, tg.group(1), global_line, tline,
                    mcts_ctx.get(tid), log_name,
                    chain_seq.get(tid, 0), block_assignments_before[tid],
                )
                if row is not None:
                    chain_open[tid] = True
                    if tg.group(1) != STOP_CHOOSING:
                        nxt = dict(block_assignments_before[tid])
                        nxt[blocker] = tg.group(1)
                        block_assignments_before[tid] = nxt
                    rows.append(row)
                    game_rows[tid].append(row)
                p.consume_block(acct)
                continue

            # -- battlefield block header ---------------------------------
            pl = RE_PLAYER_LIFE.search(line)
            if pl:
                state.on_block_header(pl.group(1), int(pl.group(2)), emitter)
                if emitter == EMITTER_MCTS and pl.group(1) == "PlayerA":
                    mcts_block_open[tid] = 0
                else:
                    mcts_block_open.pop(tid, None)
                continue

            hm = RE_HAND.search(line)
            if hm:
                state.on_hand_line(PARSER.parse_card_list(hm.group(1)))
                continue

            perm = RE_PERMANENTS.search(line)
            if perm:
                state.on_permanents_line(
                    PARSER.parse_permanents(perm.group(1)), emitter
                )
                if emitter == EMITTER_MCTS and tid in mcts_block_open:
                    mcts_block_open[tid] += 1
                    if mcts_block_open[tid] == 2:
                        # complete MCTS block: both boards freshly attributed
                        mcts_ctx[tid] = _Ctx(
                            global_line, tline, state.game_seq, state.last_turn
                        )
                        mcts_block_open.pop(tid, None)
                continue

    # EOF: finalize outcomes, count still-pending windows
    for tid, p in pendings.items():
        p.abandon_attack(acct, "abandoned_at_eof")
        p.abandon_block(acct, "abandoned_at_eof")
    for tid in threads:
        _finalize_game(tid)

    return rows, dict(acct)


def _context_ok(
    acct: Counter, ctx: _Ctx | None, state, tline: int, kind: str
) -> bool:
    """Fail-closed board-context gate. Counts the reason on failure."""
    if ctx is None:
        acct[f"drop_{kind}_no_board_context"] += 1
        return False
    if ctx.game_seq != state.game_seq:
        acct[f"drop_{kind}_board_context_previous_game"] += 1
        return False
    if tline - ctx.thread_line > MAX_CTX_GAP_THREAD_LINES:
        acct[f"drop_{kind}_board_context_out_of_window"] += 1
        return False
    return True


def _board_entry(p: dict) -> dict:
    """parse_permanents now extracts ,attacking/,blocking into flags itself;
    the suffix strip is kept as a fail-safe for rows parsed by older code."""
    name, marker = _strip_combat_suffix(p["name"])
    out = {"name": name, "tapped": bool(p.get("tapped", False))}
    for key in ("attacking", "blocking"):
        if p.get(key):
            out[key] = True
    if marker:
        out[marker] = True
    return out


def _base_row(
    state, log_name: str, phase: str, kind: str, chain: int
) -> dict:
    return {
        "game_id": state.game_id,
        "turn": state.last_turn,
        "phase": phase,
        "active_life": state.life_a,
        "opp_life": state.life_b,
        "hand": list(state.latest_hand),
        "battlefield_self": [_board_entry(p) for p in state.boards["PlayerA"]],
        "battlefield_opp": [_board_entry(p) for p in state.boards["PlayerB"]],
        "actor": "PlayerA",
        "outcome": "unknown",
        "session": "",
        "decision_kind": kind,
        "_thread": state.thread_id,
        "_game_seq": state.game_seq,
        "_log": log_name,
        "_chain": f"{state.game_id}:combat{chain}",
    }


def _try_emit_attack(
    state, p: _Pending, acct: Counter, name: str, verdict: str,
    global_line: int, tline: int, ctx: _Ctx | None, log_name: str,
    chain: int, declared_before: list,
) -> dict | None:
    if p.attack_pool is None:
        acct["drop_attack_resolution_without_pool"] += 1
        return None
    actions, pool_line, pool_tline = p.attack_pool
    if tline - pool_tline > MAX_POOL_GAP_THREAD_LINES:
        acct["drop_attack_pool_too_far"] += 1
        return None
    if p.attack_open_name is None:
        acct["drop_attack_opener_missing"] += 1
        return None
    if p.attack_open_name != name:
        acct["drop_attack_opener_name_mismatch"] += 1
        return None
    if not _context_ok(acct, ctx, state, tline, "attack"):
        return None

    menu = [f"Attack with {name}", f"Do not attack with {name}"]
    chosen = menu[0] if verdict == "true" else menu[1]
    row = _base_row(state, log_name, PHASE_ATTACK, "attack_commit", chain)
    creature_on_board = any(e["name"] == name for e in row["battlefield_self"])
    row.update({
        "menu": menu,
        "chosen": chosen,
        "creature": name,
        "attack_committed": verdict == "true",
        "declared_before": list(declared_before),
        "mcts_counts": {a: c for a, _s, c in actions},
        "mcts_scores": {a: s for a, s, _c in actions},
        "provenance": {
            "log": log_name,
            "thread": state.thread_id,
            "line_opener": p.attack_open_line,
            "line_pool": pool_line,
            "line_resolution": global_line,
            "line_context": ctx.global_line,
            "context_emitter": EMITTER_MCTS,
            "context_thread_line_gap": tline - ctx.thread_line,
            "creature_on_board": creature_on_board,
        },
    })
    acct["emitted_attack"] += 1
    return row


def _try_emit_block(
    state, p: _Pending, acct: Counter, chosen_raw: str,
    global_line: int, tline: int, ctx: _Ctx | None, log_name: str,
    chain: int, assignments_before: dict,
) -> dict | None:
    blocker = p.block_open_name
    if p.block_pool is None:
        acct["drop_block_targeting_without_pool"] += 1
        return None
    actions, pool_line, pool_tline = p.block_pool
    if tline - pool_tline > MAX_POOL_GAP_THREAD_LINES:
        acct["drop_block_pool_too_far"] += 1
        return None
    pool_names = [a for a, _s, _c in actions]
    if chosen_raw not in pool_names:
        acct["drop_block_chosen_not_in_pool"] += 1
        return None
    # Candidates: attacker names from the pool, in pool order, minus the
    # decline sentinel. Duplicate names get " #k" disambiguators so menu
    # entries stay unique (chosen maps to the first occurrence, which is
    # name-accurate: the log does not identify the instance).
    candidates = [n for n in pool_names if n != STOP_CHOOSING]
    if not candidates:
        acct["drop_block_pool_has_no_attackers"] += 1
        return None
    if not _context_ok(acct, ctx, state, tline, "block"):
        return None

    counts = Counter(candidates)
    seen: dict[str, int] = {}
    display: list[str] = []
    for n in candidates:
        if counts[n] > 1:
            seen[n] = seen.get(n, 0) + 1
            display.append(f"{n} #{seen[n]}")
        else:
            display.append(n)
    menu = [f"Block {d} with {blocker}" for d in display]
    menu.append(f"Do not block with {blocker}")
    if chosen_raw == STOP_CHOOSING:
        chosen = menu[-1]
    else:
        idx = candidates.index(chosen_raw)  # first occurrence
        chosen = menu[idx]

    row = _base_row(state, log_name, PHASE_BLOCK, "block_assign", chain)
    blocker_on_board = any(e["name"] == blocker for e in row["battlefield_self"])
    attackers_on_board = sum(
        1 for n in set(candidates)
        if any(e["name"] == n for e in row["battlefield_opp"])
    )
    row.update({
        "menu": menu,
        "chosen": chosen,
        "creature": blocker,
        "candidates": candidates,
        "candidates_display": display,
        "blocked_attacker": None if chosen_raw == STOP_CHOOSING else chosen_raw,
        "possible_targets": p.block_possible,
        "possible_targets_matches_pool": (
            p.block_possible is not None and p.block_possible == len(candidates)
        ),
        "duplicate_candidates": any(v > 1 for v in counts.values()),
        "assignments_before": dict(assignments_before),
        "mcts_counts": {a: c for a, _s, c in actions},
        "mcts_scores": {a: s for a, s, _c in actions},
        "provenance": {
            "log": log_name,
            "thread": state.thread_id,
            "line_opener": p.block_open_line,
            "line_pool": pool_line,
            "line_resolution": global_line,
            "line_context": ctx.global_line,
            "context_emitter": EMITTER_MCTS,
            "context_thread_line_gap": tline - ctx.thread_line,
            "creature_on_board": blocker_on_board,
            "attackers_on_board": attackers_on_board,
            "attackers_total": len(set(candidates)),
        },
    })
    acct["emitted_block"] += 1
    return row


# ---------------------------------------------------------------------------
# Accounting verification (requirement: total accounted == total seen)
# ---------------------------------------------------------------------------

ATTACK_DROP_REASONS = (
    "drop_attack_resolution_without_pool",
    "drop_attack_pool_too_far",
    "drop_attack_opener_missing",
    "drop_attack_opener_name_mismatch",
    "drop_attack_no_board_context",
    "drop_attack_board_context_previous_game",
    "drop_attack_board_context_out_of_window",
)

BLOCK_DROP_REASONS = (
    "drop_block_targeting_without_pool",
    "drop_block_pool_too_far",
    "drop_block_chosen_not_in_pool",
    "drop_block_pool_has_no_attackers",
    "drop_block_no_board_context",
    "drop_block_board_context_previous_game",
    "drop_block_board_context_out_of_window",
)


def _sum_prefixed(acct: dict[str, int], prefix: str) -> int:
    return sum(v for k, v in acct.items() if k.startswith(prefix))


def verify_accounting(acct: dict[str, int]) -> None:
    """Assert the closed ledger: every combat line consumed or counted."""
    # resolutions
    seen_att = acct.get("seen_attack_resolutions", 0)
    acc_att = acct.get("emitted_attack", 0) + sum(
        acct.get(k, 0) for k in ATTACK_DROP_REASONS
    )
    assert acc_att == seen_att, (
        f"attack accounting mismatch: seen={seen_att} accounted={acc_att} — "
        f"a resolution line was neither emitted nor counted"
    )
    # targeting
    seen_tgt = acct.get("seen_targeting_total", 0)
    acc_tgt = acct.get("seen_targeting_nonblock", 0) + acct.get(
        "seen_targeting_block_gated", 0
    )
    assert acc_tgt == seen_tgt, (
        f"targeting accounting mismatch: seen={seen_tgt} accounted={acc_tgt}"
    )
    seen_gated = acct.get("seen_targeting_block_gated", 0)
    acc_gated = acct.get("emitted_block", 0) + sum(
        acct.get(k, 0) for k in BLOCK_DROP_REASONS
    )
    assert acc_gated == seen_gated, (
        f"block accounting mismatch: gated={seen_gated} accounted={acc_gated} — "
        f"a Targeting line was neither emitted nor counted"
    )
    # openers and pools: consumed + abandoned == seen
    for seen_key, prefix in (
        ("seen_attack_openers", "opener_attack_"),
        ("seen_attack_pools_binary", "pool_attack_"),
        ("seen_block_openers", "opener_block_"),
        ("seen_block_pools_gated", "pool_block_"),
    ):
        seen = acct.get(seen_key, 0)
        accounted = _sum_prefixed(acct, prefix)
        assert accounted == seen, (
            f"{seen_key} ledger mismatch: seen={seen} accounted={accounted}"
        )


# ---------------------------------------------------------------------------
# Histograms and trivial-policy floors
# ---------------------------------------------------------------------------


def combat_histograms(rows: list[dict]) -> dict:
    """Class histograms + pre-registered floor checks.

    * no_block share (record level) vs the 69% always-no-block floor.
    * all-in share (combat-chain level: every emitted attack decision in the
      chain said "attack") vs the 38.6% floor.
    On breach the caller prints loudly and records the flag in the manifest.
    No silent rebalancing, ever.
    """
    attack = [r for r in rows if r.get("decision_kind") == "attack_commit"]
    block = [r for r in rows if r.get("decision_kind") == "block_assign"]

    n_commit = sum(1 for r in attack if r.get("attack_committed"))
    attack_hist = {
        "attack": n_commit,
        "no_attack": len(attack) - n_commit,
    }
    attack_true_share = n_commit / len(attack) if attack else 0.0

    n_no_block = sum(1 for r in block if r.get("blocked_attacker") is None)
    block_hist = {
        "no_block": n_no_block,
        "block": len(block) - n_no_block,
    }
    no_block_share = n_no_block / len(block) if block else 0.0

    # chain-level attack classes (emitted rows only; a dropped micro-decision
    # inside a chain removes it from this grouping, which is reported)
    chains: dict[str, list[bool]] = {}
    for r in attack:
        key = r.get("_chain") or (r.get("meta", {}) or {}).get("combat_chain") or r.get("game_id", "?")
        chains.setdefault(key, []).append(bool(r.get("attack_committed")))
    chain_classes: Counter = Counter()
    for verdicts in chains.values():
        if all(verdicts):
            chain_classes["all_in"] += 1
        elif not any(verdicts):
            chain_classes["no_attack"] += 1
        else:
            chain_classes["proper_subset"] += 1
    n_chains = sum(chain_classes.values())
    all_in_share = chain_classes.get("all_in", 0) / n_chains if n_chains else 0.0

    breach_no_block = no_block_share > NO_BLOCK_FLOOR
    breach_all_in = all_in_share > ALL_IN_FLOOR
    return {
        "attack_records": len(attack),
        "block_records": len(block),
        "attack_hist": attack_hist,
        "attack_true_share": round(attack_true_share, 4),
        "block_hist": block_hist,
        "no_block_share": round(no_block_share, 4),
        "attack_chains": n_chains,
        "attack_chain_hist": dict(chain_classes),
        "all_in_share": round(all_in_share, 4),
        "floors": {
            "no_block_floor": NO_BLOCK_FLOOR,
            "all_in_floor": ALL_IN_FLOOR,
        },
        "floor_breach_no_block": breach_no_block,
        "floor_breach_all_in": breach_all_in,
        "floor_breach": breach_no_block or breach_all_in,
    }


def print_floor_check(hist: dict, out=sys.stderr) -> None:
    print("\n  Combat class histograms:", file=out)
    print(
        f"    attack records: {hist['attack_records']}  "
        f"{hist['attack_hist']}  (attack share "
        f"{hist['attack_true_share']:.1%})",
        file=out,
    )
    print(
        f"    attack chains:  {hist['attack_chains']}  "
        f"{hist['attack_chain_hist']}  (all-in share "
        f"{hist['all_in_share']:.1%} vs floor {ALL_IN_FLOOR:.1%})",
        file=out,
    )
    print(
        f"    block records:  {hist['block_records']}  "
        f"{hist['block_hist']}  (no-block share "
        f"{hist['no_block_share']:.1%} vs floor {NO_BLOCK_FLOOR:.1%})",
        file=out,
    )
    if hist["floor_breach"]:
        print(
            "\n  ********************************************************\n"
            "  ** TRIVIAL-POLICY FLOOR BREACHED — corpus is NOT being **\n"
            "  ** rebalanced silently. floor_breach=true is recorded  **\n"
            "  ** in the manifest; the gate decision is the owner's.  **\n"
            "  ********************************************************",
            file=out,
        )
        if hist["floor_breach_no_block"]:
            print(
                f"  ** no_block share {hist['no_block_share']:.1%} > "
                f"{NO_BLOCK_FLOOR:.1%} always-no-block floor",
                file=out,
            )
        if hist["floor_breach_all_in"]:
            print(
                f"  ** all-in share {hist['all_in_share']:.1%} > "
                f"{ALL_IN_FLOOR:.1%} floor",
                file=out,
            )


# ---------------------------------------------------------------------------
# Renderer: combat row -> production-shaped training record
# ---------------------------------------------------------------------------

_BRIDGE = None
_G = None


def _lazy_imports():
    """build_magezero_bridge pulls in the production prompt stack; keep the
    scanner importable without it."""
    global _BRIDGE, _G
    if _BRIDGE is None:
        from tools.training import build_magezero_bridge as B
        from tools.training import gate_play_decisions as G
        _BRIDGE = B
        _G = G
    return _BRIDGE, _G


TRIGGER_BY_KIND = {
    "attack_commit": "combat_attackers",
    "block_assign": "combat_blockers",
}


def build_combat_record(row: dict) -> tuple[dict | None, str]:
    """Combat decisions row -> training record, or (None, drop_reason)."""
    BRIDGE, G = _lazy_imports()

    kind = row.get("decision_kind", "")
    trigger = TRIGGER_BY_KIND.get(kind)
    if trigger is None:
        return None, f"decision_kind_{kind or 'missing'}"

    outcome = row.get("outcome", "unknown")
    if outcome == "unknown":
        return None, "outcome_unknown"

    menu = row.get("menu", [])
    if len(menu) < 2:
        return None, "menu_too_small"
    chosen = row.get("chosen", "")
    if chosen not in menu:
        return None, "chosen_not_in_menu"
    answer_idx = menu.index(chosen) + 1

    gs_row = dict(row)
    gs_row["phase"] = (
        "COMBAT_DECLARE_ATTACKERS" if kind == "attack_commit"
        else "COMBAT_DECLARE_BLOCKERS"
    )
    game_state = BRIDGE.build_game_state(gs_row)

    # Log-derived combat markers -> formatter [ATK]/[BLK] flags, SELF board
    # only. Evidence sources (all from the log, never inferred):
    #   1. ',attacking'/',blocking' suffixes on the battlefield print itself
    #      (index-aligned: build_game_state keeps battlefield_self order);
    #   2. this combat's earlier commitments on this thread (declared_before /
    #      assignments_before), marked only when the name matches exactly one
    #      board entry — with duplicates the instance is unknowable from the
    #      log, so nothing is marked (fail closed on ambiguity).
    # OPP cards are deliberately NEVER marked is_attacking: MageZero carries
    # no power/toughness, and an attacking-marked opponent board makes the
    # production formatter run its block solver on fabricated 0/0 stats and
    # print "Computed optimal blocks: ..." — combat math from invented
    # numbers AND, for a no-block gold answer, a literal label leak. The
    # attacker roster reaches the model through the menu rows instead
    # ("Block <attacker> with <blocker>"), which is log-authoritative.
    bf = game_state.get("battlefield", [])
    for i, src in enumerate(row.get("battlefield_self", [])):
        if src.get("attacking"):
            bf[i]["is_attacking"] = True
        if src.get("blocking"):
            bf[i]["is_blocking"] = True

    def _mark_unique_self(names: list[str], flag: str) -> None:
        for name in names:
            hits = [
                c for c in bf
                if c["controller_seat_id"] == BRIDGE.LOCAL_SEAT
                and c["name"] == name
            ]
            if len(hits) == 1:
                hits[0][flag] = True

    if kind == "attack_commit":
        _mark_unique_self(row.get("declared_before", []), "is_attacking")
    else:
        _mark_unique_self(
            list(row.get("assignments_before", {}).keys()), "is_blocking"
        )
        # Blocking happens on the opponent's turn.
        game_state["turn"]["active_player"] = BRIDGE.OPP_SEAT

    user = BRIDGE.sanitize_user(G.build_user_message(game_state, menu, trigger=trigger))
    if "Computed optimal" in user:
        # A solver line would be combat math computed from fabricated card
        # stats, and can restate the gold answer. Fail closed.
        return None, "solver_line_rendered"
    if user.count("Legal: (pick by number)") != 1:
        return None, "menu_header_not_production"

    meta = {
        "game_id": row.get("game_id", ""),
        "turn": row.get("turn", 0),
        "phase": row.get("phase", ""),
        "outcome": outcome,
        "actor": row.get("actor", ""),
        "session": row.get("session", ""),
        "decision_kind": kind,
        "prompt_shape": "production_legal_menu",
        "system_is_autopilot_prompt": True,
        "trigger": trigger,
        "answer_pick": answer_idx,
        "menu_size": len(menu),
        "creature": row.get("creature", ""),
        "mcts_counts": row.get("mcts_counts", {}),
        "mcts_scores": row.get("mcts_scores", {}),
        "provenance": row.get("provenance", {}),
        "combat_chain": row.get("_chain", ""),
    }
    if kind == "attack_commit":
        meta["attack_committed"] = bool(row.get("attack_committed"))
        meta["declared_before"] = row.get("declared_before", [])
    else:
        meta["candidates"] = row.get("candidates", [])
        meta["blocked_attacker"] = row.get("blocked_attacker")
        meta["block_class"] = (
            "no_block" if row.get("blocked_attacker") is None else "block"
        )
        meta["possible_targets"] = row.get("possible_targets")
        meta["assignments_before"] = row.get("assignments_before", {})
        meta["duplicate_candidates"] = bool(row.get("duplicate_candidates"))

    record = {
        "system": BRIDGE.AUTOPILOT_SYSTEM_PROMPT,
        "user": user,
        "response": json.dumps({"actions": [{"pick": answer_idx}]}),
        "source": SOURCE,
        "attribution": ATTRIBUTION,
        "kind": kind,
        "meta": meta,
    }
    # The label must not be readable from the prompt: both menu options are
    # present by design (that is the pick mechanism), but no MCTS artifact
    # may appear.
    assert "score:" not in user and "count:" not in user, "MCTS leak in prompt"
    return record, ""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Extract MageZero combat micro-decisions into decisions JSONL."
    )
    ap.add_argument("--log", required=True, action="append", dest="logs")
    ap.add_argument("--out", required=True)
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args(argv)

    all_rows: list[dict] = []
    total_acct: Counter = Counter()
    for lp in args.logs:
        rows, acct = parse_combat_log(lp)
        verify_accounting(acct)
        all_rows.extend(rows)
        total_acct.update(acct)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for r in all_rows:
            clean = {k: v for k, v in r.items() if not k.startswith("_")}
            f.write(json.dumps(clean, ensure_ascii=False, sort_keys=True) + "\n")

    if args.report:
        print("=" * 60, file=sys.stderr)
        print("MAGEZERO COMBAT MICRO-DECISION SCAN", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        print(f"  rows emitted: {len(all_rows)}", file=sys.stderr)
        for k in sorted(total_acct):
            print(f"  {k}: {total_acct[k]}", file=sys.stderr)
        hist = combat_histograms(all_rows)
        print_floor_check(hist)
        print(f"  output: {out_path.resolve()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
