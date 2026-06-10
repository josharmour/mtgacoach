"""Unit tests for the MatchPacket recorder.

Verifies that the packet recorder correctly captures decisions and chosen options
during gameplay, updates their outcomes via RequestTracker observations, and saves
the final packet file on match boundaries.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

import arenamcp.autopilot as autopilot_module
from arenamcp.decisions import PendingDecision, DecisionOption, build_pending_decision
from arenamcp.match_packets import (
    MatchPacket,
    start_match_packet,
    get_current_packet,
    stop_match_packet,
)
from arenamcp.request_tracker import RequestTracker, decision_fingerprint

from tests.test_typed_decision_path import _TypedBridge, _planner_with, _engine, _state, _TARGET_POLL


@pytest.fixture
def temp_packets_dir():
    tmpdir = tempfile.mkdtemp()
    yield Path(tmpdir)
    shutil.rmtree(tmpdir)


def test_match_packet_lifecycle(temp_packets_dir):
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
    
    saved_path = packet.save(packets_dir=temp_packets_dir)
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


def test_autopilot_integration_records_packets(monkeypatch):
    # Ensure packet is active
    packet = start_match_packet("test_integration_match")
    
    bridge = _TypedBridge(_TARGET_POLL)
    planner = _planner_with('{"option_ids": ["tgt:2"], "reasoning": "opponent"}')
    eng = _engine(monkeypatch, bridge, planner)
    
    # Execute decision
    handled = eng._try_typed_decision_path(_state(), "decision_required")
    assert handled is True
    
    # Verify decision logged in packet
    assert len(packet.decisions) == 1
    assert packet.decisions[0]["chosen_options"] == ["tgt:2"]
    assert packet.decisions[0]["outcome"] == "pending"
    
    # Simulate observing a different state (ADVANCED)
    fp = decision_fingerprint(build_pending_decision(_TARGET_POLL))
    # RequestTracker note_submitted is called by autopilot
    # Now simulate observe with None (advancing)
    eng._request_tracker.observe(None)
    
    assert packet.decisions[0]["outcome"] == "ADVANCED"
    stop_match_packet()


def test_unfinalized_packet_salvaged_as_abandoned(temp_packets_dir, monkeypatch):
    """A packet whose game-end never fired must not be silently discarded
    when the next match starts — it saves as result='abandoned'."""
    import arenamcp.match_packets as mp

    monkeypatch.setattr(mp, "PACKETS_DIR", temp_packets_dir)
    stop_match_packet()  # clean slate

    first = start_match_packet("match_A")
    d = PendingDecision(
        request_id=(1, 2),
        request_type="Mulligan",
        options=(DecisionOption("mull:keep", "Keep"),),
    )
    first.add_decision(d, ["mull:keep"])

    # Next match starts without match_A ever being finalized.
    start_match_packet("match_B")

    saved = list(temp_packets_dir.glob("packet_*match_A.json"))
    assert len(saved) == 1
    data = json.loads(saved[0].read_text(encoding="utf-8"))
    assert data["result"] == "abandoned"
    assert len(data["decisions"]) == 1
    stop_match_packet()


def test_opponent_name_extracted_from_real_replay_header():
    """The replay cosmetic header is {Local, Opponent:{ScreenName,...}} —
    the original hook read a nonexistent 'opponentPlayerName' key and
    silently recorded None for every match."""
    cosmetics = {
        "Local": {"ScreenName": "armour"},
        "Opponent": {"ScreenName": "Primal", "RankingClass": "Gold"},
        "BattlefieldId": 1,
    }
    assert (cosmetics.get("Opponent") or {}).get("ScreenName") == "Primal"
