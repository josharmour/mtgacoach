"""Deck review before submitting a draft deck: grounded narration and waiting for it to be heard."""

from __future__ import annotations

import re
import threading
import time

import pytest

from arenamcp.limited_deck import (
    _deck_cards,
    deck_choice_summary,
    deck_review_narration,
    fallback_deck,
    validate_deck,
)
from arenamcp.speech_completion import SpeechCompletion, narrate_and_wait, speaking_seconds


def card(grp_id, name, cost, *, gih, type_line="Creature — Wizard", text="Vigilance", rarity="common"):
    return {
        "grp_id": grp_id,
        "name": name,
        "mana_cost": cost,
        "type_line": type_line,
        "oracle_text": text,
        "rarity": rarity,
        "gih_wr": gih,
    }


def izzet_pool():
    """Blue flyers and red burn, a deep green creature base, and white chaff."""
    blue = [card(i, f"Sky Drake {i}", "{2}{U}", gih=0.58, text="Flying") for i in range(1, 9)]
    blue += [card(10 + i, f"Tide Adept {i}", "{1}{U}", gih=0.56) for i in range(1, 5)]
    red = [
        card(
            20 + i,
            f"Spark Volley {i}",
            "{1}{R}",
            gih=0.59,
            type_line="Instant",
            text="Spark Volley deals 3 damage to target creature.",
        )
        for i in range(1, 5)
    ]
    red += [card(30 + i, f"Ember Cub {i}", "{1}{R}", gih=0.55) for i in range(1, 7)]
    red.append(
        card(
            40,
            "Undertow",
            "{U}",
            gih=0.57,
            type_line="Instant",
            text="Return target creature to its owner's hand.",
        )
    )
    green = [card(50 + i, f"Grove Bear {i}", "{1}{G}", gih=0.555) for i in range(1, 13)]
    green += [
        card(
            70 + i,
            f"Wild Brawl {i}",
            "{1}{G}",
            gih=0.56,
            type_line="Sorcery",
            text="Target creature you control fights target creature you don't control.",
        )
        for i in range(1, 3)
    ]
    white = [card(80 + i, f"Plain Squire {i}", "{W}", gih=0.49) for i in range(1, 7)]
    return blue + red + green + white


def sentences(text: str) -> list[str]:
    return [part for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part]


def test_each_option_is_two_grounded_sentences_from_a_legal_build():
    pool = izzet_pool()
    build = fallback_deck(pool)
    options = build["deck_options"]
    assert len(options) >= 2
    for option in options:
        # Every option is itself a validated, castable 40-card build from the pool.
        assert option["total_cards"] == 40 and 15 <= option["land_count"] <= 19
        validate_deck(option, pool, source="heuristic")

    narration, described = deck_review_narration(build, options, pool)
    assert described[0] is build and described[1] is not build
    for index, option in enumerate(described, 1):
        text = deck_choice_summary(option, pool, option=index, compared_to=build if index == 2 else None)
        assert len(sentences(text)) == 2, text
        assert text in narration
        deck = _deck_cards(option, pool)
        creatures = sum("Creature" in c["type_line"] for c in deck)
        assert f"with {creatures} creature or token spells" in text
        named = re.search(r"(?:led by|swapping in) (.+?), with", text).group(1).split(" and ")
        assert all(name in {c["name"] for c in deck} for name in named)
        if index == 2:  # the alternative is described by what it has that option 1 lacks
            assert not set(named) & {c["name"] for c in _deck_cards(build, pool)}
    assert narration.count("Option 1") == 1 and narration.count("Option 2") == 1
    assert sentences(narration)[-1].startswith("I'm submitting option 1 because")
    # Card statistics are never presented as a deck win rate.
    assert "%" not in narration and "win rate" not in narration.lower()


def test_interaction_is_split_into_removal_and_tempo_from_rules_text():
    pool = izzet_pool()
    build = fallback_deck(pool)
    izzet = next(o for o in build["deck_options"] if set(o["basic_lands"]) >= {"U", "R"})
    text = deck_choice_summary(izzet, pool, option=1)
    assert "4 removal spells and 1 bounce, tap, or counter spell." in text


def test_model_plan_is_reduced_to_one_clause_so_the_option_stays_two_sentences():
    pool = izzet_pool()
    counted = fallback_deck(pool)
    payload = {
        "main_deck": [{"grp_id": e["grp_id"], "count": e["count"]} for e in counted["main_deck"]],
        "basic_lands": counted["basic_lands"],
        "plan": "Fly over the top with drakes. Burn blockers early; bounce their best threat. Then race.",
        "cuts": [{"grp_id": c["grp_id"], "reason": "weaker"} for c in counted["cuts"]],
    }
    model = validate_deck(payload, pool, source="card_rules")
    text = deck_choice_summary(model, pool, option=1)
    assert len(sentences(text)) == 2
    assert "Its plan: Fly over the top with drakes, " in text
    narration, _ = deck_review_narration(model, counted["deck_options"], pool)
    assert "deck advisor's refined build" in narration


