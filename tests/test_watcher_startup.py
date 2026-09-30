import json

from arenamcp.draftstate import DraftState, create_draft_handler
from arenamcp.watcher import _latest_match_id_from_tail, _startup_anchor_from_tail


def _line_start_offsets(lines: list[str]) -> dict[str, int]:
    offset = 0
    positions: dict[str, int] = {}
    for line in lines:
        positions[line] = offset
        offset += len(f"{line}\n".encode())
    return positions


def test_startup_anchor_prefers_current_match_over_old_draft() -> None:
    lines = [
        '{"EventName":"PremierDraft_TST"}',
        '{"CardsInPack":[11,22,33]}',
        "[UnityCrossThreadLogger] MatchCreated",
        '{"type":"GREMessageType_GameStateMessage","gameStateMessage":{"type":"GameStateType_Diff"}}',
    ]
    content = ("\n".join(lines) + "\n").encode("utf-8")
    offsets = _line_start_offsets(lines)

    start, mode = _startup_anchor_from_tail(
        content,
        start_offset=0,
        file_size=len(content),
    )

    assert start == offsets["[UnityCrossThreadLogger] MatchCreated"]
    assert mode == "active_match"


def test_restart_in_deck_building_replays_completed_course() -> None:
    course = {"InternalEventName": "PickTwoDraft_FRA", "CurrentModule": "DeckSelect", "CardPool": [1, 1, 2]}
    content = (json.dumps(course) + "\n").encode()
    start, mode = _startup_anchor_from_tail(content, start_offset=0, file_size=len(content))
    assert start == 0
    assert mode == "draft_waiting"
    state = DraftState()
    create_draft_handler(state)("course", json.loads(content[start:]))
    assert state.is_building is True
    assert state.picked_cards == [1, 1, 2]


def test_startup_anchor_skips_completed_match_history() -> None:
    lines = [
        "[UnityCrossThreadLogger] MatchCreated",
        '{"type":"GREMessageType_GameStateMessage","gameStateMessage":{"type":"GameStateType_Diff"}}',
        '{"type":"GREMessageType_IntermissionReq"}',
    ]
    content = ("\n".join(lines) + "\n").encode("utf-8")

    start, mode = _startup_anchor_from_tail(
        content,
        start_offset=0,
        file_size=len(content),
    )

    assert start == len(content)
    assert mode == "idle_or_completed"


def test_startup_anchor_uses_current_active_segment_without_boundary() -> None:
    lines = [
        '{"type":"GREMessageType_GameStateMessage","gameStateMessage":{"type":"GameStateType_Diff"}}',
        '{"type":"GREMessageType_GameStateMessage","gameStateMessage":{"type":"GameStateType_Diff"}}',
    ]
    content = ("\n".join(lines) + "\n").encode("utf-8")

    start, mode = _startup_anchor_from_tail(
        content,
        start_offset=0,
        file_size=len(content),
    )

    assert start == 0
    assert mode == "mid_session_active"


def test_startup_anchor_keeps_draft_when_no_match_is_active() -> None:
    lines = [
        '{"EventName":"PremierDraft_TST"}',
        '{"CardsInPack":[11,22,33]}',
    ]
    content = ("\n".join(lines) + "\n").encode("utf-8")
    offsets = _line_start_offsets(lines)

    start, mode = _startup_anchor_from_tail(
        content,
        start_offset=0,
        file_size=len(content),
    )

    assert start == offsets['{"EventName":"PremierDraft_TST"}']
    assert mode == "draft_waiting"


def test_draft_restart_replays_prior_picks_but_not_the_previous_match():
    lines = [
        '{"type":"GREMessageType_GameStateMessage"}',
        '{"EventName":"PickTwoDraft_FRA"}',
        '{"PackCards":"11,22,33,44","SelfPack":1,"SelfPick":1}',
        '{"Pick":{"GrpIds":[11,22]}}',
        '{"PackCards":"55,66","SelfPack":1,"SelfPick":2}',
    ]
    content = ("\n".join(lines) + "\n").encode()
    start, mode = _startup_anchor_from_tail(content, start_offset=0, file_size=len(content))
    assert start == _line_start_offsets(lines)[lines[1]]
    assert mode == "draft_waiting"
    draft = DraftState()
    handle = create_draft_handler(draft)
    for line in content[start:].decode().splitlines():
        handle("draft", json.loads(line))
    assert draft.picked_cards == [11, 22]
    assert draft.cards_in_pack == [55, 66]
    assert draft.picks_per_pack == 2


def test_new_draft_join_limits_replay_to_the_current_draft():
    lines = [
        '{"EventName":"PickTwoDraft_OLD"}',
        '{"PackCards":"11,22","SelfPack":3,"SelfPick":7}',
        "Event_Join",
        '{"EventName":"PickTwoDraft_FRA"}',
        '{"PackCards":"55,66","SelfPack":1,"SelfPick":1}',
    ]
    content = ("\n".join(lines) + "\n").encode()
    start, mode = _startup_anchor_from_tail(content, start_offset=0, file_size=len(content))
    assert start == _line_start_offsets(lines)[lines[3]]
    assert mode == "draft_waiting"


def test_latest_match_id_from_tail_uses_most_recent_match_reference() -> None:
    lines = [
        '{"matchId":"old-match"}',
        '{"type":"GREMessageType_GameStateMessage","gameStateMessage":{"gameInfo":{"matchID":"current-match"}}}',
    ]
    content = ("\n".join(lines) + "\n").encode("utf-8")

    assert _latest_match_id_from_tail(content) == "current-match"
