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

# Name-attributed hand lines from ComputerPlayer.logList, e.g.
#   [1:Beginning:UPKEEP]PlayerA hand: : Island,Soul Partition, =>[pool-3-thread-4] ComputerPlayer.logList
# Unlike the positional `-> Hand:` block lines, these carry the PLAYER NAME,
# the TURN, and the PHASE on the line itself, so attribution never depends on
# which block header happened to precede them (the #452 defect family). All
# 9,063 such lines on mz_train_smoke.log are `[N:Beginning:UPKEEP]PlayerA`.
# The card list is comma-separated WITHOUT a space after separator commas,
# while commas inside card names ("Kitsa, Otterball Elite") are followed by
# one — see parse_named_hand_cards. 624 of the 9,063 lines carry an empty
# list: a legitimately empty hand, distinct from "no line found".
RE_NAMED_HAND = re.compile(r"\[(\d+):([^:\]]+):(\w+)\](\w+) hand: : (.*?)\s*(?:=>|$)")

# The MCTS/primary player. Decisions carry actor="PlayerA"; only this
# player's name-attributed hand lines may reach a training row.
PRIMARY_PLAYER = "PlayerA"

# ── Primary-deck resolution + hand deck-signature guardrail ─────────────
#
# The #452/#457-family guardrail ("the primary player must not 'hold' cards
# outside the primary player's deck") was previously pinned to UWTempo in
# tests only. It is now a runtime, fail-closed check parameterized by the
# actual .dck list, so logs whose primary deck is NOT UWTempo (e.g. the
# permanent-holdout unseen-deck logs mz_unseen_<Primary>_vs_<Opponent>.log)
# get the SAME protection instead of silently none.
#
# Resolution order (never guess):
#   1. an explicit primary_deck argument (CLI --primary-deck) — the named
#      .dck MUST exist, else DeckSignatureError;
#   2. filename inference from the `..._<Primary>_vs_<Opponent>.log`
#      convention — an inferred deck whose .dck is missing is AMBIGUOUS and
#      raises rather than being skipped (a deck-named log with no list to
#      check against must not build unchecked);
#   3. neither → None: legacy logs (mz_train_smoke.log) keep their existing
#      behavior (test-level UWTempo guardrail, no runtime check).

DEFAULT_DECKS_DIR = "/home/joshu/repos/magezero/xmage/decks"

# `..._<Primary>_vs_<Opponent>.log` — deck names as used in .dck filenames
# (letters/digits plus the separators that appear in the deck directory).
RE_LOG_DECK_NAMES = re.compile(r"_([A-Za-z0-9][A-Za-z0-9 .()-]*)_vs_([A-Za-z0-9][A-Za-z0-9 .()-]*)\.log$")

# One .dck card line: `4 [LCI:63] Malcolm, Alluring Scoundrel` (optionally
# `SB:`-prefixed for sideboard lines). LAYOUT lines are skipped by the caller.
RE_DCK_CARD = re.compile(r"^\s*(?:SB:\s*)?(\d+)\s+\[[^\]]+\]\s+(.+?)\s*$")


class DeckSignatureError(ValueError):
    """Primary-deck attribution is ambiguous or the hand signature is broken."""


def parse_dck_names(path: str | Path) -> frozenset[str]:
    """Distinct card names in an XMage .dck file (main + sideboard lines)."""
    names: set[str] = set()
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("LAYOUT"):
                continue
            m = RE_DCK_CARD.match(line)
            if m:
                names.add(m.group(2))
    if not names:
        raise DeckSignatureError(f"no card lines parsed from deck list: {path}")
    return frozenset(names)


def infer_decks_from_log_name(log_name: str) -> tuple[str, str] | None:
    """(primary, opponent) deck names from `..._<Primary>_vs_<Opponent>.log`."""
    m = RE_LOG_DECK_NAMES.search(os.path.basename(log_name))
    return (m.group(1), m.group(2)) if m else None


