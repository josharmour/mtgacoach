"""Regression tests for the fail-closed play-decision gate.

Covers the properties the gate's verdict depends on: pick extraction, menu
legality, permutation twins, the seen/unseen and format slices, threshold
clamping (no bypass), and the fail-closed CLI contract.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from tools.training import gate_play_decisions as gpd  # noqa: E402

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _menu(*specs) -> list[dict]:
    """specs: (action_type, grp_id, instance_id) tuples."""
    rows = []
    for i, (atype, grp, inst) in enumerate(specs):
        entry = {
            "action_type": atype,
            "grp_id": grp,
            "instance_id": inst,
            "ability_grp_id": None,
            "name": f"card{grp}",
        }
        rows.append(
            {
                "index": i + 1,
                "text": gpd.menu_line(entry, False),
                "action_key": gpd.entry_action_key(entry),
                "action_type": atype,
                "grp_id": grp,
                "instance_id": inst,
                "name": f"card{grp}",
            }
        )
    return rows


def _record(rid: str, menu: list[dict], gold: int, *, bucket="constructed", land=False, seen=False) -> dict:
    gold_key = menu[gold - 1]["action_key"]
    return {
        "id": rid,
        "system": "SYS",
        "user": "USER",
        "meta": {
            "menu": menu,
            "menu_size": len(menu),
            "gold_pick": gold,
            "gold_action_key": gold_key,
            "gold_action_type": menu[gold - 1]["action_type"],
            "gold_equivalent_picks": [r["index"] for r in menu if r["action_key"] == gold_key],
            "is_land_drop": land,
            "has_land_option": any(r["action_type"] == "ActionType_Play" for r in menu),
            "first_land_pick": next(
                (r["index"] for r in menu if r["action_type"] == "ActionType_Play"), None
            ),
            "pass_pick": next((r["index"] for r in menu if r["action_type"] == "ActionType_Pass"), None),
            "format": "SuperFormat_Constructed/GameVariant_Normal",
            "format_bucket": bucket,
            "deck_seen_in_train": seen,
            "menu_key_seen_in_train": False,
            "deck_max_train_jaccard": 0.0,
            "menu_cards_seen_in_train_frac": 0.0,
            "twin_id": rid,
            "variant": "identity",
            "split": "test",
            "source": {"replay_file": "r.rply", "decision_index": 0},
        },
    }


def _responses(path: Path, corpus: list[dict], backend: str, pick_fn) -> Path:
    rows = []
    for rec in corpus:
        pick = pick_fn(rec["meta"])
        response = (
            pick
            if isinstance(pick, str)
            else json.dumps({"actions": [{"pick": int(pick), "reasoning": "t"}]})
        )
        rows.append({"prompt_id": rec["id"], "backend": backend, "response": response, "error": None})
    gpd._write_jsonl(path, rows)
    return path


# ---------------------------------------------------------------------------
# constants / drift
# ---------------------------------------------------------------------------


def test_mana_action_types_match_groundtruth_extractor():
    """The local copy must not drift from the extractor's definition."""
    from tools.eval.replay.menu_groundtruth import MANA_LEVEL_ACTION_TYPES as SOURCE

    assert gpd.MANA_LEVEL_ACTION_TYPES == SOURCE


def test_menu_lines_use_production_wording():
    """Menu text must match what gamestate_decisions emits for legal actions."""
    cast = {"action_type": "ActionType_Cast", "grp_id": 1, "instance_id": 1, "name": "Shock"}
    assert gpd.menu_line(cast, True) == "Cast Shock [OK]"
    assert gpd.menu_line(cast, False) == "Cast Shock"
    assert gpd.menu_line({"action_type": "ActionType_Play", "name": "Plains"}, False) == "Play Land: Plains"
    assert gpd.menu_line({"action_type": "ActionType_Pass"}, False) == "Pass"
    assert (
        gpd.menu_line({"action_type": "ActionType_Activate", "name": "Ba Sing Se"}, False)
        == "Activate Ability: Ba Sing Se"
    )


