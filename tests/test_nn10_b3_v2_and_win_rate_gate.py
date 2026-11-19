"""Focused tests for Batch 4 NN10 (B3-v2 stratified sampler & W1/M1 win-rate promotion gate)."""

from __future__ import annotations

import copy
import gzip
import hashlib
import importlib.util
import json
import pathlib
import sys

import pytest

from arenamcp.card_facts import client as cf_client

_TOOLS_DIR = pathlib.Path(__file__).resolve().parent.parent / "tools"


def _load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, _TOOLS_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


db = _load_tool("decision_benchmark")
fr = _load_tool("fleet_review")
um = _load_tool("upgrade_metrics")


def _base_case(
    case_id: str,
    *,
    match_group: str = "g1",
    result: str = "loss",
    request_type: str = "ActionsAvailable",
    options: list[dict] | None = None,
    hand: list[dict] | None = None,
    battlefield: list[dict] | None = None,
    stack: list[dict] | None = None,
    players: list[dict] | None = None,
    turn: dict | None = None,
    opponent_hand_count: int = 3,
    decision_context: dict | None = None,
) -> dict:
    opts = (
        options
        if options is not None
        else [
            {
                "option_id": "idx:0",
                "label": "Cast Bear",
                "payable": True,
                "meta": {"actionType": "ActionType_Cast", "instanceId": 10},
            },
            {
                "option_id": "pass",
                "label": "Pass",
                "payable": True,
                "meta": {"actionType": "ActionType_Pass"},
            },
        ]
    )
    state = {
        "local_seat_id": 1,
        "players": players
        or [
            {"seat_id": 1, "is_local": True, "life_total": 20},
            {"seat_id": 2, "is_local": False, "life_total": 20},
        ],
        "turn": turn or {"turn_number": 4, "active_player": 1, "phase": "Phase_Main1", "step": ""},
        "hand": hand
        if hand is not None
        else [
            {
                "instance_id": 10,
                "grp_id": 1001,
                "name": "Grizzly Bears",
                "mana_cost": "{1}{G}",
                "type_line": "Creature — Bear",
                "oracle_text": "",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
            }
        ],
        "battlefield": battlefield if battlefield is not None else [],
        "stack": stack if stack is not None else [],
        "graveyard": [],
        "exile": [],
        "command": [],
        "zones": {"opponent_hand_count": opponent_hand_count},
    }
    if decision_context is not None:
        state["decision_context"] = decision_context
    return {
        "case_id": case_id,
        "match_group": match_group,
        "split": "train",
        "result": result,
        "chosen": ["pass"],
        "snapshot": {"version": 1, "state": state},
        "decision": {
            "request_type": request_type,
            "can_pass": True,
            "min_select": 0,
            "max_select": 1,
            "options": opts,
        },
    }


# ---------------------------------------------------------------------------
# 1. B3_V2_STRATA classification (+ negative controls)
# ---------------------------------------------------------------------------