def resolve_primary_deck(
    log_path: str | Path,
    primary_deck: str | None = None,
    decks_dir: str | Path | None = None,
) -> tuple[str, frozenset[str]] | None:
    """Resolve (deck_name, card_name_set) for the primary player, or None.

    Fail-closed: an explicit or filename-inferred deck whose .dck cannot be
    loaded raises DeckSignatureError instead of degrading to "no check".
    Returns None only when there is NO signature source at all (legacy logs
    whose filename carries no `_vs_` deck names and no --primary-deck given).
    """
    d = Path(decks_dir) if decks_dir is not None else Path(DEFAULT_DECKS_DIR)
    if primary_deck:
        name = primary_deck.removesuffix(".dck")
        dck = d / f"{name}.dck"
        if not dck.is_file():
            raise DeckSignatureError(
                f"--primary-deck {name!r} has no deck list at {dck} — refusing to "
                "attribute hands against an unknown deck (fail closed)"
            )
        return name, parse_dck_names(dck)
    inferred = infer_decks_from_log_name(str(log_path))
    if inferred is None:
        return None
    name = inferred[0]
    dck = d / f"{name}.dck"
    if not dck.is_file():
        raise DeckSignatureError(
            f"log name {os.path.basename(str(log_path))!r} names primary deck {name!r} "
            f"but no deck list exists at {dck} — pass --primary-deck/--decks-dir "
            "(fail closed: a deck-named log must not parse unchecked)"
        )
    return name, parse_dck_names(dck)


def verify_hand_signature(decisions: list[dict], deck_name: str, deck_names: frozenset[str]) -> int:
    """Every emitted hand card must be in the primary deck — else raise.

    This is the runtime form of tests' TestHandDeckSignature: a hand card
    outside the primary player's deck list means hand attribution broke
    (#452 family: positional pollution, swapped players, score suffixes).
    The whole parse is rejected with a class histogram — never a silent drop.
    Returns the number of rows checked.
    """
    from collections import Counter

    offenders: Counter[str] = Counter()
    rows_hit = 0
    for d in decisions:
        bad = [c for c in d.get("hand", []) if c not in deck_names]
        if bad:
            rows_hit += 1
            offenders.update(bad)
    if offenders:
        hist = dict(offenders.most_common(15))
        raise DeckSignatureError(
            f"hand deck-signature check FAILED against {deck_name}.dck: "
            f"{sum(offenders.values())} off-deck hand entries in {rows_hit} rows "
            f"(of {len(decisions)}). Offender histogram (top 15): {hist}. "
            "Either the primary-deck attribution is wrong (pass --primary-deck) "
            "or hand attribution regressed — refusing to emit."
        )
    return len(decisions)

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
LAST_PARSE_STATS: dict[str, int] = {
    "recovered_pass": 0,
    "ambiguous_unconsumed": 0,
    "hand_named_attributed": 0,
    "hand_dropped_unattributed": 0,
    "deck_signature_checked_rows": 0,
}


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
SMOKE_OPPONENTS_GEN0 = [
    "Standard-MonoR",
    "Standard-MonoG",
    "Standard-MonoB",
    "Standard-MonoW",
    "Standard-MonoU",
]


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


def segment_menu(raw: str, vocab: list[str]) -> list[str]:
    """Split a raw `playable abilities: [...]` payload into menu entries.

    XMage prints the menu as List.toString() — entries joined with ", " and no
    quoting — so the split points are ambiguous from the menu line alone: card
    names ("Cast Malcolm, Alluring Scoundrel") and ability text ("{T}: Draw a
    card, then discard a card.") contain ", " themselves. A plain comma split
    shattered those entries, and every `chose action` whose name contains a
    comma then failed the exact-match against the menu — 816 of 5,835
    post-filter rows (14.0%) dropped as chosen_not_in_menu on
    mz_train_smoke.log, all of them casts of comma-named cards.

    The MCTS pool line of the SAME thread-paired window is bracket-delimited
    ([<name> score: X count: N]) and therefore comma-safe: its action names are
    verbatim ground truth for what the entries can be. Segmentation anchors on
    those names (longest first) at ", " boundaries. Any stretch that matches no
    vocab name falls back to the plain comma split — the old behavior — so a
    merge is only ever produced when the pool confirms the merged form
    verbatim. Never guess.
    """
    raw = raw.strip()
    if not raw:
        return []
    names = sorted({v for v in vocab if v}, key=len, reverse=True)
    segs: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        matched = False
        for v in names:
            end = i + len(v)
            if raw.startswith(v, i) and (end == n or raw.startswith(", ", end)):
                segs.append(v)
                i = end + 2 if end < n else end
                matched = True
                break
        if matched:
            continue
        # Unknown stretch: consume up to the next ", " exactly like the old
        # comma split did. Fail closed — no pool-confirmed name, no merge.
        j = raw.find(", ", i)
        if j == -1:
            seg = raw[i:].strip()
            if seg:
                segs.append(seg)
            break
        seg = raw[i:j].strip()
        if seg:
            segs.append(seg)
        i = j + 2
    return segs


