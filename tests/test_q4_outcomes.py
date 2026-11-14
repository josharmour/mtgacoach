"""Offline regressions for staged server dashboard and uploaded dimensions."""

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "tools/monitoring/q4-staging"
spec = importlib.util.spec_from_file_location("q4_outcomes", STAGE / "coach_outcomes.py")
outcomes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(outcomes)
IDENT = {
    "one": {"id": "person", "user": "Fixture", "kind": "trial"},
    "two": {"id": "person", "user": "Fixture", "kind": "trial"},
}


def bundle(mid, result="win", mode=True, deck="Deck A", fmt="Ranked", **kw):
    return (
        dict(
            owner="one",
            install_id="private",
            match_id=mid,
            result=result,
            received_at=10,
            platform="darwin",
            autopilot_enabled=mode,
            format_name=fmt,
            deck_name=deck,
        )
        | kw
    )


def test_split_and_decks_reconcile():
    data = [
        bundle("a"),
        bundle("b", "loss", False),
        bundle("c", deck="Deck B"),
        bundle("d", fmt="Unranked"),
        bundle("e", mode=None),
    ]
    rows = outcomes.aggregate(data, IDENT, 0, 20, ("play_mode", "format_name"))
    assert sum(r["games"] for r in rows) == 5
    manual = next(r for r in rows if r["play_mode"] == "played by hand")
    assert manual["win_rate_pct"] == 0
    assert all(r["win_rate_pct"] == 100 for r in rows if r["play_mode"] == "autopilot")
    decks = outcomes.aggregate(data, IDENT, 0, 20, ("play_mode", "format_name", "deck_name"))
    assert len(decks) == 5
    assert sum(r["games"] for r in decks) == sum(r["games"] for r in rows)


@pytest.mark.parametrize("mode", [None, "false", "true", 2, [], {}])
def test_missing_or_invalid_mode_is_not_manual(mode):
    rows = outcomes.aggregate([bundle("a", mode=mode)], IDENT, 0, 20, ("play_mode",))
    assert rows[0]["play_mode"] == "unknown"


def test_deduplicate_before_dimension_groups():
    data = [
        bundle("a"),
        bundle("a", mode=False, owner="two", deck="Other"),
        bundle("b", "unknown", received_at=1),
        bundle("b", received_at=11),
    ]
    rows = outcomes.aggregate(data, IDENT, 5, 20, ("play_mode", "deck_name"))
    assert sum(r["games"] for r in rows) == 2
    conflict = next(r for r in rows if r["play_mode"] == "unknown")
    assert conflict["deck_name"] == "unknown"
    assert conflict["games"] == 1


def test_outcome_conflict_draw_and_time_boundaries():
    data = [
        bundle("a"),
        bundle("a", "loss"),
        bundle("b", "draw"),
        bundle("before", received_at=4),
        bundle("start", received_at=5),
        bundle("end", received_at=20),
    ]
    rows = outcomes.aggregate(data, IDENT, 5, 20, ("play_mode",))
    r = rows[0]
    assert (r["games"], r["wins"], r["draws"], r["unknown"], r["conflicts"]) == (3, 1, 1, 1, 1)
    assert r["win_rate_pct"] == 100
    assert outcomes.aggregate([bundle("draw", "draw")], IDENT, 0, 20)[0]["win_rate_pct"] is None


@pytest.mark.parametrize("modern", [False, True])
def test_old_and_new_schema_local_query(tmp_path, modern):
    p = tmp_path / "fixture.db"
    c = sqlite3.connect(p)
    c.executescript(
        "CREATE TABLE match_bundles(owner,install_id,match_id,received_at,result,platform);"
        "CREATE TABLE subscribers(license_key,email,name);CREATE TABLE trials(litellm_key,email);"
    )
    c.execute(
        "INSERT INTO match_bundles VALUES (?,?,?,?,?,?)",
        ("one", "secret-install", "secret-match", 10, "win", "darwin"),
    )
    if modern:
        for col, typ in [("autopilot_enabled", "INTEGER"), ("format_name", "TEXT"), ("deck_name", "TEXT")]:
            c.execute(f"ALTER TABLE match_bundles ADD COLUMN {col} {typ}")
        c.execute("UPDATE match_bundles SET autopilot_enabled=1,format_name='Ranked',deck_name='Deck A'")
    c.commit()
    c.close()
    result = outcomes.load_outcomes(p, [], 0, 20)
    assert result["modes"][0]["play_mode"] == ("autopilot" if modern else "unknown")
    assert result["totals"][0]["games"] == 1
    assert result["decks"][0]["deck_name"] == ("Deck A" if modern else "unknown")
    assert "secret-install" not in json.dumps(result)
    assert "secret-match" not in json.dumps(result)


def test_dashboard_artifact():
    dashboard = json.loads((STAGE / "grafana/dashboards/gateway-usage.json").read_text())
    panels = dashboard["panels"]
    all_panels = panels + [child for p in panels for child in p.get("panels", [])]
    assert len({p["id"] for p in all_panels}) == len(all_panels)
    for pid, view in zip(range(4720, 4724), ["autopilot", "manual", "unknown", "decks"], strict=True):
        panel = next(p for p in panels if p["id"] == pid)
        assert f"/api/mtgacoach-outcomes?view={view}" in panel["targets"][0]["url"]
        names = {col["selector"] for col in panel["targets"][0]["columns"]}
        assert {"play_mode", "format_name", "win_rate_pct"} <= names
        assert ("deck_name" in names) == (view == "decks")
    top = [p for p in panels if p["type"] != "row"]
    for i, a in enumerate(top):
        for b in top[i + 1 :]:
            x, y = a["gridPos"], b["gridPos"]
            assert not (
                x["x"] < y["x"] + y["w"]
                and y["x"] < x["x"] + x["w"]
                and x["y"] < y["y"] + y["h"]
                and y["y"] < x["y"] + x["h"]
            )
