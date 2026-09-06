"""Regression tests for the fail-closed COMBAT-decision gate.

The gate's whole job is to refuse a model that has learned a degenerate combat
reflex instead of combat. These tests build synthetic corpora with the same
*shape* as the real ``combat_gate_test.jsonl`` (79% attack / 21% block; 68.7%
of blocks are "no block") and prove the gate catches, specifically:

  * a **no-block reflex** — perfect attacks, never blocks. Scores ~0.94 overall
    and is caught only by G7 (block slice) and G10 (partial-block class).
  * an **all-in reflex** — swing with everyone, every time. Caught by G8 and G9.
  * an **illegal-name emitter** — names creatures that are not on the board.
    Caught by G2, which is absolute.
  * a **menu-order memoriser** — answers with whatever now sits at the
    positions that were correct in the identity corpus. Perfect on identity,
    caught by G3 on the permuted twin.

Plus the fail-closed CLI contract: missing input, partial coverage, backend
error rows, and the fact that no threshold can be loosened from the command
line.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from tools.training import gate_combat_decisions as gcd  # noqa: E402

DATA = REPO / "tools" / "training" / "data"


# ---------------------------------------------------------------------------
# synthetic corpus construction (byte-shaped like build_combat_decisions)
# ---------------------------------------------------------------------------


def _attack_prompt(pool: list[tuple[str, int, int]]) -> str:
    rows = [f"  {i + 1}. Attack with: {n} ({p}/{t})" for i, (n, p, t) in enumerate(pool)]
    rows.append(f"  {len(pool) + 1}. Done (confirm attackers)")
    return (
        "TRIGGER: Declare attackers phase. Choose which creatures attack.\n\n"
        "=== GAME ===\n"
        "Legal: (pick by number)\n" + "\n".join(rows) + "\n"
        "T7 YOUR | Combat | Pri:You\n"
        "Pending decision: Declare Attackers\n"
        "Life: You=15 Opp=17\n"
        "Mana: 5 (sources: W W U U U)\n"
        "On the play.\n"
        "\n"
        "YOUR BOARD:\n"
        + "\n".join(f"  {n} {p}/{t} (Creature — Test)" for (n, p, t) in pool)
        + "\n\nOPP BOARD:\n  (empty)"
    )


def _block_prompt(blockers: list[str], attackers: list[tuple[str, int, int]]) -> str:
    rows = [f"  {i + 1}. Block with: {n}" for i, n in enumerate(blockers)]
    rows.append(f"  {len(blockers) + 1}. Done (confirm blockers)")
    return (
        "TRIGGER: Opponent is attacking. Assign blockers.\n\n"
        "=== GAME ===\n"
        "Legal: (pick by number)\n" + "\n".join(rows) + "\n"
        "T8 OPP | Combat | Pri:You\n"
        "Pending decision: Declare Blockers\n"
        "Life: You=12 Opp=19\n"
        "Mana: 4 (sources: G G G G)\n"
        "On the draw.\n"
        "\n"
        "ATTACKING YOU:\n"
        + "\n".join(f"  {n} {p}/{t} - Some reminder text." for (n, p, t) in attackers)
        + "\n\nYOUR BOARD:\n"
        + "\n".join(f"  {n} 2/2 (Creature — Test)" for n in blockers)
        + "\n\nOPP BOARD:\n  (empty)"
    )


def _attack_record(rid: str, pool, chosen: list[str], *, rank="diamond") -> dict:
    cls = gcd.derive_answer_class("attack", len(pool), len(chosen))
    return {
        "id": rid,
        "kind": "combat_attack",
        "system": "SYS",
        "user": _attack_prompt(pool),
        "response": json.dumps({"actions": [{"action_type": "declare_attackers", "attacker_names": chosen}]}),
        "source": "17lands-replay_data",
        "meta": {
            "answer_class": cls.split(":", 1)[1],
            "rank": rank,
            "expansion": "EOE",
            "solver_agrees": bool(len(chosen) % 2),
            "menu_size": len(pool) + 1,
            "turn": 7,
            "split": "test",
            "variant": "identity",
            "twin_id": f"{rid}-perm",
            "game_id": f"g{rid[-3:]}",
        },
    }


def _block_record(rid: str, blockers, attackers, assignments: dict, *, rank="diamond") -> dict:
    cls = gcd.derive_answer_class("block", len(blockers), len(assignments))
    return {
        "id": rid,
        "kind": "combat_block",
        "system": "SYS",
        "user": _block_prompt(blockers, attackers),
        "response": json.dumps(
            {"actions": [{"action_type": "declare_blockers", "blocker_assignments": assignments}]}
        ),
        "source": "17lands-replay_data",
        "meta": {
            "answer_class": cls.split(":", 1)[1],
            "rank": rank,
            "expansion": "EOE",
            "solver_agrees": bool(len(assignments) % 2),
            "menu_size": len(blockers) + 1,
            "turn": 8,
            "split": "test",
            "variant": "identity",
            "twin_id": f"{rid}-perm",
            "game_id": f"g{rid[-3:]}",
        },
    }


def _permute(rec: dict) -> dict:
    """Shuffle the menu rows, keep the label — what split_combat_gate emits."""
    prefix, rows, done, suffix = gcd.menu_rows(rec["user"])
    order = list(range(len(rows)))
    rng = random.Random(f"perm:{rec['id']}")
    for _ in range(64):
        rng.shuffle(order)
        if order != sorted(order):
            break
    lines = [f"  {i}. {rows[o]}" for i, o in enumerate(order, start=1)] + [f"  {len(rows) + 1}. {done}"]
    out = dict(rec)
    out["id"] = f"{rec['id']}-perm"
    out["user"] = prefix + "\n".join(lines) + "\n" + suffix
    out["meta"] = dict(rec["meta"])
    out["meta"]["variant"] = "permuted"
    out["meta"]["twin_id"] = rec["id"]
    return out


# The block-class mix mirrors the real corpus exactly — 69% no-block, 27%
# partial, 4% block-all — because that mix is what makes G7 and G10
# load-bearing. Blocks are deliberately over-represented relative to the real
# 79/21 kind split so that ``block:partial_block`` clears its 200-sample floor
# and a PASS is reachable at all; the kind ratio affects only G6's weighting.
N_ATTACK = 1200
N_BLOCK = 800


def _corpus() -> list[dict]:
    recs: list[dict] = []
    for i in range(N_ATTACK):
        rank = "mythic" if i % 3 == 0 else "diamond"
        pool = [("Bear", 2, 2), ("Wolf", 3, 1), ("Ogre", 4, 4)][: 2 + (i % 2)]
        pool = [(f"{n}{j}", p, t) for j, (n, p, t) in enumerate(pool)]
        names = [n for (n, _p, _t) in pool]
        bucket = i % 10
        if bucket < 3:  # no attack
            chosen: list[str] = []
        elif bucket < 6:  # all in
            chosen = list(names)
        else:  # proper subset — sometimes the biggest, sometimes not
            chosen = [names[-1]] if i % 2 else names[:1]
        recs.append(_attack_record(f"a{i:04d}", pool, chosen, rank=rank))
    for i in range(N_BLOCK):
        rank = "mythic" if i % 3 == 0 else "diamond"
        blockers = [f"Guard{j}" for j in range(2 + (i % 2))]
        attackers = [("Raider", 3, 3), ("Brute", 5, 5)][: 1 + (i % 2)]
        attackers = [(f"{n}{j}", p, t) for j, (n, p, t) in enumerate(attackers)]
        atk_names = [n for (n, _p, _t) in attackers]
        bucket = i % 100
        if bucket < 69:  # 69% no block — the real corpus is 69.8%
            assignments: dict = {}
        elif bucket < 96:  # partial block
            assignments = {blockers[-1]: atk_names[0]}
        else:  # block all
            assignments = {b: atk_names[j % len(atk_names)] for j, b in enumerate(blockers)}
        recs.append(_block_record(f"b{i:04d}", blockers, attackers, assignments, rank=rank))
    return recs


@pytest.fixture(scope="module")
def corpus_files(tmp_path_factory):
    d = tmp_path_factory.mktemp("combat_gate")
    recs = _corpus()
    identity = d / "corpus.jsonl"
    permuted = d / "corpus_perm.jsonl"
    gcd._write_jsonl(identity, recs)
    gcd._write_jsonl(permuted, [_permute(r) for r in recs])
    return {"corpus": identity, "permuted_corpus": permuted}


def _responses(path: Path, corpus, label: str, attack_policy: str, block_policy: str, identity_gold=None):
    gcd._write_jsonl(
        path,
        gcd.simulate_rows(
            corpus,
            attack_policy,
            block_policy,
            label=label,
            seed=gcd.DEFAULT_SEED,
            identity_gold=identity_gold,
        ),
    )
    return path


def _gate_inputs(tmp_path: Path, files: dict, policy: str, baseline: str = "do_nothing"):
    corpus = gcd.load_corpus(files["corpus"])
    permuted = gcd.load_corpus(files["permuted_corpus"])
    identity_gold = {r.rid: gcd._gold_positions(r) for r in corpus}
    atk, blk = gcd.DRYRUN_POLICIES[policy]
    b_atk, b_blk = gcd.DRYRUN_POLICIES[baseline]
    paths = {
        **files,
        "responses": _responses(tmp_path / "cand.jsonl", corpus, "cand", atk, blk, identity_gold),
        "permuted_responses": _responses(
            tmp_path / "cand_perm.jsonl", permuted, "cand", atk, blk, identity_gold
        ),
        "baseline_responses": _responses(tmp_path / "base.jsonl", corpus, "base", b_atk, b_blk),
        "report": tmp_path / "report.json",
    }
    return paths


def _argv(paths: dict) -> list[str]:
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
        "200",
    ]


def _run(paths: dict, extra: list[str] | None = None) -> tuple[int, dict]:
    code = gcd.main(_argv(paths) + (extra or []))
    return code, json.loads(paths["report"].read_text())


def _failed_gates(report: dict) -> set[str]:
    return {f.split()[0] for f in report["failures"]}


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def test_menu_parsing_reads_names_and_pt():
    rows, kind = gcd.parse_menu(_attack_prompt([("Bear", 2, 2), ("Ogre", 4, 4)]))
    assert kind == "attack"
    assert rows == [("Bear", 2, 2), ("Ogre", 4, 4)]


def test_zero_power_annotation_does_not_break_pt_parsing():
    """2,193 rows in the real corpus carry the ``[0 POWER …]`` suffix."""
    prompt = _attack_prompt([("Wall", 0, 4)]).replace(
        "Attack with: Wall (0/4)", "Attack with: Wall (0/4) [0 POWER — attacking deals 0 damage]"
    )
    rows, _kind = gcd.parse_menu(prompt)
    assert rows == [("Wall", 0, 4)]


def test_attacker_roster_parsing_keeps_pt_and_drops_oracle_text():
    prompt = _block_prompt(["Guard0"], [("Raider0", 3, 3)])
    assert gcd.parse_attackers(prompt) == [("Raider0", 3, 3)]


def test_block_menu_and_attacker_roster_are_different_namespaces():
    rec = gcd.build_score_record(
        _block_record("b1", ["Guard0", "Guard1"], [("Raider0", 3, 3)], {"Guard1": "Raider0"})
    )
    assert rec.pool_names == ["Guard0", "Guard1"]
    assert rec.attacker_names == ["Raider0"]
    assert rec.answer_class == "block:partial_block"


@pytest.mark.parametrize(
    "response,expected_kind",
    [
        ('{"actions":[{"action_type":"declare_attackers","attacker_names":["A"]}]}', "attack"),
        ('```json\n{"actions":[{"action_type":"declare_attackers","attacker_names":[]}]}\n```', "attack"),
        ('{"actions":[{"action_type":"declare_blockers","blocker_assignments":{}}]}', "block"),
        ('{"actions":[{"pick": 2}]}', None),
        ("I would swing with the bear.", None),
        ("", None),
    ],
)
def test_extract_declaration(response, expected_kind):
    kind, _answer, _reason = gcd.extract_declaration(response)
    assert kind == expected_kind


def test_missing_attacker_names_is_unparseable_not_an_empty_attack():
    """ "No attack" is 30% of the corpus — inferring it from a malformed
    response would credit garbage with the single most likely label."""
    kind, _a, reason = gcd.extract_declaration('{"actions":[{"action_type":"declare_attackers"}]}')
    assert kind is None
    assert "attacker_names" in reason


def test_derived_answer_class_disagreeing_with_metadata_is_fatal():
    rec = _attack_record("a1", [("Bear0", 2, 2), ("Wolf1", 3, 1)], ["Bear0"])
    rec["meta"]["answer_class"] = "all_in"
    with pytest.raises(ValueError, match="disagrees with meta.answer_class"):
        gcd.build_score_record(rec)


# ---------------------------------------------------------------------------
# copy-permutation equivalence (imported from build_combat_decisions)
# ---------------------------------------------------------------------------


def test_identical_copies_are_the_same_attack():
    """Two Bears with the same P/T are interchangeable — the model cannot tell
    them apart and neither should the score."""
    rec = gcd.build_score_record(
        _attack_record("a1", [("Bear #1", 2, 2), ("Bear #2", 2, 2), ("Ogre", 4, 4)], ["Bear #1"])
    )
    assert gcd.score_declaration(rec, "attack", ["Bear #2"])["exact"]
    assert not gcd.score_declaration(rec, "attack", ["Ogre"])["exact"]


def test_differently_statted_same_name_creatures_are_not_equivalent():
    rec = gcd.build_score_record(_attack_record("a1", [("Bear #1", 2, 2), ("Bear #2", 4, 4)], ["Bear #1"]))
    assert not gcd.score_declaration(rec, "attack", ["Bear #2"])["exact"]


def test_mirror_block_assignment_among_copies_is_the_same_block():
    rec = gcd.build_score_record(
        _block_record(
            "b1",
            ["Bear #1", "Bear #2"],
            [("Raider0", 3, 3), ("Raider1", 3, 3)],
            {"Bear #1": "Raider0", "Bear #2": "Raider1"},
        )
    )
    assert gcd.score_declaration(rec, "block", {"Bear #2": "Raider1", "Bear #1": "Raider0"})["exact"]
    # Attackers are copies of each other too, so the mirror is equivalent.
    assert gcd.score_declaration(rec, "block", {"Bear #1": "Raider1", "Bear #2": "Raider0"})["exact"]


def test_blocking_the_wrong_attacker_is_not_equivalent():
    rec = gcd.build_score_record(
        _block_record("b1", ["Guard0"], [("Raider0", 3, 3), ("Brute1", 5, 5)], {"Guard0": "Raider0"})
    )
    assert not gcd.score_declaration(rec, "block", {"Guard0": "Brute1"})["exact"]


def test_exact_match_and_jaccard_agree_at_one():
    rec = gcd.build_score_record(
        _attack_record("a1", [("Bear0", 2, 2), ("Wolf1", 3, 1), ("Ogre2", 4, 4)], ["Bear0", "Ogre2"])
    )
    hit = gcd.score_declaration(rec, "attack", ["Ogre2", "Bear0"])
    assert hit["exact"] and hit["jaccard"] == 1.0
    partial = gcd.score_declaration(rec, "attack", ["Bear0"])
    assert not partial["exact"] and partial["jaccard"] == 0.5


def test_two_declines_are_a_perfect_jaccard():
    rec = gcd.build_score_record(_attack_record("a1", [("Bear0", 2, 2), ("Wolf1", 3, 1)], []))
    assert gcd.score_declaration(rec, "attack", [])["jaccard"] == 1.0


# ---------------------------------------------------------------------------
# legality
# ---------------------------------------------------------------------------


def test_name_not_on_the_board_is_illegal():
    rec = gcd.build_score_record(_attack_record("a1", [("Bear0", 2, 2), ("Wolf1", 3, 1)], ["Bear0"]))
    out = gcd.score_declaration(rec, "attack", ["Phantom Nishoba"])
    assert out["parsed"] and not out["legal"] and not out["exact"]


def test_blocking_an_attacker_that_is_not_attacking_is_illegal():
    rec = gcd.build_score_record(_block_record("b1", ["Guard0"], [("Raider0", 3, 3)], {"Guard0": "Raider0"}))
    assert not gcd.score_declaration(rec, "block", {"Guard0": "Someone Else"})["legal"]


def test_using_a_blocker_as_an_attacker_name_is_illegal():
    """The two namespaces are separate: a blocker is not a legal block target."""
    rec = gcd.build_score_record(
        _block_record("b1", ["Guard0", "Guard1"], [("Raider0", 3, 3)], {"Guard0": "Raider0"})
    )
    assert not gcd.score_declaration(rec, "block", {"Guard0": "Guard1"})["legal"]


def test_wrong_action_type_for_the_decision_family_is_illegal():
    rec = gcd.build_score_record(_attack_record("a1", [("Bear0", 2, 2), ("Wolf1", 3, 1)], ["Bear0"]))
    out = gcd.score_declaration(rec, "block", {"Bear0": "Bear0"})
    assert out["parsed"] and not out["legal"]


def test_a_repeated_name_is_one_creature_not_an_illegality():
    rec = gcd.build_score_record(_attack_record("a1", [("Bear0", 2, 2), ("Wolf1", 3, 1)], ["Bear0"]))
    out = gcd.score_declaration(rec, "attack", ["Bear0", "Bear0"])
    assert out["legal"] and out["exact"] and out["duplicate_names"]


# ---------------------------------------------------------------------------
# trivial policies / floors
# ---------------------------------------------------------------------------


def test_random_subset_expectation_counts_copy_equivalent_subsets():
    """Two identical Bears, label = attack with one of them: 2 of the 4
    identity subsets are copy-equivalent to the label."""
    rec = gcd.build_score_record(_attack_record("a1", [("Bear #1", 2, 2), ("Bear #2", 2, 2)], ["Bear #1"]))
    assert gcd.random_expectation(rec) == pytest.approx(0.5)
    all_in = gcd.build_score_record(
        _attack_record("a2", [("Bear #1", 2, 2), ("Bear #2", 2, 2)], ["Bear #1", "Bear #2"])
    )
    assert gcd.random_expectation(all_in) == pytest.approx(0.25)


def test_random_block_expectation_normalises_over_assignments():
    """One blocker, one attacker: block or don't — the label has probability 1/2."""
    rec = gcd.build_score_record(_block_record("b1", ["Guard0"], [("Raider0", 3, 3)], {"Guard0": "Raider0"}))
    assert gcd.random_expectation(rec) == pytest.approx(0.5)