def _emit_pass_decision(
    state: _ThreadState,
    pending: _PendingPool,
    menu_raw: str,
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
    pool_names = [name for name, _, _ in actions]
    menu = segment_menu(menu_raw, pool_names)
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
        "menu": menu if menu else list(pool_names),
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
        "_hand_positional": [],
    }
    state.game_decisions.append(decision)
    stats["recovered_pass"] += 1


# A menu entry that actually declares attackers or blockers. XMage phrases these
# as "Block <attacker> with <blocker>" / "Attack with <creature>"; a bare mention
# inside an oracle text ("can't be blocked by...") must not count, so the match is
# anchored to the start of the entry.
RE_COMBAT_DECLARATION = re.compile(r"^\s*(block|attack)\b", re.IGNORECASE)

# XMage's OTHER declaration shape: a multi-select where the options are creature
# names and one entry terminates the selection. Absent from mz_train_smoke.log
# (0 of 21,609 rows) but present in the engine, so both shapes are recognised —
# the classifier must not depend on one phrasing.
_MULTISELECT_TERMINATORS = frozenset({"stop choosing", "stop", "done", "finish"})


def _is_combat_menu(names: set[str]) -> bool:
    """True when this menu actually offers an attack/block declaration."""
    if any(RE_COMBAT_DECLARATION.match(n) for n in names):
        return True
    return any(n.strip().lower() in _MULTISELECT_TERMINATORS for n in names)


def classify_kind(phase_code: str, pool_actions: list[tuple]) -> str:
    """Classify a decision by what was actually OFFERED, not by the phase.

    Mapping DECLARE_ATTACKERS/DECLARE_BLOCKERS straight to "attackers"/"blockers"
    over-claims badly: XMage grants ordinary priority windows *during* those steps
    (you may cast an instant in the declare-attackers step), and those windows
    carry a priority menu. Measured on mz_train_smoke.log with the phase-based
    rule, of 1,274 rows labelled "attackers" the chosen action was:

        895  Pass
        116  {1}{U}: Untap {this}.
         98  {T}: Draw a card, then discard a card.
         40  Cast Malcolm, Alluring Scoundrel
         ...

    — not one attack declaration, and the same for "blockers". Those labels then
    let build_magezero_combat.py reverse-engineer "who is attacking" from stale
    board markers and present it as the decision the search made, producing 89
    fabricated labels out of 90 records.

    So a row is combat ONLY if its own menu offers a combat declaration.
    """
    names = {a[0] for a in pool_actions}
    if names <= {"true", "false"}:
        return "binary"
    base = DECISION_KIND_MAP.get(phase_code, "priority")
    if base in ("attackers", "blockers") and not _is_combat_menu(names):
        # Priority window that merely happens to sit in a combat step.
        return "priority"
    return base


# ── Parsing helpers ─────────────────────────────────────────────────────


def parse_pool_actions(text: str) -> list[tuple[str, float, int]]:
    return [(m[0], float(m[1]), int(m[2])) for m in RE_ACTION_TUPLE.findall(text)]


