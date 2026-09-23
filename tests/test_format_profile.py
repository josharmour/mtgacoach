"""Unit tests for Format Dispatcher, Commander Tax tracking, and Format-Specific Evaluator Config."""

from arenamcp.format_profile import (
    FormatEvaluatorConfig,
    FormatProfile,
    detect_format_profile,
)
from arenamcp.gamestate import GameObject, GameState, Zone, ZoneType


def test_detect_format_brawl_from_connect_resp():
    deck_cards = [1000 + i for i in range(99)]
    commander_ids = [9999]
    profile = detect_format_profile(
        {},
        event_name="Brawl_Ladder",
        deck_cards=deck_cards,
        commander_grp_ids=commander_ids,
    )

    assert profile.family == "brawl"
    assert profile.deck_size == 99
    assert profile.singleton is True
    assert profile.starting_life == 25
    assert profile.has_command_zone is True
    assert profile.commander_grp_ids == (9999,)
    assert profile.confidence >= 0.90
    assert "Brawl" in profile.format_summary()


def test_detect_format_limited_from_deck_size():
    deck_cards = [1000 + i for i in range(40)]
    profile = detect_format_profile(
        {},
        event_name="PremierDraft_MSH_20260623",
        deck_cards=deck_cards,
    )

    assert profile.family == "limited"
    assert profile.variant == "draft"
    assert profile.deck_size == 40
    assert profile.singleton is False
    assert profile.starting_life == 20
    assert profile.has_command_zone is False


def test_detect_format_constructed_standard():
    deck_cards = [1000 + i for i in range(60)]
    profile = detect_format_profile(
        {},
        event_name="Standard_Ladder",
        deck_cards=deck_cards,
    )

    assert profile.family == "constructed"
    assert profile.deck_size == 60
    assert profile.starting_life == 20
    assert profile.has_command_zone is False


def test_format_evaluator_config_brawl():
    profile = FormatProfile(
        family="brawl",
        variant="historic_brawl",
        deck_size=99,
        singleton=True,
        starting_life=25,
        has_command_zone=True,
    )
    config = FormatEvaluatorConfig.from_profile(profile)

    assert config.life_norm == 25
    assert config.lethal_zone == 8
    assert config.board_norm == 12
    assert config.commander_tax_step == 2
    assert config.commander_recast_value == 0.05
    assert config.token_synergy_weight == 1.0


def test_live_commander_cast_tracking():
    gs = GameState()
    cmd_zone = Zone(zone_id=10, zone_type=ZoneType.COMMAND, object_instance_ids=[101])
    stack_zone = Zone(zone_id=20, zone_type=ZoneType.STACK, object_instance_ids=[])
    gs.zones[10] = cmd_zone
    gs.zones[20] = stack_zone

    cmd_obj = GameObject(
        instance_id=101,
        grp_id=55555,
        zone_id=10,
        owner_seat_id=1,
    )
    gs.game_objects[101] = cmd_obj
    gs.commander_grp_ids = [55555]

    # Initial snapshot: casts should be 0
    gs.publish_snapshot()
    snap = gs.get_snapshot()
    cmd_cards = snap["zones"]["command"]
    assert len(cmd_cards) == 1
    assert cmd_cards[0].get("commander_casts") == 0

    # Commander cast: moves from COMMAND (zone 10) to STACK (zone 20)
    gs._update_game_object({"instanceId": 101, "grpId": 55555, "zoneId": 20})

    assert gs.commander_casts[55555] == 1

    # Snapshot should reflect 1 cast
    gs.publish_snapshot()
    snap2 = gs.get_snapshot()
    assert snap2.get("commander_casts", {}).get(55555) == 1

    # Second cast: returns to command (zone 10) then cast again to zone 20
    gs._update_game_object({"instanceId": 101, "grpId": 55555, "zoneId": 10})
    gs._update_game_object({"instanceId": 101, "grpId": 55555, "zoneId": 20})

    assert gs.commander_casts[55555] == 2
