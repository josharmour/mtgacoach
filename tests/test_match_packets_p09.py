"""P0-9: packets record executor actions; finalized matches never restart.

2026-07-05 evidence: match 1 saved decisions=0 despite 8 bridge
submissions; a junk decisions=0/result=unknown packet was saved after the
server re-surfaced the finished match id and recording restarted.
"""

import json

import arenamcp.match_packets as mp


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(mp, "_current_packet", None)
    monkeypatch.setattr(mp, "_finalized_match_ids", set())
    monkeypatch.setattr(mp, "PACKETS_DIR", tmp_path)


def test_executed_actions_recorded_in_packet(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    packet = mp.start_match_packet("m-1")
    packet.add_executed_action(
        "cast_spell", card_name="Talisman of Unity", turn=3, path="GRE bridge"
    )
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


def test_finalized_match_never_restarts_recording(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
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