def parse_card_list(text: str) -> list[str]:
    """Split a ``-> Hand: [...]`` payload into card names.

    The GameStateEvaluator2 emitter appends an evaluator score to every hand
    entry ("Adarkar Wastes:5"); other emitters of the same line shape do not
    (8,185 of 18,943 hand lines on mz_train_smoke.log carry it). The suffix is
    log noise, not part of the card name — strip it the same way
    parse_permanents already does, or a quarter of combat-row hand mentions
    render as nonexistent card names ("Negate:5") that no card lookup can
    resolve.
    """
    text = text.strip()
    if not text:
        return []
    return [re.sub(r":\d+$", "", c.strip()).strip() for c in text.split(";")]


# Separator commas in logList output are NOT followed by a space; commas
# inside card names ("Kitsa, Otterball Elite") always are. Verified against
# magezero_card_map.json: 10 names contain a comma, 0 contain a comma not
# followed by a space.
RE_NAMED_HAND_SEP = re.compile(r",(?!\s)")


def parse_named_hand_cards(text: str) -> list[str]:
    """Split a ComputerPlayer.logList card list (trailing comma, no-space seps)."""
    text = text.strip()
    if text.endswith(","):
        text = text[:-1]
    if not text:
        return []
    return [c.strip() for c in RE_NAMED_HAND_SEP.split(text) if c.strip()]


def parse_permanents(text: str) -> list[dict[str, Any]]:
    """Split a ``-> Permanents: [...]`` payload into board entries.

    XMage appends state markers to the name ("Kitsa, Otterball Elite,tapped,
    attacking"). ``,tapped`` was always extracted; ``,attacking``/``,blocking``
    were not, so priority-row prompts rendered nonexistent card names like
    "Skrelv, Defector Mite,attacking" that no card lookup can resolve. All
    three markers are extracted into flags; flag keys are only present when
    True (matches the combat scanner's row shape).
    """
    text = text.strip()
    if not text:
        return []
    result = []
    for entry in text.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        flags = {}
        for marker in ("tapped", "attacking", "blocking"):
            token = f",{marker}"
            if token in entry:
                flags[marker] = True
                entry = entry.replace(token, "")
        name = re.sub(r":\d+$", "", entry.strip()).strip()
        result.append({"name": name, "tapped": flags.pop("tapped", False), **flags})
    return result


# ── Session detection ───────────────────────────────────────────────────


class SessionInfo:
    def __init__(self, seq: int, start_ts: str, opponent: str, n_games: int, *, primary: str = "UWTempo"):
        self.seq = seq
        self.start_ts = start_ts
        self.opponent = opponent
        self.n_games = n_games
        # Primary (MCTS) deck name for the session label. "UWTempo" is the
        # legacy default (every smoke/curriculum log to date); unseen-deck
        # logs carry their resolved deck name instead.
        self.primary = primary
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
        return f"session{self.seq}_{self.primary}_vs_{self.opponent}"


def detect_sessions(
    lines: list[str],
    is_smoke_log: bool = False,
    *,
    primary: str = "UWTempo",
    opponent_name: str | None = None,
) -> list[SessionInfo]:
    opponents = list(SMOKE_OPPONENTS_GEN0)
    sessions: list[SessionInfo] = []
    seq = 0
    opp_idx = 0

    for line in lines:
        sm = RE_SIMULATING.search(line)
        if sm:
            ts = _extract_ts(line, "unknown")
            n_games = int(sm.group(1))
            if is_smoke_log:
                opponent = opponents[opp_idx % len(opponents)]
            elif opponent_name:
                opponent = opponent_name
            else:
                opponent = f"session{seq}"
            sess = SessionInfo(seq, ts, opponent, n_games, primary=primary)
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


