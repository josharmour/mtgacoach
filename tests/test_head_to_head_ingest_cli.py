"""Local synthetic CLI only; no real matches or inference."""

import json

import pytest
from tests.test_head_to_head_ingest import fixture
from tools import head_to_head_ingest as ingest


def inputs(tmp_path):
    protocol, evidence = fixture()
    run, records = tmp_path / "run.json", tmp_path / "evidence.json"
    run.write_text(json.dumps(protocol))
    records.write_text(json.dumps(evidence))
    return ["--run", str(run), "--evidence", str(records)], protocol, records


@pytest.mark.parametrize("mode", ["json", "table"])
def test_cli_aggregate_preserves_caveats(tmp_path, capsys, mode):
    args, protocol, records = inputs(tmp_path)
    original = records.read_bytes()
    ingest.main(args + ["--format", mode])
    captured = capsys.readouterr()
    assert not captured.err
    assert "not_verified_by_schema_or_join" in captured.out
    assert "not_established" in captured.out
    assert "not_assessed" in captured.out
    assert protocol["run_id"] not in captured.out
    assert str(tmp_path) not in captured.out
    assert records.read_bytes() == original
    if mode == "json":
        assert json.loads(captured.out)["paired_games"] == 2


@pytest.mark.parametrize(
    "body",
    [
        '{"private":"secret", "private":"other"}',
        "[NaN]",
        "[Infinity]",
        "PRIVATE SENTINEL",
        '["\\ud800"]',
        "null",
    ],
)
def test_invalid_input_never_echoes_data_or_paths(tmp_path, capsys, body):
    args, _, records = inputs(tmp_path)
    records.write_text(body)
    with pytest.raises(SystemExit) as error:
        ingest.main(args)
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "invalid local head-to-head evidence\n"


def test_bounded_input_and_nonregular_refuse(tmp_path, capsys, monkeypatch):
    args, _, records = inputs(tmp_path)
    monkeypatch.setattr(ingest, "MAX_INPUT_BYTES", 8)
    with pytest.raises(SystemExit):
        ingest.main(args)
    assert capsys.readouterr().out == ""
    with pytest.raises(ValueError):
        ingest._read_json(tmp_path)


def test_missing_input_is_generic(tmp_path, capsys):
    args, _, records = inputs(tmp_path)
    records.unlink()
    with pytest.raises(SystemExit):
        ingest.main(args)
    assert capsys.readouterr().err == "invalid local head-to-head evidence\n"


def test_fifo_refuses_without_waiting_for_writer(tmp_path):
    import os

    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(ValueError):
        ingest._read_json(fifo)
