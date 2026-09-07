import pytest
from arenamcp.mcts_evaluator import MCTSEvaluator as E, MCTSBranch
from arenamcp import magezero_gating as G

def state(cost=None, pool=None, oracle=''):
    card={'name':'Fixture Permanent','type_line':'Artifact','oracle_text':oracle}
    if cost is not None: card['mana_cost']=cost
    return {'local_seat_id':1,'turn':{'turn_number':5,'phase':'Phase_Main1','active_player':1,'priority_player':1},
      'players':[{'seat_id':1,'is_local':True,'life_total':20,'mana_pool':pool or {},'lands_played':0},
                 {'seat_id':2,'life_total':20}], 'hand':[card], 'battlefield':[], 'stack':[]}

@pytest.mark.parametrize('cost',[None,'{W/U}','{W/P}'])
def test_unknown_or_unsupported_cost_rejected(cost):
    assert E._create_mechanical_afterstate(state(cost),MCTSBranch(action='Cast: Fixture Permanent',action_type='cast')) is None

def test_gre_legal_action_does_not_replace_a_payment_plan():
    s=state('{U}',{'R':1});s['legal_actions']=['Cast Fixture Permanent']
    assert E._create_mechanical_afterstate(s,MCTSBranch(action='Cast: Fixture Permanent',action_type='cast')) is None

def test_etb_target_mechanics_rejected():
    s=state('{1}',{'C':1},'When this artifact enters the battlefield, destroy target creature.')
    assert E._create_mechanical_afterstate(s,MCTSBranch(action='Cast: Fixture Permanent',action_type='cast')) is None

def test_hand_zero_resolution_agrees():
    s=state();s['players'][1].update(hand_count=0,cards_in_hand=4)
    assert E._resolve_opponent_hand_count(s,1)==G._resolve_opponent_hand_count_and_tier(s)

def test_actual_revealed_duplicates_are_subtracted(monkeypatch):
    # Use real OpponentModel.classify; do not inject a profile with artificial duplicates.
    monkeypatch.setattr(G,'_GAUNTLET_POOLS',{'fixture':['Forest','Forest','Island']})
    s=state();s['opponent_hand_count']=1
    s['battlefield']=[{'name':'Forest','type_line':'Basic Land — Forest','controller_seat_id':2,'instance_id':i} for i in (10,11)]
    meta=G.sample_opponent_hands_meta(s,seed=1)
    assert meta.pool_size==1
    assert all(sample==['Island'] for sample in meta.samples)

def test_unknown_hand_is_not_sent_as_known_empty(monkeypatch):
    from types import SimpleNamespace
    from arenamcp.model_zoo import ModelZooClient
    from arenamcp.magezero_client import MageZeroClient
    from arenamcp.opponent_model import OpponentModel
    captured=[]
    selected=SimpleNamespace(label='Fixture',similarity=1.,model_spec=SimpleNamespace(model_id='fixture'))
    monkeypatch.setattr(ModelZooClient,'select',classmethod(lambda cls,*a,**kw:selected))
    monkeypatch.setattr(MageZeroClient,'check_health',classmethod(lambda cls,**kw:True))
    def evaluate(cls,items,**kw): captured.extend(items);return None
    monkeypatch.setattr(MageZeroClient,'evaluate_batch',classmethod(evaluate))
    s=state()
    E._apply_magezero_lookahead(s,.5,[],OpponentModel.classify(s))
    assert not captured or all(hand is None for _,hand in captured)
