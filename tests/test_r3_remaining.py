import pytest
from arenamcp.mcts_evaluator import MCTSEvaluator
from arenamcp.magezero_client import MageZeroClient
from arenamcp.magezero_encoder import MageZeroStateEncoder
from test_mcts_cache_semantics import _base_state

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(MageZeroClient, 'check_health', classmethod(lambda cls, **kw: False))
    MCTSEvaluator.reset_cache()
    yield
    MCTSEvaluator.reset_cache()

def test_summoning_sickness_invalidates():
    s = _base_state()
    before = MageZeroStateEncoder.encode(s)
    first = MCTSEvaluator.evaluate(s)
    s['battlefield'][1]['is_summoning_sick'] = True
    assert MageZeroStateEncoder.encode(s) != before
    assert MCTSEvaluator.evaluate(s) is not first

def test_format_change_invalidates():
    s = _base_state()
    s['format_profile'] = {'family': 'constructed', 'variant': 'standard'}
    first = MCTSEvaluator.evaluate(s)
    s['format_profile'] = {'family': 'limited', 'variant': 'draft'}
    cached = MCTSEvaluator.evaluate(s)
    forced = MCTSEvaluator.evaluate(s, force=True)
    assert first.format_summary != forced.format_summary
    assert cached.format_summary == forced.format_summary

def test_deck_selection_input_invalidates():
    s = _base_state()
    s['hero_deck_list'] = ['Forest'] * 60
    first = MCTSEvaluator.evaluate(s)
    s['hero_deck_list'] = ['Island'] * 60
    assert MCTSEvaluator.evaluate(s) is not first

def test_null_player_count_fallback_invalidates():
    s = _base_state()
    s['players'][1]['hand_count'] = None
    first = MCTSEvaluator.evaluate(s)
    s['players'][1]['cards_in_hand'] = 1
    cached = MCTSEvaluator.evaluate(s)
    forced = MCTSEvaluator.evaluate(s, force=True)
    assert first.root_win_probability != forced.root_win_probability
    assert cached.root_win_probability == forced.root_win_probability

def test_mixed_raw_card_entries_do_not_crash():
    s = _base_state()
    s['hand'].append('Forest')
    MCTSEvaluator.evaluate(s)
