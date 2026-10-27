import pytest

from arenamcp.decisions import build_pending_decision


@pytest.mark.parametrize("raw", [False, True])
def test_trigger_stacking_context_survives_without_inventing_identity(raw):
    fields = {"context": "SelectionContext_TriggeredAbility", "optionContext": "OptionContext_Stacking"}
    poll = {
        "has_pending": True,
        "request_type": "SelectN",
        "select_n_ids": [901, 902],
        "select_n_min": 1,
        "select_n_max": 2,
    }
    if raw:
        poll["request_payload"] = fields
    else:
        poll.update(select_n_context=fields["context"], select_n_option_context=fields["optionContext"])
    decision = build_pending_decision(poll, resolve_instance=lambda _: "")
    assert "already-triggered abilities" in decision.prompt_text
    assert [o.option_id for o in decision.options] == ["sel:901", "sel:902"]
    assert all(o.meta["identity_known"] is False for o in decision.options)
    assert (decision.min_select, decision.max_select) == (1, 2)


def test_ordinary_selection_is_not_described_as_trigger_stacking():
    decision = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "SelectN",
            "select_n_ids": [901],
            "select_n_context": "SelectionContext_Discard",
            "select_n_option_context": "OptionContext_Stacking",
        },
        resolve_instance=lambda _: "Forest",
    )
    assert "triggered abilities" not in decision.prompt_text


@pytest.mark.parametrize("lookup_fails", [False, True])
def test_trigger_option_never_uses_colliding_card_namespace(monkeypatch, lookup_fails):
    from arenamcp import server
    from arenamcp.gamestate import GameObject, GameObjectKind, GameState
    from arenamcp.gamestate_decisions import _handle_select_n_req

    state = GameState()
    state.game_objects[901] = GameObject(
        instance_id=901,
        grp_id=122104,
        zone_id=25,
        owner_seat_id=2,
        object_kind=GameObjectKind.ABILITY,
    )
    calls = []

    def ability(grp_id):
        calls.append(grp_id)
        if lookup_fails:
            raise ValueError("missing ability")
        return {"name": "Prosperous Innkeeper ability"}

    monkeypatch.setattr(server, "enrich_ability", ability)
    monkeypatch.setattr(server, "get_card_info", lambda _: pytest.fail("ability ID used as card ID"))
    _handle_select_n_req(
        state,
        {
            "selectNReq": {
                "minSel": 1,
                "maxSel": 1,
                "ids": [901],
                "idType": "IdType_InstanceId",
                "context": "SelectionContext_TriggeredAbility",
                "optionContext": "OptionContext_Stacking",
            }
        },
    )
    assert calls == [122104]
    assert state.decision_context["option_cards"] == [
        "Ability (ID: 122104)" if lookup_fails else "Prosperous Innkeeper ability"
    ]


def test_snapshot_exposes_only_current_offered_ability_objects():
    from arenamcp.gamestate import GameObject, GameObjectKind, GameState

    state = GameState()
    state.game_objects[893] = GameObject(instance_id=893, grp_id=77305, zone_id=28, owner_seat_id=2)
    for iid in [901, 902]:
        state.game_objects[iid] = GameObject(
            instance_id=iid,
            grp_id=122104,
            object_kind=GameObjectKind.ABILITY,
            parent_instance_id=893,
            zone_id=25,
            owner_seat_id=2,
        )
    state.decision_context = {
        "context_raw": "SelectionContext_TriggeredAbility",
        "option_ids": [901, 893, 999],
    }
    with state._state_lock:
        snapshot = state._build_raw_snapshot_locked()
    offered = snapshot["pending_selection_objects"]
    assert [o["instance_id"] for o in offered] == [901]
    assert offered[0]["selection_source_grp_id"] == 77305
    state.decision_context = {}
    with state._state_lock:
        assert state._build_raw_snapshot_locked()["pending_selection_objects"] == []
    assert offered[0]["instance_id"] == 901


