"""Synthetic protocol fixtures, not real gameplay evidence."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "h", Path(__file__).parents[1] / "tools/head_to_head.py"
)
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


def setup():
    p = dict(
        version=1,
        run_id="a" * 64,
        deck_sha256="b" * 64,
        format="Standard",
        games=2,
        machines=["c" * 64, "d" * 64],
        sides={
            "A": dict(model="same-model", code_revision="e" * 40),
            "B": dict(model="same-model", code_revision="f" * 40),
        },
    )
    rows = []
    for game in (1, 2):
        for side in ("A", "B"):
            rows.append(
                dict(
                    game_id=str(game) * 64,
                    run_id=p["run_id"],
                    game=game,
                    side=side,
                    machine=p["machines"][(side == "B") ^ (game > 1)],
                    deck_sha256=p["deck_sha256"],
                    format=p["format"],
                    **p["sides"][side],
                    winner="A",
                    on_play="A" if game == 1 else "B",
                    metrics=dict(
                        decisions=10,
                        decision_seconds=5,
                        timeouts=1,
                        safe_defaults=2,
                        model_calls=8,
                        input_tokens=100,
                        output_tokens=20,
                    ),
                )
            )
    return p, rows


def test_pairs_swap_same_model_different_code_and_table():
    p, rows = setup()
    result = h.analyze(p, rows)
    assert result["paired_games"] == 2
    assert result["sides"]["A"]["wins"] == 2
    assert result["sides"]["A"]["wins_on_play"] == 1
    assert result["sides"]["A"]["wins_on_draw"] == 1
    assert result["sides"]["B"]["seconds_per_decision"] == 0.5
    assert "live twenty-game gate not assessed" in h.table(result)
    assert p["run_id"] not in json.dumps(result)
    assert "same-model" not in h.table(result)


def test_duplicate_exact_dedup_and_conflicting_reject():
    p, rows = setup()
    assert (
        h.analyze(p, rows + [copy.deepcopy(rows[0])])["duplicate_records_ignored"] == 1
    )
    bad = copy.deepcopy(rows[0])
    bad["winner"] = "B"
    with pytest.raises(h.InvalidRun):
        h.analyze(p, rows + [bad])


@pytest.mark.parametrize(
    "field,value",
    [
        ("machine", "c" * 64),
        ("deck_sha256", "f" * 64),
        ("code_revision", "a" * 40),
        ("format", "Brawl"),
        ("model", "wrong"),
        ("on_play", "unknown"),
        ("winner", "B"),
        ("game_id", "3" * 64),
    ],
)
def test_cross_machine_conflicts_reject(field, value):
    p, rows = setup()
    rows[1][field] = value
    with pytest.raises(h.InvalidRun):
        h.analyze(p, rows)


def test_no_reused_game_identity_across_sequences():
    p, rows = setup()
    rows[2]["game_id"] = rows[0]["game_id"]
    rows[3]["game_id"] = rows[0]["game_id"]
    with pytest.raises(h.InvalidRun):
        h.analyze(p, rows)


def test_draw_unknown_and_missing_not_losses():
    p, rows = setup()
    for r in rows[:2]:
        r["winner"] = "draw"
    result = h.analyze(p, rows[:3])
    assert result["draws"] == 1 and result["missing_or_unpaired_games"] == 1
    assert (
        result["decisive_games"] == 0 and result["sides"]["A"]["win_rate_95ci"] is None
    )
    for r in rows:
        r["winner"] = "unknown"
    assert h.analyze(p, rows)["unknown_outcomes"] == 2


def test_intervals_and_unknown_play():
    assert h.wilson(0, 0) is None
    lo, hi = h.wilson(6, 10)
    assert lo < 0.5 < hi
    lo, hi = h.wilson(60, 100)
    assert 0.49 < lo < 0.51 and 0.68 < hi < 0.70
    p, rows = setup()
    for r in rows:
        r["on_play"] = "unknown"
    assert h.analyze(p, rows)["sides"]["A"]["wins_unknown_play"] == 2


def test_private_extra_keys_and_numeric_poison_reject():
    for change in (
        lambda r: r.update(customer="Private Person"),
        lambda r: r["metrics"].update(decision_seconds=float("nan")),
        lambda r: r["metrics"].update(input_tokens=10**400),
        lambda r: r["metrics"].update(timeouts=True),
    ):
        p, rows = setup()
        change(rows[0])
        with pytest.raises(h.InvalidRun):
            h.analyze(p, rows)


def test_cli_errors_do_not_echo_private_input(tmp_path, capsys):
    p = tmp_path / "p.json"
    p.write_text("{Private Person")
    with pytest.raises(SystemExit) as e:
        h.main(["--run", str(p), "--records", str(p)])
    assert (
        e.value.code == 2
        and capsys.readouterr().err == "invalid head-to-head evidence\n"
    )