def test_every_named_reflex_answers_both_families():
    """A block reflex evaluated on an attack record must commit nothing rather
    than raise — the per-slice floors are computed on mixed slices."""
    attack = gcd.build_score_record(_attack_record("a1", [("Bear0", 2, 2), ("Wolf1", 3, 1)], []))
    block = gcd.build_score_record(_block_record("b1", ["Guard0", "Guard1"], [("Raider0", 3, 3)], {}))
    for policy in set(gcd.ATTACK_REFLEXES) | set(gcd.BLOCK_REFLEXES):
        if "random" in policy:
            continue
        for rec in (attack, block):
            kind, answer = gcd.policy_declaration(policy, rec, random.Random(0))
            assert kind == rec.kind
            assert gcd.score_declaration(rec, kind, answer)["legal"]


def test_reference_policies_expose_the_no_block_floor(corpus_files):
    corpus = gcd.load_corpus(corpus_files["corpus"])
    refs = gcd.reference_policies(corpus)
    # The corpus is built 69% no-block, mirroring the real 69.8%.
    assert refs["block_slice"]["always_no_block"] == pytest.approx(0.69, abs=0.02)
    assert refs["best_block_reflex"]["policy"] == "always_no_block"
    # G6's floor is the best PAIR, which no single named policy can beat.
    best_combined = refs["best_combined"]["accuracy"]
    assert all(v <= best_combined for v in refs["overall"].values() if v is not None)


