import ast, subprocess
from copy import deepcopy
from collections import Counter
from tools.training import build_magezero_bridge as B, run_wp3_pipeline as P
from test_bridge_policy_eligibility import _row
from test_legacy_render_unknown_facts import _gs
from arenamcp.coach import CoachEngine
import arenamcp.coach as coach

def test_won_only_rejects_lost_records():
    result,reason=B.build_record(_row(outcome='lost'),outcome_mode='won_only')
    assert result is None

def test_render_accounting_reports_dropped_count():
    data=P._render_accounting({'train':[{}]*7},{'train':Counter()}, {'outcome_filter':3},10)
    assert data['dropped_by_filters']==3

def test_opponent_combat_flags_preserved():
    s=B.build_game_state(_row(battlefield_opp=[{'name':'Forest','attacking':True,'blocking':True}]))
    opp=next(c for c in s['battlefield'] if c['controller_seat_id']==2)
    assert opp['is_attacking'] and opp['is_blocking']

def test_live_board_formatter_matches_prechange():
    # Compile precisely the old method, using current unchanged module dependencies.
    src=subprocess.check_output(['git','show','a956ac1:src/arenamcp/coach.py'],text=True)
    tree=ast.parse(src)
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='CoachEngine')
    fn=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_format_board_card')
    ns=dict(coach.__dict__);exec(compile(ast.Module(body=[fn],type_ignores=[]),'<baseline-method>','exec'),ns)
    engine=CoachEngine.__new__(CoachEngine)
    card={'name':'Fixture Artifact','type_line':'Artifact','oracle_text':'At the beginning of your upkeep, draw a card.'}
    import inspect
    # Bind supplied parameters using the actual method signature.
    values={'card':card,'turn_num':5,'is_local':True,'local_seat':1,
            'name_counts':Counter({'Fixture Artifact':1}),'name_seen':Counter(),
            'for_planner':True,'attachments':{}}
    sig=inspect.signature(engine._format_board_card)
    kwargs={k:v for k,v in values.items() if k in sig.parameters}
    before=ns['_format_board_card'](engine,**deepcopy(kwargs))
    after=engine._format_board_card(**deepcopy(kwargs))
    assert after==before
