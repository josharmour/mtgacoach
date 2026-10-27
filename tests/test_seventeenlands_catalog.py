"""Catalog build/fetch and fallback ordering for rotated-set 17lands ratings (#218)."""

import csv
import gzip
import io
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from arenamcp import seventeenlands_catalog as slc
from arenamcp.draftstats import DraftStatsCache


def _meta(name, mtga_id, rarity="uncommon", types=("Creature — Human",), color="W"):
    return {"name": name, "mtga_id": mtga_id, "rarity": rarity, "types": list(types), "color": color}


def _header(names):
    return ["draft_id", "won"] + [
        f"{prefix}_{name}" for name in names for prefix in ("opening_hand", "drawn", "deck")
    ]


def _row(header, slots, won):
    """slots: {card_name: (opening_hand, drawn, deck)}; unlisted cards count 0."""
    row = ["d1", "True" if won else "False"]
    for col in header[2:]:
        for prefix in ("opening_hand", "drawn", "deck"):
            if col.startswith(prefix + "_"):
                name = col[len(prefix) + 1 :]
                oh, dr, dk = slots.get(name, (0, 0, 0))
                value = {"opening_hand": oh, "drawn": dr, "deck": dk}[prefix]
                row.append(str(value))
                break
    return row


@pytest.fixture(autouse=True)
def _fast_enrich(monkeypatch):
    """Skip the local card database in catalog builds (unit speed)."""
    monkeypatch.setattr(
        slc,
        "enrich_meta_rows",
        lambda meta_rows: {
            str(row["name"]): {
                "rarity": row.get("rarity") or "",
                "colors": [row["color"]] if row.get("color") in ("W", "U", "B", "R", "G") else [],
                "type_line": " — ".join(row.get("types") or []),
                "grp_id": row.get("mtga_id") if isinstance(row.get("mtga_id"), int) else None,
            }
            for row in meta_rows
        },
    )


def test_build_catalog_computes_gih_and_gp(monkeypatch):
    monkeypatch.setattr(slc, "MIN_GIH_GAMES", 2)
    meta = [_meta("Alpha", 1), _meta("Beta", 2)]
    header = _header(["Alpha", "Beta"])
    rows = [
        (header, _row(header, {"Alpha": (1, 0, 1)}, won=True)),
        (header, _row(header, {"Alpha": (0, 1, 1)}, won=False)),
        (header, _row(header, {"Alpha": (0, 0, 1)}, won=False)),
        (header, _row(header, {"Beta": (0, 0, 1)}, won=True)),
    ]
    catalog = slc.build_catalog_from_stream("DFT", meta, iter(rows))
    alpha = catalog["cards"]["Alpha"]
    beta = catalog["cards"]["Beta"]
    # Alpha: present in 3 games, in hand in 2 of them (1 win / 2).
    assert alpha["gih_games"] == 2
    assert alpha["gih_wr"] == 0.5
    assert beta["gih_games"] == 0
    assert beta["gih_wr"] is None
    assert catalog["coverage"]["rating_games"] == 4


def test_build_catalog_requires_min_gih_games():
    meta = [_meta("Alpha", 1)]
    header = _header(["Alpha"])
    rows = [(header, _row(header, {"Alpha": (1, 0, 1)}, won=True))]
    catalog = slc.build_catalog_from_stream("DFT", meta, iter(rows))
    assert catalog["cards"]["Alpha"]["gih_wr"] is None
    assert catalog["cards"]["Alpha"]["gih_games"] == 1


def test_build_catalog_leaves_basic_lands_unrated():
    meta = [_meta("Plains", 10, types=("Basic Land — Plains",))]
    header = _header(["Plains"])
    rows = [(header, _row(header, {"Plains": (1, 0, 1)}, won=True))] * 600
    catalog = slc.build_catalog_from_stream("DFT", meta, iter(rows))
    plains = catalog["cards"]["Plains"]
    assert plains["gih_games"] == 0
    assert plains["gih_wr"] is None


def test_build_catalog_synthesizes_unique_grp_ids():
    meta = [_meta("Alpha", None), _meta("Beta", None), _meta("Gamma", 7)]
    header = _header(["Alpha", "Beta", "Gamma"])
    rows = [(header, _row(header, {}, won=True))]
    catalog = slc.build_catalog_from_stream("DFT", meta, iter(rows))
    grp_ids = [card["grp_id"] for card in catalog["cards"].values()]
    assert len(set(grp_ids)) == len(grp_ids)
    assert catalog["cards"]["Gamma"]["grp_id"] == 7


def test_read_catalog_rejects_thin_or_broken_files(tmp_path):
    path = tmp_path / "DFT.catalog.json"
    thin = {"set": "DFT", "cards": {f"c{i}": {"gih_wr": None} for i in range(10)}}
    path.write_text(json.dumps(thin))
    assert slc.read_catalog(path) is None
    path.write_text("{broken")
    assert slc.read_catalog(path) is None
    good = {"set": "DFT", "cards": {f"c{i}": {"gih_wr": 0.55} for i in range(slc.MIN_RATED_CARDS)}}
    path.write_text(json.dumps(good))
    assert slc.read_catalog(path) is not None


