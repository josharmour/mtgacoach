from __future__ import annotations

from arenamcp.gamestate import (
    GameObject,
    GameObjectKind,
    GameState,
    Player,
    TurnInfo,
    Zone,
    ZoneType,
)


def _seed(state: GameState) -> None:
    """Populate enough fields to exercise the round-trip."""
    state.match_id = "match-abc"
    state.local_seat_id = 1
    state.turn_info = TurnInfo(
        turn_number=7,
        active_player=1,
        priority_player=2,
        phase="Phase_Main1",
        step="Step_DeclareAttack",
    )
    state.players[1] = Player(
        seat_id=1,
        life_total=15,
        lands_played=1,
        mana_pool={"G": 2, "W": 1},
        team_id=1,
        status="active",
    )
    state.players[2] = Player(
        seat_id=2,
        life_total=12,
        lands_played=2,
        mana_pool={},
        team_id=2,
        status="active",
    )
    state.zones[10] = Zone(
        zone_id=10,
        zone_type=ZoneType.HAND,
        owner_seat_id=1,
        object_instance_ids=[100, 101],
    )
    state.zones[11] = Zone(
        zone_id=11,
        zone_type=ZoneType.BATTLEFIELD,
        owner_seat_id=None,
        object_instance_ids=[200],
    )
    state.game_objects[100] = GameObject(
        instance_id=100,
        grp_id=70123,
        zone_id=10,
        owner_seat_id=1,
        card_types=["Creature"],
        subtypes=["Treefolk"],
        power=3,
        toughness=4,
        is_tapped=False,
        object_kind=GameObjectKind.CARD,
        counters={"+1/+1": 2},
    )
    state.game_objects[200] = GameObject(
        instance_id=200,
        grp_id=80456,
        zone_id=11,
        owner_seat_id=1,
        controller_seat_id=1,
        card_types=["Creature"],
        is_tapped=True,
        is_attacking=True,
        turn_entered_battlefield=5,
        object_kind=GameObjectKind.CARD,
    )
    state.played_cards = {1: [70123, 80456]}
    state.damage_taken = {1: 5, 2: 8}
    state.designations = {1: {"Monarch"}}
    state.legal_actions = ["Attack", "Pass"]
    state.legal_actions_raw = [{"actionType": "Attack", "instanceId": 200}]
    state.pending_decision = "Declare Attackers"
    state.decision_seat_id = 1
    state.decision_context = {"foo": "bar"}
    state._seen_instances.update({100, 200})
    state._untap_prevention.add(200)


def test_checkpoint_round_trip_preserves_core_state() -> None:
    src = GameState()
    _seed(src)

    checkpoint = src.export_checkpoint()
    assert checkpoint["schema_version"] == 1

    dst = GameState()
    assert dst.restore_checkpoint(checkpoint) is True

    assert dst.match_id == "match-abc"
    assert dst.local_seat_id == 1
    assert dst.turn_info.turn_number == 7
    assert dst.turn_info.phase == "Phase_Main1"
    assert dst.turn_info.step == "Step_DeclareAttack"

    assert set(dst.players) == {1, 2}
    assert dst.players[1].life_total == 15
    assert dst.players[1].mana_pool == {"G": 2, "W": 1}
    assert dst.players[2].life_total == 12

    # Enums round-trip correctly through JSON-friendly string values
    assert dst.zones[10].zone_type is ZoneType.HAND
    assert dst.zones[11].zone_type is ZoneType.BATTLEFIELD
    assert dst.zones[10].object_instance_ids == [100, 101]

    assert dst.game_objects[100].grp_id == 70123
    assert dst.game_objects[100].subtypes == ["Treefolk"]
    assert dst.game_objects[100].counters == {"+1/+1": 2}
    assert dst.game_objects[100].object_kind is GameObjectKind.CARD
    assert dst.game_objects[200].is_attacking is True
    assert dst.game_objects[200].turn_entered_battlefield == 5

    assert dst.played_cards == {1: [70123, 80456]}
    assert dst.damage_taken == {1: 5, 2: 8}
    assert dst.designations == {1: {"Monarch"}}
    assert dst.legal_actions == ["Attack", "Pass"]
    assert dst.legal_actions_raw == [{"actionType": "Attack", "instanceId": 200}]
    assert dst.pending_decision == "Declare Attackers"
    assert dst.decision_context == {"foo": "bar"}
    assert 100 in dst._seen_instances
    assert 200 in dst._untap_prevention


def test_restore_rejects_unknown_schema_version() -> None:
    dst = GameState()
    assert dst.restore_checkpoint({"schema_version": 99, "match_id": "x"}) is False


def test_restore_rejects_non_dict_payload() -> None:
    dst = GameState()
    assert dst.restore_checkpoint("not-a-dict") is False  # type: ignore[arg-type]
    assert dst.restore_checkpoint(None) is False  # type: ignore[arg-type]