def test_single_distinct_build_says_so_instead_of_inventing_an_alternative():
    pool = [card(i, f"Grove Bear {i}", "{1}{G}", gih=0.55) for i in range(1, 24)]
    build = fallback_deck(pool)
    narration, described = deck_review_narration(build, [build], pool)
    assert narration.startswith("Only one legal 40-card build fits this pool.")
    assert described == [build] and "Option 2" not in narration


# ---------------------------------------------------------------------------
# Speech completion handshake
# ---------------------------------------------------------------------------

FAST = {"accept_timeout": 0.2, "render_timeout": 0.5, "poll": 0.01}


def desktop(completion, script, delay=0.02):
    """A fake desktop that acknowledges each utterance with the scripted states."""
    sent = []
    scripts = list(script)

    def send(text, speech_id):
        sent.append(speech_id)
        states = scripts.pop(0) if scripts else []

        def play():
            for state in states:
                time.sleep(delay)
                completion.update(speech_id, state)

        threading.Thread(target=play, daemon=True).start()

    return send, sent


def test_narration_returns_only_after_playback_finishes():
    completion = SpeechCompletion()
    send, sent = desktop(completion, [["accepted", "started", "finished"]], delay=0.1)
    began = time.monotonic()
    assert narrate_and_wait(
        "Two decks.", send=send, completion=completion, cancelled=lambda: False, wait_options=FAST
    )
    assert time.monotonic() - began >= 0.3  # not on acceptance or playback start
    assert len(sent) == 1


@pytest.mark.parametrize("ending", ["stopped", "cancelled"])
def test_stopped_or_cancelled_narration_never_reports_heard(ending):
    completion = SpeechCompletion()
    stop = threading.Event()
    states = [["accepted", "started", "stopped"]] if ending == "stopped" else [["accepted", "started"]]
    send, _ = desktop(completion, states)
    if ending == "cancelled":
        threading.Timer(0.1, stop.set).start()
    assert not narrate_and_wait(
        "Two decks.", send=send, completion=completion, cancelled=stop.is_set, wait_options=FAST
    )


def test_superseded_narration_is_spoken_again_once():
    completion = SpeechCompletion()
    send, sent = desktop(completion, [["accepted", "superseded"], ["accepted", "started", "finished"]])
    assert narrate_and_wait(
        "Two decks.", send=send, completion=completion, cancelled=lambda: False, wait_options=FAST
    )
    assert len(sent) == 2

    completion = SpeechCompletion()
    send, sent = desktop(completion, [["accepted", "superseded"], ["accepted", "superseded"]])
    assert not narrate_and_wait(
        "Two decks.", send=send, completion=completion, cancelled=lambda: False, wait_options=FAST
    )


@pytest.mark.parametrize("states", [[], ["accepted", "muted"], ["accepted", "failed"], ["accepted"]])
def test_unheard_narration_holds_for_its_full_speaking_time(states):
    """No ack (older desktop), muted, failed or a stuck renderer: never continue early."""
    completion = SpeechCompletion()
    send, _ = desktop(completion, [states])
    held = []
    text = " ".join(["word"] * 60)
    began = time.monotonic()
    assert narrate_and_wait(
        text, send=send, completion=completion, cancelled=lambda: False, sleep=held.append, wait_options=FAST
    )
    waited = time.monotonic() - began + sum(held)
    assert waited >= speaking_seconds(text) - 0.05


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


def test_engine_sends_review_with_a_speech_id_and_waits_for_the_desktop():
    from types import SimpleNamespace

    from arenamcp.standalone_draft_event import _DraftEventMixin

    completion = SpeechCompletion()
    emitted = []

    def emit(**kwargs):
        emitted.append(kwargs)
        for state in ("accepted", "started", "finished"):
            completion.update(kwargs["speech_id"], state)

    coach = _DraftEventMixin()
    coach.ui = SimpleNamespace(speech_completion=completion, emit_speech_request=emit)
    coach._voice_output = SimpleNamespace(current_voice=("am_eric", "Eric"), speed=1.2, muted=False)
    assert coach._narrate_deck_review("Option 1 is blue-red.", lambda: False)
    assert emitted[0]["speech_id"] and emitted[0]["voice_id"] == "am_eric" and emitted[0]["speed"] == 1.2

    spoken = []
    coach.ui = SimpleNamespace()
    coach._voice_output = SimpleNamespace(muted=False, speak=lambda text, blocking: spoken.append(blocking))
    assert coach._narrate_deck_review("Option 1 is blue-red.", lambda: False)
    assert spoken == [True]  # in-process voice plays synchronously
