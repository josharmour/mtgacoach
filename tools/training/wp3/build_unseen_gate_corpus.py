#!/usr/bin/env python3
"""UNSEEN-DECK gate slice builder: MageZero logs -> gate-shaped eval prompts.

Purpose (rl-pipeline-fix.md MORNING PRIORITY item 1)
----------------------------------------------------
Turn MageZero MCTS games played with PERMANENT-HOLDOUT decks (HighNoonControl,
BGRoots, BWBats — never in any training corpus) into an eval corpus with the
SAME record shape as the strategic gate corpus
(``tools/training/data/gate_strategic_decisions_test.jsonl``, built by
``tools.training.gate_play_decisions``), so ``run_b5_gate_eval.py
--unseen-corpus`` can score any candidate+baseline pair on it and report the
seen-vs-unseen generalization gap.

Shape contract (verified against the real seen corpus)
------------------------------------------------------
Top-level: ``{id, system, user, max_tokens, temperature, meta}`` where
``system`` is the imported production ``AUTOPILOT_SYSTEM_PROMPT`` and ``user``
is rendered by the SAME production prompt builder the training corpus uses
(``gate_play_decisions.build_user_message`` via
``build_magezero_bridge.build_game_state``) — never re-implemented.
``meta`` carries every field ``gate_play_decisions.evaluate`` reads:
``menu`` (rows with index/text/action_key/action_type/grp_id/instance_id/
name), ``gold_pick``, ``gold_equivalent_picks``, ``menu_size``,
``is_land_drop``, ``gold_action_type``, ``format_bucket``, ``request_type``,
``deck_seen_in_train`` (False — permanent holdout), ``pass_pick``,
``first_land_pick``, ``split``, ``variant``, ``twin_id``.
The permuted twin file mirrors the seen gate's convention: ids get the
``#perm`` suffix, twins are produced by ``gate_play_decisions.permute_decision``
(the shuffle is forced to move the gold pick when possible).

Honest differences from the seen corpus, recorded in the manifest:
  * the gold label is the MCTS TEACHER's pick, not a human pick;
  * card facts are name-only (no grp_ids / oracle text / card_facts_audit) —
    identical to the WP-3 training records, so seen-vs-unseen is NOT
    prompt-fidelity-matched to the replay-derived seen gate; the
    generalization gap section in run_b5_gate_eval reports both slices'
    provenance for exactly this reason;
  * ``is_own_turn`` mirrors the bridge's known limitation (active player is
    always the local seat in the reconstructed state).

Fail-closed rules
-----------------
  * every input log MUST resolve a primary deck (filename inference or
    --primary-deck); the parser's hand deck-signature guardrail runs and any
    off-deck hand card aborts (parse_magezero_log.DeckSignatureError);
  * a primary deck that is a KNOWN TRAINING DECK aborts — this corpus is
    definitionally unseen;
  * any rendered prompt whose sha256 appears in a training corpus aborts;
  * "score:"/"count:" (MCTS leak markers) in any prompt abort;
  * fewer than --min-records surviving records abort.

Usage
-----
    /home/joshu/venv-train/bin/python3 tools/training/wp3/build_unseen_gate_corpus.py \\
        --log /home/joshu/mz_unseen_HighNoonControl_vs_BGRoots.log \\
        --log /home/joshu/mz_unseen_BWBats_vs_HighNoonControl.log \\
        --out-dir tools/training/data
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src"
for p in (str(SRC), str(REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Order matters: build_magezero_bridge registers a minimal `arenamcp` package
# (no PySide6 cascade) and loads the production AUTOPILOT_SYSTEM_PROMPT;
# gate_play_decisions then finds arenamcp.action_planner already in
# sys.modules exactly as run_wp3_pipeline does.
from tools.training import build_magezero_bridge as BRIDGE  # noqa: E402
from tools.training import gate_play_decisions as G  # noqa: E402
from tools.training import parse_magezero_log as PARSER  # noqa: E402

STEM_DEFAULT = "gate_unseen_deck"
DEFAULT_OUT_DIR = REPO / "tools" / "training" / "data"

# Decks that HAVE appeared in a training corpus (smoke + gen curricula).
# A gate slice built from one of these is not "unseen" — hard abort.
TRAINING_DECKS = frozenset(
    {
        "UWTempo",
        "Standard-MonoR",
        "Standard-MonoG",
        "Standard-MonoB",
        "Standard-MonoW",
        "Standard-MonoU",
    }
)

# Entry text -> MTGA-style action type, mirroring how XMage phrases its
# priority menus (measured on wp3/decisions.jsonl: Play/Cast/Pass/ability
# text cover >99.7% of entries; "true"/"false" rows are binary decisions the
# builder excludes wholesale).
def classify_action_type(text: str) -> str:
    t = text.strip()
    if t == "Pass":
        return "ActionType_Pass"
    if t.startswith("Play "):
        return "ActionType_Play"
    if t.startswith("Cast "):
        return "ActionType_Cast"
    if ":" in t:
        # Ability grammar: "{T}: Draw a card...", "{W/P}, {T}: ...",
        # "Sacrifice {this}: ..." — cost, colon, effect.
        return "ActionType_Activate"
    return "ActionType_Other"


def entry_name(text: str, action_type: str) -> str | None:
    if action_type == "ActionType_Play":
        return text[len("Play ") :].strip() or None
    if action_type == "ActionType_Cast":
        return text[len("Cast ") :].strip() or None
    return None


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_sha() -> str:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, timeout=10
        )
        return r.stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def die(msg: str) -> None:
    print(f"FAIL-CLOSED: {msg}", file=sys.stderr)
    sys.exit(2)


def load_training_prompt_hashes(paths: list[Path]) -> tuple[set[str], list[str]]:
    """sha256 of every `user` prompt in the given training JSONL files."""
    hashes: set[str] = set()
    used: list[str] = []
    for p in paths:
        if not p.is_file():
            continue
        used.append(str(p))
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                u = rec.get("user")
                if u:
                    hashes.add(sha256_text(u))
    return hashes, used


def default_training_corpora() -> list[Path]:
    out: list[Path] = []
    data = REPO / "tools" / "training" / "data"
    for d in sorted(data.glob("wp3*")):
        for name in ("train.jsonl", "val.jsonl", "test.jsonl"):
            p = d / name
            if p.is_file():
                out.append(p)
    return out


def build_decision(row: dict, drops: Counter) -> dict | None:
    """MageZero decisions-JSONL row -> gate decision dict, or None (counted)."""
    kind = row.get("decision_kind", "priority")
    if kind != "priority":
        drops[f"dk_{kind}"] += 1
        return None
    menu_texts = [str(m) for m in row.get("menu", [])]
    if len(menu_texts) < 2:
        drops["menu_too_small"] += 1
        return None
    chosen = row.get("chosen", "")
    if not chosen:
        drops["chosen_empty"] += 1
        return None
    if chosen not in menu_texts:
        drops["chosen_not_in_menu"] += 1
        return None

    rows = []
    for i, text in enumerate(menu_texts):
        atype = classify_action_type(text)
        rows.append(
            {
                "index": i + 1,
                "text": text,
                # No grp_ids exist for MageZero data: the TEXT is the action
                # identity, which also implements the seen gate's text-equality
                # equivalence rule (identical lines are the same choice).
                "action_key": text,
                "action_type": atype,
                "grp_id": None,
                "instance_id": None,
                "name": entry_name(text, atype),
            }
        )
    gold_pick = menu_texts.index(chosen) + 1
    gold_row = rows[gold_pick - 1]
    equivalents = sorted(r["index"] for r in rows if r["text"] == gold_row["text"])

    return {
        "game_state": BRIDGE.build_game_state(row),
        "menu": rows,
        "gold_pick": gold_pick,
        "gold_action_key": gold_row["action_key"],
        "gold_action_type": gold_row["action_type"],
        "gold_equivalent_picks": equivalents,
        "menu_size": len(rows),
        "is_land_drop": gold_row["action_type"] == "ActionType_Play",
        "has_land_option": any(r["action_type"] == "ActionType_Play" for r in rows),
        "pass_pick": next((r["index"] for r in rows if r["action_type"] == "ActionType_Pass"), None),
        "first_land_pick": next((r["index"] for r in rows if r["action_type"] == "ActionType_Play"), None),
        "menu_key_spell": sorted({r["action_key"] for r in rows}),
        "n_meaningful_options": sum(1 for r in rows if r["action_type"] != "ActionType_Pass"),
        "trigger": G.TRIGGER,
        "request_type": "ActionsAvailable",
        "decision_kind": "priority_action",
        "phase": row.get("phase"),
        "step": "",
        # Bridge limitation carried over honestly: the reconstructed state
        # always makes the local seat active (build_magezero_bridge).
        "is_own_turn": True,
        "turn_number": row.get("turn"),
        "decision_uid": None,
        "format": "MageZero_unseen",
        "format_bucket": "constructed",
        # Permanent holdout by construction; evaluate() reads this field.
        "deck_seen_in_train": False,
        "outcome": row.get("outcome", "unknown"),
        "source": {
            "replay_file": row.get("_log") or row.get("game_id", "").split(":")[0],
            "game_id": row.get("game_id"),
            "session": row.get("session"),
            "turn": row.get("turn"),
        },
    }


def render_record(decision: dict, record_id: str, variant: str, twin_id: str) -> dict:
    menu_texts = [r["text"] for r in decision["menu"]]
    user = G.build_user_message(decision["game_state"], menu_texts, decision.get("trigger", G.TRIGGER))
    if user.count("Legal: (pick by number)") != 1:
        raise RuntimeError("production formatter degraded — refusing a non-production-shaped record")
    meta = {k: v for k, v in decision.items() if k != "game_state" and not k.startswith("_")}
    meta["split"] = "test"
    meta["variant"] = variant
    meta["twin_id"] = twin_id
    return {
        "id": record_id,
        "system": BRIDGE.AUTOPILOT_SYSTEM_PROMPT,
        "user": user,
        "max_tokens": 400,
        "temperature": 0.0,
        "meta": meta,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--log", action="append", dest="logs", required=True, type=Path)
    ap.add_argument(
        "--primary-deck",
        default=None,
        help="primary (MCTS) deck name; default per-log filename inference "
        "(_<Primary>_vs_<Opponent>.log). With multiple logs, prefer inference.",
    )
    ap.add_argument("--decks-dir", default=None, help="XMage deck dir (.dck lists)")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--stem", default=STEM_DEFAULT)
    ap.add_argument("--seed", type=int, default=20260731)
    ap.add_argument(
        "--include-land-drops",
        action="store_true",
        help="keep land-drop-gold decisions. Default EXCLUDED, matching the "
        "seen strategic gate corpus (exclude_land_drops: true in its manifest).",
    )
    ap.add_argument(
        "--training-corpus",
        action="append",
        type=Path,
        default=None,
        help="training JSONL(s) whose prompts must not appear here "
        "(default: every wp3*/train,val,test .jsonl under tools/training/data)",
    )
    ap.add_argument("--min-records", type=int, default=30)
    args = ap.parse_args(argv)

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    identity_path = out_dir / f"{args.stem}_test.jsonl"
    permuted_path = out_dir / f"{args.stem}_test_permuted.jsonl"
    manifest_path = out_dir / f"{args.stem}_manifest.json"

    if args.primary_deck and len(args.logs) > 1:
        die(
            "--primary-deck with multiple --log files would assert ONE deck for "
            "all of them; rely on per-log filename inference instead, or run "
            "one build per log"
        )

    # ---- parse (deck resolution + signature guardrail inside) ------------
    inputs: list[dict] = []
    all_rows: list[dict] = []
    for lp in args.logs:
        if not lp.is_file():
            die(f"log not found: {lp}")
        try:
            resolved = PARSER.resolve_primary_deck(
                lp, primary_deck=args.primary_deck, decks_dir=args.decks_dir
            )
        except PARSER.DeckSignatureError as e:
            die(str(e))
        if resolved is None:
            die(
                f"cannot resolve a primary deck for {lp.name} — an unseen-deck "
                "gate corpus with unknown deck attribution is meaningless. "
                "Name the log _<Primary>_vs_<Opponent>.log or pass --primary-deck."
            )
        deck_name, deck_cards = resolved
        if deck_name in TRAINING_DECKS:
            die(
                f"primary deck {deck_name!r} ({lp.name}) is a TRAINING deck — "
                "this slice must contain only permanent-holdout decks"
            )
        try:
            rows, sessions = PARSER.parse_log(
                str(lp), primary_deck=args.primary_deck, decks_dir=args.decks_dir
            )
        except PARSER.DeckSignatureError as e:
            die(str(e))
        checked = PARSER.LAST_PARSE_STATS.get("deck_signature_checked_rows", 0)
        print(
            f"[unseen-gate] {lp.name}: {len(rows)} decisions, primary={deck_name} "
            f"({len(deck_cards)} names), signature OK on {checked} rows"
        )
        inputs.append(
            {
                "log": str(lp),
                "log_sha256": sha256_file(lp),
                "primary_deck": deck_name,
                "deck_distinct_names": len(deck_cards),
                "decisions_parsed": len(rows),
                "deck_signature_checked_rows": checked,
                "sessions": len(sessions),
            }
        )
        all_rows.extend(rows)

    # ---- decisions -> gate decisions ------------------------------------
    drops: Counter = Counter()
    decisions: list[dict] = []
    for row in all_rows:
        d = build_decision(row, drops)
        if d is None:
            continue
        if d["is_land_drop"] and not args.include_land_drops:
            drops["land_drop_excluded"] += 1
            continue
        decisions.append(d)

    # ---- render + dedupe (seen-gate policy: identical prompt+gold collapse,
    # identical prompt+different gold drops the whole group) ---------------
    rendered: list[tuple[dict, dict]] = []  # (decision, identity record)
    seen_prompt_gold: dict[str, str] = {}
    ambiguous_prompts: set[str] = set()
    for i, d in enumerate(decisions):
        rec_id = f"uz-{Path(inputs[0]['log']).stem if len(inputs) == 1 else args.stem}-{i:05d}"
        rec = render_record(d, rec_id, "identity", rec_id)
        u = rec["user"]
        gold_text = d["menu"][d["gold_pick"] - 1]["text"]
        if u in seen_prompt_gold:
            if seen_prompt_gold[u] == gold_text:
                drops["duplicate_prompt_collapsed"] += 1
            else:
                # This row AND every previously-kept row with this prompt are
                # unanswerable — drop the whole group, counted per row below.
                ambiguous_prompts.add(u)
                drops["ambiguous_duplicate_prompt_dropped"] += 1
            continue
        seen_prompt_gold[u] = gold_text
        rendered.append((d, rec))
    if ambiguous_prompts:
        kept: list[tuple[dict, dict]] = []
        for d, rec in rendered:
            if rec["user"] in ambiguous_prompts:
                drops["ambiguous_duplicate_prompt_dropped"] += 1
            else:
                kept.append((d, rec))
        rendered = kept

    if len(rendered) < args.min_records:
        die(
            f"only {len(rendered)} records survived (< --min-records {args.min_records}); "
            f"drop ledger: {dict(drops)}"
        )

    # ---- leak checks -----------------------------------------------------
    corpora = args.training_corpus if args.training_corpus else default_training_corpora()
    train_hashes, train_files = load_training_prompt_hashes(corpora)
    leaks = 0
    for _, rec in rendered:
        for text in (rec["user"], rec["system"]):
            if "score:" in text or "count:" in text:
                die(f"MCTS leak marker in prompt {rec['id']}")
        if sha256_text(rec["user"]) in train_hashes:
            leaks += 1
    if leaks:
        die(
            f"{leaks} rendered prompts appear in training corpora ({train_files}) — "
            "either the decks are not holdouts or a corpus leaked"
        )

    # ---- permuted twins --------------------------------------------------
    identity_records: list[dict] = []
    permuted_records: list[dict] = []
    for d, rec in rendered:
        identity_records.append(rec)
        twin = G.permute_decision(d, seed_key=f"{args.seed}:{rec['id']}")
        # `permutation` rides along in meta via the twin dict itself.
        prec = render_record(twin, f"{rec['id']}#perm", "permuted", rec["id"])
        permuted_records.append(prec)

    # ---- write -----------------------------------------------------------
    def write_jsonl(path: Path, records: list[dict]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    write_jsonl(identity_path, identity_records)
    write_jsonl(permuted_path, permuted_records)

    metas = [r["meta"] for r in identity_records]
    sizes = sorted(m["menu_size"] for m in metas)
    n = len(metas)
    composition = {
        "n": n,
        "by_gold_action_type": dict(Counter(m["gold_action_type"] for m in metas)),
        "by_phase": dict(Counter(m.get("phase") or "unknown" for m in metas)),
        "by_outcome": dict(Counter(m.get("outcome", "unknown") for m in metas)),
        "pass_gold_share": round(
            sum(1 for m in metas if m["gold_action_type"] == "ActionType_Pass") / n, 4
        ),
        "land_gold_share": round(sum(1 for m in metas if m["is_land_drop"]) / n, 4),
        "menu_size": {
            "min": sizes[0],
            "median": sizes[len(sizes) // 2],
            "max": sizes[-1],
            "mean": round(sum(sizes) / n, 2),
        },
    }
    manifest = {
        "corpus": args.stem,
        "built_ts": time.time(),
        "git_sha": git_sha(),
        "seed": args.seed,
        "builder": "tools/training/wp3/build_unseen_gate_corpus.py",
        "inputs": inputs,
        "gold_label_provenance": "MCTS teacher pick (production search settings), NOT a human pick",
        "filters": {
            "decision_kind": "priority only",
            "exclude_land_drops": not args.include_land_drops,
            "duplicate_prompts": "identical prompt+gold collapsed; identical prompt+different gold dropped",
        },
        "drops": dict(drops),
        "composition": composition,
        "training_prompt_exclusion": {
            "files": train_files,
            "hashes": len(train_hashes),
            "overlaps": 0,
        },
        "honest_notes": [
            "prompts are name-only (no oracle text/grp_ids), matching the WP-3 "
            "training records rather than the replay-derived seen gate — the "
            "seen-vs-unseen comparison is provenance-annotated, not fidelity-matched",
            "is_own_turn is always True (bridge state-reconstruction limitation)",
            "deck_seen_in_train is False by construction (permanent holdout decks)",
        ],
        "files": {
            "identity": {"path": str(identity_path), "records": len(identity_records),
                         "sha256": sha256_file(identity_path)},
            "permuted": {"path": str(permuted_path), "records": len(permuted_records),
                         "sha256": sha256_file(permuted_path)},
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"[unseen-gate] wrote {len(identity_records)} identity + {len(permuted_records)} permuted records")
    print(f"[unseen-gate] composition: {json.dumps(composition['by_gold_action_type'])}")
    print(f"[unseen-gate] drops: {json.dumps(dict(drops))}")
    print(f"[unseen-gate] manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