def test_do_nothing_extends_to_the_same_policy_under_both_names(corpus_files):
    """Stated in the report rather than hidden: the two 'commit nothing'
    reflexes are one policy overall."""
    refs = gcd.reference_policies(gcd.load_corpus(corpus_files["corpus"]))
    assert refs["overall"]["always_no_attack"] == refs["overall"]["always_no_block"]


# ---------------------------------------------------------------------------
# the four failure modes this gate exists to catch
# ---------------------------------------------------------------------------


def test_gate_passes_an_oracle(corpus_files, tmp_path):
    paths = _gate_inputs(tmp_path, corpus_files, "oracle")
    code, report = _run(paths)
    assert code == 0, report["failures"]
    assert report["verdict"] == "PASS"
    assert report["failures"] == []
    assert report["candidate"]["overall"]["accuracy"] == 1.0
    assert report["candidate"]["illegal_declarations"] == 0
    assert report["permutation_invariance"]["abs_accuracy_gap"] == 0.0


def test_gate_blocks_a_no_block_reflex(corpus_files, tmp_path):
    """Perfect attacks + never blocks. Looks excellent overall; G7 and G10 are
    the only things standing between it and a PASS."""
    paths = _gate_inputs(tmp_path, corpus_files, "no_block")
    code, report = _run(paths)
    assert code == 2
    assert report["verdict"] == "BLOCKED"
    # It really does look good — 0.88 here, 0.94 on the real corpus — and G6,
    # the OVERALL floor, passes. Only the family and class floors catch it.
    assert report["candidate"]["overall"]["accuracy"] > 0.85
    assert _failed_gates(report) == {"G7", "G10"}
    by_id = {c["id"]: c for c in report["criteria"]}
    assert by_id["G6"]["verdict"] == "PASS"
    assert by_id["G8"]["verdict"] == "PASS"
    g7 = next(c for c in report["criteria"] if c["id"] == "G7")
    assert g7["reflex_policy"] == "always_no_block"
    assert g7["margin_observed"] == 0.0