def parse_log(
    log_path: str,
    *,
    primary_deck: str | None = None,
    decks_dir: str | Path | None = None,
) -> tuple[list[dict], list[SessionInfo]]:
    log_name = os.path.basename(log_path)
    is_smoke = "smoke" in log_name

    # Primary-deck resolution (fail-closed; see resolve_primary_deck). None
    # means "no signature source" — legacy behavior, no runtime check.
    resolved = resolve_primary_deck(log_path, primary_deck=primary_deck, decks_dir=decks_dir)
    inferred = infer_decks_from_log_name(log_name)
    primary_label = resolved[0] if resolved else "UWTempo"
    opponent_label = inferred[1] if inferred else None

    with open(log_path, encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()

    sessions = detect_sessions(
        all_lines, is_smoke_log=is_smoke, primary=primary_label, opponent_name=opponent_label
    )

    # Per-thread state
    threads: dict[str, _ThreadState] = {}
    thread_game_seq: dict[str, int] = {}

    decisions: list[dict] = []
    pending_pool: dict[str, _PendingPool] = {}
    # Raw `playable abilities` payloads, one per thread. Kept UNSPLIT: the
    # entries are comma-joined and comma-containing, so the split needs the
    # paired pool's action names as anchors (see segment_menu).
    pending_menu: dict[str, str] = {}
    # Recovery + hand-attribution accounting, reported by --report.
    stats: dict[str, int] = {
        "recovered_pass": 0,
        "ambiguous_unconsumed": 0,
        "hand_named_attributed": 0,
        "hand_dropped_unattributed": 0,
    }

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
                _emit_pass_decision(state, prev, pending_menu.get(thread_id, ""), stats)
            _finalize_game(state, decisions, stats)
            thread_game_seq[thread_id] += 1
            state.start_new_game(thread_game_seq[thread_id])
            pending_menu.pop(thread_id, None)
            continue

        # ── logLife (turn/phase/life totals) ─────────────────────
        lm = RE_LOG_LIFE.search(line)
        if lm:
            state.on_log_life(int(lm.group(1)), lm.group(2), lm.group(3), int(lm.group(4)), int(lm.group(5)))
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
                    _emit_pass_decision(state, prev, pending_menu.pop(thread_id, ""), stats)
                pending_pool[thread_id] = _PendingPool(
                    actions=actions,
                    phase_code=pool_match.group(1),
                    turn=state.last_turn,
                )
            continue

        # ── Playable abilities (legal menu) ──────────────────────
        pm = RE_PLAYABLE.search(line)
        if pm:
            pending_menu[thread_id] = pm.group(1)
            continue

        # ── Chose action (the decision label) ────────────────────
        cm = RE_CHOSE_ACTION.search(line)
        if cm:
            turn = int(cm.group(1))
            phase_code = cm.group(3)
            chosen = cm.group(4).strip()

            pending = pending_pool.pop(thread_id, None)
            menu_raw = pending_menu.pop(thread_id, "")

            if pending is None:
                continue  # Minimax decision, skip
            pool_actions = pending.actions

            # The chosen name comes from the (comma-safe) `chose action` line;
            # add it to the anchors in case the paired pool is a sub-decision
            # pool ("(top: X)pool=") whose names are not the menu's entries.
            menu = segment_menu(menu_raw, [name for name, _, _ in pool_actions] + [chosen])

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
                "_hand_positional": [],
            }
            state.game_decisions.append(decision)
            continue

        # ── Player life: also opens a battlefield block (#430) ───
        pl = RE_PLAYER_LIFE.search(line)
        if pl:
            em = RE_EMITTER.search(line)
            state.on_block_header(pl.group(1), int(pl.group(2)), em.group(1) if em else None)
            continue

        # ── Name-attributed hand (ComputerPlayer.logList): the ONLY source
        # that may fill a training row's `hand`. The line itself names the
        # player, the turn, and the phase — no positional inference. Lines
        # naming any other player are ignored (their hand is hidden
        # information; leak class L4). ──────────────────────────────────
        nh = RE_NAMED_HAND.search(line)
        if nh:
            if nh.group(4) == PRIMARY_PLAYER:
                state.named_hands[int(nh.group(1))] = parse_named_hand_cards(nh.group(5))
            continue

        # ── Positional `-> Hand:` block lines: DIAGNOSTIC ONLY. They are
        # attributed by block-header adjacency (the #452 defect family), so
        # they feed the internal `_hand_positional` shadow used by --report
        # reconciliation, never the `hand` field itself. PlayerB blocks are
        # the opponent's hidden hand and are skipped entirely (leak L4). ─
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
            _emit_pass_decision(st, prev, pending_menu.get(tid, ""), stats)
    pending_pool.clear()

    # Finalize remaining games
    for state in threads.values():
        _finalize_game(state, decisions, stats)

    # Enrich with sessions using per-thread game counts
    _enrich_sessions(decisions, sessions)

    # Runtime deck-signature guardrail (raises DeckSignatureError on any
    # off-deck hand card; see verify_hand_signature).
    if resolved is not None:
        stats["deck_signature_checked_rows"] = verify_hand_signature(decisions, resolved[0], resolved[1])
    else:
        stats["deck_signature_checked_rows"] = 0

    LAST_PARSE_STATS.update(stats)

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
        # Name-attributed hands from ComputerPlayer.logList, keyed by TURN.
        # Reset per game. The value may legitimately be [] (empty hand at
        # upkeep) — presence of the key means "attributed", absence means
        # the row's hand cannot be sourced and the row is dropped+counted.
        self.named_hands: dict[int, list[str]] = {}
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
        self.named_hands = {}
        self.boards = {"PlayerA": [], "PlayerB": []}
        self.last_log_life_a = None
        self.last_log_life_b = None
        self.last_turn = 0
        self._block_player = None
        self._block_emitter = None
        self._block_perm_lines = 0
        self._block_flushed = False

    def on_log_life(self, turn: int, phase_name: str, phase_code: str, life_a: int, life_b: int):
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
        # DIAGNOSTIC ONLY since the hand-attribution switch: positional
        # `-> Hand:` block lines feed `_hand_positional` (reconciliation
        # shadow), never the `hand` field. PlayerB blocks are the opponent's
        # hidden hand (leak class L4) and are skipped entirely.
        if self._block_player == "PlayerB":
            return
        self.latest_hand = cards
        _backfill_positional_hand(self, cards)

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


