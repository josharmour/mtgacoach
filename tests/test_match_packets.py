"""Unit tests for the MatchPacket recorder.

Verifies that the packet recorder correctly captures decisions and chosen options
during gameplay, updates their outcomes via RequestTracker observations, and saves
the final packet file on match boundaries.
"""

from __future__ import annotations

import json

from tests.fakes import TARGET_POLL, TypedBridge, make_engine, planner_with, target_state

import arenamcp.match_packets as mp
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.match_packets import (
    get_current_packet,
    start_match_packet,
    stop_match_packet,
)
from arenamcp.request_tracker import decision_fingerprint


def test_match_packet_lifecycle(tmp_path):
    # Ensure starting/stopping sets global singleton
    assert get_current_packet() is None

    packet = start_match_packet("test_match_123")
    assert get_current_packet() is packet
    assert packet.match_id == "test_match_123"

    # Add a decision
    d = PendingDecision(
        request_id=(1, 2),
        request_type="Mulligan",
        options=(
            DecisionOption("mull:keep", "Keep"),
            DecisionOption("mull:mull", "Mull"),
        ),
    )
    packet.add_decision(d, ["mull:keep"])

    assert len(packet.decisions) == 1
    assert packet.decisions[0]["chosen_options"] == ["mull:keep"]
    assert packet.decisions[0]["outcome"] == "pending"

    # Update outcome
    fp = decision_fingerprint(d)
    packet.update_outcome(fp, "ADVANCED")
    assert packet.decisions[0]["outcome"] == "ADVANCED"

    # Stop/save
    packet.result = "win"
    packet.deck_strategy = "mono_red"
    packet.opponent_name = "Sparky"
    packet.replay_path = "/tmp/mtgacoach_Replay1.rply"

    stopped = stop_match_packet()
    assert stopped is packet
    assert get_current_packet() is None

    saved_path = packet.save(packets_dir=tmp_path)
    assert saved_path is not None
    assert saved_path.exists()

    saved_data = json.loads(saved_path.read_text(encoding="utf-8"))
    assert saved_data["match_id"] == "test_match_123"
    assert saved_data["result"] == "win"
    assert saved_data["deck_strategy"] == "mono_red"
    assert saved_data["opponent_name"] == "Sparky"
    assert saved_data["replay_path"] == "/tmp/mtgacoach_Replay1.rply"
    assert len(saved_data["decisions"]) == 1
    assert saved_data["decisions"][0]["chosen_options"] == ["mull:keep"]
    assert saved_data["decisions"][0]["outcome"] == "ADVANCED"


def test_autopilot_integration_records_packets():
    # Ensure packet is active
    packet = start_match_packet("test_integration_match")

    bridge = TypedBridge(TARGET_POLL)
    planner = planner_with('{"option_ids": ["tgt:2"], "reasoning": "opponent"}')
    eng = make_engine(bridge, planner)

    # Execute decision
    handled = eng._try_typed_decision_path(target_state(known_source=True), "decision_required")
    assert handled is True

    # Verify decision logged in packet
    assert len(packet.decisions) == 1
    assert packet.decisions[0]["chosen_options"] == ["tgt:2"]
    assert packet.decisions[0]["outcome"] == "pending"

    # The autopilot noted the submission; the window closing settles it as ADVANCED.
    eng._request_tracker.observe(None)

    assert packet.decisions[0]["outcome"] == "ADVANCED"
    stop_match_packet()


def test_unfinalized_packet_salvaged_as_abandoned(tmp_path, monkeypatch):
    """A packet whose game-end never fired must not be silently discarded
    when the next match starts — it saves as result='abandoned'."""
    monkeypatch.setattr(mp, "PACKETS_DIR", tmp_path)

    first = start_match_packet("match_A")
    d = PendingDecision(
        request_id=(1, 2),
        request_type="Mulligan",
        options=(DecisionOption("mull:keep", "Keep"),),
    )
    first.add_decision(d, ["mull:keep"])

    # Next match starts without match_A ever being finalized.
    start_match_packet("match_B")

    saved = list(tmp_path.glob("packet_*match_A.json"))
    assert len(saved) == 1
    data = json.loads(saved[0].read_text(encoding="utf-8"))
    assert data["result"] == "abandoned"
    assert len(data["decisions"]) == 1
    stop_match_packet()