def test_classify_hard_strata_all_five_positive_and_negative_controls():
    assert len(db.B3_V2_STRATA) == 5

    # Trivial 1-option decision never enters hard strata.
    trivial = _base_case(
        "trivial",
        options=[
            {"option_id": "pass", "label": "Pass", "payable": True, "meta": {"actionType": "ActionType_Pass"}}
        ],
        stack=[{"instance_id": 99, "name": "Opt", "controller_seat_id": 2}],
    )
    assert db.classify_hard_strata(trivial) == []

    # Vanilla baseline: 2 options on empty board -> no hard strata.
    vanilla = _base_case("vanilla")
    assert db.classify_hard_strata(vanilla) == []

    # Stratum 1: stack_sequencing
    stacked = _base_case(
        "s1",
        stack=[
            {
                "instance_id": 99,
                "name": "Cut Down",
                "type_line": "Instant",
                "oracle_text": "Destroy target creature.",
                "controller_seat_id": 2,
            }
        ],
    )
    assert "stack_sequencing" in db.classify_hard_strata(stacked)
    assert "stack_sequencing" not in db.classify_hard_strata(vanilla)

    # Stratum 2: aura_equipment_protection
    aura_case = _base_case(
        "s2",
        hand=[
            {
                "instance_id": 10,
                "name": "Shardmage's Rescue",
                "type_line": "Enchantment — Aura",
                "oracle_text": "Flash\nEnchanted creature has hexproof until end of turn.",
                "controller_seat_id": 1,
            }
        ],
    )
    assert "aura_equipment_protection" in db.classify_hard_strata(aura_case)
    assert "aura_equipment_protection" not in db.classify_hard_strata(vanilla)

    # Stratum 3: card_facts_non_vanilla (vs vanilla record with empty effects/modes)
    non_vanilla_card = {
        "instance_id": 10,
        "name": "Fury",
        "mana_cost": "{3}{R}{R}",
        "type_line": "Creature — Elemental Incarnation",
        "oracle_text": "When Fury enters, it deals 4 damage divided.",
        "controller_seat_id": 1,
    }
    nv_hash = cf_client.hash_for_card(non_vanilla_card)
    assert nv_hash
    facts_case = _base_case("s3", hand=[non_vanilla_card])
    records_with_effect = {
        nv_hash: {"name": "Fury", "rules_hash": nv_hash, "effects": [{"trigger": "ETB"}], "modes": []}
    }
    records_vanilla = {nv_hash: {"name": "Fury", "rules_hash": nv_hash, "effects": [], "modes": []}}
    assert "card_facts_non_vanilla" in db.classify_hard_strata(
        facts_case, records_by_hash=records_with_effect
    )
    assert "card_facts_non_vanilla" not in db.classify_hard_strata(
        facts_case, records_by_hash=records_vanilla
    )

    # Stratum 4: close_combat (combat priority window with creatures on both sides)
    combat_creatures = [
        {"instance_id": 30, "name": "Bear", "type_line": "Creature", "controller_seat_id": 1},
        {"instance_id": 31, "name": "Elf", "type_line": "Creature", "controller_seat_id": 2},
    ]
    combat_case = _base_case(
        "s4",
        request_type="DeclareAttackers",
        battlefield=combat_creatures,
        turn={"turn_number": 4, "active_player": 1, "phase": "Phase_Combat", "step": "Step_DeclareAttack"},
    )
    assert "close_combat" in db.classify_hard_strata(combat_case)
    assert "close_combat" not in db.classify_hard_strata(
        _base_case("noncombat", battlefield=combat_creatures)
    )

    # Stratum 5: open_mana_sweeper_trick (opp >=2 untapped lands + our >=2 creatures vs tapped out)
    two_ours_and_open_lands = [
        {"instance_id": 30, "name": "Bear A", "type_line": "Creature", "controller_seat_id": 1},
        {"instance_id": 31, "name": "Bear B", "type_line": "Creature", "controller_seat_id": 1},
        {
            "instance_id": 40,
            "name": "Island",
            "type_line": "Basic Land — Island",
            "controller_seat_id": 2,
            "is_tapped": False,
        },
        {
            "instance_id": 41,
            "name": "Plains",
            "type_line": "Basic Land — Plains",
            "controller_seat_id": 2,
            "is_tapped": False,
        },
    ]
    tapped_lands = [
        {**c, "is_tapped": True} if c["controller_seat_id"] == 2 else c for c in two_ours_and_open_lands
    ]
    assert "open_mana_sweeper_trick" in db.classify_hard_strata(
        _base_case("s5", battlefield=two_ours_and_open_lands)
    )
    assert "open_mana_sweeper_trick" not in db.classify_hard_strata(
        _base_case("s5-tapped", battlefield=tapped_lands)
    )


# ---------------------------------------------------------------------------
# 2. Stratified sampling, blind privacy stripping & stratum summary
# ---------------------------------------------------------------------------