def test_typed_prompt_preserves_each_offered_ability_effect():
    from arenamcp.action_planner import ActionPlanner

    prompts = []

    class Backend:
        def complete(self, system, user, **kwargs):
            prompts.append(user)
            return '{"option_ids":["sel:901"],"reasoning":"Resolve the life gain trigger."}'

    decision = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "SelectN",
            "select_n_ids": [901],
            "select_n_min": 1,
            "select_n_max": 1,
            "select_n_context": "SelectionContext_TriggeredAbility",
            "select_n_option_context": "OptionContext_Stacking",
        },
        resolve_instance=lambda _: "Innkeeper ability",
    )
    state = {
        "local_seat_id": 2,
        "turn": {"turn_number": 10, "active_player": 2, "phase": "Phase_Main1"},
        "players": [{"seat_id": 2, "is_local": True, "life_total": 4}],
        "pending_selection_objects": [
            {
                "instance_id": 901,
                "object_kind": "ABILITY",
                "name": "Innkeeper ability",
                "oracle_text": "Whenever another creature enters under your control, you gain 1 life.",
            },
            {
                "instance_id": 902,
                "object_kind": "ABILITY",
                "name": "Unrelated trigger",
                "oracle_text": "UNRELATED_EFFECT_MUST_NOT_APPEAR",
            },
        ],
    }
    planner = ActionPlanner(Backend(), timeout=1)
    assert planner.plan_decision_options(decision, state) == ["sel:901"]
    assert "OFFERED TRIGGER FACTS:" in prompts[0]
    assert "you gain 1 life" in prompts[0]
    assert "UNRELATED_EFFECT_MUST_NOT_APPEAR" not in prompts[0]
    assert planner._last_decision_reasoning == "Resolve the life gain trigger."


def test_recorded_pending_trigger_survives_snapshot_server_and_typed_prompt(monkeypatch):
    from arenamcp import server
    from arenamcp.action_planner import ActionPlanner
    from arenamcp.gamestate import GameObject, GameObjectKind, GameState
    from arenamcp.gamestate_decisions import _handle_select_n_req

    state = GameState()
    state.local_seat_id = 2
    state.game_objects[893] = GameObject(instance_id=893, grp_id=77305, zone_id=28, owner_seat_id=2)
    state.game_objects[901] = GameObject(
        instance_id=901,
        grp_id=122104,
        zone_id=25,
        owner_seat_id=2,
        object_kind=GameObjectKind.ABILITY,
        parent_instance_id=893,
    )
    enrichment_calls = []

    def enrich(grp_id, source_grp_id=None):
        enrichment_calls.append((grp_id, source_grp_id))
        return {"name": "Innkeeper ability", "oracle_text": "You gain 1 life."}

    monkeypatch.setattr(server, "enrich_ability", enrich)
    monkeypatch.setattr(server, "game_state", state)
    monkeypatch.setattr(server, "watcher", object())
    monkeypatch.setattr(server, "_save_match_state_if_needed", lambda: None)
    monkeypatch.setattr(server, "_get_bridge_overlay", lambda **kwargs: None)
    _handle_select_n_req(
        state,
        {
            "selectNReq": {
                "ids": [901],
                "idType": "IdType_InstanceId",
                "minSel": 1,
                "maxSel": 1,
                "context": "SelectionContext_TriggeredAbility",
                "optionContext": "OptionContext_Stacking",
            }
        },
    )
    with state._state_lock:
        state._published_snapshot = state._build_raw_snapshot_locked()
    snapshot = server.get_game_state()
    assert snapshot["stack"] == []
    assert enrichment_calls[-1] == (122104, 77305)
    objects = snapshot["pending_selection_objects"]
    decision = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "SelectN",
            "select_n_ids": [901],
            "select_n_min": 1,
            "select_n_max": 1,
            "select_n_context": "SelectionContext_TriggeredAbility",
            "select_n_option_context": "OptionContext_Stacking",
        },
        resolve_instance=lambda iid: next(o["name"] for o in objects if o["instance_id"] == iid),
    )
    assert decision.options[0].label == "Innkeeper ability"
    assert decision.options[0].meta["identity_known"] is True
    prompts = []

    class Backend:
        def complete(self, system, user, **kwargs):
            prompts.append(user)
            return '{"option_ids":["sel:901"],"reasoning":"Resolve life gain."}'

    planner = ActionPlanner(Backend(), timeout=1)
    assert planner.plan_decision_options(decision, snapshot) == ["sel:901"]
    assert "You gain 1 life." in prompts[0]
    assert "already-triggered abilities" in prompts[0]
