"""Copies are not independent E4 observations; individual BO3 events remain."""

import copy

import pytest
from tools.upgrade_metrics import pivotal_thinking_summary


def bundle(match, pivotal, routine=10):
    return {
        "match_id": match,
        "packet": {
            "decisions": [
                {
                    "trace": {
                        "pivotal_thinking": {
                            "category": category,
                            "calls": 1,
                            "call_usage": [{"reasoning_tokens": value}],
                        }
                    }
                }
                for category, value in (("pivotal", pivotal), ("routine", routine))
            ]
        },
    }


def test_duplicate_does_not_flip_tenfold_gate_or_reweight():
    a, b = bundle("a", 160), bundle("b", 20)
    before = copy.deepcopy([a, b])
    base = pivotal_thinking_summary([a, b])
    result = pivotal_thinking_summary([a, b, copy.deepcopy(a)])
    assert not base["tenfold_token_gate_met"]
    assert result["duplicate_bundles_ignored"] == 1
    assert {k: v for k, v in result.items() if k != "duplicate_bundles_ignored"} == {
        k: v for k, v in base.items() if k != "duplicate_bundles_ignored"
    }
    assert [a, b] == before


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("metadata_only", [False, True])
def test_conflicting_match_refuses_without_identity_disclosure(reverse, metadata_only):
    a = bundle("private-match", 160)
    b = copy.deepcopy(a)
    if metadata_only:
        b["timestamp"] = "changed"
    else:
        b["packet"]["decisions"][0]["trace"]["pivotal_thinking"]["call_usage"][0]["reasoning_tokens"] = 1
    with pytest.raises(ValueError, match="^conflicting copies of a match in E4 evidence$"):
        pivotal_thinking_summary([b, a] if reverse else [a, b])


def test_distinct_decisions_in_single_bo3_bundle_are_preserved():
    a = bundle("a", 100)
    a["packet"]["decisions"] *= 3
    result = pivotal_thinking_summary([a] * 20)
    assert result["observed_matches"] == 1
    assert result["categories"]["pivotal"]["decisions"] == 3
    assert result["categories"]["routine"]["decisions"] == 3
    assert result["duplicate_bundles_ignored"] == 19
    assert result["overall_e4_gate_met"] is False


def test_key_order_copy_ignored():
    a = bundle("a", 100)
    b = dict(reversed(list(a.items())))
    assert pivotal_thinking_summary([a, b])["duplicate_bundles_ignored"] == 1
