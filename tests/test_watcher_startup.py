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

    assert start == offsets['{"CardsInPack":[11,22,33]}']
    assert mode == "draft_waiting"


def test_latest_match_id_from_tail_uses_most_recent_match_reference() -> None:
    lines = [
        '{"matchId":"old-match"}',
        '{"type":"GREMessageType_GameStateMessage","gameStateMessage":{"gameInfo":{"matchID":"current-match"}}}',
    ]
    content = ("\n".join(lines) + "\n").encode("utf-8")

    assert _latest_match_id_from_tail(content) == "current-match"
