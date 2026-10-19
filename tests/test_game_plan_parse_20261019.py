"""#114: game-plan answers that look schema-correct were rejected as "no usable plan".

Evidence (2026-10-19, coach log + the gateway's Langfuse traces): both answers
finished normally (finish_reason "stop", 2615 and 3701 output tokens), so
nothing was cut off. The parser only read the first fenced block and gave up
on any JSON slip:

* 13:00:36: one valid-looking object with a bare ``""`` member in the first
  turn step (``"hold": "...",\\n ""\\n }``).
* 13:01:29: a fenced draft with a fourth ``{"turn": null, "_note": ""}`` step
  that stops at ``],``, then "Wait—I need to output valid JSON", a second
  unfinished draft, and finally the full plan as one unfenced compact object.

Across all 695 plan answers of 2026-10-18/19, 53 were unusable: 25 now parse
(self-corrected drafts, bare members, half-quoted or valueless keys,
escaped-quote objects) and the rest are cut-off streams or garbage.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from arenamcp import game_plan
from arenamcp.game_plan import GamePlan, GamePlanManager, save_unusable_answer

FIXTURES = Path(__file__).parent / "fixtures"


def _answer(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _parse(text: str) -> GamePlan | None:
    return GamePlanManager._parse(text, 14)


# --- the two live answers ------------------------------------------------------------------------


def test_the_1300_answer_with_a_bare_string_member_is_used():
    text = _answer("game_plan_answer_20261019_130036.txt")
    assert '"hold": "Hand empty; if drawn: burn spell goes face or kills a lifelink flyer; ' in text
    plan = _parse(text)
    assert plan is not None and not plan.is_empty()
    assert plan.role == "aggressor"
    assert [step["turn"] for step in plan.turn_plan] == ["T", "T+1", "T+2"]
    assert plan.turn_plan[0]["attack"].startswith("Slickshot Show-Off at the player")
    assert "" not in plan.turn_plan[0]
    assert plan.win_conditions and plan.path


def test_the_1301_answer_uses_the_plan_written_after_the_broken_drafts():
    text = _answer("game_plan_answer_20261019_130129.txt")
    assert "Wait—I need to output valid JSON" in text
    plan = _parse(text)
    assert plan is not None and not plan.is_empty()
    assert plan.role == "race"
    assert plan.path == "Swing both while Hortimancer taps out; drawn removal clears it; burn finishes from <=8."
    assert plan.win_conditions == [
        "Both attackers connect twice plus drawn burn reach",
        "Kill Hortimancer, grind out with topdecked burn",
    ]
    assert [step["turn"] for step in plan.turn_plan] == ["T", "T+1", "T+2"]
    assert all("_note" not in step for step in plan.turn_plan)
    assert plan.lessons[1] == "Resolving Cindermaw locked their lifegain at the key moment"


# --- the other slips seen in the 2026-10-18/19 answers ------------------------------------------

_TURNS = '[{"turn": "T", "land": "", "cast": [], "attack": "Slickshot Show-Off", "hold": ""}]'


def test_a_key_missing_its_opening_quote_is_repaired():
    text = (
        '```json\n{\n  "role": "defender",\n  "turns": ' + _TURNS + ',\n  "path": "block, then swing",\n'
        '  "switch_if": [\n    "they lose the flyer -> attack"\n],\nactive_mechanisms": [\n    "engine_1 live"\n],\n'
        'resource_priorities": ["keep Shock"]\n}\n```'
    )
    plan = _parse(text)
    assert plan is not None and plan.path == "block, then swing"
    assert plan.active_mechanisms == ["engine_1 live"] and plan.resource_priorities == ["keep Shock"]


def test_a_key_name_without_a_value_is_dropped():
    text = (
        '```json\n{\n  "role": "aggressor",\n  "turns": ' + _TURNS + ',\n  "path": "fly over",\n'
        '  "active_mechanisms": ["engine_1"],\n  resource_priorities,\n  assumptions,\n  lessons\n}\n```'
    )
    plan = _parse(text)
    assert plan is not None and plan.path == "fly over" and plan.active_mechanisms == ["engine_1"]
    assert plan.resource_priorities == [] and plan.lessons == []


def test_a_bare_string_member_mid_object_is_dropped():
    text = '{"role": "race", "turns": [{"turn": "T", "hold": "x", "", "attack": "both"}], "path": "race"}'
    plan = _parse(text)
    assert plan is not None and plan.turn_plan == [{"turn": "T", "hold": "x", "attack": "both"}]


def test_an_object_written_with_escaped_quotes_is_used():
    text = (
        "```json\n{\n  \"role\": \"race\",\n  resource_priorities,\n  ...\n}\n```\n\n"
        "Wait—I need clean final output:\n\n```json\n"
        '{\n  \\"role\\": \\"race\\",\n  \\"turns\\": [{\\"turn\\": \\"T\\", \\"attack\\": \\"all\\"}],\n'
        '  \\"path\\": \\"swing every turn\\"\n}\n```'
    )
    plan = _parse(text)
    assert plan is not None and plan.path == "swing every turn" and plan.turn_plan[0]["attack"] == "all"


def test_a_role_only_draft_does_not_shadow_the_full_plan_after_it():
    text = (
        'Final answer below as clean JSON:\n\n{\n  "role": "defender"\n}\n\n'
        'Final structured answer:\n\n{"role": "defender", "turns": ' + _TURNS + ', "path": "block"}'
    )
    plan = _parse(text)
    assert plan is not None and plan.path == "block" and plan.role == "defender"


def test_a_repair_never_picks_a_turn_step_or_invents_a_value():
    # The #58 guard holds: a broken outer object is not replaced by its turn step.
    assert _parse('{"turns": [{"turn": "T", "land": "Mountain"}], "path": oops}') is None
    # An answer cut off mid-object (a cancelled stream) stays unusable.
    cut = _answer("game_plan_answer_20261019_130036.txt")[:1500]
    assert _parse(cut) is None
    # JavaScript inside the object is not "repaired".
    garbage = '{"role": "aggressor", "path": "x", "lessons": ["a", ["b", ""].slice(0,1)[0]]}'
    assert _parse(garbage) is None


# --- the full answer goes to ~/.arenamcp/debug ---------------------------------------------------


class _Backend:
    def complete(self, *args, **kwargs):  # pragma: no cover - never called here
        raise AssertionError("no model call expected")


def test_an_unusable_answer_is_logged_and_saved_in_full(caplog):
    mgr = GamePlanManager(_Backend())
    answer = "```json\n{\n  \"role\": \"race\",\n" + "  \"note\": \"" + "x" * 900 + "\" oops\n}\n```"
    with caplog.at_level(logging.INFO, logger="arenamcp.game_plan"):
        assert mgr._usable_plan(answer, 14, cancel=None, effort="high") is None
    warning = next(r.getMessage() for r in caplog.records if "no usable plan" in r.getMessage())
    assert f"high effort, {len(answer)} chars" in warning and "x" * 300 not in warning
    saved = list(game_plan.UNUSABLE_PLAN_DIR.glob("game_plan_unusable_*.txt"))
    assert len(saved) == 1
    content = saved[0].read_text(encoding="utf-8")
    assert content.startswith(f"# game-plan answer with no usable plan: turn 14, high effort, {len(answer)} chars")
    assert content.endswith(answer)
    assert any(str(saved[0]) in r.getMessage() for r in caplog.records)
    assert mgr._reform_failure == "no usable plan from the model"


def test_only_the_newest_twenty_answers_are_kept():
    folder = game_plan.UNUSABLE_PLAN_DIR
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(25):
        (folder / f"game_plan_unusable_20261019_1200{index:02d}_000.txt").write_text("old", encoding="utf-8")
    (folder / "other_debug_file.txt").write_text("keep", encoding="utf-8")
    newest = save_unusable_answer("new answer", turn_num=3, effort="low")
    kept = sorted(path.name for path in folder.glob("game_plan_unusable_*.txt"))
    assert len(kept) == game_plan.UNUSABLE_PLAN_KEEP == 20
    assert newest is not None and newest.name in kept
    assert "game_plan_unusable_20261019_120000_000.txt" not in kept
    assert (folder / "other_debug_file.txt").exists()


def test_a_failed_save_never_raises(monkeypatch, tmp_path):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(game_plan, "UNUSABLE_PLAN_DIR", blocker / "debug")
    assert save_unusable_answer("answer", turn_num=1, effort="high") is None


@pytest.mark.parametrize("fixture", ["game_plan_answer_20261019_130036.txt", "game_plan_answer_20261019_130129.txt"])
def test_the_live_answers_now_form_a_plan_through_the_reform_path(fixture):
    mgr = GamePlanManager(_Backend())
    plan = mgr._usable_plan(_answer(fixture), 14, cancel=None, effort="high")
    assert plan is not None and not plan.is_empty()
    assert not list(game_plan.UNUSABLE_PLAN_DIR.glob("game_plan_unusable_*.txt"))
