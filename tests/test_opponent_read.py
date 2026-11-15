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


@pytest.mark.parametrize("hidden", [{"identity_known": False}, {"face_down_reason": 8}, {"isFacedown": True}])
def test_hidden_evidence_wins_over_public_visibility(hidden):
    state = captured(328)
    card = state["zones"]["battlefield"].pop()
    state["zones"]["exile"] = [{**card, "visibility": "Visibility_Public", **hidden}]
    assert not OpponentRead().plan_block(state)
    assert not cast_read(OpponentRead(), state)


def test_vito_targets_while_priest_drains_each_opponent_per_gain():
    from dataclasses import asdict

    from arenamcp.card_facts.reviewed_notes import corpus

    state = captured(327)
    sources = corpus()[0]

    def permanent(gid):
        return dict(
            asdict(sources[gid]),
            grp_id=gid,
            instance_id=100 + gid,
            owner_seat_id=2,
            controller_seat_id=2,
            object_kind="CARD",
        )

    state["zones"]["battlefield"] = [permanent(73310), permanent(71909)]
    read = OpponentRead()
    by_card = {v["evidence"][0]["name"]: v["text"] for v in cast_read(read, state).values()}
    assert "each opponent 1 life" in by_card["Marauding Blight-Priest"]
    assert "target opponent lose that much life" in by_card["Vito, Thorn of the Dusk Rose"]
    # A conflicting printed version has no standing as a source.
    state["zones"]["battlefield"] = [{**permanent(71909), "oracle_text": "Different rules"}]
    assert not cast_read(OpponentRead(), state)


def test_confirmed_pair_and_seen_threat_precede_unseen_partners():
    from dataclasses import asdict

    from arenamcp.card_facts.reviewed_notes import corpus

    state = captured(328)
    for gid in (73310, 81841, 94046):
        state["zones"]["battlefield"].append(
            {
                **asdict(corpus()[0][gid]),
                "grp_id": gid,
                "instance_id": gid + 100,
                "owner_seat_id": 2,
                "controller_seat_id": 2,
                "object_kind": "CARD",
            }
        )
    read = OpponentRead()
    facts = list(cast_read(read, state).values())
    assert {e["name"] for e in facts[0]["evidence"]} == {"Ball Lightning", "Minsc & Boo, Timeless Heroes"}
    assert len(facts[-1]["evidence"]) == 1 and "no confirmed sighting" in facts[-1]["text"]
    block = read.plan_block(state)
    assert "Both Ball Lightning" in block and "each opponent 1 life" in block
    assert "Lunar Convocation is a possible partner" not in block


def test_native_public_rules_follow_two_observed_prints_without_scripted_inference(monkeypatch):
    from types import SimpleNamespace

    from arenamcp.opponent_read import mark_public_native_prints

    source = json.loads((Path(__file__).parent / "aoa/fixtures/public_20261114_193910.json").read_text())
    ids = (93636, 91652)
    cards = {
        card["grp_id"]: {**card, "owner_seat_id": 2, "object_kind": "CARD"}
        for card in source["zones"]["battlefield"]
        if card.get("grp_id") in ids
    }
    prints = {
        g: SimpleNamespace(**{**card, "is_token": False, "linked_face_ids": []}) for g, card in cards.items()
    }
    db = SimpleNamespace(
        get_raw_mtgadb=lambda: SimpleNamespace(
            cached_cards=lambda gids: {g: prints[g] for g in gids if g in prints}
        )
    )
    monkeypatch.setattr("arenamcp.card_db.initialized_card_database", lambda: db)
    reader = OpponentRead()

    def view(seq, members, zone="battlefield"):
        state = captured(327)
        state["commentary_source_sequence"] = seq
        state["zones"] = {zone: members}
        mark_public_native_prints(state)
        return state

    first = view(1, [cards[ids[0]].copy()])
    one = reader.plan_block(first)
    assert cards[ids[0]]["name"] in one and cards[ids[1]]["name"] not in one
    second = view(2, [cards[ids[1]].copy()])
    (fact,) = cast_read(reader, second).values()
    assert fact["kind"] == "opponent_revealed_rules"
    assert [r["grp_id"] for r in fact["evidence"]] == list(reversed(ids))
    assert all(r["observed_zone"] == "battlefield" for r in fact["evidence"])
    assert reader.plan_block(first) == one
    assert "What plan, if any" in reader.plan_block(second)
    assert not OpponentRead().plan_block(view(3, [cards[ids[1]].copy()], "hand"))
    for changes in (
        {"oracle_text": "PRIVATE_CANARY"},
        {"is_copy": True},
        {"face_down": True},
        {"identity_known": False},
        {"owner_seat_id": 1},
        {"grp_id": -1},
        {"object_kind": "ABILITY"},
        {"controller_seat_id": True},
    ):
        modified = {**cards[ids[0]], **changes, "public_native_print": "PRIVATE_CANARY"}
        state = view(1, [modified])
        assert not cast_read(OpponentRead(), state)
        assert "PRIVATE_CANARY" not in OpponentRead().plan_block(state)
    stale = first["zones"]["battlefield"][0].copy()
    stale["grp_id"] = ids[1]
    first["zones"]["battlefield"] = [stale]
    assert not cast_read(OpponentRead(), first)


def test_cold_or_busy_native_catalog_abstains():
    from threading import Lock

    from arenamcp.mtgadb import MTGADatabase

    db = object.__new__(MTGADatabase)
    db._conn_lock = Lock()
    db._card_cache = {22: "already read"}
    assert db.cached_cards({11, 22}) == {22: "already read"}
    with db._conn_lock:
        assert not db.cached_cards({22})