def test_resolve_prefers_user_cache_then_packaged(tmp_path, monkeypatch):
    monkeypatch.setattr(slc, "MIN_RATED_CARDS", 1)
    cache_cards = {"cached": {"gih_wr": 0.6}}
    packaged_cards = {"packaged": {"gih_wr": 0.5}}
    cache_file = slc.catalog_path(tmp_path, "DFT")
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text(json.dumps({"set": "DFT", "cards": cache_cards}))

    packaged_file = tmp_path / "packaged" / "DFT.catalog.json"
    packaged_file.parent.mkdir()
    packaged_file.write_text(json.dumps({"set": "DFT", "cards": packaged_cards}))
    monkeypatch.setattr(slc, "packaged_catalog_path", lambda exp: packaged_file)

    scheduled = []
    monkeypatch.setattr(slc, "schedule_fetch", lambda exp, cache_dir: scheduled.append(exp) or True)

    assert slc.resolve_catalog("DFT", tmp_path) == cache_cards

    cache_file.unlink()
    assert slc.resolve_catalog("DFT", tmp_path) == packaged_cards

    packaged_file.unlink()
    assert slc.resolve_catalog("DFT", tmp_path) is None
    assert scheduled == ["DFT"]

    scheduled.clear()
    assert slc.resolve_catalog("DFT", tmp_path, allow_fetch=False) is None
    assert scheduled == []


def _gzip_response(header, rows):
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        text = io.TextIOWrapper(gz, encoding="utf-8", newline="")
        writer = csv.writer(text)
        writer.writerow(header)
        writer.writerows(rows)
        text.flush()
    payload = buf.getvalue()

    response = MagicMock()
    response.raise_for_status.return_value = None
    response.__enter__ = MagicMock(return_value=response)
    response.__exit__ = MagicMock(return_value=False)
    response.iter_content.side_effect = lambda chunk_size=1 << 20: iter(
        [payload[i : i + chunk_size] for i in range(0, len(payload), chunk_size)]
    )
    return response


