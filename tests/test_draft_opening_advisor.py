"""Opening decisions must not inherit the deck they are supposed to discover."""

import json
from unittest.mock import Mock

import pytest

from arenamcp.draft_advisor import DraftAdvisor


def card(gid, name, cost, text, type_line="Artifact"):
    return dict(grp_id=gid, name=name, mana_cost=cost, oracle_text=text, type_line=type_line)


EXTRUDER = card(
    1,
    "Legion Extruder",
    "{1}{R}",
    "When this artifact enters, it deals 2 damage to any target.\n"
    "{2}, {T}, Sacrifice another artifact: Create a 3/3 colorless Golem artifact creature token.",
)
HOLE = card(
    2,
    "Portable Hole",
    "{W}",
    "When this artifact enters, exile target nonland permanent an opponent controls "
    "with mana value 2 or less until this artifact leaves the battlefield.",
)
MOX = card(3, "Mox Emerald", "{0}", "{T}: Add {G}.")


def payload(ids, partners=()):
    return {
        "picks": [dict(grp_id=gid, reason="A useful card.", synergy_with=list(partners)) for gid in ids],
        "plan": "Build a red-white artifact sacrifice deck.",
        "lane": "WR",
        "needs": ["More artifact fodder"],
    }


@pytest.mark.parametrize("picks_per_pack,pick,pool_size", [(1, 3, 2), (2, 4, 6), (1, 1, 12)])
def test_opening_drops_stale_plan_before_request_and_does_not_persist_new_commitment(
    picks_per_pack, pick, pool_size
):
    backend = Mock()
    backend.complete.return_value = json.dumps(payload([1, 3][:picks_per_pack]))
    advisor = DraftAdvisor(backend)
    advisor._event_name = "CubeDraft_Powered"
    advisor._lane = "WR"
    advisor._plan = "Build Boros artifacts; find fodder."
    pack = dict(
        event_name=advisor._event_name,
        pack_number=1,
        pick_number=pick,
        picks_per_pack=picks_per_pack,
        cards=[EXTRUDER, MOX],
        picked_cards=[HOLE] * pool_size,
        deck_plan=[{"lane": "WR", "needs": ["artifact fodder"]}],
    )
    try:
        result = advisor.recommend(pack, {})
        message = json.loads(backend.complete.call_args.args[1])
        assert message["draft_stage"] == "opening"
        assert message["previous_lane"] == message["previous_plan"] == ""
        assert message["deck_plan"] == []
        assert message["pool_summary"]["main_colors"] == []
        assert message["already_drafted_not_pickable"][0]["copies"] == pool_size
        assert result["lane"] == ""
        assert result["needs"] == []
        assert "red-white" in result["rejected_plan"]
        assert "red-white" not in advisor._plan
        advisor.recommend(pack, {})
        assert json.loads(backend.complete.call_args.args[1])["previous_plan"] == ""
    finally:
        advisor._executor.shutdown(wait=True)


@pytest.mark.parametrize("required,has_edges", [(1, False), (2, True)])
def test_pack_only_synergy_requires_being_able_to_pick_both(required, has_edges):
    backend = Mock()
    backend.complete.return_value = json.dumps(payload([1, 2][:required]))
    advisor = DraftAdvisor(backend)
    try:
        advisor.recommend(
            dict(pack_number=1, pick_number=1, picks_per_pack=required, cards=[EXTRUDER, HOLE]), {}
        )
        message = json.loads(backend.complete.call_args.args[1])
        assert bool(message["supported_synergies"]) == has_edges
    finally:
        advisor._executor.shutdown(wait=True)


def test_owned_synergy_retains_return_from_exile_cost_in_request_and_result():
    backend = Mock()
    backend.complete.return_value = json.dumps(payload([1], [2]))
    advisor = DraftAdvisor(backend)
    try:
        result = advisor.recommend(
            dict(pack_number=2, pick_number=1, cards=[EXTRUDER, MOX], picked_cards=[HOLE]), {}
        )
        message = json.loads(backend.complete.call_args.args[1])
        for edges in (message["supported_synergies"], result["recommendations"][0]["synergy_evidence"]):
            edge = next(e for e in edges if e["source_id"] == 2 and e["payoff_id"] == 1)
            assert edge["cautions"][0]["code"] == "sacrifice_ends_exile_until_source_leaves"
        assert not any(e["source_id"] == 3 for e in message["supported_synergies"])
    finally:
        advisor._executor.shutdown(wait=True)
