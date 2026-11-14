"""Synthetic reports only; never evidence of completed real benchmark gates."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "report", Path(__file__).parents[1] / "tools/benchmark_report.py"
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def report():
    return dict(
        version=1,
        corpus_sha256="a" * 64,
        configuration_sha256="b" * 64,
        facts_sha256="c" * 64,
        labels_sha256="f" * 64,
        model_layer="not_run",
        results=[
            dict(
                case_id="d" * 64,
                status="answered",
                selection_sha256="e" * 64,
                seconds=0.1,
                generation_attempts=0,
                score="not_scored",
            )
        ],
    )


def test_summary_and_equal_diff_hide_identifiers():
    r = report()
    assert m.summary(r)["statuses"] == {"answered": 1}
    delta = m.compare(r, copy.deepcopy(r))
    assert delta["comparable"] and delta["changed_selection"] == 0
    assert "d" * 64 not in json.dumps(delta)
    assert "acceptance: not assessed" in m.table(delta)


@pytest.mark.parametrize(
    "key", ["corpus_sha256", "configuration_sha256", "facts_sha256", "labels_sha256", "case_set"]
)
def test_incomparable_provenance_never_claims_behavior_delta(key):
    a = report()
    b = report()
    if key == "case_set":
        b["results"][0]["case_id"] = "f" * 64
    else:
        b[key] = "0" * 64
    result = m.compare(a, b)
    assert not result["comparable"]
    assert key in result["mismatches"]
    assert "changed_selection" not in result


def test_duplicate_ids_and_invalid_fields_rejected():
    for mutate in (
        lambda r: r["results"].append(copy.deepcopy(r["results"][0])),
        lambda r: r.update(customer="Private Person"),
        lambda r: r["results"][0].update(error="Private Person"),
        lambda r: r["results"][0].update(status="Private Person"),
        lambda r: r["results"][0].update(case_id="Private Person"),
        lambda r: r["results"][0].update(seconds=float("nan")),
        lambda r: r["results"][0].update(generation_attempts=True),
        lambda r: r["results"][0].update(generation_attempts=1),
        lambda r: r["results"][0].update(status="withheld", score="acceptable"),
    ):
        r = report()
        mutate(r)
        with pytest.raises(m.InvalidReport):
            m.summary(r)


def test_cli_diff_needs_no_cases_and_errors_never_echo_input(tmp_path, capsys):
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report()))
    m.main(["--diff", str(path), str(path), "--format", "json"])
    assert json.loads(capsys.readouterr().out)["comparable"]
    path.write_text("{Private Person")
    with pytest.raises(SystemExit) as error:
        m.main(["--report", str(path)])
    assert error.value.code == 2
    assert capsys.readouterr().err == "invalid benchmark report\n"


def test_supplied_score_counts_do_not_invent_labels():
    r = report()
    r["results"][0]["score"] = "acceptable"
    assert m.summary(r)["scores"] == {"acceptable": 1}
    assert m.summary(r)["acceptance"] == "not_assessed"


def test_duration_and_counter_overflow_rejected():
    for number in (10**1000, float("inf")):
        r = report()
        r["results"][0]["seconds"] = number
        with pytest.raises(m.InvalidReport):
            m.summary(r)
    r = report()
    r["results"][0]["seconds"] = 1e308
    second = copy.deepcopy(r["results"][0])
    second["case_id"] = "0" * 64
    r["results"].append(second)
    with pytest.raises(m.InvalidReport):
        m.summary(r)
    r = report()
    r["results"][0]["generation_attempts"] = 10**1000
    with pytest.raises(m.InvalidReport):
        m.summary(r)


def test_table_includes_supplied_scores_and_label_set_gates_diff():
    a = report()
    a["results"][0]["score"] = "acceptable"
    assert "score acceptable: 1" in m.table(m.summary(a))
    b = copy.deepcopy(a)
    b["labels_sha256"] = "0" * 64
    b["results"][0]["score"] = "blunder"
    delta = m.compare(a, b)
    assert not delta["comparable"]
    assert delta["mismatches"] == ["labels_sha256"]
    assert "changed_score" not in delta
