from pathlib import Path

from arenamcp import game_plan
from arenamcp.game_plan import GamePlanManager, _plan_object, save_unusable_answer


def test_parse_game_plan_salvages_truncated_json_after_complete_turns():
    raw = """{
  "role": "aggressor",
  "role_reason": "we have two flyers and burn in hand",
  "turns": [
    {"turn": "T", "land": "Mountain", "cast": ["Shock"], "attack": "all", "hold": ""},
    {"turn": "T+1", "land": "", "cast": [], "attack": "all", "hold": ""}
  ],
  "win_conditions": ["Burn opponent out after flyer chip damage"],
  "path": "Attack for 4 in the air on T5 and finish with Shock",
  "assumptions": ["opponent has no lifegain"""
    obj = _plan_object(raw)
    assert obj is not None
    data, _ = obj
    assert data["role"] == "aggressor"
    assert len(data["turns"]) == 2
    plan = GamePlanManager._parse(raw, turn_num=5)
    assert not plan.is_empty()
    assert plan.role == "aggressor"
    assert plan.win_conditions == ["Burn opponent out after flyer chip damage"]


def test_parse_game_plan_repairs_multiline_and_digit_leading_unquoted_list_item():
    raw = """{
  "role": "defender",
  "role_reason": "behind on board against two attackers",
  "turns": [
    {"turn": "T", "land": "Plains", "cast": ["Day of Judgment"], "attack": "none", "hold": ""}
  ],
  "win_conditions": ["Stabilize with wrath and win with planeswalker"],
  "path": "Cast Day of Judgment on T4 then deploy Elspeth",
  "assumptions": [
    "opponent has no counterspell",
    2 turns of breathing room after wiping the board
    to deploy Elspeth
  ]
}"""
    plan = GamePlanManager._parse(raw, turn_num=4)
    assert not plan.is_empty()
    assert plan.role == "defender"
    assert any("2 turns of breathing room" in a for a in plan.assumptions)


def test_save_unusable_answer_records_match_id(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(game_plan, "UNUSABLE_PLAN_DIR", tmp_path)
    out = save_unusable_answer("not json at all", turn_num=8, effort="high", match_id="match-abc-123")
    assert out is not None
    content = out.read_text(encoding="utf-8")
    assert "match match-abc-123, turn 8, high effort" in content
