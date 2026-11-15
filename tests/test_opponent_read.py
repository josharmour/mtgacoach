"""Public reveal sequences from bug_20261114_193910, with match/player identities removed."""

import copy
import json
from pathlib import Path

import pytest

from arenamcp.commentary.events import ObservationAdapter
from arenamcp.game_plan import GamePlanManager
from arenamcp.opponent_read import OpponentRead


def captured(sequence):
    # Local seat 1, opponent seat 2. These instance IDs, owners and object kinds are
    # printed in the original public board. The Case resolves at seq 328, then Lunar
    # Convocation is cast at 335. Use only these two public objects, never either hand.
    path = Path(__file__).parent / "aoa/fixtures/public_20261114_193910.json"
    source = json.loads(path.read_text())
    observed = {332: ("battlefield", 328), 334: ("stack", 335)}
    zones = {"stack": [], "battlefield": []}
    for card in source["zones"]["battlefield"]:
        instance = card.get("instance_id")
        if instance not in observed:
            continue
        zone, start = observed[instance]
        if sequence >= start:
            zone = "battlefield" if instance == 334 and sequence >= 338 else zone
            zones[zone].append({**card, "object_kind": "CARD", "owner_seat_id": 2})
    return {
        **zones,
        "zones": zones,
        "turn": source["turn"],
        "players": source["players"],
        "match_id": "redacted",
        "game_number": 1,
        "local_seat_id": 1,
        "commentary_source_sequence": sequence,
    }


def cast_read(reader, state):
    return reader.observation_facts(ObservationAdapter().project(state))


def test_captured_sequence_updates_the_booth_and_plan():
    reader = OpponentRead()
    early = cast_read(reader, captured(328))
    assert len(early) == 1
    hypothesis = next(iter(early.values()))
    assert "Lunar Convocation is a possible partner" in hypothesis["text"]
    assert [c["name"] for c in hypothesis["evidence"]] == ["Case of the Uneaten Feast"]
    assert "read" in hypothesis["text"]
    assert not cast_read(reader, captured(327))
    late = cast_read(reader, captured(335))
    assert len(late) == 2
    assert all(k.startswith("state:opponent_read:") for k in late)
    assert all(f["visibility"] == "public" and f["opponent_seat"] == 2 for f in late.values())
    assert all("redacted" not in str(f) for f in late.values())
    pair, threat = late.values()
    assert "Both" in pair["text"] and "lose life that turn" in pair["text"]
    assert [c["observed_zone"] for c in pair["evidence"]] == ["battlefield", "stack"]
    assert "flying 1/1 Bat" in threat["text"]

    class Backend:
        def complete(self, system, user, max_tokens, **kwargs):
            self.prompt = user
            return '{"path":"race before the enchantments take over","win_conditions":["combat damage"]}'

    backend = Backend()
    manager = GamePlanManager(backend)
    assert "possible partner" in manager.strategy_block(captured(328))
    manager.maybe_reform(captured(335))
    assert "Opponent plan inferences from public cards" in backend.prompt
    assert "Both Case of the Uneaten Feast and Lunar Convocation have appeared" in backend.prompt
    assert "Lunar Convocation" in manager.strategy_block(captured(338))


def test_public_sightings_survive_departure_and_cannot_cross_games_or_seats():
    reader = OpponentRead()
    state = captured(335)
    facts = cast_read(reader, state)
    state["zones"] = {"battlefield": [], "hand": [{"name": "PRIVATE_HAND_CANARY"}]}
    state["commentary_source_sequence"] = 340
    assert cast_read(reader, state) == facts
    for field, value in (("match_id", "next"), ("game_number", 2), ("local_seat_id", 2)):
        assert not cast_read(reader, {**state, field: value})
    assert not reader.plan_block({**state, "match_id": ""})
    assert not reader.plan_block({**state, "local_seat_id": True})


@pytest.mark.parametrize(
    "patch",
    [
        {"owner_seat_id": 1},
        {"controller_seat_id": 1},
        {"owner_seat_id": None},
        {"owner_seat_id": True},
        {"face_down": True},
        {"isFacedown": True},
        {"visibility": "Private"},
        {"object_kind": "TOKEN"},
        {"object_kind": "ABILITY"},
        {"is_copy": True},
        {"copied_from_grp_id": 99},
        {"original_grp_id": 99},
        {"base_grp_id": 99},
        {"token_creator_id": 7},
        {"is_token": True},
        {"oracle_text": "different printing"},
        {"grp_id": 3},
        {"grp_id": True},
    ],
)
def test_hidden_stolen_or_unconfirmed_sources_create_no_hypothesis(patch):
    state = captured(328)
    state["zones"]["battlefield"][0].update(patch)
    assert not OpponentRead().plan_block(state)
    assert not cast_read(OpponentRead(), state)


def test_commander_print_and_private_canaries():
    from arenamcp.card_facts.reviewed_notes import corpus

    print_ = corpus()[0][71283]
    commander = dict(
        name=print_.name,
        oracle_text=print_.oracle_text,
        mana_cost=print_.mana_cost,
        type_line=print_.type_line,
        grp_id=71283,
        owner_seat_id=2,
        controller_seat_id=2,
        object_kind="CARD",
        instance_id=42,
    )
    state = captured(327)
    state["zones"].update(
        command=[commander],
        hand=[dict(commander, name="Private hand canary")],
        opponent_library=[dict(commander, name="Private library canary")],
    )
    state["revealed_cards"] = [dict(commander, name="untrusted hand identity")]
    original = copy.deepcopy(state)
    text = OpponentRead().plan_block(state)
    assert "Winota" in text and "as an attacker" in text
    assert "canary" not in text and "untrusted" not in text and state == original
    commander["grp_id"] = 96294
    assert not OpponentRead().plan_block(state)


@pytest.mark.parametrize(
    "visibility, expected", [(None, False), ("Visibility_Private", False), ("Visibility_Public", True)]
)
def test_exile_identity_requires_public_visibility_in_both_plan_and_booth(visibility, expected):
    state = captured(328)
    card = state["zones"]["battlefield"].pop()
    state["zones"]["exile"] = [{**card, "visibility": visibility}]
    assert bool(OpponentRead().plan_block(state)) == expected
    assert bool(cast_read(OpponentRead(), state)) == expected
