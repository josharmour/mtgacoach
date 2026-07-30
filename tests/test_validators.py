"""Unit tests for tools/training/validators.py — WP-1.2 contract validators.

Covers every validator function's pass and fail paths:
  - validate_action_schema_json
  - validate_keyword_contract
  - validate_length_cap
  - validate_action_legality
  - validate_all
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.training.validators import (  # noqa: E402
    ACTION_SCHEMA,
    DECISION_PROMPTS,
    MAX_RESPONSE_CHARS,
    validate_action_legality,
    validate_action_schema_json,
    validate_all,
    validate_keyword_contract,
    validate_length_cap,
)

# ============================================================================
# Re-export assertions
# ============================================================================


def test_imports_real_constants():
    """Validators imports the real ACTION_SCHEMA / DECISION_PROMPTS, not stubs."""
    assert isinstance(ACTION_SCHEMA, str) and len(ACTION_SCHEMA) > 50
    assert '"actions"' in ACTION_SCHEMA
    assert isinstance(DECISION_PROMPTS, dict) and "mulligan" in DECISION_PROMPTS


# ============================================================================
# validate_action_schema_json
# ============================================================================


class TestActionSchemaJson:
    def test_valid_simple_pick(self):
        ok, msg = validate_action_schema_json('{"actions": [{"pick": 1, "reasoning": "Play mountain"}]}')
        assert ok is True
        assert "Valid" in msg

    def test_valid_fenced_json(self):
        ok, msg = validate_action_schema_json('```json\n{"actions": [{"pick": 1}]}\n```')
        assert ok is True
        assert "Valid" in msg

    def test_valid_no_fence_label(self):
        ok, msg = validate_action_schema_json('```\n{"actions": [{"pick": 1}]}\n```')
        assert ok is True
        assert "Valid" in msg

    def test_valid_action_type(self):
        ok, msg = validate_action_schema_json(
            '{"actions": [{"action_type": "declare_attackers", "attacker_names": ["Grizzly Bears"]}]}'
        )
        assert ok is True

    def test_empty_response(self):
        ok, msg = validate_action_schema_json("")
        assert ok is False
        assert "Empty" in msg

    def test_whitespace_only(self):
        ok, msg = validate_action_schema_json("   \n  ")
        assert ok is False
        assert "Empty" in msg

    def test_not_json(self):
        ok, msg = validate_action_schema_json("KEEP")
        assert ok is False
        assert "parse error" in msg

    def test_not_a_dict(self):
        ok, msg = validate_action_schema_json('["list", "not", "dict"]')
        assert ok is False
        assert "not an object" in msg

    def test_missing_actions_key(self):
        ok, msg = validate_action_schema_json('{"not_actions": []}')
        assert ok is False
        assert "Missing" in msg

    def test_empty_actions_list(self):
        ok, msg = validate_action_schema_json('{"actions": []}')
        assert ok is False
        assert "empty" in msg.lower()

    def test_action_not_object(self):
        ok, msg = validate_action_schema_json('{"actions": [42]}')
        assert ok is False
        assert "not an object" in msg

    def test_missing_all_required_fields(self):
        ok, msg = validate_action_schema_json('{"actions": [{"foo": "bar"}]}')
        assert ok is False
        assert "missing" in msg.lower()

    def test_pick_not_int(self):
        ok, msg = validate_action_schema_json('{"actions": [{"pick": "one"}]}')
        assert ok is False
        assert "integer" in msg

    def test_trailing_comma_stripped(self):
        ok, msg = validate_action_schema_json('{"actions": [{"pick": 1,},]}')
        assert ok is True


# ============================================================================
# validate_keyword_contract
# ============================================================================


class TestKeywordContract:
    def test_mulligan_response_keep(self):
        ok, msg = validate_keyword_contract("Mulligan: KEEP or MULLIGAN", "KEEP")
        assert ok is True

    def test_mulligan_response_mulligan(self):
        ok, msg = validate_keyword_contract("Mulligan: KEEP or MULLIGAN", "MULLIGAN")
        assert ok is True

    def test_mulligan_response_json_pick(self):
        ok, msg = validate_keyword_contract("Mulligan: KEEP or MULLIGAN", '{"actions": [{"pick": 1}]}')
        assert ok is True

    def test_mulligan_invalid_word(self):
        ok, msg = validate_keyword_contract("Mulligan: KEEP or MULLIGAN", "maybe")
        assert ok is False
        assert "Mulligan" in msg or "must be" in msg

    def test_mulligan_json_malformed(self):
        ok, msg = validate_keyword_contract("Mulligan: KEEP or MULLIGAN", "{bad json}")
        assert ok is False
        assert "must be" in msg

    def test_not_mulligan_passes_through(self):
        ok, msg = validate_keyword_contract("Legal: [1] Cast Lightning Bolt", "anything")
        assert ok is True

    def test_keep_hand_variant(self):
        ok, msg = validate_keyword_contract("Keep Hand or Mulligan", "Keep")
        assert ok is True


# ============================================================================
# validate_length_cap
# ============================================================================


class TestLengthCap:
    def test_short_response_passes(self):
        ok, msg = validate_length_cap("short")
        assert ok is True
        assert "within" in msg.lower()

    def test_empty_passes(self):
        ok, msg = validate_length_cap("")
        assert ok is True

    def test_exceeds_max_chars(self):
        long = "x" * (MAX_RESPONSE_CHARS + 1)
        ok, msg = validate_length_cap(long)
        assert ok is False
        assert "exceeds" in msg.lower()
        assert str(MAX_RESPONSE_CHARS) in msg

    def test_reasoning_too_long_in_json(self):
        long_reason = "x" * 300  # > MAX_REASONING_CHARS (200)
        resp = json.dumps({"actions": [{"pick": 1, "reasoning": long_reason}]})
        ok, msg = validate_length_cap(resp)
        assert ok is False
        assert "reasoning" in msg.lower()

    def test_voice_advice_too_long(self):
        long_advice = "x" * 500  # > MAX_VOICE_ADVICE_CHARS (400)
        resp = json.dumps({"actions": [{"pick": 1}], "voice_advice": long_advice})
        ok, msg = validate_length_cap(resp)
        assert ok is False
        assert "voice_advice" in msg.lower()

    def test_fenced_json_reasoning_check(self):
        long_reason = "x" * 300
        resp = f"```json\n{{'actions': [{{'pick': 1, 'reasoning': '{long_reason}'}}]}}\n```"
        # Use actual JSON; fenced content is unwrapped
        resp = '```json\n{"actions": [{"pick": 1, "reasoning": "' + long_reason + '"}]}\n```'
        ok, msg = validate_length_cap(resp)
        assert ok is False
        assert "reasoning" in msg


# ============================================================================
# validate_action_legality
# ============================================================================


class TestActionLegality:
    def test_valid_pick_bracket(self):
        prompt = "Legal:\n [1] Play Land: Mountain\n [2] Cast Lightning Bolt\n [3] Pass"
        resp = '{"actions": [{"pick": 2}]}'
        ok, msg = validate_action_legality(prompt, resp)
        assert ok is True

    def test_invalid_pick(self):
        prompt = "Legal:\n [1] Play Land: Mountain\n [2] Cast Lightning Bolt\n [3] Pass"
        resp = '{"actions": [{"pick": 99}]}'
        ok, msg = validate_action_legality(prompt, resp)
        assert ok is False
        assert "does not exist" in msg

    def test_valid_pick_numbered_menu(self):
        prompt = "Legal:\n  1. Play Land: Mountain\n  2. Cast Lightning Bolt\n  3. Pass"
        resp = '{"actions": [{"pick": 2}]}'
        ok, msg = validate_action_legality(prompt, resp)
        assert ok is True

    def test_valid_card_name(self):
        prompt = "Legal:\n [1] Cast Lightning Bolt\n [2] Pass"
        resp = '{"actions": [{"card_name": "Lightning Bolt"}]}'
        ok, msg = validate_action_legality(prompt, resp)
        assert ok is True

    def test_invalid_card_name(self):
        prompt = "Legal:\n [1] Cast Lightning Bolt\n [2] Pass"
        # Must include action_type (or pick/reasoning) for schema validation to pass
        resp = '{"actions": [{"action_type": "cast_spell", "card_name": "Counterspell"}]}'
        ok, msg = validate_action_legality(prompt, resp)
        assert ok is False
        assert "not found" in msg

    def test_non_json_skips_legality(self):
        ok, msg = validate_action_legality("Legal:\n [1] KEEP", "KEEP")
        assert ok is True
        assert "Skipped" in msg

    def test_no_legal_section_passes(self):
        prompt = "Just a regular prompt without a legal section"
        resp = '{"actions": [{"pick": 1}]}'
        ok, msg = validate_action_legality(prompt, resp)
        assert ok is True


# ============================================================================
# validate_all (integration)
# ============================================================================


class TestValidateAll:
    def test_clean_triplet_passes(self):
        user = "Legal:\n [1] Cast Lightning Bolt"
        resp = '{"actions": [{"pick": 1}]}'
        ok, reasons = validate_all("", user, resp)
        assert ok is True
        assert len(reasons) == 0

    def test_bad_json_rejected(self):
        ok, reasons = validate_all("", "", "NOT JSON")
        assert ok is False
        assert len(reasons) >= 1

    def test_overlong_rejected(self):
        long_reason = "x" * 300
        resp = json.dumps({"actions": [{"pick": 1, "reasoning": long_reason}]})
        ok, reasons = validate_all("", "Legal:\n [1] Pass", resp)
        assert ok is False
        assert any("reasoning" in r.lower() for r in reasons)

    def test_invalid_pick_rejected(self):
        user = "Legal:\n [1] KEEP [2] MULLIGAN"
        resp = json.dumps({"actions": [{"pick": 99}]})
        ok, reasons = validate_all("", user, resp)
        assert ok is False
        assert any("does not exist" in r.lower() for r in reasons)

    def test_mulligan_invalid_rejected(self):
        ok, reasons = validate_all("", "Mulligan decision", "maybe")
        assert ok is False


# ============================================================================
# Convenience: run with pytest directly
# ============================================================================
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
