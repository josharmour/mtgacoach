"""Attack-tax awareness (Ghostly Prison / Propaganda class effects).

Field report 2026-07-16: opponent had Ghostly Prison; the coach advised
casting until empty, then attacking with six creatures — a {12} tax it
never counted. The prompt must state the per-creature price and how many
attackers current mana can cover.
"""

from unittest import mock

from arenamcp.coach import CoachEngine

GHOSTLY_PRISON_ORACLE = (
    "Creatures can't attack you unless their controller pays {o2} for each "
    "creature they control that's attacking you."
)
PROPAGANDA_ORACLE = (
    "Creatures can't attack you unless their controller pays {2} for each "
    "creature they control that's attacking you."
)


def _coach():
    with mock.patch.object(CoachEngine, "__init__", lambda self: None):
        c = CoachEngine()
    return c


def _card(name, oracle="", owner=2, tapped=False, types="Enchantment"):
    return {
        "name": name,
        "oracle_text": oracle,
        "owner_seat_id": owner,
        "is_tapped": tapped,
        "type_line": types,
        "grp_id": 1,
    }


def _game_state(opp_extras=(), lands=4):
    battlefield = [
        _card("Ghostly Prison", GHOSTLY_PRISON_ORACLE, owner=2),
        *opp_extras,
    ]
    for i in range(lands):
        battlefield.append(
            _card(f"Forest {i}", owner=1, types="Basic Land — Forest")
        )
    return {
        "players": [
            {"seat_id": 1, "is_local": True},
            {"seat_id": 2, "is_local": False},
        ],
        "battlefield": battlefield,
        "hand": [],
        "decision_context": {"type": "declare_attackers",
                             "legal_attackers": ["Bear", "Spirit"]},
        "pending_decision": "Declare Attackers",
        "turn": {"turn_number": 5, "phase": "Phase_Combat"},
    }


def test_detects_arena_encoded_tax():
    c = _coach()
    taxes = c._detect_attack_taxes([_card("Ghostly Prison", GHOSTLY_PRISON_ORACLE)])
    assert taxes == [("Ghostly Prison", 2)]


def test_detects_plain_encoded_tax():
    c = _coach()
    taxes = c._detect_attack_taxes([_card("Propaganda", PROPAGANDA_ORACLE)])
    assert taxes == [("Propaganda", 2)]


def test_no_tax_no_lines():
    c = _coach()
    assert c._attack_tax_lines([_card("Grizzly Bears", "")], None) == []


def test_tax_lines_state_price_and_budget():
    c = _coach()
    gs = _game_state(lands=4)
    with mock.patch.object(CoachEngine, "_available_mana_now", return_value=4):
        lines = c._attack_tax_lines(
            [b for b in gs["battlefield"] if b["owner_seat_id"] == 2], gs
        )
    text = "\n".join(lines)
    assert "ATTACK TAX" in text
    assert "Ghostly Prison" in text
    assert "{2} PER CREATURE" in text
    assert "at most 2 attacker(s)" in text
    assert "BUDGET MANA BEFORE CASTING SPELLS" in text


def test_stacked_taxes_sum():
    c = _coach()
    cards = [
        _card("Ghostly Prison", GHOSTLY_PRISON_ORACLE),
        _card("Propaganda", PROPAGANDA_ORACLE),
    ]
    with mock.patch.object(CoachEngine, "_available_mana_now", return_value=8):
        lines = c._attack_tax_lines(cards, {"battlefield": []})
    text = "\n".join(lines)
    assert "{4} PER CREATURE" in text
    assert "at most 2 attacker(s)" in text