def test_sample_stratified_determinism_loss_priority_and_blind_privacy():
    pool = []
    # Build 5 cases per stratum across distinct matches and win/loss outcomes + 2 trivial cases.
    for idx in range(6):
        res = "loss" if idx % 2 == 0 else "win"
        pool.append(
            _base_case(
                f"stk-{idx}",
                match_group=f"m-stk-{idx}",
                result=res,
                stack=[{"instance_id": 90, "name": "Opt"}],
            )
        )
        pool.append(
            _base_case(
                f"aur-{idx}",
                match_group=f"m-aur-{idx}",
                result=res,
                hand=[
                    {
                        "instance_id": 10,
                        "name": "Rancor",
                        "type_line": "Enchantment — Aura",
                        "oracle_text": "Enchanted creature gets +2/+0",
                    }
                ],
            )
        )
        pool.append(
            _base_case(
                f"cmb-{idx}",
                match_group=f"m-cmb-{idx}",
                result=res,
                turn={
                    "turn_number": 5,
                    "active_player": 1,
                    "phase": "Phase_Combat",
                    "step": "Step_BeginCombat",
                },
                battlefield=[
                    {"instance_id": 30, "name": "Bear", "type_line": "Creature", "controller_seat_id": 1},
                    {"instance_id": 31, "name": "Elf", "type_line": "Creature", "controller_seat_id": 2},
                ],
            )
        )
    pool.append(
        _base_case(
            "triv-1",
            options=[{"option_id": "pass", "label": "Pass", "meta": {"actionType": "ActionType_Pass"}}],
        )
    )

    sel_a, stats_a = db.sample_stratified_b3_v2(pool, target_size=12, seed=42)
    sel_b, stats_b = db.sample_stratified_b3_v2(list(reversed(pool)), target_size=12, seed=42)
    assert [c["case_id"] for c in sel_a] == [c["case_id"] for c in sel_b]
    assert stats_a == stats_b
    assert len(sel_a) == 12
    assert "triv-1" not in {c["case_id"] for c in sel_a}
    assert stats_a["by_result"].get("loss", 0) >= stats_a["by_result"].get("win", 0)

    # Blind template strips outcomes/picks/match_group and conforms to load_labels.
    snapshot_before = copy.deepcopy(sel_a[0]["snapshot"])
    blinded, template = db.build_blind_label_template(sel_a, corpus_file="blinded.local.json")
    assert len(blinded) == 12
    for item in blinded:
        assert set(item.keys()) == {"case_id", "snapshot", "pending_decision", "strata", "primary_stratum"}
        assert "chosen" not in item and "result" not in item and "match_group" not in item
    # Deep copy independence check
    blinded[0]["snapshot"]["state"]["local_seat_id"] = 99
    assert sel_a[0]["snapshot"] == snapshot_before

    assert template["counts"]["total"] == 12 and template["counts"]["abstain"] == 12
    first_cid = blinded[0]["case_id"]
    assert template["labels"][first_cid]["status"] == "abstain"

    # Leak detection raises UnsafeCase cleanly.
    leaked_key_case = copy.deepcopy(sel_a[0])
    leaked_key_case["snapshot"]["state"]["players"][0]["name"] = "Secret Player"
    with pytest.raises(db.UnsafeCase):
        db.build_blind_label_template([leaked_key_case])


