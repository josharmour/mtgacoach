#!/usr/bin/env python3
"""Fail-closed PLAY-DECISION gate (docs/rl-training-status.md §16, §17.4).

Why this file exists
--------------------
The Stage-0 gate corpus (`gate_prompts_v2.jsonl`) is **100% mulligan**, so it
cannot measure the feature the project is now building: production-shaped
play decisions — a numbered ``Legal:`` menu answered with ``{"pick": N}``.
This gate scores exactly that, on **real MTGA menus**.

Corpus provenance — real, not synthesised
-----------------------------------------
Everything here is derived from ``tools/eval/data/replay_menu_groundtruth.jsonl``
(665 start-of-own-Main-1 decisions extracted verbatim from 104 MTGA ``.rply``
replays by ``tools/eval/replay/menu_groundtruth.py``): the menu is MTGA's own
``actionsAvailableReq.actions[]`` and the label is the action the human
actually submitted (joined ``performActionResp.respId == msgId``). No menu is
synthesised, no label is inferred.

Prompt construction — production functions, reconstructed state
---------------------------------------------------------------
The prompt is built by **calling production code**, not by re-implementing it
(the failure mode that killed both prior training runs, §8b / §15):

  * ``system`` is the imported ``arenamcp.action_planner.AUTOPILOT_SYSTEM_PROMPT``
    object — never retyped.
  * ``user`` is produced by ``ActionPlanner._build_action_prompt()``, which
    calls ``CoachEngine._format_game_context(for_planner=True)`` and rewrites
    the ``Legal:`` line into the numbered menu exactly as production does.
  * ``[OK]`` affordability tags come from production's own fallback path,
    ``RulesEngine._can_afford`` over ``RulesEngine._get_mana_pool`` — NOT from
    the replay, because MTGA's ``actions[]`` is not affordability-filtered
    (measured: 49% of active Cast entries exceed the untapped-land count).

**Honest limits of the reconstruction** (recorded in the manifest under
``prompt_fidelity``): the groundtruth records carry a *summarised* board, so
the prompt lacks live power/toughness modifications, counters, opponent tapped
state, graveyards, the stack, and the raw GRE block. Base P/T and oracle text
are restored from the card DB. This is production-*shaped*, not byte-identical
to a live prompt; §17.2's byte-identity path belongs to the dataset builder.

Mana-level actions (``ActionType_Activate_Mana`` / ``ActionType_FloatMana``)
are dropped from the numbered menu because production drops them
(``action_planner.py``: they were the #1 source of unmatchable proposals).
Decisions whose human pick *was* a mana action are dropped from the corpus and
counted in the manifest.

Splits
------
Grouped by replay file (game-level leakage guard) and stratified by format
bucket, because the corpus is format-skewed — 69 Brawl / 26 Constructed /
8 Limited replay files. The **non-Brawl slice is primary** (17lands training
data is Limited); every accuracy number is also reported per bucket.

  * ``test``  — NEVER used for training. The gate scores this.
  * ``dev``   — separate, for iteration.
  * ``train`` — everything else; the trainable replay list is written out so
    the dataset builder can exclude the held-out games mechanically.

Gates (§17.4) — all hard, all fail-closed
-----------------------------------------
  G1 schema validity          >= 99%   (``validators.validate_action_schema_json``)
  G2 legality                 == 100%  pick index within [1, menu_size];
                                       ANY violation is a hard fail —
                                       production has to execute it
  G3 permutation invariance   |acc(identity) - acc(permuted)| <= 0.05 on the
                              permuted twin corpus (same decisions, shuffled
                              menus). A large gap means the model memorised
                              positions instead of reading the menu. Caveat
                              to read with it: a model that reads the menu but
                              breaks ties by POSITION (e.g. "play whichever
                              land is listed first") also moves under a
                              shuffle — the measured always-land reference
                              policy swings 0.64 -> 0.51. The report's
                              ``action_agreement`` separates the two: a
                              memoriser's agreement collapses, a tie-breaker's
                              stays high.
  G4 pick accuracy            reported with binomial (Wilson) 95% CIs, split
                              by format bucket, land-drop vs strategic, and
                              seen-vs-unseen menus
  G5 non-inferiority          paired-bootstrap 95% CI of the accuracy delta
                              vs a declared baseline (same estimator as
                              ``gate_stage0.paired_bootstrap_ci``, imported)

ANY missing input, sample shortfall, or exception => verdict BLOCKED, exit 2,
no registration. There is no bypass flag of any kind.

Honesty guard: 67% of this corpus's picks are land drops, so a policy that
always plays a land scores deceptively well. Every report carries
``reference_policies`` (always-first / always-land / always-pass / random) and
a ``strategic`` slice that excludes land drops. Read those before believing an
accuracy number.

Usage
-----
    # 1. build the corpus + permuted twins + split manifest
    python -m tools.training.gate_play_decisions build

    # 2. get responses (any runner that reads {id, system, user})
    python -m tools.eval.run \\
        --prompts tools/training/data/gate_play_decisions_test.jsonl \\
        --responses tools/eval/data/pd_candidate.jsonl \\
        --backend online:deepseek-v4-flash

    # ... or prove the harness with a synthetic policy (no model, no GPU)
    python -m tools.training.gate_play_decisions simulate \\
        --corpus tools/training/data/gate_play_decisions_test.jsonl \\
        --policy first --out tools/eval/data/pd_first.jsonl

    # 3. gate
    python -m tools.training.gate_play_decisions gate \\
        --corpus tools/training/data/gate_play_decisions_test.jsonl \\
        --permuted-corpus tools/training/data/gate_play_decisions_test_permuted.jsonl \\
        --responses tools/eval/data/pd_candidate.jsonl \\
        --permuted-responses tools/eval/data/pd_candidate_permuted.jsonl \\
        --baseline-responses tools/eval/data/pd_baseline.jsonl \\
        --backend-label <candidate> --baseline-label <baseline> \\
        --report tools/training/data/gate_play_report.json
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import logging
import math
import random
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.training.gate_stage0 import paired_bootstrap_ci  # noqa: E402
from tools.training.validators import validate_action_schema_json  # noqa: E402

logger = logging.getLogger("tools.training.gate_play_decisions")

# --------------------------------------------------------------------------
# Constants — thresholds are §17.4's, not negotiable at the CLI except upward
# --------------------------------------------------------------------------

DEFAULT_GROUNDTRUTH = REPO / "tools" / "eval" / "data" / "replay_menu_groundtruth.jsonl"
DEFAULT_OUT_DIR = REPO / "tools" / "training" / "data"
CORPUS_STEM = "gate_play_decisions"

MIN_SAMPLES = 100  # gate refuses to certify the test split below this
MIN_NONBRAWL_SAMPLES = 40  # the non-Brawl slice is primary — needs a real n
MIN_STRATEGIC_SAMPLES = 30  # non-land-drop decisions; the honest headline
MIN_SCHEMA_RATE = 0.99  # G1
MAX_LEGALITY_VIOLATIONS = 0  # G2 — any violation is a hard fail
MAX_PERMUTATION_ACC_GAP = 0.05  # G3
MAX_ACCURACY_REGRESSION = 0.02  # G5

DEFAULT_SEED = 20260725
DEFAULT_TEST_FRACTION = 0.30
DEFAULT_DEV_FRACTION = 0.15
DEFAULT_DECK_OVERLAP_THRESHOLD = 0.5

# Mirrors tools/eval/replay/menu_groundtruth.MANA_LEVEL_ACTION_TYPES. Kept as a
# local copy so the gate path has no dependency on the replay reader stack;
# tests/test_gate_play_decisions.py asserts the two stay in sync.
MANA_LEVEL_ACTION_TYPES = frozenset({"ActionType_Activate_Mana", "ActionType_FloatMana"})

# The trigger production fires for a start-of-own-main-1 priority window.
TRIGGER = "new_turn"


# --------------------------------------------------------------------------
# Small IO / stats helpers
# --------------------------------------------------------------------------


def _read_jsonl(path: Path) -> Iterator[dict]:
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def _write_jsonl(path: Path, rows: Iterable[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
    return n


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def wilson_ci(successes: int, n: int, z: float = 1.959963984540054) -> list[float] | None:
    """Binomial 95% confidence interval (Wilson score, no scipy).

    Wilson rather than normal-approximation because the per-format slices are
    small (Limited is ~26 decisions) and the normal interval misbehaves there.
    """
    if n <= 0:
        return None
    p = successes / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except Exception:
        return ""


# --------------------------------------------------------------------------
# Corpus construction
# --------------------------------------------------------------------------


def format_bucket(super_format: str, variant: str) -> str:
    """Collapse (superFormat, variant) into the three buckets that exist.

    The corpus is 69 Brawl / 26 Constructed / 8 Limited replay files; Brawl is
    a singleton-commander format whose decisions generalise least to the
    Limited data the model is trained on, so it is reported separately and
    treated as secondary.
    """
    if super_format == "SuperFormat_Limited":
        return "limited"
    if variant == "GameVariant_Brawl":
        return "brawl"
    return "constructed"


def entry_action_key(entry: dict) -> str:
    """Identity of the *action*, ignoring which copy of a card supplies it.

    Two Plains in hand produce two menu entries that are strategically the
    same choice; crediting only the exact instance the human clicked would
    understate accuracy. Ability activations keep their abilityGrpId because
    different abilities on one permanent are different choices.
    """
    atype = str(entry.get("action_type") or "?").replace("ActionType_", "")
    grp = entry.get("grp_id")
    ability = entry.get("ability_grp_id")
    if grp is None:
        return atype
    if ability is not None:
        return f"{atype}:{grp}:{ability}"
    return f"{atype}:{grp}"


def entry_identity(entry: dict) -> tuple:
    """Exact identity of a menu row (used to locate the human's pick)."""
    return (
        entry.get("action_type"),
        entry.get("grp_id"),
        entry.get("instance_id"),
        entry.get("ability_grp_id"),
    )


class _CardFacts:
    """Card lookups for state reconstruction (build-time only).

    ``gate`` never touches this: the built corpus carries everything the gate
    needs, so gating runs on any machine without a card database.
    """

    def __init__(self, *, allow_network: bool = False) -> None:
        self._cache: dict[int, dict] = {}
        self._db = None
        self._mtga = None
        self.creature_rows = 0
        self.unknown_pt_rows = 0
        try:
            from arenamcp.mtgadb import MTGADatabase

            with contextlib.redirect_stdout(io.StringIO()):
                self._mtga = MTGADatabase()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"MTGA local DB unavailable (no base P/T): {type(e).__name__}: {e}")
        try:
            if allow_network:
                from arenamcp.card_db import get_card_database

                with contextlib.redirect_stdout(io.StringIO()):
                    self._db = get_card_database()
            else:
                # Local sources only. Scryfall's API fallback would make the
                # built corpus depend on network weather (and it rate-limits at
                # this volume) — a gate corpus has to be reproducible.
                from arenamcp.card_db import create_card_database
                from arenamcp.mtgjson import get_mtgjson

                mtgjson = None
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        mtgjson = get_mtgjson()
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"MTGJSON unavailable: {type(e).__name__}: {e}")
                self._db = create_card_database(scryfall_cache=None, mtgjson_db=mtgjson, mtga_db=self._mtga)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"card database unavailable: {type(e).__name__}: {e}")

    def get(self, grp_id: Any) -> dict:
        if grp_id is None:
            return {
                "name": "",
                "type_line": "",
                "oracle_text": "",
                "mana_cost": "",
                "cmc": 0.0,
                "power": None,
                "toughness": None,
                "resolved": False,
            }
        gid = int(grp_id)
        hit = self._cache.get(gid)
        if hit is not None:
            return hit
        info = None
        if self._db is not None:
            try:
                info = self._db.get_card_by_arena_id(gid)
            except Exception:  # noqa: BLE001
                info = None
        out = {
            "name": getattr(info, "name", None) or f"#{gid}",
            "type_line": getattr(info, "type_line", "") or "",
            "oracle_text": getattr(info, "oracle_text", "") or "",
            "mana_cost": getattr(info, "mana_cost", "") or "",
            "cmc": getattr(info, "cmc", 0.0) or 0.0,
            "power": None,
            "toughness": None,
            "resolved": info is not None,
        }
        if self._mtga is not None:
            try:
                card = self._mtga.get_card(gid)
            except Exception:  # noqa: BLE001
                card = None
            if card is not None:
                # Base printed P/T only. Counters and static buffs are not
                # recoverable from the summarised replay state — recorded as a
                # fidelity limit in the manifest.
                out["power"] = getattr(card, "power", "") or None
                out["toughness"] = getattr(card, "toughness", "") or None
                if not out["oracle_text"]:
                    out["oracle_text"] = getattr(card, "oracle_text", "") or ""
        self._cache[gid] = out
        return out