def _backfill_positional_hand(state: _ThreadState, cards: list[str]):
    """Shadow of the PRE-switch hand flow, kept for --report reconciliation.

    Fills the internal `_hand_positional` field exactly the way the old
    parser filled `hand` (oldest-unfilled-first), so the report can measure
    how often the positional source disagreed with the name-attributed one.
    Stripped on write; never reaches a training row.
    """
    for d in reversed(state.game_decisions):
        if not d.get("_hand_positional"):
            d["_hand_positional"] = list(cards)
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


def _finalize_game(state: _ThreadState, all_decisions: list[dict], stats: dict[str, int]):
    """Close out a game: assign hands, outcomes, boards; emit surviving rows.

    Hand attribution is by NAME + THREAD + TURN: a row's hand comes from the
    ComputerPlayer.logList line for the primary player on this thread-game
    with the row's turn number. A row whose turn has no such line is DROPPED
    and counted (`hand_dropped_unattributed`) — never guessed, never emitted
    with a fabricated "empty hand" (an empty hand in a prompt asserts "you
    hold nothing", which is an observation, not an absence of one).

    Rows are appended to `all_decisions` here, not at creation, so a drop is
    a real drop and not a mutation race.
    """
    if not state.game_decisions:
        return
    outcome = state.infer_outcome()
    for d in state.game_decisions:
        d["outcome"] = outcome
        # Pre-switch shadow fallback, mirroring the old `hand` finalize path.
        if not d.get("_hand_positional"):
            d["_hand_positional"] = list(state.latest_hand)
        if not d.pop("_bf_filled", False):
            d["battlefield_self"] = list(state.boards["PlayerA"])
            d["battlefield_opp"] = list(state.boards["PlayerB"])

        named = state.named_hands.get(d.get("turn", -1))
        if named is None:
            stats["hand_dropped_unattributed"] += 1
            continue
        d["hand"] = list(named)
        stats["hand_named_attributed"] += 1
        all_decisions.append(d)
    state.game_decisions = []


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
    parser = argparse.ArgumentParser(description="Parse MageZero XMage logs into decisions JSONL.")
    parser.add_argument(
        "--log", required=True, action="append", dest="logs", help="Path(s) to mz_train.log file(s)"
    )
    parser.add_argument("--out", required=True, help="Output JSONL path")
    parser.add_argument("--report", action="store_true", help="Print reconciliation report")
    parser.add_argument(
        "--primary-deck",
        default=None,
        help="Primary (MCTS) deck name, e.g. HighNoonControl. Overrides filename "
        "inference; the .dck must exist under --decks-dir (fail closed). "
        "Enables the runtime hand deck-signature guardrail.",
    )
    parser.add_argument(
        "--decks-dir",
        default=None,
        help=f"XMage deck directory (default: {DEFAULT_DECKS_DIR})",
    )
    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    all_decisions: list[dict] = []
    win_rates_by_log: dict[str, list[dict]] = {}

    for log_path in args.logs:
        log_name = os.path.basename(log_path)
        decisions, sessions = parse_log(
            log_path, primary_deck=args.primary_deck, decks_dir=args.decks_dir
        )
        all_decisions.extend(decisions)

        wr_lines = []
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = RE_WIN_RATE.search(line)
                if m:
                    wr_lines.append(
                        {
                            "win_rate": float(m.group(1)),
                            "n_wins": int(m.group(2)),
                            "n_total": int(m.group(3)),
                        }
                    )
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


