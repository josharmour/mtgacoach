#!/usr/bin/env python3
"""Build PRODUCTION-SHAPED play-decision training records from real MTGA menus.

Why this file exists
--------------------
The play-decision training corpus and the gate that measures it disagree on
prompt shape, which is the failure mode that killed three prior runs:

  TRAIN  ``tools/training/data/play_decisions_nontrivial.jsonl`` (13,671)
         system  = a *coach-flavoured* prompt, NOT ``AUTOPILOT_SYSTEM_PROMPT``
         menu    = ``Candidate: (pick by number — RECONSTRUCTED, NOT a
                   complete legal-action list)``
         The label is honest: that menu is synthesised from 17lands replay
         data and only recovers ~62% of the real MTGA menu, so calling it
         ``Legal:`` would teach the model that production menus are
         incomplete. It must NOT be relabelled.

  GATE   ``tools/training/data/gate_play_decisions_test.jsonl``
         system  = ``arenamcp.action_planner.AUTOPILOT_SYSTEM_PROMPT``
         menu    = ``Legal: (pick by number)`` built from REAL MTGA menus

So the model is trained on one shape and measured on another. The fix is not
to relabel the synthetic menu — it is to ADD records in the production shape,
built from real menus, so the model sees both and the shape the gate measures
is one it has actually been trained on.

What this builds
----------------
``tools/training/data/play_decisions_bridge.jsonl`` — real-menu decisions in
the training record shape (``system``/``user``/``response``/``source``/
``attribution``/``kind``/``meta``) whose ``system`` and ``user`` are byte-for-
byte the shapes the gate uses, because they are produced by **the same
functions the gate builder calls** (``tools.training.gate_play_decisions``).
No prompt text is re-implemented here; re-implementation is the original sin.

Then ``tools/training/data/play_decisions_mixed.jsonl`` = bridge records +
the 13,671 candidate-shape records, shuffled with a fixed seed.

Sources
-------
  * ``tools/training/data/manasight_menu_groundtruth.jsonl`` (2,503) —
    manasight-corpus, MIT OR Apache-2.0, an independent public corpus with no
    provenance overlap with the gate.
  * ``tools/eval/data/replay_menu_groundtruth.jsonl`` (665) — the owner's own
    ``.rply`` files. **Only** the replays listed under ``trainable_replays`` in
    ``gate_play_decisions_trainable_replays.json`` are eligible; the rest are
    the gate's test/dev games.

Two filters that are not negotiable
-----------------------------------
1. **start-of-own-Main-1 only.** The gate corpus is 100% start-of-own-Main-1
   and the prompt hard-codes ``TRIGGER = "new_turn"`` -> "Your turn started
   (Main Phase 1). Plan your plays." ``gate_play_decisions.build_game_state``
   also hard-codes ``lands_played = 0`` and ``turn_entered_battlefield = -1``
   ("nothing entered this turn"). Both are true at the start of your Main 1
   and false anywhere else. manasight carries every priority window it saw
   (1,830 of 2,503 are combat / Main 2 / end step / the opponent's turn);
   emitting those under this trigger would fabricate the decision context and
   the land-drop availability, i.e. invent a THIRD shape. They are excluded
   and counted.

2. **Leakage is the cardinal sin.** No record whose replay or decision appears
   in the gate's test or dev split may enter training. Enforced three ways and
   all three are reported: (a) replay-id filter from the manifest, (b) exact
   ``(replay stem, decision_index)`` identity match, (c) SHA-256 of the
   rendered user prompt against every gate test/dev prompt, identity and
   permuted. Any non-zero intersection is a hard failure.

Card facts must be fully loaded (fail-closed)
---------------------------------------------
``arenamcp.mtgjson.get_mtgjson()`` loads in a **background daemon thread** and
returns immediately, so a caller that builds prompts right away races the
loader: unresolved permanents get an empty type line, produce no mana, and the
prompt renders "Mana: 0" with wrong ``[S,NEED:...]`` tags. That race silently
corrupted 20.4% of the gate corpus once already.

Card sources are therefore obtained through
``arenamcp.card_db.require_card_database``, which blocks until every local
source is loaded and raises otherwise — one shared implementation, used by
this builder, the gate corpus builder and the replay menu extractor alike.
Every record is additionally screened by ``tools.training.corpus_selfcheck``
and dropped if its card facts are degraded, and
``gate_corpus_divergence`` below re-renders the gate's own prompts as an
independent cross-check that train and gate agree.

Usage
-----
    python -m tools.training.build_bridge_dataset build
    python -m tools.training.build_bridge_dataset mix
    python -m tools.training.build_bridge_dataset all      # both, then stats
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.training import corpus_selfcheck  # noqa: E402
from tools.training import gate_play_decisions as G  # noqa: E402

logger = logging.getLogger("tools.training.build_bridge_dataset")

DATA = REPO / "tools" / "training" / "data"
EVAL_DATA = REPO / "tools" / "eval" / "data"

MANASIGHT_GT = DATA / "manasight_menu_groundtruth.jsonl"
REPLAY_GT = EVAL_DATA / "replay_menu_groundtruth.jsonl"
TRAINABLE_MANIFEST = DATA / "gate_play_decisions_trainable_replays.json"

# Every gate artefact a training record must not resemble. Permuted twins are
# included: the same decision with a shuffled menu is still the same game.
GATE_HELD_OUT = [
    DATA / "gate_play_decisions_test.jsonl",
    DATA / "gate_play_decisions_test_permuted.jsonl",
    DATA / "gate_play_decisions_dev.jsonl",
    DATA / "gate_play_decisions_dev_permuted.jsonl",
]

BRIDGE_OUT = DATA / "play_decisions_bridge.jsonl"
CANDIDATE_IN = DATA / "play_decisions_nontrivial.jsonl"
MIXED_OUT = DATA / "play_decisions_mixed.jsonl"
BRIDGE_STATS = DATA / "play_decisions_bridge_stats.json"
MIXED_STATS = DATA / "play_decisions_mixed_stats.json"

MIX_SEED = 20260725

SOURCE_LABELS = {
    "manasight": ("manasight-menu_groundtruth", "manasight-corpus (MIT OR Apache-2.0)"),
    "replay": ("mtgacoach-replay_menu_groundtruth", "mtgacoach owner MTGA .rply replays"),
}


# --------------------------------------------------------------------------
# IO helpers
# --------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _write_jsonl(path: Path, rows: list[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _decision_identity(replay_file: str, decision_index: Any) -> str:
    """Gate-comparable identity: the only keys the gate corpus carries.

    Deliberately coarse. A manasight session log holds several matches, so this
    collides across matches within one file — which errs toward flagging a
    collision that is not a leak, never toward missing one. ``_record_identity``
    is the unique key.
    """
    return f"{Path(str(replay_file)).stem}#{decision_index}"


def _record_identity(rec: dict) -> str:
    """Unique key for one decision (match and game qualified)."""
    return "|".join(
        str(rec.get(k, ""))
        for k in ("replay_file", "match_id", "match_index", "game_number", "decision_index", "msg_id")
    )


# --------------------------------------------------------------------------
# Card facts — fail closed rather than build degraded prompts
# --------------------------------------------------------------------------


def load_card_facts() -> G._CardFacts:
    """``_CardFacts`` with every local card source fully loaded.

    The blocking/fail-closed behaviour now lives in
    ``arenamcp.card_db.require_card_database``, which ``_CardFacts`` calls —
    one implementation shared by this builder, the gate corpus builder and the
    replay menu extractor, rather than three copies that can drift apart.
    """
    t0 = time.time()
    facts = G._CardFacts(allow_network=False)
    logger.info(f"card database loaded in {time.time() - t0:.1f}s")
    return facts


# --------------------------------------------------------------------------
# Record construction
# --------------------------------------------------------------------------


def build_bridge_record(
    rec: dict,
    facts: G._CardFacts,
    *,
    source_corpus: str,
    system_prompt: str,
) -> tuple[dict | None, str]:
    """Groundtruth record -> production-shaped training record.

    Returns ``(record, drop_reason)``; exactly one of the two is meaningful.
    """
    if not rec.get("is_start_of_main1"):
        return None, "not_start_of_main1"
    if not rec.get("is_own_turn"):
        return None, "not_own_turn"

    decision = G.build_decision(rec, facts)
    if decision is None:
        return None, "unusable"
    if "drop_reason" in decision:
        return None, str(decision["drop_reason"])

    menu_texts = [r["text"] for r in decision["menu"]]
    user = G.build_user_message(decision["game_state"], menu_texts)

    # The guard that the prior runs lacked. Shape is asserted per record, not
    # assumed from the import at the top of the file.
    if "Legal: (pick by number)" not in user:
        return None, "menu_header_not_production"
    if "Candidate:" in user:
        return None, "menu_header_is_candidate_shape"

    gold_pick = int(decision["gold_pick"])
    if not (1 <= gold_pick <= decision["menu_size"]):
        return None, "gold_pick_out_of_range"

    # Same fail-closed card-fact check the gate corpus applies. A training
    # record whose battlefield lands did not resolve teaches the model to
    # answer a board that never existed, which is worse than not training on
    # it at all — so it is dropped and counted, never emitted quietly.
    record_id = _record_identity(rec)
    degraded = corpus_selfcheck.findings_from_audit(record_id, decision[corpus_selfcheck.AUDIT_KEY])
    if degraded:
        logger.warning(f"degraded card facts, dropping {record_id}: {[f.kind for f in degraded]}")
        return None, f"degraded_card_facts:{degraded[0].kind}"

    label, attribution = SOURCE_LABELS[source_corpus]
    meta = {k: v for k, v in decision.items() if k != "game_state" and not k.startswith("_")}
    meta["source_corpus"] = source_corpus
    meta["prompt_shape"] = "production_legal_menu"
    meta["menu_is_candidate_not_legal"] = False
    meta["system_is_autopilot_prompt"] = True
    meta["split"] = "train"
    meta["decision_identity"] = _decision_identity(rec.get("replay_file", ""), rec.get("decision_index"))
    meta["record_identity"] = _record_identity(rec)
    meta["user_sha256"] = _sha256_text(user)
    if rec.get("match_id"):
        meta["source"]["match_id"] = rec["match_id"]

    record = {
        "system": system_prompt,
        "user": user,
        "response": json.dumps({"actions": [{"pick": gold_pick}]}),
        "source": label,
        "attribution": attribution,
        "kind": "play_decision",
        "meta": meta,
    }
    return record, ""


# --------------------------------------------------------------------------
# Leakage
# --------------------------------------------------------------------------


def load_held_out() -> dict:
    """Every prompt hash and decision identity the gate holds out."""
    prompt_hashes: set[str] = set()
    identities: set[str] = set()
    replay_files: set[str] = set()
    per_file: dict[str, int] = {}
    for path in GATE_HELD_OUT:
        if not path.exists():
            raise FileNotFoundError(f"gate corpus missing, cannot verify leakage: {path}")
        rows = _read_jsonl(path)
        per_file[path.name] = len(rows)
        for row in rows:
            prompt_hashes.add(_sha256_text(row["user"]))
            src = (row.get("meta") or {}).get("source") or {}
            rf = src.get("replay_file")
            if rf:
                replay_files.add(rf)
                identities.add(_decision_identity(rf, src.get("decision_index")))
    return {
        "prompt_hashes": prompt_hashes,
        "identities": identities,
        "replay_files": replay_files,
        "records_per_file": per_file,
    }


# --------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------


def _hist(counter: Counter) -> dict:
    return {str(k): v for k, v in sorted(counter.items(), key=lambda kv: kv[0])}


def summarise(records: list[dict]) -> dict:
    """The three distributions that decide whether this corpus is trainable."""
    n = len(records)
    if not n:
        return {"n": 0}
    action = Counter()
    picks = Counter()
    sizes = Counter()
    buckets = Counter()
    corpora = Counter()
    pick_frac = Counter()
    land_drop = 0
    pass_available = 0
    for r in records:
        m = r["meta"]
        action[m["gold_action_type"]] += 1
        picks[m["gold_pick"]] += 1
        sizes[m["menu_size"]] += 1
        buckets[m["format_bucket"]] += 1
        corpora[m["source_corpus"]] += 1
        land_drop += bool(m["is_land_drop"])
        pass_available += m.get("pass_pick") is not None
        # Where in the menu the answer sits, normalised: 0.0 first, 1.0 last.
        denom = max(m["menu_size"] - 1, 1)
        pick_frac[round((m["gold_pick"] - 1) / denom, 1)] += 1

    pass_n = action.get("ActionType_Pass", 0)
    size_vals = sorted(m["menu_size"] for m in (r["meta"] for r in records))
    pick_vals = sorted(m["gold_pick"] for m in (r["meta"] for r in records))
    return {
        "n": n,
        "by_source_corpus": dict(corpora.most_common()),
        "by_format_bucket": dict(buckets.most_common()),
        "by_gold_action_type": dict(action.most_common()),
        "pass_n": pass_n,
        "pass_share": round(pass_n / n, 4),
        "pass_option_available_share": round(pass_available / n, 4),
        "land_drop_n": land_drop,
        "land_drop_share": round(land_drop / n, 4),
        "strategic_n": n - land_drop,
        "menu_size": {
            "min": size_vals[0],
            "median": size_vals[n // 2],
            "max": size_vals[-1],
            "mean": round(sum(size_vals) / n, 2),
            "histogram": _hist(sizes),
        },
        "pick_index": {
            "min": pick_vals[0],
            "median": pick_vals[n // 2],
            "max": pick_vals[-1],
            "mean": round(sum(pick_vals) / n, 2),
            "histogram": _hist(picks),
            # Positional bias check: if the answer were uniform over the menu
            # this is flat. A spike at 0.0 or 1.0 means "always first/last"
            # scores well and the gate's permutation test will punish it.
            "normalised_position_histogram": _hist(pick_frac),
        },
        "reference_policies": {
            "always_first": round(sum(1 for r in records if r["meta"]["gold_pick"] == 1) / n, 4),
            "always_last": round(
                sum(1 for r in records if r["meta"]["gold_pick"] == r["meta"]["menu_size"]) / n, 4
            ),
            "always_pass": round(
                sum(
                    1
                    for r in records
                    if r["meta"].get("pass_pick") is not None
                    and r["meta"]["gold_pick"] == r["meta"]["pass_pick"]
                )
                / n,
                4,
            ),
            # The honesty baseline the gate manifest reports: 65% of these
            # decisions are land drops, so "play a land, else pick 1" already
            # scores well. Any model number has to beat this, not chance.
            "always_land_else_first": round(
                sum(1 for r in records if r["meta"]["gold_pick"] == (r["meta"].get("first_land_pick") or 1))
                / n,
                4,
            ),
            "uniform_random_expectation": round(sum(1.0 / r["meta"]["menu_size"] for r in records) / n, 4),
        },
    }


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------


def cmd_build(args: argparse.Namespace) -> int:
    from arenamcp.action_planner import AUTOPILOT_SYSTEM_PROMPT

    for path in (MANASIGHT_GT, REPLAY_GT, TRAINABLE_MANIFEST):
        if not path.exists():
            logger.error(f"missing input: {path}")
            return 2

    held = load_held_out()
    manifest = json.loads(TRAINABLE_MANIFEST.read_text(encoding="utf-8"))
    trainable = set(manifest.get("trainable_replays") or [])
    excluded = set(manifest.get("excluded_replays") or [])
    split_by_replay = manifest.get("split_by_replay") or {}

    # Manifest sanity. If "trainable" and the gate's own held-out replay list
    # disagree, the manifest's semantics are ambiguous -> be conservative and
    # refuse, rather than guessing which one is authoritative.
    manifest_conflict = sorted(trainable & held["replay_files"])
    if manifest_conflict:
        logger.error(
            f"manifest lists {len(manifest_conflict)} replays as trainable that appear in the "
            f"gate test/dev corpus: {manifest_conflict[:5]} — refusing to build"
        )
        return 2
    split_conflict = sorted(f for f in trainable if split_by_replay.get(f) != "train")
    if split_conflict:
        logger.error(f"manifest split_by_replay disagrees with trainable_replays: {split_conflict[:5]}")
        return 2

    facts = load_card_facts()

    records: list[dict] = []
    drops: dict[str, Counter] = defaultdict(Counter)
    read_counts: dict[str, int] = {}
    replay_filtered = 0

    for corpus, path in (("manasight", MANASIGHT_GT), ("replay", REPLAY_GT)):
        raw = _read_jsonl(path)
        read_counts[corpus] = len(raw)
        for rec in raw:
            rfile = rec.get("replay_file", "")
            if corpus == "replay":
                # Whitelist, not blacklist: a replay absent from the manifest
                # has unknown split and is treated as held out.
                if rfile not in trainable:
                    replay_filtered += 1
                    drops[corpus]["not_in_trainable_replays"] += 1
                    continue
            record, reason = build_bridge_record(
                rec, facts, source_corpus=corpus, system_prompt=AUTOPILOT_SYSTEM_PROMPT
            )
            if record is None:
                drops[corpus][reason] += 1
                continue
            # Per-record system-prompt identity guard. This is the check whose
            # absence killed the prior runs: the corpus is only production-
            # shaped if EVERY record carries the imported object's exact text.
            if record["system"] != AUTOPILOT_SYSTEM_PROMPT:
                raise AssertionError(f"system prompt drift on {rfile}#{rec.get('decision_index')}")
            records.append(record)

    if not records:
        logger.error("no usable bridge records — fail closed")
        return 2

    # ---- leakage measurement ------------------------------------------------
    bridge_hashes = Counter(r["meta"]["user_sha256"] for r in records)
    bridge_identities = {r["meta"]["decision_identity"] for r in records}
    bridge_replays = {r["meta"]["source"]["replay_file"] for r in records}

    hash_intersection = sorted(set(bridge_hashes) & held["prompt_hashes"])
    identity_intersection = sorted(bridge_identities & held["identities"])
    replay_intersection = sorted(bridge_replays & held["replay_files"])
    internal_dupes = {h: c for h, c in bridge_hashes.items() if c > 1}

    leakage = {
        "gate_files_checked": held["records_per_file"],
        "gate_prompts_hashed": len(held["prompt_hashes"]),
        "gate_decision_identities": len(held["identities"]),
        "gate_replay_files": len(held["replay_files"]),
        "bridge_prompt_hashes": len(bridge_hashes),
        "bridge_decision_identities": len(bridge_identities),
        "bridge_unique_record_identities": len({r["meta"]["record_identity"] for r in records}),
        "bridge_replay_files": len(bridge_replays),
        "prompt_hash_intersection_n": len(hash_intersection),
        "decision_identity_intersection_n": len(identity_intersection),
        "replay_file_intersection_n": len(replay_intersection),
        "replay_records_excluded_by_manifest": replay_filtered,
        "internal_duplicate_prompt_n": len(internal_dupes),
        "internal_duplicate_records": sum(internal_dupes.values()),
    }
    for key in (
        "prompt_hash_intersection_n",
        "decision_identity_intersection_n",
        "replay_file_intersection_n",
    ):
        if leakage[key]:
            logger.error(f"LEAKAGE: {key}={leakage[key]} — refusing to write a contaminated corpus")
            return 2

    # ---- how far the on-disk gate corpus has drifted from current code ------
    # Reported, not enforced: the gate corpus was built while MTGJSON was still
    # loading in the background, so a share of its prompts carry unresolved
    # card facts. Training prompts built now are correct; the gate's are not
    # uniformly so, and that residual skew belongs in the record.
    divergence = _measure_gate_divergence(facts)

    written = _write_jsonl(BRIDGE_OUT, records)
    stats = {
        "corpus": "play_decisions_bridge_v1",
        "built_ts": time.time(),
        "git_sha": G._git_sha(),
        "purpose": (
            "production-shaped (AUTOPILOT_SYSTEM_PROMPT + 'Legal: (pick by number)') play "
            "decisions built from REAL MTGA menus, to close the train/gate prompt-shape gap"
        ),
        "prompt_fidelity": {
            "system": "imported arenamcp.action_planner.AUTOPILOT_SYSTEM_PROMPT (asserted per record)",
            "user": "tools.training.gate_play_decisions.build_user_message -> "
            "ActionPlanner._build_action_prompt (the same call the gate corpus uses)",
            "trigger": G.TRIGGER,
            "response": '{"actions": [{"pick": N}]} with N = the real menu index the human clicked',
            "window": "start of own Main 1 only — the only window where the gate's trigger text "
            "and build_game_state's lands_played=0 / no-summoning-sickness assumptions hold",
        },
        "sources": {
            corpus: {
                "path": str(path),
                "sha256": G._sha256_file(path),
                "records_read": read_counts[corpus],
                "records_kept": sum(1 for r in records if r["meta"]["source_corpus"] == corpus),
                "drops": dict(drops[corpus].most_common()),
                "license": SOURCE_LABELS[corpus][1],
            }
            for corpus, path in (("manasight", MANASIGHT_GT), ("replay", REPLAY_GT))
        },
        "trainable_manifest": {
            "path": str(TRAINABLE_MANIFEST),
            "trainable_replays": len(trainable),
            "excluded_replays": len(excluded),
            "policy": "whitelist — a replay absent from trainable_replays is treated as held out",
        },
        "leakage": leakage,
        "gate_corpus_divergence": divergence,
        "distribution": summarise(records),
        "distribution_by_source": {
            corpus: summarise([r for r in records if r["meta"]["source_corpus"] == corpus])
            for corpus in ("manasight", "replay")
        },
        "output": {"path": str(BRIDGE_OUT), "records": written},
    }
    BRIDGE_STATS.write_text(json.dumps(stats, indent=2), encoding="utf-8")
    logger.info(f"wrote {written} bridge records -> {BRIDGE_OUT}")
    logger.info(f"stats -> {BRIDGE_STATS}")
    return 0


def _measure_gate_divergence(facts: G._CardFacts) -> dict:
    """Rebuild the gate's own test prompts and count how many still match.

    A mismatch is not a leak and not a defect in this builder — it measures how
    much of the on-disk gate corpus was rendered with unresolved card facts.
    """
    path = DATA / "gate_play_decisions_test.jsonl"
    if not path.exists() or not REPLAY_GT.exists():
        return {"checked": 0, "note": "gate test corpus or groundtruth missing"}
    gt = {_decision_identity(r["replay_file"], r["decision_index"]): r for r in _read_jsonl(REPLAY_GT)}
    checked = 0
    identical = 0
    for row in _read_jsonl(path):
        src = (row.get("meta") or {}).get("source") or {}
        rec = gt.get(_decision_identity(src.get("replay_file", ""), src.get("decision_index")))
        if rec is None:
            continue
        decision = G.build_decision(rec, facts)
        if decision is None or "drop_reason" in decision:
            continue
        checked += 1
        rebuilt = G.build_user_message(decision["game_state"], [r["text"] for r in decision["menu"]])
        identical += rebuilt == row["user"]
    divergent = checked - identical
    return {
        "checked": checked,
        "identical_n": identical,
        "divergent_n": divergent,
        "divergent_share": round(divergent / checked, 4) if checked else 0.0,
        "interpretation": (
            "0 means the on-disk gate corpus and this builder render the same decisions "
            "identically — train and gate agree in content as well as in shape."
            if divergent == 0
            else "NON-ZERO: the on-disk gate corpus does not reproduce from current code. "
            "Historically this meant it was built while arenamcp.mtgjson.get_mtgjson() was "
            "still loading in its background thread, so battlefield permanents rendered "
            "with no type line (0 mana, wrong [S,NEED:...] tags). Card sources now load "
            "fail-closed via arenamcp.card_db.require_card_database and the corpus builder "
            "self-checks, so a non-zero value here means the gate corpus is STALE — rebuild "
            "it with `python -m tools.training.gate_play_decisions build` before gating."
        ),
    }


# --------------------------------------------------------------------------
# mix
# --------------------------------------------------------------------------


def cmd_mix(args: argparse.Namespace) -> int:
    if not BRIDGE_OUT.exists():
        logger.error(f"bridge corpus missing — run `build` first: {BRIDGE_OUT}")
        return 2
    if not CANDIDATE_IN.exists():
        logger.error(f"candidate corpus missing: {CANDIDATE_IN}")
        return 2

    bridge = _read_jsonl(BRIDGE_OUT)
    repeat = max(1, int(args.bridge_repeat))

    # Candidate records are held as raw lines: 13,671 records / 141 MB, and
    # nothing here needs them parsed beyond a shape check on the first one.
    candidate_lines = [ln for ln in CANDIDATE_IN.read_text(encoding="utf-8").splitlines() if ln.strip()]
    probe = json.loads(candidate_lines[0])
    if set(probe) != set(bridge[0]):
        logger.error(f"record shape mismatch: candidate={sorted(probe)} bridge={sorted(bridge[0])}")
        return 2
    if "Candidate:" not in probe["user"]:
        logger.warning("candidate corpus first record does not carry the 'Candidate:' menu header")

    bridge_lines = [json.dumps(r, ensure_ascii=False) for r in bridge] * repeat
    mixed = bridge_lines + candidate_lines
    rng = random.Random(args.seed)
    rng.shuffle(mixed)

    MIXED_OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(MIXED_OUT, "w", encoding="utf-8") as f:
        for line in mixed:
            f.write(line + "\n")

    n_prod = len(bridge_lines)
    n_cand = len(candidate_lines)
    total = n_prod + n_cand
    stats = {
        "corpus": "play_decisions_mixed_v1",
        "built_ts": time.time(),
        "git_sha": G._git_sha(),
        "seed": args.seed,
        "shuffle": "random.Random(seed).shuffle over the concatenated line list",
        "inputs": {
            "bridge": {
                "path": str(BRIDGE_OUT),
                "records": len(bridge),
                "repeat": repeat,
                "emitted": n_prod,
                "shape": "production — AUTOPILOT_SYSTEM_PROMPT + 'Legal: (pick by number)'",
            },
            "candidate": {
                "path": str(CANDIDATE_IN),
                "records": n_cand,
                "shape": "candidate — 'Candidate: (pick by number — RECONSTRUCTED, ...)' "
                "(deliberately NOT relabelled: the synthesised menu really is incomplete)",
            },
        },
        "output": {"path": str(MIXED_OUT), "records": total},
        "ratio": {
            "production_shape_n": n_prod,
            "candidate_shape_n": n_cand,
            "production_share": round(n_prod / total, 4),
            "candidate_share": round(n_cand / total, 4),
            "candidate_per_production": round(n_cand / n_prod, 2),
        },
        "bridge_distribution": summarise(bridge),
    }
    MIXED_STATS.write_text(json.dumps(stats, indent=2), encoding="utf-8")
    logger.info(f"wrote {total} mixed records ({n_prod} production / {n_cand} candidate) -> {MIXED_OUT}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="build the production-shaped bridge corpus")
    b.set_defaults(func=cmd_build)

    m = sub.add_parser("mix", help="shuffle bridge + candidate records into the final training mix")
    m.add_argument("--seed", type=int, default=MIX_SEED)
    m.add_argument(
        "--bridge-repeat",
        type=int,
        default=1,
        help="emit each bridge record N times. Default 1 (no upsampling). Raise it only with a "
        "measured reason: the bridge corpus is the minority shape and duplicates trade "
        "shape coverage against memorisation risk.",
    )
    m.set_defaults(func=cmd_mix)

    a = sub.add_parser("all", help="build then mix")
    a.add_argument("--seed", type=int, default=MIX_SEED)
    a.add_argument("--bridge-repeat", type=int, default=1)
    a.set_defaults(func=lambda args: cmd_build(args) or cmd_mix(args))
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
