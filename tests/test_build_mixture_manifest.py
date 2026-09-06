"""Unit tests for tools/training/wp3/build_mixture_manifest.py helpers.

Fixture-only — no dependence on the (gitignored) corpora under
tools/training/data/ or on ~/.arenamcp. The demo run against the real corpora
is recorded in PROGRESS-wp3-next-run-prep.md and the PR body.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.training.wp3 import build_mixture_manifest as M  # noqa: E402

VOCAB = {
    "island": {"name": "Island", "type_line": "Basic Land — Island"},
    "plains": {"name": "Plains", "type_line": "Basic Land — Plains"},
    "seachrome coast": {"name": "Seachrome Coast", "type_line": "Land"},
    "no more lies": {"name": "No More Lies", "type_line": "Instant"},
    "skrelv, defector mite": {
        "name": "Skrelv, Defector Mite",
        "type_line": "Legendary Creature — Phyrexian Mite",
    },
    "veteran survivor": {"name": "Veteran Survivor", "type_line": "Creature — Human Survivor"},
}

MZ_USER = """TRIGGER: Your turn started (Main Phase 1). Plan your plays.

=== GAME ===
Legal: (pick by number)
  1. Play Island
  2. Cast No More Lies
  3. Pass
T2 YOUR | Main1 | Pri:You

YOUR BOARD:
  Seachrome Coast
OPP BOARD:
  (empty)

HAND:
  Island  [S,LAND]
  Skrelv, Defector Mite  [S,OK]

Respond with ONLY a JSON action plan matching the schema.
"""

CANDIDATE_USER = """TRIGGER: Your turn started (Main Phase 1). Plan your plays.

=== GAME ===
Candidate: (pick by number — RECONSTRUCTED, NOT a complete legal-action list)
  1. Cast Veteran Survivor
  2. Play Land: Plains
  3. Pass
T1 YOUR | Main1 | Pri:You

HAND:
  Veteran Survivor {W} 1/1 (Creature — Human Survivor) - Survival — stuff.
  Plains (Basic Land — Plains) - ({T}: Add {W}.)