def test_gate_blocks_an_all_in_reflex(corpus_files, tmp_path):
    """Swing with everyone, every time. Caught on the attack slice (G8) and on
    the class the degenerate answers cannot reach (G9)."""
    paths = _gate_inputs(tmp_path, corpus_files, "all_in")
    code, report = _run(paths)
    assert code == 2
    assert {"G8", "G9"} <= _failed_gates(report)
    assert report["candidate"]["by_answer_class"]["attack:proper_subset"]["accuracy"] == 0.0


def test_gate_blocks_an_illegal_name_emitter(corpus_files, tmp_path):
    """G2 is absolute: production refuses a declaration naming a creature that
    is not on the board, so the gate does too."""
    paths = _gate_inputs(tmp_path, corpus_files, "illegal")
    code, report = _run(paths)
    assert code == 2
    assert "G2" in _failed_gates(report)
    assert report["candidate"]["illegal_rate"] == 1.0
    assert report["candidate"]["illegal_declarations"] == report["candidate"]["responses_matched"]
    # Reported separately from accuracy, as the spec requires.
    assert report["candidate"]["overall"]["accuracy"] == 0.0


def test_gate_blocks_a_menu_order_memoriser(corpus_files, tmp_path):
    """Perfect on the identity corpus, wrong on the twin — the label is a NAME
    SET and cannot move under a menu reorder, so any gap is memorisation."""
    paths = _gate_inputs(tmp_path, corpus_files, "memoriser")
    code, report = _run(paths)
    assert code == 2
    assert "G3" in _failed_gates(report)
    perm = report["permutation_invariance"]
    assert perm["identity_accuracy"] == 1.0
    assert perm["permuted_accuracy"] < 1.0
    assert perm["abs_accuracy_gap"] > gcd.MAX_PERMUTATION_ACC_GAP
    # An oracle is invariant; a memoriser's set agreement collapses with it.
    assert perm["set_agreement"] < 1.0


