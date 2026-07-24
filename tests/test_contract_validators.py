"""Unit tests for tools/training/validators.py."""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.training.validators import (
    validate_action_legality,
    validate_action_schema_json,
    validate_all,
    validate_keyword_contract,
)



def test_valid_json_schema():
    valid_json = '{"actions": [{"pick": 1, "reasoning": "Play mountain"}]}'
    ok, msg = validate_action_schema_json(valid_json)
    assert ok is True
    assert "Valid" in msg


def test_invalid_json_schema():
    invalid_json = '{"actions": [{"pick": "one"}]}'
    ok, msg = validate_action_schema_json(invalid_json)
    assert ok is False
    assert "integer" in msg


def test_action_legality_valid_pick():
    prompt_user = "Legal:\n [1] Play Land: Mountain\n [2] Cast Lightning Bolt\n [3] Pass"
    response = '{"actions": [{"pick": 2}]}'
    ok, msg = validate_action_legality(prompt_user, response)
    assert ok is True


def test_action_legality_invalid_pick():
    prompt_user = "Legal:\n [1] Play Land: Mountain\n [2] Cast Lightning Bolt\n [3] Pass"
    response = '{"actions": [{"pick": 99}]}'
    ok, msg = validate_action_legality(prompt_user, response)
    assert ok is False
    assert "does not exist" in msg


def test_validate_all_clean():
    user_p = "Legal:\n [1] Cast Lightning Bolt"
    resp = '{"actions": [{"pick": 1}]}'
    ok, reasons = validate_all("", user_p, resp)
    assert ok is True
    assert len(reasons) == 0
