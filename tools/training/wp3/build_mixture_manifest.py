#!/usr/bin/env python3
"""Breadth-vs-depth mixture manifest PROTOTYPE (rl-pipeline-fix.md MORNING
PRIORITY item 4).

Proposal under evaluation: low-weight blunder-filtered HUMAN data (breadth
prior — thousands of distinct cards) under high-weight MAGEZERO self-play data
(depth). This tool builds the MANIFEST for a candidate mixture so the owner
can pick the weight from measured numbers. It does NOT write training data
unless ``--emit`` is passed — the owner decision on weights comes first.

Per source and for the mixture it reports:
  - record counts (with fail-closed drop-and-count for unparseable records)
  - production-shape fraction — the repo's ``Legal:`` menu heuristic
    (build_bridge_dataset.py): user contains ``Legal: (pick by number)`` and
    NOT ``Candidate:``; the Candidate: header marks synthesized 17lands menus
  - distinct-card coverage, validated against the MTGJSON name index
    (~/.arenamcp/mtgjson/name_index.json) — unvalidated extractions are
    counted, never silently included
  - gold-action class histogram (cast / play_land / pass / ...) per repo
    reporting culture
  - trivial-policy floor stats where computable (always_first / always_last /
    always_pass / always_land_else_first / uniform_random_expectation),
    the same reflex families gate_play_decisions.py floors G6 on. Records
    whose meta carries ``gold_equivalent_picks`` use it; otherwise the gold
    pick alone is the equivalence class (an UNDERESTIMATE of reflex accuracy
    vs the gate's version — stated in the manifest, not hidden).

Mixture semantics: ALL magezero records are kept (depth anchor); human
records are sampled deterministically (--seed) so that
human/(human+magezero) == --human-weight, capped at availability (capping is
reported, never silent).

Example (manifest only):
  python tools/training/wp3/build_mixture_manifest.py \\
      --magezero-corpus tools/training/data/wp3_v2_priority/train.jsonl \\
      --human-corpus tools/training/data/play_decisions_mixed.jsonl \\
      --human-weight 0.2 \\
      --out tools/training/data/mixture_manifest_w020.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import statistics
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

DEFAULT_VOCAB = os.path.expanduser("~/.arenamcp/mtgjson/name_index.json")

PRODUCTION_MENU_HEADER = "Legal: (pick by number)"  # build_bridge_dataset.py heuristic
CANDIDATE_MARKER = "Candidate:"
AUTOPILOT_SYSTEM_PREFIX = "You are an MTG Arena autopilot"

MENU_ENTRY_RE = re.compile(r"^\s+(\d+)\.\s+(.+?)\s*$")
# 2-space-indented entry line (hand/board): deeper indents are oracle text.
TWO_SPACE_ENTRY_RE = re.compile(r"^  (\S.*?)\s*$")
DISPLAY_DISAMBIG_RE = re.compile(r"\s*#\d+$")

NON_CARD_ENTRY_TEXT = {"(empty)", "empty", "none"}


# ---------------------------------------------------------------------------
# Vocab
# ---------------------------------------------------------------------------


def load_vocab(path: str) -> dict:
    """MTGJSON name index: lowercase name -> {name, type_line, ...}.

    Double-faced / split names ("A // B") are additionally indexed by each
    face, because XMage and MTGA prompts render a single face name (measured:
    'Cecil, Dark Knight', 'Malcolm' were the top unrecognized tokens before
    this). Face keys never overwrite an existing exact name.
    """
    with open(path, encoding="utf-8") as fh:
        vocab = json.load(fh)
    faces = {}
    for key, info in vocab.items():
        if " // " in key:
            for face in key.split(" // "):
                face = face.strip()
                if face and face not in vocab and face not in faces:
                    faces[face] = info
    vocab.update(faces)
    return vocab


def _clean_card_token(text: str) -> str:
    """Strip cost/flag/oracle suffixes from an entry line, keep the name part."""
    # Cut at the first cost/flag/parenthetical/dash-oracle suffix.
    for sep in ("  ", " {", " [", " (", " - "):
        idx = text.find(sep)
        if idx > 0:
            text = text[:idx]
    token = DISPLAY_DISAMBIG_RE.sub("", text.strip())
    # Measured rendering artifacts (top unrecognized tokens before these):
    token = re.sub(r"\s+x\d+$", "", token)  # "Forest x2" duplicate-count marker
    token = re.sub(r"\s+\d+/\d+$", "", token)  # "Gene Pollinator 1/2" P/T suffix
    token = re.sub(r",(attacking|blocking|tapped)$", "", token)  # XMage combat marker
    return token.strip()


def _resolve_card(token: str, vocab: dict) -> str | None:
    """Return canonical card name, or None if not a known card."""
    if not token or token.lower() in NON_CARD_ENTRY_TEXT:
        return None
    hit = vocab.get(token.lower())
    return hit["name"] if hit else None


# ---------------------------------------------------------------------------
# Menu parsing + action classification
# ---------------------------------------------------------------------------


def parse_menu(user: str) -> list[str]:
    """Numbered menu entry texts, in order. Empty list = no parseable menu."""
    lines = user.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if PRODUCTION_MENU_HEADER in ln or ln.lstrip().startswith("Candidate: (pick by number"):
            start = i + 1
            break
    if start is None:
        return []
    entries: list[str] = []
    expected = 1
    for ln in lines[start:]:
        m = MENU_ENTRY_RE.match(ln)
        if not m or int(m.group(1)) != expected:
            break
        entries.append(m.group(2))
        expected += 1
    return entries


def classify_entry(text: str, vocab: dict) -> tuple[str, str | None]:
    """(action_class, card_name_or_None) for one menu entry."""
    t = text.strip()
    # Strip trailing status tags like "[OK]".
    t = re.sub(r"\s*\[[A-Za-z !?+]*\]\s*$", "", t)
    if t == "Pass" or t.startswith(("Pass ", "Pass(", "Pass (")):
        return "pass", None
    if t.startswith("Play Land: "):
        return "play_land", _resolve_card(_clean_card_token(t[len("Play Land: ") :]), vocab)
    if t.startswith("Cast "):
        return "cast", _resolve_card(_clean_card_token(t[len("Cast ") :]), vocab)
    if t.startswith("Activate"):
        rest = re.sub(r"^Activate( ability of)?\s*", "", t)
        return "activate", _resolve_card(_clean_card_token(rest), vocab)
    if t.startswith("{") or "}: " in t:
        # XMage renders ability activations as the raw ability text, e.g.
        # "{T}: Draw a card, then discard a card." (measured in wp3_v2 menus).
        return "activate", None
    if t.startswith(("Declare Attackers", "Attack")):
        return "attack", None
    if t in ("KEEP", "MULLIGAN") or t.startswith(("Keep", "Mulligan")):
        return "mulligan_keep", None
    if t.startswith("Play "):
        # XMage/MageZero land plays render as "Play <name>"; MDFC/adventure
        # plays also land here. Card resolution decides whether it is a land.
        card = _resolve_card(_clean_card_token(t[len("Play ") :]), vocab)
        return "play", card
    return "other", None


def _entry_is_land_play(action_class: str, card: str | None, vocab: dict) -> bool:
    if action_class == "play_land":
        return True
    if action_class == "play" and card is not None:
        info = vocab.get(card.lower())
        return bool(info and "Land" in info.get("type_line", ""))
    return False


# ---------------------------------------------------------------------------
# Per-record extraction
# ---------------------------------------------------------------------------


def extract_cards_from_user(user: str, vocab: dict, unrecognized: Counter) -> set[str]:
    """Card names from menu entries + 2-space-indented hand/board lines.

    Fail-closed accounting: tokens that do not resolve against the vocab are
    tallied in ``unrecognized`` (and reported), never silently included or
    silently dropped.
    """
    cards: set[str] = set()
    for entry in parse_menu(user):
        action_class, card = classify_entry(entry, vocab)
        if card:
            cards.add(card)
        elif action_class in ("cast", "play", "play_land", "activate"):
            token = re.sub(r"^(Cast |Play Land: |Play |Activate( ability of)?\s*)", "", entry)
            unrecognized[_clean_card_token(token)] += 1
    for ln in user.splitlines():
        m = TWO_SPACE_ENTRY_RE.match(ln)
        if not m:
            continue
        if MENU_ENTRY_RE.match(ln):
            continue  # menu entries handled above
        token = _clean_card_token(m.group(1))
        card = _resolve_card(token, vocab)
        if card:
            cards.add(card)
        elif token and token.lower() not in NON_CARD_ENTRY_TEXT:
            unrecognized[token] += 1
    return cards


def extract_gold_pick(response: str) -> int | None:
    try:
        obj = json.loads(response)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(obj, dict):
        if isinstance(obj.get("pick"), int):
            return obj["pick"]
        actions = obj.get("actions")
        if isinstance(actions, list) and actions and isinstance(actions[0], dict):
            pick = actions[0].get("pick")
            if isinstance(pick, int):
                return pick
    return None


# ---------------------------------------------------------------------------
# Corpus scan
# ---------------------------------------------------------------------------


def scan_corpus(path: str, vocab: dict, *, keep_line_numbers: bool = False) -> dict:
    """One streaming pass; returns stats + (optionally) usable line numbers."""
    n_lines = 0
    drops = Counter()
    n_ok = 0
    production = 0
    candidate_shape = 0
    neither_shape = 0
    autopilot_system = 0
    menu_sizes: list[int] = []
    action_hist = Counter()
    source_hist = Counter()
    cards: set[str] = set()
    unrecognized: Counter = Counter()
    usable_lines: list[int] = []

    # Trivial-policy tallies (over records with a parseable menu + pick)
    triv_n = 0
    hits = Counter()  # policy -> hits
    rand_expect_sum = 0.0
    equivalence_from_meta = 0

    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh):
            n_lines += 1
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                drops["bad_json"] += 1
                continue
            user = rec.get("user")
            response = rec.get("response")
            if not isinstance(user, str) or not isinstance(response, str):
                drops["missing_user_or_response"] += 1
                continue
            n_ok += 1
            if keep_line_numbers:
                usable_lines.append(lineno)
            source_hist[str(rec.get("source", "?"))] += 1

            is_production = PRODUCTION_MENU_HEADER in user and CANDIDATE_MARKER not in user
            if is_production:
                production += 1
            elif CANDIDATE_MARKER in user:
                candidate_shape += 1
            else:
                neither_shape += 1
            if str(rec.get("system", "")).startswith(AUTOPILOT_SYSTEM_PREFIX):
                autopilot_system += 1

            cards |= extract_cards_from_user(user, vocab, unrecognized)

            menu = parse_menu(user)
            pick = extract_gold_pick(response)
            if not menu:
                drops["no_parseable_menu"] += 1
                continue
            menu_sizes.append(len(menu))
            if pick is None or not (1 <= pick <= len(menu)):
                drops["pick_unparseable_or_out_of_range"] += 1
                continue

            classified = [classify_entry(e, vocab) for e in menu]
            action_hist[classified[pick - 1][0]] += 1

            meta = rec.get("meta") if isinstance(rec.get("meta"), dict) else {}
            gold_set = meta.get("gold_equivalent_picks")
            if isinstance(gold_set, list) and gold_set:
                gold = {int(g) for g in gold_set}
                equivalence_from_meta += 1
            else:
                gold = {pick}

            pass_pick = next(
                (i + 1 for i, (ac, _) in enumerate(classified) if ac == "pass"), None
            )
            land_pick = next(
                (
                    i + 1
                    for i, (ac, card) in enumerate(classified)
                    if _entry_is_land_play(ac, card, vocab)
                ),
                None,
            )
            triv_n += 1
            if 1 in gold:
                hits["always_first_option"] += 1
            if len(menu) in gold:
                hits["always_last_option"] += 1
            if pass_pick is not None and pass_pick in gold:
                hits["always_pass"] += 1
            if (land_pick or 1) in gold:
                hits["always_land_else_first"] += 1
            if (pass_pick or 1) in gold:
                hits["always_pass_else_first"] += 1
            rand_expect_sum += len(gold) / len(menu)

    trivial: dict = {"computable_n": triv_n, "gold_equivalence_from_meta_n": equivalence_from_meta}
    if triv_n:
        for k in (
            "always_first_option",
            "always_last_option",
            "always_pass",
            "always_pass_else_first",
            "always_land_else_first",
        ):
            trivial[k] = round(hits[k] / triv_n, 4)
        trivial["uniform_random_expectation"] = round(rand_expect_sum / triv_n, 4)
        best = max(
            (k for k in ("always_land_else_first", "always_pass", "always_pass_else_first")),
            key=lambda k: trivial[k],
        )
        trivial["best_trivial_policy"] = best
        trivial["best_trivial_accuracy"] = trivial[best]
        trivial["note"] = (
            "records without meta.gold_equivalent_picks use the single gold pick "
            "as its own equivalence class — reflex accuracies are a lower bound "
            "vs gate_play_decisions.reference_policies on such records"
        )

    stats = {
        "path": path,
        "lines": n_lines,
        "records_usable": n_ok,
        "drops": dict(drops),
        "by_source": dict(source_hist),
        "production_shape": {
            "heuristic": f"user contains {PRODUCTION_MENU_HEADER!r} and not {CANDIDATE_MARKER!r} (build_bridge_dataset.py)",
            "production_n": production,
            "candidate_n": candidate_shape,
            "neither_n": neither_shape,
            "production_fraction": round(production / n_ok, 4) if n_ok else None,
            "autopilot_system_prompt_n": autopilot_system,
        },
        "menu_size": {
            "n": len(menu_sizes),
            "min": min(menu_sizes) if menu_sizes else None,
            "median": int(statistics.median(menu_sizes)) if menu_sizes else None,
            "mean": round(statistics.fmean(menu_sizes), 2) if menu_sizes else None,
            "max": max(menu_sizes) if menu_sizes else None,
        },
        "gold_action_class_histogram": dict(action_hist),
        "card_coverage": {
            "distinct_cards_recognized": len(cards),
            "unrecognized_distinct_tokens": len(unrecognized),
            "unrecognized_total_occurrences": sum(unrecognized.values()),
            "unrecognized_top20": unrecognized.most_common(20),
        },
        "trivial_policies": trivial,
    }
    return {"stats": stats, "cards": cards, "usable_lines": usable_lines}


# ---------------------------------------------------------------------------
# Mixture
# ---------------------------------------------------------------------------


def plan_mixture(n_mz: int, n_human_avail: int, human_weight: float, seed: int) -> dict:
    """All magezero records + enough sampled human records to hit the weight."""
    if not (0.0 <= human_weight < 1.0):
        raise ValueError("--human-weight must be in [0, 1)")
    n_human_target = round(human_weight / (1.0 - human_weight) * n_mz) if human_weight else 0
    capped = n_human_target > n_human_avail
    n_human = min(n_human_target, n_human_avail)
    total = n_mz + n_human
    return {
        "requested_human_weight": human_weight,
        "magezero_records": n_mz,
        "human_records_target": n_human_target,
        "human_records_available": n_human_avail,
        "human_records_sampled": n_human,
        "capped_by_availability": capped,
        "realized_human_share": round(n_human / total, 4) if total else 0.0,
        "total_records": total,
        "seed": seed,
    }


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(os.path.abspath(__file__)),
            check=True,
        ).stdout.strip()
    except Exception:
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--magezero-corpus", required=True, help="MageZero (depth) corpus .jsonl")
    ap.add_argument("--human-corpus", required=True, help="human (breadth) corpus .jsonl")
    ap.add_argument("--human-weight", type=float, required=True, help="target human share of the mixture, e.g. 0.2")
    ap.add_argument("--out", required=True, help="manifest JSON output path")
    ap.add_argument("--vocab", default=DEFAULT_VOCAB, help=f"MTGJSON name index (default {DEFAULT_VOCAB})")
    ap.add_argument("--seed", type=int, default=20260731)
    ap.add_argument(
        "--emit",
        default=None,
        metavar="PATH",
        help="ALSO write the mixed training .jsonl to PATH. Off by default: the "
        "owner picks the weight from the manifest BEFORE any training data is built.",
    )
    ap.add_argument(
        "--min-production-share",
        type=float,
        default=None,
        help="with --emit: refuse (exit 2) if the mixture's production-shape fraction is below this floor",
    )
    args = ap.parse_args(argv)

    if not os.path.isfile(args.vocab):
        print(f"FAIL: card-name vocab not found at {args.vocab} — coverage numbers would be unvalidated. Fail closed.")
        return 2
    vocab = load_vocab(args.vocab)

    t0 = time.time()
    mz = scan_corpus(args.magezero_corpus, vocab)
    human = scan_corpus(args.human_corpus, vocab, keep_line_numbers=True)

    n_mz = mz["stats"]["records_usable"]
    n_human_avail = human["stats"]["records_usable"]
    if n_mz == 0 or n_human_avail == 0:
        print("FAIL: a source corpus has zero usable records — fail closed.")
        return 2

    mixture = plan_mixture(n_mz, n_human_avail, args.human_weight, args.seed)

    # Deterministic sample of human usable-line indices.
    rng = random.Random(args.seed)
    sampled_positions = sorted(
        rng.sample(range(n_human_avail), mixture["human_records_sampled"])
    )
    sampled_linenos = set(human["usable_lines"][p] for p in sampled_positions)

    # Second pass over the human corpus: coverage + production share of the
    # sampled subset (exact, not extrapolated).
    sampled_cards: set[str] = set()
    sampled_production = 0
    unrec = Counter()
    with open(args.human_corpus, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh):
            if lineno not in sampled_linenos:
                continue
            rec = json.loads(raw)
            user = rec["user"]
            if PRODUCTION_MENU_HEADER in user and CANDIDATE_MARKER not in user:
                sampled_production += 1
            sampled_cards |= extract_cards_from_user(user, vocab, unrec)

    mix_total = mixture["total_records"]
    mix_production = mz["stats"]["production_shape"]["production_n"] + sampled_production
    mixture_stats = {
        **mixture,
        "distinct_cards": {
            "magezero": len(mz["cards"]),
            "human_full_corpus": len(human["cards"]),
            "human_sampled_subset": len(sampled_cards),
            "mixture_union": len(mz["cards"] | sampled_cards),
            "mixture_new_vs_magezero": len(sampled_cards - mz["cards"]),
            "full_union_ceiling": len(mz["cards"] | human["cards"]),
        },
        "production_shape_fraction": round(mix_production / mix_total, 4) if mix_total else None,
        "production_shape_n": mix_production,
    }

    manifest = {
        "manifest": "wp3_mixture_prototype_v1",
        "purpose": (
            "breadth-vs-depth mixture PROTOTYPE — owner picks --human-weight from these "
            "numbers before any training data is built (rl-pipeline-fix.md MORNING PRIORITY item 4)"
        ),
        "built_ts": time.time(),
        "git_sha": _git_sha(),
        "elapsed_seconds": round(time.time() - t0, 2),
        "vocab": {"path": args.vocab, "entries": len(vocab)},
        "sources": {
            "magezero": mz["stats"],
            "human": human["stats"],
        },
        "mixture": mixture_stats,
        "emitted": None,
    }

    if args.emit:
        if os.path.exists(args.emit):
            print(f"FAIL: --emit target {args.emit} already exists — refusing to overwrite.")
            return 2
        if (
            args.min_production_share is not None
            and mixture_stats["production_shape_fraction"] < args.min_production_share
        ):
            print(
                f"FAIL: mixture production-shape fraction "
                f"{mixture_stats['production_shape_fraction']} < floor {args.min_production_share} — fail closed."
            )
            return 2
        lines_out: list[str] = []
        with open(args.magezero_corpus, encoding="utf-8") as fh:
            for raw in fh:
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec.get("user"), str) and isinstance(rec.get("response"), str):
                    lines_out.append(raw if raw.endswith("\n") else raw + "\n")
        with open(args.human_corpus, encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh):
                if lineno in sampled_linenos:
                    lines_out.append(raw if raw.endswith("\n") else raw + "\n")
        rng2 = random.Random(args.seed + 1)
        rng2.shuffle(lines_out)
        with open(args.emit, "w", encoding="utf-8") as fh:
            fh.writelines(lines_out)
        sha = hashlib.sha256("".join(lines_out).encode("utf-8")).hexdigest()
        manifest["emitted"] = {"path": args.emit, "records": len(lines_out), "sha256": sha}
        print(f"emitted {len(lines_out)} records -> {args.emit}")
    else:
        print("manifest only — no training data written (pass --emit to build the mixture file)")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")
    print(f"manifest -> {args.out}")
    print(
        json.dumps(
            {
                "magezero_records": n_mz,
                "human_records_available": n_human_avail,
                "mixture": mixture_stats,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
