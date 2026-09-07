from copy import deepcopy
import pytest
from arenamcp.model_zoo import ModelSpec, ManifestError
from test_model_contract import _UWTEMPO_MANIFEST_V2

@pytest.mark.parametrize('case', ['nonhex_hash','nan_threshold','negative_counts','wrong_size','empty_identity','missing_versions','empty_certified_evidence'])
def test_invalid_manifest_rejected(case):
    d=deepcopy(_UWTEMPO_MANIFEST_V2)
    if case=='nonhex_hash': d['checkpoint_hash']='z'*64
    elif case=='nan_threshold': d['gate']['deck_similarity_threshold']=float('nan')
    elif case=='negative_counts': d['deck']['deck_counts']['Island']=-7
    elif case=='wrong_size': d['deck']['size']=999
    elif case=='empty_identity': d['model_id']=''
    elif case=='missing_versions':
        d.pop('encoder_version');d.pop('action_schema_version');d.pop('value_target')
    elif case=='empty_certified_evidence':
        d['promotion_status']='certified'
        d['certification']={'evaluated_at':None,'criteria_version':None,'panel':{'games':0,'wins':0},'aggregation':None,'threshold':None}
    with pytest.raises(ManifestError): ModelSpec.from_manifest(d)
