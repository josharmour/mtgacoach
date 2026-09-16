from dataclasses import replace
from arenamcp.model_zoo import ModelZooClient, ModelSpec
from arenamcp.magezero_client import MageZeroClient, _validate_result
from arenamcp.mcts_evaluator import MCTSTreePayload, MCTSBranch
from test_model_zoo import _v2_manifest, _uwtempo_profile, _full_hero_deck


def test_experimental_is_explicit_and_never_allows_rejected(monkeypatch):
    ModelZooClient.reset()
    endpoint = 'http://fixture'
    manifest = _v2_manifest(status='uncertified')
    spec = ModelSpec.from_manifest(manifest, is_resident=True)
    monkeypatch.setattr(MageZeroClient, 'get_active_endpoint', lambda: endpoint)
    monkeypatch.setattr(ModelZooClient, '_active_host', endpoint)
    monkeypatch.setattr(ModelZooClient, '_models_by_host', {endpoint: [spec]})
    monkeypatch.delenv('MAGEZERO_EXPERIMENTAL', raising=False)
    assert ModelZooClient.select(_uwtempo_profile(), _full_hero_deck(), refresh=False) is None
    monkeypatch.setenv('MAGEZERO_EXPERIMENTAL', '1')
    selected = ModelZooClient.select(_uwtempo_profile(), _full_hero_deck(), refresh=False)
    assert selected and 'experimental, uncalibrated' in selected.label
    ModelZooClient._models_by_host[endpoint] = [replace(spec, promotion_status='rejected')]
    assert ModelZooClient.select(_uwtempo_profile(), _full_hero_deck(), refresh=False) is None
    ModelZooClient.reset()


def test_requested_checkpoint_requires_identity_echo():
    row = {'value': .1, 'policy_player': [0.] * 128, 'policy_opponent': [0.] * 128}
    result, error = _validate_result([row], 1, 128, expected_checkpoint_hash='a'*64)
    assert not result and 'served-checkpoint-missing' in error


def test_experimental_prompt_does_not_claim_calibrated_win_probability():
    payload = MCTSTreePayload(eval_source='MageZero UWTempo v2 — experimental, uncalibrated',
                             branches=[MCTSBranch(action='Pass', action_type='pass',
                                                  prior_probability=.7, score_provenance='prior_only')])
    text = payload.format_for_llm_prompt()
    assert 'Root Model Score (uncalibrated)' in text
    assert 'not calibrated win probabilities' in text
    assert 'Policy weight: 70.0%; outcome not evaluated' in text
    assert 'Candidate order remains the tactical heuristic ranking' in text
