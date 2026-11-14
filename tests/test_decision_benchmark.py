"""Synthetic fixtures test infrastructure; never count toward the real-case gate."""

import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "benchmark", Path(__file__).parents[1] / "tools/decision_benchmark.py"
)
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


def sample():
    return {
        "match_id": "private-match",
        "install_id": "private-install",
        "result": "win",
        "packet": {
            "opponent_name": "Private Person",
            "decisions": [
                {
                    "state_snapshot": {
                        "version": 1,
                        "state": {
                            "match_id": "private-match",
                            "players": [{"seat_id": 1, "player_name": "Private Person"}],
                            "hand": [{"name": "Fury", "instance_id": 7}],
                        },
                    },
                    "pending_decision": {
                        "request_id": [1, 2],
                        "request_type": "Mulligan",
                        "options": [{"option_id": "mull:keep", "label": "Keep", "payable": None, "meta": {}}],
                    },
                    "chosen_options": ["mull:keep"],
                }
            ],
        },
    }


def test_real_shape_build_privacy_and_grouping(tmp_path):
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(sample()))
    cases, manifest, counts = b.build_cases([path])
    assert counts["eligible"] == 1
    text = json.dumps(cases)
    assert "Private Person" not in text and "private-match" not in text and "private-install" not in text
    assert "Fury" in text and "instance_id" in text
    assert manifest[0]["source"] == str(path)
    other = tmp_path / "copy.json"
    other.write_text(path.read_text())
    duplicated, _, counts = b.build_cases([path, other])
    assert len(duplicated) == 1 and counts["duplicates"] == 1
    assert duplicated[0]["match_group"] == cases[0]["match_group"]


def test_reject_missing_snapshot_and_unmatched_choice(tmp_path):
    bundle = sample()
    bundle["packet"]["decisions"][0]["chosen_options"] = ["absent"]
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle))
    cases, _, counts = b.build_cases([path])
    assert not cases and counts["unmatched_chosen"] == 1


def test_scoring_and_diff():
    assert b.score(["a"], {"acceptable": [["a"]], "blunders": [["b"]]}) == "acceptable"
    assert b.score(["b"], {"acceptable": [["a"]], "blunders": [["b"]]}) == "blunder"
    assert b.score(["c"], {}) == "unlabelled_choice"
    assert b.diff_reports(
        {"results": [{"case_id": "1", "status": "answered", "chosen": ["a"]}]},
        {"results": [{"case_id": "1", "status": "withheld", "chosen": []}]},
    )["changed"] == ["1"]


def test_three_code_layer_cases_without_model(tmp_path):
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(sample()))
    case = b.build_cases([path])[0][0]
    for kind in ["Mulligan", "ActionsAvailable", "OptionalAction"]:
        case["decision"]["request_type"] = kind
        case["snapshot"]["state"].update(
            zones={},
            local_seat_id=1,
            battlefield=[],
            turn={"active_player": 1, "turn_number": 1, "phase": "Phase_Main1"},
        )
        result = b.replay(case)
        assert result["status"] in {"answered", "withheld", "invalid"}
        assert result["seconds"] >= 0


def test_backend_is_fail_closed():
    import pytest

    with pytest.raises(b.ModelRequired):
        b.NoModel().complete("not sent")
    with pytest.raises(b.ModelRequired):
        _ = b.NoModel().unknown_interface


def test_unknown_snapshot_not_reconstructed(tmp_path):
    bundle = sample()
    bundle["packet"]["decisions"][0]["state_snapshot"]["version"] = 999
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle))
    cases, _, counts = b.build_cases([path])
    assert cases == [] and counts["no_v1_snapshot"] == 1


def test_nested_identity_discovery_including_maps_and_short_names(tmp_path):
    bundle = sample()
    bundle["playerName"] = "Amy"
    bundle["packet"]["player_names"] = {"1": "Hidden Opponent"}
    d = bundle["packet"]["decisions"][0]
    d["pending_decision"]["options"][0]["label"] = "Keep against Amy and Hidden Opponent"
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle))
    cases, _, _ = b.build_cases([path])
    assert cases
    text = json.dumps(cases)
    assert "Amy" not in text and "Hidden Opponent" not in text
    assert "Fury" in text