def test_a_single_illegal_name_is_enough_to_block(corpus_files, tmp_path):
    paths = _gate_inputs(tmp_path, corpus_files, "oracle")
    rows = list(gcd._read_jsonl(paths["responses"]))
    rows[0]["response"] = json.dumps(
        {"actions": [{"action_type": "declare_attackers", "attacker_names": ["Phantom Nishoba"]}]}
    )
    gcd._write_jsonl(paths["responses"], rows)
    code, report = _run(paths)
    assert code == 2
    assert "G2" in _failed_gates(report)
    assert report["candidate"]["illegal_declarations"] == 1


# ---------------------------------------------------------------------------
# fail-closed contract
# ---------------------------------------------------------------------------


def test_gate_blocks_on_missing_input(corpus_files, tmp_path):
    paths = _gate_inputs(tmp_path, corpus_files, "oracle")
    paths["permuted_responses"].unlink()
    code, report = _run(paths)
    assert code == 2
    assert any("missing required input" in f for f in report["failures"])


def test_gate_blocks_on_partial_coverage(corpus_files, tmp_path):
    paths = _gate_inputs(tmp_path, corpus_files, "oracle")
    gcd._write_jsonl(paths["responses"], list(gcd._read_jsonl(paths["responses"]))[:400])
    code, report = _run(paths)
    assert code == 2
    assert any("fail closed (no partial gate)" in f for f in report["failures"])