def test_fetch_catalog_builds_and_caches_from_mocked_http(tmp_path, monkeypatch):
    monkeypatch.setattr(slc, "MIN_GIH_GAMES", 2)
    monkeypatch.setattr(slc, "MIN_RATED_CARDS", 2)
    meta_rows = [_meta("Alpha", 1), _meta("Beta", 2)]
    header = _header(["Alpha", "Beta"])
    game_rows = [
        _row(header, {"Alpha": (1 if i % 2 == 0 else 0, i % 2, 1)}, won=i % 2 == 0) for i in range(3)
    ]
    # Beta sits in every deck but only shows up in hand every third game.
    game_rows += [
        _row(header, {"Beta": (1 if i % 3 == 0 else 0, 0, 1)}, won=(i // 3) % 2 == 0) for i in range(9)
    ]

    meta_response = MagicMock()
    meta_response.raise_for_status.return_value = None
    meta_response.json.return_value = meta_rows

    def fake_get(url, **kwargs):
        if "card_ratings" in url:
            return meta_response
        return _gzip_response(header, game_rows)

    with patch.object(slc.requests, "get", side_effect=fake_get):
        path = slc.fetch_catalog("DFT", tmp_path)

    assert path == slc.catalog_path(tmp_path, "DFT")
    catalog = json.loads(path.read_text(encoding="utf-8"))
    alpha = catalog["cards"]["Alpha"]
    beta = catalog["cards"]["Beta"]
    # Alpha: in hand in all 3 games (opening hand or drawn), wins at i=0 and i=2.
    assert alpha["gih_games"] == 3
    assert alpha["gih_wr"] == round(2 / 3, 5)
    # Beta: in hand at i=0/3/6; wins at i=0 and i=6.
    assert beta["gih_games"] == 3
    assert beta["gih_wr"] == round(2 / 3, 5)
    assert catalog["coverage"]["rating_games"] == len(game_rows)


def test_fetch_catalog_degrades_when_no_card_list(tmp_path):
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = []
    with patch.object(slc.requests, "get", return_value=response):
        assert slc.fetch_catalog("DFT", tmp_path) is None
        assert not slc.catalog_path(tmp_path, "DFT").exists()


def test_fetch_catalog_keeps_partial_sample_when_byte_cap_truncates(tmp_path, monkeypatch):
    """A byte-capped stream ends mid-gzip; the games read so far still ship."""
    monkeypatch.setattr(slc, "MIN_GIH_GAMES", 2)
    monkeypatch.setattr(slc, "MIN_RATED_CARDS", 1)
    meta_rows = [_meta("Alpha", 1)]
    header = _header(["Alpha"])
    game_rows = [_row(header, {"Alpha": (1, 0, 1)}, won=i % 2 == 0) for i in range(800)]

    meta_response = MagicMock()
    meta_response.raise_for_status.return_value = None
    meta_response.json.return_value = meta_rows

    full = _gzip_response(header, game_rows)
    payload = b"".join(full.iter_content(chunk_size=1 << 20))
    truncated = payload[: int(len(payload) * 0.7)]  # cut inside the gzip stream

    def fake_get(url, **kwargs):
        if "card_ratings" in url:
            return meta_response
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.__enter__ = MagicMock(return_value=response)
        response.__exit__ = MagicMock(return_value=False)
        response.iter_content.side_effect = lambda chunk_size=1 << 20: iter(
            [truncated[i : i + chunk_size] for i in range(0, len(truncated), chunk_size)]
        )
        return response

    with patch.object(slc.requests, "get", side_effect=fake_get):
        path = slc.fetch_catalog("DFT", tmp_path)

    assert path is not None
    catalog = json.loads(path.read_text(encoding="utf-8"))
    assert catalog["coverage"]["rating_games"] < len(game_rows)
    assert catalog["coverage"]["rating_games"] > 0


def test_schedule_fetch_runs_once_and_backs_off_after_failure(tmp_path):
    slc.reset_fetch_state()
    calls = []

    with patch.object(slc, "fetch_catalog", side_effect=lambda exp, cache_dir: calls.append(exp)):

        def wait_for(n):
            # Observing fetch_catalog's call is not observing worker completion:
            # the thread still has to publish its backoff state under the lock.
            # perf_counter is unaffected by the mocked scheduling clock below.
            deadline = time.perf_counter() + 5
            while time.perf_counter() < deadline:
                with slc._lock:
                    if len(calls) >= n and "DFT" not in slc._threads:
                        return len(calls)
                time.sleep(0.01)
            raise AssertionError("catalog worker did not finish")

        assert slc.schedule_fetch("DFT", tmp_path) is True
        # A second schedule inside the attempt interval is suppressed.
        assert slc.schedule_fetch("DFT", tmp_path) is False
        assert wait_for(1) == 1

        # After a failure the next attempt waits out the backoff window.
        future = time.monotonic() + slc._MIN_ATTEMPT_INTERVAL_S + slc._BACKOFF_BASE_S + 1
        with patch.object(slc.time, "monotonic", return_value=future):
            assert slc.schedule_fetch("DFT", tmp_path) is True
            assert wait_for(2) == 2

        # A success clears the failure count (advance past the grown backoff).
        with patch.object(slc.time, "monotonic", return_value=future + slc._MAX_BACKOFF_S + 1):
            with patch.object(slc, "fetch_catalog", side_effect=lambda exp, cache_dir: tmp_path):
                assert slc.schedule_fetch("DFT", tmp_path) is True
                assert wait_for(2) == 2
                assert slc._failure_count.get("DFT") is None
    slc.reset_fetch_state()


def test_draftstats_merges_packaged_catalog_for_rotated_set(tmp_path, monkeypatch):
    """Thin API rows for a rotated set pick up win rates from the packaged catalog."""
    monkeypatch.setattr(slc, "MIN_RATED_CARDS", 1)
    cache = tmp_path / "17lands"
    cache.mkdir()
    rows = [{"name": f"Card {i}", "ever_drawn_win_rate": None} for i in range(25)]
    (cache / "DFT_PremierDraft.json").write_text(json.dumps(rows))

    catalog_cards = {"card 7": {"gih_wr": 0.61, "gp_wr": 0.58, "gih_games": 4200}}
    packaged_file = tmp_path / "packaged" / "DFT.catalog.json"
    packaged_file.parent.mkdir()
    packaged_file.write_text(json.dumps({"set": "DFT", "cards": catalog_cards}))
    monkeypatch.setattr(slc, "packaged_catalog_path", lambda exp: packaged_file)
    monkeypatch.setattr(slc, "schedule_fetch", lambda exp, cache_dir: True)

    stats = DraftStatsCache(cache_dir=cache)
    rating = stats.get_draft_rating("Card 7", "DFT")
    assert rating is not None
    assert rating.gih_wr == 0.61
    assert rating.games_in_hand == 4200
    # Unlisted cards stay unrated but present.
    missing = stats.get_draft_rating("Card 3", "DFT")
    assert missing is not None and missing.gih_wr is None


def test_draftstats_degrades_without_any_catalog(tmp_path, monkeypatch):
    """No cache, no packaged catalog, fetch failing: unrated rows, no exception."""
    cache = tmp_path / "17lands"
    cache.mkdir()
    rows = [{"name": f"Card {i}", "ever_drawn_win_rate": None} for i in range(25)]
    (cache / "DFT_PremierDraft.json").write_text(json.dumps(rows))

    monkeypatch.setattr(slc, "packaged_catalog_path", lambda exp: None)
    monkeypatch.setattr(slc, "schedule_fetch", lambda exp, cache_dir: True)

    stats = DraftStatsCache(cache_dir=cache)
    rating = stats.get_draft_rating("Card 7", "DFT")
    assert rating is not None and rating.gih_wr is None
