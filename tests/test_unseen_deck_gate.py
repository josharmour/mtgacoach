"""Unseen-deck gate slice wiring (MORNING PRIORITY item 1, measurement half).

Proves, on fixtures, everything that must work before the real holdout logs
(mz_unseen_HighNoonControl_vs_BGRoots.log / mz_unseen_BWBats_vs_HighNoonControl.log)
exist:

1. parse_magezero_log works when the PRIMARY deck is NOT UWTempo — deck
   signatures derive from the .dck lists (filename inference or
   --primary-deck), and attribution ambiguity / off-deck hands FAIL CLOSED.
2. build_unseen_gate_corpus emits records in the strategic-gate shape that
   run_b5_gate_eval.check_corpora accepts, with valid permuted twins.
3. run_b5_gate_eval's --unseen-corpus generalization scoring produces the
   seen/unseen/gap/CI section, and the flag defaults to None (absent flag =
   unchanged behavior).

The fixture log reuses the exact line grammar validated against
mz_train_smoke.log in tests/test_parse_magezero_log.py, with players/cards
renamed per the real BGRoots/HighNoonControl .dck lists.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
REPO = _HERE.parent
sys.path.insert(0, str(REPO / "tools" / "training"))

from parse_magezero_log import (  # noqa: E402
    LAST_PARSE_STATS,
    DeckSignatureError,
    infer_decks_from_log_name,
    parse_dck_names,
    parse_log,
    resolve_primary_deck,
)

# ---------------------------------------------------------------------------
# Fixture material: real BGRoots / HighNoonControl names from the .dck lists
# ---------------------------------------------------------------------------

BGROOTS_DCK = """4 [OTJ:266] Blooming Marsh
4 [LCI:102] Deep-Cavern Bat
3 [TMT:257] Forest
4 [MKM:208] Insidious Roots
2 [FDN:227] Llanowar Elves
4 [BRO:264] Llanowar Wastes
4 [MKM:105] Snarling Gorehound
2 [TMT:255] Swamp
4 [ONE:218] Tyvar, Jubilant Brawler
4 [DFT:268] Wastewood Verge
LAYOUT MAIN:(1,1)(NONE,false,50)|([OTJ:266],[OTJ:266])
"""

HIGHNOON_DCK = """4 [KTK:233] Flooded Strand
4 [OTJ:15] High Noon
1 [TMT:254] Island
3 [CLU:141] Lightning Bolt
1 [TMT:256] Mountain
1 [TMT:253] Plains
4 [M11:70] Preordain
3 [H2R:3] Solitude
4 [EMN:189] Spell Queller
"""

T = "=>[pool-3-thread-1]"

# Grammar identical to the shapes pinned in tests/test_parse_magezero_log.py
# (named-hand logList lines, logLife, playable abilities, pool, chose action),
# only the deck is BGRoots. One game, 3 chose-action windows + 1 recovered
# pass; "Tyvar, Jubilant Brawler" exercises comma-name segmentation.
FIXTURE_LOG = (
    "INFO  2026-07-31 08:00:00,000 Simulating 1 games. =>[main]\n"
    f"INFO  2026-07-31 08:00:01,000 Player A won the die roll {T} GameImpl\n"
    f"INFO  2026-07-31 08:00:02,000 [1:Beginning:UPKEEP]PlayerA hand: : "
    f"Swamp,Llanowar Elves,Deep-Cavern Bat,Tyvar, Jubilant Brawler, {T} ComputerPlayer.logList \n"
    f"INFO  2026-07-31 08:00:03,000 [1:Precombat Main:PRECOMBAT_MAIN]"
    f"[player PlayerA:20][player PlayerB:20] {T} GameImpl.logLife\n"
    f"INFO  2026-07-31 08:00:04,000 [1:Precombat Main:PRECOMBAT_MAIN]PlayerA "
    f"playable abilities: [Play Swamp, Pass] {T} ComputerPlayerMCTS\n"
    f"INFO  PRECOMBAT_MAIN0pool= actions: [Pass score: -0.1 count: 50] "
    f"[Play Swamp score: 0.1 count: 200] {T} MCTSNode.bestChild\n"
    f"INFO  2026-07-31 08:00:05,000 [1:Precombat Main:PRECOMBAT_MAIN]"
    f"chose action:Play Swamp success ratio: 0.1 {T} ComputerPlayerMCTS\n"
    f"INFO  2026-07-31 08:00:06,000 [2:Beginning:UPKEEP]PlayerA hand: : "
    f"Llanowar Elves,Deep-Cavern Bat,Tyvar, Jubilant Brawler,Llanowar Wastes, {T} ComputerPlayer.logList \n"
    f"INFO  2026-07-31 08:00:07,000 [2:Precombat Main:PRECOMBAT_MAIN]"
    f"[player PlayerA:20][player PlayerB:19] {T} GameImpl.logLife\n"
    f"INFO  2026-07-31 08:00:08,000 [2:Precombat Main:PRECOMBAT_MAIN]PlayerA "
    f"playable abilities: [Cast Llanowar Elves, Play Llanowar Wastes, Pass] {T} ComputerPlayerMCTS\n"
    f"INFO  PRECOMBAT_MAIN0pool= actions: [Pass score: -0.05 count: 30] "
    f"[Cast Llanowar Elves score: 0.2 count: 400] "
    f"[Play Llanowar Wastes score: 0.05 count: 100] {T} MCTSNode.bestChild\n"
    f"INFO  2026-07-31 08:00:09,000 [2:Precombat Main:PRECOMBAT_MAIN]"
    f"chose action:Cast Llanowar Elves success ratio: 0.2 {T} ComputerPlayerMCTS\n"
    f"INFO  2026-07-31 08:00:10,000 [3:Beginning:UPKEEP]PlayerA hand: : "
    f"Deep-Cavern Bat,Tyvar, Jubilant Brawler, {T} ComputerPlayer.logList \n"
    f"INFO  2026-07-31 08:00:11,000 [3:Precombat Main:PRECOMBAT_MAIN]"
    f"[player PlayerA:18][player PlayerB:17] {T} GameImpl.logLife\n"
    f"INFO  2026-07-31 08:00:12,000 [3:Precombat Main:PRECOMBAT_MAIN]PlayerA "
    f"playable abilities: [Cast Deep-Cavern Bat, Cast Tyvar, Jubilant Brawler, Pass] {T} ComputerPlayerMCTS\n"
    f"INFO  PRECOMBAT_MAIN0pool= actions: [Pass score: -0.02 count: 20] "
    f"[Cast Deep-Cavern Bat score: 0.3 count: 300] "
    f"[Cast Tyvar, Jubilant Brawler score: 0.1 count: 150] {T} MCTSNode.bestChild\n"
    f"INFO  2026-07-31 08:00:13,000 [3:Precombat Main:PRECOMBAT_MAIN]"
    f"chose action:Cast Deep-Cavern Bat success ratio: 0.3 {T} ComputerPlayerMCTS\n"
    # Recovered pass window: pool never consumed by a chose line, argmax Pass.
    f"INFO  2026-07-31 08:00:14,000 [3:Postcombat Main:POSTCOMBAT_MAIN]PlayerA "
    f"playable abilities: [Cast Tyvar, Jubilant Brawler, Pass] {T} ComputerPlayerMCTS\n"
    f"INFO  POSTCOMBAT_MAIN0pool= actions: [Pass score: 0.4 count: 500] "
    f"[Cast Tyvar, Jubilant Brawler score: 0.02 count: 40] {T} MCTSNode.bestChild\n"
    "INFO  2026-07-31 08:00:15,000 Player A win rate: 100.00% (1/1) =>[main]\n"
)

BGROOTS_NAMES = {
    "Blooming Marsh",
    "Deep-Cavern Bat",
    "Forest",
    "Insidious Roots",
    "Llanowar Elves",
    "Llanowar Wastes",
    "Snarling Gorehound",
    "Swamp",
    "Tyvar, Jubilant Brawler",
    "Wastewood Verge",
}


@pytest.fixture()
def decks_dir(tmp_path: Path) -> Path:
    d = tmp_path / "decks"
    d.mkdir()
    (d / "BGRoots.dck").write_text(BGROOTS_DCK, encoding="utf-8")
    (d / "HighNoonControl.dck").write_text(HIGHNOON_DCK, encoding="utf-8")
    return d


@pytest.fixture()
def fixture_log(tmp_path: Path) -> Path:
    p = tmp_path / "mz_unseen_BGRoots_vs_HighNoonControl.log"
    p.write_text(FIXTURE_LOG, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 1. Deck resolution: derive-from-.dck, --primary-deck override, fail-closed
# ---------------------------------------------------------------------------


class TestDeckResolution:
    def test_dck_parsing_matches_deck_list(self, decks_dir: Path):
        assert parse_dck_names(decks_dir / "BGRoots.dck") == frozenset(BGROOTS_NAMES)

    def test_filename_inference(self):
        assert infer_decks_from_log_name("mz_unseen_HighNoonControl_vs_BGRoots.log") == (
            "HighNoonControl",
            "BGRoots",
        )
        assert infer_decks_from_log_name("/x/y/mz_unseen_BWBats_vs_HighNoonControl.log") == (
            "BWBats",
            "HighNoonControl",
        )

    def test_legacy_log_names_do_not_infer(self):
        assert infer_decks_from_log_name("mz_train_smoke.log") is None
        assert infer_decks_from_log_name("mz_train.log") is None

    def test_resolve_by_inference(self, decks_dir: Path):
        name, cards = resolve_primary_deck(
            "mz_unseen_BGRoots_vs_HighNoonControl.log", decks_dir=decks_dir
        )
        assert name == "BGRoots"
        assert cards == frozenset(BGROOTS_NAMES)

    def test_explicit_primary_deck_wins_over_filename(self, decks_dir: Path):
        name, cards = resolve_primary_deck(
            "mz_unseen_BGRoots_vs_HighNoonControl.log",
            primary_deck="HighNoonControl",
            decks_dir=decks_dir,
        )
        assert name == "HighNoonControl"
        assert "Lightning Bolt" in cards

    def test_explicit_deck_missing_fails_closed(self, decks_dir: Path):
        with pytest.raises(DeckSignatureError):
            resolve_primary_deck("whatever.log", primary_deck="NoSuchDeck", decks_dir=decks_dir)

    def test_inferred_deck_missing_dck_fails_closed(self, decks_dir: Path):
        """A deck-NAMED log whose list is absent is ambiguous — never skipped."""
        with pytest.raises(DeckSignatureError):
            resolve_primary_deck("mz_unseen_EsperTempo_vs_BGRoots.log", decks_dir=decks_dir)

    def test_no_signature_source_returns_none(self, decks_dir: Path):
        assert resolve_primary_deck("mz_train_smoke.log", decks_dir=decks_dir) is None


# ---------------------------------------------------------------------------
# 2. Parser on a non-UWTempo primary deck
# ---------------------------------------------------------------------------


class TestNonUWTempoParsing:
    def test_parses_and_attributes_hands(self, fixture_log: Path, decks_dir: Path):
        decisions, sessions = parse_log(str(fixture_log), decks_dir=decks_dir)
        assert len(decisions) == 4  # 3 chose-action + 1 recovered pass
        for d in decisions:
            for card in d["hand"]:
                assert card in BGROOTS_NAMES, f"off-deck hand card {card!r}"
        by_turn = {(d["turn"], d["phase"]): d for d in decisions}
        assert by_turn[(1, "PRECOMBAT_MAIN")]["hand"] == [
            "Swamp",
            "Llanowar Elves",
            "Deep-Cavern Bat",
            "Tyvar, Jubilant Brawler",
        ]
        assert by_turn[(1, "PRECOMBAT_MAIN")]["chosen"] == "Play Swamp"
        assert by_turn[(2, "PRECOMBAT_MAIN")]["chosen"] == "Cast Llanowar Elves"
        # Comma-named card survives menu segmentation via the pool vocab.
        assert "Cast Tyvar, Jubilant Brawler" in by_turn[(3, "PRECOMBAT_MAIN")]["menu"]
        assert by_turn[(3, "POSTCOMBAT_MAIN")]["chosen"] == "Pass"
        assert LAST_PARSE_STATS["deck_signature_checked_rows"] == 4

    def test_session_label_uses_resolved_deck_not_uwtempo(self, fixture_log: Path, decks_dir: Path):
        decisions, sessions = parse_log(str(fixture_log), decks_dir=decks_dir)
        assert sessions[0].label == "session0_BGRoots_vs_HighNoonControl"
        assert all(d["session"] == "session0_BGRoots_vs_HighNoonControl" for d in decisions)

    def test_off_deck_hand_fails_closed(self, tmp_path: Path, decks_dir: Path):
        """An opponent card in the primary hand aborts the parse — the #452/#457
        guardrail as a runtime check, parameterized by the .dck, not weakened."""
        polluted = FIXTURE_LOG.replace(
            "Swamp,Llanowar Elves,Deep-Cavern Bat,Tyvar, Jubilant Brawler,",
            "Swamp,Lightning Bolt,Deep-Cavern Bat,Tyvar, Jubilant Brawler,",
        )
        p = tmp_path / "mz_unseen_BGRoots_vs_HighNoonControl.log"
        p.write_text(polluted, encoding="utf-8")
        with pytest.raises(DeckSignatureError) as exc:
            parse_log(str(p), decks_dir=decks_dir)
        assert "Lightning Bolt" in str(exc.value)

    def test_wrong_primary_deck_designation_fails_closed(self, fixture_log: Path, decks_dir: Path):
        """Designating the OPPONENT deck as primary must abort, not mislabel."""
        with pytest.raises(DeckSignatureError):
            parse_log(str(fixture_log), primary_deck="HighNoonControl", decks_dir=decks_dir)

    def test_legacy_log_unchanged(self, tmp_path: Path, decks_dir: Path):
        """No deck names in the filename + no --primary-deck = old behavior."""
        p = tmp_path / "mz_custom.log"
        p.write_text(FIXTURE_LOG, encoding="utf-8")
        decisions, sessions = parse_log(str(p), decks_dir=decks_dir)
        assert len(decisions) == 4
        assert LAST_PARSE_STATS["deck_signature_checked_rows"] == 0
        assert sessions[0].label.startswith("session0_UWTempo_vs_")

    def test_pipeline_plumbs_primary_deck(self):
        import inspect

        from tools.training import run_wp3_pipeline as P

        assert "primary_deck" in inspect.signature(P.run_pipeline).parameters
        assert "decks_dir" in inspect.signature(P.run_pipeline).parameters
        args = P.build_arg_parser().parse_args(["--log", "x.log"])
        assert args.primary_deck is None
        assert args.decks_dir is None


# ---------------------------------------------------------------------------
# 3. Corpus builder: gate-shaped records + permuted twins, fail-closed rules
# ---------------------------------------------------------------------------


def _build(tmp_path: Path, fixture_log: Path, decks_dir: Path, *extra: str) -> Path:
    from tools.training.wp3 import build_unseen_gate_corpus as B

    out_dir = tmp_path / "out"
    fake_train = tmp_path / "fake_train.jsonl"
    if not fake_train.exists():
        fake_train.write_text(json.dumps({"user": "unrelated training prompt"}) + "\n", encoding="utf-8")
    rc = B.main(
        [
            "--log",
            str(fixture_log),
            "--decks-dir",
            str(decks_dir),
            "--out-dir",
            str(out_dir),
            "--min-records",
            "2",
            "--training-corpus",
            str(fake_train),
            *extra,
        ]
    )
    assert rc == 0
    return out_dir


class TestUnseenCorpusBuilder:
    def test_emits_gate_shaped_records(self, tmp_path: Path, fixture_log: Path, decks_dir: Path):
        from tools.training import build_magezero_bridge as BRIDGE

        out_dir = _build(tmp_path, fixture_log, decks_dir)
        identity = out_dir / "gate_unseen_deck_test.jsonl"
        permuted = out_dir / "gate_unseen_deck_test_permuted.jsonl"
        manifest = out_dir / "gate_unseen_deck_manifest.json"
        assert identity.is_file() and permuted.is_file() and manifest.is_file()

        recs = [json.loads(x) for x in identity.read_text(encoding="utf-8").splitlines()]
        # 4 parsed decisions - 1 land drop (excluded by default) = 3
        assert len(recs) == 3
        for r in recs:
            for key in ("id", "system", "user", "max_tokens", "temperature", "meta"):
                assert key in r, f"missing top-level key {key}"
            assert r["system"] == BRIDGE.AUTOPILOT_SYSTEM_PROMPT
            assert "Legal: (pick by number)" in r["user"]
            m = r["meta"]
            assert 1 <= m["gold_pick"] <= m["menu_size"]
            assert m["gold_pick"] in m["gold_equivalent_picks"]
            assert m["menu_size"] == len(m["menu"])
            assert m["deck_seen_in_train"] is False
            assert m["is_land_drop"] is False  # excluded by default
            for row in m["menu"]:
                for key in ("index", "text", "action_key", "action_type", "grp_id", "instance_id", "name"):
                    assert key in row
        golds = {r["meta"]["menu"][r["meta"]["gold_pick"] - 1]["text"] for r in recs}
        assert golds == {"Cast Llanowar Elves", "Cast Deep-Cavern Bat", "Pass"}

    def test_permuted_twins_valid(self, tmp_path: Path, fixture_log: Path, decks_dir: Path):
        out_dir = _build(tmp_path, fixture_log, decks_dir)
        identity = {
            json.loads(x)["id"]: json.loads(x)
            for x in (out_dir / "gate_unseen_deck_test.jsonl").read_text(encoding="utf-8").splitlines()
        }
        perms = [
            json.loads(x)
            for x in (out_dir / "gate_unseen_deck_test_permuted.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert len(perms) == len(identity)
        for p in perms:
            assert p["id"].endswith("#perm")
            twin = identity[p["id"][: -len("#perm")]]
            assert p["meta"]["twin_id"] == twin["id"]
            assert p["meta"]["variant"] == "permuted"
            assert "permutation" in p["meta"]
            # The gold ANSWER is preserved under permutation.
            gold_perm = p["meta"]["menu"][p["meta"]["gold_pick"] - 1]["text"]
            gold_id = twin["meta"]["menu"][twin["meta"]["gold_pick"] - 1]["text"]
            assert gold_perm == gold_id
            # Same options, different order (or forced identical when n small).
            assert sorted(r["text"] for r in p["meta"]["menu"]) == sorted(
                r["text"] for r in twin["meta"]["menu"]
            )

    def test_check_corpora_accepts_the_pair(self, tmp_path: Path, fixture_log: Path, decks_dir: Path):
        """The exact preflight run_b5_gate_eval applies must pass."""
        from tools.training.wp3 import run_b5_gate_eval as R

        out_dir = _build(tmp_path, fixture_log, decks_dir)
        n = R.check_corpora(
            out_dir / "gate_unseen_deck_test.jsonl",
            out_dir / "gate_unseen_deck_test_permuted.jsonl",
        )
        assert n == 3

    def test_manifest_carries_histograms_and_provenance(
        self, tmp_path: Path, fixture_log: Path, decks_dir: Path
    ):
        out_dir = _build(tmp_path, fixture_log, decks_dir)
        m = json.loads((out_dir / "gate_unseen_deck_manifest.json").read_text(encoding="utf-8"))
        assert m["inputs"][0]["primary_deck"] == "BGRoots"
        assert m["inputs"][0]["deck_signature_checked_rows"] == 4
        assert m["composition"]["by_gold_action_type"] == {
            "ActionType_Cast": 2,
            "ActionType_Pass": 1,
        }
        assert m["drops"]["land_drop_excluded"] == 1
        assert "MCTS teacher pick" in m["gold_label_provenance"]

    def test_training_deck_as_primary_fails_closed(self, tmp_path: Path, decks_dir: Path):
        (decks_dir / "UWTempo.dck").write_text("7 [MKM:273] Island\n", encoding="utf-8")
        p = tmp_path / "mz_unseen_UWTempo_vs_BGRoots.log"
        p.write_text(FIXTURE_LOG, encoding="utf-8")
        from tools.training.wp3 import build_unseen_gate_corpus as B

        with pytest.raises(SystemExit) as exc:
            B.main(
                ["--log", str(p), "--decks-dir", str(decks_dir), "--out-dir", str(tmp_path / "o")]
            )
        assert exc.value.code == 2

    def test_unresolvable_deck_fails_closed(self, tmp_path: Path, decks_dir: Path):
        p = tmp_path / "mz_mystery.log"
        p.write_text(FIXTURE_LOG, encoding="utf-8")
        from tools.training.wp3 import build_unseen_gate_corpus as B

        with pytest.raises(SystemExit) as exc:
            B.main(
                ["--log", str(p), "--decks-dir", str(decks_dir), "--out-dir", str(tmp_path / "o")]
            )
        assert exc.value.code == 2

    def test_training_prompt_overlap_fails_closed(
        self, tmp_path: Path, fixture_log: Path, decks_dir: Path
    ):
        """A rendered prompt found in a training corpus must abort the build."""
        from tools.training.wp3 import build_unseen_gate_corpus as B

        out_dir = _build(tmp_path, fixture_log, decks_dir)
        rec = json.loads(
            (out_dir / "gate_unseen_deck_test.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        leaky_train = tmp_path / "leaky_train.jsonl"
        leaky_train.write_text(json.dumps({"user": rec["user"]}) + "\n", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            B.main(
                [
                    "--log",
                    str(fixture_log),
                    "--decks-dir",
                    str(decks_dir),
                    "--out-dir",
                    str(tmp_path / "out2"),
                    "--min-records",
                    "2",
                    "--training-corpus",
                    str(leaky_train),
                ]
            )
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# 4. run_b5_gate_eval generalization wiring
# ---------------------------------------------------------------------------


class TestGeneralizationWiring:
    def test_flag_defaults_to_none(self):
        """Absent flag = the option contributes nothing (additive guarantee)."""
        from tools.training.wp3 import run_b5_gate_eval as R

        args = R.build_parser().parse_args([])
        assert args.unseen_corpus is None
        assert args.unseen_permuted_corpus is None

    def test_bootstrap_gap_degenerate_cases(self):
        from tools.training.wp3 import run_b5_gate_eval as R

        # Candidate perfect on seen, zero on unseen; baseline perfect on both.
        seen = [(1, 1)] * 40
        unseen = [(0, 1)] * 40
        raw_ci, adj_ci = R._bootstrap_generalization(seen, unseen, bootstraps=200)
        assert raw_ci == [1.0, 1.0]
        assert adj_ci == [1.0, 1.0]

    def test_bootstrap_gap_zero_when_identical(self):
        from tools.training.wp3 import run_b5_gate_eval as R

        pairs = [(1, 0), (0, 1), (1, 1), (0, 0)] * 10
        raw_ci, adj_ci = R._bootstrap_generalization(pairs, list(pairs), bootstraps=500)
        assert raw_ci[0] <= 0.0 <= raw_ci[1]
        assert adj_ci[0] <= 0.0 <= adj_ci[1]

    def test_compute_generalization_end_to_end(
        self, tmp_path: Path, fixture_log: Path, decks_dir: Path
    ):
        """Score the builder's real output through the same scorer the gate
        uses (gate_play_decisions.evaluate) via compute_generalization."""
        from tools.training.wp3 import run_b5_gate_eval as R

        out_dir = _build(tmp_path, fixture_log, decks_dir)
        corpus = out_dir / "gate_unseen_deck_test.jsonl"
        perm_corpus = out_dir / "gate_unseen_deck_test_permuted.jsonl"
        recs = [json.loads(x) for x in corpus.read_text(encoding="utf-8").splitlines()]
        perm_recs = [json.loads(x) for x in perm_corpus.read_text(encoding="utf-8").splitlines()]

        cand, base = "cand-label", "base-label"

        def wrong_pick(meta: dict) -> int:
            for i in range(1, meta["menu_size"] + 1):
                if i not in meta["gold_equivalent_picks"]:
                    return i
            raise AssertionError("no wrong pick exists")

        def write_resp(path: Path, rows: list[tuple[str, str, int]]) -> Path:
            with open(path, "w", encoding="utf-8") as f:
                for pid, backend, pick in rows:
                    f.write(
                        json.dumps(
                            {
                                "prompt_id": pid,
                                "backend": backend,
                                "response": json.dumps({"actions": [{"pick": pick}]}),
                            }
                        )
                        + "\n"
                    )
            return path

        # Seen slice: candidate and baseline both answer gold (acc 1.0 each).
        seen_cand = write_resp(
            tmp_path / "seen_cand.jsonl", [(r["id"], cand, r["meta"]["gold_pick"]) for r in recs]
        )
        seen_base = write_resp(
            tmp_path / "seen_base.jsonl", [(r["id"], base, r["meta"]["gold_pick"]) for r in recs]
        )
        # Unseen slice: candidate always wrong (acc 0.0), baseline gold (1.0).
        unseen_cand = write_resp(
            tmp_path / "unseen_cand.jsonl", [(r["id"], cand, wrong_pick(r["meta"])) for r in recs]
        )
        unseen_base = write_resp(
            tmp_path / "unseen_base.jsonl", [(r["id"], base, r["meta"]["gold_pick"]) for r in recs]
        )
        unseen_perm = write_resp(
            tmp_path / "unseen_perm.jsonl",
            [(r["id"], cand, r["meta"]["gold_pick"]) for r in perm_recs],
        )

        g = R.compute_generalization(
            seen_corpus_path=corpus,
            seen_cand_resp=seen_cand,
            seen_base_resp=seen_base,
            unseen_corpus_path=corpus,
            unseen_cand_resp=unseen_cand,
            unseen_base_resp=unseen_base,
            unseen_perm_corpus_path=perm_corpus,
            unseen_perm_resp=unseen_perm,
            cand_label=cand,
            base_label=base,
            bootstraps=200,
        )
        assert g["seen"]["candidate_overall"] == 1.0
        assert g["unseen"]["candidate_overall"] == 0.0
        assert g["seen"]["baseline_overall"] == 1.0
        assert g["unseen"]["baseline_overall"] == 1.0
        assert g["gap_candidate_seen_minus_unseen"] == 1.0
        assert g["gap_bootstrap_95ci"] == [1.0, 1.0]
        assert g["baseline_adjusted_gap"] == 1.0
        assert g["baseline_adjusted_gap_95ci"] == [1.0, 1.0]
        # Permuted arm answered its permuted gold: permutation gap = 0 - 1.
        assert g["unseen"]["candidate_permuted_overall"] == 1.0
        assert g["unseen"]["permutation_gap_candidate"] == -1.0

    def test_compute_generalization_fails_closed_on_mismatched_responses(
        self, tmp_path: Path, fixture_log: Path, decks_dir: Path
    ):
        from tools.training.wp3 import run_b5_gate_eval as R

        out_dir = _build(tmp_path, fixture_log, decks_dir)
        corpus = out_dir / "gate_unseen_deck_test.jsonl"
        perm_corpus = out_dir / "gate_unseen_deck_test_permuted.jsonl"
        empty = tmp_path / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            R.compute_generalization(
                seen_corpus_path=corpus,
                seen_cand_resp=empty,
                seen_base_resp=empty,
                unseen_corpus_path=corpus,
                unseen_cand_resp=empty,
                unseen_base_resp=empty,
                unseen_perm_corpus_path=perm_corpus,
                unseen_perm_resp=empty,
                cand_label="c",
                base_label="b",
                bootstraps=50,
            )
        assert exc.value.code == 2