def test_stratum_summary_and_cli_sample_b3_v2(tmp_path):
    # CLI --sample-b3-v2 without --bundles exits cleanly with status 2.
    with pytest.raises(SystemExit) as exc:
        db.main(["--sample-b3-v2", str(tmp_path / "out")])
    assert exc.value.code == 2

    # Build real-schema sanitized gzipped bundle containing 2 hard cases + 1 vanilla case.
    bundle_dir = tmp_path / "bundles"
    bundle_dir.mkdir()
    bundle = {
        "arena_match_id": "secret-match-xyz",
        "result": "loss",
        "packet": {
            "decisions": [
                {
                    "state_snapshot": {
                        "version": 1,
                        "state": {
                            "local_seat_id": 1,
                            "players": [
                                {"seat_id": 1, "is_local": True, "life_total": 12},
                                {"seat_id": 2, "is_local": False, "life_total": 14},
                            ],
                            "turn": {
                                "turn_number": 4,
                                "active_player": 1,
                                "phase": "Phase_Main1",
                                "step": "",
                            },
                            "hand": [
                                {
                                    "instance_id": 10,
                                    "name": "Pacifism Aura",
                                    "type_line": "Enchantment — Aura",
                                    "oracle_text": "Enchant creature",
                                    "controller_seat_id": 1,
                                }
                            ],
                            "battlefield": [],
                            "stack": [
                                {
                                    "instance_id": 99,
                                    "name": "Shock",
                                    "type_line": "Instant",
                                    "oracle_text": "Deals 2 damage.",
                                    "controller_seat_id": 2,
                                }
                            ],
                            "graveyard": [],
                            "exile": [],
                            "command": [],
                            "zones": {"opponent_hand_count": 2},
                        },
                    },
                    "pending_decision": {
                        "request_id": [1, 1],
                        "request_type": "ActionsAvailable",
                        "can_pass": True,
                        "min_select": 0,
                        "max_select": 1,
                        "options": [
                            {
                                "option_id": "idx:0",
                                "label": "Cast Pacifism Aura",
                                "payable": True,
                                "meta": {"actionType": "ActionType_Cast", "instanceId": 10},
                            },
                            {
                                "option_id": "pass",
                                "label": "Pass",
                                "payable": True,
                                "meta": {"actionType": "ActionType_Pass"},
                            },
                        ],
                    },
                    "chosen_options": ["pass"],
                }
            ]
        },
    }
    with gzip.open(bundle_dir / "b1.json.gz", "wt", encoding="utf-8") as fh:
        json.dump(bundle, fh)

    out_dir = tmp_path / "b3-v2"
    db.main(
        [
            "--sample-b3-v2",
            str(out_dir),
            "--bundles",
            str(bundle_dir),
            "--sample-size",
            "10",
            "--sample-seed",
            "7",
        ]
    )

    blinded_path = out_dir / "blinded.local.json"
    labels_path = out_dir / "labels_template.local.json"
    stats_path = out_dir / "sample_stats.local.json"
    manifest_path = out_dir / "private-selected-manifest.local.json"
    assert blinded_path.exists() and labels_path.exists() and stats_path.exists() and manifest_path.exists()

    blinded_text = blinded_path.read_text()
    assert "secret-match-xyz" not in blinded_text
    assert "chosen" not in blinded_text and "match_group" not in blinded_text
    loaded_cases, loaded_sha = db.load_cases_file(blinded_path)
    assert len(loaded_cases) == 1
    tpl = json.loads(labels_path.read_text())
    assert tpl["corpus_sha256"] == loaded_sha == hashlib.sha256(blinded_path.read_bytes()).hexdigest()
    # Since all labels in template default to status="abstain", load_labels yields empty active dict.
    active_labels, _ = db.load_labels(labels_path)
    assert active_labels == {}

    # Stratum summary with active labels tracks blunder rates accurately.
    cid = loaded_cases[0]["case_id"]
    summ = db.stratum_summary(
        loaded_cases,
        [{"case_id": cid, "status": "answered", "chosen": ["pass"]}],
        labels={cid: {"acceptable": [["idx:0"]], "blunders": [["pass"]], "strata": ["stack_sequencing"]}},
    )
    prim = next(iter(summ["by_primary_stratum"].values()))
    assert prim["scored"] == 1 and prim["blunder"] == 1 and prim["blunder_rate"] == 1.0


# ---------------------------------------------------------------------------
# 3. Wilson interval & W1/M1 win-rate promotion gate (fleet_review + upgrade_metrics)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mod", [fr, um])
def test_wilson_and_promotion_gate_boundaries(mod):
    assert mod.BASELINE_WIN_RATE == 0.484
    assert mod.TARGET_WIN_RATE == 0.520
    assert mod.MIN_GATE_MATCHES == 30

    assert mod.wilson_interval(0, 0) == (0.0, 0.0)
    lo0, hi0 = mod.wilson_interval(0, 30)
    assert lo0 == 0.0 and 0.0 < hi0 < 0.2
    lo1, hi1 = mod.wilson_interval(30, 30)
    assert 0.8 < lo1 < 1.0 and hi1 == pytest.approx(1.0)
    low, high = mod.wilson_interval(16, 30)
    assert 0.3 < low < (16 / 30) < high < 0.75

    # Insufficient matches (< 30)
    g_insuff = mod.evaluate_promotion_gate(20, 9)
    assert g_insuff["status"] == "insufficient_matches"
    assert g_insuff["passed"] is False and g_insuff["min_matches_met"] is False

    # Boundary: exact 52.0% (26W/24L) is NOT strictly > 52.0% -> failed
    g_exact = mod.evaluate_promotion_gate(26, 24)
    assert g_exact["status"] == "failed"
    assert (
        g_exact["passed"] is False and g_exact["min_matches_met"] is True and g_exact["win_rate_met"] is False
    )

    # Passing: 16W/14L (53.3%) with safe hop-limit & B3-v2 rates -> passed
    g_pass = mod.evaluate_promotion_gate(16, 14, watchdog_hop_limit_rate=0.01, b3_v2_blunder_rate=0.02)
    assert g_pass["status"] == "passed" and g_pass["passed"] is True
    assert g_pass["delta_vs_baseline"] == pytest.approx((16 / 30) - 0.484)

    # Clock safety regression -> failed
    g_clock = mod.evaluate_promotion_gate(16, 14, watchdog_hop_limit_rate=0.06)
    assert (
        g_clock["status"] == "failed" and g_clock["clock_safety_met"] is False and g_clock["passed"] is False
    )

    # B3-v2 blunder regression -> failed
    g_b3 = mod.evaluate_promotion_gate(16, 14, b3_v2_blunder_rate=0.08)
    assert g_b3["status"] == "failed" and g_b3["b3_v2_met"] is False and g_b3["passed"] is False


