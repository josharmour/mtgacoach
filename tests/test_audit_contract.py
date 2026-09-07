from dataclasses import replace
import threading
import pytest
from arenamcp.model_zoo import ModelZooClient as Zoo, ModelSpec, ManifestError
from arenamcp.magezero_client import MageZeroClient
from arenamcp.format_profile import FormatProfile
from test_model_zoo import _v2_manifest

@pytest.fixture(autouse=True)
def setup(monkeypatch):
    Zoo.reset()
    monkeypatch.setattr(MageZeroClient, 'get_active_endpoint', classmethod(lambda cls: 'http://fixture-A'))
    yield
    Zoo.reset()

def seeded(monkeypatch, status='certified', warm=False):
    spec=replace(ModelSpec.from_manifest(_v2_manifest(warm=warm)), is_resident=True, promotion_status=status)
    monkeypatch.setattr(Zoo, '_fetch_manifests', classmethod(lambda cls, host: {'models':[spec], 'resident_ids':{spec.model_id}}))
    Zoo.refresh(force=True, async_ok=False)
    return spec

def pick(spec, refresh=True):
    return Zoo.select(FormatProfile(family='constructed', deck_size=60), spec.deck_counts, refresh=refresh)

@pytest.mark.parametrize('status', ['uncertified','rejected'])
def test_noncertified_not_selected(monkeypatch,status):
    assert pick(seeded(monkeypatch,status)) is None

def test_unavailable_endpoint_invalidates_selection(monkeypatch):
    spec=seeded(monkeypatch)
    monkeypatch.setattr(MageZeroClient,'get_active_endpoint',classmethod(lambda cls: None))
    assert pick(spec) is None

def test_failed_same_host_refresh_invalidates_selection(monkeypatch):
    spec=seeded(monkeypatch)
    def fail(cls,host): raise OSError('fixture failure')
    monkeypatch.setattr(Zoo,'_fetch_manifests',classmethod(fail))
    assert Zoo.refresh(force=True,async_ok=False)==[]
    assert pick(spec) is None

def test_warm_does_not_use_other_hosts_capability(monkeypatch):
    spec=seeded(monkeypatch,warm=True)
    monkeypatch.setattr(MageZeroClient,'get_active_endpoint',classmethod(lambda cls:'http://fixture-B'))
    called=threading.Event()
    monkeypatch.setattr(Zoo,'_post_warm',classmethod(lambda cls,*args,**kwargs:called.set()))
    Zoo.warm(spec.model_id)
    assert not called.wait(.15)

def test_one_opponent_is_not_complete_ten_deck_certification():
    d=_v2_manifest()
    d['promotion_status']='certified'
    d['certification']={
      'evaluated_at':'2026-09-06T00:00:00Z','criteria_version':'all10-v1',
      'checkpoint_hash':d['checkpoint_hash'],'deck_hash':d['deck']['deck_hash'],
      'aggregation':'mean_win_rate','threshold':.5,
      'panel':{'decks':['only-one'],'games':2,'wins':1,'losses':1,'draws':0,
               'arms':[{'opponent':'only-one','games':2,'win_rate':.5,'returncode':0}]}}
    with pytest.raises(ManifestError): ModelSpec.from_manifest(d)
