"""Tests for the MageZero combat micro-decision adapter.

Two fixture families:

1. ``fixture_combat_interleaved.log`` — a REAL, unmodified excerpt of
   mz_train_smoke.log (global lines 2270-2530) in which all six XMage threads
   interleave. It contains exactly one attack micro-decision (thread-1,
   Skrelv, Defector Mite, MCTS pool [false 104 / true 867]) and one block
   micro-decision (thread-5, Malcolm declining to block Razorkin Needlehead /
   Emberheart Challenger), plus a spell-targeting 'Targeting' line (thread-6)
   that must NOT be mistaken for a block, plus priority pools on other
   threads that must not cross-pair. Expected values below were read off the
   raw log by hand.

2. Synthetic orphan fixtures proving the scanner fails closed: unpaired
   resolutions, missing openers, missing board context, cross-game context
   and cross-thread interference each produce a counted drop, never a record.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for p in (str(REPO / "src"), str(REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)

from tools.training import magezero_combat_micro as C  # noqa: E402

FIXTURE = REPO / "tools" / "training" / "wp3" / "fixtures" / "fixture_combat_interleaved.log"

EM = "ComputerPlayerMCTS.printBattlefieldScore"


# ---------------------------------------------------------------------------
# Synthetic-line helpers (shape byte-compatible with the real log)
# ---------------------------------------------------------------------------


def L(text: str, thread: int, method: str) -> str:
    return f"INFO  2026-07-28 21:34:00,000 {text}  =>[pool-3-thread-{thread}] {method} \n"


def bf_block(thread: int, hand: str, self_perms: str, opp_perms: str) -> list[str]:
    return [
        L("[PlayerA], life = 20", thread, EM),
        L(f"-> Hand: [{hand}]", thread, EM),
        L(f"-> Permanents: [{self_perms}]", thread, EM),
        L(f"-> Permanents: [{opp_perms}]", thread, EM),
    ]


def attack_seq(thread: int, name: str, verdict: str) -> list[str]:
    return [
        L(f"base choose use attack with: {name}?", thread, "ComputerPlayerMCTS.chooseUse"),
        L(
            "DECLARE_ATTACKERS0pool= actions: [false score: 0.013 count: 104] [true score: 0.147 count: 867]",
            thread,
            "MCTSNode.bestChild",
        ),
        L(f"use attack with: {name}?: {verdict}", thread, "ComputerPlayerMCTS.chooseUse"),
    ]


def scan(tmp_path: Path, lines: list[str]):
    p = tmp_path / "synthetic.log"
    p.write_text("".join(lines), encoding="utf-8")
    rows, acct = C.parse_combat_log(str(p))
    C.verify_accounting(acct)
    return rows, acct


# ---------------------------------------------------------------------------
# 1. Real interleaved excerpt
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real():
    rows, acct = C.parse_combat_log(str(FIXTURE))
    C.verify_accounting(acct)
    return rows, acct


def test_real_fixture_yield(real):
    rows, acct = real
    assert len(rows) == 2
    kinds = sorted(r["decision_kind"] for r in rows)
    assert kinds == ["attack_commit", "block_assign"]


def test_real_attack_record_matches_raw_log(real):
    rows, _ = real
    (atk,) = [r for r in rows if r["decision_kind"] == "attack_commit"]
    assert atk["_thread"] == "pool-3-thread-1"
    assert atk["creature"] == "Skrelv, Defector Mite"
    assert atk["attack_committed"] is True
    assert atk["chosen"] == "Attack with Skrelv, Defector Mite"
    assert atk["mcts_counts"] == {"false": 104, "true": 867}
    assert atk["mcts_scores"] == {"false": 0.013, "true": 0.147}
    # Board owner check (#452's board-swap class): thread-1's OWN board holds
    # Skrelv + Seachrome Coast + Island; the OPPONENT board is two Mountains.
    self_names = [e["name"] for e in atk["battlefield_self"]]
    opp_names = [e["name"] for e in atk["battlefield_opp"]]
    assert self_names == ["Seachrome Coast", "Skrelv, Defector Mite", "Island"]
    assert opp_names == ["Mountain", "Mountain"]
    assert atk["provenance"]["creature_on_board"] is True
    assert atk["hand"] == [
        "Negate",
        "Soul Partition",
        "Skrelv, Defector Mite",
        "Meticulous Archive",
    ]
    assert atk["turn"] == 4
    assert atk["active_life"] == 20 and atk["opp_life"] == 20
    # provenance line numbers, checked against the fixture file itself
    prov = atk["provenance"]
    lines = FIXTURE.read_text(encoding="utf-8").splitlines()
    assert "use attack with: Skrelv, Defector Mite?: true" in lines[prov["line_resolution"] - 1]
    assert "DECLARE_ATTACKERS0pool=" in lines[prov["line_pool"] - 1]
    assert "base choose use attack with" in lines[prov["line_opener"] - 1]
    assert C.EMITTER_MCTS in lines[prov["line_context"] - 1]


def test_real_block_record_matches_raw_log(real):
    rows, _ = real
    (blk,) = [r for r in rows if r["decision_kind"] == "block_assign"]
    assert blk["_thread"] == "pool-3-thread-5"
    assert blk["creature"] == "Malcolm, Alluring Scoundrel"
    assert blk["blocked_attacker"] is None  # Targeting Stop Choosing
    assert blk["candidates"] == ["Razorkin Needlehead", "Emberheart Challenger"]
    assert blk["chosen"] == "Do not block with Malcolm, Alluring Scoundrel"
    assert blk["mcts_counts"] == {
        "Stop Choosing": 498,
        "Razorkin Needlehead": 35,
        "Emberheart Challenger": 189,
    }
    assert blk["possible_targets"] == 2
    assert blk["possible_targets_matches_pool"] is True
    # Board ownership: PlayerA (UW) holds Islands; PlayerB (mono-R) the
    # Mountains + Razorkin. A swap here is exactly the #452 defect class.
    assert [e["name"] for e in blk["battlefield_self"]] == ["Island", "Island"]
    assert [e["name"] for e in blk["battlefield_opp"]] == [
        "Mountain",
        "Mountain",
        "Razorkin Needlehead",
    ]
    assert blk["turn"] == 5
    assert blk["active_life"] == 17 and blk["opp_life"] == 20


def test_real_fixture_spell_targeting_not_a_block(real):
    _, acct = real
    # thread-6's 'Targeting Sleep-Cursed Faerie' has no block opener: ignored.
    assert acct["seen_targeting_nonblock"] == 1
    assert acct["seen_targeting_block_gated"] == 1


def test_real_fixture_no_cross_thread_pairing(real):
    _, acct = real
    # Other threads run priority pools between thread-1's pool and its
    # resolution; none of them may be consumed by the combat pairing.
    assert acct["seen_pools_noncombat"] > 0
    assert acct["emitted_attack"] == 1
    assert acct["emitted_block"] == 1
    assert acct.get("drop_attack_resolution_without_pool", 0) == 0


# ---------------------------------------------------------------------------
# 2. Synthetic fail-closed fixtures
# ---------------------------------------------------------------------------

CTX1 = bf_block(1, "Negate", "Island; Grizzly Bears", "Mountain")


def test_orphan_resolution_without_pool_drops(tmp_path):
    lines = CTX1 + [
        L("base choose use attack with: Grizzly Bears?", 1, "ComputerPlayerMCTS.chooseUse"),
        L("use attack with: Grizzly Bears?: true", 1, "ComputerPlayerMCTS.chooseUse"),
    ]
    rows, acct = scan(tmp_path, lines)
    assert rows == []
    assert acct["drop_attack_resolution_without_pool"] == 1


def test_opener_name_mismatch_drops(tmp_path):
    lines = CTX1 + [
        L("base choose use attack with: Other Creature?", 1, "ComputerPlayerMCTS.chooseUse"),
        L(
            "DECLARE_ATTACKERS0pool= actions: [false score: 0.1 count: 10] [true score: 0.2 count: 20]",
            1,
            "MCTSNode.bestChild",
        ),
        L("use attack with: Grizzly Bears?: true", 1, "ComputerPlayerMCTS.chooseUse"),
    ]
    rows, acct = scan(tmp_path, lines)
    assert rows == []
    assert acct["drop_attack_opener_name_mismatch"] == 1


def test_attack_without_board_context_drops(tmp_path):
    rows, acct = scan(tmp_path, attack_seq(1, "Grizzly Bears", "true"))
    assert rows == []
    assert acct["drop_attack_no_board_context"] == 1


def test_context_from_previous_game_drops(tmp_path):
    lines = (
        CTX1
        + [L("Player A won the die roll", 1, "ParallelDataGenerator.runSingleGame")]
        + attack_seq(1, "Grizzly Bears", "true")
    )
    rows, acct = scan(tmp_path, lines)
    assert rows == []
    # die roll resets the thread's context entirely
    assert acct["drop_attack_no_board_context"] == 1


def test_cross_thread_pool_is_never_paired(tmp_path):
    # pool arrives on thread 2; resolution on thread 1 must NOT consume it.
    lines = CTX1 + [
        L("base choose use attack with: Grizzly Bears?", 1, "ComputerPlayerMCTS.chooseUse"),
        L(
            "DECLARE_ATTACKERS0pool= actions: [false score: 0.1 count: 10] [true score: 0.2 count: 20]",
            2,
            "MCTSNode.bestChild",
        ),
        L("use attack with: Grizzly Bears?: true", 1, "ComputerPlayerMCTS.chooseUse"),
    ]
    rows, acct = scan(tmp_path, lines)
    assert rows == []
    assert acct["drop_attack_resolution_without_pool"] == 1


def test_intervening_priority_decision_invalidates_pool(tmp_path):
    lines = CTX1 + [
        L("base choose use attack with: Grizzly Bears?", 1, "ComputerPlayerMCTS.chooseUse"),
        L(
            "DECLARE_ATTACKERS0pool= actions: [false score: 0.1 count: 10] [true score: 0.2 count: 20]",
            1,
            "MCTSNode.bestChild",
        ),
        L(
            "[4:Precombat Main:PRECOMBAT_MAIN]chose action:Cast Negate success ratio: 0.5",
            1,
            "ComputerPlayerMCTS.priority",
        ),
        L("use attack with: Grizzly Bears?: true", 1, "ComputerPlayerMCTS.chooseUse"),
    ]
    rows, acct = scan(tmp_path, lines)
    assert rows == []
    assert acct["pool_attack_abandoned_at_priority_decision"] == 1
    assert acct["drop_attack_resolution_without_pool"] == 1


def test_targeting_without_block_opener_is_spell_targeting(tmp_path):
    lines = CTX1 + [L("Targeting Grizzly Bears", 1, "ComputerPlayerMCTS.makeChoice")]
    rows, acct = scan(tmp_path, lines)
    assert rows == []
    assert acct["seen_targeting_nonblock"] == 1


def test_block_targeting_without_pool_drops(tmp_path):
    lines = CTX1 + [
        L(
            "base choose target choose which creature to block for Grizzly Bears",
            1,
            "ComputerPlayerMCTS.makeChoice",
        ),
        L("Targeting Stop Choosing", 1, "ComputerPlayerMCTS.makeChoice"),
    ]
    rows, acct = scan(tmp_path, lines)
    assert rows == []
    assert acct["drop_block_targeting_without_pool"] == 1


def test_block_chosen_not_in_pool_drops(tmp_path):
    lines = CTX1 + [
        L(
            "base choose target choose which creature to block for Grizzly Bears",
            1,
            "ComputerPlayerMCTS.makeChoice",
        ),
        L("possible targets: 1", 1, "ComputerPlayerMCTS.makeChoice"),
        L(
            "DECLARE_BLOCKERS0pool= actions: [Stop Choosing score: -0.1 count: 50] "
            "[Raging Goblin score: -0.2 count: 5]",
            1,
            "MCTSNode.bestChild",
        ),
        L("Targeting Some Other Creature", 1, "ComputerPlayerMCTS.makeChoice"),
    ]
    rows, acct = scan(tmp_path, lines)
    assert rows == []
    assert acct["drop_block_chosen_not_in_pool"] == 1


def test_block_happy_path_and_chosen_attacker(tmp_path):
    lines = CTX1 + [
        L(
            "base choose target choose which creature to block for Grizzly Bears",
            1,
            "ComputerPlayerMCTS.makeChoice",
        ),
        L("possible targets: 1", 1, "ComputerPlayerMCTS.makeChoice"),
        L(
            "DECLARE_BLOCKERS0pool= actions: [Stop Choosing score: -0.1 count: 50] "
            "[Raging Goblin score: 0.4 count: 500]",
            1,
            "MCTSNode.bestChild",
        ),
        L("Targeting Raging Goblin", 1, "ComputerPlayerMCTS.makeChoice"),
    ]
    rows, acct = scan(tmp_path, lines)
    assert acct["emitted_block"] == 1
    (blk,) = rows
    assert blk["blocked_attacker"] == "Raging Goblin"
    assert blk["chosen"] == "Block Raging Goblin with Grizzly Bears"
    assert blk["menu"][-1] == "Do not block with Grizzly Bears"


def test_attack_decline_records_no_attack(tmp_path):
    rows, acct = scan(tmp_path, CTX1 + attack_seq(1, "Grizzly Bears", "false"))
    (atk,) = rows
    assert atk["attack_committed"] is False
    assert atk["chosen"] == "Do not attack with Grizzly Bears"


# ---------------------------------------------------------------------------
# 3. Renderer
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rendered_real():
    rows, _ = C.parse_combat_log(str(FIXTURE))
    out = []
    for r in rows:
        r = dict(r)
        r["outcome"] = "won"
        rec, reason = C.build_combat_record(r)
        assert rec is not None, reason
        out.append(rec)
    return out


def test_render_shapes(rendered_real):
    BRIDGE, _ = C._lazy_imports()
    for rec in rendered_real:
        assert rec["system"] == BRIDGE.AUTOPILOT_SYSTEM_PROMPT
        assert rec["user"].count("Legal: (pick by number)") == 1
        assert rec["source"] == "magezero_combat_micro"
        assert rec["kind"] in ("attack_commit", "block_assign")


def test_render_no_leaks(rendered_real):
    for rec in rendered_real:
        u = rec["user"]
        assert "score:" not in u
        assert "count:" not in u
        assert "Computed optimal" not in u
        # MCTS visit counts from the fixture must not appear anywhere
        for count in ("867", "498", "189"):
            assert count not in u


def test_render_attack_answer(rendered_real):
    (atk,) = [r for r in rendered_real if r["kind"] == "attack_commit"]
    assert atk["response"] == '{"actions": [{"pick": 1}]}'
    assert atk["meta"]["attack_committed"] is True
    assert "1. Attack with Skrelv, Defector Mite" in atk["user"]


def test_render_block_answer_and_opp_turn(rendered_real):
    (blk,) = [r for r in rendered_real if r["kind"] == "block_assign"]
    assert blk["response"] == '{"actions": [{"pick": 3}]}'
    assert blk["meta"]["block_class"] == "no_block"
    # blocking happens on the opponent's turn
    assert "T5 OPP" in blk["user"]


def test_render_unknown_outcome_fails_closed():
    rows, _ = C.parse_combat_log(str(FIXTURE))
    rec, reason = C.build_combat_record(rows[0])  # outcome still "unknown"
    assert rec is None
    assert reason == "outcome_unknown"


# ---------------------------------------------------------------------------
# 4. Histograms and floors
# ---------------------------------------------------------------------------


def _atk(chain: str, committed: bool) -> dict:
    return {
        "decision_kind": "attack_commit",
        "attack_committed": committed,
        "_chain": chain,
        "game_id": chain,
    }


def _blk(blocked) -> dict:
    return {"decision_kind": "block_assign", "blocked_attacker": blocked, "game_id": "g"}


def test_all_in_floor_breach_is_flagged_not_rebalanced():
    rows = [_atk("c1", True), _atk("c1", True), _atk("c2", True), _atk("c3", False)]
    hist = C.combat_histograms(rows)
    assert hist["attack_chains"] == 3
    assert hist["attack_chain_hist"] == {"all_in": 2, "no_attack": 1}
    # combat_histograms rounds shares to 4 decimals
    assert hist["all_in_share"] == pytest.approx(2 / 3, abs=1e-4)
    assert hist["floor_breach_all_in"] is True
    assert hist["floor_breach"] is True


def test_no_block_floor_breach():
    rows = [_blk(None)] * 8 + [_blk("Goblin")] * 2
    hist = C.combat_histograms(rows)
    assert hist["no_block_share"] == pytest.approx(0.8)
    assert hist["floor_breach_no_block"] is True


def test_floors_pass_when_balanced():
    rows = [_atk("c1", True), _atk("c2", False), _atk("c3", False)] + [
        _blk(None),
        _blk("Goblin"),
    ]
    hist = C.combat_histograms(rows)
    assert hist["floor_breach"] is False


# ---------------------------------------------------------------------------
# 5. Pipeline wiring: --include-combat default OFF
# ---------------------------------------------------------------------------


def test_pipeline_flag_default_off_drops_combat_rows():
    from collections import Counter

    from tools.training import run_wp3_pipeline as P

    rows, _ = C.parse_combat_log(str(FIXTURE))
    for r in rows:
        r["outcome"] = "won"
    drops: dict[str, Counter] = {}
    rendered = P.stage_render({"train": rows}, drops, {}, include_combat=False)
    assert rendered["train"] == []
    assert drops["train"]["dk_attack_commit_flag_off"] == 1
    assert drops["train"]["dk_block_assign_flag_off"] == 1


def test_pipeline_flag_on_renders_combat_rows():
    from collections import Counter

    from tools.training import run_wp3_pipeline as P

    rows, _ = C.parse_combat_log(str(FIXTURE))
    for r in rows:
        r["outcome"] = "won"
    drops: dict[str, Counter] = {}
    rendered = P.stage_render({"train": rows}, drops, {}, include_combat=True)
    assert len(rendered["train"]) == 2
    kinds = sorted(rec["kind"] for rec in rendered["train"])
    assert kinds == ["attack_commit", "block_assign"]


def test_cli_default_is_off():
    from tools.training import run_wp3_pipeline as P

    args = P.build_arg_parser().parse_args(["--log", "x.log"])
    assert args.include_combat is False
