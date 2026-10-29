from dataclasses import replace

import pytest

from arenamcp.commentary.contracts import TERMINAL_STATES, Epoch, SpeechCategory, Utterance
from arenamcp.desktop.speech_arbiter import SpeechArbiter

EPOCH = Epoch("match", 1)


def line(uid, category="commentary", **kwargs):
    return Utterance(uid, uid, SpeechCategory(category), EPOCH, 108, **kwargs)


@pytest.fixture
def rig():
    started, stopped, statuses = [], [], []
    now = [100.0]
    arbiter = SpeechArbiter(
        start=started.append,
        stop=lambda: stopped.append(True),
        status=lambda uid, state: statuses.append((uid, state)),
        clock=lambda: now[0],
    )
    arbiter.reset(EPOCH)
    return arbiter, started, stopped, statuses, now


def finish(a, uid):
    a.playback_status(uid, "started", epoch=EPOCH)
    a.playback_status(uid, "finished", epoch=EPOCH)


def test_commentary_waits_for_workflow_and_expires(rig):
    a, started, stopped, statuses, now = rig
    a.submit(line("grind", "workflow", interruptible=False))
    a.submit(line("comment"))
    assert [u.utterance_id for u in started] == ["grind"]
    now[0] = 109
    a.playback_status("grind", "started", epoch=EPOCH)
    a.tick()
    assert not stopped
    assert ("comment", "superseded") in statuses


def test_essential_preempts_commentary_and_stale_completion_is_ignored(rig):
    a, started, stopped, statuses, _ = rig
    a.submit(line("comment", interruptible=False))
    a.submit(line("error", "essential"))
    assert len(stopped) == 1
    a.playback_status("comment", "finished", epoch=EPOCH)
    assert a.active.utterance_id == "error"
    finish(a, "error")
    assert [s for uid, s in statuses if uid == "comment" and s in TERMINAL_STATES] == ["superseded"]
    assert a.active is None


def test_reply_waits_for_successful_playback_not_rendering(rig):
    a, started, _, statuses, _ = rig
    a.submit(line("call"))
    a.submit(line("reply", anchor_id="call"))
    assert len(started) == 1
    a.playback_status("call", "finished", epoch=EPOCH)  # no playback ever started
    assert ("call", "failed") in statuses
    assert ("reply", "superseded") in statuses
    assert len(started) == 1


def test_successful_anchor_releases_reply(rig):
    a, started, _, _, _ = rig
    a.submit(line("call"))
    a.submit(line("reply", anchor_id="call"))
    finish(a, "call")
    assert [u.utterance_id for u in started] == ["call", "reply"]


def test_one_pending_line_and_exactly_one_terminal_for_replaced_line(rig):
    a, _, _, statuses, _ = rig
    a.submit(line("a"))
    a.submit(line("b"))
    a.submit(line("c"))
    assert not a.submit(line("b"))
    assert a.pending.utterance_id == "c"
    a.clear()
    a.clear()
    for uid in ("a", "b", "c"):
        assert len([s for u, s in statuses if u == uid and s in TERMINAL_STATES]) == 1


def test_epoch_reset_and_old_epoch_ack_cannot_finish_reused_id(rig):
    a, _, _, statuses, _ = rig
    a.submit(line("call"))
    second = Epoch("match", 2)
    a.reset(second)
    a.submit(replace(line("call"), epoch=second))
    a.playback_status("call", "finished", epoch=EPOCH)
    assert a.active.epoch == second
    assert statuses.count(("call", "stopped")) == 1


def test_global_mute_and_mode_off_clear_optional_speech(rig):
    a, _, _, statuses, _ = rig
    a.submit(line("comment"))
    a.submit(line("reply", anchor_id="comment"))
    a.set_muted(True)
    assert ("comment", "muted") in statuses and ("reply", "muted") in statuses
    a.submit(line("error", "essential"))
    assert ("error", "muted") in statuses
    a.set_muted(False)
    a.submit(line("grind", "workflow"))
    a.submit(line("next"))
    a.disable_commentary()
    assert a.active.utterance_id == "grind" and a.pending is None


def test_invalidated_evidence_stops_audio_and_start_failure_is_terminal(rig):
    a, _, _, statuses, _ = rig
    a.submit(line("comment"))
    a._valid = lambda item: False
    a.tick()
    assert ("comment", "superseded") in statuses
    a._valid = lambda item: True

    def broken(item):
        raise RuntimeError("audio failed")

    a._start = broken
    a.submit(line("next"))
    assert statuses[-1] == ("next", "failed")
