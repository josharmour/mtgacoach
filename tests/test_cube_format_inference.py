"""Powered Cube must not borrow a pack card's retail draft environment (#293)."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp.draft_event import DraftEventDriver


def driver():
    db = Mock()
    db.get_card.return_value = SimpleNamespace(expansion_code="DMU")
    return DraftEventDriver(
        bridge_fn=lambda: None, tracker_fn=lambda: None, primer_service=Mock(), card_db=db
    )


@pytest.mark.parametrize("event", ["CubeDraft_Powered_20261020", "CubeDraft_20261020"])
def test_cube_never_infers_retail_set_or_requests_its_primer(event):
    d = driver()
    d._new_run_if_needed(event, 1, 1)
    assert d._set_code_for([1, 2, 3]) == ""
    assert d._primer("DMU") is None
    d._primers.get.assert_not_called()
    d._primers.ensure.assert_not_called()
    d._primers.quick.assert_not_called()
    # A run that already inferred DMU must drop it on its next observation.
    d.run.set_code = "DMU"
    d._new_run_if_needed(event, 1, 2)
    assert d.run.set_code == ""


def test_normal_draft_retains_inference_and_primer():
    d = driver()
    d._new_run_if_needed("PremierDraft_DMU_20261020", 1, 1)
    assert d._set_code_for([1, 2, 3]) == "DMU"
    assert d._primer("DMU") is d._primers.get.return_value
    d._primers.get.assert_called_once_with("DMU")