def _print_report(
    decisions: list[dict], win_rates_by_log: dict[str, list[dict]], log_paths: list[str], out_path: Path
):

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

    n_named = LAST_PARSE_STATS.get("hand_named_attributed", 0)
    n_dropped = LAST_PARSE_STATS.get("hand_dropped_unattributed", 0)
    print("\n  Hand attribution (name-attributed ComputerPlayer.logList only):")
    print(f"    Rows with named hand:        {n_named}")
    print(f"    Rows DROPPED (no named hand line for the row's turn): {n_dropped}")
    n_sig = LAST_PARSE_STATS.get("deck_signature_checked_rows", 0)
    if n_sig:
        print(f"    Deck-signature guardrail:    PASSED on {n_sig} rows (0 off-deck hand cards)")
    else:
        print("    Deck-signature guardrail:    NOT RUN (no primary deck resolved; legacy log)")
    # Reconciliation against the pre-switch positional source (diagnostic;
    # `:N` evaluator score suffixes are stripped before comparing so only
    # CONTENT differences count, not format pollution).
    both = 0
    differ = 0
    _score_suffix = re.compile(r":\d+$")
    for d in decisions:
        pos = d.get("_hand_positional")
        if not pos:
            continue
        both += 1
        pos_norm = sorted(_score_suffix.sub("", c).strip() for c in pos)
        if pos_norm != sorted(d.get("hand", [])):
            differ += 1
    if both:
        print(
            f"    Positional-source disagreement (diagnostic): {differ}/{both} = {100 * differ / both:.1f}%"
        )

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
            games_won = {
                d.get("game_id", "")
                for d in log_decisions
                if d.get("session") == sess and d.get("outcome") == "won"
            }
            games_lost = {
                d.get("game_id", "")
                for d in log_decisions
                if d.get("session") == sess and d.get("outcome") == "lost"
            }
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
                expected_wins = (
                    round(wr["n_wins"] / wr["n_total"] * inferred_games) if wr["n_total"] > 0 else 0
                )
                diff = abs(inferred_wins - expected_wins)
                mark = "✅" if diff <= 2 else "⚠️"
                print(
                    f"      {mark} {sess}: logged {wr['n_wins']}/{wr['n_total']} "
                    f"→ inferred {inferred_wins}/{inferred_games} (expected {expected_wins}, diff={diff})"
                )

            # Overall reconciliation (proportional)
            total_inferred_wins = sum(o["won"] for o in session_outcomes.values())
            total_inferred_games = len(log_games)
            total_expected_wins = (
                round(total_logged_wins / total_logged_games * total_inferred_games)
                if total_logged_games > 0
                else 0
            )
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
