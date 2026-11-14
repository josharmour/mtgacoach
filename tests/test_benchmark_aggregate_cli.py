import importlib.util
import json
import logging
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("b", Path(__file__).parents[1] / "tools/decision_benchmark.py")
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


def setup(monkeypatch):
    monkeypatch.setattr(
        b, "build_cases", lambda paths: ([{"private": "secret"}], [{"source": "private-path"}], {"secret": 1})
    )

    def replay(case):
        print("private raw prompt")
        logging.critical("private log")
        return {"status": "withheld", "seconds": 1.5, "generation_attempts": 2, "chosen": ["private-choice"]}

    monkeypatch.setattr(b, "replay", replay)


def test_no_export_or_private_output(tmp_path, monkeypatch, capsys):
    setup(monkeypatch)
    before = logging.root.manager.disable
    b.main(["--aggregate-only", "--bundles", str(tmp_path)])
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert captured.err == ""
    assert "private" not in captured.out and "secret" not in captured.out
    assert result["cases"] == 1 and result["counts"]["withheld"] == 1
    assert result["inert_generation_attempts"] == 2
    assert not list(tmp_path.iterdir())
    assert logging.root.manager.disable == before


@pytest.mark.parametrize("flag", ["--cases", "--report"])
def test_export_options_forbidden(tmp_path, flag, capsys):
    with pytest.raises(SystemExit) as exc:
        b.main(["--aggregate-only", "--bundles", str(tmp_path), flag, str(tmp_path / "output")])
    assert exc.value.code == 2
    assert not list(tmp_path.iterdir())


def test_failure_is_generic_and_restores_logging(tmp_path, monkeypatch, capsys):
    setup(monkeypatch)

    def fail(case):
        raise RuntimeError("private token")

    monkeypatch.setattr(b, "replay", fail)
    before = logging.root.manager.disable
    with pytest.raises(SystemExit):
        b.main(["--aggregate-only", "--bundles", str(tmp_path)])
    captured = capsys.readouterr()
    assert "private" not in captured.err and captured.out == ""
    assert logging.root.manager.disable == before


@pytest.mark.parametrize(
    "row",
    [
        {"status": "unexpected"},
        {"status": "answered", "seconds": float("nan"), "generation_attempts": 0},
        {"status": "answered", "seconds": 1, "generation_attempts": True},
    ],
)
def test_bad_result_refused(tmp_path, monkeypatch, row):
    setup(monkeypatch)
    monkeypatch.setattr(b, "replay", lambda case: row)
    with pytest.raises(ValueError):
        b.aggregate_only(tmp_path)


def test_static_database_is_never_initialized(tmp_path, monkeypatch):
    setup(monkeypatch)
    from arenamcp import card_db, match_context
    from arenamcp.action_planner import _ability_rules_text

    calls = []
    monkeypatch.setattr(card_db, "get_card_database", lambda: calls.append("unsafe"))

    def replay(case):
        assert match_context._local_card(123, 0) == {}
        assert _ability_rules_text(123) == ""
        return {"status": "withheld", "seconds": 0, "generation_attempts": 0}

    monkeypatch.setattr(b, "replay", replay)
    b.aggregate_only(tmp_path)
    assert calls == []


def test_direct_fd_output_hidden_and_restored(tmp_path, monkeypatch, capfd):
    import os
    import subprocess
    import sys

    setup(monkeypatch)

    def replay(case):
        os.write(1, b"private native stdout")
        os.write(2, b"private native stderr")
        subprocess.run([sys.executable, "-c", 'import os; os.write(2,b"private child")'], check=True)
        raise ValueError("private failure")

    monkeypatch.setattr(b, "replay", replay)
    with pytest.raises(ValueError):
        b.aggregate_only(tmp_path)
    out = capfd.readouterr()
    assert out.out == "" and out.err == ""
    os.write(1, b"restored out")
    os.write(2, b"restored err")
    out = capfd.readouterr()
    assert out.out == "restored out" and out.err == "restored err"