def test_fleet_review_and_upgrade_metrics_gate_integration():
    reviews = []
    summaries = []
    # 16 autopilot wins, 14 autopilot losses (53.3%), plus 5 autopilot-off losses
    # which upgrade_metrics must NOT fold into the autopilot promotion gate.
    for i in range(35):
        res = "win" if i < 16 else "loss"
        ap_on = i < 30
        b = {
            "match_id": f"m-{i}",
            "started_at": 1794300000 + i * 60,
            "result": res,
            "settings_subset": {"autopilot_enabled": ap_on},
            "coach_log": {"lines": []},
            "packet": {
                "decisions": [
                    {
                        "pending_decision": {"options": [{"option_id": "idx:0"}]},
                        "chosen_options": ["idx:0"],
                        "trace": {},
                    }
                ]
            },
            "final_state": {
                "turn_number": 7,
                "local_seat_id": 1,
                "players": [{"seat_id": 1, "life_total": 10}, {"seat_id": 2, "life_total": 0}],
            },
        }
        reviews.append(fr.review_bundle(b))
        summaries.append(um.summarize_bundle(b))

    fr_agg = fr.aggregate(reviews)
    fr_json = fr.json_report(fr_agg)
    assert fr_json["win_rate_summary"]["wins"] == 16
    assert fr_json["win_rate_summary"]["losses"] == 19
    assert fr_json["promotion_gate"]["status"] == "failed"  # all 35 matches -> 16/35 = 45.7%
    assert "Win-rate promotion gate" in fr.render_report(fr_agg)

    um_agg = um.aggregate(summaries)
    # Autopilot ON alone has 16W/14L = 53.3% >= 30 matches -> passed!
    assert um_agg["autopilot"]["on"]["wins"] == 16 and um_agg["autopilot"]["on"]["losses"] == 14
    assert um_agg["promotion_gate"]["wins"] == 16
    assert um_agg["promotion_gate"]["losses"] == 14
    assert um_agg["promotion_gate"]["status"] == "passed"
    assert "Promotion gate (autopilot on): passed" in um.render_report(um_agg)