def _as_int_pt(value: Any) -> int | None:
    """Printed P/T as an int, or None for ``*``/unknown.

    Production snapshots carry numeric power/toughness and the coach formatter
    sums them (``_format_attack_combat``); a string there raises. Variable
    (``*``) P/T is genuinely not a number, so it is reported as unknown rather
    than guessed at.
    """
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _battlefield_card(
    obj: dict, seat: int, facts: _CardFacts, *, synthetic_instance: int | None = None
) -> dict:
    """One battlefield row in the shape ``_format_game_context`` consumes."""
    grp = obj.get("grp_id")
    f = facts.get(grp)
    is_creature = "creature" in (f["type_line"] or "").lower()
    card = {
        "instance_id": obj.get("instance_id", synthetic_instance),
        "grp_id": grp,
        "name": obj.get("name") or f["name"],
        "type_line": f["type_line"],
        "oracle_text": f["oracle_text"],
        "mana_cost": f["mana_cost"],
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "is_tapped": bool(obj.get("is_tapped", False)),
        # Start of own Main 1: nothing can have entered this turn, so no
        # permanent is summoning sick. -1 states "entered before this turn".
        "turn_entered_battlefield": -1,
    }
    if is_creature:
        facts.creature_rows += 1
        power = _as_int_pt(f["power"])
        toughness = _as_int_pt(f["toughness"])
        if power is None or toughness is None:
            # ``*`` P/T and DB misses (0.6% of creature rows in this corpus)
            # fall back to 0; counted so the manifest can report the rate.
            facts.unknown_pt_rows += 1
        card["power"] = power if power is not None else 0
        card["toughness"] = toughness if toughness is not None else 0
    return card


def build_game_state(rec: dict, facts: _CardFacts) -> dict:
    """Reconstruct a production-shaped snapshot from a groundtruth record."""
    state = rec["state"]
    local = int(rec["local_seat"])
    opp = 1 if local == 2 else 2

    battlefield: list[dict] = []
    for obj in state.get("lands_in_play") or []:
        battlefield.append(_battlefield_card(obj, local, facts))
    for obj in state.get("creatures_in_play") or []:
        battlefield.append(_battlefield_card(obj, local, facts))
    for obj in state.get("other_permanents") or []:
        battlefield.append(_battlefield_card(obj, local, facts))
    # Opponent permanents survive the extractor as bare grpIds — no instance
    # ids, no tapped state. Synthetic instance ids keep the formatter's
    # attachment/dedup logic working; they are never scored against.
    for base, key in (
        (900000, "opp_lands_grp_ids"),
        (910000, "opp_creatures_grp_ids"),
        (920000, "opp_other_grp_ids"),
    ):
        for i, grp in enumerate(state.get(key) or []):
            battlefield.append(_battlefield_card({"grp_id": grp}, opp, facts, synthetic_instance=base + i))

    hand = []
    for card in state.get("hand") or []:
        f = facts.get(card.get("grp_id"))
        hand.append(
            {
                "instance_id": card.get("instance_id"),
                "grp_id": card.get("grp_id"),
                "name": card.get("name") or f["name"],
                "type_line": card.get("type_line") or f["type_line"],
                "mana_cost": card.get("mana_cost") or f["mana_cost"],
                "cmc": card.get("cmc") if card.get("cmc") is not None else f["cmc"],
                "oracle_text": f["oracle_text"],
            }
        )

    life = state.get("life") or {}
    return {
        "players": [
            {
                "seat_id": local,
                "is_local": True,
                "life_total": life.get("you"),
                # Start of own Main 1 — the land drop has not been used yet.
                "lands_played": 0,
                "hand_size": state.get("hand_size", len(hand)),
            },
            {
                "seat_id": opp,
                "is_local": False,
                "life_total": life.get("opp"),
                "lands_played": 0,
                "hand_size": state.get("opp_hand_size", 0),
            },
        ],
        "turn": {
            "turn_number": rec.get("turn_number", 0),
            "phase": rec.get("phase", "Phase_Main1"),
            "step": rec.get("step", ""),
            "active_player": rec.get("active_player"),
            "priority_player": rec.get("priority_player"),
        },
        "battlefield": battlefield,
        "hand": hand,
        "stack": [],
        "graveyard": [],
        "legal_actions": [],
    }