# ---------------------------------------------------------------------------
# pick extraction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "response,expected",
    [
        ('{"actions": [{"pick": 3}]}', 3),
        ('```json\n{"actions": [{"pick": 7, "reasoning": "x"}]}\n```', 7),
        ('{"actions": [{"pick": 2,}],}', 2),  # trailing commas
        ('{"actions": [{"pick": "4"}]}', 4),  # string digit — production's regex accepts it
        ('prose then {"pick": 5} somewhere', 5),  # regex fallback, same as production
        ("I would play the land.", None),
        ('{"actions": [{"action_type": "pass_priority"}]}', None),
        ('{"actions": [{"pick": true}]}', None),  # bool is not a pick
        ("", None),
    ],
)
def test_extract_pick(response, expected):
    assert gpd.extract_pick(response) == expected


# ---------------------------------------------------------------------------
# menu identity / equivalence
# ---------------------------------------------------------------------------


def test_duplicate_lands_are_one_equivalence_class():
    menu = _menu(
        ("ActionType_Cast", 111, 1),
        ("ActionType_Play", 222, 2),
        ("ActionType_Play", 222, 3),
        ("ActionType_Pass", None, None),
    )
    rec = _record("d1", menu, gold=2, land=True)
    # The human clicked the first Plains; picking the second is the same play.
    assert rec["meta"]["gold_equivalent_picks"] == [2, 3]


def test_different_cards_are_not_equivalent():
    menu = _menu(("ActionType_Play", 222, 2), ("ActionType_Play", 333, 3))
    rec = _record("d2", menu, gold=1, land=True)
    assert rec["meta"]["gold_equivalent_picks"] == [1]


def test_ability_activations_key_on_ability_id():
    a = {"action_type": "ActionType_Activate", "grp_id": 9, "instance_id": 1, "ability_grp_id": 100}
    b = {"action_type": "ActionType_Activate", "grp_id": 9, "instance_id": 1, "ability_grp_id": 200}
    assert gpd.entry_action_key(a) != gpd.entry_action_key(b)


# ---------------------------------------------------------------------------
# permutation twin
# ---------------------------------------------------------------------------


def test_permutation_moves_gold_and_preserves_the_action():
    menu = _menu(
        ("ActionType_Cast", 1, 10),
        ("ActionType_Cast", 2, 11),
        ("ActionType_Play", 3, 12),
        ("ActionType_Pass", None, None),
    )
    decision = {
        "menu": menu,
        "gold_pick": 3,
        "gold_action_key": menu[2]["action_key"],
        "gold_equivalent_picks": [3],
    }
    twin = gpd.permute_decision(decision, "seed:1")
    assert twin["gold_pick"] != decision["gold_pick"], "a twin that leaves gold in place tests nothing"
    assert twin["menu"][twin["gold_pick"] - 1]["action_key"] == decision["gold_action_key"]
    assert sorted(r["action_key"] for r in twin["menu"]) == sorted(r["action_key"] for r in menu)
    assert [r["index"] for r in twin["menu"]] == [1, 2, 3, 4]


def test_permutation_is_deterministic():
    menu = _menu(*[("ActionType_Cast", i, i) for i in range(1, 7)])
    decision = {
        "menu": menu,
        "gold_pick": 2,
        "gold_action_key": menu[1]["action_key"],
        "gold_equivalent_picks": [2],
    }
    a = gpd.permute_decision(decision, "k")
    b = gpd.permute_decision(decision, "k")
    assert a["permutation"] == b["permutation"]


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------


def test_wilson_ci_bounds():
    assert gpd.wilson_ci(0, 0) is None
    lo, hi = gpd.wilson_ci(50, 100)
    assert 0.39 < lo < 0.41 and 0.59 < hi < 0.61
    lo, hi = gpd.wilson_ci(10, 10)
    assert hi == 1.0 and lo < 1.0  # never a zero-width interval at the boundary
    for k, n in ((0, 10), (3, 7), (17, 17)):
        lo, hi = gpd.wilson_ci(k, n)
        assert 0.0 <= lo <= k / n <= hi <= 1.0


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------


