"""Tests for Bo3 sideboarding recommendations and post-match /analyze review prompt generator."""

import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path

from arenamcp.coach import CoachEngine, SIDEBOARD_RECOMMENDATION_PROMPT
from arenamcp.match_history import MatchRecord, generate_match_review_prompt
from arenamcp.standalone import StandaloneCoach
from arenamcp.pipe_adapter import PipeAdapter


class DummyBackend:
    def __init__(self, response: str = "IN:\n- 2x Disdainful Stroke\nOUT:\n- 2x Cut Down\nPLAN:\nCounter big threats."):
        self.response = response
        self.last_prompt = None
        self.last_user = None

    def complete(self, system_prompt: str, user_message: str, max_tokens: int = 2048) -> str:
        self.last_prompt = system_prompt
        self.last_user = user_message
        return self.response


def test_coach_recommend_sideboard_tuples():
    """Test CoachEngine.recommend_sideboard with tuple card inputs."""
    backend = DummyBackend()
    coach = CoachEngine(backend=backend)

    maindeck = [
        ("Cut Down", "Instant", "Destroy target creature with total power and toughness 5 or less."),
        ("Lightning Bolt", "Instant", "Deal 3 damage to any target."),
    ]
    sideboard = [
        ("Disdainful Stroke", "Instant", "Counter target spell with mana value 4 or greater."),
        ("Duress", "Sorcery", "Target opponent reveals their hand."),
    ]
    opp_cards = [
        ("Sheoldred, the Apocalypse", "Creature — Phyrexian Praetor", "Deathtouch"),
    ]
    game_history = [{"result": "loss", "turns": 8}]

    result = coach.recommend_sideboard(
        maindeck_cards=maindeck,
        sideboard_cards=sideboard,
        opponent_cards_seen=opp_cards,
        game_history=game_history,
    )

    assert result is not None
    assert "Disdainful Stroke" in result
    assert "Cut Down" in result
    assert backend.last_prompt == SIDEBOARD_RECOMMENDATION_PROMPT
    assert "Cut Down" in backend.last_user
    assert "Disdainful Stroke" in backend.last_user
    assert "Sheoldred" in backend.last_user
    assert "Game 1: loss (8 turns)" in backend.last_user


def test_coach_recommend_sideboard_dicts_and_strings():
    """Test CoachEngine.recommend_sideboard with dicts and plain string card names."""
    backend = DummyBackend(response="IN:\n- 1x Negate\nOUT:\n- 1x Fatal Push\nPLAN:\nProtect combo.")
    coach = CoachEngine(backend=backend)

    maindeck = [{"name": "Fatal Push", "type_line": "Instant", "oracle_text": "Destroy target creature..."}]
    sideboard = ["Negate", "Spell Pierce"]
    opp_cards = ["Archmage's Charm"]

    result = coach.recommend_sideboard(
        maindeck_cards=maindeck,
        sideboard_cards=sideboard,
        opponent_cards_seen=opp_cards,
    )

    assert result is not None
    assert "Negate" in result
    assert "Fatal Push" in backend.last_user
    assert "Spell Pierce" in backend.last_user


def test_coach_recommend_sideboard_error_fallback():
    """Test recommend_sideboard when backend returns empty or error."""
    backend = DummyBackend(response="Error 500: Server unavailable")
    coach = CoachEngine(backend=backend)

    result = coach.recommend_sideboard(
        maindeck_cards=["Mountain"],
        sideboard_cards=["Smash to Smithereens"],
        opponent_cards_seen=["Sol Ring"],
    )

    assert result is None


def test_match_history_generate_match_review_prompt():
    """Test match_history.generate_match_review_prompt formatting."""
    record = MatchRecord(
        match_id="match_12345",
        timestamp="2026-07-22T09:00:00Z",
        result="win",
        opponent_name="Opponent42",
        local_deck_colors=["U", "B"],
        opponent_colors_seen=["R", "G"],
        format_name="Standard",
        turns=12,
        local_life_final=14,
        opponent_life_final=0,
    )

    advice_history = [
        {
            "game_snapshot": {"turn_number": 3, "phase": "Main1"},
            "trigger": "decision",
            "advice": "Cast Counterspell targeting Bloodbraid Elf.",
        }
    ]
    opp_cards = ["Bloodbraid Elf", "Lightning Bolt"]
    missed_decisions = [
        {"turn": 5, "phase": "Combat", "decision_type": "declare_blockers", "prompt_text": "Select blockers"}
    ]

    prompt = generate_match_review_prompt(
        record=record,
        advice_history=advice_history,
        opponent_cards=opp_cards,
        missed_decisions=missed_decisions,
        replay_summary="Turn 3: Cast Bloodbraid Elf",
    )

    assert "POST-MATCH REVIEW REQUEST (/analyze)" in prompt
    assert "Match ID: match_12345" in prompt
    assert "Result: WIN" in prompt
    assert "Opponent: Opponent42" in prompt
    assert "Format: Standard" in prompt
    assert "Player Deck Colors: U, B" in prompt
    assert "Opponent Colors Seen: R, G" in prompt
    assert "Match Duration: 12 turns" in prompt
    assert "Player=14, Opponent=0" in prompt
    assert "Bloodbraid Elf" in prompt
    assert "Turn 3 (Main1) [decision]: Cast Counterspell" in prompt
    assert "MISSED DECISION POINTS" in prompt
    assert "REPLAY SUMMARY" in prompt

    # Test dataclass method round-trip
    method_prompt = record.to_review_prompt(
        advice_history=advice_history,
        opponent_cards=opp_cards,
    )
    assert method_prompt == generate_match_review_prompt(record, advice_history=advice_history, opponent_cards=opp_cards)