def _affordable(entry: dict, mana_pool: dict) -> bool:
    """Production's own fallback affordability check.

    MTGA's ``actions[]`` is NOT affordability-filtered (measured: 49% of active
    Cast entries in this corpus exceed the untapped-land count), and the
    groundtruth records do not carry ``autoTapSolution``. Tagging every Cast
    ``[OK]`` would therefore be a fabrication, and the system prompt tells the
    model to *trust* ``[OK]``. So the tag is recomputed the same way
    ``gamestate_decisions`` does when the GRE gives no autotap solution.
    """
    from arenamcp.rules_engine import RulesEngine

    cost = entry.get("mana_cost") or ""
    if not cost:
        return False
    try:
        return bool(RulesEngine._can_afford(cost, mana_pool))
    except Exception:  # noqa: BLE001
        return False


def menu_line(entry: dict, affordable: bool) -> str:
    """Legal-action text in production's wording (``gamestate_decisions``)."""
    atype = entry.get("action_type")
    name = entry.get("name") or "?"
    if atype == "ActionType_Pass":
        return "Pass"
    if atype == "ActionType_Play":
        return f"Play Land: {name}"
    if atype == "ActionType_Cast":
        return f"Cast {name}{' [OK]' if affordable else ''}"
    if atype == "ActionType_Activate":
        # No ability cost is recoverable, so no [OK] is claimed. The system
        # prompt already states that a missing [OK] on an activation does not
        # imply it is unaffordable.
        return f"Activate Ability: {name}"
    return f"Action: {str(atype).replace('ActionType_', '').lower()}"


def _new_planner():
    """A bare ActionPlanner used purely as the prompt builder.

    ``__new__`` avoids the planner's live dependencies (backend, game state
    manager); the per-call attributes ``_build_action_prompt`` reads are set
    explicitly so the prompt has no turn plan / memo carry-over.
    """
    from arenamcp.action_planner import ActionPlanner

    planner = ActionPlanner.__new__(ActionPlanner)
    planner._game_plan = ""
    planner._turn_intent = ""
    planner._turn_memo = None
    planner._turn_memo_turn = -1
    planner._active_turn_plan = None
    planner._turn_executed = []
    planner._last_menu = []
    return planner


def build_user_message(game_state: dict, menu_texts: list[str]) -> str:
    """Call production's prompt builder. Never re-implement it (§17.2).

    ``_build_action_prompt`` swallows a formatter exception and degrades to
    ``_fallback_format``, whose output is NOT production-shaped. A corpus built
    from that would repeat the exact failure that killed the two prior runs, so
    the degraded shape is detected here and raises instead.
    """
    planner = _new_planner()
    game_state = dict(game_state)
    game_state["legal_actions"] = list(menu_texts)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        message = planner._build_action_prompt(game_state, TRIGGER, legal_actions=list(menu_texts))
    if "Legal: (pick by number)" not in message or "Life: You=" not in message:
        raise RuntimeError(
            "production formatter degraded to the fallback shape — refusing to build a "
            "non-production-shaped corpus"
        )
    return message


def build_decision(rec: dict, facts: _CardFacts) -> dict | None:
    """Groundtruth record -> scoreable decision, or None if unusable.

    Returns a dict with the menu, the 1-based gold pick, and the metadata the
    gate reports on. Prompt text is added later (after split assignment) so
    the permuted twin can share this work.
    """
    pick = rec.get("real_pick") or {}
    if pick.get("status") != "ok":
        return {"drop_reason": "unresolved_pick"}

    full_menu = rec.get("real_menu") or []
    idx = pick.get("menu_index")
    if idx is None or not (0 <= int(idx) < len(full_menu)):
        return {"drop_reason": "pick_index_out_of_range"}
    gold_entry = full_menu[int(idx)]

    # Production strips mana-level actions from the numbered menu.
    menu = [e for e in full_menu if e.get("action_type") not in MANA_LEVEL_ACTION_TYPES]
    if gold_entry.get("action_type") in MANA_LEVEL_ACTION_TYPES:
        return {"drop_reason": "gold_pick_is_mana_action"}
    if len(menu) < 2:
        return {"drop_reason": "menu_too_small"}

    game_state = build_game_state(rec, facts)
    from arenamcp.rules_engine import RulesEngine

    try:
        with contextlib.redirect_stdout(io.StringIO()):
            mana_pool = RulesEngine._get_mana_pool(game_state, int(rec["local_seat"]))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"mana pool failed for {rec.get('replay_file')}#{rec.get('decision_index')}: {e}")
        mana_pool = {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0, "Any": 0, "total": 0}

    gold_identity = entry_identity(gold_entry)
    gold_key = entry_action_key(gold_entry)
    rows = []
    gold_pick = None
    for i, entry in enumerate(menu):
        affordable = entry.get("action_type") in ("ActionType_Cast",) and _affordable(entry, mana_pool)
        rows.append(
            {
                "index": i + 1,
                "text": menu_line(entry, affordable),
                "action_key": entry_action_key(entry),
                "action_type": entry.get("action_type"),
                "grp_id": entry.get("grp_id"),
                "instance_id": entry.get("instance_id"),
                "name": entry.get("name"),
            }
        )
        if entry_identity(entry) == gold_identity and gold_pick is None:
            gold_pick = i + 1
    if gold_pick is None:
        return {"drop_reason": "gold_pick_lost_in_filter"}

    equivalents = [r["index"] for r in rows if r["action_key"] == gold_key]
    # Every card id this decision exposes — used at split time to measure how
    # much of the held-out game the training split has already seen.
    card_ids = {r["grp_id"] for r in rows if r["grp_id"] is not None}
    card_ids |= {c.get("grp_id") for c in game_state["hand"] if c.get("grp_id") is not None}
    card_ids |= {
        c.get("grp_id")
        for c in game_state["battlefield"]
        if c.get("grp_id") is not None and c.get("owner_seat_id") == int(rec["local_seat"])
    }
    return {
        "game_state": game_state,
        "_card_ids": sorted(card_ids),
        "_menu_card_ids": sorted({r["grp_id"] for r in rows if r["grp_id"] is not None}),
        "menu": rows,
        "gold_pick": gold_pick,
        "gold_action_key": gold_key,
        "gold_action_type": gold_entry.get("action_type"),
        "gold_equivalent_picks": equivalents,
        "menu_size": len(rows),
        "is_land_drop": gold_entry.get("action_type") == "ActionType_Play",
        "has_land_option": any(r["action_type"] == "ActionType_Play" for r in rows),
        "pass_pick": next((r["index"] for r in rows if r["action_type"] == "ActionType_Pass"), None),
        "first_land_pick": next((r["index"] for r in rows if r["action_type"] == "ActionType_Play"), None),
        "menu_key_spell": sorted({r["action_key"] for r in rows}),
        "format": f"{rec.get('super_format')}/{rec.get('variant')}",
        "format_bucket": format_bucket(rec.get("super_format", ""), rec.get("variant", "")),
        "source": {
            "replay_file": rec.get("replay_file"),
            "decision_index": rec.get("decision_index"),
            "msg_id": rec.get("msg_id"),
            "game_number": rec.get("game_number"),
            "turn_number": rec.get("turn_number"),
            "main1_window_ordinal": rec.get("main1_window_ordinal"),
        },
    }