def _small_corpus() -> list[dict]:
    corpus = []
    for i in range(6):
        menu = _menu(
            ("ActionType_Cast", 100 + i, 1),
            ("ActionType_Play", 200, 2),
            ("ActionType_Pass", None, None),
        )
        corpus.append(
            _record(
                f"d{i}",
                menu,
                gold=2 if i % 2 == 0 else 1,
                bucket="brawl" if i < 3 else "limited",
                land=(i % 2 == 0),
                seen=(i == 0),
            )
        )
    return corpus


def test_evaluate_scores_accuracy_legality_and_slices(tmp_path):
    corpus = _small_corpus()
    resp = _responses(tmp_path / "r.jsonl", corpus, "b", lambda m: m["gold_pick"])
    metrics = gpd.evaluate(corpus, resp, "b")

    assert metrics["responses_matched"] == 6
    assert metrics["missing_responses"] == 0
    assert metrics["schema_valid_rate"] == 1.0
    assert metrics["legality_violations"] == 0
    assert metrics["overall"]["accuracy"] == 1.0
    assert metrics["by_format_bucket"]["brawl"]["n"] == 3
    assert metrics["by_format_bucket"]["limited"]["n"] == 3
    assert metrics["non_brawl"]["n"] == 3
    assert metrics["land_drop"]["n"] == 3
    assert metrics["strategic_non_land_drop"]["n"] == 3
    assert metrics["deck_seen_in_train"]["n"] == 1
    assert metrics["deck_unseen_in_train"]["n"] == 5


def test_evaluate_flags_out_of_range_picks(tmp_path):
    corpus = _small_corpus()
    resp = _responses(tmp_path / "r.jsonl", corpus, "b", lambda m: m["menu_size"] + 1)
    metrics = gpd.evaluate(corpus, resp, "b")
    assert metrics["legality_violations"] == 6
    assert metrics["overall"]["accuracy"] == 0.0
    assert metrics["legality_violation_examples"][0]["pick"] == 4


def test_evaluate_counts_unparseable_as_schema_failure(tmp_path):
    corpus = _small_corpus()
    resp = _responses(tmp_path / "r.jsonl", corpus, "b", lambda m: "just play the land")
    metrics = gpd.evaluate(corpus, resp, "b")
    assert metrics["schema_valid_rate"] == 0.0
    assert metrics["pick_extraction_rate"] == 0.0
    # An unparseable response is NOT a legality violation — it is a G1 failure.
    assert metrics["legality_violations"] == 0


def test_evaluate_credits_an_equivalent_duplicate(tmp_path):
    menu = _menu(("ActionType_Play", 5, 1), ("ActionType_Play", 5, 2), ("ActionType_Pass", None, None))
    corpus = [_record("dup", menu, gold=1, land=True)]
    resp = _responses(tmp_path / "r.jsonl", corpus, "b", lambda m: 2)
    metrics = gpd.evaluate(corpus, resp, "b")
    assert metrics["overall"]["accuracy"] == 1.0
    assert metrics["overall"]["accuracy_exact_index"] == 0.0


def test_evaluate_ignores_other_backends_and_dedupes(tmp_path):
    corpus = _small_corpus()
    path = tmp_path / "r.jsonl"
    rows = []
    for rec in corpus:
        pick = rec["meta"]["gold_pick"]
        rows.append(
            {"prompt_id": rec["id"], "backend": "b", "response": json.dumps({"actions": [{"pick": pick}]})}
        )
        rows.append(
            {"prompt_id": rec["id"], "backend": "other", "response": json.dumps({"actions": [{"pick": 99}]})}
        )
    rows.append(
        {"prompt_id": corpus[0]["id"], "backend": "b", "response": json.dumps({"actions": [{"pick": 99}]})}
    )
    gpd._write_jsonl(path, rows)
    metrics = gpd.evaluate(corpus, path, "b")
    assert metrics["responses_matched"] == 6
    assert metrics["duplicate_response_rows"] == 1
    assert metrics["legality_violations"] == 0