"""


# ---------------------------------------------------------------------------
# Menu parsing + classification
# ---------------------------------------------------------------------------


def test_parse_menu_production_shape():
    assert M.parse_menu(MZ_USER) == ["Play Island", "Cast No More Lies", "Pass"]


def test_parse_menu_candidate_shape():
    assert M.parse_menu(CANDIDATE_USER) == ["Cast Veteran Survivor", "Play Land: Plains", "Pass"]


def test_parse_menu_absent():
    assert M.parse_menu("no menu here\n  1. not after a header\n") == []


def test_classify_entries():
    assert M.classify_entry("Pass", VOCAB) == ("pass", None)
    assert M.classify_entry("Cast No More Lies", VOCAB) == ("cast", "No More Lies")
    assert M.classify_entry("Play Land: Plains", VOCAB) == ("play_land", "Plains")
    assert M.classify_entry("Play Island", VOCAB) == ("play", "Island")
    assert M.classify_entry("Declare Attackers (Attack All)", VOCAB) == ("attack", None)


def test_play_entry_land_detection_uses_type_line():
    ac, card = M.classify_entry("Play Island", VOCAB)
    assert M._entry_is_land_play(ac, card, VOCAB)
    ac, card = M.classify_entry("Cast No More Lies", VOCAB)
    assert not M._entry_is_land_play(ac, card, VOCAB)


# ---------------------------------------------------------------------------
# Card extraction (vocab-validated, drop-and-count)
# ---------------------------------------------------------------------------


def test_extract_cards_mz_shape():
    from collections import Counter

    unrec = Counter()
    cards = M.extract_cards_from_user(MZ_USER, VOCAB, unrec)
    assert cards == {"Island", "No More Lies", "Seachrome Coast", "Skrelv, Defector Mite"}


def test_extract_cards_candidate_shape_strips_cost_and_oracle():
    from collections import Counter

    unrec = Counter()
    cards = M.extract_cards_from_user(CANDIDATE_USER, VOCAB, unrec)
    assert cards == {"Veteran Survivor", "Plains"}


def test_unknown_tokens_are_counted_not_silently_included():
    from collections import Counter

    unrec = Counter()
    user = "Legal: (pick by number)\n  1. Cast Totally Fake Card\n  2. Pass\n"
    cards = M.extract_cards_from_user(user, VOCAB, unrec)
    assert cards == set()
    assert unrec["Totally Fake Card"] == 1


def test_display_disambiguator_stripped():
    from collections import Counter

    cards = M.extract_cards_from_user("HAND:\n  Plains #2  [S,LAND]\n", VOCAB, Counter())
    assert cards == {"Plains"}


# ---------------------------------------------------------------------------
# Gold pick extraction
# ---------------------------------------------------------------------------


def test_extract_gold_pick_shapes():
    assert M.extract_gold_pick('{"actions": [{"pick": 4}]}') == 4
    assert M.extract_gold_pick('{"pick": 2}') == 2
    assert M.extract_gold_pick("not json") is None
    assert M.extract_gold_pick('{"actions": []}') is None


# ---------------------------------------------------------------------------
# Mixture math
# ---------------------------------------------------------------------------


def test_plan_mixture_hits_requested_weight():
    plan = M.plan_mixture(n_mz=4808, n_human_avail=14659, human_weight=0.2, seed=1)
    assert plan["human_records_sampled"] == 1202
    assert not plan["capped_by_availability"]
    assert abs(plan["realized_human_share"] - 0.2) < 0.001


def test_plan_mixture_caps_at_availability_and_reports_it():
    plan = M.plan_mixture(n_mz=100, n_human_avail=10, human_weight=0.5, seed=1)
    assert plan["human_records_sampled"] == 10
    assert plan["capped_by_availability"]
    assert plan["realized_human_share"] < 0.5


def test_plan_mixture_zero_weight():
    plan = M.plan_mixture(n_mz=100, n_human_avail=10, human_weight=0.0, seed=1)
    assert plan["human_records_sampled"] == 0
    assert plan["realized_human_share"] == 0.0


def test_plan_mixture_rejects_weight_one():
    with pytest.raises(ValueError):
        M.plan_mixture(n_mz=1, n_human_avail=1, human_weight=1.0, seed=1)


# ---------------------------------------------------------------------------
# scan_corpus end-to-end on a tiny fixture
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def test_scan_corpus_fixture(tmp_path):
    recs = [
        {
            "system": "You are an MTG Arena autopilot. ...",
            "user": MZ_USER,
            "response": '{"actions": [{"pick": 1}]}',
            "source": "magezero_bridge",
            "meta": {},
        },
        {
            "system": "You are an MTG Arena autopilot. ...",
            "user": CANDIDATE_USER,
            "response": '{"actions": [{"pick": 2}]}',
            "source": "17lands-replay_data",
            "meta": {},
        },
        {"system": "x", "user": "no menu at all", "response": '{"actions": [{"pick": 1}]}', "source": "junk"},
    ]
    p = tmp_path / "corpus.jsonl"
    _write_jsonl(p, recs)
    out = M.scan_corpus(str(p), VOCAB)
    s = out["stats"]
    assert s["records_usable"] == 3
    assert s["production_shape"]["production_n"] == 1
    assert s["production_shape"]["candidate_n"] == 1
    assert s["production_shape"]["neither_n"] == 1
    assert s["drops"]["no_parseable_menu"] == 1
    # Both parseable records chose a land drop (Play Island / Play Land: Plains)
    assert s["trivial_policies"]["computable_n"] == 2
    assert s["trivial_policies"]["always_land_else_first"] == 1.0
    assert out["cards"] >= {"Island", "Plains", "Veteran Survivor"}


def test_scan_corpus_drop_and_count_bad_lines(tmp_path):
    p = tmp_path / "corpus.jsonl"
    p.write_text('not json\n{"user": 5, "response": "x"}\n', encoding="utf-8")
    s = M.scan_corpus(str(p), VOCAB)["stats"]
    assert s["records_usable"] == 0
    assert s["drops"]["bad_json"] == 1
    assert s["drops"]["missing_user_or_response"] == 1