def test_nn10_review_vanilla_cast_mode_loss_interleave_and_ap_off_isolation():
    from arenamcp.card_facts.prompt_render import normalized_scryfall_card
    from arenamcp.card_facts.source_identity import executable_source_hash

    # Finding 1: real compiled vanilla creature (with name="cast" mode) is NOT non-vanilla,
    # while native "{oR}" card is resolved via candidate_fact_hashes (Scryfall-normalized hash).
    bear = {
        "instance_id": 10,
        "name": "Grizzly Bears",
        "mana_cost": "{1}{G}",
        "type_line": "Creature — Bear",
        "oracle_text": "",
        "controller_seat_id": 1,
    }
    shock_native = {
        "instance_id": 10,
        "name": "Shock",
        "mana_cost": "{oR}",
        "type_line": "Instant",
        "oracle_text": "Shock deals 2 damage to any target.",
        "controller_seat_id": 1,
    }
    bear_hash = cf_client.hash_for_card(bear)
    shock_norm = normalized_scryfall_card(shock_native)
    assert shock_norm is not None
    shock_norm_hash = executable_source_hash(shock_norm)
    assert bear_hash and shock_norm_hash and shock_norm_hash != cf_client.hash_for_card(shock_native)

    records = {
        bear_hash: {
            "name": "Grizzly Bears",
            "mana_cost": "{1}{G}",
            "rules_hash": bear_hash,
            "modes": [{"name": "cast", "cost": {"mana": "{1}{G}"}, "cost_text": "{1}{G}"}],
            "effects": [],
            "mana": [],
        },
        shock_norm_hash: {
            "name": "Shock",
            "mana_cost": "{R}",
            "rules_hash": shock_norm_hash,
            "modes": [{"name": "cast", "cost": {"mana": "{R}"}, "cost_text": "{R}"}],
            "effects": [
                {"trigger": "CAST", "effect": {"effect_type": "Damage", "amount": 2, "target": "any_target"}}
            ],
            "mana": [],
        },
    }
    assert "card_facts_non_vanilla" not in db.classify_hard_strata(
        _base_case("bear", hand=[bear]), records_by_hash=records
    )
    extra_cost_records = {
        bear_hash: {
            "name": "Grizzly Bears",
            "mana_cost": "{1}{G}",
            "rules_hash": bear_hash,
            "modes": [{"name": "cast", "cost": {"mana": "{1}{G}"}, "cost_text": "Discard a card"}],
            "effects": [],
            "mana": [],
        }
    }
    assert "card_facts_non_vanilla" in db.classify_hard_strata(
        _base_case("bear-extra", hand=[bear]), records_by_hash=extra_cost_records
    )
    assert "card_facts_non_vanilla" in db.classify_hard_strata(
        _base_case("shock", hand=[shock_native]), records_by_hash=records
    )

    # Finding 7: realistic large pool with both losses & wins preserves win representation under loss_weight.
    large_pool = []
    for i in range(25):
        large_pool.append(
            _base_case(
                f"loss-{i}",
                match_group=f"ml-{i}",
                result="loss",
                stack=[{"instance_id": 99, "name": "Opt"}],
            )
        )
        large_pool.append(
            _base_case(
                f"win-{i}",
                match_group=f"mw-{i}",
                result="win",
                stack=[{"instance_id": 99, "name": "Opt"}],
            )
        )
    selected, stats = db.sample_stratified_b3_v2(large_pool, target_size=20, seed=11, loss_weight=0.7)
    assert len(selected) == 20
    assert stats["by_result"].get("loss", 0) == 14
    assert stats["by_result"].get("win", 0) == 6

    # Finding 10: autopilot=False watchdog hop-limit waits must NOT skew autopilot=True gate rate.
    hop_line = (
        "2026-11-10 08:29:00.000 | WARNING | arenamcp.action_planner | "
        "post-cast priority: hop limit (6) reached; leaving ActionsAvailable to the watchdog"
    )
    ap_on_clean = um.summarize_bundle(
        {
            "match_id": "m-on",
            "started_at": 1794300000,
            "result": "win",
            "settings_subset": {"autopilot_enabled": True},
            "coach_log": {"lines": []},
            "packet": {
                "decisions": [
                    {
                        "pending_decision": {"options": [{"option_id": "idx:0"}]},
                        "chosen_options": ["idx:0"],
                        "trace": {},
                    }
                ]
            },
        }
    )
    ap_off_noisy = um.summarize_bundle(
        {
            "match_id": "m-off",
            "started_at": 1794300100,
            "result": "loss",
            "settings_subset": {"autopilot_enabled": False},
            "coach_log": {"lines": [hop_line]},
            "packet": {
                "decisions": [
                    {
                        "pending_decision": {"options": [{"option_id": "idx:0"}]},
                        "chosen_options": ["idx:0"],
                        "trace": {},
                    }
                ]
            },
        }
    )
    agg_isolated = um.aggregate([ap_on_clean, ap_off_noisy])
    assert agg_isolated["promotion_gate"]["watchdog_hop_limit_rate"] == 0.0
