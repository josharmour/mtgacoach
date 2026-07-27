#!/usr/bin/env python3
"""Split the COMBAT decision corpus into leak-free train / dev / test gate splits.

Why this file exists
--------------------
``tools/training/data/combat_decisions.jsonl`` (96,131 records) is the first
corpus whose answer is a **subset**, not a menu index: production's
``declare_attackers`` / ``declare_blockers`` actions carry an explicit creature
list, so a ``{"pick": N}`` split of it would be unexecutable (see the corpus's
own ``READ_THIS_FIRST``). It therefore needs its own held-out splits before any
of it is trained on, built with the same discipline as the play / strategic
gate corpora (``gate_play_decisions.assign_splits``,
``build_bridge_dataset``'s leakage block):

  * the leakage unit is the **game**, never the record;
  * splits are stratified so the held-out sets are not a different corpus from
    the training set;
  * dev/test get **permuted twins** — same decision, menu rows shuffled — as the
    memorisation tripwire (``gate_play_decisions.permute_decision``'s idea,
    adapted: there is no gold index to move, so the tripwire asks whether the
    *set* the model answers with survives a reordering of the menu);
  * everything is written to a manifest with the source hash, the git sha, the
    seed, the filters and the leak-check result.

Leakage unit — the game, enforced at DRAFT level
------------------------------------------------
``meta.record_key`` is ``draft_id|game_no|set|turn_tag|kind_tag``. Two records
from one game share a board, a 40-card pool, an opponent and a deck, so a
record-level split would put near-identical states on both sides of the fence.

**Individual games are not addressable in this corpus**: ``game_no`` is the
literal string ``1`` on all 96,121 17lands records, so several different games
of one draft run collide on one ``record_key``. Verified — the four records
keyed ``b3de3afa…|1|EOE|t6|atk`` are four separate games (life totals
24/8, 16/14, 18/17, 18/13; ``won`` and ``on_play`` both differ).

The group key is therefore ``draft_id|game_no``, which collapses to
``draft_id`` — the DRAFT, a strict superset of the game. No game can span
splits because no draft does, and the same guard also stops one 40-card pool
from appearing on both sides. The 4 ``.rply`` records, whose record_key is
``<file>.rply|msg<N>|<kind>``, are grouped by replay file stem.

Record ids are re-minted (they are not unique upstream)
------------------------------------------------------
``id`` is derived from ``record_key``, so it inherits the collision: the source
has **51,668 distinct ids for 96,131 records**. Any harness that keys responses
by prompt id — ``tools.eval.run``, ``gate_play_decisions.evaluate``'s
``by_id = {rec["id"]: rec}`` — would silently score one record per id and drop
the other 46%. Every emitted record therefore gets ``<orig_id>-<NN>``, NN being
its occurrence ordinal in source file order (stable, because the manifest pins
the source sha256); the original is preserved as ``meta.source_record_id``.
This is a workaround, not a fix: ``build_combat_decisions.py`` should put a real
game discriminator in ``record_key``.

Stratification
--------------
Strata are the two attributes that are properties of the GAME (so a group can
carry exactly one value): expansion x rank. Within that, allocation is a
need-weighted greedy over the record-level labels that must be preserved but
are NOT group-constant — ``kind`` x answer class:

    combat_attack : no_attack | all_in | proper_subset
    combat_block  : no_block  | partial_block | block_all

(``block_all`` = every creature in the blocker pool blocks; ``partial_block`` =
some but not all.) A group is scored against each of dev/test by how much of
each split's *remaining* per-label need it fills, normalised by the corpus
total for that label so a rare class (``block_all``, 0.8% of the corpus) pulls
as hard as a common one. Groups whose best score is 0 fall through to train.

Filters (both recorded in the manifest)
---------------------------------------
1. The 10 non-17lands records (6 manasight player-log, 4 ``.rply``) are forced
   to TRAIN. They are a different provenance with null rank/expansion and are
   too few to stratify; letting them fall into a gate split would add an
   unmeasurable slice to it.
2. A prompt whose sha256 also occurs in another game is dropped from dev/test
   (kept in train). Measured: 2 prompt hashes span 2 games (8 records). An
   identical prompt on both sides of the fence is memorisable regardless of
   which games it came from — this is ``build_bridge_dataset``'s prompt-hash
   intersection check applied at split time rather than after the fact.

Usage
-----
    .venv/bin/python -m tools.training.split_combat_gate
    .venv/bin/python -m tools.training.split_combat_gate --seed 20260726 \
        --test-fraction 0.07 --dev-fraction 0.07
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "tools" / "training" / "data"
SOURCE = DATA / "combat_decisions.jsonl"
STEM = "combat_gate"

DEFAULT_SEED = 20260726
DEFAULT_TEST_FRACTION = 0.07
DEFAULT_DEV_FRACTION = 0.07

MENU_HEADER = "Legal: (pick by number)"
MENU_RE = re.compile(r"^Legal: \(pick by number\)\n((?:  \d+\. .*\n)+)", re.M)
ROW_RE = re.compile(r"^  (\d+)\. (.*)$")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, timeout=10
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def game_id(record_key: str) -> str:
    """Leakage unit. ``draft_id|game_no`` for 17lands, replay stem for .rply."""
    parts = (record_key or "").split("|")
    if len(parts) == 5:
        return f"{parts[0]}|{parts[1]}"
    if parts and parts[0].endswith(".rply"):
        return parts[0]
    return record_key or "UNKNOWN"


def answer_class(kind: str, meta: dict) -> str:
    """The label shape the gate slices on."""
    if kind == "combat_attack":
        if meta.get("is_no_attack"):
            return "no_attack"
        if meta.get("is_all_in"):
            return "all_in"
        if meta.get("is_proper_subset"):
            return "proper_subset"
        return "other_attack"
    if meta.get("is_no_block"):
        return "no_block"
    pool = meta.get("blocker_pool_size") or 0
    n = meta.get("n_blocks") or 0
    if n and pool and n >= pool:
        return "block_all"
    return "partial_block"


def response_names(response: str) -> list[str]:
    """Creature names the label commits to — the thing permutation must not move."""
    try:
        action = json.loads(response)["actions"][0]
    except Exception:  # noqa: BLE001
        return []
    if action.get("action_type") == "declare_attackers":
        return list(action.get("attacker_names") or [])
    return list((action.get("blocker_assignments") or {}).keys())


def menu_rows(user: str) -> tuple[str, list[str], str, str]:
    """(prefix, creature rows, done row, suffix) — rows are text WITHOUT ``  N. ``."""
    m = MENU_RE.search(user)
    if not m:
        raise ValueError("prompt has no numbered Legal: menu")
    if user.count(MENU_HEADER) != 1:
        raise ValueError("prompt has more than one Legal: menu")
    block = m.group(1)
    texts = []
    for line in block.rstrip("\n").split("\n"):
        rm = ROW_RE.match(line)
        if not rm:
            raise ValueError(f"unparseable menu row: {line!r}")
        texts.append(rm.group(2))
    if not texts[-1].startswith("Done"):
        raise ValueError("last menu row is not the Done confirm row")
    for t in texts[:-1]:
        if not (t.startswith("Attack with: ") or t.startswith("Block with: ")):
            raise ValueError(f"unexpected non-combat menu row: {t!r}")
    return user[: m.start(1)], texts[:-1], texts[-1], user[m.end(1) :]


# Every menu row shape in the corpus, measured over all 96,131 records:
#   216,081  ``Attack with: NAME (2/3)``
#    58,211  ``Block with: NAME``
#     2,503  ``Attack with: NAME``                  (variable P/T, no printed box)
#     2,193  ``Attack with: NAME (0/4) [0 POWER — attacking deals 0 damage]``
# The annotation suffix is why a naive ``rsplit(" (")`` mis-parsed 630 records.
_ANNOTATION_RE = re.compile(r"\s*\[[^\[\]]*\]\s*$")
_PT_RE = re.compile(r"\s*\(-?[\d*+]+/-?[\d*+]+\)\s*$")


def row_name(text: str) -> str:
    """Creature name from a menu row (``Attack with: X (2/3) [ann]`` / ``Block with: X``)."""
    body = text.split(": ", 1)[1] if ": " in text else text
    body = _ANNOTATION_RE.sub("", body)
    body = _PT_RE.sub("", body)
    return body.strip()


def render_menu(prefix: str, rows: list[str], done: str, suffix: str) -> str:
    lines = [f"  {i}. {t}" for i, t in enumerate(rows + [done], start=1)]
    return prefix + "\n".join(lines) + "\n" + suffix


def permute_record(rec: dict, seed: int) -> dict:
    """Permuted twin: identical decision, shuffled menu rows, identical response.

    The shuffle is forced off the identity permutation (menus here have >= 2
    creature rows, so one always exists) — an unmoved menu measures nothing.
    ``Done`` stays last because production always renders it last; moving it
    would change the decision's shape, not just its order.
    """
    prefix, rows, done, suffix = menu_rows(rec["user"])
    order = list(range(len(rows)))
    rng = random.Random(f"{seed}:{rec['id']}")
    for _ in range(64):
        rng.shuffle(order)
        if order != sorted(order):
            break
    new_rows = [rows[i] for i in order]
    out = dict(rec)
    out["id"] = f"{rec['id']}-perm"
    out["user"] = render_menu(prefix, new_rows, done, suffix)
    meta = dict(rec["meta"])
    meta["variant"] = "permuted"
    meta["twin_id"] = rec["id"]
    meta["permutation"] = [i + 1 for i in order]
    out["meta"] = meta
    # --- the assertions this variant exists to make -----------------------
    assert out["response"] == rec["response"], "permutation changed the label"
    assert sorted(new_rows) == sorted(rows), "permutation changed the menu contents"
    assert new_rows != rows, "permutation is the identity"
    names = set(response_names(rec["response"]))
    available = {row_name(t) for t in new_rows}
    assert names <= available, f"label names not expressible in permuted menu: {names - available}"
    return out


# --------------------------------------------------------------------------
# pass 1 — index
# --------------------------------------------------------------------------


def unique_id(orig_id: str, ordinal: int) -> str:
    """Source ids collide (see module docstring) — disambiguate by file order."""
    return f"{orig_id}-{ordinal:02d}"


def build_index(source: Path) -> list[dict]:
    index: list[dict] = []
    seen_ids: Counter = Counter()
    with open(source, "rb") as f:
        for lineno, raw in enumerate(f):
            rec = json.loads(raw)
            meta = rec["meta"]
            uid = unique_id(rec["id"], seen_ids[rec["id"]])
            seen_ids[rec["id"]] += 1
            cls = answer_class(rec["kind"], meta)
            # Checked here, before a single byte is written: the label is a list
            # of NAMES, so the permuted twin is only valid if every name is a
            # menu row. Anything else means the corpus, not the permutation, is
            # broken — and the run must stop rather than emit half a corpus.
            _, rows, _, _ = menu_rows(rec["user"])
            label_ok = set(response_names(rec["response"])) <= {row_name(t) for t in rows}
            index.append(
                {
                    "lineno": lineno,
                    "id": uid,
                    "source_record_id": rec["id"],
                    "label_expressible": label_ok,
                    "kind": rec["kind"],
                    "source": rec.get("source"),
                    "game_id": game_id(meta.get("record_key", "")),
                    "answer_class": cls,
                    "rank": meta.get("rank"),
                    "expansion": meta.get("expansion"),
                    "turn": meta.get("turn"),
                    "menu_size": meta.get("menu_size"),
                    "solver_agrees": meta.get("solver_agrees"),
                    "prompt_sha": hashlib.sha256(rec["user"].encode()).hexdigest(),
                    "label": f"{rec['kind']}:{cls}",
                }
            )
    return index


# --------------------------------------------------------------------------
# pass 2 — group-level stratified allocation
# --------------------------------------------------------------------------


def assign_splits(
    index: list[dict],
    *,
    seed: int,
    test_fraction: float,
    dev_fraction: float,
    forced_train_games: set[str] | None = None,
) -> dict:
    """Need-weighted greedy over games. Returns {game_id: split} + diagnostics."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in index:
        groups[row["game_id"]].append(row)

    # Games containing a filtered record are pinned to train WHOLESALE. Demoting
    # the single offending record instead puts the rest of its game in dev/test
    # while the record sits in train — which is the leak the split exists to
    # prevent (caught by the leak check on the first dry run: train&test = 1).
    forced_train = set(forced_train_games or ())
    forced_train |= {
        gid for gid, rows in groups.items() if any(r["source"] != "17lands-replay_data" for r in rows)
    }

    # Stratum = the game-constant attributes. Each stratum gets its OWN dev/test
    # budget (frac x that stratum's records), so expansion and rank proportions
    # are preserved by construction rather than hoped for — a single global
    # budget lets whichever stratum is processed first eat the whole allowance
    # (measured: dev and test came out 100% EOE).
    def stratum(rows: list[dict]) -> tuple:
        return (
            Counter(r["expansion"] for r in rows).most_common(1)[0][0],
            Counter(r["rank"] for r in rows).most_common(1)[0][0],
        )

    by_stratum: dict[tuple, list[str]] = defaultdict(list)
    for gid, rows in groups.items():
        if gid in forced_train:
            continue
        by_stratum[stratum(rows)].append(gid)

    assignment: dict[str, str] = {gid: "train" for gid in forced_train}
    stratum_report: dict[str, dict] = {}

    for strat in sorted(by_stratum, key=lambda t: (str(t[0]), str(t[1]))):
        gids = sorted(by_stratum[strat])
        random.Random(f"{seed}:{strat}").shuffle(gids)
        s_rows = [r for gid in gids for r in groups[gid]]
        s_totals = Counter(r["label"] for r in s_rows)
        s_n = len(s_rows)
        targets = {
            "test": {"labels": {k: v * test_fraction for k, v in s_totals.items()}, "n": s_n * test_fraction},
            "dev": {"labels": {k: v * dev_fraction for k, v in s_totals.items()}, "n": s_n * dev_fraction},
        }
        have = {s: {"labels": Counter(), "n": 0} for s in ("test", "dev")}
        for gid in gids:
            rows = groups[gid]
            counts = Counter(r["label"] for r in rows)
            best_split, best_score = None, 0.0
            for split in ("test", "dev"):
                t, h = targets[split], have[split]
                if h["n"] + len(rows) > t["n"] * 1.02:
                    continue  # hard cap: never overshoot this stratum's budget
                score = 0.0
                for lbl, c in counts.items():
                    need = t["labels"][lbl] - h["labels"][lbl]
                    if need > 0:
                        score += min(need, c) / max(s_totals[lbl], 1)
                # keeps the split filling toward its record budget once every
                # per-label need is met, so the sizes do not fall short
                n_need = t["n"] - h["n"]
                if n_need > 0:
                    score += 0.1 * min(n_need, len(rows)) / max(s_n, 1)
                if score > best_score:
                    best_split, best_score = split, score
            if best_split is None:
                assignment[gid] = "train"
                continue
            assignment[gid] = best_split
            have[best_split]["labels"].update(counts)
            have[best_split]["n"] += len(rows)
        stratum_report[f"{strat[0]}|{strat[1]}"] = {
            "games": len(gids),
            "records": s_n,
            "test_records": have["test"]["n"],
            "dev_records": have["dev"]["n"],
        }

    return {
        "assignment": assignment,
        "forced_train_games": sorted(forced_train),
        "strata": stratum_report,
    }