def test_gate_blocks_on_backend_error_rows(corpus_files, tmp_path):
    paths = _gate_inputs(tmp_path, corpus_files, "oracle")
    rows = list(gcd._read_jsonl(paths["responses"]))
    rows[0]["error"] = "HTTP 401"
    rows[0]["response"] = ""
    gcd._write_jsonl(paths["responses"], rows)
    code, report = _run(paths)
    assert code == 2
    assert any("backend errors" in f for f in report["failures"])


def test_gate_blocks_on_a_sample_shortfall(corpus_files, tmp_path):
    """A slice too small to measure is not a slice to gate on — and a corpus
    that cannot fill one is BLOCKED, never silently gated on what is left."""
    small = tmp_path / "small.jsonl"
    small_perm = tmp_path / "small_perm.jsonl"
    recs = list(gcd._read_jsonl(corpus_files["corpus"]))[:300]
    gcd._write_jsonl(small, recs)
    gcd._write_jsonl(small_perm, [_permute(r) for r in recs])
    paths = _gate_inputs(tmp_path, {"corpus": small, "permuted_corpus": small_perm}, "oracle")
    code, report = _run(paths)
    assert code == 2
    assert any("G4 insufficient samples" in f for f in report["failures"])


def test_unparseable_responses_fail_the_schema_gate(corpus_files, tmp_path):
    paths = _gate_inputs(tmp_path, corpus_files, "oracle")
    rows = list(gcd._read_jsonl(paths["responses"]))
    for row in rows[:200]:
        row["response"] = "I would block the big one."
    gcd._write_jsonl(paths["responses"], rows)
    code, report = _run(paths)
    assert code == 2
    assert "G1" in _failed_gates(report)
    assert report["candidate"]["declaration_parse_rate"] < gcd.MIN_SCHEMA_RATE


