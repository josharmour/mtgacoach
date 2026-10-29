"""Grind narration follows progress and serializes desktop speech without blocking play."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

from arenamcp.auto_quest import QuestSnapshot
from arenamcp.speech_completion import SpeechCompletion
from arenamcp.standalone_auto_quest import AutoQuestMixin


def runtime():
    app = AutoQuestMixin()
    app._auto_quest = {"goal": "mastery"}
    app.ui = SimpleNamespace(status=Mock(), advice=Mock())
    app._voice_output = SimpleNamespace(muted=False, speak=Mock(), current_voice=("voice", "Voice"), speed=1)
    return app


def drain(run):
    worker = run.get("speech_worker")
    if worker:
        worker.join(timeout=2)
        assert not worker.is_alive()


def test_progress_spoken_once_then_completion_and_reset(monkeypatch):
    monkeypatch.setattr("arenamcp.standalone_auto_quest.time.time", lambda: 1000)
    app = runtime()
    run = app._auto_quest
    snapshot = QuestSnapshot(
        quests=[{"locKey": "black", "goal": 20, "endingProgress": 3}],
        observed_at=1000,
        rewards_at=1000,
        rewards={
            "_dailyRewardSequenceId": 4,
            "_dailyRewardResetTimestamp": "1970-01-02T00:00:00Z",
            "_weeklyRewardSequenceId": 14,
            "_weeklyRewardResetTimestamp": "1970-01-08T00:00:00Z",
        },
    )
    descriptions = {"black_Text": "Cast {quantity} black spells."}
    app._narrate_grind_progress(run, snapshot, descriptions)
    drain(run)
    spoken = app._voice_output.speak.call_args.args[0]
    assert "black spells" in spoken and "3 of 20" in spoken
    assert "Daily win rewards: 4 of 10" in spoken
    assert "Weekly win rewards: 14 of 15" in spoken
    app._narrate_grind_progress(run, snapshot, descriptions)
    drain(run)
    assert app._voice_output.speak.call_count == 1
    snapshot.quests[0]["endingProgress"] = 20
    snapshot.rewards["_weeklyRewardSequenceId"] = 15
    app._narrate_grind_progress(run, snapshot, descriptions)
    drain(run)
    spoken = app._voice_output.speak.call_args.args[0]
    assert "Quest complete" in spoken and "Weekly win reward target complete" in spoken
    snapshot.quests[0]["endingProgress"] = 3
    app._narrate_grind_progress(run, snapshot, descriptions)
    drain(run)
    assert "3 of 20" in app._voice_output.speak.call_args.args[0]


def test_stale_rewards_not_narrated_and_daily4_respected(monkeypatch):
    monkeypatch.setattr("arenamcp.standalone_auto_quest.time.time", lambda: 3000)
    app = runtime()
    app._auto_quest["goal"] = "daily4"
    snapshot = QuestSnapshot(
        quests=[],
        rewards_at=0,
        rewards={"_dailyRewardSequenceId": 4, "_dailyRewardResetTimestamp": "1970-01-02T00:00:00Z"},
    )
    app._narrate_grind_progress(app._auto_quest, snapshot, {})
    app._voice_output.speak.assert_not_called()
    snapshot.rewards_at = 3000
    app._narrate_grind_progress(app._auto_quest, snapshot, {})
    drain(app._auto_quest)
    assert "Daily win reward target complete" in app._voice_output.speak.call_args.args[0]


def test_desktop_waits_for_ack_without_blocking_caller():
    app = runtime()
    run = app._auto_quest
    completion = app.ui.speech_completion = SpeechCompletion()
    sent = []
    first = threading.Event()
    second = threading.Event()

    def emit(**request):
        sent.append(request)
        completion.update(request["speech_id"], "started")
        (first if len(sent) == 1 else second).set()

    app.ui.emit_speech_request = emit
    app._announce_grind(run, "prepare", "Preparing a black deck.")
    assert first.wait(2)
    app._announce_grind(run, "queue", "Entering Standard Play.")
    assert not second.is_set()
    completion.update(sent[0]["speech_id"], "finished")
    assert second.wait(2)
    completion.update(sent[1]["speech_id"], "finished")
    drain(run)
    assert [r["text"] for r in sent] == ["Preparing a black deck.", "Entering Standard Play."]


def test_muting_and_voice_failure_do_not_break_grind():
    app = runtime()
    run = app._auto_quest
    app._voice_output.muted = True
    app._announce_grind(run, "one", "Quest progress.")
    drain(run)
    app._voice_output.speak.assert_not_called()
    app._voice_output.muted = False
    app._voice_output.speak.side_effect = RuntimeError("audio unavailable")
    app._announce_grind(run, "two", "Quest complete.")
    drain(run)
    app._announce_grind(run, "three", "Weekly target complete.")
    drain(run)
    assert app._voice_output.speak.call_count == 2


def test_stop_discards_pending_navigation_but_speaks_completion():
    app = runtime()
    run = app._auto_quest
    started = threading.Event()
    release = threading.Event()
    spoken = []

    def speak(text, blocking):
        spoken.append(text)
        if len(spoken) == 1:
            started.set()
            assert release.wait(2)

    app._voice_output.speak = speak
    app._announce_grind(run, "initial", "Checking quests.")
    assert started.wait(2)
    app._announce_grind(run, "queue", "Entering Standard Play.")
    app._announce_grind(run, "complete", "All targets complete.")
    app._announce_grind(run, "off", "Grind stopped.")
    app._auto_quest = None
    release.set()
    drain(run)
    assert spoken == ["Checking quests.", "All targets complete.", "Grind stopped."]