# ---------------------------------------------------------------------------
# permutation metrics + reference policies
# ---------------------------------------------------------------------------


def test_permutation_metrics_detect_index_memorisation(tmp_path):
    corpus = _small_corpus()
    permuted = []
    for rec in corpus:
        decision = dict(rec["meta"])
        twin = gpd.permute_decision(decision, f"s:{rec['id']}")
        twin_rec = {"id": rec["id"] + "#perm", "system": "S", "user": "U", "meta": dict(twin)}
        twin_rec["meta"]["twin_id"] = rec["id"]
        permuted.append(twin_rec)

    identity = gpd.evaluate(
        corpus, _responses(tmp_path / "i.jsonl", corpus, "b", lambda m: m["gold_pick"]), "b"
    )
    # A memoriser answers with the index that was right in the identity corpus.
    identity_gold = {r["id"]: r["meta"]["gold_pick"] for r in corpus}
    memo = gpd.evaluate(
        permuted,
        _responses(tmp_path / "p.jsonl", permuted, "b", lambda m, g=identity_gold: g[m["twin_id"]]),
        "b",
    )
    metrics = gpd.permutation_metrics(identity, memo, permuted)
    assert metrics["paired_n"] == 6
    assert metrics["identity_accuracy"] == 1.0
    assert metrics["abs_accuracy_gap"] > gpd.MAX_PERMUTATION_ACC_GAP


def test_reference_policies_expose_the_land_drop_floor():
    corpus = _small_corpus()
    refs = gpd.reference_policies(corpus)
    comp = gpd.corpus_composition(corpus)
    assert refs["always_land_else_first"] == pytest.approx(0.5)
    assert comp["trivial_land_drop_share"] == pytest.approx(0.5)
    assert comp["strategic_n"] == 3
    assert 0.0 < refs["uniform_random_expectation"] < 1.0


# ---------------------------------------------------------------------------
# fail-closed CLI contract
# ---------------------------------------------------------------------------


def _big_corpus(n=120) -> list[dict]:
    corpus = []
    for i in range(n):
        menu = _menu(
            ("ActionType_Cast", 100 + i, 1),
            ("ActionType_Play", 200, 2),
            ("ActionType_Cast", 300 + i, 3),
            ("ActionType_Pass", None, None),
        )
        corpus.append(
            _record(
                f"d{i:03d}",
                menu,
                gold=2 if i % 3 else 1,
                bucket=("brawl" if i % 2 else "limited"),
                land=bool(i % 3),
            )
        )
    return corpus


def _write_gate_inputs(tmp_path: Path, cand_pick, base_pick):
    corpus = _big_corpus()
    permuted = []
    for rec in corpus:
        twin = gpd.permute_decision(dict(rec["meta"]), f"s:{rec['id']}")
        twin_rec = {"id": rec["id"] + "#perm", "system": "S", "user": "U", "meta": dict(twin)}
        twin_rec["meta"]["twin_id"] = rec["id"]
        permuted.append(twin_rec)
    paths = {
        "corpus": tmp_path / "corpus.jsonl",
        "permuted_corpus": tmp_path / "corpus_perm.jsonl",
        "responses": tmp_path / "cand.jsonl",
        "permuted_responses": tmp_path / "cand_perm.jsonl",
        "baseline_responses": tmp_path / "base.jsonl",
        "report": tmp_path / "report.json",
    }
    gpd._write_jsonl(paths["corpus"], corpus)
    gpd._write_jsonl(paths["permuted_corpus"], permuted)
    _responses(paths["responses"], corpus, "cand", cand_pick)
    _responses(paths["permuted_responses"], permuted, "cand", cand_pick)
    _responses(paths["baseline_responses"], corpus, "base", base_pick)
    return paths