def permute_decision(decision: dict, seed_key: str) -> dict:
    """Permuted twin: identical decision, shuffled menu.

    The shuffle is forced to move the gold pick (when the menu allows it) so a
    model that memorised "the answer is usually index 2" is measurably wrong on
    the twin. Deterministic in ``seed_key`` so twins are reproducible.
    """
    rng = random.Random(seed_key)
    n = len(decision["menu"])
    order = list(range(n))
    gold_old = decision["gold_pick"] - 1
    for _ in range(64):
        rng.shuffle(order)
        if order.index(gold_old) != gold_old:
            break
    rows = []
    for new_i, old_i in enumerate(order):
        row = dict(decision["menu"][old_i])
        row["index"] = new_i + 1
        rows.append(row)
    out = dict(decision)
    out["menu"] = rows
    out["gold_pick"] = order.index(gold_old) + 1
    out["gold_equivalent_picks"] = [
        r["index"] for r in rows if r["action_key"] == decision["gold_action_key"]
    ]
    out["pass_pick"] = next((r["index"] for r in rows if r["action_type"] == "ActionType_Pass"), None)
    out["first_land_pick"] = next((r["index"] for r in rows if r["action_type"] == "ActionType_Play"), None)
    out["permutation"] = [i + 1 for i in order]
    return out


def to_corpus_record(decision: dict, record_id: str, split: str, variant: str, twin_id: str) -> dict:
    """Emit the {id, system, user, meta} shape ``tools.eval.run`` consumes."""
    from arenamcp.action_planner import AUTOPILOT_SYSTEM_PROMPT

    menu_texts = [r["text"] for r in decision["menu"]]
    user = build_user_message(decision["game_state"], menu_texts)
    # "_"-prefixed keys are build-time bookkeeping and never reach the corpus.
    meta = {k: v for k, v in decision.items() if k != "game_state" and not k.startswith("_")}
    meta["split"] = split
    meta["variant"] = variant
    meta["twin_id"] = twin_id
    return {
        "id": record_id,
        "system": AUTOPILOT_SYSTEM_PROMPT,
        "user": user,
        "max_tokens": 400,
        "temperature": 0.0,
        "meta": meta,
    }


# --------------------------------------------------------------------------
# Split assignment
# --------------------------------------------------------------------------


def assign_splits(
    decisions_by_file: dict[str, list[dict]],
    *,
    seed: int,
    test_fraction: float,
    dev_fraction: float,
    min_test_files: int = 3,
    min_dev_files: int = 2,
) -> dict[str, str]:
    """Replay-file level, format-stratified, deterministic.

    Splitting by replay FILE (not by decision) is the leakage guard: two
    decisions from the same game share a board, a deck and an opponent.
    Stratifying by format bucket keeps the scarce non-Brawl games out of a
    single split — with 8 Limited files, a naive random split can easily put
    zero Limited games in test. ``min_test_files``/``min_dev_files`` floor the
    small buckets so Limited is always represented (it is the format 17lands
    training data comes from, so it is the slice that matters most), at the
    cost of leaving that bucket little training data — acceptable, because
    Limited training volume comes from 17lands, not from these replays.
    """
    buckets: dict[str, list[str]] = defaultdict(list)
    for fname, decisions in decisions_by_file.items():
        buckets[decisions[0]["format_bucket"]].append(fname)

    assignment: dict[str, str] = {}
    for bucket in sorted(buckets):
        files = sorted(buckets[bucket])
        rng = random.Random(f"{seed}:{bucket}")
        rng.shuffle(files)
        n_test = max(min_test_files, round(len(files) * test_fraction))
        n_dev = max(min_dev_files, round(len(files) * dev_fraction))
        if n_test + n_dev >= len(files):
            raise ValueError(
                f"format bucket {bucket!r} has {len(files)} replay files — cannot carve "
                f"test={n_test}/dev={n_dev} and leave any training data"
            )
        for f in files[:n_test]:
            assignment[f] = "test"
        for f in files[n_test : n_test + n_dev]:
            assignment[f] = "dev"
        for f in files[n_test + n_dev :]:
            assignment[f] = "train"
    return assignment


# --------------------------------------------------------------------------
# Reference policies (the honesty guard)
# --------------------------------------------------------------------------


def reference_policies(records: list[dict]) -> dict:
    """Accuracy of trivial policies on this corpus.

    67% of start-of-main-1 picks in this corpus are land drops, so
    ``always_land`` scores far above chance while knowing nothing about Magic.
    Any candidate that does not clear these numbers has learned nothing.
    """
    n = len(records)
    if not n:
        return {}

    def _acc(fn) -> float:
        hits = 0
        for rec in records:
            meta = rec["meta"]
            pick = fn(meta)
            if pick is not None and pick in meta["gold_equivalent_picks"]:
                hits += 1
        return round(hits / n, 4)

    random_expectation = round(
        sum(len(r["meta"]["gold_equivalent_picks"]) / r["meta"]["menu_size"] for r in records) / n, 4
    )
    return {
        "n": n,
        "always_first_option": _acc(lambda m: 1),
        "always_last_option": _acc(lambda m: m["menu_size"]),
        "always_land_else_first": _acc(lambda m: m["first_land_pick"] or 1),
        "always_pass": _acc(lambda m: m["pass_pick"]),
        "uniform_random_expectation": random_expectation,
    }


