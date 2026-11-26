"""Tests for stripping internal Arena grp_ids and boilerplate from deck prose."""

from __future__ import annotations

from arenamcp.deck_strategy import clean_deck_prose
from arenamcp.desktop.decks_tab import report_html


def test_clean_deck_prose_strips_card_ids_and_preserves_game_numbers():
    raw = (
        "U/R noncreature-spell tempo-midrange (Star Trek-themed, 40 cards, no designated commander). "
        "Cast a noncreature spell nearly every turn to grow Cetacean Ops (106625) and charge "
        "Romulan Bird-of-Prey (106654), draw cards off Make It So (106648)/Consider (106628)/"
        "Suurok ETBs (106660), then win with a wide-flying/trampling board: Cetacean at 5-7 power, "
        "Khan (106732) with trample and ETB pings, stationed spacecraft fliers, with La'An (106733) "
        "disabling a blocker once attacking power totals 8+."
    )
    cleaned = clean_deck_prose(raw)
    for gid in ("106625", "106654", "106648", "106628", "106660", "106732", "106733"):
        assert gid not in cleaned
    assert "no designated commander" not in cleaned.lower()
    assert "(Star Trek-themed, 40 cards)" in cleaned
    assert "Make It So/Consider/Suurok ETBs" in cleaned
    assert "Cetacean at 5-7 power" in cleaned
    assert "8+" in cleaned

    # Opposite control: normal parenthetical stats/years and slash spacing remain intact.
    normal = "Makes (1/1) Servo tokens across early / mid turns and curves out cleanly (2026)."
    assert clean_deck_prose(normal) == normal


def test_report_html_and_playbook_report_clean_all_prose_fields():
    from types import SimpleNamespace

    from arenamcp.deck_library import playbook_report

    html = report_html(
        {
            "archetype": "Izzet Spells (no designated commander)",
            "summary": "Grow Cetacean Ops (106625) and win in the air.",
            "primary_plan": "Pair Khan (106732) with evasion.",
            "backup_plan": "Grind with Make It So (106648)/Consider (106628).",
            "mechanisms": [
                {
                    "effect": "Trigger Bird (106654) twice.",
                    "cards": ["Romulan Bird-of-Prey"],
                    "requires": ["Noncreature spell with Cetacean Ops (106625)"],
                    "avoid": ["Tapping out before Make It So (106648)"],
                }
            ],
            "key_cards": [{"name": "Cetacean Ops", "role": "Main payoff (106625) for spells."}],
            "phases": {"early": "Stick Spanner (106658)."},
        },
        name="Quick Draft TRK",
    )
    for gid in ("106625", "106732", "106648", "106628", "106654", "106658"):
        assert gid not in html
    assert "no designated commander" not in html.lower()
    assert "Make It So/Consider." in html
    assert "Main payoff for spells." in html

    pb = SimpleNamespace(
        data={
            "archetype": "Jeskai Blink",
            "spoken_summary": "Loop Suurok (106660).",
            "primary_plan": "Value.",
            "backup_plan": "Beatdown.",
            "mechanisms": [
                {
                    "id": "m1",
                    "effect": "Blink (106660)",
                    "cards": [106660],
                    "requires": ["Board presence with Khan (106732)"],
                    "avoid": ["Blocking with Ops (106625)"],
                }
            ],
            "decision_rules": [
                {
                    "id": "r1",
                    "decisions": ["cast"],
                    "baseline": "Hold Consider (106628)",
                    "when": "Up against Bird (106654)",
                    "prefer": "Cast Make It So (106648)",
                    "unless": "Lethal on board (no designated commander)",
                }
            ],
            "card_roles": {"106625": "Scaling threat (106625)."},
            "phases": {"late": "Close with La'An (106733)."},
            "commanders": [
                {
                    "card": 106732,
                    "deployment": "Cast Khan (106732) turn 4.",
                    "ongoing_value": "Ping with (106625).",
                    "replay_value": "",
                    "preserve_or_reuse": "",
                    "constraints": "Needs noncreature (106648).",
                }
            ],
        },
        catalog={
            106625: {"name": "Cetacean Ops"},
            106660: {"name": "Suurok"},
            106732: {"name": "Khan"},
        },
    )
    rep = playbook_report(pb)
    dumped = str(rep)
    for gid in ("106625", "106628", "106648", "106654", "106660", "106732", "106733"):
        assert gid not in dumped
    assert "no designated commander" not in dumped.lower()
