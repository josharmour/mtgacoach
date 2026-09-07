from copy import deepcopy
from pathlib import Path
import pytest
from arenamcp.magezero_parity import load_fixture,validate_record,compare_case,assert_parity_evidence,FixtureValidationError

def record():
    return deepcopy(load_fixture(Path('tests/fixtures/xmage_parity/synthetic_harness.jsonl'))[0])

def test_emitter_string_alone_does_not_establish_provenance():
    r=record();r['provenance']={'emitter_kind':'xmage_jvm_hdf5','encoder_version':'unknown','action_schema_version':'unknown'}
    assert validate_record(r)

def test_missing_value_perspective_columns_fail_comparison():
    r=record();actual=deepcopy(r['emitted_rows']);actual[0]['row']=actual[0]['row'][:128]
    try: report=compare_case(r,actual)
    except FixtureValidationError: return
    assert not report.all_equal

def test_wrong_row_identity_fails_comparison():
    r=record();actual=deepcopy(r['emitted_rows']);actual[0]['row_index']+=1000
    try: report=compare_case(r,actual)
    except FixtureValidationError: return
    assert not report.all_equal

def test_wrong_schema_version_rejected():
    r=record();r['schema_version']='unsupported-v999'
    assert validate_record(r)