def test_thresholds_cannot_be_loosened(corpus_files, tmp_path):
    """No bypass flag: a loosening CLI value is ignored and recorded."""
    paths = _gate_inputs(tmp_path, corpus_files, "no_block")
    code, report = _run(
        paths,
        [
            "--min-samples",
            "1",
            "--min-block-samples",
            "1",
            "--min-schema-rate",
            "0.0",
            "--max-permutation-gap",
            "1.0",
            "--max-accuracy-regression",
            "1.0",
            "--min-trivial-margin",
            "-1.0",
            "--min-nondegenerate-delta-ci-lower",
            "-1.0",
        ],
    )
    assert code == 2
    t = report["thresholds"]
    assert t["min_samples"] == gcd.MIN_SAMPLES
    assert t["min_block_samples"] == gcd.MIN_BLOCK_SAMPLES
    assert t["min_schema_rate"] == gcd.MIN_SCHEMA_RATE
    assert t["max_permutation_gap"] == gcd.MAX_PERMUTATION_ACC_GAP
    assert t["max_accuracy_regression"] == gcd.MAX_ACCURACY_REGRESSION
    assert t["min_trivial_margin"] == gcd.MIN_TRIVIAL_POLICY_MARGIN
    assert t["min_nondegenerate_delta_ci_lower"] == gcd.MIN_NONDEGENERATE_DELTA_CI_LOWER
    assert len(t["clamped_cli_overrides"]) == 7
    # Still blocked — the reflex is caught by the floors it tried to disable.
    assert {"G7", "G10"} <= _failed_gates(report)


