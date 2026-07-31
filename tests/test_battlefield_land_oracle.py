"""Battlefield land oracle text must reach the prompt (fix #395).

Root cause: ``_format_board_card`` skipped oracle text for ALL lands
(``if raw_oracle and not is_land``), so non-basic lands with activated
abilities — like Evendo, Waking Haven whose ability requires 12+ charge
counters from stationing — were invisible to the model beyond name/type.

The fix lets non-basic lands with abilities through, capped at 300 chars.
"""

from __future__ import annotations

from collections import Counter

from arenamcp.coach import CoachEngine

LOCAL_SEAT = 1
TURN_NUM = 6


def _fmt() -> CoachEngine:
    """CoachEngine usable for pure formatting (no __init__)."""
    return CoachEngine.__new__(CoachEngine)


# ── helpers ──────────────────────────────────────────────────────────────


def _land(
    name: str,
    type_line: str = "Land — Planet",
    oracle_text: str = "",
    is_tapped: bool = False,
    basic: bool = False,
    counters: dict | None = None,
) -> dict:
    return {
        "instance_id": 100,
        "name": name,
        "type_line": type_line,
        "oracle_text": oracle_text,
        "owner_seat_id": LOCAL_SEAT,
        "controller_seat_id": LOCAL_SEAT,
        "is_tapped": is_tapped,
        "turn_entered_battlefield": TURN_NUM,
        "counters": counters or {},
    }


def _basic_forest() -> dict:
    return _land(
        "Forest",
        type_line="Basic Land — Forest",
        oracle_text="({T}: Add {G}.)",
        basic=True,
    )


def _evendo() -> dict:
    """Evendo, Waking Haven — non-basic land with station mechanic."""
    return _land(
        "Evendo, Waking Haven",
        type_line="Legendary Land — Planet",
        oracle_text=(
            "This land enters tapped.\n"
            "{T}: Add {G}.\n"
            "Station (Tap another creature you control: Put charge counters equal "
            "to its power on this Planet. Station only as a sorcery.)\n"
            "12+ | {G}, {T}: Add {G} for each creature you control."
        ),
    )


def _format_lines(card: dict) -> list[str]:
    """Render one board card the same way _format_game_context does."""
    return _fmt()._format_board_card(
        card,
        local_seat=LOCAL_SEAT,
        turn_num=TURN_NUM,
        attachments={},
        name_counts=Counter({card["name"]: 1}),
        name_seen={},
        is_local=True,
        for_planner=False,
    )


# ── tests ────────────────────────────────────────────────────────────────


class TestLandHasAbilities:
    """Unit tests for the _land_has_abilities helper."""

    def test_basic_forest_has_no_abilities(self) -> None:
        assert CoachEngine._land_has_abilities("({T}: Add {G}.)", "Basic Land — Forest") is False

    def test_basic_island_has_no_abilities(self) -> None:
        assert CoachEngine._land_has_abilities("({T}: Add {U}.)", "Basic Land — Island") is False

    def test_evendo_is_nonbasic_has_abilities(self) -> None:
        oracle = (
            "This land enters tapped.\n"
            "{T}: Add {G}.\n"
            "Station (...).\n"
            "12+ | {G}, {T}: Add {G} for each creature you control."
        )
        assert CoachEngine._land_has_abilities(oracle, "Legendary Land — Planet") is True

    def test_nonbasic_mana_land_has_abilities(self) -> None:
        # Shock land — even simple non-basics should show oracle
        assert (
            CoachEngine._land_has_abilities(
                "{T}: Add {R}.\nMay have {R}, sacrificing 2 life.",
                "Land — Volcano",
            )
            is True
        )

    def test_basic_with_no_oracle(self) -> None:
        assert CoachEngine._land_has_abilities("", "Basic Land — Plains") is False


class TestBattlefieldLandOracleDisplay:
    """Non-basic land oracle text must appear in the battlefield section."""

    def test_evendo_oracle_reaches_prompt(self) -> None:
        """Evendo's 12+ condition must be visible in the rendered board."""
        lines = _format_lines(_evendo())

        # The card name must appear
        assert any("Evendo, Waking Haven" in line for line in lines)

        # Oracle text must be included (this was missing before the fix)
        # After reminder-text removal, the key line is:
        #   "12+ | {G}, {T}: Add {G} for each creature you control."
        full_text = "\n".join(lines)
        assert "12+" in full_text, "Evendo's 12+ charge counter threshold should be visible in the prompt"
        assert "creature you control" in full_text, (
            "Evendo's activated ability effect should be visible in the prompt"
        )

    def test_basic_forest_oracle_not_shown(self) -> None:
        """Basic lands still skip oracle text — no regression."""
        lines = _format_lines(_basic_forest())
        full_text = "\n".join(lines)

        # Name should appear
        assert "Forest" in full_text

        # Reminder text should NOT appear (stripped and keyword-only for basic)
        assert "Add {G}" not in full_text

    def test_long_resident_evendo_shows_oracle_in_coach_mode(self) -> None:
        """Coach mode (for_planner=False) always shows non-basic land oracle."""
        card = _evendo()
        card["turn_entered_battlefield"] = 1  # entered long ago
        lines = _fmt()._format_board_card(
            card,
            local_seat=LOCAL_SEAT,
            turn_num=TURN_NUM,
            attachments={},
            name_counts=Counter({"Evendo, Waking Haven": 1}),
            name_seen={},
            is_local=True,
            for_planner=False,  # Coach mode — always show
        )
        full_text = "\n".join(lines)
        assert "12+" in full_text

    def test_long_resident_evendo_hidden_in_planner_mode(self) -> None:
        """Planner mode (for_planner=True) hides old permanents' oracle."""
        card = _evendo()
        card["turn_entered_battlefield"] = 1  # entered long ago
        lines = _fmt()._format_board_card(
            card,
            local_seat=LOCAL_SEAT,
            turn_num=TURN_NUM,
            attachments={},
            name_counts=Counter({"Evendo, Waking Haven": 1}),
            name_seen={},
            is_local=True,
            for_planner=True,  # Planner mode — skip old permanents
        )
        full_text = "\n".join(lines)
        # Should NOT have oracle text in planner mode for old permanents
        assert "12+" not in full_text

    def test_recent_evendo_shows_oracle_in_planner_mode(self) -> None:
        """Planner mode keeps oracle for recently-entered permanents."""
        card = _evendo()
        card["turn_entered_battlefield"] = TURN_NUM  # just entered
        lines = _fmt()._format_board_card(
            card,
            local_seat=LOCAL_SEAT,
            turn_num=TURN_NUM,
            attachments={},
            name_counts=Counter({"Evendo, Waking Haven": 1}),
            name_seen={},
            is_local=True,
            for_planner=True,
        )
        full_text = "\n".join(lines)
        assert "12+" in full_text, "Recent ETB oracle should be visible even in planner mode"

    def test_oracle_capped_at_300_chars_for_lands(self) -> None:
        """Very long land oracle text is capped at 300 chars."""
        long_oracle = "X" * 500  # Simulate a very verbose oracle
        card = _land(
            "Verbose Land",
            type_line="Land",
            oracle_text=long_oracle,
        )
        lines = _format_lines(card)
        full_text = "\n".join(lines)

        # The "X" * 300 + "..." should be present, not 500
        # After reminder removal (no parens), we check length of the oracle line
        oracle_line = [l for l in lines if "X" in l]
        assert len(oracle_line) == 1
        # The line should contain at most 300 X's plus ellipsis
        assert oracle_line[0].count("X") <= 300