def test_standalone_get_sideboard_recommendations():
    """Test StandaloneCoach.get_sideboard_recommendations integration."""
    mock_mcp = MagicMock()
    mock_mcp.get_game_state.return_value = {
        "deck_cards": [101, 102],
        "sideboard_cards": [201],
        "opponent_played_cards": [301],
    }
    mock_mcp.get_card_info.side_effect = lambda cid: {
        101: {"name": "Llanowar Elves", "type_line": "Creature", "oracle_text": "{T}: Add {G}."},
        102: {"name": "Giant Growth", "type_line": "Instant", "oracle_text": "Target creature gets +3/+3."},
        201: {"name": "Plummet", "type_line": "Instant", "oracle_text": "Destroy target creature with flying."},
        301: {"name": "Serra Angel", "type_line": "Creature", "oracle_text": "Flying, vigilance"},
    }.get(cid, {})

    mock_coach_engine = MagicMock()
    mock_coach_engine.recommend_sideboard.return_value = "IN:\n- 1x Plummet\nOUT:\n- 1x Giant Growth\nPLAN:\nAnswer flyer."

    mock_ui = MagicMock()

    with patch.object(StandaloneCoach, "_init_mcp"):
        standalone = StandaloneCoach()
        standalone._mcp = mock_mcp
        standalone._coach = mock_coach_engine
        standalone.ui = mock_ui

        rec = standalone.get_sideboard_recommendations()

        assert rec is not None
        assert "Plummet" in rec
        mock_coach_engine.recommend_sideboard.assert_called_once()
        mock_ui.advice.assert_called_with(rec, "SIDEBOARD")


def test_gamestate_last_game_opponent_cards_survive_intermission_reset():
    """prepare_for_game_end() must stash played_cards before reset() wipes them."""
    from arenamcp.gamestate import GameState, Player

    gs = GameState()
    gs.set_local_seat_id(1, source=2)
    gs.players[1] = Player(seat_id=1)
    gs.players[2] = Player(seat_id=2)
    gs.played_cards = {1: [100], 2: [301, 302]}

    # IntermissionReq handler order between Bo3 games: capture, then reset.
    gs.prepare_for_game_end()
    gs.reset()

    assert gs.get_opponent_played_cards() == []
    assert gs.get_last_game_opponent_played_cards() == [301, 302]

    # Bounded to the last game: the next game end overwrites the stash.
    gs.set_local_seat_id(1, source=2)
    gs.players[1] = Player(seat_id=1)
    gs.players[2] = Player(seat_id=2)
    gs.played_cards = {2: [999]}
    gs.prepare_for_game_end()
    gs.reset()
    assert gs.get_last_game_opponent_played_cards() == [999]


def test_standalone_sideboard_falls_back_to_pre_reset_stash():
    """Between Bo3 games the IntermissionReq reset() has wiped played_cards —
    /sideboard must fall back to the pre-reset game-end stash."""
    from arenamcp import server
    from arenamcp.gamestate import GameState, Player

    gs = GameState()
    gs.set_local_seat_id(1, source=2)
    gs.players[1] = Player(seat_id=1)
    gs.players[2] = Player(seat_id=2)
    gs.played_cards = {2: [301]}
    gs.prepare_for_game_end()
    gs.reset()
    assert gs.get_opponent_played_cards() == []

    mock_mcp = MagicMock()
    mock_mcp.get_game_state.return_value = {
        "deck_cards": [101],
        "sideboard_cards": [201],
        # No opponent_played_cards in the snapshot (the between-games case).
    }
    mock_mcp.get_card_info.side_effect = lambda cid: {
        101: {"name": "Llanowar Elves", "type_line": "Creature", "oracle_text": "{T}: Add {G}."},
        201: {"name": "Plummet", "type_line": "Instant", "oracle_text": "Destroy target creature with flying."},
        301: {"name": "Serra Angel", "type_line": "Creature", "oracle_text": "Flying, vigilance"},
    }.get(cid, {})

    mock_coach_engine = MagicMock()
    mock_coach_engine.recommend_sideboard.return_value = (
        "IN:\n- 1x Plummet\nOUT:\n- 1x Llanowar Elves\nPLAN:\nAnswer flyer."
    )

    with patch.object(StandaloneCoach, "_init_mcp"), \
         patch.object(server, "get_opponent_played_cards", return_value=[]), \
         patch.object(server, "game_state", gs):
        standalone = StandaloneCoach()
        standalone._mcp = mock_mcp
        standalone._coach = mock_coach_engine
        standalone.ui = MagicMock()

        rec = standalone.get_sideboard_recommendations()

    assert rec is not None
    mock_coach_engine.recommend_sideboard.assert_called_once()
    _, kwargs = mock_coach_engine.recommend_sideboard.call_args
    opp_seen = kwargs["opponent_cards_seen"]
    assert any("Serra Angel" in str(entry) for entry in opp_seen)


def test_pipe_adapter_slash_command_sideboard():
    """Test PipeAdapter slash command handler for /sideboard and /sb."""
    adapter = PipeAdapter()
    mock_coach = MagicMock()
    adapter.bind_coach(mock_coach)

    with patch("threading.Thread") as mock_thread:
        assert adapter._try_slash_command("/sideboard") is True
        mock_thread.assert_called_once()

    with patch("threading.Thread") as mock_thread:
        assert adapter._try_slash_command("/sb") is True
        mock_thread.assert_called_once()

    with patch("threading.Thread") as mock_thread:
        assert adapter._try_slash_command("/analyze") is True
        mock_thread.assert_called_once()
