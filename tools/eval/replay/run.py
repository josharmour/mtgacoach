"""Run replay-based eval across multiple decision kinds.

Supported decision kinds (each gets its own prompt + parser):
  - ActionsAvailable  -> "what should I cast/play/activate?"  (numbered choice)
  - DeclareAttackers  -> "who should attack?"                  (set of attacker IDs)
  - DeclareBlockers   -> "who should block whom?"              (block assignment)
  - Mulligan          -> "keep or mull this hand?"             (KEEP/MULL)

ActionsAvailable can be filtered to high-signal decisions only
(`--high-signal-only`): your Main Phase, your priority, with at least one
Cast/Play in the legal set. This filters out instant-speed responses,
mid-cast targeting payments, etc., focusing on the "what to play this
turn" decision.

Output JSONL fields per record:
  replay, decision_index, msg_id, turn, phase, kind,
  ground_truth (action_type, grp_ids, instance_ids, summary),
  backend, model, response, error,
  match (bool, exact),
  jaccard (float, set-overlap; 1.0 if exact, else partial),
  latency_ms,
  + kind-specific fields (actions list, attacker/blocker sets, etc.)

Idempotent: skip (replay, msg_id, backend) triples already on disk.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Iterable

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tools.eval.run import BackendSpec  # noqa: E402

from .reader import parse_replay_path  # noqa: E402
from .decisions import extract_decisions, Decision  # noqa: E402
from .state import snapshot_at_decision, GameStateSnapshot  # noqa: E402
from .prompts import (  # noqa: E402
    SYSTEM_PROMPT,
    DA_SYSTEM_PROMPT,
    DB_SYSTEM_PROMPT,
    MULL_SYSTEM_PROMPT,
    ActionChoice,
    CreatureChoice,
    enumerate_actions,
    build_actions_available_prompt,
    build_actions_available_prompt_raw_json,
    build_declare_attackers_prompt,
    build_declare_blockers_prompt,
    build_mulligan_prompt,
    parse_coach_choice,
    parse_attack_set,
    parse_block_assignment,
    parse_mulligan_choice,
    matches_ground_truth,
    is_high_signal_actions_available,
    jaccard,
    _creatures_from_ids,
    _name_for_grpid,
)


def _list_replays(replays_dir: Path, max_replays: int | None) -> list[Path]:
    files = sorted(replays_dir.glob("*.rply"))
    if max_replays is not None:
        files = files[:max_replays]
    return files


def _existing_keys(out_path: Path) -> set[tuple[str, int, str]]:
    seen: set[tuple[str, int, str]] = set()
    if not out_path.exists():
        return seen
    with open(out_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
                seen.add((rec["replay"], int(rec["msg_id"]), rec["backend"]))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
    return seen


def _ask_backend(backend, system_prompt: str, user_text: str, max_tokens: int = 400) -> tuple[str, str | None, float]:
    """Returns (response, error, latency_ms)."""
    started = time.perf_counter()
    error = None
    response = ""
    try:
        client = backend.build()
        response = client.complete(
            system_prompt=system_prompt,
            user_message=user_text,
            max_tokens=max_tokens,
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    return response, error, (time.perf_counter() - started) * 1000


def _make_base_record(replay_path: Path, meta, d: Decision, snap: GameStateSnapshot) -> dict:
    return {
        "replay": replay_path.name,
        "decision_index": d.index,
        "msg_id": int(d.request.msg_id) if d.request.msg_id is not None else None,
        "turn": snap.turn_number,
        "phase": snap.phase,
        "active_player": snap.active_player,
        "priority_player": snap.priority_player,
        "kind": d.ground_truth.kind,
        "ground_truth": {
            "action_type": d.ground_truth.action_type,
            "grp_ids": list(d.ground_truth.grp_ids),
            "instance_ids": list(d.ground_truth.instance_ids),
            "summary": d.ground_truth.summary,
            "keep": d.ground_truth.keep,
        },
    }


# ---------------------------------------------------------------------------
# Per-kind handlers
# ---------------------------------------------------------------------------


def _handle_actions_available(
    snap: GameStateSnapshot, d: Decision, replay_path, meta, messages,
    backends, backend_specs: list[str], seen: set, high_signal_only: bool,
    out_f, log_prefix: str, prompt_variant: str = "default",
) -> int:
    actions = enumerate_actions(d.request)
    if len(actions) <= 1:
        return 0
    if high_signal_only and not is_high_signal_actions_available(snap, d.request, actions):
        return 0
    if prompt_variant == "raw_json":
        user_text = build_actions_available_prompt_raw_json(snap, d.request, actions)
        label_suffix = "#raw_json"
    else:
        user_text = build_actions_available_prompt(snap, d.request, actions)
        label_suffix = ""
    written = 0
    for spec, backend in zip(backend_specs, backends):
        label = backend.label + label_suffix
        key = (replay_path.name, int(d.request.msg_id), label)
        if key in seen:
            continue
        response, error, lat = _ask_backend(backend, SYSTEM_PROMPT, user_text)
        choice = parse_coach_choice(response, actions)
        matched = matches_ground_truth(choice, d.ground_truth)
        rec = _make_base_record(replay_path, meta, d, snap)
        rec.update({
            "actions": [{"number": a.number, "action_type": a.action_type,
                         "grp_id": a.grp_id, "instance_id": a.instance_id,
                         "label": a.label} for a in actions],
            "backend": label, "spec": spec,
            "model": getattr(backend, "model", spec),
            "prompt_variant": prompt_variant,
            "response": response, "error": error,
            "choice_number": choice.number if choice else None,
            "choice_action_type": choice.action_type if choice else None,
            "choice_grp_id": choice.grp_id if choice else None,
            "match": matched, "jaccard": 1.0 if matched else 0.0,
            "latency_ms": round(lat, 1),
        })
        out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out_f.flush()
        seen.add(key)
        flag = "OK" if matched else ("ERR" if error else "MISS")
        print(f"  {log_prefix} {label[:30]:30}  AA  chose#{choice.number if choice else '?':>3}  "
              f"truth={d.ground_truth.summary[:35]:35} {flag} {lat:.0f}ms")
        written += 1
    return written


def _handle_declare_attackers(
    snap: GameStateSnapshot, d: Decision, replay_path, meta, messages,
    backends, backend_specs: list[str], seen: set,
    out_f, log_prefix: str,
) -> int:
    da = d.request.payload.get("declareAttackersReq") or {}
    qualified_ids = sorted(int(a.get("attackerInstanceId")) for a in (da.get("qualifiedAttackers") or [])
                           if a.get("attackerInstanceId") is not None)
    if not qualified_ids:
        return 0
    qualified = _creatures_from_ids(snap, qualified_ids)
    truth_set = set(d.ground_truth.instance_ids)
    user_text = build_declare_attackers_prompt(snap, d.request, qualified)
    written = 0
    for spec, backend in zip(backend_specs, backends):
        label = backend.label
        key = (replay_path.name, int(d.request.msg_id), label)
        if key in seen:
            continue
        response, error, lat = _ask_backend(backend, DA_SYSTEM_PROMPT, user_text, max_tokens=300)
        coach_set = parse_attack_set(response, qualified)
        matched = (coach_set == truth_set) if coach_set is not None else False
        jacc = jaccard(coach_set or set(), truth_set) if coach_set is not None else 0.0
        rec = _make_base_record(replay_path, meta, d, snap)
        rec.update({
            "qualified_ids": qualified_ids,
            "qualified_names": [c.name for c in qualified],
            "backend": label, "spec": spec,
            "model": getattr(backend, "model", spec),
            "response": response, "error": error,
            "coach_attacker_ids": sorted(coach_set) if coach_set is not None else None,
            "match": matched, "jaccard": round(jacc, 3),
            "latency_ms": round(lat, 1),
        })
        out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out_f.flush()
        seen.add(key)
        flag = "OK" if matched else ("ERR" if error else f"J{jacc:.2f}")
        cs = sorted(coach_set) if coach_set is not None else "?"
        print(f"  {log_prefix} {label[:25]:25}  ATK  coach={cs} truth={sorted(truth_set)} "
              f"{flag} {lat:.0f}ms")
        written += 1
    return written


def _handle_declare_blockers(
    snap: GameStateSnapshot, d: Decision, replay_path, meta, messages,
    backends, backend_specs: list[str], seen: set,
    out_f, log_prefix: str,
) -> int:
    db = d.request.payload.get("declareBlockersReq") or {}
    blockers_proto = db.get("blockers") or []
    # Attacker IDs are derived from the union of attackerInstanceIds across
    # the blocker offerings — those are who's attacking us right now.
    attacker_id_set: set[int] = set()
    blocker_id_set: set[int] = set()
    for b in blockers_proto:
        bid = b.get("blockerInstanceId")
        if bid is not None:
            blocker_id_set.add(int(bid))
        for aid in (b.get("attackerInstanceIds") or []):
            attacker_id_set.add(int(aid))
    if not attacker_id_set:
        return 0
    blocker_ids = sorted(blocker_id_set)
    attacker_ids = sorted(attacker_id_set)
    blockers_choices = _creatures_from_ids(snap, blocker_ids)
    attackers_choices = _creatures_from_ids(snap, attacker_ids)
    # Ground truth: dict {blocker_iid: attacker_iid}
    truth_assignment: dict[int, int] = {}
    for b in blockers_proto:
        bid = b.get("blockerInstanceId")
        atks = b.get("attackerInstanceIds") or []
        if bid is None or not atks:
            continue
        # If multiple atks, take the first (rare to have one blocker assigned to multiple)
        truth_assignment[int(bid)] = int(atks[0])
    user_text = build_declare_blockers_prompt(snap, d.request, attackers_choices, blockers_choices)
    written = 0
    for spec, backend in zip(backend_specs, backends):
        label = backend.label
        key = (replay_path.name, int(d.request.msg_id), label)
        if key in seen:
            continue
        response, error, lat = _ask_backend(backend, DB_SYSTEM_PROMPT, user_text, max_tokens=300)
        coach_assignment = parse_block_assignment(response, blockers_choices, attackers_choices)
        coach_pairs = set(coach_assignment.items()) if coach_assignment is not None else set()
        truth_pairs = set(truth_assignment.items())
        matched = (coach_pairs == truth_pairs) if coach_assignment is not None else False
        jacc = jaccard(coach_pairs, truth_pairs) if coach_assignment is not None else 0.0
        rec = _make_base_record(replay_path, meta, d, snap)
        rec.update({
            "attacker_ids": attacker_ids,
            "blocker_ids": blocker_ids,
            "ground_truth_blocks": [{"blocker": k, "attacker": v} for k, v in truth_assignment.items()],
            "backend": label, "spec": spec,
            "model": getattr(backend, "model", spec),
            "response": response, "error": error,
            "coach_blocks": ([{"blocker": k, "attacker": v} for k, v in coach_assignment.items()]
                              if coach_assignment is not None else None),
            "match": matched, "jaccard": round(jacc, 3),
            "latency_ms": round(lat, 1),
        })
        out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out_f.flush()
        seen.add(key)
        flag = "OK" if matched else ("ERR" if error else f"J{jacc:.2f}")
        print(f"  {log_prefix} {label[:25]:25}  BLK  match={matched} jacc={jacc:.2f} {flag} {lat:.0f}ms")
        written += 1
    return written


def _handle_mulligan(
    snap: GameStateSnapshot, d: Decision, replay_path, meta, messages,
    backends, backend_specs: list[str], seen: set,
    out_f, log_prefix: str,
) -> int:
    # Hand cards: pull from the current zone-of-type-Hand for the local seat.
    seat = snap.local_seat_id
    hand_cards = snap.hand(seat)
    # For mulligan #N, the hand has 7-N cards (London) but the LOCAL seat sees
    # all 7 anyway in most replays since mulligan only happens at game start.
    mulligan_count = 0  # We don't track this without reading prompt parameters.
    # Best-effort: derive from the prompt parameters (NumberOfCards).
    prompt = d.request.payload.get("prompt") or {}
    for p in (prompt.get("parameters") or []):
        if p.get("parameterName") == "NumberOfCards":
            n = p.get("numberValue")
            if isinstance(n, int):
                mulligan_count = max(0, 7 - n)
            break
    user_text = build_mulligan_prompt(snap, d.request, hand_cards, mulligan_count)
    truth_keep = bool(d.ground_truth.keep)
    written = 0
    for spec, backend in zip(backend_specs, backends):
        label = backend.label
        key = (replay_path.name, int(d.request.msg_id), label)
        if key in seen:
            continue
        response, error, lat = _ask_backend(backend, MULL_SYSTEM_PROMPT, user_text, max_tokens=200)
        coach_keep = parse_mulligan_choice(response)
        matched = (coach_keep == truth_keep) if coach_keep is not None else False
        rec = _make_base_record(replay_path, meta, d, snap)
        rec.update({
            "mulligan_count": mulligan_count,
            "hand_grp_ids": [c.get("grpId") for c in hand_cards],
            "hand_names": [_name_for_grpid(c.get("grpId")) for c in hand_cards],
            "backend": label, "spec": spec,
            "model": getattr(backend, "model", spec),
            "response": response, "error": error,
            "coach_keep": coach_keep,
            "match": matched, "jaccard": 1.0 if matched else 0.0,
            "latency_ms": round(lat, 1),
        })
        out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out_f.flush()
        seen.add(key)
        flag = "OK" if matched else ("ERR" if error else "MISS")
        print(f"  {log_prefix} {label[:25]:25}  MUL  coach={coach_keep} truth={truth_keep} {flag} {lat:.0f}ms")
        written += 1
    return written


_HANDLERS = {
    "ActionsAvailable": _handle_actions_available,
    "DeclareAttackers": _handle_declare_attackers,
    "DeclareBlockers": _handle_declare_blockers,
    "Mulligan": _handle_mulligan,
}


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------


def run(
    replay_paths: Iterable[Path],
    out_path: Path,
    backend_specs: list[str],
    license_key: str = "",
    max_decisions_per_replay: int | None = None,
    seat: int = 2,
    kinds: set[str] | None = None,
    high_signal_only: bool = False,
    prompt_variant: str = "default",
) -> None:
    backends = [BackendSpec.parse(s, license_key=license_key) for s in backend_specs]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    seen = _existing_keys(out_path)
    print(f"loaded {len(seen)} existing records from {out_path}")
    kinds = kinds or set(_HANDLERS.keys())

    with open(out_path, "a", encoding="utf-8") as out_f:
        for replay_path in replay_paths:
            try:
                meta, messages = parse_replay_path(replay_path)
            except Exception as exc:
                print(f"skip {replay_path.name}: parse failed: {exc}", file=sys.stderr)
                continue
            decisions = extract_decisions(messages)
            scoreable = [d for d in decisions
                         if d.ground_truth and d.ground_truth.kind in kinds and d.request.msg_id is not None]
            if max_decisions_per_replay is not None:
                scoreable = scoreable[:max_decisions_per_replay]
            print(f"\n=== {replay_path.name} === local={meta.local_screen_name!r} "
                  f"opp={meta.opponent_screen_name!r} candidates={len(scoreable)}")

            for d in scoreable:
                handler = _HANDLERS.get(d.ground_truth.kind)
                if handler is None:
                    continue
                snap = snapshot_at_decision(messages, d.request, local_seat_id=seat)
                log_prefix = f"d{d.index:3d} T{snap.turn_number}"
                if d.ground_truth.kind == "ActionsAvailable":
                    handler(snap, d, replay_path, meta, messages, backends, backend_specs,
                            seen, high_signal_only, out_f, log_prefix,
                            prompt_variant=prompt_variant)
                else:
                    handler(snap, d, replay_path, meta, messages, backends, backend_specs,
                            seen, out_f, log_prefix)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--replays-dir", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--backend", action="append", required=True,
                   help="Backend spec (online:gpt-5.4, openai-compatible|url|model, ollama:qwen2.5:14b, ...)")
    p.add_argument("--license-key", default=os.environ.get("MTGACOACH_LICENSE_KEY", ""))
    p.add_argument("--max-replays", type=int, default=None)
    p.add_argument("--max-decisions-per-replay", type=int, default=None)
    p.add_argument("--seat", type=int, default=2)
    p.add_argument("--kinds", default="ActionsAvailable,DeclareAttackers,DeclareBlockers,Mulligan",
                   help="Comma-separated decision kinds to score")
    p.add_argument("--high-signal-only", action="store_true",
                   help="For ActionsAvailable, only score decisions on your Main Phase with priority and a card-action available")
    p.add_argument("--prompt-variant", choices=["default", "raw_json"], default="default",
                   help="ActionsAvailable prompt format: structured English (default) or raw JSON state. Backend label is suffixed with '#raw_json' so variants don't collide in the responses file.")
    args = p.parse_args()

    files = _list_replays(args.replays_dir, args.max_replays)
    print(f"replay corpus: {len(files)} file(s) from {args.replays_dir}")
    kinds = set(k.strip() for k in args.kinds.split(",") if k.strip())
    run(replay_paths=files, out_path=args.out, backend_specs=args.backend,
        license_key=args.license_key,
        max_decisions_per_replay=args.max_decisions_per_replay, seat=args.seat,
        kinds=kinds, high_signal_only=args.high_signal_only,
        prompt_variant=args.prompt_variant)


if __name__ == "__main__":
    main()
