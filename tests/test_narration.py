"""Driver voice is factual about intent and shares existing model calls."""

from types import SimpleNamespace

import pytest

from arenamcp.action_planner import ActionPlan, ActionType, GameAction
from arenamcp.autopilot import AutopilotEngine
from arenamcp.coach import CoachEngine
from arenamcp.narration import action_narration, narration_policy, submission_narration


def test_attack_speech_counts_copies_without_changing_assignments():
    names = [f"Hare Apparent [id:{i}]" for i in range(4)]
    names += ["Welcoming Vampire [id:10]"]
    names += [f"*Rabbit [id:{i}]" for i in range(20, 30)]
    assignments = dict.fromkeys(names, "Ugin, Eye of the Storms [id:99]")
    action = GameAction(
        action_type=ActionType.DECLARE_ATTACKERS,
        attacker_names=names[:],
        attacker_targets=assignments.copy(),
    )
    spoken = action_narration(ActionPlan(actions=[action]).spoken_actions())
    assert "4 Hare Apparent, Welcoming Vampire, and 10 Rabbit tokens at Ugin, Eye of the Storms" in spoken
    assert "[id:" not in spoken
    assert action.attacker_names == names
    assert action.attacker_targets == assignments


def test_attack_speech_keeps_counts_separate_by_target():
    from arenamcp.narration import combat_declaration

    spoken = combat_declaration(
        "Attack",
        {
            "*Rabbit [id:1]": "Ugin [id:8]",
            "*Rabbit [id:2]": "Ugin [id:8]",
            "*Rabbit [id:3]": "Opponent",
            "Rabbit [id:4]": "Opponent",
        },
    )
    assert spoken == "Attack with 2 Rabbit tokens at Ugin, and Rabbit token and Rabbit at the opponent"


def test_attack_speech_counts_copies_without_explicit_target_assignments():
    action = GameAction(
        action_type=ActionType.DECLARE_ATTACKERS,
        attacker_names=["Hare Apparent [id:1]", "Hare Apparent [id:2]"],
        target_names=["Opponent"],
    )
    assert ActionPlan(actions=[action]).spoken_actions() == "Attack with 2 Hare Apparent at the opponent."


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("Cast Play with Fire.", "Casting Play with Fire."),
        ("Don't attack.", "Not attacking."),
        ("Don't block.", "Not blocking."),
        (
            "Attack Jace with Bear; Attack opponent with Dragon.",
            "Attacking Jace with Bear, and opponent with Dragon.",
        ),
        ("Block Dragon with Giant.", "Blocking Dragon with Giant."),
        ("Choose The Notary Hobbits.", "Choosing The Notary Hobbits."),
        ("Mulligan.", "Taking a mulligan."),
        ("Resolve.", "Passing priority."),
        ("Pass priority.", "Passing priority."),
        ("I'm holding removal.", "Holding removal."),
        ("The opponent cast Murder.", "The opponent cast Murder."),
    ],
)
def test_driver_actions_preserve_names_targets_and_observations(action, expected):
    assert action_narration(action) == expected


def test_preview_is_intent_without_claiming_an_action_happened():
    assert action_narration("Cast Murder.", planned=True) == "Plan: casting Murder."
    assert action_narration("Don't attack.", planned=True) == "Plan: not attacking."
    plan = ActionPlan(actions=[GameAction(action_type=ActionType.CAST_SPELL, card_name="Murder")])
    engine = AutopilotEngine.__new__(AutopilotEngine)
    assert "PLAN: casting Murder." in engine._format_plan_preview(plan)


def test_submission_reason_is_plan_instead_of_an_instruction_to_viewer():
    assert submission_narration("Cast Birds of Paradise.", "Add mana for next turn.") == (
        "Casting Birds of Paradise. Plan: adding mana for next turn."
    )
    assert submission_narration("Choose Forest.", "This supports the next land drop.") == (
        "Choosing Forest. This supports the next land drop."
    )


def test_report_attack_is_one_sentence_grouped_by_target_without_protocol_labels():
    assignments = {
        "Llanowar Elves [id:700]": "Opponent",
        "Badgermole Cub [id:701]": "Opponent",
        "*Rhino Warrior [id:702]": "Nicol Bolas, Dragon-God [774]",
    }
    action = GameAction(action_type=ActionType.DECLARE_ATTACKERS, attacker_targets=assignments.copy())
    speech = submission_narration(ActionPlan(actions=[action]).spoken_actions())
    assert speech == (
        "Attacking with Llanowar Elves and Badgermole Cub at the opponent, "
        "and Rhino Warrior token at Nicol Bolas, Dragon-God."
    )
    assert action.attacker_targets == assignments  # Rendering cannot alter execution identity.


def test_multiple_blockers_share_one_action_and_preserve_the_other_attacker():
    action = GameAction(
        action_type=ActionType.DECLARE_BLOCKERS,
        blocker_assignments={"Bear #1": "Giant [id:10]", "Bear #2": "Giant [id:10]", "Elf": "Goblin [id:11]"},
    )
    assert submission_narration(ActionPlan(actions=[action]).spoken_actions()) == (
        "Blocking with Bear #1 and Bear #2 against Giant, and Elf against Goblin."
    )


def test_autopilot_uses_my_in_actions_and_rationale_without_rewriting_card_titles():
    assert submission_narration("Return your commander.", "Your creatures can protect your life total.") == (
        "Returning my commander. My creatures can protect my life total."
    )
    assert action_narration("Cast Your Temple Is Under Attack.") == "Casting Your Temple Is Under Attack."
    assert submission_narration("Choose Forest.", "That land is yours.") == (
        "Choosing Forest. That land is mine."
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
        assert response == "Plan: casting Murder."
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


def test_autopilot_answer_retains_model_written_observation(monkeypatch):
    coach, backend = make_coach(monkeypatch, "The opponent is holding two cards.")
    response = coach.get_advice({}, question="What do they have?", narration_mode="autopilot")
    assert response == "The opponent is holding two cards."
    assert len(backend.calls) == 1
    assert "sportscaster" in backend.calls[0][0]


def test_background_win_plan_uses_driver_voice_without_new_requests(monkeypatch):
    coach, backend = make_coach(monkeypatch, "I plan to keep removal available.")
    coach.narration_mode = "autopilot"
    result = coach.get_win_plan({}, turns=3)
    assert result == "Plan: keeping removal available."
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


def test_model_supplied_action_prefixes_are_removed_without_changing_observations():
    assert action_narration("I’m attacking with my Elf.") == "Attacking with my Elf."
    assert action_narration("I'm blocking with my token.") == "Blocking with my token."
    assert action_narration("I plan to cast Murder.") == "Plan: casting Murder."
    assert action_narration("I'm holding removal.", planned=True) == "Plan: holding removal."
    assert action_narration("I'm concerned about their open mana.") == "I'm concerned about their open mana."


def test_submission_also_normalizes_old_model_action_prefixes_in_the_rationale():
    assert submission_narration("Play Forest.", "I'm holding my removal.") == (
        "Playing Forest. Plan: holding my removal."
    )
