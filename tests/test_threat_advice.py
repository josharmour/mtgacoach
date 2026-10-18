import pytest

from arenamcp.backend_health import BACKEND_ERROR_PREFIX, LOCAL_FALLBACK_PREFIX
from arenamcp.coach import CoachEngine
from arenamcp.game_plan import GamePlanManager


@pytest.fixture(autouse=True)
def _no_background_game_plan(monkeypatch):
    """get_advice() schedules a background game-plan reform that calls the
    same backend on another thread, so its call could land in
    ``backend.calls`` before (or after) the threat prompt, and the thread
    could hold the shared STRATEGIC_LANE into later tests. These tests are
    about the threat prompt only."""
    monkeypatch.setattr(GamePlanManager, "request_reform", lambda self, game_state, **_kwargs: False)


def _threat_call(backend: "_DummyBackend") -> str:
    """The one user message carrying the threat prompt."""
    matches = [user for _system, user in backend.calls if "THREAT ALERT" in user]
    assert len(matches) == 1, f"expected one THREAT ALERT call, got {len(matches)} of {len(backend.calls)}"
    return matches[0]


class _DummyBackend:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []
        self.model = "dummy-threat-model"

    def complete(self, system_prompt: str, user_message: str, *args, **kwargs) -> str:
        self.calls.append((system_prompt, user_message))
        return self.response


def _sample_game_state() -> dict:
    return {
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 14},
            {"seat_id": 2, "is_local": False, "life_total": 18},
        ],
        "turn": {"turn_number": 6, "phase": "Phase_Main1", "step": "Step_PreCombatMain"},
        "hand": [
            {
                "name": "Go for the Throat",
                "mana_cost": "{1}{B}",
                "type_line": "Instant",
                "oracle_text": "Destroy target nonartifact creature.",
            },
            {
                "name": "Cut Down",
                "mana_cost": "{B}",
                "type_line": "Instant",
                "oracle_text": "Destroy target creature with total power and toughness 5 or less.",
            },
        ],
        "battlefield": [
            {
                "name": "Swamp",
                "type_line": "Basic Land — Swamp",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
                "is_tapped": False,
            },
            {
                "name": "Restless Cottage",
                "type_line": "Land",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
                "is_tapped": False,
            },
            {
                "name": "Mosswood Dreadknight",
                "type_line": "Creature — Human Knight",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
                "is_tapped": False,
                "power": 3,
                "toughness": 2,
            },
        ],
        "graveyard": [],
        "stack": [],
    }


def test_threat_prompt_uses_specific_card_and_available_answers(monkeypatch):
    backend = _DummyBackend("Use Go for the Throat now before it snowballs.")
    coach = CoachEngine(backend=backend)
    coach._deck_strategy = "Trade resources early, then win with recursive threats."
    coach._rules_db = type("RulesStub", (), {"get_rules_for_situation": lambda *args, **kwargs: []})()
    monkeypatch.setattr(coach, "_format_game_context", lambda state: "CTX")

    threat = {
        "name": "Sheoldred, the Apocalypse",
        "warning": "Drains 2 on your draws, heals on theirs!",
        "card": {
            "name": "Sheoldred, the Apocalypse",
            "type_line": "Legendary Creature — Phyrexian Praetor",
            "oracle_text": "Deathtouch. Whenever you draw a card, you lose 2 life.",
            "power": 4,
            "toughness": 5,
        },
    }

    advice = coach.get_advice(
        _sample_game_state(),
        trigger="threat_detected",
        style="concise",
        threat=threat,
    )

    assert advice.startswith("Sheoldred, the Apocalypse is the key threat.")
    assert backend.calls, "expected backend.complete to be called"
    user_message = _threat_call(backend)
    assert "THREAT ALERT: Sheoldred, the Apocalypse" in user_message
    assert "Available answers now: Go for the Throat" in user_message
    assert "Name Sheoldred, the Apocalypse explicitly in the first sentence." in user_message


def test_threat_fallback_is_specific_when_backend_errors(monkeypatch):
    backend = _DummyBackend(f"{BACKEND_ERROR_PREFIX} LLM timed out")
    coach = CoachEngine(backend=backend)
    coach._rules_db = type("RulesStub", (), {"get_rules_for_situation": lambda *args, **kwargs: []})()
    monkeypatch.setattr(coach, "_format_game_context", lambda state: "CTX")

    threat = {
        "name": "The Wandering Emperor",
        "warning": "Flash! Can exile or make blockers anytime.",
        "card": {
            "name": "The Wandering Emperor",
            "type_line": "Legendary Planeswalker",
            "oracle_text": "+1: Put a +1/+1 counter on up to one target creature. It gains first strike until end of turn.",
            "counters": {"Loyalty": 3},
        },
    }

    advice = coach.get_advice(
        _sample_game_state(),
        trigger="threat_detected",
        style="concise",
        threat=threat,
    )

    # The planeswalker branch: our untapped Mosswood Dreadknight can attack it.
    assert advice.startswith(LOCAL_FALLBACK_PREFIX)
    assert "The Wandering Emperor is the problem. Attack it this turn" in advice


def test_system_prompts_warn_against_pointless_protective_abilities():
    """Regression for the Adanto Vanguard report: the coach told the user to
    activate a 'Pay 4 life: indestructible' ability with no blockers and no
    removal on the stack — pure life loss. Both system prompts must carry an
    explicit rule against paying life for protection without a concrete threat."""
    from arenamcp.coach import CONCISE_SYSTEM_PROMPT, DEFAULT_SYSTEM_PROMPT

    rules = {
        DEFAULT_SYSTEM_PROMPT: "- PROTECTIVE / LIFE-PAYMENT ABILITIES: Do NOT pay life",
        CONCISE_SYSTEM_PROMPT: "- PROTECTIVE ABILITIES: Don't pay life for indestructible/hexproof/protection",
    }
    for prompt, rule in rules.items():
        (line,) = [line for line in prompt.splitlines() if line.startswith(rule)]
        # The rule ties protection to an actual threat, not blanket activation.
        assert "concrete threat" in line.lower()
