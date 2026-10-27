from unittest.mock import Mock

from tests.fakes import make_engine

from arenamcp import server


def test_server_enriches_only_observed_library_objects(monkeypatch):
    monkeypatch.setattr(server, "watcher", object())
    monkeypatch.setattr(server, "_save_match_state_if_needed", lambda: None)
    monkeypatch.setattr(server, "_get_bridge_overlay", lambda **kwargs: None)
    monkeypatch.setattr(
        server,
        "game_state",
        Mock(
            get_published_snapshot=lambda **kwargs: {
                "zones": {"library_visible": [{"instance_id": 458, "grp_id": 75553, "object_kind": "CARD"}]}
            }
        ),
    )
    monkeypatch.setattr(server, "_serialize_snapshot_obj", lambda obj: {**obj, "name": "Forest"})
    assert server.get_game_state()["library_visible"] == [
        {"instance_id": 458, "grp_id": 75553, "object_kind": "CARD", "name": "Forest"}
    ]


def test_typed_selection_resolves_observed_library_card():
    state = {"local_seat_id": 1, "library_visible": [{"instance_id": 458, "name": "Forest"}]}
    planner = Mock()
    planner.plan_decision_options.return_value = ["sel:458"]
    engine = make_engine(planner=planner, game_state=state)
    engine._gre_bridge = Mock(connected=True)
    poll = {
        "has_pending": True,
        "request_type": "SelectN",
        "request_class": "SelectNRequest",
        "game_state_id": 1,
        "msg_id": 2,
        "select_n_ids": [458],
        "select_n_min": 1,
        "select_n_max": 1,
    }
    engine._gre_bridge.get_pending_actions.return_value = poll
    engine._try_typed_decision_path(state, "decision_required")
    decision = planner.plan_decision_options.call_args.args[0]
    assert decision.options[0].label == "Forest"
    assert decision.options[0].meta["identity_known"] is True