# --------------------------------------------------------------------------
# distributions + leak check
# --------------------------------------------------------------------------


def distributions(rows: list[dict]) -> dict:
    n = len(rows)

    def dist(key) -> dict:
        c = Counter(key(r) for r in rows)
        return {str(k): {"n": v, "pct": round(100.0 * v / n, 2)} for k, v in sorted(c.items(), key=str)}

    turns = sorted(r["turn"] for r in rows if r["turn"] is not None)
    sizes = sorted(r["menu_size"] for r in rows if r["menu_size"] is not None)
    return {
        "n": n,
        "groups_drafts": len({r["game_id"] for r in rows}),
        "kind": dist(lambda r: r["kind"]),
        "answer_class": dist(lambda r: r["label"]),
        "rank": dist(lambda r: r["rank"]),
        "expansion": dist(lambda r: r["expansion"]),
        "menu_size_median": sizes[len(sizes) // 2] if sizes else None,
        "turn_median": turns[len(turns) // 2] if turns else None,
        "solver_agrees_pct": round(100.0 * sum(1 for r in rows if r["solver_agrees"]) / n, 2) if n else None,
    }


def leak_check(by_split: dict[str, list[dict]]) -> dict:
    games = {s: {r["game_id"] for r in rows} for s, rows in by_split.items()}
    prompts = {s: {r["prompt_sha"] for r in rows} for s, rows in by_split.items()}
    pairs = [("train", "dev"), ("train", "test"), ("dev", "test")]
    out = {
        "unit": "draft (draft_id|game_no from meta.record_key) — a strict superset of the game, "
        "because game_no is a constant '1' upstream and cannot address individual games",
        "groups_per_split": {s: len(v) for s, v in games.items()},
        "game_intersections": {f"{a}&{b}": sorted(games[a] & games[b])[:20] for a, b in pairs},
        "game_intersection_sizes": {f"{a}&{b}": len(games[a] & games[b]) for a, b in pairs},
        "prompt_sha_intersection_sizes": {f"{a}&{b}": len(prompts[a] & prompts[b]) for a, b in pairs},
    }
    out["clean"] = all(v == 0 for v in out["game_intersection_sizes"].values()) and all(
        v == 0 for v in out["prompt_sha_intersection_sizes"].values()
    )
    return out


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", type=Path, default=SOURCE)
    ap.add_argument("--out-dir", type=Path, default=DATA)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--test-fraction", type=float, default=DEFAULT_TEST_FRACTION)
    ap.add_argument("--dev-fraction", type=float, default=DEFAULT_DEV_FRACTION)
    args = ap.parse_args()

    t0 = time.time()
    print(f"[1/4] indexing {args.source} ...", flush=True)
    index = build_index(args.source)
    print(f"      {len(index)} records, {len({r['game_id'] for r in index})} games", flush=True)

    if len({r["id"] for r in index}) != len(index):
        print("minted ids are not unique — refusing to split", file=sys.stderr)
        return 2

    inexpressible = [r["id"] for r in index if not r["label_expressible"]]
    if inexpressible:
        print(
            f"CORPUS DEFECT — {len(inexpressible)} labels name a creature that is not a menu row "
            f"(e.g. {inexpressible[:5]}); refusing to split",
            file=sys.stderr,
        )
        return 2

    # Filter 2: a prompt occurring in more than one game makes EVERY game it
    # touches train-only (a game is atomic; see assign_splits).
    sha_games = defaultdict(set)
    for r in index:
        sha_games[r["prompt_sha"]].add(r["game_id"])
    cross_game_shas = {s for s, g in sha_games.items() if len(g) > 1}
    cross_game_records = [r["id"] for r in index if r["prompt_sha"] in cross_game_shas]
    cross_game_games = {g for s in cross_game_shas for g in sha_games[s]}

    print("[2/4] assigning splits ...", flush=True)
    res = assign_splits(
        index,
        seed=args.seed,
        test_fraction=args.test_fraction,
        dev_fraction=args.dev_fraction,
        forced_train_games=cross_game_games,
    )
    assignment = res["assignment"]

    split_of_id: dict[str, str] = {}
    by_split: dict[str, list[dict]] = defaultdict(list)
    for r in index:
        split = assignment.get(r["game_id"], "train")
        split_of_id[r["id"]] = split
        by_split[split].append(r)

    print("      " + ", ".join(f"{s}={len(v)}" for s, v in sorted(by_split.items())), flush=True)

    leak = leak_check(by_split)
    if not leak["clean"]:
        print("LEAKAGE DETECTED — refusing to write:", json.dumps(leak, indent=2), file=sys.stderr)
        return 2

    print("[3/4] writing splits + permuted twins ...", flush=True)
    paths = {s: args.out_dir / f"{STEM}_{s}.jsonl" for s in ("train", "dev", "test")}
    perm_paths = {s: args.out_dir / f"{STEM}_{s}_permuted.jsonl" for s in ("dev", "test")}
    handles = {s: open(p, "w", encoding="utf-8") for s, p in paths.items()}
    perm_handles = {s: open(p, "w", encoding="utf-8") for s, p in perm_paths.items()}
    written = Counter()
    seen_ids: Counter = Counter()
    try:
        with open(args.source, encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                # Same file order as build_index, so the same ordinal.
                orig_id = rec["id"]
                rec["id"] = unique_id(orig_id, seen_ids[orig_id])
                seen_ids[orig_id] += 1
                split = split_of_id[rec["id"]]
                meta = dict(rec["meta"])
                meta["source_record_id"] = orig_id
                meta["split"] = split
                meta["variant"] = "identity"
                meta["game_id"] = game_id(meta.get("record_key", ""))
                meta["answer_class"] = answer_class(rec["kind"], meta)
                if split in perm_handles:
                    meta["twin_id"] = f"{rec['id']}-perm"
                out = dict(rec)
                out["meta"] = meta
                handles[split].write(json.dumps(out, ensure_ascii=False) + "\n")
                written[split] += 1
                if split in perm_handles:
                    twin = permute_record(out, args.seed)
                    perm_handles[split].write(json.dumps(twin, ensure_ascii=False) + "\n")
                    written[f"{split}_permuted"] += 1
    finally:
        for h in list(handles.values()) + list(perm_handles.values()):
            h.close()

    print("[4/4] manifest ...", flush=True)
    dists = {s: distributions(rows) for s, rows in by_split.items()}
    dists["source_all"] = distributions(index)
    def _slice_n(split: str, label: str) -> int:
        return dists[split]["answer_class"].get(label, {}).get("n", 0)

    def _halfwidth(n: int, p: float = 0.5, z: float = 1.959963984540054) -> float | None:
        """Wilson 95% half-width at the worst case p=0.5 — 'how tight is this slice'."""
        if n <= 0:
            return None
        denom = 1.0 + z * z / n
        return round(100 * (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom, 1)

    gated = {}
    for name, label in (
        ("attack_proper_subset", "combat_attack:proper_subset"),
        ("block_partial", "combat_block:partial_block"),
        ("block_all", "combat_block:block_all"),
        ("attack_no_attack", "combat_attack:no_attack"),
        ("attack_all_in", "combat_attack:all_in"),
        ("block_no_block", "combat_block:no_block"),
    ):
        for split in ("dev", "test"):
            n = _slice_n(split, label)
            gated[f"{name}_{split}"] = {
                "n": n,
                "wilson95_halfwidth_pp_at_p50": _halfwidth(n),
                "too_small_to_gate": n < 200,
            }
    manifest = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "builder": "tools/training/split_combat_gate.py",
        "git_sha": _git_sha(),
        "source": {
            "path": str(args.source.relative_to(REPO)),
            "sha256": _sha256_file(args.source),
            "records": len(index),
            "games": len({r["game_id"] for r in index}),
        },
        "seed": args.seed,
        "fractions": {"test": args.test_fraction, "dev": args.dev_fraction, "train": "remainder"},
        "leakage_unit": (
            "draft — meta.record_key -> draft_id|game_no (replay stem for .rply records). "
            "game_no is a constant '1' upstream so individual games are not addressable; the "
            "draft strictly contains every game of that run, so no game spans splits."
        ),
        "id_reminting": {
            "reason": (
                "source ids are hashes of record_key and collide: 51,668 distinct ids for 96,131 "
                "records. A harness keying responses by prompt id would score one record per id "
                "and silently drop the rest."
            ),
            "rule": "<source_id>-<NN>, NN = occurrence ordinal in source file order",
            "source_distinct_ids": len({r["source_record_id"] for r in index}),
            "emitted_distinct_ids": len({r["id"] for r in index}),
            "original_kept_at": "meta.source_record_id",
        },
        "algorithm": (
            "groups (= games) bucketed by stratum (expansion x majority rank), shuffled with "
            "Random(f'{seed}:{stratum}'), then allocated by need-weighted greedy: a game goes to "
            "the split whose remaining per-label deficit it best fills (labels = kind x answer "
            "class, normalised by corpus total; rank/expansion at weight 0.25), with a hard "
            "1.02x budget cap; score 0 => train."
        ),
        "filters": [
            {
                "name": "non_17lands_forced_train",
                "rule": "any game containing a record whose source != '17lands-replay_data' is pinned to train",
                "groups": len({r["game_id"] for r in index if r["source"] != "17lands-replay_data"}),
                "records": sum(1 for r in index if r["source"] != "17lands-replay_data"),
            },
            {
                "name": "cross_game_duplicate_prompt_forced_train",
                "rule": (
                    "if sha256(user) occurs in more than one game, every game it touches is pinned "
                    "to train (whole games, never single records)"
                ),
                "prompt_hashes": len(cross_game_shas),
                "records": len(cross_game_records),
                "groups_pinned": len(cross_game_games),
            },
        ],
        "strata": res["strata"],
        "total_groups_pinned_to_train": len(res["forced_train_games"]),
        "splits": {s: written[s] for s in ("train", "dev", "test", "dev_permuted", "test_permuted")},
        "distributions": dists,
        "leak_check": leak,
        "gated_slice_sizes": gated,
        "permutation": {
            "scope": "the 'Attack with:' / 'Block with:' rows of the numbered Legal: menu; the "
            "'Done (confirm ...)' row stays last",
            "rng": "Random(f'{seed}:{record_id}'), reshuffled until != identity",
            "response_unchanged": True,
            "asserted": [
                "response byte-identical to the identity twin",
                "menu row multiset unchanged",
                "permutation != identity",
                "every name in the label is still a menu row in the permuted menu",
            ],
            "caveat": (
                "labels are name-lists, so a permuted answer is correct iff the SET matches — a "
                "scorer that compares list ORDER will report false failures. The 'Computed optimal "
                "attack' solver line and the YOUR BOARD ordering are NOT permuted, so this tripwire "
                "measures menu-position memorisation only."
            ),
        },
        "label_expressible_failures": len(inexpressible),
        "files": {
            **{s: str(p.relative_to(REPO)) for s, p in paths.items()},
            **{f"{s}_permuted": str(p.relative_to(REPO)) for s, p in perm_paths.items()},
        },
        "elapsed_sec": round(time.time() - t0, 1),
    }
    mpath = args.out_dir / f"{STEM}_manifest.json"
    mpath.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"splits": manifest["splits"], "gated": gated, "leak_clean": leak["clean"]}, indent=2))
    print(f"manifest -> {mpath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