def test_opponent_name_comes_from_the_replay_header(tmp_path):
    """The .rply cosmetic header is {Local, Opponent:{ScreenName,...}} — the
    original hook read a nonexistent 'opponentPlayerName' key and silently
    recorded None for every match."""
    from arenamcp.match_history import parse_replay_cosmetics

    header = {
        "Local": {"ScreenName": "armour"},
        "Opponent": {"ScreenName": "Primal", "RankingClass": "Gold"},
        "BattlefieldId": 1,
    }
    replay = tmp_path / "mtgacoach_Replay1.rply"
    replay.write_text("#Version 3\n" + json.dumps(header) + "\n{}\n", encoding="utf-8")
    cosmetics = parse_replay_cosmetics(str(replay))
    assert (cosmetics.get("Opponent") or {}).get("ScreenName") == "Primal"
    not_a_replay = tmp_path / "notes.txt"
    not_a_replay.write_text("hello\n", encoding="utf-8")
    assert parse_replay_cosmetics(str(not_a_replay)) is None


# --- P0-9 (2026-07-05): packets record executor actions; finalized matches never restart.
# Match 1 saved decisions=0 despite 8 bridge submissions; a junk
# decisions=0/result=unknown packet was saved after the server re-surfaced the
# finished match id and recording restarted. (conftest resets the module state.)


def test_executed_actions_recorded_in_packet(tmp_path):
    packet = mp.start_match_packet("m-1")
    packet.add_executed_action("cast_spell", card_name="Talisman of Unity", turn=3, path="GRE bridge")
    packet.add_executed_action("pass_priority", turn=3)
    assert len(packet.decisions) == 2
    packet.result = "win"
    path = packet.save(tmp_path)
    data = json.loads(path.read_text())
    assert len(data["decisions"]) == 2
    exec0 = data["decisions"][0]["executed_action"]
    assert exec0["action_type"] == "cast_spell"
    assert exec0["card_name"] == "Talisman of Unity"
    assert data["decisions"][0]["outcome"] == "executed"


def test_finalized_match_never_restarts_recording(tmp_path):
    packet = mp.start_match_packet("m-2")
    packet.add_executed_action("play_land", card_name="Forest", turn=1)
    packet.result = "loss"
    packet.save(tmp_path)
    mp.stop_match_packet()

    # The server's late "Completed match event for unseen match" re-surfaces
    # the finished id — recording must NOT restart.
    assert mp.start_match_packet("m-2") is None
    assert mp.get_current_packet() is None

    # A genuinely new match still records.
    assert mp.start_match_packet("m-3") is not None


def test_recent_decisions_summarize_options_pick_and_trace():
    from tests.strategic_states import actions_decision

    from arenamcp.match_packets import MatchPacket

    packet = MatchPacket("m1")
    decision = actions_decision(
        [
            ("idx:0", "Play Land: Forest", None, True),
            ("idx:1", "Play Land: Mountain", None, True),
            ("pass", "Pass", None, None),
        ]
    )
    packet.add_decision(
        decision,
        ["idx:0"],
        trace={"policy": "line_search", "reasoning": "follows the best line"},
        explanation="Playing Forest.",
        game_state={"turn": {"turn_number": 5, "phase": "Phase_Main1", "active_player": 2, "step": ""}},
    )
    (summary,) = packet.recent_decisions()
    assert summary["request_type"] == "ActionsAvailable"
    assert summary["chosen"] == ["Play Land: Forest"]
    assert summary["options"] == ["Play Land: Forest", "Play Land: Mountain", "Pass"]
    assert summary["trace"]["policy"] == "line_search"
    assert summary["explanation"] == "Playing Forest."
    assert summary["turn"]["turn_number"] == 5