def corpus_composition(records: list[dict]) -> dict:
    """Format mix, land-drop share, menu sizes — read before any accuracy."""
    n = len(records)
    metas = [r["meta"] for r in records]
    land = sum(1 for m in metas if m["is_land_drop"])
    sizes = sorted(m["menu_size"] for m in metas)
    return {
        "n": n,
        "by_format_bucket": dict(Counter(m["format_bucket"] for m in metas)),
        "by_format": dict(Counter(m["format"] for m in metas)),
        "by_gold_action_type": dict(Counter(m["gold_action_type"] for m in metas)),
        "trivial_land_drop_n": land,
        "trivial_land_drop_share": round(land / n, 4) if n else 0.0,
        "strategic_n": n - land,
        "menu_size": {
            "min": sizes[0] if sizes else 0,
            "median": sizes[len(sizes) // 2] if sizes else 0,
            "max": sizes[-1] if sizes else 0,
            "mean": round(sum(sizes) / n, 2) if n else 0.0,
        },
        "replay_files": len({m["source"]["replay_file"] for m in metas}),
        "leakage": {
            "deck_seen_in_train_n": sum(1 for m in metas if m.get("deck_seen_in_train")),
            "deck_unseen_in_train_n": sum(1 for m in metas if not m.get("deck_seen_in_train")),
            "exact_menu_seen_in_train_n": sum(1 for m in metas if m.get("menu_key_seen_in_train")),
            "mean_menu_cards_seen_in_train": round(
                sum(m.get("menu_cards_seen_in_train_frac", 0.0) for m in metas) / n, 4
            )
            if n
            else 0.0,
            "max_deck_jaccard_vs_train": max(
                (m.get("deck_max_train_jaccard", 0.0) for m in metas), default=0.0
            ),
        },
    }


# --------------------------------------------------------------------------
# Response parsing + scoring
# --------------------------------------------------------------------------

_PICK_RE = re.compile(r'"pick"\s*:\s*(\d+)')


def extract_pick(response: str) -> int | None:
    """Extract ``{"pick": N}`` the way production does.

    Mirrors ``ActionPlanner._parse_response``: JSON first, then the same
    regex fallback production uses when a model wraps or corrupts the JSON.
    Returns None when no integer pick is recoverable.
    """
    s = (response or "").strip()
    if not s:
        return None
    body = s
    fence = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", s, re.DOTALL)
    if fence:
        body = fence.group(1).strip()
    body = re.sub(r",\s*([\]}])", r"\1", body)
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        actions = data.get("actions")
        if isinstance(actions, list) and actions and isinstance(actions[0], dict):
            pick = actions[0].get("pick")
            if isinstance(pick, bool):
                return None
            if isinstance(pick, int):
                return pick
            if isinstance(pick, str) and pick.strip().isdigit():
                # production's regex fallback would accept this
                return int(pick.strip())
    m = _PICK_RE.search(s)
    if m:
        return int(m.group(1))
    return None


def _slice_stats(outcomes: list[dict]) -> dict:
    n = len(outcomes)
    correct = sum(1 for o in outcomes if o["correct"])
    exact = sum(1 for o in outcomes if o["correct_exact"])
    return {
        "n": n,
        "correct": correct,
        "accuracy": round(correct / n, 4) if n else None,
        "accuracy_95ci": wilson_ci(correct, n),
        "accuracy_exact_index": round(exact / n, 4) if n else None,
    }


def evaluate(corpus: list[dict], responses_path: Path, backend_label: str) -> dict:
    """Score one (corpus, responses, backend) triple. No thresholds here."""
    by_id = {rec["id"]: rec for rec in corpus}
    seen_ids: set[str] = set()
    duplicate_ids = 0
    outcomes: list[dict] = []
    per_id: dict[str, dict] = {}
    n = errors = schema_ok = pick_ok = 0
    legality_violations: list[dict] = []
    decoding_params: list[dict] = []

    for row in _read_jsonl(responses_path):
        if backend_label and row.get("backend") != backend_label:
            continue
        pid = row.get("prompt_id")
        rec = by_id.get(pid)
        if rec is None:
            continue
        if pid in seen_ids:
            duplicate_ids += 1
            continue
        seen_ids.add(pid)
        n += 1
        if row.get("decoding_params"):
            decoding_params.append(row["decoding_params"])
        elif "temperature" in row:
            decoding_params.append({"temperature": row["temperature"]})
        meta = rec["meta"]
        if row.get("error"):
            errors += 1
            continue
        response = row.get("response") or ""
        ok_schema, _ = validate_action_schema_json(response)
        if ok_schema:
            schema_ok += 1
        pick = extract_pick(response)
        legal = pick is not None and 1 <= pick <= meta["menu_size"]
        if pick is not None:
            pick_ok += 1
            if not legal:
                legality_violations.append({"id": pid, "pick": pick, "menu_size": meta["menu_size"]})
        chosen_key = None
        if legal:
            chosen_key = meta["menu"][pick - 1]["action_key"]
        outcome = {
            "id": pid,
            "pick": pick,
            "legal": legal,
            "schema_ok": ok_schema,
            "chosen_action_key": chosen_key,
            "correct": bool(legal and pick in meta["gold_equivalent_picks"]),
            "correct_exact": bool(legal and pick == meta["gold_pick"]),
            "format_bucket": meta["format_bucket"],
            "is_land_drop": meta["is_land_drop"],
            "deck_seen_in_train": bool(meta.get("deck_seen_in_train")),
            "twin_id": meta.get("twin_id"),
        }
        outcomes.append(outcome)
        per_id[pid] = outcome

    scored = [o for o in outcomes if o["pick"] is not None]
    by_bucket = defaultdict(list)
    for o in outcomes:
        by_bucket[o["format_bucket"]].append(o)

    nonbrawl = [o for o in outcomes if o["format_bucket"] != "brawl"]
    strategic = [o for o in outcomes if not o["is_land_drop"]]
    land = [o for o in outcomes if o["is_land_drop"]]
    seen = [o for o in outcomes if o["deck_seen_in_train"]]
    unseen = [o for o in outcomes if not o["deck_seen_in_train"]]

    return {
        "backend": backend_label,
        "corpus_n": len(corpus),
        "responses_matched": n,
        "duplicate_response_rows": duplicate_ids,
        "missing_responses": len(corpus) - n,
        "errors": errors,
        # G1
        "schema_valid_rate": round(schema_ok / n, 4) if n else 0.0,
        "pick_extraction_rate": round(pick_ok / n, 4) if n else 0.0,
        # G2
        "legality_violations": len(legality_violations),
        "legality_violation_examples": legality_violations[:10],
        "legality_rate": round((len(scored) - len(legality_violations)) / len(scored), 4) if scored else 0.0,
        # G4
        "overall": _slice_stats(outcomes),
        "by_format_bucket": {b: _slice_stats(v) for b, v in sorted(by_bucket.items())},
        "non_brawl": _slice_stats(nonbrawl),
        "strategic_non_land_drop": _slice_stats(strategic),
        "land_drop": _slice_stats(land),
        "deck_seen_in_train": _slice_stats(seen),
        "deck_unseen_in_train": _slice_stats(unseen),
        "pick_index_histogram": dict(Counter(o["pick"] for o in scored)),
        "per_id": per_id,
        "recorded_decoding_params": decoding_params[:5] if decoding_params else [{"temperature": 0.0}],
    }


def permutation_metrics(identity: dict, permuted: dict, permuted_corpus: list[dict]) -> dict:
    """G3 — same decisions, shuffled menus.

    Two numbers: the accuracy gap (gated) and the action-level agreement
    (advisory — a model can legitimately flip a close call, but flipping most
    of them means it is reading positions, not cards).
    """
    twin_of = {rec["id"]: rec["meta"]["twin_id"] for rec in permuted_corpus}
    pairs = []
    for perm_id, perm_outcome in permuted["per_id"].items():
        base_id = twin_of.get(perm_id)
        base_outcome = identity["per_id"].get(base_id)
        if base_outcome is None:
            continue
        pairs.append((base_outcome, perm_outcome))

    if not pairs:
        return {"paired_n": 0}

    n = len(pairs)
    id_correct = sum(1 for b, _ in pairs if b["correct"])
    perm_correct = sum(1 for _, p in pairs if p["correct"])
    agree = sum(
        1
        for b, p in pairs
        if b["chosen_action_key"] is not None and b["chosen_action_key"] == p["chosen_action_key"]
    )
    same_index = sum(1 for b, p in pairs if b["pick"] is not None and b["pick"] == p["pick"])
    return {
        "paired_n": n,
        "identity_accuracy": round(id_correct / n, 4),
        "identity_accuracy_95ci": wilson_ci(id_correct, n),
        "permuted_accuracy": round(perm_correct / n, 4),
        "permuted_accuracy_95ci": wilson_ci(perm_correct, n),
        "accuracy_gap": round((id_correct - perm_correct) / n, 4),
        "abs_accuracy_gap": round(abs(id_correct - perm_correct) / n, 4),
        "action_agreement": round(agree / n, 4),
        "same_index_rate": round(same_index / n, 4),
    }


# --------------------------------------------------------------------------
# Subcommand: build
# --------------------------------------------------------------------------


def cmd_build(args: argparse.Namespace) -> int:
    gt_path: Path = args.groundtruth
    if not gt_path.exists():
        logger.error(f"groundtruth corpus missing: {gt_path}")
        return 2

    raw = list(_read_jsonl(gt_path))
    logger.info(f"read {len(raw)} groundtruth decisions from {gt_path}")
    facts = _CardFacts(allow_network=args.allow_network_card_lookups)

    decisions_by_file: dict[str, list[dict]] = defaultdict(list)
    drops = Counter()
    for rec in raw:
        built = build_decision(rec, facts)
        if built is None or "drop_reason" in built:
            drops[(built or {}).get("drop_reason", "unknown")] += 1
            continue
        built["_gt"] = rec
        decisions_by_file[rec["replay_file"]].append(built)

    total = sum(len(v) for v in decisions_by_file.values())
    if total == 0:
        logger.error("no usable decisions after filtering — fail closed")
        return 2
    logger.info(
        f"usable decisions: {total} across {len(decisions_by_file)} replay files; drops={dict(drops)}"
    )

    assignment = assign_splits(
        decisions_by_file,
        seed=args.seed,
        test_fraction=args.test_fraction,
        dev_fraction=args.dev_fraction,
        min_test_files=args.min_test_files,
        min_dev_files=args.min_dev_files,
    )

    # ---- leakage measurement (the seen-vs-unseen split) --------------------
    # Splitting by replay file stops *decision* leakage, not *deck* leakage:
    # the same Brawl deck is piloted across many replays, so a held-out game
    # can still be one the trainer has effectively already answered. Three
    # measures, coarse to fine:
    #   deck_seen_in_train  — this replay's card pool overlaps a training
    #                         replay's by >= --deck-overlap-threshold (Jaccard).
    #                         This drives the seen/unseen accuracy split.
    #   menu_cards_seen_*   — share of this menu's card ids seen anywhere in
    #                         training.
    #   menu_key_seen_*     — the exact menu recurred (rare; reported only).
    train_menu_keys: set[tuple] = set()
    train_card_ids: set[int] = set()
    file_card_ids: dict[str, set[int]] = {}
    for fname, decisions in decisions_by_file.items():
        ids: set[int] = set()
        for d in decisions:
            ids.update(d["_card_ids"])
        file_card_ids[fname] = ids
        if assignment[fname] == "train":
            train_card_ids |= ids
            for d in decisions:
                train_menu_keys.add(tuple(d["menu_key_spell"]))

    train_files = [f for f, s in assignment.items() if s == "train"]

    def _deck_overlap(fname: str) -> float:
        own = file_card_ids[fname]
        if not own:
            return 0.0
        best = 0.0
        for other in train_files:
            theirs = file_card_ids[other]
            if not theirs:
                continue
            union = len(own | theirs)
            if union:
                best = max(best, len(own & theirs) / union)
        return round(best, 4)

    out_dir: Path = args.out_dir
    written: dict[str, int] = {}
    corpora: dict[str, list[dict]] = {}
    for split in ("test", "dev"):
        identity_records: list[dict] = []
        permuted_records: list[dict] = []
        for fname in sorted(decisions_by_file):
            if assignment[fname] != split:
                continue
            overlap = _deck_overlap(fname)
            for decision in decisions_by_file[fname]:
                stem = Path(fname).stem
                rid = f"pd-{stem}-{decision['source']['decision_index']:04d}"
                menu_ids = decision["_menu_card_ids"]
                seen_ids = [g for g in menu_ids if g in train_card_ids]
                decision["menu_key_seen_in_train"] = tuple(decision["menu_key_spell"]) in train_menu_keys
                decision["deck_max_train_jaccard"] = overlap
                decision["deck_seen_in_train"] = overlap >= args.deck_overlap_threshold
                decision["menu_cards_seen_in_train_frac"] = (
                    round(len(seen_ids) / len(menu_ids), 4) if menu_ids else 0.0
                )
                identity_records.append(to_corpus_record(decision, rid, split, "identity", rid))
                twin = permute_decision(decision, f"{args.seed}:perm:{rid}")
                permuted_records.append(to_corpus_record(twin, f"{rid}#perm", split, "permuted", rid))
        corpora[split] = identity_records
        corpora[f"{split}_permuted"] = permuted_records
        written[f"{split}"] = _write_jsonl(out_dir / f"{CORPUS_STEM}_{split}.jsonl", identity_records)
        written[f"{split}_permuted"] = _write_jsonl(
            out_dir / f"{CORPUS_STEM}_{split}_permuted.jsonl", permuted_records
        )

    if not corpora["test"]:
        logger.error("test split is empty — fail closed")
        return 2

    trainable = sorted(f for f, s in assignment.items() if s == "train")
    held_out = sorted(f for f, s in assignment.items() if s != "train")
    train_path = out_dir / f"{CORPUS_STEM}_trainable_replays.json"
    train_path.write_text(
        json.dumps(
            {
                "purpose": "Replay files the dataset builder MAY train on. Everything in "
                "excluded_replays is gate/dev data and must never enter a training set.",
                "trainable_replays": trainable,
                "excluded_replays": held_out,
                "split_by_replay": dict(sorted(assignment.items())),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    manifest = {
        "corpus": "play_decisions_v1",
        "built_ts": time.time(),
        "git_sha": _git_sha(),
        "seed": args.seed,
        "source": {
            "path": str(gt_path),
            "sha256": _sha256_file(gt_path),
            "records": len(raw),
            "generator": "tools/eval/replay/menu_groundtruth.py (real MTGA .rply menus)",
        },
        "dropped": dict(drops),
        "splits": {
            "policy": "grouped by replay file, stratified by format bucket, deterministic in seed",
            "test_fraction": args.test_fraction,
            "dev_fraction": args.dev_fraction,
            "deck_overlap_threshold": args.deck_overlap_threshold,
            "replay_files": dict(Counter(assignment.values())),
            "decisions": {k: written.get(k, 0) for k in ("test", "dev")},
            "note": "test is NEVER trainable; the trainable replay list is written alongside so "
            "the dataset builder can exclude held-out games mechanically",
        },
        "composition": {
            "test": corpus_composition(corpora["test"]),
            "dev": corpus_composition(corpora["dev"]) if corpora["dev"] else {},
        },
        "reference_policies": {
            "test": reference_policies(corpora["test"]),
            "test_permuted": reference_policies(corpora["test_permuted"]),
            "dev": reference_policies(corpora["dev"]) if corpora["dev"] else {},
        },
        "prompt_fidelity": {
            "system_prompt": "imported arenamcp.action_planner.AUTOPILOT_SYSTEM_PROMPT (never retyped)",
            "user_message": "arenamcp.action_planner.ActionPlanner._build_action_prompt (production)",
            "menu": "verbatim MTGA actionsAvailableReq.actions[], minus mana-level actions "
            "(production drops those from the numbered menu)",
            "ok_tags": "recomputed via RulesEngine._can_afford/_get_mana_pool — MTGA's actions[] "
            "is not affordability-filtered and the replay carries no autoTapSolution",
            "creature_rows": facts.creature_rows,
            "unknown_pt_rows": facts.unknown_pt_rows,
            "unknown_pt_share": round(facts.unknown_pt_rows / facts.creature_rows, 4)
            if facts.creature_rows
            else 0.0,
            "known_gaps": [
                "board P/T is the printed base P/T; counters and static buffs are not in the "
                "summarised replay state, and '*' P/T renders as 0 (see unknown_pt_share)",
                "opponent permanents have grpIds only — no tapped state, no instance ids",
                "no graveyards, no stack, no raw GRE block, no exile/zone history",
                "turn_entered_battlefield is set to 'before this turn', which is correct for "
                "start-of-own-main-1 (nothing can have entered yet) but disables ETB-recency text",
                "byte-identity with a live production prompt is NOT claimed; §17.2's identity "
                "path belongs to the dataset builder",
            ],
        },
        "honesty_note": (
            f"{corpus_composition(corpora['test'])['trivial_land_drop_share']:.1%} of the test split "
            "is a land drop. A model that always plays a land scores near that number while knowing "
            "nothing — read reference_policies and the strategic_non_land_drop slice before believing "
            "any accuracy figure."
        ),
        "outputs": {k: str(out_dir / f"{CORPUS_STEM}_{k}.jsonl") for k in written},
        "trainable_replays_file": str(train_path),
    }
    manifest_path = out_dir / f"{CORPUS_STEM}_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    logger.info(f"wrote {written} records; manifest -> {manifest_path}")
    logger.info(f"test composition: {json.dumps(manifest['composition']['test'])}")
    logger.info(f"test reference policies: {json.dumps(manifest['reference_policies']['test'])}")
    return 0


# --------------------------------------------------------------------------
# Subcommand: simulate (harness proof — no model, no GPU)
# --------------------------------------------------------------------------

POLICIES = (
    "first",
    "last",
    "land_else_first",
    "pass_else_first",
    "random",
    "oracle",
    "index_memoriser",
    "broken_schema",
    "illegal",
)


def _policy_pick(policy: str, meta: dict, rng: random.Random) -> Any:
    if policy == "first":
        return 1
    if policy == "last":
        return meta["menu_size"]
    if policy == "land_else_first":
        return meta["first_land_pick"] or 1
    if policy == "pass_else_first":
        return meta["pass_pick"] or 1
    if policy == "random":
        return rng.randint(1, meta["menu_size"])
    if policy == "oracle":
        return meta["gold_pick"]
    if policy == "index_memoriser":
        # Answers with the index that was correct in the IDENTITY corpus,
        # regardless of what is at that index now. Correct on identity,
        # near-chance on the permuted twin — exactly what G3 must catch.
        return meta.get("identity_gold_pick", meta["gold_pick"])
    if policy == "illegal":
        return meta["menu_size"] + 1
    return None


def cmd_simulate(args: argparse.Namespace) -> int:
    corpus_path: Path = args.corpus
    if not corpus_path.exists():
        logger.error(f"corpus missing: {corpus_path}")
        return 2
    corpus = list(_read_jsonl(corpus_path))

    identity_gold: dict[str, int] = {}
    if args.identity_corpus and args.identity_corpus.exists():
        for rec in _read_jsonl(args.identity_corpus):
            identity_gold[rec["id"]] = rec["meta"]["gold_pick"]

    rows = []
    for rec in corpus:
        meta = dict(rec["meta"])
        twin = meta.get("twin_id")
        if twin in identity_gold:
            meta["identity_gold_pick"] = identity_gold[twin]
        rng = random.Random(f"{args.seed}:{args.policy}:{rec['id']}")
        pick = _policy_pick(args.policy, meta, rng)
        if args.policy == "broken_schema":
            response = "I would play the land, it is clearly the strongest option here."
        else:
            response = json.dumps(
                {"actions": [{"pick": int(pick), "reasoning": f"{args.policy} policy"}]},
                ensure_ascii=False,
            )
        rows.append(
            {
                "prompt_id": rec["id"],
                "backend": args.backend_label or f"synthetic:{args.policy}",
                "model": f"synthetic-{args.policy}",
                "response": response,
                "error": None,
                "latency_ms": 0.0,
                "response_chars": len(response),
                "temperature": 0.0,
                "decoding_params": {"temperature": 0.0, "policy": args.policy},
                "ts": time.time(),
            }
        )
    n = _write_jsonl(args.out, rows)
    logger.info(f"simulated policy={args.policy} n={n} -> {args.out}")
    return 0


# --------------------------------------------------------------------------
# Subcommand: gate
# --------------------------------------------------------------------------


def _clamp_thresholds(args: argparse.Namespace) -> list[str]:
    """CLI thresholds may only be made STRICTER than the §17.4 constants.

    This is what "no bypass flag of any kind" means operationally: a flag that
    could loosen a hard gate is a bypass with extra steps, so any loosening
    value is ignored and the override is recorded in the report.
    """
    notes: list[str] = []
    clamps = (
        ("min_samples", MIN_SAMPLES, max),
        ("min_nonbrawl_samples", MIN_NONBRAWL_SAMPLES, max),
        ("min_strategic_samples", MIN_STRATEGIC_SAMPLES, max),
        ("min_schema_rate", MIN_SCHEMA_RATE, max),
        ("max_permutation_gap", MAX_PERMUTATION_ACC_GAP, min),
        ("max_accuracy_regression", MAX_ACCURACY_REGRESSION, min),
    )
    for name, floor, op in clamps:
        given = getattr(args, name)
        clamped = op(given, floor)
        if clamped != given:
            notes.append(f"--{name.replace('_', '-')}={given} ignored (would loosen); using {clamped}")
            setattr(args, name, clamped)
    return notes


def cmd_gate(args: argparse.Namespace) -> int:
    clamp_notes = _clamp_thresholds(args)
    for note in clamp_notes:
        logger.warning(note)
    verdict = "BLOCKED"
    failures: list[str] = []
    candidate: dict = {}
    baseline: dict = {}
    permuted: dict = {}
    perm_metrics: dict = {}
    bootstrap: dict = {}
    composition: dict = {}
    references: dict = {}

    try:
        required = {
            "corpus": args.corpus,
            "permuted-corpus": args.permuted_corpus,
            "responses": args.responses,
            "permuted-responses": args.permuted_responses,
            "baseline-responses": args.baseline_responses,
        }
        for name, path in required.items():
            if path is None or not Path(path).exists():
                failures.append(f"missing required input --{name}: {path}")

        if not failures:
            corpus = list(_read_jsonl(args.corpus))
            permuted_corpus = list(_read_jsonl(args.permuted_corpus))
            if not corpus:
                failures.append("gate corpus is empty — fail closed")
            composition = corpus_composition(corpus) if corpus else {}
            references = reference_policies(corpus) if corpus else {}

        if not failures:
            candidate = evaluate(corpus, args.responses, args.backend_label)
            permuted = evaluate(permuted_corpus, args.permuted_responses, args.backend_label)
            baseline = evaluate(corpus, args.baseline_responses, args.baseline_label)
            perm_metrics = permutation_metrics(candidate, permuted, permuted_corpus)

            # ---- coverage / sample sufficiency (fail closed) ----
            for label, metrics, size in (
                ("candidate", candidate, len(corpus)),
                ("permuted", permuted, len(permuted_corpus)),
                ("baseline", baseline, len(corpus)),
            ):
                if metrics["responses_matched"] != size:
                    failures.append(
                        f"{label} responses cover {metrics['responses_matched']}/{size} corpus records "
                        "— fail closed (no partial gate)"
                    )
                if metrics["errors"]:
                    failures.append(
                        f"{label} responses contain {metrics['errors']} backend errors — fail closed"
                    )
                if metrics["duplicate_response_rows"]:
                    failures.append(
                        f"{label} responses contain {metrics['duplicate_response_rows']} duplicate prompt_ids"
                    )
            if candidate["responses_matched"] < args.min_samples:
                failures.append(
                    f"insufficient samples: {candidate['responses_matched']} < {args.min_samples}"
                )
            if candidate["non_brawl"]["n"] < args.min_nonbrawl_samples:
                failures.append(
                    f"insufficient non-Brawl samples: {candidate['non_brawl']['n']} < "
                    f"{args.min_nonbrawl_samples} (the non-Brawl slice is primary)"
                )
            if candidate["strategic_non_land_drop"]["n"] < args.min_strategic_samples:
                failures.append(
                    f"insufficient strategic (non-land-drop) samples: "
                    f"{candidate['strategic_non_land_drop']['n']} < {args.min_strategic_samples}"
                )

            # ---- G1 schema ----
            if candidate["schema_valid_rate"] < args.min_schema_rate:
                failures.append(
                    f"G1 schema_valid_rate {candidate['schema_valid_rate']:.4f} < {args.min_schema_rate}"
                )

            # ---- G2 legality (any violation is a hard fail) ----
            if candidate["legality_violations"] > MAX_LEGALITY_VIOLATIONS:
                failures.append(
                    f"G2 legality: {candidate['legality_violations']} pick(s) outside [1, menu_size] — "
                    f"hard fail (production must execute the pick). "
                    f"examples={candidate['legality_violation_examples'][:3]}"
                )
            if permuted["legality_violations"] > MAX_LEGALITY_VIOLATIONS:
                failures.append(
                    f"G2 legality (permuted twin): {permuted['legality_violations']} pick(s) "
                    "outside [1, menu_size] — hard fail"
                )
            unparsed = candidate["responses_matched"] - round(
                candidate["pick_extraction_rate"] * candidate["responses_matched"]
            )
            if candidate["pick_extraction_rate"] < args.min_schema_rate:
                failures.append(
                    f"G2 pick_extraction_rate {candidate['pick_extraction_rate']:.4f} < "
                    f"{args.min_schema_rate} ({unparsed} responses yielded no executable pick)"
                )

            # ---- G3 permutation invariance ----
            if perm_metrics.get("paired_n", 0) < args.min_samples:
                failures.append(
                    f"G3 permutation twin paired on {perm_metrics.get('paired_n', 0)} decisions "
                    f"< {args.min_samples} — fail closed"
                )
            elif perm_metrics["abs_accuracy_gap"] > args.max_permutation_gap:
                failures.append(
                    f"G3 permutation invariance: |identity {perm_metrics['identity_accuracy']:.4f} - "
                    f"permuted {perm_metrics['permuted_accuracy']:.4f}| = "
                    f"{perm_metrics['abs_accuracy_gap']:.4f} > {args.max_permutation_gap} — the model is "
                    "reading menu positions, not the menu"
                )

            # ---- G5 paired-bootstrap non-inferiority vs baseline ----
            pairs = []
            for pid, cand_outcome in candidate["per_id"].items():
                base_outcome = baseline["per_id"].get(pid)
                if base_outcome is None:
                    continue
                pairs.append(
                    {
                        "prompt_id": pid,
                        # gate_stage0's estimator computes a balanced delta only
                        # for keep/mull arms; play decisions have no such arms,
                        # so the balanced field comes back null by construction
                        # and only the raw delta CI is used.
                        "truth": cand_outcome["format_bucket"],
                        "cand_correct": cand_outcome["correct"],
                        "base_correct": base_outcome["correct"],
                    }
                )
            if len(pairs) < args.min_samples:
                failures.append(
                    f"G5 paired bootstrap has {len(pairs)} paired decisions < {args.min_samples} — fail closed"
                )
            else:
                bootstrap = paired_bootstrap_ci(pairs, n_bootstraps=args.bootstraps, seed=args.seed)
                delta = candidate["overall"]["accuracy"] - baseline["overall"]["accuracy"]
                bootstrap["point_accuracy_delta"] = round(delta, 4)
                raw_ci = bootstrap.get("raw_accuracy_delta_95ci")
                if not raw_ci:
                    failures.append("G5 paired bootstrap produced no CI — fail closed")
                elif raw_ci[0] < -args.max_accuracy_regression:
                    failures.append(
                        f"G5 non-inferiority: accuracy delta 95% CI lower bound {raw_ci[0]:+.4f} < "
                        f"-{args.max_accuracy_regression} (point delta {delta:+.4f}) vs baseline "
                        f"{args.baseline_label!r}"
                    )

            # ---- advisory (reported, not gated) ----
            trivial = (references or {}).get("always_land_else_first")
            if trivial is not None and candidate["overall"]["accuracy"] is not None:
                candidate["beats_trivial_land_policy"] = bool(candidate["overall"]["accuracy"] > trivial)
            if args.min_accuracy is not None and candidate["overall"]["accuracy"] is not None:
                if candidate["overall"]["accuracy"] < args.min_accuracy:
                    failures.append(
                        f"accuracy {candidate['overall']['accuracy']:.4f} < --min-accuracy {args.min_accuracy}"
                    )

        if not failures:
            verdict = "PASS"
    except Exception as e:  # any evaluation error blocks promotion
        logger.exception("gate exception")
        failures.append(f"gate exception: {type(e).__name__}: {e}")
        verdict = "BLOCKED"

    def _strip(metrics: dict) -> dict:
        return {k: v for k, v in metrics.items() if k != "per_id"}

    report = {
        "gate": "play_decisions",
        "verdict": verdict,
        "failures": failures,
        "corpus_composition": composition,
        "reference_policies": references,
        "candidate": _strip(candidate),
        "candidate_permuted": _strip(permuted),
        "baseline": _strip(baseline),
        "permutation_invariance": perm_metrics,
        "paired_bootstrap": bootstrap,
        "thresholds": {
            "min_samples": args.min_samples,
            "min_nonbrawl_samples": args.min_nonbrawl_samples,
            "min_strategic_samples": args.min_strategic_samples,
            "min_schema_rate": args.min_schema_rate,
            "max_legality_violations": MAX_LEGALITY_VIOLATIONS,
            "max_permutation_gap": args.max_permutation_gap,
            "max_accuracy_regression": args.max_accuracy_regression,
            "min_accuracy": args.min_accuracy,
            "clamped_cli_overrides": clamp_notes,
        },
        "inputs": {
            "corpus": str(args.corpus),
            "permuted_corpus": str(args.permuted_corpus),
            "responses": str(args.responses),
            "permuted_responses": str(args.permuted_responses),
            "baseline_responses": str(args.baseline_responses),
            "backend_label": args.backend_label,
            "baseline_label": args.baseline_label,
        },
        "honesty_note": (
            "Land drops are "
            f"{(composition or {}).get('trivial_land_drop_share', 0):.1%} of this corpus. Compare "
            "candidate accuracy against reference_policies.always_land_else_first and read the "
            "strategic_non_land_drop slice before treating overall accuracy as skill. A G3 gap can "
            "also come from position-based tie-breaking rather than memorisation — check "
            "permutation_invariance.action_agreement to tell them apart."
        ),
        "git_sha": _git_sha(),
        "ts": time.time(),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    logger.info(f"gate verdict: {verdict}; report -> {args.report}")
    for msg in failures:
        logger.error(f"  gate failure: {msg}")

    if args.register:
        if verdict != "PASS":
            logger.error("Refusing to register: gate verdict is not PASS (fail closed).")
            return 2
        if not (args.gen_id and args.base_model and args.adapter_path):
            logger.error("--register requires --gen-id, --base-model, --adapter-path")
            return 2
        from tools.training.registry import ModelRegistry

        reg = ModelRegistry(args.registry_dir)
        reg.register_generation(
            gen_id=args.gen_id,
            base_model=args.base_model,
            adapter_path=args.adapter_path,
            parent_gen=args.parent_gen,
            git_sha=report["git_sha"],
            metadata={
                "stage": "play-decisions",
                "gate_corpus": str(args.corpus),
                "backend_label": args.backend_label,
            },
            gate_report=report,
        )
        logger.info(f"Registered {args.gen_id} in {args.registry_dir}/registry.sqlite")

    return 0 if verdict == "PASS" else 2


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="build the gate corpus + permuted twins + manifest")
    b.add_argument("--groundtruth", type=Path, default=DEFAULT_GROUNDTRUTH)
    b.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    b.add_argument("--seed", type=int, default=DEFAULT_SEED)
    b.add_argument("--test-fraction", type=float, default=DEFAULT_TEST_FRACTION)
    b.add_argument("--dev-fraction", type=float, default=DEFAULT_DEV_FRACTION)
    b.add_argument(
        "--deck-overlap-threshold",
        type=float,
        default=DEFAULT_DECK_OVERLAP_THRESHOLD,
        help="Jaccard overlap of card pools above which a held-out replay counts as a deck "
        "the training split has already seen (drives the seen/unseen accuracy split)",
    )
    b.add_argument("--min-test-files", type=int, default=3, help="floor per format bucket")
    b.add_argument("--min-dev-files", type=int, default=2, help="floor per format bucket")
    b.add_argument(
        "--allow-network-card-lookups",
        action="store_true",
        help="permit Scryfall API fallback for unresolved cards (off by default — a gate "
        "corpus must be reproducible without the network)",
    )
    b.set_defaults(func=cmd_build)

    s = sub.add_parser("simulate", help="write synthetic-policy responses (harness proof, no model)")
    s.add_argument("--corpus", type=Path, required=True)
    s.add_argument(
        "--identity-corpus",
        type=Path,
        default=None,
        help="identity corpus, so index_memoriser can answer with the identity gold index",
    )
    s.add_argument("--policy", choices=POLICIES, required=True)
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--backend-label", default="")
    s.add_argument("--seed", type=int, default=DEFAULT_SEED)
    s.set_defaults(func=cmd_simulate)

    g = sub.add_parser("gate", help="score a candidate; PASS (0) or BLOCKED (2)")
    g.add_argument("--corpus", type=Path, required=True)
    g.add_argument("--permuted-corpus", type=Path, required=True)
    g.add_argument("--responses", type=Path, required=True)
    g.add_argument("--permuted-responses", type=Path, required=True)
    g.add_argument("--baseline-responses", type=Path, required=True)
    g.add_argument("--backend-label", required=True)
    g.add_argument("--baseline-label", required=True)
    g.add_argument("--report", type=Path, required=True)
    # Every threshold below is clamped so it can only be made STRICTER than
    # the §17.4 constant — see _clamp_thresholds. There is no loosening flag.
    g.add_argument("--min-samples", type=int, default=MIN_SAMPLES)
    g.add_argument("--min-nonbrawl-samples", type=int, default=MIN_NONBRAWL_SAMPLES)
    g.add_argument("--min-strategic-samples", type=int, default=MIN_STRATEGIC_SAMPLES)
    g.add_argument("--min-schema-rate", type=float, default=MIN_SCHEMA_RATE)
    g.add_argument("--max-permutation-gap", type=float, default=MAX_PERMUTATION_ACC_GAP)
    g.add_argument("--max-accuracy-regression", type=float, default=MAX_ACCURACY_REGRESSION)
    g.add_argument(
        "--min-accuracy",
        type=float,
        default=None,
        help="optional EXTRA floor on pick accuracy (tightens the gate; there is no loosening flag)",
    )
    g.add_argument("--bootstraps", type=int, default=10000)
    g.add_argument("--seed", type=int, default=DEFAULT_SEED)
    g.add_argument("--register", action="store_true")
    g.add_argument("--registry-dir", type=Path, default=REPO / "models")
    g.add_argument("--gen-id", default="")
    g.add_argument("--base-model", default="")
    g.add_argument("--adapter-path", type=Path)
    g.add_argument("--parent-gen", default=None)
    g.set_defaults(func=cmd_gate)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s | %(message)s",
        stream=sys.stderr,
    )
    try:
        return args.func(args)
    except Exception as e:  # fail closed at the top level too
        logger.exception("fatal error")
        print(f"BLOCKED: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