def _gate_argv(paths: dict) -> list[str]:
    return [
        "gate",
        "--corpus",
        str(paths["corpus"]),
        "--permuted-corpus",
        str(paths["permuted_corpus"]),
        "--responses",
        str(paths["responses"]),
        "--permuted-responses",
        str(paths["permuted_responses"]),
        "--baseline-responses",
        str(paths["baseline_responses"]),
        "--backend-label",
        "cand",
        "--baseline-label",
        "base",
        "--report",
        str(paths["report"]),
        "--bootstraps",
        "300",
    ]


def test_gate_passes_a_perfect_candidate(tmp_path):
    paths = _write_gate_inputs(tmp_path, lambda m: m["gold_pick"], lambda m: m["first_land_pick"] or 1)
    assert gpd.main(_gate_argv(paths)) == 0
    report = json.loads(paths["report"].read_text())
    assert report["verdict"] == "PASS"
    assert report["failures"] == []
    assert report["candidate"]["legality_violations"] == 0
    assert report["permutation_invariance"]["abs_accuracy_gap"] == 0.0
    assert report["paired_bootstrap"]["raw_accuracy_delta_95ci"][0] > 0


def test_gate_blocks_on_a_single_illegal_pick(tmp_path):
    """G2 is absolute: one out-of-range pick blocks, however good the rest is."""

    def cand(meta):
        return (
            meta["menu_size"] + 1
            if meta["gold_pick"] == 1 and meta["format_bucket"] == "limited"
            else meta["gold_pick"]
        )

    paths = _write_gate_inputs(tmp_path, cand, lambda m: m["gold_pick"])
    assert gpd.main(_gate_argv(paths)) == 2
    report = json.loads(paths["report"].read_text())
    assert report["verdict"] == "BLOCKED"
    assert any("G2 legality" in f for f in report["failures"])


def test_gate_blocks_on_missing_input(tmp_path):
    paths = _write_gate_inputs(tmp_path, lambda m: m["gold_pick"], lambda m: m["gold_pick"])
    paths["permuted_responses"].unlink()
    assert gpd.main(_gate_argv(paths)) == 2
    report = json.loads(paths["report"].read_text())
    assert report["verdict"] == "BLOCKED"
    assert any("missing required input" in f for f in report["failures"])


def test_gate_blocks_on_partial_coverage(tmp_path):
    paths = _write_gate_inputs(tmp_path, lambda m: m["gold_pick"], lambda m: m["gold_pick"])
    rows = list(gpd._read_jsonl(paths["responses"]))[:50]
    gpd._write_jsonl(paths["responses"], rows)
    assert gpd.main(_gate_argv(paths)) == 2
    report = json.loads(paths["report"].read_text())
    assert any("fail closed (no partial gate)" in f for f in report["failures"])


def test_gate_blocks_on_backend_error_rows(tmp_path):
    paths = _write_gate_inputs(tmp_path, lambda m: m["gold_pick"], lambda m: m["gold_pick"])
    rows = list(gpd._read_jsonl(paths["responses"]))
    rows[0]["error"] = "HTTP 401"
    rows[0]["response"] = ""
    gpd._write_jsonl(paths["responses"], rows)
    assert gpd.main(_gate_argv(paths)) == 2
    report = json.loads(paths["report"].read_text())
    assert any("backend errors" in f for f in report["failures"])


def test_gate_thresholds_cannot_be_loosened(tmp_path):
    """No bypass flag: a loosening CLI value is ignored and recorded."""
    paths = _write_gate_inputs(tmp_path, lambda m: m["menu_size"] + 1, lambda m: m["gold_pick"])
    argv = _gate_argv(paths) + [
        "--min-samples",
        "1",
        "--min-schema-rate",
        "0.0",
        "--max-permutation-gap",
        "1.0",
        "--max-accuracy-regression",
        "1.0",
    ]
    assert gpd.main(argv) == 2
    report = json.loads(paths["report"].read_text())
    thresholds = report["thresholds"]
    assert thresholds["min_samples"] == gpd.MIN_SAMPLES
    assert thresholds["min_schema_rate"] == gpd.MIN_SCHEMA_RATE
    assert thresholds["max_permutation_gap"] == gpd.MAX_PERMUTATION_ACC_GAP
    assert thresholds["max_accuracy_regression"] == gpd.MAX_ACCURACY_REGRESSION
    assert len(thresholds["clamped_cli_overrides"]) == 4