def test_invalid_selections_and_result_are_excluded(tmp_path):
    for chosen in ([], ["mull:keep", "mull:keep"]):
        bundle = sample()
        bundle["packet"]["decisions"][0]["chosen_options"] = chosen
        path = tmp_path / "bundle.json"
        path.write_text(json.dumps(bundle))
        assert b.build_cases([path])[0] == []
    bundle = sample()
    bundle["result"] = "Private Person won"
    path.write_text(json.dumps(bundle))
    assert b.build_cases([path])[0] == []


def test_semantic_identity_collision_and_unknown_schema_fail_closed(tmp_path):
    for kind in ("card_collision", "unchosen_id", "unknown_meta", "numeric_key"):
        bundle = sample()
        bundle["player_id"] = "Secret Account"
        decision = bundle["packet"]["decisions"][0]
        if kind == "card_collision":
            bundle["player_name"] = "Fury"
        elif kind == "unchosen_id":
            decision["pending_decision"]["options"].append(
                {"option_id": "Secret Account", "label": "Other", "payable": None, "meta": {}}
            )
        elif kind == "numeric_key":
            bundle["account_id"] = 12345
            decision["pending_decision"]["options"][0]["meta"]["12345"] = "hidden"
        else:
            decision["pending_decision"]["options"][0]["meta"]["customer_notes"] = "private"
        path = tmp_path / "bundle.json"
        path.write_text(json.dumps(bundle))
        cases, _, counts = b.build_cases([path])
        assert not cases, counts
        assert json.loads(path.read_text()) == bundle


def test_meta_context_cannot_impersonate_card_or_numeric_game_reference(tmp_path):
    for meta in ({"name": "Opaque Customer", "type_line": "Account"}, {"source_id": "customer-private-123"}):
        bundle = sample()
        bundle["packet"]["decisions"][0]["pending_decision"]["options"][0]["meta"] = meta
        path = tmp_path / "bundle.json"
        path.write_text(json.dumps(bundle))
        assert b.build_cases([path])[0] == []


def test_payable_accepts_only_none_or_exact_boolean(tmp_path):
    for value in ({"customer": "Opaque Customer"}, ["private"], "yes", 1, 0):
        bundle = sample()
        bundle["packet"]["decisions"][0]["pending_decision"]["options"][0]["payable"] = value
        path = tmp_path / "bundle.json"
        path.write_text(json.dumps(bundle))
        assert b.build_cases([path])[0] == []


def test_numeric_identity_game_reference_collision_is_rejected(tmp_path):
    for private, reference in (
        (924681, 924681),
        (924681, 924681.0),
        (924681.0, 924681),
        ("924681.0", 924681),
        (924681.0, "924681"),
    ):
        bundle = sample()
        bundle["account_id"] = private
        bundle["packet"]["decisions"][0]["pending_decision"]["options"][0]["meta"] = {"source_id": reference}
        path = tmp_path / "bundle.json"
        path.write_text(json.dumps(bundle))
        cases, _, counts = b.build_cases([path])
        assert not cases, counts


def test_game_step_is_bounded_enum(tmp_path):
    for step, accepted in [
        ("Step_Draw", True),
        ("Step_DeclareAttack", True),
        ("OpaqueCustomer", False),
        ("Step_X", False),
    ]:
        bundle = sample()
        bundle["packet"]["decisions"][0]["state_snapshot"]["state"]["turn"] = {"step": step}
        path = tmp_path / "bundle.json"
        path.write_text(json.dumps(bundle))
        assert bool(b.build_cases([path])[0]) is accepted


def test_observed_public_ladder_formats_preserved_not_free_form(tmp_path):
    for format_name, accepted in [
        ("Ladder", True),
        ("Brawl Ladder", True),
        ("Ladder OpaqueCustomer", False),
        ("QuickDraft BLB 20261107", False),
    ]:
        bundle = sample()
        bundle["packet"]["decisions"][0]["state_snapshot"]["state"]["format_name"] = format_name
        path = tmp_path / "bundle.json"
        path.write_text(json.dumps(bundle))
        cases, _, _ = b.build_cases([path])
        assert bool(cases) is accepted
        if cases:
            assert cases[0]["snapshot"]["state"]["format_name"] == format_name


