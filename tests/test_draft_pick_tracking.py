from arenamcp.draftstate import DraftState, create_draft_handler


def test_duplicate_copies_survive_across_picks_but_repeated_events_do_not():
    state = DraftState(pack_number=1, pick_number=1, cards_in_pack=[101, 102])
    handle = create_draft_handler(state)
    handle("DraftPick", {"GrpIds": [101, 102], "Pick": 1})
    handle("DraftPick", {"GrpIds": [101, 102], "Pick": 1})
    assert state.picked_cards == [101, 102]
    state.pick_number = 2
    state.cards_in_pack = [101, 103]
    handle("DraftPick", {"GrpIds": [101, 103], "Pick": 2})
    assert state.picked_cards == [101, 102, 101, 103]
    assert state.cards_in_pack == []


def test_pick_two_allows_two_copies_of_same_card():
    state = DraftState(pack_number=1, pick_number=1, cards_in_pack=[101, 101, 102])
    create_draft_handler(state)("DraftPick", {"GrpIds": [101, 101], "Pick": 1})
    assert state.picked_cards == [101, 101]
    assert state.cards_in_pack == [102]
    assert state.picks_per_pack == 2


def test_normal_draft_tracks_copies_across_windows():
    state = DraftState(pack_number=1, pick_number=1, cards_in_pack=[101])
    handle = create_draft_handler(state)
    handle("DraftPick", {"GrpId": 101, "Pick": 1})
    handle("DraftPick", {"GrpId": 101, "Pick": 1})
    state.pick_number = 2
    handle("DraftPick", {"GrpId": 101, "Pick": 2})
    assert state.picked_cards == [101, 101]
    state.reset()
    assert not state.pick_history


def test_late_event_name_preserves_picks_collected_after_a_restart():
    state = DraftState(
        is_active=True, cards_in_pack=[30, 40], picked_cards=[10, 10, 20], pack_number=2, pick_number=4
    )
    handler = create_draft_handler(state)
    handler("event", {"EventName": "PickTwoDraft_FRA"})
    assert state.picked_cards == [10, 10, 20]
    assert state.cards_in_pack == [30, 40]
    assert state.pack_number == 2
    assert state.pick_number == 4
    assert state.picks_per_pack == 2


def test_pack_with_embedded_event_name_recovers_format_before_processing_cards():
    state = DraftState()
    handler = create_draft_handler(state)
    handler(
        "event", {"EventName": "PickTwoDraft_FRA", "CardsInPack": [10, 20], "PackNumber": 0, "PickNumber": 0}
    )
    assert state.event_name == "PickTwoDraft_FRA"
    assert state.set_code == "FRA"
    assert state.picks_per_pack == 2
    assert state.cards_in_pack == [10, 20]