def test_gate_refuses_to_register_when_blocked(tmp_path):
    paths = _write_gate_inputs(tmp_path, lambda m: m["menu_size"] + 1, lambda m: m["gold_pick"])
    argv = _gate_argv(paths) + ["--register", "--registry-dir", str(tmp_path / "models")]
    assert gpd.main(argv) == 2
    assert not (tmp_path / "models").exists()


# ---------------------------------------------------------------------------
# corpus construction (only when the real groundtruth corpus is present)
# ---------------------------------------------------------------------------

GROUNDTRUTH = gpd.DEFAULT_GROUNDTRUTH


@pytest.mark.skipif(not GROUNDTRUTH.exists(), reason="replay groundtruth corpus not available")
def test_build_decision_drops_mana_actions_and_keeps_the_gold_pick():
    raw = list(gpd._read_jsonl(GROUNDTRUTH))[:40]
    facts = gpd._CardFacts()
    built = 0
    for rec in raw:
        decision = gpd.build_decision(rec, facts)
        if "drop_reason" in decision:
            continue
        built += 1
        assert all(r["action_type"] not in gpd.MANA_LEVEL_ACTION_TYPES for r in decision["menu"])
        assert 1 <= decision["gold_pick"] <= decision["menu_size"]
        gold_row = decision["menu"][decision["gold_pick"] - 1]
        assert gold_row["action_key"] == decision["gold_action_key"]
        assert decision["gold_pick"] in decision["gold_equivalent_picks"]
    assert built > 0


@pytest.mark.skipif(not GROUNDTRUTH.exists(), reason="replay groundtruth corpus not available")
def test_built_prompt_is_production_shaped():
    raw = list(gpd._read_jsonl(GROUNDTRUTH))
    facts = gpd._CardFacts()
    for rec in raw:
        decision = gpd.build_decision(rec, facts)
        if "drop_reason" in decision:
            continue
        record = gpd.to_corpus_record(decision, "pd-test", "test", "identity", "pd-test")
        from arenamcp.action_planner import AUTOPILOT_SYSTEM_PROMPT

        assert record["system"] is AUTOPILOT_SYSTEM_PROMPT
        assert "Legal: (pick by number)" in record["user"]
        for row in decision["menu"]:
            assert f"  {row['index']}. {row['text']}" in record["user"]
        assert "game_state" not in record["meta"]
        assert not any(k.startswith("_") for k in record["meta"])
        return
    pytest.fail("no usable decision in the groundtruth corpus")


@pytest.mark.skipif(not GROUNDTRUTH.exists(), reason="replay groundtruth corpus not available")
def test_splits_are_deterministic_disjoint_and_format_stratified():
    raw = list(gpd._read_jsonl(GROUNDTRUTH))
    by_file: dict[str, list[dict]] = {}
    for rec in raw:
        if (rec.get("real_pick") or {}).get("status") != "ok":
            continue
        by_file.setdefault(rec["replay_file"], []).append(
            {"format_bucket": gpd.format_bucket(rec["super_format"], rec["variant"])}
        )
    a = gpd.assign_splits(by_file, seed=1234, test_fraction=0.3, dev_fraction=0.15)
    b = gpd.assign_splits(by_file, seed=1234, test_fraction=0.3, dev_fraction=0.15)
    assert a == b
    c = gpd.assign_splits(by_file, seed=9999, test_fraction=0.3, dev_fraction=0.15)
    assert a != c, "the split must actually depend on the seed"

    buckets: dict[str, set] = {}
    for fname, split in a.items():
        buckets.setdefault(by_file[fname][0]["format_bucket"], set()).add(split)
    for bucket, splits in buckets.items():
        assert {"test", "dev", "train"} <= splits, f"bucket {bucket} missing a split"
