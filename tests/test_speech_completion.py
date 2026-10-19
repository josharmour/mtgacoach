"""Waiting for the desktop to finish speaking one utterance (speech_completion.py).

The desktop acknowledges each utterance over the pipe: accepted -> started ->
finished, or stopped / superseded / failed / muted. The engine continues only
once the narration was heard, or after its full speaking time when it could not
be voiced. Time is faked: waits advance the clock and deliver the scripted
acknowledgments, so nothing here sleeps.
"""

from __future__ import annotations

import pytest
from tests.draft_fakes import FakeTime

from arenamcp.speech_completion import SpeechCompletion, narrate_and_wait, speaking_seconds

FAST = {"accept_timeout": 0.2, "render_timeout": 0.5, "poll": 0.01}


def handshake():
    clock = FakeTime()
    return clock, SpeechCompletion(clock=clock, idle=clock.advance)


def desktop(clock, completion, script, delay=0.02):
    """A fake desktop that acknowledges each utterance with the scripted states, `delay` apart."""
    sent = []
    scripts = list(script)

    def send(text, speech_id):
        sent.append(speech_id)
        for step, state in enumerate(scripts.pop(0) if scripts else [], start=1):
            clock.at(delay * step, lambda state=state: completion.update(speech_id, state))

    return send, sent


def narrate(clock, completion, send, cancelled=lambda: False, text="Two decks."):
    return narrate_and_wait(
        text,
        send=send,
        completion=completion,
        cancelled=cancelled,
        clock=clock,
        sleep=clock.advance,
        wait_options=FAST,
    )


def test_narration_returns_only_after_playback_finishes():
    clock, completion = handshake()
    send, sent = desktop(clock, completion, [["accepted", "started", "finished"]], delay=0.1)
    assert narrate(clock, completion, send)
    assert clock.now == pytest.approx(0.3, abs=0.011)  # not on acceptance or playback start
    assert len(sent) == 1


@pytest.mark.parametrize("ending", ["stopped", "cancelled"])
def test_stopped_or_cancelled_narration_never_reports_heard(ending):
    clock, completion = handshake()
    states = [["accepted", "started", "stopped"]] if ending == "stopped" else [["accepted", "started"]]
    send, _ = desktop(clock, completion, states)
    assert not narrate(clock, completion, send, cancelled=lambda: ending == "cancelled" and clock.now >= 0.1)


def test_superseded_narration_is_spoken_again_once():
    clock, completion = handshake()
    send, sent = desktop(clock, completion, [["accepted", "superseded"], ["accepted", "started", "finished"]])
    assert narrate(clock, completion, send)
    assert len(sent) == 2

    clock, completion = handshake()
    send, sent = desktop(clock, completion, [["accepted", "superseded"], ["accepted", "superseded"]])
    assert not narrate(clock, completion, send)


@pytest.mark.parametrize("states", [[], ["accepted", "muted"], ["accepted", "failed"], ["accepted"]])
def test_unheard_narration_holds_for_its_full_speaking_time(states):
    """No ack (older desktop), muted, failed or a stuck renderer: never continue early."""
    clock, completion = handshake()
    send, _ = desktop(clock, completion, [states])
    text = " ".join(["word"] * 60)
    assert narrate(clock, completion, send, text=text)
    assert clock.now >= speaking_seconds(text) - 0.05


def test_stale_and_late_acknowledgments_are_ignored():
    completion = SpeechCompletion()
    speech_id = completion.begin()
    assert not completion.update("someone-else", "finished")
    assert completion.update(speech_id, "accepted")
    assert completion.update(speech_id, "stopped")
    assert not completion.update(speech_id, "finished")  # after a terminal state
    assert completion.wait(speech_id, seconds_to_speak=1, **FAST) == "stopped"
    assert not completion.update(speech_id, "finished")  # after the wait is over


def test_muted_voice_holds_for_reading_time_and_honors_cancel():
    held = []
    assert narrate_and_wait(
        "a b c d e f g h i j",
        send=None,
        completion=None,
        cancelled=lambda: False,
        muted=True,
        sleep=held.append,
    )
    assert sum(held) >= speaking_seconds("a b c d e f g h i j") - 1e-6
    calls = iter([False, False, True, True, True])
    assert not narrate_and_wait(
        "a b c", send=None, completion=None, cancelled=lambda: next(calls), muted=True, sleep=lambda s: None
    )
