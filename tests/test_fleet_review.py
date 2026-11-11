"""fleet_review: W1 mining of shared match bundles for recurring misplays.

Fixtures are built from REAL logged cases in the shared corpus
(/home/joshu/fable-nov-evidence/arenamcp/match_bundles) and real coach-log
lines from /home/joshu/fable-nov-evidence/arenamcp/standalone.log; the
originating match id or log location is named in each test/docstring.
Rule-boundary negatives are labelled as such.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys

import pytest

_TOOLS_DIR = pathlib.Path(__file__).resolve().parent.parent / "tools"
_SPEC = importlib.util.spec_from_file_location("fleet_review", _TOOLS_DIR / "fleet_review.py")
fr = importlib.util.module_from_spec(_SPEC)
sys.modules["fleet_review"] = fr
_SPEC.loader.exec_module(fr)


# --------------------------------------------------------------------------
# Fixture builders. Shapes mirror the real bundles.
# --------------------------------------------------------------------------


def _bundle(
    match_id,
    *,
    format_name="Brawl Ladder",
    turn=8,
    lives=(25, 22),
    coach_lines=(),
    deck_strategy="",
    command_zone=(),
    opponent_seat_id=1,
    decisions=(),
):
    return {
        "match_id": match_id,
        "created_utc": "2026-11-10T00:00:00Z",
        "final_state": {
            "format_name": format_name,
            "opponent_seat_id": opponent_seat_id,
            "turn_info": {"turn_number": turn},
            "players": [
                {"seat_id": 0, "life_total": lives[0]},
                {"seat_id": 1, "life_total": lives[1]},
            ],
            "zones": {"command": list(command_zone)},
        },
        "coach_log": {"lines": list(coach_lines)},
        "packet": {"deck_strategy": deck_strategy, "decisions": list(decisions)},
    }


# Real coach-log lines, verbatim from the corpus ---------------------------

# Match 00651f77-a4b5-41b7-bfe6-089c5cbcf090, 2026-11-10 08:29:23
HOP_LIMIT_LINE = (
    "2026-11-10 08:29:23.566 | WARNING  | arenamcp.autopilot | "
    "post-cast priority: hop limit (1) reached; leaving ActionsAvailable "
    "to the watchdog"
)

# Match 0e07798d-d9ee-4564-8583-6e25e8796a7e, 2026-11-10 06:33:12
SET_ASIDE_LINE = (
    "2026-11-10 06:33:12.156 | WARNING  | arenamcp.action_planner | "
    "typed-decision fallback (the model's pick was set aside): the "
    "board-math line casts Laelia, the Blade Reforged -> ['idx:0']"
)

# Match 00651f77-a4b5-41b7-bfe6-089c5cbcf090, 2026-11-10 08:28:22
MULLIGAN_KEEP_LINE = (
    "2026-11-10 08:28:22.578 | WARNING  | arenamcp.action_planner | "
    "Mulligan guard: KEEP (mulligans taken: 0): 2 lands on the draw with "
    "cheap plays (Hired Claw, Slickshot Show-Off, Lightning Strike, "
    "Lightning Strike, Shock) — deterministic policy, LLM not consulted"
)

# Match 034f2385-6e42-4b6d-b2c7-c6132cd85c57, 2026-11-10 06:20:28
MULLIGAN_MULL_LINE = (
    "2026-11-10 06:20:28.710 | WARNING  | arenamcp.action_planner | "
    "Mulligan guard: MULLIGAN (mulligans taken: 0): 7 lands in 7 cards; "
    "a 6-card hand is better — deterministic policy, LLM not consulted"
)

# Match 05c4910b-7d64-4bbd-bd4a-87377843ac06, 2026-11-10 04:05:08
CONCEDE_EXECUTED_LINE = (
    "2026-11-10 04:05:08.917 | INFO     | arenamcp.concede | "
    "[CONCEDE] verified before conceding: confidence 0.97, Concede: "
    "their 4/4 flying Giada beatdown closes the race"
)

# Match 06d2f9bf-9737-4143-bc8c-5c4e5da92510, 2026-11-10 07:44:31 — the only
# ERROR-level line shape in the corpus coach logs.
BACKEND_SENTINEL_ERROR_LINE = (
    "2026-11-10 07:44:31.936 | ERROR    | arenamcp.action_planner | "
    "Backend returned error sentinel; no plan: request changed during planning"
)

# Real timeout lines from standalone.log (the coach's own log; e.g. lines
# around 2026-08-03 22:36–22:37 in the shared evidence copy).
PROXY_TIMEOUT_ERROR_LINE = (
    "2026-08-03 22:36:50 | ERROR    | arenamcp.backends.proxy | API error: Request timed out."
)
LLM_CALL_TIMED_OUT_LINE = (
    "2026-08-03 22:37:07 | WARNING  | arenamcp.coach | "
    "LLM API call timed out after 17.0s (model may be too slow for "
    "real-time coaching)"
)

# Routine stale-decision housekeeping from match 05c4910b, 2026-11-10
# 04:04:10 — carries "timeout=" WITHOUT a space and must NOT count as a
# game-ending timeout.
STALE_DECISION_TIMEOUT_LINE = (
    "2026-11-10 04:04:10.400 | INFO     | arenamcp.gamestate_decisions | "
    "Auto-clearing stale decision 'Optional Action' (no source_id, age=15s, "
    "timeout=15s)"
)

# Real metric/plan lines ----------------------------------------------------

# Match 06d2f9bf, 2026-11-10 07:44:31 (cancelled call; metrics line verbatim)
REQUEST_METRICS_LINE = (
    "2026-11-10 07:44:31.936 | INFO     | arenamcp.backends.proxy | "
    "[PROXY] Request metrics: {'model': 'current', 'request_started_unix_s': "
    "1794325471.302902, 'response_id': None, 'client_setup_ms': 0.0, "
    "'lane_wait_ms': 0.0, 'stream_chunks': 0, 'first_reasoning_ms': None, "
    "'stream_content_chars': 0, 'stream_reasoning_chars': 0, "
    "'served_model': None, 'status': 'cancelled', 'streamed': False, "
    "'attempts': 1, 'input_chars': 21087, 'input_tokens': None, "
    "'output_tokens': None, 'cached_input_tokens': None, "
    "'reasoning_tokens': None, 'ttft_ms': None, 'first_content_ms': None, "
    "'total_ms': 633.8, 'finish_reason': None, 'call_class': "
    "'decision.plan', 'priority': None, 'reasoning_effort': 'none', "
    "'thinking_token_budget': None, 'max_tokens': 4096, 'budget_s': 15.0}"
)

# Match 00651f77, 2026-11-10 08:28:25
PLAN_FORMED_LINE = (
    "2026-11-10 08:28:25.466 | INFO     | arenamcp.game_plan | "
    "Game plan reform (first plan): turn 2, plan for turn 2"
)

# Match c41c9c6a, 2026-11-10 08:49:42 — the real "path corrected" form the
# M1-definition regex counts.
PATH_CORRECTED_LINE = (
    "2026-11-10 08:49:42.091 | INFO     | arenamcp.game_plan | "
    "GamePlan validation (turn 12): path: plan claim corrected: Lyra, "
    "Archangel of Dawn can block Fleeting Effigy"
)

# Match 00651f77, 2026-11-10 08:29:56
BIG_PROMPT_LINE = (
    "2026-11-10 08:29:56.828 | INFO     | arenamcp.action_planner | "
    "typed-decision: big prompt (21002 chars): first token within 8.1 s, "
    "budget 12.1 s"
)


# Real command-zone objects, verbatim from the corpus -----------------------

# Match 0e5bb04f — opponent commander grp_id 97871 (resolves via
# arenamcp.card_db to Eddie Brock).
EDDIE_BROCK_CARD = {
    "instance_id": 239,
    "grp_id": 97871,
    "zone_id": 26,
    "owner_seat_id": 2,
    "controller_seat_id": 2,
    "visibility": "Visibility_Public",
    "card_types": ["CardType_Creature"],
    "subtypes": ["Human", "Hero", "Villain"],
    "power": 3,
    "toughness": 3,
    "object_kind": "CARD",
    "super_types": ["SuperType_Legendary"],
}

# Match 346239cd-cadf-4bb9-869b-58ac39e89402 — grp_id 78770 sits in the
# opponent command zone but is a Dungeon game object (card_types
# [CardType_Dungeon], no super types): NOT a commander.
DUNGEON_CARD = {
    "instance_id": 469,
    "grp_id": 78770,
    "zone_id": 26,
    "owner_seat_id": 2,
    "controller_seat_id": 2,
    "visibility": "Visibility_Public",
    "card_types": ["CardType_Dungeon"],
    "subtypes": [],
    "power": None,
    "toughness": None,
    "object_kind": "CARD",
    "super_types": [],
}

# Match 44bc39ca — typeless emblem-like object (grp_id 2, object_kind EMBLEM).
EMBLEM_OBJECT = {
    "instance_id": 830,
    "grp_id": 2,
    "zone_id": 26,
    "owner_seat_id": 1,
    "controller_seat_id": 1,
    "visibility": "Visibility_Public",
    "card_types": [],
    "subtypes": [],
    "object_kind": "EMBLEM",
    "super_types": [],
}

# Match 44bc39ca — a legendary Planeswalker commander (grp_id 69656).
BOLAS_CARD = {
    "instance_id": 848,
    "grp_id": 69656,
    "zone_id": 26,
    "owner_seat_id": 1,
    "controller_seat_id": 1,
    "visibility": "Visibility_Public",
    "card_types": ["CardType_Planeswalker"],
    "subtypes": ["Bolas"],
    "object_kind": "CARD",
    "super_types": ["SuperType_Legendary"],
}


# Real packet decision entry (verbose option meta trimmed), match
# 0e07798d-d9ee-4564-8583-6e25e8796a7e — an ActionsAvailable decision whose
# model pick was set aside and the board-math fallback answered idx:0.
BOARD_MATH_FALLBACK_DECISION = {
    "pending_decision": {
        "request_id": [412, 559],
        "request_type": "ActionsAvailable",
        "options": [
            {"option_id": "idx:0", "label": "Cast Laelia, the Blade Reforged"},
            {"option_id": "pass", "label": "Pass"},
        ],
        "min_select": 1,
        "max_select": 1,
        "can_pass": True,
    },
    "chosen_options": ["idx:0"],
    "outcome": "ADVANCED",
    "trace": {
        "fallback": "board_math",
        "validated_ids": ["idx:0"],
        "fallback_reason": ("the board-math line casts Laelia, the Blade Reforged"),
    },
}


# --------------------------------------------------------------------------
# Flags and watchdog hand-offs
# --------------------------------------------------------------------------


def test_watchdog_hop_limit_counts_by_decision_type():
    """Real case: match 00651f77 hop-limit hand-off of ActionsAvailable."""
    bundle = _bundle("00651f77", coach_lines=[HOP_LIMIT_LINE])
    rv = fr.review_bundle(bundle)
    assert rv["flags"]["watchdog_hop_limit"] == 1
    assert rv["watchdog_types"] == {"ActionsAvailable": 1}
    assert rv["flag_examples"]["watchdog_hop_limit"] == ["00651f77"]


def test_watchdog_hand_off_rows_carry_example_match_ids():
    """Real cases: hop-limit hand-offs of different decision types from
    matches 00651f77 (ActionsAvailable) and 06fbd224 (SelectTargets) each
    keep their own example match id in the aggregate."""
    agg = fr.aggregate(
        [
            fr.review_bundle(_bundle("00651f77", coach_lines=[HOP_LIMIT_LINE])),
            fr.review_bundle(
                _bundle(
                    "06fbd224",
                    coach_lines=[HOP_LIMIT_LINE.replace("leaving ActionsAvailable", "leaving SelectTargets")],
                )
            ),
        ]
    )
    assert agg["watchdog_types"] == {"ActionsAvailable": 1, "SelectTargets": 1}
    assert agg["watchdog_examples"]["ActionsAvailable"] == ["00651f77"]
    assert agg["watchdog_examples"]["SelectTargets"] == ["06fbd224"]


def test_model_pick_set_aside_flagged():
    """Real case: match 0e07798d typed-decision fallback (pick set aside)."""
    bundle = _bundle("0e07798d", coach_lines=[SET_ASIDE_LINE])
    rv = fr.review_bundle(bundle)
    assert rv["flags"]["model_pick_set_aside"] == 1


def test_fallbacks_grouped_by_decision_type_from_packet():
    """Real case: match 0e07798d's ActionsAvailable decision answered by the
    board_math fallback (packet trace.fallback), grouped under its
    request_type with the match id as example."""
    bundle = _bundle("0e07798d", decisions=[BOARD_MATH_FALLBACK_DECISION])
    rv = fr.review_bundle(bundle)
    assert rv["fallbacks"] == {("ActionsAvailable", "board_math"): 1}

    agg = fr.aggregate([rv])
    key = ("ActionsAvailable", "board_math")
    assert agg["fallbacks_by_decision_type"][key] == 1
    assert agg["fallback_examples"][key] == ["0e07798d"]


def test_mulligan_guard_subcounts_keep_vs_mulligan():
    """Real cases: KEEP in match 00651f77, MULLIGAN in match 034f2385."""
    keep = fr.review_bundle(_bundle("00651f77", coach_lines=[MULLIGAN_KEEP_LINE]))
    mull = fr.review_bundle(_bundle("034f2385", coach_lines=[MULLIGAN_MULL_LINE]))
    assert keep["flags"]["mulligan_guard"] == 1
    assert mull["flags"]["mulligan_guard"] == 1
    agg = fr.aggregate([keep, mull])
    # Neither bundle carries a hop-limit hand-off, so no watchdog subtype.
    assert agg["watchdog_types"] == {}
    assert agg["flags"]["mulligan_guard"] == 2


# --------------------------------------------------------------------------
# Opposing commander identification (generic card facts)
# --------------------------------------------------------------------------


def test_opponent_commander_resolved_from_command_zone():
    """Real case: match 0e5bb04f — opponent commander grp_id 97871
    (legendary Creature; resolves via arenamcp.card_db to Eddie Brock)
    sitting in their end-of-game command zone."""
    bundle = _bundle("0e5bb04f", command_zone=[EDDIE_BROCK_CARD], opponent_seat_id=2)
    rv = fr.review_bundle(bundle)
    assert rv["opponent_commanders"] == ["Eddie Brock"]


def test_opponent_commander_planeswalker_real_case_44bc39ca():
    """Real case: match 44bc39ca — grp_id 69656, a legendary Planeswalker
    (resolves via arenamcp.card_db to Nicol Bolas, Dragon-God) in the
    opponent command zone, is a commander."""
    bundle = _bundle("44bc39ca", command_zone=[BOLAS_CARD], opponent_seat_id=1)
    rv = fr.review_bundle(bundle)
    assert rv["opponent_commanders"] == ["Nicol Bolas, Dragon-God"]


def test_opponent_commander_dungeon_excluded_real_case_346239cd():
    """Real case: match 346239cd — grp_id 78770 (card_types
    [CardType_Dungeon], no super types) sits in the opponent command zone
    but is a Dungeon game object, not a commander; the game reports
    'unknown' instead of naming the Dungeon."""
    bundle = _bundle("346239cd", command_zone=[DUNGEON_CARD], opponent_seat_id=2)
    rv = fr.review_bundle(bundle)
    assert rv["opponent_commanders"] == ["unknown"]

    # And it never enters the opposing-commander win-rate table.
    agg = fr.aggregate([rv])
    assert set(agg["opponent_commanders"]) == {"unknown"}


def test_opponent_commander_skips_typeless_emblem_objects():
    """Real case: matches 44bc39ca/988f05f1/d28cfaa9 carry a typeless
    grp_id=2 EMBLEM object in the command zone; it is a game object,
    not a commander card."""
    bundle = _bundle("44bc39ca", command_zone=[EMBLEM_OBJECT], opponent_seat_id=1)
    rv = fr.review_bundle(bundle)
    assert rv["opponent_commanders"] == ["unknown"]


def test_opponent_commander_other_seat_ignored():
    """Rule boundary: command-zone cards owned by the local seat are not
    attributed to the opponent (mirrors owner_seat_id filtering used on
    every corpus bundle)."""
    own_card = dict(EDDIE_BROCK_CARD, owner_seat_id=0)
    bundle = _bundle("synthetic-rule-boundary", command_zone=[own_card], opponent_seat_id=1)
    assert fr.review_bundle(bundle)["opponent_commanders"] == ["unknown"]


# --------------------------------------------------------------------------
# Early-end suspects
# --------------------------------------------------------------------------


def test_early_end_suspect_real_case_17ce59ea():
    """Real case: match 17ce59ea — Brawl Ladder, ended turn 8 with lives
    [25, 22] (both within margin of the Brawl starting life of 25) and
    no executed-concede / ERROR-level / timed-out line in its coach log."""
    bundle = _bundle("17ce59ea", format_name="Brawl Ladder", turn=8, lives=(25, 22))
    rv = fr.review_bundle(bundle)
    assert rv["early_end"] is True
    assert rv["starting_life"] == fr.STARTING_LIFE_BRAWL


def test_early_end_negative_low_life_real_case_c05802d7():
    """Real case: match c05802d7 — Brawl Ladder turn 8 but a seat at
    -128 life; far below starting life, so not an early-end suspect."""
    bundle = _bundle("c05802d7", format_name="Brawl Ladder", turn=8, lives=(25, -128))
    assert fr.review_bundle(bundle)["early_end"] is False


def test_early_end_negative_late_turn_real_case_00651f77():
    """Real case: match 00651f77 — Ladder game that ran to turn 15 with
    lives [2, -5]; past the early window regardless of life totals."""
    bundle = _bundle("00651f77", format_name="Ladder", turn=15, lives=(2, -5))
    assert fr.review_bundle(bundle)["early_end"] is False


def test_early_end_suppressed_by_executed_concede():
    """Real concede wording from match 05c4910b ('[CONCEDE] verified
    before conceding') suppresses the early-end flag even inside the
    early window; advisory '[CONCEDE] loss confidence ...' musings do
    NOT suppress (they appear in games that were not conceded)."""
    early_conceded = _bundle(
        "05c4910b", format_name="Ladder", turn=3, lives=(20, 18), coach_lines=[CONCEDE_EXECUTED_LINE]
    )
    assert fr.review_bundle(early_conceded)["early_end"] is False

    advisory_only = _bundle(
        "rule-boundary-advisory",
        format_name="Ladder",
        turn=3,
        lives=(20, 18),
        coach_lines=["[CONCEDE] loss confidence rising; considering options"],
    )
    assert fr.review_bundle(advisory_only)["early_end"] is True


def test_early_end_suppressed_by_backend_error_sentinel_real_case_06d2f9bf():
    """Real case: match 06d2f9bf — its coach log carries the ERROR-level
       backend sentinel line ('Backend returned error sentinel; no plan:
       request changed during planning'); an ERROR-level line explains an
    abnormal ending path, so the game is not an unexplained early end."""
    bundle = _bundle(
        "06d2f9bf", format_name="Ladder", turn=3, lives=(20, 13), coach_lines=[BACKEND_SENTINEL_ERROR_LINE]
    )
    rv = fr.review_bundle(bundle)
    assert rv["has_error_line"] is True
    assert rv["early_end"] is False


def test_early_end_suppressed_by_timed_out_lines_from_standalone_log():
    """Real logged timeout lines from standalone.log ('LLM API call timed
    out after 17.0s ...' and the proxy ERROR 'API error: Request timed out.')
    explain an early end; a full-life turn-3 boundary game carrying either is
    not an early-end suspect."""
    for line in (LLM_CALL_TIMED_OUT_LINE, PROXY_TIMEOUT_ERROR_LINE):
        bundle = _bundle(
            "rule-boundary-timeout", format_name="Ladder", turn=3, lives=(20, 20), coach_lines=[line]
        )
        rv = fr.review_bundle(bundle)
        assert rv["has_timeout_line"] or rv["has_error_line"]
        assert rv["early_end"] is False


def test_routine_stale_decision_timeout_does_not_suppress():
    """Real line from match 05c4910b ('Auto-clearing stale decision ...
    timeout=15s') is routine housekeeping — 'timeout=' without a space — and
    must not mask an otherwise unexplained early end."""
    bundle = _bundle(
        "05c4910b-housekeeping-boundary",
        format_name="Ladder",
        turn=3,
        lives=(20, 18),
        coach_lines=[STALE_DECISION_TIMEOUT_LINE],
    )
    rv = fr.review_bundle(bundle)
    assert rv["has_timeout_line"] is False
    assert rv["has_error_line"] is False
    assert rv["early_end"] is True


# --------------------------------------------------------------------------
# M1 aggregates per deck (implemented, not dropped)
# --------------------------------------------------------------------------


def test_m1_aggregates_present_per_deck():
    """Real logged lines (matches 06d2f9bf, 00651f77, c41c9c6a): the
    per-deck aggregate carries inference per call class, plans formed /
    path-corrected, mulligan keeps and decision prompt percentiles."""
    bundle = _bundle(
        "06d2f9bf",
        format_name="Ladder",
        coach_lines=[
            REQUEST_METRICS_LINE,
            PLAN_FORMED_LINE,
            PATH_CORRECTED_LINE,
            BIG_PROMPT_LINE,
            MULLIGAN_KEEP_LINE,
        ],
        decisions=[
            {
                "pending_decision": {"request_type": "ActionsAvailable", "options": [{"option_id": "pass"}]},
                "chosen_options": ["pass"],
                "trace": {},
            }
        ],
    )
    agg = fr.aggregate([fr.review_bundle(bundle)])
    deck = agg["by_deck"]["Ladder | unknown"]
    cls = deck["call_classes"]["decision.plan"]
    assert cls["calls"] == 1
    assert cls["sec"] == pytest.approx(0.6338)
    assert deck["plans_formed"] == 1
    assert deck["path_corrected"] == 1
    assert deck["big_prompts"] == [21002]
    json_deck = fr.json_report(agg)["by_deck"]["Ladder | unknown"]
    assert json_deck["prompt_median_chars"] == 21002
    assert json_deck["prompt_p90_chars"] == 21002
    # KEEP line states "2 lands" -> one keep recorded with the result.
    assert deck["keeps"] == [(2, None)]
    # The pass-only decision counts as an answer and a pass answer.
    assert deck["answers"] == 1
    assert deck["pass_answers"] == 1


def test_render_report_shows_every_ranked_row_with_examples_or_none():
    """The rendered report must not claim examples it cannot show: every
    flag / watchdog / fallback row prints '(none)' when it has no example id.
    Built from two real-case bundles (00651f77 hop line + c05802d7 negative)."""
    reviews = [
        fr.review_bundle(
            _bundle("00651f77", format_name="Ladder", turn=15, lives=(2, -5), coach_lines=[HOP_LIMIT_LINE])
        ),
        fr.review_bundle(_bundle("c05802d7", format_name="Brawl Ladder", turn=8, lives=(25, -128))),
        fr.review_bundle(_bundle("17ce59ea", format_name="Brawl Ladder", turn=8, lives=(25, 22))),
    ]
    agg = fr.aggregate(reviews)
    text = fr.render_report(agg)
    # Watchdog rows carry example ids...
    assert "e.g. 00651f77" in text
    # ...and the early-end section lists the real suspect.
    assert "17ce59ea" in text


_EVIDENCE_DIR = "/home/joshu/fable-nov-evidence/arenamcp/match_bundles"


@pytest.mark.skipif(not os.path.isdir(_EVIDENCE_DIR), reason="shared evidence corpus not mounted")
def test_collect_reviews_skips_live_context_json():
    """Real case: the corpus directory holds live_context.json beside the
    100 match bundles; it has no coach_log/packet and must be skipped and
    counted, not folded into an 'unknown' coverage row."""
    reviews, skipped = fr.collect_reviews(_EVIDENCE_DIR)
    assert skipped == 1
    assert len(reviews) == 100
    # The stray file previously manufactured this bogus row.
    assert ("unknown", "unknown") not in fr.aggregate(reviews)["by_deck"]
