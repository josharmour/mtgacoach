from arenamcp.cast_growth_risk import before_targeting_cast, grown, per_cast_growth
from arenamcp.play_safety import damage_would_kill


def mauler():
    return {
        "instance_id": 30,
        "controller_seat_id": 2,
        "name": "Taurean Mauler",
        "oracle_text": "Changeling\nWhenever an opponent casts a spell, you may put a +1/+1 counter on Taurean Mauler.",
        "power": 5,
        "toughness": 5,
        "damage": 2,
        "damaged_this_turn": True,
    }


def test_optional_opposing_cast_growth_precedes_damage():
    card = mauler()
    assert damage_would_kill(card, 3)
    assert per_cast_growth(card) == 1
    assert not damage_would_kill(grown(card), 3)
    assert grown(card, 2)["toughness"] == 7
    assert card["toughness"] == 5


def test_targeting_projects_only_a_new_cast_not_an_already_cast_spell():
    state = {"local_seat_id": 1, "hand": [{"instance_id": 10, "type_line": "Instant"}], "stack": []}
    assert before_targeting_cast(state, 10, mauler())["toughness"] == 6
    state["stack"] = [state["hand"].pop()]
    assert before_targeting_cast(state, 10, mauler())["toughness"] == 5


def test_own_cast_and_target_triggers_not_confused_with_opponent_casts():
    card = mauler()
    card["oracle_text"] = "Whenever you cast a spell, put a +1/+1 counter on this creature."
    assert per_cast_growth(card) == 0