def test_extra_accuracy_floor_can_only_tighten(corpus_files, tmp_path):
    paths = _gate_inputs(tmp_path, corpus_files, "oracle")
    code, report = _run(paths, ["--min-accuracy", "1.01"])
    assert code == 2
    assert any("--min-accuracy" in f for f in report["failures"])


def test_gate_never_registers(corpus_files, tmp_path):
    """This gate has no promotion path, even on PASS."""
    paths = _gate_inputs(tmp_path, corpus_files, "oracle")
    code, report = _run(paths, ["--register"])
    assert report["verdict"] == "PASS"
    assert code == 2


def test_block_all_class_is_reported_but_not_gated(corpus_files, tmp_path):
    paths = _gate_inputs(tmp_path, corpus_files, "oracle")
    _code, report = _run(paths)
    assert "block:block_all" in gcd.NON_GATED_ANSWER_CLASSES
    assert "block:block_all" in report["candidate"]["by_answer_class"]
    assert any("block:block_all measured but NOT gated" in a for a in report["advisories"])


def test_solver_agreement_is_reported_but_not_gated(corpus_files, tmp_path):
    paths = _gate_inputs(tmp_path, corpus_files, "oracle")
    _code, report = _run(paths)
    assert set(report["candidate"]["by_solver_agreement"]) >= {"True", "False"}
    assert any("solver diagnostic (NOT gated)" in a for a in report["advisories"])
    assert not any(f.startswith("solver") for f in report["failures"])


def test_jaccard_is_reported_on_every_slice(corpus_files, tmp_path):
    paths = _gate_inputs(tmp_path, corpus_files, "no_block")
    _code, report = _run(paths)
    cand = report["candidate"]
    assert cand["overall"]["jaccard_mean"] is not None
    assert all(v["jaccard_mean"] is not None for v in cand["by_kind"].values())
    assert not any("jaccard" in f.lower() for f in report["failures"])


# ---------------------------------------------------------------------------
# recorded dry-run fixtures against the real corpus
# ---------------------------------------------------------------------------

DRYRUN_EXPECTED = {
    "oracle": ("PASS", set()),
    "no_block": ("BLOCKED", {"G7", "G10"}),
    "all_in": ("BLOCKED", {"G8", "G9"}),
    "illegal": ("BLOCKED", {"G2"}),
    "memoriser": ("BLOCKED", {"G3"}),
}


@pytest.mark.parametrize("policy", sorted(DRYRUN_EXPECTED))
def test_recorded_dryrun_verdicts_against_the_real_corpus(policy):
    """The committed reports are the gate's own evidence. If a change to the
    scoring flips one of these, that is the change to justify."""
    path = DATA / f"{gcd.DRYRUN_STEM}_{policy}.json"
    if not path.exists():
        pytest.skip("dry-run fixtures not built (run: gate_combat_decisions dryrun)")
    report = json.loads(path.read_text())
    expected_verdict, expected_gates = DRYRUN_EXPECTED[policy]
    assert report["verdict"] == expected_verdict
    assert expected_gates <= _failed_gates(report)


def test_recorded_no_block_reflex_looks_good_overall():
    """The number that makes G7 load-bearing: a never-blocks model scores 0.94
    overall on the real corpus."""
    path = DATA / f"{gcd.DRYRUN_STEM}_no_block.json"
    if not path.exists():
        pytest.skip("dry-run fixtures not built")
    report = json.loads(path.read_text())
    assert report["candidate"]["overall"]["accuracy"] > 0.9
    assert _failed_gates(report) == {"G7", "G10"}


@pytest.mark.skipif(not (DATA / "combat_gate_test.jsonl").exists(), reason="combat gate corpus not available")
def test_real_corpus_parses_and_matches_its_manifest():
    """Every record in the real test split must be scoreable — a parse failure
    is a corpus bug, and the gate must never quietly skip one."""
    corpus = gcd.load_corpus(DATA / "combat_gate_test.jsonl", limit=2000)
    assert len(corpus) == 2000
    assert {r.kind for r in corpus} <= {"attack", "block"}
    for rec in corpus:
        kind, answer = rec.gold_declaration
        assert gcd.score_declaration(rec, kind, answer)["exact"], rec.rid
