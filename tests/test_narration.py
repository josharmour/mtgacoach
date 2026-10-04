"""Driver voice is factual about intent and shares existing model calls."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlan, ActionType, GameAction
from arenamcp.autopilot import AutopilotEngine
from arenamcp.coach import CoachEngine
from arenamcp.narration import action_narration, narration_policy, submission_narration


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("Cast Play with Fire.", "I'm casting Play with Fire."),
        ("Don't attack.", "I'm not attacking."),
        ("Don't block.", "I'm not blocking."),
        (
            "Attack Jace with Bear; Attack opponent with Dragon.",
            "I'm attacking Jace with Bear; I'm attacking opponent with Dragon.",
        ),
        ("Block Dragon with Giant.", "I'm blocking Dragon with Giant."),
        ("Choose The Notary Hobbits.", "I'm choosing The Notary Hobbits."),
        ("Mulligan.", "I'm taking a mulligan."),
        ("Resolve.", "I'm passing priority."),
        ("Pass priority.", "I'm passing priority."),
        ("I'm holding removal.", "I'm holding removal."),
        ("The opponent cast Murder.", "The opponent cast Murder."),
    ],
)
def test_driver_actions_preserve_names_targets_and_observations(action, expected):
    assert action_narration(action) == expected


def test_preview_is_intent_without_claiming_an_action_happened():
    assert action_narration("Cast Murder.", planned=True) == "I plan to cast Murder."
    assert action_narration("Don't attack.", planned=True) == "I plan not to attack."
    plan = ActionPlan(actions=[GameAction(action_type=ActionType.CAST_SPELL, card_name="Murder")])
    engine = AutopilotEngine.__new__(AutopilotEngine)
    assert "PLAN: I plan to cast Murder." in engine._format_plan_preview(plan)


def test_submission_reason_is_plan_instead_of_an_instruction_to_viewer():
    assert submission_narration("Cast Birds of Paradise.", "Add mana for next turn.") == (
        "I'm casting Birds of Paradise. I plan to add mana for next turn."
    )
    assert submission_narration("Choose Forest.", "This supports the next land drop.") == (
        "I'm choosing Forest. This supports the next land drop."
    )


class Backend:
    timeout_s = 5

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def complete(self, system, user, *args, **kwargs):
        self.calls.append((system, user))
        return self.reply


def make_coach(monkeypatch, reply="Cast Murder."):
    backend = Backend(reply)
    coach = CoachEngine(backend=backend)
    coach._rules_db = SimpleNamespace(get_rules_for_situation=lambda *args, **kwargs: [])
    monkeypatch.setattr(coach, "_ensure_game_plan_mgr", lambda: None)
    monkeypatch.setattr(coach, "_build_context", lambda state: "Current board: opponent has priority.")
    monkeypatch.setattr(coach, "_postprocess_advice", lambda response, *args, **kwargs: response)
    return coach, backend


@pytest.mark.parametrize("mode", ["advisor", "autopilot"])
def test_coach_routes_prompt_and_final_text_without_an_extra_call(monkeypatch, mode):
    monkeypatch.setenv("MTGACOACH_STRUCTURED_ADVICE", "0")
    coach, backend = make_coach(monkeypatch)
    coach.narration_mode = mode
    response = coach.get_advice({}, trigger="new_turn", style="quick")
    assert len(backend.calls) == 1
    prompt = backend.calls[0][0]
    if mode == "autopilot":
        assert "VOICE ROLE — AUTOPILOT" in prompt
        assert "a cast is not a resolved spell" in prompt
        assert response == "I plan to cast Murder."
    else:
        assert "VOICE ROLE — ADVISOR" in prompt
        assert response == "Cast Murder."


def test_paused_or_explicit_advisor_override_does_not_claim_bot_control(monkeypatch):
    coach, backend = make_coach(monkeypatch)
    coach.narration_mode = "autopilot"
    response = coach.get_advice({}, question="What would you recommend?", narration_mode="advisor")
    assert "VOICE ROLE — ADVISOR" in backend.calls[0][0]
    assert "VOICE ROLE — AUTOPILOT" not in backend.calls[0][0]
    assert response == "Cast Murder."


def test_autopilot_conversation_retains_model_written_observation(monkeypatch):
    coach, backend = make_coach(monkeypatch, "The opponent is holding two cards.")
    response = coach.get_advice({}, conversational=True, narration_mode="autopilot")
    assert response == "The opponent is holding two cards."
    assert len(backend.calls) == 1
    assert "sportscaster" in backend.calls[0][0]


def test_background_win_plan_uses_driver_voice_without_new_requests(monkeypatch):
    coach, backend = make_coach(monkeypatch, "I plan to keep removal available.")
    coach.narration_mode = "autopilot"
    result = coach.get_win_plan({}, turns=3)
    assert result == "I plan to keep removal available."
    assert len(backend.calls) == 1
    assert "VOICE ROLE — AUTOPILOT" in backend.calls[0][0]


def test_postmatch_attributes_autopilot_errors_and_distinguishes_advice_from_execution(monkeypatch):
    coach, backend = make_coach(monkeypatch, "I spent removal too early.")
    coach.generate_post_match_analysis([], "loss", 12, narration_mode="autopilot")
    prompt = backend.calls[0][0]
    assert "do not attribute its errors to the human" in prompt
    assert "not proof of execution" in prompt
    assert len(backend.calls) == 1


def test_driver_policy_does_not_instruct_the_spectator_to_operate_the_game():
    policy = narration_policy("autopilot")
    assert "Do not instruct the human" in policy
    assert "selected tutor card is not yet in hand" in policy
    assert "current state/events confirm" in policy


@pytest.mark.parametrize("dry_run", [False, True])
def test_native_mac_input_reports_intent_without_claiming_cast_completion(monkeypatch, dry_run):
    from test_native_mac_autopilot import command, make_engine

    engine, controller, backend, state, notices = make_engine(
        monkeypatch, response=command(reason="Cast Murder."), dry_run=dry_run
    )
    engine._speak_fn = Mock()
    assert engine.process_trigger(state, "desktop_poll")
    expected = "I plan to cast Murder."
    if dry_run:
        expected = "Preview only: " + expected
        controller.execute.assert_not_called()
        engine._speak_fn.assert_not_called()
    else:
        controller.execute.assert_called_once()
        engine._speak_fn.assert_called_once_with(expected, False)
    notices.assert_called_once_with(expected, "AUTOPILOT")
    assert backend.complete_with_image.call_count == 1
    assert "A click is an attempt" in backend.complete_with_image.call_args.args[0]


@pytest.mark.parametrize("kind", ["move", "scroll"])
def test_native_navigation_does_not_generate_speech(monkeypatch, kind):
    from arenamcp.native_mac_input import DesktopAction
    from test_native_mac_autopilot import command, make_engine, make_frame

    engine, _, _, _, _ = make_engine(monkeypatch)
    engine._speak_fn = Mock()
    action = DesktopAction.from_dict(command(kind, reason="Locate the card", amount=-1))
    assert engine._send(make_frame(), action)
    engine._speak_fn.assert_not_called()


def test_native_failed_or_repeated_input_does_not_repeat_speech(monkeypatch):
    from arenamcp.native_mac_input import DesktopAction
    from test_native_mac_autopilot import command, make_engine, make_frame

    engine, controller, _, _, _ = make_engine(monkeypatch)
    engine._speak_fn = Mock()
    action = DesktopAction.from_dict(command(reason="Choose Forest."))
    controller.execute.return_value = False
    assert not engine._send(make_frame(), action)
    engine._speak_fn.assert_not_called()
    controller.execute.return_value = True
    assert engine._send(make_frame(), action)
    assert engine._send(make_frame(), action)
    engine._speak_fn.assert_called_once_with("I plan to choose Forest.", False)