def test_actual_turn_aliases_and_pending_combat_shape_preserved(tmp_path):
    import copy

    turn = {
        "phase": "Main1",
        "step": "None",
        "stage": "Play",
        "pending_combat_steps": [{"active_player": 2, "turn": 3, "step": "Step_DeclareBlock"}],
    }
    for change, accepted in [
        (None, True),
        ("stage", False),
        ("step", False),
        ("pending_extra", False),
        ("pending_turn", False),
        ("pending_step", False),
    ]:
        current = copy.deepcopy(turn)
        if change in {"stage", "step"}:
            current[change] = "OpaqueCustomer"
        elif change == "pending_extra":
            current["pending_combat_steps"][0]["customer"] = "secret"
        elif change == "pending_turn":
            current["pending_combat_steps"][0]["turn"] = "private"
        elif change == "pending_step":
            current["pending_combat_steps"][0]["step"] = "OpaqueCustomer"
        bundle = sample()
        bundle["packet"]["decisions"][0]["state_snapshot"]["state"]["turn"] = current
        path = tmp_path / "bundle.json"
        path.write_text(json.dumps(bundle))
        cases, _, _ = b.build_cases([path])
        assert bool(cases) is accepted
        if cases:
            assert cases[0]["snapshot"]["state"]["turn"] == current


def test_grouped_schema_builder_preserves_action_graph_and_rejects_collision(tmp_path):
    from tests.test_benchmark_schema import action_structures

    bundle = sample()
    d = bundle["packet"]["decisions"][0]
    d["pending_decision"]["request_type"] = "ActionsAvailable"
    state = d["state_snapshot"]["state"]
    state.update(
        controlled_turn={
            "opponent_controlled_by_you": False,
            "you_controlled_by_opponent": False,
            "deciding_for_opponent": False,
        },
        zones={"library_count": 40, "library_count_source": "log_zone_membership"},
        decision_context={
            "type": "ActionsAvailable",
            "requestType": "ActionsAvailable",
            "requestClass": "ActionsAvailableRequest",
            "sourceId": 2,
            "prompt": {"promptId": 1, "parameters": []},
        },
    )
    meta = {"actionType": "ActionType_Cast", "instanceId": 7, **action_structures()}
    d["pending_decision"]["options"][0]["meta"] = meta
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle))
    cases, _, counts = b.build_cases([path])
    assert len(cases) == 1, counts
    assert cases[0]["decision"]["options"][0]["meta"] == meta
    bundle["account_id"] = 3
    path.write_text(json.dumps(bundle))
    assert b.build_cases([path])[0] == []


def test_counter_unknown_label_preserved_without_semantic_interpretation(tmp_path):
    for counters, accepted in [
        ({"unknown": 2, "Flying": 1}, True),
        ({"PrivateCustomer": 2}, False),
        ({"unknown": "PrivateCustomer"}, False),
    ]:
        bundle = sample()
        bundle["packet"]["decisions"][0]["state_snapshot"]["state"]["hand"][0]["counters"] = counters
        path = tmp_path / "bundle.json"
        path.write_text(json.dumps(bundle))
        cases, _, _ = b.build_cases([path])
        assert bool(cases) is accepted
        if cases:
            assert cases[0]["snapshot"]["state"]["hand"][0]["counters"] == counters


def test_deck_arena_ids_exact_integers_and_identity_collisions(tmp_path):
    for deck, private, accepted in [
        ([42, 43], None, True),
        (["private"], None, False),
        ([True], None, False),
        ([42], 42, False),
    ]:
        bundle = sample()
        bundle["packet"]["decisions"][0]["state_snapshot"]["state"]["deck_cards"] = deck
        if private is not None:
            bundle["account_id"] = private
        path = tmp_path / "bundle.json"
        path.write_text(json.dumps(bundle))
        cases, _, _ = b.build_cases([path])
        assert bool(cases) is accepted
        if cases:
            assert cases[0]["snapshot"]["state"]["deck_cards"] == deck
