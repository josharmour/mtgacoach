#!/usr/bin/env python3
"""Teacher-relabelling pipeline: label the replay strategic-groundtruth corpus
with DeepSeek-V4 ("dsv4") via the local LiteLLM gateway.

The "v4-from-dsv4" program. For every decision in
``tools/eval/data/replay_strategic_groundtruth.jsonl`` the record is rendered
into the PRODUCTION prompt shape (system = ``AUTOPILOT_SYSTEM_PROMPT``, user =
``ActionPlanner._build_action_prompt`` output) by reusing
``tools.training.gate_play_decisions`` — never a re-implementation (§17.2).
dsv4 is then asked to choose among the legal actions BY ACTION KEY (the
``Type:grpId`` strings of ``real_menu_key``) and give a concise rationale, as
strict JSON.

Output records are MODEL-AGNOSTIC: plain (system, user) prompt text plus the
teacher's pick/rationale keyed by action key. No chat-template formatting.

Fidelity notes (measured over the full 2,878-record corpus before this script
was written):
  * The rendered menu's simple keys are always a subset of ``real_menu_key``;
    the gold pick's key is always a member. Validation is therefore done
    against the RENDERED key set (stricter than, and contained in,
    ``real_menu_key``): 2,055 records carry extra ``real_menu_key`` entries
    for mana-level actions that production strips from the numbered menu, and
    offering those to the teacher would let it pick an action the student
    will never see.
  * 106 records (84 priority_action, 22 block_assignment) have one key
    mapping to multiple distinct menu texts (e.g. the same blocker in front
    of two different attackers). At key level those choices collapse; every
    text for the key is shown joined with " | ".

Resumable/idempotent: existing ids in --out are skipped; writes are
append-only and flushed per record.

Usage:
    python3 tools/training/relabel_dsv4.py --limit 10          # smoke
    python3 tools/training/relabel_dsv4.py                     # full run

Auth: reads LITELLM_MASTER_KEY from the environment or from
/home/joshu/docker-stack/litellm/.env at runtime. The key is never written to
output files.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for _p in (str(REPO), str(REPO / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import requests  # noqa: E402

DEFAULT_INPUT = REPO / "tools" / "eval" / "data" / "replay_strategic_groundtruth.jsonl"
DEFAULT_OUT = REPO / "tools" / "training" / "data" / "dsv4_labels_v1.jsonl"

ENDPOINT = "http://localhost:8444/v1/chat/completions"
MODEL = "dsv4"
ENV_FILE = Path("/home/joshu/docker-stack/litellm/.env")

CALL_TIMEOUT_S = 90
HTTP_RETRIES = 3  # attempts per call on transport/5xx/429 errors
MAX_TOKENS = 300
TEMPERATURE = 0.0
PROGRESS_EVERY = 25


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


def read_api_key() -> str:
    key = os.environ.get("LITELLM_MASTER_KEY", "").strip()
    if key:
        return key
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("LITELLM_MASTER_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError as e:
        raise SystemExit(f"cannot read {ENV_FILE}: {e}") from e
    raise SystemExit(f"LITELLM_MASTER_KEY not found in env or {ENV_FILE}")


# --------------------------------------------------------------------------
# Rendering (reuses gate_play_decisions; never re-implemented)
# --------------------------------------------------------------------------


def simple_key(action_type: object, grp_id: object) -> str:
    """The extractor's ``menu_key`` per-entry form: ``Type:grpId`` / bare type.

    Mirrors tools/eval/replay/menu_groundtruth.menu_key so picks validate
    against ``real_menu_key`` exactly.
    """
    short = str(action_type).replace("ActionType_", "")
    return f"{short}:{grp_id}" if grp_id is not None else short


RENDER_TIMEOUT_S = 20


class _RenderTimeout(Exception):
    pass


def _render_alarm(signum, frame):  # noqa: ARG001
    raise _RenderTimeout()


def build_job_guarded(rec: dict, facts, build_decision, build_user_message) -> dict:
    """build_job with a wall-clock guard (main thread only).

    Returns a drop record instead of hanging when the combat solver blows up.
    """
    import signal
    prev = signal.signal(signal.SIGALRM, _render_alarm)
    signal.setitimer(signal.ITIMER_REAL, RENDER_TIMEOUT_S)
    try:
        return build_job(rec, facts, build_decision, build_user_message)
    except _RenderTimeout:
        rid = f"{rec.get('decision_uid')}:{rec.get('replay_file')}"
        return {"record": {
            "id": rid, "source": "replay_strategic_groundtruth",
            "legal_actions": rec.get("real_menu_key") or [], "teacher_model": MODEL,
            "meta": {"turn_number": rec.get("turn_number"), "phase": rec.get("phase"),
                     "step": rec.get("step") or "", "is_own_turn": rec.get("is_own_turn"),
                     "decision_kind": rec.get("decision_kind")},
            "system": None, "user": None, "teacher_pick": None,
            "teacher_rationale": None, "gold_pick": None, "agrees_with_gold": None,
            "error": f"render_timeout:{RENDER_TIMEOUT_S}s"}}
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prev)


def build_job(rec: dict, facts, build_decision, build_user_message) -> dict:
    """Render one groundtruth record into a callable job (no LLM contact)."""
    rid = f"{rec.get('decision_uid')}:{rec.get('replay_file')}"
    base = {
        "id": rid,
        "source": "replay_strategic_groundtruth",
        "legal_actions": rec.get("real_menu_key") or [],
        "teacher_model": MODEL,
        "meta": {
            "turn_number": rec.get("turn_number"),
            "phase": rec.get("phase"),
            "step": rec.get("step") or "",
            "is_own_turn": rec.get("is_own_turn"),
            "decision_kind": rec.get("decision_kind"),
        },
    }
    decision = build_decision(rec, facts)
    if decision is None or decision.get("drop_reason"):
        reason = decision.get("drop_reason") if decision else "build_decision_none"
        return {"record": {**base, "system": None, "user": None, "teacher_pick": None,
                           "teacher_rationale": None, "gold_pick": None,
                           "agrees_with_gold": None, "error": f"build_drop:{reason}"}}

    user = build_user_message(
        decision["game_state"],
        [r["text"] for r in decision["menu"]],
        decision.get("trigger", "new_turn"),
    )

    # key -> distinct menu texts, in menu order. Duplicate texts under one key
    # (three Plains) collapse; distinct texts under one key are joined so no
    # information is silently dropped.
    key_texts: dict[str, list[str]] = {}
    menu_keys: list[str] = []
    for r in decision["menu"]:
        k = simple_key(r["action_type"], r["grp_id"])
        menu_keys.append(k)
        texts = key_texts.setdefault(k, [])
        if r["text"] not in texts:
            texts.append(r["text"])

    gold_row = decision["menu"][decision["gold_pick"] - 1]
    gold_key = simple_key(gold_row["action_type"], gold_row["grp_id"])

    base["system"] = _SYSTEM_PROMPT
    base["user"] = user
    base["gold_pick"] = gold_key
    base["meta"]["menu_keys"] = menu_keys  # aligns key i with numbered menu row i+1
    return {
        "record": base,
        "user": user,
        "key_texts": key_texts,
        "gold_key": gold_key,
    }


def call_suffix(key_texts: dict[str, list[str]]) -> str:
    lines = [
        "",
        "---",
        "RELABELLING TASK — this OVERRIDES the answer-format instructions above:",
        "Choose the single best action. Respond with STRICT JSON only — no prose,",
        "no markdown fences, nothing outside the JSON object:",
        '{"pick": "<action_key>", "rationale": "<1-2 sentences>"}',
        '"pick" MUST be copied verbatim from these action_key values:',
    ]
    for k, texts in key_texts.items():
        lines.append(f'  "{k}" = {" | ".join(texts)}')
    return "\n".join(lines)


# --------------------------------------------------------------------------
# LLM call + response parsing
# --------------------------------------------------------------------------

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def http_call(session: requests.Session, api_key: str, system: str, messages_user: list[dict]) -> str:
    """One chat completion with transport-level retries. Returns content text."""
    payload = {
        "model": MODEL,
        "messages": [{"role": "system", "content": system}, *messages_user],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        # extra_body equivalent for a raw HTTP client: LiteLLM forwards this
        # top-level param to vLLM, disabling the thought channel.
        "chat_template_kwargs": {"thinking": False},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last_err: Exception | None = None
    for attempt in range(HTTP_RETRIES):
        try:
            resp = session.post(ENDPOINT, json=payload, headers=headers, timeout=CALL_TIMEOUT_S)
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"] or ""
            if resp.status_code in (429,) or resp.status_code >= 500:
                last_err = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            else:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        except (requests.RequestException, KeyError, ValueError) as e:
            last_err = e
        time.sleep(2 ** (attempt + 1))
    raise RuntimeError(f"call failed after {HTTP_RETRIES} attempts: {last_err}")


def parse_pick(raw: str, valid_keys: set[str]) -> tuple[str | None, str | None, str]:
    """Return (pick, rationale, error). error == "" on success."""
    text = raw.strip()
    m = _JSON_RE.search(text)
    if not m:
        return None, None, "no JSON object found in response"
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        return None, None, f"invalid JSON: {e}"
    if not isinstance(obj, dict):
        return None, None, "JSON is not an object"
    pick = obj.get("pick")
    rationale = obj.get("rationale")
    if not isinstance(pick, str) or pick not in valid_keys:
        return None, str(rationale) if rationale is not None else None, f"pick {pick!r} not in legal action keys"
    return pick, str(rationale) if rationale is not None else "", ""


def label_one(session: requests.Session, api_key: str, job: dict) -> dict:
    """Run the teacher call (with one error-correcting retry) for a job."""
    record = job["record"]
    if "user" not in job:  # build-time drop, already a failure record
        return record
    key_texts = job["key_texts"]
    valid = set(key_texts)
    suffix = call_suffix(key_texts)
    user_msgs = [{"role": "user", "content": job["user"] + suffix}]

    try:
        raw = http_call(session, api_key, record["system"], user_msgs)
    except RuntimeError as e:
        return {**record, "teacher_pick": None, "teacher_rationale": None,
                "agrees_with_gold": None, "error": f"http:{e}"}

    pick, rationale, err = parse_pick(raw, valid)
    if err:
        # One error-correcting retry with the model's own reply in context.
        reminder = (
            f"Your previous reply was invalid: {err}. Reply again with STRICT JSON "
            'only: {"pick": "<action_key>", "rationale": "<1-2 sentences>"}. '
            f"pick must be exactly one of: {sorted(valid)}"
        )
        retry_msgs = user_msgs + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": reminder},
        ]
        try:
            raw2 = http_call(session, api_key, record["system"], retry_msgs)
        except RuntimeError as e:
            return {**record, "teacher_pick": None, "teacher_rationale": None,
                    "agrees_with_gold": None, "error": f"http_on_retry:{e}",
                    "raw_response": raw[:2000]}
        pick, rationale, err = parse_pick(raw2, valid)
        if err:
            return {**record, "teacher_pick": None, "teacher_rationale": None,
                    "agrees_with_gold": None, "error": f"invalid_after_retry:{err}",
                    "raw_response": raw2[:2000]}
        raw = raw2

    return {**record, "teacher_pick": pick, "teacher_rationale": rationale,
            "agrees_with_gold": pick == job["gold_key"]}


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


_SYSTEM_PROMPT = ""  # set in main() after imports


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--limit", type=int, default=None, help="label at most N new records (smoke tests)")
    args = ap.parse_args(argv)

    api_key = read_api_key()

    # Quiet arenamcp's rich INFO logging before the heavy imports fire it up.
    logging.basicConfig(level=logging.WARNING)
    logging.disable(logging.INFO)

    with contextlib.redirect_stdout(io.StringIO()):
        from arenamcp.action_planner import AUTOPILOT_SYSTEM_PROMPT
        from tools.training.gate_play_decisions import _CardFacts, build_decision, build_user_message

    global _SYSTEM_PROMPT
    _SYSTEM_PROMPT = AUTOPILOT_SYSTEM_PROMPT

    done_ids: set[str] = set()
    if args.out.exists():
        with open(args.out, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rid = json.loads(line).get("id")
                except json.JSONDecodeError:
                    continue
                if rid:
                    done_ids.add(rid)
    print(f"[start] resuming with {len(done_ids)} ids already in {args.out}", flush=True)

    facts = _CardFacts()

    # Records are rendered lazily and streamed to the workers. Rendering runs
    # the production prompt builder, which runs the combat solver — and on a
    # small number of wide late-game boards that search blows up
    # combinatorially (observed 2026-08-03: one record held the whole run for
    # >5 min with no output, because the original code rendered all 2878
    # records up front before the first API call). Two guards:
    #   * RENDER_TIMEOUT_S alarm per record — a blowup is dropped, not fatal.
    #     SIGALRM only fires on the main thread, so rendering stays here and
    #     only the HTTP calls are farmed out.
    #   * streaming submit — labelling starts on record one and progress is
    #     visible immediately.
    pending = [json.loads(line) for line in open(args.input, encoding="utf-8")
               if line.strip()]
    total_input = len(pending)
    todo = [r for r in pending
            if f"{r.get('decision_uid')}:{r.get('replay_file')}" not in done_ids]
    if args.limit is not None:
        todo = todo[:args.limit]
    n_jobs = len(todo)
    print(f"[start] {total_input} input records read, {n_jobs} to label "
          f"(concurrency={args.concurrency}, render_timeout={RENDER_TIMEOUT_S}s)",
          flush=True)
    if not todo:
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    write_lock = threading.Lock()
    stats = {"done": 0, "valid": 0, "invalid": 0, "agree": 0}
    t0 = time.time()

    session = requests.Session()
    with open(args.out, "a", encoding="utf-8") as out_f:
        def emit(result: dict) -> None:
            with write_lock:
                out_f.write(json.dumps(result, ensure_ascii=False) + "\n")
                out_f.flush()
                stats["done"] += 1
                if result.get("teacher_pick") is not None:
                    stats["valid"] += 1
                    if result.get("agrees_with_gold"):
                        stats["agree"] += 1
                else:
                    stats["invalid"] += 1
                if stats["done"] % PROGRESS_EVERY == 0 or stats["done"] == n_jobs:
                    rate = stats["done"] / max(time.time() - t0, 1e-9)
                    agree_pct = 100.0 * stats["agree"] / max(stats["valid"], 1)
                    print(f"[progress] done={stats['done']}/{n_jobs} "
                          f"valid={stats['valid']} invalid={stats['invalid']} "
                          f"gold-agreement={agree_pct:.1f}% "
                          f"rate={rate:.2f}/s eta={((n_jobs-stats['done'])/max(rate,1e-9))/60:.0f}min",
                          flush=True)

        # Render on this thread (SIGALRM guard needs it), submit as we go, and
        # keep at most a few batches in flight so memory stays flat.
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            inflight: set = set()
            max_inflight = max(args.concurrency * 3, 6)
            n_render_timeout = 0
            for rec in todo:
                job = build_job_guarded(rec, facts, build_decision, build_user_message)
                r = job.get("record") or {}
                if r.get("error", "").startswith("render_timeout"):
                    n_render_timeout += 1
                    emit(r)
                    continue
                if r.get("user") is None and r.get("error"):
                    emit(r)
                    continue
                inflight.add(pool.submit(label_one, session, api_key, job))
                while len(inflight) >= max_inflight:
                    done_now, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                    for fut in done_now:
                        try:
                            emit(fut.result())
                        except Exception as e:  # noqa: BLE001
                            print(f"[error] worker crashed: {e}", flush=True)
            for fut in as_completed(inflight):
                try:
                    emit(fut.result())
                except Exception as e:  # noqa: BLE001 — never lose the run to one record
                    print(f"[error] worker crashed: {e}", flush=True)
            if n_render_timeout:
                print(f"[render] {n_render_timeout} records dropped on the "
                      f"{RENDER_TIMEOUT_S}s combat-solver guard", flush=True)

    agree_pct = 100.0 * stats["agree"] / max(stats["valid"], 1)
    print(f"[done] {stats['done']} labelled: {stats['valid']} valid, "
          f"{stats['invalid']} invalid, gold-agreement {agree_pct:.1f}% "
          f"in {(time.time() - t0)/60:.1f} min", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
