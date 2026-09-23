from copy import deepcopy
import pytest
from arenamcp.mcts_evaluator import MCTSEvaluator
from test_mcts_cache_semantics import _base_state

@pytest.fixture(autouse=True)
def clean_cache():
    MCTSEvaluator.reset_cache()
    yield
    MCTSEvaluator.reset_cache()

def test_optional_card_metadata_does_not_crash():
    state = _base_state()
    state['hand'].append({'name': 'Forest', 'type_line': 'Basic Land — Forest'})
    MCTSEvaluator.evaluate(state)

def test_pending_decision_in_place_change_invalidates():
    state = _base_state()
    state['pending_decision'] = {'type': 'choose', 'options': ['A']}
    first = MCTSEvaluator.evaluate(state)
    state['pending_decision']['options'].append('B')
    assert MCTSEvaluator.evaluate(state) is not first

def test_attacking_flag_invalidates():
    state = _base_state()
    first = MCTSEvaluator.evaluate(state)
    state['battlefield'][1]['is_attacking'] = True
    assert MCTSEvaluator.evaluate(state) is not first

def test_oracle_text_invalidates():
    state = _base_state()
    first = MCTSEvaluator.evaluate(state)
    state['battlefield'][1]['oracle_text'] = 'Flying. Haste.'
    assert MCTSEvaluator.evaluate(state) is not first

def test_stack_order_invalidates():
    state = _base_state()
    state['stack'] = [{'name': 'Spell Pierce', 'instance_id': 9, 'type_line': 'Instant'}, {'name': 'Fading Hope', 'instance_id': 10, 'type_line': 'Instant'}]
    first = MCTSEvaluator.evaluate(state)
    state['stack'].reverse()
    assert MCTSEvaluator.evaluate(state) is not first

def test_top_level_opponent_hand_count_invalidates():
    state = _base_state()
    state['opponent_hand_count'] = 4
    first = MCTSEvaluator.evaluate(state)
    state['opponent_hand_count'] = 1
    assert MCTSEvaluator.evaluate(state) is not first
