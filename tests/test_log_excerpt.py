"""Bounded log excerpts for bug reports (2026-10-09: Player.log evidence lost to rotation)."""

from __future__ import annotations

from arenamcp.gamestate_annotations import _action_type_name
from arenamcp.log_excerpt import player_log_excerpt, tail_lines

PLAYER_LOG = """[UnityCrossThreadLogger]10/9/2026 7:43:12 AM: Match to X: GreToClientEvent
{ "transactionId": "a", "greToClientEvent": { "greToClientMessages": [ { "type": "GREMessageType_GameStateMessage" } ] } }
UnityEngine.DebugLogHandler:Internal_Log(LogType, LogOption, String, Object)

[UnityCrossThreadLogger]10/9/2026 7:43:15 AM: X to Match: ClientToGremessage
{
  "payload": {
    "type": "ClientMessageType_EnterSideboardingReq"
  }
}
ArgumentOutOfRangeException: Specified argument was out of the range of valid values.
Parameter name: No Pantry found that has registered type Core.Meta.UI.SceneUITransforms.
  at SharedClientCore.Pantry.GetContainerByType () [0x00000] in <0>:0
  at CardBackSelectorPopup.Init () [0x00000] in <0>:0
  at DisplayItemSleeve.Init (UnityEngine.Transform selectorTransform, System.Boolean isSideboarding) [0x00000] in <0>:0
Some unrelated line
[UnityCrossThreadLogger]Client.SceneChange {"fromSceneName":"None","toSceneName":"EventLanding","initiator":"User","context":"FRA_Trad_Draft"}
SceneLoader:LoadContentInternal(NavContentType, SceneChangeInitiator, String, Boolean, Action, Boolean)
"""


def test_player_log_excerpt_keeps_timestamped_events_and_exception_frames(tmp_path):
    path = tmp_path / "Player.log"
    path.write_text(PLAYER_LOG, encoding="utf-8")
    excerpt = player_log_excerpt(path, tail=3)
    assert excerpt["exists"] is True and excerpt["size_bytes"] == len(PLAYER_LOG.encode())
    events = excerpt["events"]
    assert events[0] == '[10/9/2026 7:43:15 AM] "type": "ClientMessageType_EnterSideboardingReq"'
    assert any("ArgumentOutOfRangeException" in line for line in events)
    assert any(line.strip().startswith("at CardBackSelectorPopup.Init") for line in events)
    assert any("Client.SceneChange" in line and "7:43:15 AM" in line for line in events)
    # The GRE JSON bodies and plain noise are not events.
    assert not any("transactionId" in line or "unrelated" in line for line in events)
    assert len(excerpt["tail"]) == 3 and excerpt["tail"][-1].startswith("SceneLoader:")


def test_player_log_excerpt_reports_a_missing_file_without_raising(tmp_path):
    missing = player_log_excerpt(tmp_path / "nope.log")
    assert missing["exists"] is False
    assert player_log_excerpt(None) == {"path": "", "exists": False}


def test_tail_lines_reads_only_the_end_of_a_large_file(tmp_path):
    path = tmp_path / "big.log"
    path.write_text("".join(f"line {i}\n" for i in range(5000)), encoding="utf-8")
    lines = tail_lines(path, 3, max_bytes=200)
    assert lines == ["line 4997\n", "line 4998\n", "line 4999\n"]
    assert tail_lines(tmp_path / "absent.log", 3) == []


def test_action_type_names_cover_numbers_enum_strings_and_nothing():
    assert _action_type_name(3) == "Play"
    assert _action_type_name("1") == "Cast"
    assert _action_type_name("ActionType_Activate") == "Activate"
    assert _action_type_name([5]) == "Pass"
    assert _action_type_name(None) == "unknown"
    assert _action_type_name(99) == "ActionType_99"
