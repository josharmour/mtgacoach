"""Format labels must not invent a free London mulligan."""

import pytest

from arenamcp import mulligan_policy as mp


@pytest.mark.parametrize("format_name", ["Brawl Ladder", "Historic Brawl", "Brawl", "Standard", ""])
def test_format_name_without_explicit_evidence_grants_no_free_mulligan(format_name):
    state = {"format_name": format_name, "decision_context": {"mulliganCount": 4}}
    assert mp.free_mulligans_from_state(state) == 0
    hand = [mp.hand_card({"name": "Mountain", "type_line": "Basic Land — Mountain"})] * 7
    found = mp.MulliganSituation(
        hand, mp.mulligans_from_state(state), False, mp.free_mulligans_from_state(state)
    )
    assert found.bottom_count == 4
    assert found.keep_size == 3


@pytest.mark.parametrize(
    "evidence",
    [
        {"_free_mulligans": 1},
        {"decision_context": {"free_mulligan_count": 1}},
        {"decision_context": {"freeMulliganCount": 1}},
        {"_bridge_request_payload": {"freeMulliganCount": 1}},
        {"local_seat_id": 2, "players": [{"seat_id": 2, "free_mulligan_count": 1}]},
    ],
)
def test_explicit_free_mulligan_evidence_is_preserved(evidence):
    assert mp.free_mulligans_from_state({"format_name": "Brawl Ladder", **evidence}) == 1


def test_explicit_zero_overrides_other_evidence():
    assert (
        mp.free_mulligans_from_state({"_free_mulligans": 0, "decision_context": {"freeMulliganCount": 1}})
        == 0
    )
