"""Regression probes copied from the read-only Q3 audit (#293/#297)."""

from tests.test_cube_format_inference import driver
from tests.test_draft_unrated_fallback import make_driver

from arenamcp.set_primer import SetPrimer


def test_unknown_event_does_not_claim_a_retail_format_from_printings():
    d = driver()
    d._db.get_card.return_value.expansion_code = "LEA"
    d._new_run_if_needed("", 1, 1)
    inferred = d._set_code_for([1, 2, 3])
    assert inferred == "", f"Missing course metadata is inferred as retail {inferred}"


def test_empty_quick_primer_still_uses_rules_metadata_instead_of_pack_order():
    d, bridge, _ = make_driver([1, 2])
    d.run.event_name = "PremierDraft_UNKNOWN"
    d.run.set_code = "LEA"
    d._primers.get.return_value = SetPrimer(set_code="LEA", cards={})
    d._step_pick()
    submitted = [fields for action, fields in bridge.sent if action == "submit_draft_pick"]
    assert submitted[0]["cards"][0]["grp_id"] == 2, "Empty primer chose first vanilla over removal"


def test_unknown_context_discards_stale_retail_set_and_never_requests_primer():
    d = driver()
    d.run.set_code = "LEA"
    d._new_run_if_needed("", 1, 2)
    assert d.run.event_name == ""
    assert d.run.set_code == ""
    assert d._primer("LEA") is None
    d._primers.get.assert_not_called()
    d._primers.ensure.assert_not_called()
    d._primers.quick.assert_not_called()


def test_transient_missing_course_does_not_erase_known_event():
    d = driver()
    d._new_run_if_needed("PremierDraft_DMU_20261020", 1, 1)
    d.run.set_code = "DMU"
    d._new_run_if_needed("", 1, 2)
    assert d.run.event_name == "PremierDraft_DMU_20261020"
    assert d.run.set_code == "DMU"
    assert d._primer("DMU") is d._primers.get.return_value


def test_unknown_event_can_acquire_real_course_metadata_later():
    d = driver()
    d._new_run_if_needed("", 1, 1)
    assert d._set_code_for([1]) == ""
    d._new_run_if_needed("PremierDraft_DMU_20261020", 1, 2)
    assert d._set_code_for([1]) == "DMU"
    assert d.run.event_name == "PremierDraft_DMU_20261020"


def test_empty_synchronous_quick_primer_uses_local_rules():
    d, bridge, _ = make_driver([1, 2])
    d.run.event_name = "PremierDraft_LEA_Test"
    d.run.set_code = "LEA"
    d._primers.get.return_value = None
    d._primers.quick.return_value = SetPrimer(set_code="LEA", cards={})
    d._step_pick()
    d._primers.ensure.assert_called_once_with("LEA")
    d._primers.quick.assert_called_once_with("LEA")
    submitted = [fields for action, fields in bridge.sent if action == "submit_draft_pick"]
    assert submitted[0]["cards"][0]["grp_id"] == 2


def test_empty_primer_fallback_does_not_veto_valid_model_pick():
    from unittest.mock import Mock

    advisor = Mock()
    advisor.recommend.return_value = {
        "reasoning_source": "card_rules",
        "recommendations": [{"grp_id": 1, "reason": "A role the heuristic misses."}],
    }
    d, _, _ = make_driver([1, 2], advisor=advisor)
    d.run.event_name = "PremierDraft_LEA_Test"
    d._primers.get.return_value = SetPrimer(set_code="LEA", cards={})
    primer = d._primer("LEA")
    assert primer is None
    result = d._refine_pick([1, 2], primer, [], [], 1)
    assert result[0] == [1]
    assert advisor.recommend.call_args.args[1]["evaluations"][0]["grp_id"] == 2


def test_populated_primer_with_unrated_metadata_remains_available():
    from arenamcp.set_primer import SetCard

    d = driver()
    d.run.event_name = "PremierDraft_TST_Test"
    primer = SetPrimer("TST", cards={1: SetCard(1, "Unrated card", "R", "R", "Creature", 2, "Haste")})
    d._primers.get.return_value = primer
    assert d._primer("TST") is primer


def test_unknown_deck_context_does_not_pass_stale_printing_set_to_pool_lookup(monkeypatch):
    from unittest.mock import Mock

    d = driver()
    d.run.set_code = "LEA"
    d._pool_cards_fn = Mock(return_value=[])
    d._restore_plan = lambda ids: ""
    monkeypatch.setattr("arenamcp.limited_deck.fallback_deck", lambda *args, **kwargs: {})
    assert d._decide_deck([1, 2, 3]) is None
    d._pool_cards_fn.assert_called_once_with([1, 2, 3], "")
    d._primers.get.assert_not_called()
    assert d.run.event_name == d.run.set_code == ""
