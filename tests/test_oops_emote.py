"""Arena's "Oops" emote when autoplay gets stuck or clearly blunders (arenamcp.oops).

Fixtures are today's real incidents (2026-10-06):

* 18:56:13 Stingerquill Charm (our 395) dealt 3 damage to our own Yuriko (266),
  which died to SBA_Damage: the autopilot had picked Yuriko as the "beneficial"
  target (bug_20261006_185803). Player.log gameStateId 391, annotations verbatim.
* 13:50:24 Fblthp 1/1 (our 288) attacked Jace past an untapped Keeper of the
  Quiet Hour 3/2 (293), was blocked and died; nothing got through
  (bug_20261006_135027). Player-prev.log gameStateId 122, annotations verbatim.
* 17:47:07 Void Extrapolator 2/2 (231) into the same Keeper (237): both died, a trade.
* 17:51-18:57 Theoretical Necromancer activated and cancelled at its target step
  (the Self-cancel guard), and the MANUAL REQUIRED reasons of the day.
"""

from __future__ import annotations

import copy
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp.gre_bridge import EMOTE_EXPIRES_MS, GREBridge, GREBridgeError
from arenamcp.mac_bridge_adapter import MacBridgeAdapter
from arenamcp.oops import (
    ATTACK_BLUNDER,
    MANUAL_DWELL_S,
    MAX_PER_GAME,
    MIN_GAP_S,
    SELF_CANCEL,
    SELF_HARM,
    STALE_S,
    STUCK_LOOP,
    STUCK_MANUAL,
    OopsController,
    attack_blunders,
    manual_reason_counts,
    message_facts,
    same_game,
    self_harm_reason,
)
from arenamcp.pipe_adapter import PipeAdapter
from arenamcp.standalone_oops import _OopsMixin
from test_draft_autoplay import Obj, World

LOCAL, OPP = 2, 1
MATCH = "91e054ef-6006-49bc-8c36-f1ee36cc738d"
FBLTHP_MATCH = "3da54de9-9820-4ae2-b49e-8f81c913c446"


def detail(key: str, value) -> dict:
    if isinstance(value, str):
        return {"key": key, "type": "KeyValuePairValueType_string", "valueString": [value]}
    return {"key": key, "type": "KeyValuePairValueType_int32", "valueInt32": [value]}


def ann(kind: str, affected: list[int], affector: int | None = None, **details) -> dict:
    annotation = {"affectedIds": affected, "type": [f"AnnotationType_{kind}"]}
    if affector is not None:
        annotation["affectorId"] = affector
    if details:
        annotation["details"] = [detail(k, v) for k, v in details.items()]
    return annotation


# Player.log 6:56:13 PM, gameStateId 391 (turn 14, our Main1): the annotations verbatim.
STINGERQUILL_ANNOTATIONS = [
    ann("ResolutionStart", [395], 395, grpid=106390),
    ann("DamageDealt", [266], 395, damage=3, type=2, markDamage=1),
    ann("ResolutionComplete", [395], 395, grpid=106390),
    ann("ObjectIdChanged", [395], 2, orig_id=395, new_id=398),
    ann("ZoneTransfer", [398], 2, zone_src=27, zone_dest=37, category="Resolve"),
    ann("ObjectIdChanged", [266], orig_id=266, new_id=399),
    ann("ZoneTransfer", [399], zone_src=28, zone_dest=37, category="SBA_Damage"),
]
STINGERQUILL_OBJECTS = {
    266: {"controller": LOCAL, "grp_id": 106471, "types": ["CardType_Creature"]},
    395: {"controller": LOCAL, "grp_id": 106390, "types": ["CardType_Instant"]},
    398: {"controller": LOCAL, "grp_id": 106390, "types": ["CardType_Instant"]},
    399: {"controller": LOCAL, "grp_id": 106471, "types": ["CardType_Creature"]},
    389: {"controller": OPP, "grp_id": 106338, "types": ["CardType_Creature"]},
}
# Player-prev.log 1:50:24 PM, gameStateId 122 (turn 6, our combat damage step).
FBLTHP_ANNOTATIONS = [
    ann("PhaseOrStepModified", [2], phase=3, step=7),
    ann("DamageDealt", [293], 288, damage=1, type=1, markDamage=1),
    ann("DamageDealt", [288], 293, damage=3, type=1, markDamage=1),
    ann("ObjectIdChanged", [288], orig_id=288, new_id=306),
    ann("ZoneTransfer", [306], zone_src=28, zone_dest=37, category="SBA_Damage"),
]
FBLTHP_OBJECTS = {
    288: {"controller": LOCAL, "grp_id": 106458, "types": ["CardType_Creature"]},
    306: {"controller": LOCAL, "grp_id": 106458, "types": ["CardType_Creature"]},
    293: {"controller": OPP, "grp_id": 106416, "types": ["CardType_Artifact", "CardType_Creature"]},
    298: {"controller": OPP, "grp_id": 106555, "types": ["CardType_Planeswalker"]},
}
# Player.log 5:47:07 PM, gameStateId 130: Void Extrapolator 2/2 and Keeper 3/2 trade.
VOID_TRADE_ANNOTATIONS = [
    ann("PhaseOrStepModified", [2], phase=3, step=7),
    ann("DamageDealt", [237], 231, damage=2, type=1, markDamage=1),
    ann("DamageDealt", [231], 237, damage=3, type=1, markDamage=1),
    ann("LayeredEffectDestroyed", [7002], 231),
    ann("ObjectIdChanged", [231], orig_id=231, new_id=252),
    ann("ZoneTransfer", [252], zone_src=28, zone_dest=37, category="SBA_Damage"),
    ann("ObjectIdChanged", [237], orig_id=237, new_id=253),
    ann("ZoneTransfer", [253], zone_src=28, zone_dest=33, category="SBA_Damage"),
]
VOID_OBJECTS = {
    231: {"controller": LOCAL, "grp_id": 106299, "types": ["CardType_Creature"]},
    237: {"controller": OPP, "grp_id": 106416, "types": ["CardType_Artifact", "CardType_Creature"]},
}
NAMES = {
    106390: "Stingerquill Charm",
    106471: "Yuriko, Hope from the Shadows",
    106458: "Fblthp, Impossibly Lost",
    106416: "Keeper of the Quiet Hour",
}
# Arena card texts (from the bug reports' game states).
TEXTS = {
    106390: "Choose one —\n• Stingerquill Charm deals 3 damage to any target.\n"
    "• Target creature gains first strike and deathtouch until end of turn.\n"
    "• Create a 2/2 colorless Wizard Soldier creature token named Cadet. It gains haste until end of turn.",
    106471: "Flash\nWhen Yuriko enters, choose one —\n• Target creature gets -X/-0 until end of turn, "
    "where X is the number of cards in your graveyard.\n• Surveil 2.",
}


def facts_of(annotations, objects, *, turn, phase, step, active=LOCAL, match=MATCH, game=None):
    return message_facts(
        annotations,
        describe=objects.get,
        name_of=lambda grp: NAMES.get(grp, ""),
        local_seat=LOCAL,
        seats=[OPP, LOCAL],
        match_id=match,
        turn=turn,
        phase=phase,
        step=step,
        active_player=active,
        game_number=game,
    )


def stingerquill_fact() -> dict:
    (fact,) = facts_of(STINGERQUILL_ANNOTATIONS, STINGERQUILL_OBJECTS, turn=14, phase="Phase_Main1", step="")
    return fact


def stingerquill_target(**override) -> dict:
    # The autopilot's SelectTargets submission at 18:56:09: source 395, target 266.
    return {"at": 0.0, "match_id": MATCH, "turn": 14, "source_id": 395, "targets": [266], **override}


def land(instance, name, seat, tapped=False):
    return {
        "instance_id": instance,
        "grp_id": 1,
        "name": name,
        "controller_seat_id": seat,
        "owner_seat_id": seat,
        "type_line": f"Basic Land — {name}",
        "card_types": ["Land"],
        "is_tapped": tapped,
        "oracle_text": "",
    }


def creature(instance, name, seat, power, toughness, text="", tapped=False, **extra):
    return {
        "instance_id": instance,
        "grp_id": 2,
        "name": name,
        "controller_seat_id": seat,
        "owner_seat_id": seat,
        "type_line": "Creature",
        "card_types": ["Creature"],
        "power": power,
        "toughness": toughness,
        "is_tapped": tapped,
        "oracle_text": text,
        **extra,
    }


FBLTHP_TEXT = (
    "When one or more of your opponents are dealt combat damage during your turn, draw two cards. "
    "If your library has no cards in it, you win the game. Fblthp's owner shuffles him into their library."
)


def fblthp_board() -> dict:
    """The board the autopilot declared Fblthp's attack on (1:50:18 PM, turn 6)."""
    return {
        "match_id": FBLTHP_MATCH,
        "turn": {
            "turn_number": 6,
            "active_player": LOCAL,
            "phase": "Phase_Combat",
            "step": "Step_DeclareAttack",
        },
        "players": [
            {"seat_id": OPP, "is_local": False, "life_total": 20, "status": "InGame"},
            {"seat_id": LOCAL, "is_local": True, "life_total": 20, "status": "InGame"},
        ],
        "battlefield": [
            land(281, "Forest", OPP),
            land(283, "Mountain", LOCAL, tapped=True),
            land(285, "Swamp", OPP),
            land(287, "Island", LOCAL, tapped=True),
            land(292, "Forest", OPP),
            creature(288, "Fblthp, Impossibly Lost", LOCAL, 1, 1, FBLTHP_TEXT),
            creature(
                293,
                "Keeper of the Quiet Hour",
                OPP,
                3,
                2,
                "When this creature enters, empower Jace 2.",
                card_types=["Artifact", "Creature"],
            ),
            {
                "instance_id": 298,
                "name": "Jace",
                "controller_seat_id": OPP,
                "owner_seat_id": OPP,
                "type_line": "Token Planeswalker — Jace",
                "card_types": ["Planeswalker"],
                "oracle_text": "Surveil 1.\nDraw a card.",
                "counters": {"Loyalty": 1},
            },
        ],
        "hand": [
            {
                "instance_id": 242,
                "name": "Tam's Resistance",
                "oracle_text": "Put a +1/+1 counter on up to one target creature.",
            },
            {
                "instance_id": 244,
                "name": "Mindseeker Oculus",
                "oracle_text": "When this creature enters, empower Jace 4.",
            },
        ],
    }


def fblthp_attack(**override) -> dict:
    return {
        "at": 0.0,
        "match_id": FBLTHP_MATCH,
        "turn": 6,
        "attackers": [288],
        "state": fblthp_board(),
        **override,
    }


def fblthp_facts(game=None):
    return facts_of(
        FBLTHP_ANNOTATIONS,
        FBLTHP_OBJECTS,
        turn=6,
        phase="Phase_Combat",
        step="Step_CombatDamage",
        match=FBLTHP_MATCH,
        game=game,
    )


def live_state(turn=14, match=MATCH, **extra) -> dict:
    return {
        "match_id": match,
        "turn": {"turn_number": turn, "active_player": LOCAL, "stage": "Play"},
        "players": [
            {"seat_id": OPP, "is_local": False, "life_total": 20, "status": "InGame"},
            {"seat_id": LOCAL, "is_local": True, "life_total": 12, "status": "InGame"},
        ],
        "pending_decision": "Select Targets",
        **extra,
    }


class Harness:
    """An OopsController with scripted hands, a fake clock and synchronous sends."""

    def __init__(self) -> None:
        self.settings = {"oops_emote": True}
        self.autopilot = True
        self.bridge = True
        self.over = False
        self.conceding = False
        self.stuck = True
        self.draft = False
        self.now = 1000.0
        self.sent: list[dict] = []
        self.result: dict | Exception = {
            "ok": True,
            "submitted_type": "Emote",
            "emote_id": "Phrase_Basic_Oops",
        }
        self.controller = OopsController(
            emote_fn=self.emote,
            settings_get=lambda key, default=None: self.settings.get(key, default),
            autopilot_on=lambda: self.autopilot,
            bridge_ready=lambda: self.bridge,
            game_over=lambda: self.over,
            concede_active=lambda game_key=None: self.conceding,
            still_stuck=lambda state: self.stuck,
            draft_active=lambda: self.draft,
            card_text=lambda grp: TEXTS.get(grp, ""),
            clock=lambda: self.now,
            wall_clock=lambda: 1_790_000_000.0 + self.now,
            start_thread=lambda target, *args: target(*args),
        )

    def emote(self, state: dict) -> dict:
        self.sent.append(state)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def observe(self, state=None, game=(MATCH, 1), **kwargs) -> None:
        self.controller.observe(state or live_state(), game, **kwargs)

    def report(self, kind=SELF_CANCEL, key="k", text="the autopilot cancelled its own play", state=None):
        return self.controller.report(kind, key, text, state or live_state())

    def outcomes(self) -> list[tuple[str, str]]:
        return [(d["kind"], d["outcome"]) for d in self.controller.snapshot()["recent"]]


# --- MANUAL REQUIRED reasons ------------------------------------------------------------


@pytest.mark.parametrize(
    "reason, counts",
    [
        # 2026-10-06 17:48:19, 19:01:31, 15:23:16 (the coach log's exact reasons)
        ("SelectTargets: no safe automatic choice — pick manually", True),
        ("Bridge couldn't handle declare_blockers (?) — take this action manually.", True),
        ("Safe-default submission failed for non-passable request", True),
        ("Bridge couldn't handle select_target (?) — take this action manually.", True),
        ("Search: no safe automatic choice — pick manually", True),
        ("SelectN not accepted after 3 submissions", True),
        ("Blocked action repeated in the same priority window", True),
        ("Action repeatedly failed (3x): cast_spell Lightning Greaves", True),
        ("Target selection was not verified — check targets manually", True),
        # never: not the autopilot's own stuck move
        ("Unmapped GRE interaction", False),  # 13:53:22 and 14:05:43, both between games
        ("Planner produced no safe action", False),  # 18:51:33, the LLM gateway outage
        ("ActionsAvailable not accepted after 3 submissions", False),  # repeated passes
        ("GRE bridge is unavailable for play_land (Forest) — take this action manually.", False),
        ("Bridge couldn't handle choose_starting_player (?) — take this action manually.", False),
        ("You control the opponent's turn — choose for them (SelectTargets)", False),
        (
            "Game advanced past activate_ability (Lightning Greaves) — bridge no longer offers this action. "
            "Take it manually if still needed.",
            False,
        ),
        ("Something new nobody has seen yet", False),
    ],
)
def test_only_stuck_manual_reasons_count(reason, counts):
    assert manual_reason_counts(reason) is counts


# --- stuck triggers ---------------------------------------------------------------------


def test_semantic_progress_pause_sends_one_oops_at_once(caplog):
    h = Harness()
    h.observe()
    reason = "The same selecttargets choice did not advance after 3 submissions over 9s."
    with caplog.at_level(logging.INFO, logger="arenamcp.oops"):
        assert h.report(STUCK_LOOP, "sha256-of-the-window", reason)
    assert len(h.sent) == 1 and h.sent[0]["match_id"] == MATCH
    assert not h.report(STUCK_LOOP, "sha256-of-the-window", reason)  # one Oops per incident
    assert len(h.sent) == 1
    assert "[OOPS] sent for stuck_loop" in caplog.text
    assert h.controller.snapshot()["sent_this_game"] == 1


def test_manual_required_waits_and_sends_only_when_still_stuck():
    h = Harness()
    h.observe()
    reason = "SelectTargets: no safe automatic choice — pick manually"
    assert h.report(STUCK_MANUAL, ("w", 1), reason)
    assert h.sent == []  # not yet: a manual hand-back often resolves in seconds
    h.now += MANUAL_DWELL_S / 2
    h.observe()
    assert h.sent == []
    h.now += MANUAL_DWELL_S / 2
    h.observe()
    assert len(h.sent) == 1

    resolved = Harness()
    resolved.observe()
    resolved.report(STUCK_MANUAL, ("w", 2), reason)
    resolved.stuck = False  # the user (or the game) moved on within 10 s
    resolved.now += MANUAL_DWELL_S
    resolved.observe()
    assert resolved.sent == []
    assert resolved.outcomes()[-1] == (STUCK_MANUAL, "suppressed")


def test_manual_reasons_outside_the_allowlist_are_suppressed_at_once():
    h = Harness()
    h.observe()
    assert not h.report(STUCK_MANUAL, ("w", 1), "Unmapped GRE interaction")
    h.now += MANUAL_DWELL_S
    h.observe()
    assert h.sent == [] and h.outcomes() == [(STUCK_MANUAL, "suppressed")]


def test_a_new_game_drops_a_waiting_manual_incident():
    h = Harness()
    h.observe()
    h.report(STUCK_MANUAL, ("w", 1), "Safe-default submission failed for non-passable request")
    h.now += MANUAL_DWELL_S
    h.observe(live_state(turn=1, match="next-match"), game=("next-match", 2))
    assert h.sent == []
    assert h.controller.snapshot()["recent"][-1]["why"] == "the game changed"


# --- rate limits and suppression -------------------------------------------------------------


def test_rate_limits_one_per_incident_two_per_game_sixty_seconds_apart():
    h = Harness()
    h.observe()
    assert h.report(SELF_CANCEL, "a")
    assert len(h.sent) == 1
    h.now += MIN_GAP_S - 1
    h.report(SELF_CANCEL, "b")  # too soon
    assert len(h.sent) == 1
    h.now += 2
    h.report(SELF_CANCEL, "b")  # the same incident is never retried
    h.report(SELF_CANCEL, "c")
    assert len(h.sent) == 2
    h.now += MIN_GAP_S + 1
    h.report(SELF_CANCEL, "d")  # the game's cap
    assert len(h.sent) == MAX_PER_GAME == 2
    reasons = [d["why"] for d in h.controller.snapshot()["recent"] if d["outcome"] == "suppressed"]
    assert any("since the last Oops" in why for why in reasons)
    assert any("already sent 2 this game" in why for why in reasons)

    h.observe(live_state(turn=1, match="next-match"), game=("next-match", 2))
    h.report(SELF_CANCEL, "a", state=live_state(turn=1, match="next-match"))
    assert len(h.sent) == 3  # a new game has its own two


@pytest.mark.parametrize(
    "flip, why",
    [
        ("autopilot", "autoplay is off"),
        ("bridge", "the bridge is not connected"),
        ("over", "the game is over"),
        ("conceding", "a concede offer is active"),
        ("draft", "a draft is active"),
        ("setting", "setting is off"),
    ],
)
def test_suppressed_when_autoplay_is_off_or_the_game_is_not_live(flip, why):
    h = Harness()
    h.observe()
    if flip == "setting":
        h.settings["oops_emote"] = False
    else:
        setattr(h, flip, not getattr(h, flip))
    h.report(SELF_CANCEL, "a")
    assert h.sent == []
    assert why in h.controller.snapshot()["recent"][-1]["why"]


def test_never_after_the_game_is_over_by_the_board_either():
    h = Harness()
    h.observe()
    dead = live_state()
    dead["players"][1]["life_total"] = 0
    h.report(SELF_CANCEL, "a", state=dead)
    h.report(SELF_CANCEL, "b", state=live_state(pending_decision="Mulligan"))
    assert h.sent == []


def test_a_refusal_frees_the_slot_and_a_missing_wheel_stops_the_game():
    h = Harness()
    h.observe()
    h.result = {"ok": False, "error": "Stale emote: the turn changed (14 -> 15)"}
    h.report(SELF_CANCEL, "a")
    h.result = {"ok": True}
    h.report(SELF_CANCEL, "b")  # nothing was sent before: no gap applies
    assert len(h.sent) == 2 and h.controller.snapshot()["sent_this_game"] == 1

    deck = Harness()
    deck.observe()
    deck.result = {
        "ok": False,
        "error": "Phrase_Basic_Oops is not on this deck's emote wheel (Phrase_MSH_Hi)",
    }
    deck.report(SELF_CANCEL, "a")
    deck.now += MIN_GAP_S + 1
    deck.report(SELF_CANCEL, "b")
    assert len(deck.sent) == 1  # never asked again this game
    assert "emote wheel" in deck.controller.snapshot()["recent"][-1]["why"]


def test_outcome_unknown_counts_and_is_never_retried():
    h = Harness()
    h.observe()
    h.result = {
        "ok": False,
        "outcome_unknown": True,
        "error": "Pipe read timeout (5.0s) for action=send_emote",
    }
    h.report(SELF_CANCEL, "a")
    h.report(SELF_CANCEL, "b")
    assert len(h.sent) == 1  # counted: the 60 s gap applies
    assert h.controller.snapshot()["sent_this_game"] == 1
    h.result = RuntimeError("bridge exploded mid-send")
    h.now += MIN_GAP_S + 1
    h.report(SELF_CANCEL, "c")
    assert len(h.sent) == 2 and h.controller.snapshot()["sent_this_game"] == 2


def test_an_unsupported_bridge_stops_until_it_reconnects():
    h = Harness()
    h.observe()
    h.result = {"ok": False, "unsupported": True, "error": "Unknown action: send_emote"}
    h.report(SELF_CANCEL, "a")
    h.report(SELF_CANCEL, "b")
    assert len(h.sent) == 1
    h.bridge = False
    h.report(SELF_CANCEL, "c")
    h.bridge = True
    h.result = {"ok": True}
    h.report(SELF_CANCEL, "d")
    assert len(h.sent) == 2


def test_clear_pending_drops_waiting_incidents_and_snapshot_reports_them():
    h = Harness()
    h.observe()
    h.report(STUCK_MANUAL, ("w", 1), "SelectTargets: no safe automatic choice — pick manually")
    snapshot = h.controller.snapshot()
    assert snapshot["pending"][0]["kind"] == STUCK_MANUAL and snapshot["oops_emote"] is True
    h.controller.clear_pending("autoplay was toggled")
    h.now += MANUAL_DWELL_S
    h.observe()
    assert h.sent == [] and h.controller.snapshot()["pending"] == []


def test_an_engine_reload_keeps_the_games_count_and_gap():
    h = Harness()
    h.observe()
    h.report(SELF_CANCEL, "a")
    saved = h.controller.export_state()
    assert saved["sent"] == 1 and saved["match_id"] == MATCH

    reloaded = Harness()
    reloaded.now = h.now + 30  # 30 s later, in a new process
    reloaded.controller.resume_from(saved)
    reloaded.observe()
    reloaded.report(SELF_CANCEL, "b")
    assert reloaded.sent == []  # still inside the 60 s gap
    reloaded.report(SELF_CANCEL, "a")  # and incident "a" was already handled
    reloaded.now += MIN_GAP_S
    reloaded.report(SELF_CANCEL, "c")
    assert len(reloaded.sent) == 1
    reloaded.now += MIN_GAP_S + 1
    reloaded.report(SELF_CANCEL, "d")
    assert len(reloaded.sent) == 1  # two this game, counting the one before the reload


# --- clear blunders: our own spell on our own permanent -----------------------------------------


def test_the_stingerquill_misfire_is_a_self_harm_fact():
    fact = stingerquill_fact()
    assert fact["kind"] == SELF_HARM
    assert (fact["source_id"], fact["target_id"], fact["category"], fact["damage"]) == (
        395,
        266,
        "SBA_Damage",
        3,
    )
    assert (fact["source_name"], fact["target_name"]) == (
        "Stingerquill Charm",
        "Yuriko, Hope from the Shadows",
    )


def test_damage_that_does_not_kill_or_hits_the_opponent_is_no_self_harm():
    survived = [
        a for a in STINGERQUILL_ANNOTATIONS if "ZoneTransfer" not in a["type"][0] or a["affectedIds"] != [399]
    ]
    assert facts_of(survived, STINGERQUILL_OBJECTS, turn=14, phase="Phase_Main1", step="") == []
    at_them = copy.deepcopy(STINGERQUILL_ANNOTATIONS)
    at_them[1]["affectedIds"] = [389]  # the Greenhouse Propagator the model wanted
    at_them[5]["details"] = [detail("orig_id", 389), detail("new_id", 399)]
    assert facts_of(at_them, STINGERQUILL_OBJECTS, turn=14, phase="Phase_Main1", step="") == []


def test_self_harm_needs_the_autopilots_own_target_choice():
    fact = stingerquill_fact()
    texts = lambda grp: TEXTS.get(grp, "")  # noqa: E731
    assert "Stingerquill Charm dealt 3 damage to our own Yuriko" in self_harm_reason(
        fact, [stingerquill_target()], texts
    )
    assert self_harm_reason(fact, [], texts) == ""  # the user picked it
    assert self_harm_reason(fact, [stingerquill_target(source_id=401)], texts) == ""
    assert self_harm_reason(fact, [stingerquill_target(turn=13)], texts) == ""
    assert self_harm_reason(fact, [stingerquill_target(targets=[389])], texts) == ""
    payoff = {106471: "When this creature dies, draw two cards.", 106390: TEXTS[106390]}
    assert self_harm_reason(fact, [stingerquill_target()], payoff.get) == ""
    own = {106390: "Stingerquill Charm deals 3 damage to target creature you control. Draw two cards."}
    assert self_harm_reason(fact, [stingerquill_target()], lambda grp: own.get(grp, "")) == ""


def test_the_stingerquill_misfire_sends_one_oops():
    h = Harness()
    h.observe()
    h.observe(facts=[stingerquill_fact()], evidence={"targets": [stingerquill_target()], "attacks": []})
    assert len(h.sent) == 1
    assert h.controller.snapshot()["recent"][-1]["kind"] == SELF_HARM
    h.observe(facts=[stingerquill_fact()], evidence={"targets": [stingerquill_target()]})
    assert len(h.sent) == 1  # the same fact again is the same incident


def test_the_game_state_records_the_stingerquill_fact(monkeypatch):
    from arenamcp.gamestate import GameState

    state = GameState()
    state.local_seat_id = LOCAL
    state.match_id = MATCH
    monkeypatch.setattr(state, "_resolve_card_name", lambda grp: NAMES.get(grp, "?"))

    def obj(instance, grp, zone, types):
        return {
            "instanceId": instance,
            "grpId": grp,
            "type": "GameObjectType_Card",
            "zoneId": zone,
            "visibility": "Visibility_Public",
            "ownerSeatId": LOCAL,
            "controllerSeatId": LOCAL,
            "cardTypes": types,
        }

    state.update_from_message(
        {
            "type": "GameStateType_Diff",
            "gameStateId": 391,
            "turnInfo": {"phase": "Phase_Main1", "turnNumber": 14, "activePlayer": 2, "priorityPlayer": 2},
            "gameObjects": [
                obj(266, 106471, 30, ["CardType_Creature"]),
                obj(395, 106390, 30, ["CardType_Instant"]),
                obj(398, 106390, 37, ["CardType_Instant"]),
                obj(399, 106471, 37, ["CardType_Creature"]),
            ],
            "annotations": STINGERQUILL_ANNOTATIONS,
        }
    )
    facts, seq = state.oops_facts_since(0)
    assert [f["kind"] for f in facts] == [SELF_HARM] and seq == facts[0]["seq"]
    assert (facts[0]["source_name"], facts[0]["target_name"], facts[0]["turn"]) == (
        "Stingerquill Charm",
        "Yuriko, Hope from the Shadows",
        14,
    )
    assert state.oops_facts_since(seq) == ([], seq)


# --- clear blunders: an attack into a free kill -------------------------------------------------


def test_fblthp_into_an_untapped_keeper_is_an_attack_blunder():
    (fact,) = fblthp_facts()
    assert fact["kind"] == "combat" and fact["deaths"] == {288: "SBA_Damage"}
    ((attacker, reason),) = attack_blunders(fact, fblthp_attack(), [fact])
    assert attacker == 288
    assert "Fblthp, Impossibly Lost 1/1 attacked into an untapped Keeper of the Quiet Hour 3/2" in reason


def test_a_trade_is_not_a_blunder():
    board = fblthp_board()
    board["battlefield"][5] = creature(231, "Void Extrapolator", LOCAL, 2, 2)
    board["battlefield"][6]["instance_id"] = 237
    (fact,) = facts_of(
        VOID_TRADE_ANNOTATIONS, VOID_OBJECTS, turn=6, phase="Phase_Combat", step="Step_CombatDamage"
    )
    record = {"match_id": MATCH, "turn": 6, "attackers": [231], "state": board}
    assert fact["deaths"] == {231: "SBA_Damage", 237: "SBA_Damage"}
    assert attack_blunders(fact, record, [fact]) == []


def test_damage_through_or_a_trick_during_combat_is_not_a_blunder():
    (fact,) = fblthp_facts()
    hit = copy.deepcopy(fact)
    hit["damage"].append(
        {"source": 290, "target": OPP, "amount": 2, "target_kind": "player", "target_controller": OPP}
    )
    two = fblthp_attack(attackers=[288, 290])
    assert attack_blunders(hit, two, [hit]) == []  # an alpha strike that got damage in

    cast = facts_of(
        [ann("UserActionTaken", [310], OPP, actionType=1, abilityGrpId=0)],
        FBLTHP_OBJECTS,
        turn=6,
        phase="Phase_Combat",
        step="Step_DeclareBlock",
        match=FBLTHP_MATCH,
    )
    assert cast[0]["kind"] == "combat_action"
    assert attack_blunders(fact, fblthp_attack(), [*cast, fact]) == []  # a combat trick: unpredictable

    mana = facts_of(
        [ann("UserActionTaken", [302], LOCAL, actionType=4, abilityGrpId=1002)],
        FBLTHP_OBJECTS,
        turn=6,
        phase="Phase_Combat",
        step="Step_DeclareBlock",
        match=FBLTHP_MATCH,
    )
    assert mana == []  # a mana ability changes nothing


def test_combat_is_judged_whole_at_the_regular_damage_step():
    (fact,) = fblthp_facts()
    first_strike = {**copy.deepcopy(fact), "step": "Step_FirstStrikeDamage"}
    assert attack_blunders(first_strike, fblthp_attack(), [first_strike]) == []  # not yet: wait for the rest
    regular = {**copy.deepcopy(fact), "damage": [], "deaths": {}}
    regular["damage"].append(
        {"source": 290, "target": OPP, "amount": 3, "target_kind": "player", "target_controller": OPP}
    )
    # Fblthp died to first strike, but another attacker then hit the opponent.
    assert attack_blunders(regular, fblthp_attack(attackers=[288, 290]), [first_strike, regular]) == []
    quiet = {**copy.deepcopy(fact), "damage": [], "deaths": {}}
    ((attacker, _why),) = attack_blunders(quiet, fblthp_attack(), [first_strike, quiet])
    assert attacker == 288


def test_an_unpredictable_or_forced_attack_is_not_a_blunder():
    (fact,) = fblthp_facts()
    sturdy = fblthp_attack()
    # Divining Duelist 3/2 into Hallway Heckler 2/3: the block should have killed the blocker.
    sturdy["state"]["battlefield"][5] = creature(288, "Divining Duelist", LOCAL, 3, 2)
    sturdy["state"]["battlefield"][6] = creature(293, "Hallway Heckler", OPP, 2, 3)
    assert attack_blunders(fact, sturdy, [fact]) == []

    tapped = fblthp_attack()
    tapped["state"]["battlefield"][6]["is_tapped"] = True  # it could not have blocked
    assert attack_blunders(fact, tapped, [fact]) == []

    forced = fblthp_attack(pending={"attackers": [{"attackerInstanceId": 288, "mustAttack": True}]})
    assert attack_blunders(fact, forced, [fact]) == []

    theirs = copy.deepcopy(fact)
    theirs["active_player"] = OPP  # on their turn our creature blocked, it did not attack
    assert attack_blunders(theirs, fblthp_attack(), [theirs]) == []


def test_the_fblthp_attack_sends_one_oops():
    h = Harness()
    state = live_state(turn=6, match=FBLTHP_MATCH)
    h.observe(state, game=(FBLTHP_MATCH, 1))
    h.observe(state, game=(FBLTHP_MATCH, 1), facts=fblthp_facts(), evidence={"attacks": [fblthp_attack()]})
    assert len(h.sent) == 1 and h.controller.snapshot()["recent"][-1]["kind"] == ATTACK_BLUNDER


# --- GREBridge.send_emote ----------------------------------------------------------------------


def test_bridge_emote_capability_follows_the_client():
    bridge = GREBridge()
    assert bridge.emote_supported is False  # not connected
    bridge._connected = True
    for version, supported in [
        (None, False),
        ("0.6.3", False),
        ("0.6.4", False),
        ("0.6.5", True),
        ("0.7", True),
    ]:
        bridge.plugin_version = version
        assert bridge.emote_supported is supported, version
    bridge.plugin_version = "0.6.3"
    bridge._mac_adapter = object()  # the native library's adapter always can
    assert bridge.emote_supported is True
    bridge._emote_unsupported = True  # it said "Unknown action: send_emote"
    assert bridge.emote_supported is False


def test_bridge_send_emote_sends_once_and_never_retries(monkeypatch):
    bridge = GREBridge()
    bridge._connected = True
    bridge._mac_adapter = object()
    sent: list[dict] = []
    reconnects: list[bool] = []

    def failing(cmd: dict, timeout: float | None = None) -> dict:
        sent.append(cmd)
        raise GREBridgeError("Pipe read timeout (5.0s) for action=send_emote")

    monkeypatch.setattr(bridge, "_send_command", failing)
    monkeypatch.setattr(bridge, "connect", lambda: reconnects.append(True) or True)
    result = bridge.send_emote("oops", expected_match_id=MATCH, expected_turn=14)
    assert result["outcome_unknown"] and not result["ok"] and len(sent) == 1 and reconnects == []
    assert sent[0] == {
        "action": "send_emote",
        "emote": "oops",
        "expires_in_ms": EMOTE_EXPIRES_MS,
        "expected_match_id": MATCH,
        "expected_turn": 14,
    }

    monkeypatch.setattr(
        bridge,
        "_send_command",
        lambda cmd, timeout=None: {"ok": False, "error": "Unknown action: send_emote"},
    )
    assert bridge.send_emote()["unsupported"] is True
    assert bridge.emote_supported is False  # remembered until the client reconnects
    assert bridge.send_emote()["unsupported"] is True


def test_bridge_send_emote_never_raises(monkeypatch):
    bridge = GREBridge()
    bridge._connected = True
    bridge.plugin_version = "0.6.5"
    calls: list[dict] = []
    assert bridge.send_emote("good game")["ok"] is False  # only Oops

    def boom(cmd, timeout=None):
        calls.append(cmd)
        raise RuntimeError("adapter bug")

    monkeypatch.setattr(bridge, "_send_command", boom)
    result = bridge.send_emote()
    assert result == {"ok": False, "outcome_unknown": True, "error": "adapter bug"} and len(calls) == 1
    monkeypatch.setattr(
        bridge, "_send_command", lambda cmd, timeout=None: {"ok": True, "emote_id": "Phrase_Basic_Oops"}
    )
    assert bridge.send_emote()["ok"] is True


# --- the native Mac bridge: the emote wheel's own click -------------------------------------------

GRE, CHANNEL, UI, RECEIVED, DIALOG, OTHER, WHEEL, OPTIONS, DATA = 40, 41, 80, 81, 90, 91, 100, 101, 102


class EmoteWorld(World):
    """World plus class expectations, list dumps and failing calls, like the probe."""

    def __init__(self) -> None:
        super().__init__()
        self.failures: dict[tuple[int, str], str] = {}
        self.dumps: dict[int, dict] = {}  # extra fields the probe encodes with an object

    def node(self, value):
        node = super().node(value)
        if isinstance(value, Obj) and value.handle in self.dumps:
            node = {**node, **self.dumps[value.handle]}
        return node

    def send(self, command: dict, timeout: float | None) -> dict:
        results: list = []
        for index, op in enumerate(command["ops"]):
            if op["op"] == "expect" and "class" in op:
                handle = self.target(op["target"], results)
                if handle is None or self.classes[handle].rsplit(".", 1)[-1] != op["class"]:
                    return {"ok": False, "error": f"expected {op['class']}", "failed_op": index}
                results.append(True)
                continue
            if op["op"] == "call":
                handle = self.target(op["target"], results)
                if (handle, op["method"]) in self.failures:
                    self.calls.append((handle, op["method"], op["args"]))
                    return {"ok": False, "error": self.failures[(handle, op["method"])], "failed_op": index}
            outcome = self._one(op, results)
            if outcome is not None:
                return {**outcome, "failed_op": index}
        return {"ok": True, "results": results}

    def _one(self, op: dict, results: list) -> dict | None:
        kind = op["op"]
        if kind == "find":
            handle = self.finds.get(op["class"])
            results.append({"$c": op["class"], "$h": handle} if handle else {"$none": "not found"})
            return None
        handle = self.target(op["target"], results)
        if handle is None:
            if op.get("optional"):
                results.append(None)
                return None
            return {
                "ok": False,
                "error": f"null target for {op['op']} {op.get('member') or op.get('method')}",
            }
        members = self.objects[handle]
        if kind == "get":
            results.append(self.node(members.get(op["member"])))
        elif kind == "expect":
            if members.get(op["member"]) != op["equals"]:
                return {"ok": False, "error": f"identity mismatch: {op['member']}"}
            results.append(True)
        elif kind == "call":
            self.calls.append((handle, op["method"], op["args"]))
            result = self.results.get((handle, op["method"]))
            results.append(self.node(result(op["args"]) if callable(result) else result))
        return None


def emote_world(
    *,
    match_state="GameInProgress",
    scene="DuelScene",
    stage="Play",
    manager=True,
    npe=False,
    channel_target=GRE,
    dialogs=(DIALOG,),
    wheel_disposed=False,
    on_wheel=("Phrase_Basic_Hello", "Phrase_Basic_Oops", "Phrase_Basic_GoodGame"),
) -> EmoteWorld:
    world = EmoteWorld()
    world.add(GRE, "GreClient.Rules.GreInterface", _disposed=False)
    world.add(42, "GreClient.Rules.GreInterface", _disposed=True)  # last game's
    info = world.add(50, "Wotc.Mtgo.Gre.External.Messaging.GameInfo", MatchID=MATCH, GameNumber=1)
    game = world.add(60, "GreClient.Rules.MtgGameState", Stage=stage, GameWideTurn=14, GameInfo=Obj(info))
    scenes = world.add(70, "MatchSceneManager", Current=scene)
    match = world.add(20, "MatchManager", MatchState=match_state, GreInterface=Obj(GRE))
    world.add(
        CHANNEL, "System.Action<Wotc.Mtgo.Gre.External.Messaging.UIMessage>", m_target=Obj(channel_target)
    )
    world.add(RECEIVED, "System.Action<System.String>")
    world.add(
        UI,
        "UIMessageHandler",
        _sendUIMessage=Obj(CHANNEL),
        EmoteRecievedCallback=Obj(RECEIVED),
    )
    data = [world.add(DATA + i, "EmoteData", Id=emote) for i, emote in enumerate(on_wheel)]
    world.add(OPTIONS, "System.Collections.Generic.List<EmoteData>", Count=len(data))
    # A depth-2 dump lists each EmoteData with its Id.
    world.dumps[OPTIONS] = {
        "$n": len(data),
        "$items": [{"$c": "EmoteData", "$h": h, "Id": world.objects[h]["Id"]} for h in data],
    }
    world.results[(OPTIONS, "get_Item")] = lambda args: Obj(data[args[0]["int"]])
    world.add(WHEEL, "EmoteOptionsController", Disposed=wheel_disposed, _equippedEmoteOptions=Obj(OPTIONS))
    for handle in {DIALOG, OTHER} & set(dialogs) | {DIALOG}:
        world.add(
            handle,
            "LocalPlayerDialogController",
            _uiMessageHandler=Obj(UI),
            _emoteOptionsController=Obj(WHEEL),
        )
    world.add(95, "Wotc.Mtga.DuelScene.Emotes.OpponentDialogController")
    # The probe dumps each handler's m_target inside GetInvocationList's array.
    world.results[(RECEIVED, "GetInvocationList")] = lambda args: [
        *(
            {
                "$c": "System.Action<System.String>",
                "$h": 900 + i,
                "m_target": {"$c": "LocalPlayerDialogController", "$h": h},
            }
            for i, h in enumerate(dialogs)
        ),
        {
            "$c": "System.Action<System.String>",
            "$h": 950,
            "m_target": {"$c": "Wotc.Mtga.DuelScene.Emotes.OpponentDialogController", "$h": 95},
        },
    ]
    if manager:
        members = {
            "MatchManager": Obj(match),
            "MatchSceneManager": Obj(scenes),
            "CurrentGameState": Obj(game),
            "UIMessageHandler": Obj(UI),
            "_npeDirector": Obj(world.add(97, "NPEDirector")) if npe else None,
        }
        world.finds["GameManager"] = world.add(10, "GameManager", **members)
    return world


def clicks(world: World) -> list:
    return [(h, a) for h, m, a in world.calls if m == "EmoteClicked"]


def test_mac_emote_clicks_oops_on_the_wheel():
    world = emote_world()
    result = MacBridgeAdapter(world.send).handle(
        {"action": "send_emote", "emote": "oops", "expected_match_id": MATCH, "expected_turn": 14}
    )
    assert result == {
        "ok": True,
        "submitted_type": "Emote",
        "emote": "oops",
        "emote_id": "Phrase_Basic_Oops",
        "match_id": MATCH,
        "game_number": 1,
        "turn": 14,
    }
    assert clicks(world) == [(WHEEL, [{"str": "Phrase_Basic_Oops"}])]


@pytest.mark.parametrize(
    "world_args, command",
    [
        ({"match_state": "GameComplete"}, {}),
        ({"scene": "MatchEnd"}, {}),
        ({"stage": "GameOver"}, {}),
        ({"manager": False}, {}),  # draft, deck building, menus: no GameManager
        ({}, {"expected_turn": 15}),
        ({}, {"expected_match_id": "another-match"}),
        ({}, {"emote": "good game"}),
        ({"npe": True}, {}),  # the tutorial has no emote wheel
        ({"channel_target": 42}, {}),  # still wired to last game's GreInterface
        ({"dialogs": ()}, {}),
        ({"dialogs": (DIALOG, OTHER)}, {}),
        ({"wheel_disposed": True}, {}),
        ({"on_wheel": ("Phrase_MSH_Hello", "Phrase_Basic_GoodGame")}, {}),  # an MSH starter deck's wheel
    ],
)
def test_mac_emote_refuses_unless_oops_is_on_a_live_duels_wheel(world_args, command):
    world = emote_world(**world_args)
    result = MacBridgeAdapter(world.send).handle({"action": "send_emote", **command})
    assert result["ok"] is False and not result.get("outcome_unknown")
    assert clicks(world) == []


def test_mac_emote_names_what_is_on_the_wheel_when_oops_is_not():
    world = emote_world(on_wheel=("Phrase_MSH_Hello",))
    result = MacBridgeAdapter(world.send).handle({"action": "send_emote"})
    assert "not on this deck's emote wheel (Phrase_MSH_Hello)" in result["error"]


def test_mac_emote_refuses_when_the_wheel_changed_between_batches():
    world = emote_world()
    batches: list[dict] = []

    def change_wheel(command: dict, timeout: float | None) -> dict:
        batches.append(command)
        if len(batches) == 3:  # Arena rebuilt the wheel after we read it
            world.objects[DATA + 1]["Id"] = "Phrase_Basic_Sorry"
        return world.send(command, timeout)

    result = MacBridgeAdapter(change_wheel).handle({"action": "send_emote"})
    assert result["ok"] is False and not result.get("outcome_unknown") and clicks(world) == []


def test_mac_emote_failure_before_the_click_sends_nothing_and_at_it_is_unknown():
    world = emote_world()
    world.failures[(OPTIONS, "get_Item")] = "ArgumentOutOfRangeException"
    result = MacBridgeAdapter(world.send).handle({"action": "send_emote"})
    assert result["ok"] is False and not result.get("outcome_unknown") and clicks(world) == []

    world = emote_world()
    world.failures[(WHEEL, "EmoteClicked")] = "NullReferenceException"
    result = MacBridgeAdapter(world.send).handle({"action": "send_emote"})
    assert result == {"ok": False, "outcome_unknown": True, "error": "NullReferenceException"}

    world = emote_world()
    batches: list[dict] = []

    def timeout_on_click(command: dict, timeout: float | None) -> dict:
        batches.append(command)
        if len(batches) == 3:
            return {"ok": False, "error": "main thread did not answer in time (ticks=5); outcome unknown"}
        return world.send(command, timeout)

    result = MacBridgeAdapter(timeout_on_click).handle({"action": "send_emote"})
    assert result["outcome_unknown"] is True and len(batches) == 3


def test_mac_emote_guards_the_click_in_the_same_frame():
    world = emote_world()
    batches: list[dict] = []

    def record(command: dict, timeout: float | None) -> dict:
        batches.append(command)
        return world.send(command, timeout)

    MacBridgeAdapter(record).handle({"action": "send_emote"})
    guarded = batches[-1]["ops"]
    expects = [
        (op.get("member"), op.get("equals"), op.get("class")) for op in guarded if op["op"] == "expect"
    ]
    assert ("MatchState", "GameInProgress", None) in expects
    assert ("Current", "DuelScene", None) in expects and ("Stage", "Play", None) in expects
    assert ("GameWideTurn", 14, None) in expects and ("MatchID", MATCH, None) in expects
    assert (None, None, "GreInterface") in expects and ("Disposed", False, None) in expects
    assert ("Id", "Phrase_Basic_Oops", None) in expects
    assert (guarded[-1]["op"], guarded[-1]["method"], guarded[-1]["args"]) == (
        "call",
        "EmoteClicked",
        [{"str": "Phrase_Basic_Oops"}],
    )
    assert [(op["method"], op["args"]) for op in guarded if op["op"] == "call"] == [
        ("get_Item", [{"int": 1}]),  # the wheel's Oops entry, re-read in the click's frame
        ("EmoteClicked", [{"str": "Phrase_Basic_Oops"}]),
    ]
    assert ("Count", 3, None) in expects
    # Nothing in the read-only batches clicks, sends or changes anything.
    for batch in batches[:-1]:
        assert all(
            op["op"] in ("find", "get", "expect") or op["method"] == "GetInvocationList"
            for op in batch["ops"]
        )


# --- the autopilot's reports ---------------------------------------------------------------------


def test_the_self_cancel_guard_reports_an_oops_incident(monkeypatch):
    from test_graveyard_recursion_and_modal_targets import (
        NECRO_IDS,
        _actions_poll,
        _engine,
        _loop_state,
        _targets_poll,
    )

    engine, bridge, _ = _engine(monkeypatch)
    reports: list[tuple] = []
    engine._oops_fn = lambda *args: reports.append(args)
    state = _loop_state()
    bridge.get_pending_actions.return_value = _actions_poll()
    assert engine._try_typed_decision_path(state, "decision_required")
    bridge.get_pending_actions.return_value = _targets_poll(NECRO_IDS, game_state_id=460, msg_id=648)
    assert engine._try_typed_decision_path(state, "decision_required")
    ((kind, key, text, reported_state),) = reports
    # One incident per play per game, whatever the turn (see the recurring Necromancer test).
    assert kind == SELF_CANCEL and key == ("ActionType_Activate", "theoretical necromancer", 173944)
    assert "activating 'theoretical necromancer' and cancelled it (SelectTargets cancelled)" in text
    assert reported_state["turn"]["turn_number"] == 16


def test_manual_required_reports_its_reason(monkeypatch):
    from test_graveyard_recursion_and_modal_targets import _engine, _loop_state

    engine, _bridge, _ = _engine(monkeypatch)
    engine._try_submit_plan_advancing_play = lambda state: False
    engine._try_auto_respond_escape = lambda state, why: False
    reports: list[tuple] = []
    engine._oops_fn = lambda *args: reports.append(args)
    state = _loop_state()
    engine._pause_for_manual("SelectTargets: no safe automatic choice — pick manually", state)
    ((kind, key, text, _state),) = reports
    assert kind == STUCK_MANUAL and text == "SelectTargets: no safe automatic choice — pick manually"
    assert key[0] == text and len(key[1]) == 16
    assert engine.window_still_given_up(state)
    assert engine.window_still_given_up({**state, "turn": {"turn_number": 17}}) is False
    assert engine._given_up_window_sig is not None  # the read-only check cleared nothing


def test_the_semantic_guard_reports_before_the_capture_turns_autoplay_off(monkeypatch):
    from arenamcp.autopilot_progress import DecisionProgressGuard
    from test_graveyard_recursion_and_modal_targets import NECRO_IDS, _engine, _loop_state, _targets_poll

    engine, _bridge, _ = _engine(monkeypatch)
    now = [0.0]
    engine._progress_guard = DecisionProgressGuard(clock=lambda: now[0])
    order: list[str] = []
    engine._oops_fn = lambda kind, *rest: order.append(kind)
    engine._stuck_report_fn = lambda reason, context: order.append("capture")
    state = _loop_state()
    poll = _targets_poll(NECRO_IDS, game_state_id=460, msg_id=648)
    for _ in range(3):
        assert not engine._observe_decision_progress(poll, state)
        engine._progress_guard.note_attempt("submit_targets", {"args": ([271],)})
        now[0] += 4.5
    assert engine._observe_decision_progress(poll, state)
    assert order == [STUCK_LOOP, "capture"]


def test_submitted_targets_and_declared_attacks_become_evidence(monkeypatch):
    from test_graveyard_recursion_and_modal_targets import _engine

    engine, bridge, _ = _engine(monkeypatch)
    bridge.submit_targets.return_value = True
    state = live_state(decision_context={"type": "target_selection", "source_id": 395})
    poll = {"has_pending": True, "request_type": "SelectTargets", "decision_context": {"sourceId": 395}}
    assert engine._progress_bridge(state, poll=poll).submit_targets([266])
    (record,) = engine.oops_evidence()["targets"]
    assert (record["match_id"], record["turn"], record["source_id"], record["targets"]) == (
        MATCH,
        14,
        395,
        [266],
    )

    bridge.submit_targets.return_value = False  # refused: nothing to remember
    engine._progress_bridge(state, poll=poll).submit_targets([389])
    assert len(engine.oops_evidence()["targets"]) == 1

    engine._note_declared_attack(fblthp_board(), [{"attackerInstanceId": 288}], {"attackers": []})
    (attack,) = engine.oops_evidence()["attacks"]
    assert (
        attack["attackers"] == [288] and attack["turn"] == 6 and attack["state"]["match_id"] == FBLTHP_MATCH
    )


# --- coach wiring: setting, pipe, mixin, diagnostics ----------------------------------------------


def test_the_setting_defaults_on():
    from arenamcp.settings import DEFAULTS

    assert DEFAULTS["oops_emote"] is True


def test_set_oops_emote_reaches_the_engine_and_falls_back_to_settings():
    toggled: list[bool] = []
    adapter = PipeAdapter()
    adapter._emit = lambda event: None  # type: ignore[method-assign]
    adapter._coach = SimpleNamespace(set_oops_emote=toggled.append)
    adapter._dispatch({"cmd": "set_oops_emote", "enabled": False})
    adapter._dispatch({"cmd": "set_oops_emote", "enabled": True})
    adapter._dispatch({"cmd": "set_oops_emote", "enabled": "yes"})  # only a real True turns it on
    assert toggled == [False, True, False]

    stored: dict = {}
    adapter._coach = SimpleNamespace(settings=SimpleNamespace(set=lambda k, v: stored.__setitem__(k, v)))
    adapter._dispatch({"cmd": "set_oops_emote", "enabled": False})
    assert stored == {"oops_emote": False}


class Runtime(_OopsMixin):
    """The coach attributes the Oops mixin reads."""

    def __init__(self) -> None:
        self.stored: dict = {}
        self.settings = SimpleNamespace(
            get=lambda k, d=None: self.stored.get(k, d), set=self.stored.__setitem__
        )
        self._running = True
        self._autopilot_enabled = True
        self._autopilot_dry_run = False
        self._autopilot = SimpleNamespace(
            _config=SimpleNamespace(dry_run=False, land_drop_mode=False),
            state=SimpleNamespace(value="paused"),
            window_still_given_up=lambda state: state.get("_bridge_request_type") == "SelectTargets",
            oops_evidence=lambda: {"targets": [], "attacks": []},
        )
        self._bridge_poller = SimpleNamespace(connected=True)
        self._init_oops()


def test_paused_autoplay_still_counts_as_on_but_dry_run_and_land_only_do_not():
    runtime = Runtime()
    assert runtime._oops_autopilot_on()  # MANUAL REQUIRED / the semantic pause leave it PAUSED
    runtime._autopilot._config.land_drop_mode = True
    assert not runtime._oops_autopilot_on()
    runtime._autopilot._config.land_drop_mode = False
    runtime._autopilot_dry_run = True
    assert not runtime._oops_autopilot_on()
    runtime._autopilot_dry_run = False
    runtime._autopilot_enabled = False
    assert not runtime._oops_autopilot_on()
    runtime._autopilot_enabled = True
    runtime._running = False  # stopping or reloading
    assert not runtime._oops_autopilot_on()


def test_the_mixin_needs_an_emote_capable_bridge(monkeypatch):
    import arenamcp.gre_bridge as gre_bridge

    runtime = Runtime()
    fake = Mock(emote_supported=False)
    monkeypatch.setattr(gre_bridge, "get_bridge", lambda: fake)
    assert not runtime._oops_bridge_ready()
    fake.emote_supported = True
    assert runtime._oops_bridge_ready()
    runtime._bridge_poller.connected = False
    assert not runtime._oops_bridge_ready()

    fake.send_emote.return_value = {"ok": True}
    assert runtime._oops_emote_via_bridge(live_state()) == {"ok": True}
    fake.send_emote.assert_called_once_with("oops", expected_match_id=MATCH, expected_turn=14)


def test_the_mixin_judges_the_dwell_on_the_bridge_stamped_state():
    runtime = Runtime()
    runtime._bridge_judged_state = lambda state, up: (
        {**state, "_bridge_request_type": "SelectTargets"} if up else state
    )
    assert runtime._oops_still_stuck(live_state())
    runtime._autopilot.state.value = "idle"  # the user resumed autoplay
    assert not runtime._oops_still_stuck(live_state())


def test_turning_the_setting_off_saves_it_and_drops_waiting_incidents():
    runtime = Runtime()
    runtime._oops.observe(live_state(), (MATCH, 1))
    runtime._oops.report(
        STUCK_MANUAL, ("w", 1), "SelectTargets: no safe automatic choice — pick manually", live_state()
    )
    assert runtime._oops.snapshot()["pending"]
    assert runtime.set_oops_emote(False) is False
    assert runtime.stored == {"oops_emote": False} and runtime._oops.snapshot()["pending"] == []
    assert runtime._oops_snapshot()["oops_emote"] is False


def test_bug_reports_include_the_oops_state():
    from arenamcp.standalone_diagnostics import _DiagnosticsMixin

    class DiagnosticsRuntime(Runtime, _DiagnosticsMixin):
        pass

    info = DiagnosticsRuntime()._collect_autopilot_info()
    assert info["oops"]["oops_emote"] is True and info["oops"]["max_per_game"] == MAX_PER_GAME


def test_the_engine_reload_checkpoint_carries_the_oops_record():
    runtime = Runtime()
    runtime._oops.observe(live_state(), (MATCH, 1))
    record = runtime._oops_export()
    assert record["match_id"] == MATCH and record["sent"] == 0
    runtime._oops_resume_from(record)
    assert runtime._oops._resume == record


def test_the_tools_menu_toggles_the_oops_emote(qapp, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtGui import QAction

    from arenamcp.desktop.main_window import MainWindow

    window = MainWindow()
    try:
        sent: list[dict] = []
        monkeypatch.setattr(window._session._process, "send_payload", sent.append)
        (action,) = [a for a in window.findChildren(QAction) if a.text() == "Send 'Oops' Emote"]
        assert action.isCheckable() and action.isChecked()  # default on
        action.setChecked(False)
        assert {"cmd": "set_oops_emote", "enabled": False} in sent
        assert window._settings.get("oops_emote") is False
        action.setChecked(True)
        assert {"cmd": "set_oops_emote", "enabled": True} in sent
    finally:
        window.close()


def test_a_reported_incident_never_blocks_or_raises(monkeypatch):
    h = Harness()
    h.observe()
    h.controller._emote_fn = Mock(side_effect=KeyError("boom"))
    assert h.report(SELF_CANCEL, "a")  # counted as maybe-sent, logged, never raised
    h.controller._still_stuck = Mock(side_effect=RuntimeError("engine gone"))
    h.report(STUCK_MANUAL, ("w", 1), "SelectTargets: no safe automatic choice — pick manually")
    h.now += MANUAL_DWELL_S
    h.observe()  # still_stuck raising reads as "not stuck"
    assert h.controller.snapshot()["recent"][-1]["outcome"] == "suppressed"
    h.controller.observe(
        None, None, facts=[{"kind": "combat", "match_id": None}], evidence={"attacks": [{"turn": None}]}
    )


# --- review regressions (2026-10-06 evening) ------------------------------------------------------


def exchange_board(opp_life: int = 20) -> dict:
    """Our Grizzly Bears 2/2 and Craw Wurm 5/5 attack an untapped Hill Giant 3/3 and Grey Ogre 2/2."""
    return {
        "match_id": MATCH,
        "game_number": 1,
        "turn": {
            "turn_number": 7,
            "active_player": LOCAL,
            "phase": "Phase_Combat",
            "step": "Step_DeclareAttack",
            "stage": "Play",
        },
        "players": [
            {"seat_id": OPP, "is_local": False, "life_total": opp_life, "status": "InGame"},
            {"seat_id": LOCAL, "is_local": True, "life_total": 14, "status": "InGame"},
        ],
        "battlefield": [
            land(281, "Forest", LOCAL, tapped=True),
            creature(300, "Grizzly Bears", LOCAL, 2, 2),
            creature(301, "Craw Wurm", LOCAL, 5, 5),
            creature(310, "Hill Giant", OPP, 3, 3),
            creature(311, "Grey Ogre", OPP, 2, 2),
        ],
        "hand": [],
    }


EXCHANGE_OBJECTS = {
    300: {"controller": LOCAL, "grp_id": 1, "types": ["CardType_Creature"]},
    301: {"controller": LOCAL, "grp_id": 2, "types": ["CardType_Creature"]},
    310: {"controller": OPP, "grp_id": 3, "types": ["CardType_Creature"]},
    311: {"controller": OPP, "grp_id": 4, "types": ["CardType_Creature"]},
}


def combat_record(board: dict, attackers: list[int], **override) -> dict:
    turn = board["turn"]["turn_number"]
    return {
        "at": 0.0,
        "match_id": board["match_id"],
        "game_number": board.get("game_number"),
        "turn": turn,
        "attackers": attackers,
        "state": board,
        **override,
    }


@pytest.mark.parametrize("opp_life", [20, 7])
def test_an_exchange_across_two_attackers_is_not_a_blunder(opp_life):
    """Giant blocks Bears (Bears die), Ogre blocks Wurm (Ogre dies): we lost a 2/2, they lost a 2/2.

    The losing-attack guard keeps this attack (the opponent can't answer both
    attackers for free; at 7 life it is lethal unblocked), so it is no Oops.
    """
    from arenamcp.combat_strategy import losing_attackers

    assert losing_attackers(exchange_board(opp_life), [300, 301]) == {}
    facts = facts_of(
        [
            ann("DamageDealt", [310], 300, damage=2, type=1),
            ann("DamageDealt", [300], 310, damage=3, type=1),
            ann("DamageDealt", [311], 301, damage=5, type=1),
            ann("DamageDealt", [301], 311, damage=2, type=1),
            ann("ObjectIdChanged", [300], orig_id=300, new_id=320),
            ann("ZoneTransfer", [320], zone_src=28, zone_dest=37, category="SBA_Damage"),
            ann("ObjectIdChanged", [311], orig_id=311, new_id=321),
            ann("ZoneTransfer", [321], zone_src=28, zone_dest=33, category="SBA_Damage"),
        ],
        EXCHANGE_OBJECTS,
        turn=7,
        phase="Phase_Combat",
        step="Step_CombatDamage",
        game=1,
    )
    record = combat_record(exchange_board(opp_life), [300, 301])
    assert attack_blunders(facts[0], record, facts) == []
    h = Harness()
    state = live_state(turn=7, game_number=1)
    h.observe(state)
    h.observe(state, facts=facts, evidence={"attacks": [record]})
    assert h.sent == []


def test_an_attack_the_losing_attack_guard_kept_is_not_a_blunder():
    """Bears die to the Giant; the Wall holds off the Tortoise. Nothing of theirs died and nothing got through,
    but the guard kept the attack (the Wall can't kill the Tortoise), so the Oops follows the guard."""
    from arenamcp.combat_strategy import losing_attackers

    board = exchange_board()
    board["battlefield"][2] = creature(301, "Giant Tortoise", LOCAL, 1, 5)
    board["battlefield"][4] = creature(311, "Wall of Stone", OPP, 0, 7, "Defender")
    assert losing_attackers(copy.deepcopy(board), [300, 301]) == {}
    facts = facts_of(
        [
            ann("PhaseOrStepModified", [LOCAL], phase=3, step=7),
            ann("DamageDealt", [310], 300, damage=2, type=1),
            ann("DamageDealt", [300], 310, damage=3, type=1),
            ann("DamageDealt", [311], 301, damage=1, type=1),
            ann("ObjectIdChanged", [300], orig_id=300, new_id=320),
            ann("ZoneTransfer", [320], zone_src=28, zone_dest=37, category="SBA_Damage"),
        ],
        EXCHANGE_OBJECTS,
        turn=7,
        phase="Phase_Combat",
        step="Step_CombatDamage",
        game=1,
    )
    assert facts[0]["deaths"] == {300: "SBA_Damage"}
    assert attack_blunders(facts[0], combat_record(board, [300, 301]), facts) == []
    # The same Bears attacking alone was a free kill the guard would have dropped.
    alone = copy.deepcopy(board)
    del alone["battlefield"][2]
    ((attacker, _why),) = attack_blunders(facts[0], combat_record(alone, [300]), facts)
    assert attacker == 300


def test_same_game_tells_bo3_games_apart():
    assert same_game({"match_id": "m", "game_number": 1}, {"match_id": "m", "game_number": 1})
    assert not same_game({"match_id": "m", "game_number": 1}, {"match_id": "m", "game_number": 2})
    assert not same_game({"match_id": "m"}, {"match_id": "n"})
    assert same_game({"match_id": "m", "game_number": None}, {"match_id": "m", "game_number": 2})
    (fact,) = fblthp_facts(game=2)
    assert attack_blunders(fact, fblthp_attack(game_number=1), [fact]) == []
    assert (
        self_harm_reason(
            {**stingerquill_fact(), "game_number": 2},
            [stingerquill_target(game_number=1)],
            lambda grp: TEXTS.get(grp, ""),
        )
        == ""
    )


@pytest.mark.parametrize("games", [(1, 2), (None, None)])
def test_bo3_game_two_never_resends_game_ones_attack_blunder(games):
    """Match 3da54de9 was a Bo3: game 2 shares the match id and restarts turn numbers.

    Game 1's Fblthp Oops on turn 6, then game 2's turn-6 trade (our 2/2 for
    their 2/2) while the engine still holds game 1's attack record: nothing.
    """
    first, second = games
    h = Harness()
    g1_attack = fblthp_attack(game_number=first)
    game1 = live_state(turn=6, match=FBLTHP_MATCH, game_number=first)
    h.observe(
        game1, game=(FBLTHP_MATCH, 1), facts=fblthp_facts(game=first), evidence={"attacks": [g1_attack]}
    )
    assert len(h.sent) == 1
    h.now += 600
    game2 = live_state(turn=6, match=FBLTHP_MATCH, game_number=second)
    h.observe(game2, game=(FBLTHP_MATCH, 2))
    board2 = {
        **fblthp_board(),
        "game_number": second,
        "battlefield": [creature(400, "Bear", LOCAL, 2, 2), creature(401, "Bear", OPP, 2, 2)],
    }
    bears = {
        400: {"controller": LOCAL, "grp_id": 9, "types": ["CardType_Creature"]},
        401: {"controller": OPP, "grp_id": 9, "types": ["CardType_Creature"]},
    }
    trade = facts_of(
        [
            ann("PhaseOrStepModified", [LOCAL], phase=3, step=7),
            ann("DamageDealt", [401], 400, damage=2, type=1),
            ann("DamageDealt", [400], 401, damage=2, type=1),
            ann("ObjectIdChanged", [400], orig_id=400, new_id=410),
            ann("ZoneTransfer", [410], zone_src=28, zone_dest=37, category="SBA_Damage"),
            ann("ObjectIdChanged", [401], orig_id=401, new_id=411),
            ann("ZoneTransfer", [411], zone_src=28, zone_dest=33, category="SBA_Damage"),
        ],
        bears,
        turn=6,
        phase="Phase_Combat",
        step="Step_CombatDamage",
        match=FBLTHP_MATCH,
        game=second,
    )
    g2_attack = combat_record(board2, [400])
    h.observe(game2, game=(FBLTHP_MATCH, 2), facts=trade, evidence={"attacks": [g1_attack, g2_attack]})
    assert len(h.sent) == 1 and h.outcomes() == [(ATTACK_BLUNDER, "sent")]


@pytest.mark.parametrize("games", [(1, 2), (None, None)])
def test_bo3_game_two_quiet_combat_never_rejudges_game_one(games):
    """Game 2, turn 6: our attacker is held off by a wall and nothing dies. That regular damage step is
    judged too, and must not re-read game 1's turn-6 Fblthp death from the controller's fact history."""
    first, second = games
    h = Harness()
    g1_attack = fblthp_attack(game_number=first)
    game1 = live_state(turn=6, match=FBLTHP_MATCH, game_number=first)
    h.observe(
        game1, game=(FBLTHP_MATCH, 1), facts=fblthp_facts(game=first), evidence={"attacks": [g1_attack]}
    )
    h.now += 600
    game2 = live_state(turn=6, match=FBLTHP_MATCH, game_number=second)
    h.observe(game2, game=(FBLTHP_MATCH, 2))
    quiet = facts_of(
        [
            ann("PhaseOrStepModified", [LOCAL], phase=3, step=7),
            ann("DamageDealt", [701], 700, damage=2, type=1),
        ],
        {
            700: {"controller": LOCAL, "grp_id": 9, "types": ["CardType_Creature"]},
            701: {"controller": OPP, "grp_id": 8, "types": ["CardType_Creature"]},
        },
        turn=6,
        phase="Phase_Combat",
        step="Step_CombatDamage",
        match=FBLTHP_MATCH,
        game=second,
    )
    board2 = {
        **fblthp_board(),
        "game_number": second,
        "battlefield": [creature(700, "Bear", LOCAL, 2, 2), creature(701, "Wall", OPP, 0, 4)],
    }
    h.observe(
        game2,
        game=(FBLTHP_MATCH, 2),
        facts=quiet,
        evidence={"attacks": [g1_attack, combat_record(board2, [700])]},
    )
    assert len(h.sent) == 1


def test_bo3_game_one_facts_never_hide_a_game_two_blunder():
    """A game-1 trick during turn 6's combat must not excuse game 2's turn-6 Fblthp attack."""
    h = Harness()
    trick = facts_of(
        [ann("UserActionTaken", [310], OPP, actionType=1, abilityGrpId=0)],
        FBLTHP_OBJECTS,
        turn=6,
        phase="Phase_Combat",
        step="Step_DeclareBlock",
        match=FBLTHP_MATCH,
        game=1,
    )
    h.observe(live_state(turn=6, match=FBLTHP_MATCH, game_number=1), game=(FBLTHP_MATCH, 1), facts=trick)
    game2 = live_state(turn=6, match=FBLTHP_MATCH, game_number=2)
    h.observe(game2, game=(FBLTHP_MATCH, 2))
    h.observe(
        game2,
        game=(FBLTHP_MATCH, 2),
        facts=fblthp_facts(game=2),
        evidence={"attacks": [fblthp_attack(game_number=2)]},
    )
    assert len(h.sent) == 1


def test_the_engine_keeps_only_this_games_evidence(monkeypatch):
    from test_graveyard_recursion_and_modal_targets import _engine

    engine, bridge, _ = _engine(monkeypatch)
    engine._note_declared_attack({**fblthp_board(), "game_number": 1}, [{"attackerInstanceId": 288}], None)
    game2 = {**fblthp_board(), "game_number": 2}
    game2["turn"] = {**game2["turn"], "turn_number": 6}
    engine._note_declared_attack(game2, [{"attackerInstanceId": 400}], None)
    assert [(r["game_number"], r["attackers"]) for r in engine.oops_evidence()["attacks"]] == [(2, [400])]

    # Game numbers unknown: a lower turn number than a stored record means a new game.
    engine._oops_attacks.clear()
    engine._note_declared_attack(fblthp_board(), [{"attackerInstanceId": 288}], None)
    early = fblthp_board()
    early["turn"] = {**early["turn"], "turn_number": 2}
    engine._note_declared_attack(early, [{"attackerInstanceId": 500}], None)
    assert [r["attackers"] for r in engine.oops_evidence()["attacks"]] == [[500]]

    bridge.submit_targets.return_value = True
    poll = {"has_pending": True, "request_type": "SelectTargets", "decision_context": {"sourceId": 395}}
    engine._progress_bridge(live_state(game_number=1), poll=poll).submit_targets([266])
    engine._progress_bridge(live_state(turn=3, game_number=2), poll=poll).submit_targets([212])
    assert [(r["game_number"], r["targets"]) for r in engine.oops_evidence()["targets"]] == [(2, [212])]


def test_the_game_state_stamps_the_game_number_and_time_on_facts(monkeypatch):
    from arenamcp.gamestate import GameState

    state = GameState()
    state.local_seat_id = LOCAL
    monkeypatch.setattr(state, "_resolve_card_name", lambda grp: NAMES.get(grp, "?"))
    state.update_from_message(
        {
            "type": "GameStateType_Full",
            "gameStateId": 1,
            # Player-prev.log line 8411: game 2 of match 3da54de9 starts.
            "gameInfo": {
                "matchID": FBLTHP_MATCH,
                "gameNumber": 2,
                "stage": "GameStage_Start",
                "matchState": "MatchState_GameInProgress",
            },
            "turnInfo": {
                "phase": "Phase_Combat",
                "step": "Step_CombatDamage",
                "turnNumber": 6,
                "activePlayer": 2,
            },
            "annotations": [ann("PhaseOrStepModified", [LOCAL], phase=3, step=7)],
        }
    )
    assert state.game_number == 2 and state.get_published_snapshot()["game_number"] == 2
    facts, _ = state.oops_facts_since(0)
    (fact,) = facts
    assert (fact["kind"], fact["match_id"], fact["game_number"]) == ("combat", FBLTHP_MATCH, 2)
    assert isinstance(fact["at"], float)
    assert state.export_checkpoint()["game_number"] == 2
    state.reset()  # IntermissionReq between games
    assert state.game_number is None


def test_get_game_state_carries_the_game_number(monkeypatch):
    import arenamcp.server as server_module

    class _Snapshot:
        def ensure_local_seat_id(self):
            return None

        def get_published_snapshot(self, deep_copy=False):
            return {"match_id": FBLTHP_MATCH, "game_number": 2, "turn_info": {"turn_number": 6}, "zones": {}}

    monkeypatch.setattr(server_module, "watcher", object())
    monkeypatch.setattr(server_module, "game_state", _Snapshot())
    monkeypatch.setattr(server_module, "_save_match_state_if_needed", lambda: None)
    monkeypatch.setattr(server_module, "_get_bridge_overlay", lambda **_kwargs: {})
    result = server_module.get_game_state()
    assert (result["match_id"], result["game_number"]) == (FBLTHP_MATCH, 2)


def test_no_oops_while_the_auto_concede_is_being_sent():
    """The countdown claims itself (armed turns False) before the concede is sent and confirmed."""
    import test_concede as tc

    conc = tc.Harness(tc.bug_state())
    key = (conc.state["match_id"], 1)
    h = Harness()
    h.controller._concede_active = conc.controller.concede_in_progress
    h.observe(conc.state, game=key)
    assert h.report(
        STUCK_MANUAL, ("w", 1), "SelectTargets: no safe automatic choice — pick manually", conc.state
    )
    conc.observe(key)
    assert conc.controller.armed and conc.controller.concede_in_progress(key)
    h.observe(conc.state, game=key)  # a countdown arms: the waiting incident is dropped
    assert h.controller.snapshot()["pending"] == []
    seen: list[bool] = []

    def during_concede():
        seen.append(conc.controller.armed)
        h.now += MANUAL_DWELL_S + 1
        h.observe(conc.state, game=key)
        h.report(SELF_CANCEL, "x", state=conc.state)

    conc.on_concede = during_concede
    conc.run_countdown()
    assert seen == [False] and conc.states[-1] == "conceded"
    assert h.sent == []
    assert "conceded" in h.controller.snapshot()["recent"][-1]["why"]
    assert conc.controller.concede_in_progress(key) and not conc.controller.concede_in_progress(("other", 1))


def test_a_cancelled_countdown_frees_the_oops():
    import test_concede as tc

    conc = tc.Harness(tc.bug_state())
    key = (conc.state["match_id"], 1)
    h = Harness()
    h.controller._concede_active = conc.controller.concede_in_progress
    h.observe(conc.state, game=key)
    conc.observe(key)
    h.report(SELF_CANCEL, "a", state=conc.state)
    assert h.sent == []
    assert conc.controller.cancel("ui")  # "keep playing"
    h.report(SELF_CANCEL, "b", state=conc.state)
    assert len(h.sent) == 1


def test_the_mixin_asks_whether_this_games_concede_is_in_progress():
    runtime = Runtime()
    asked: list = []
    runtime.concede_in_progress = lambda game_key: asked.append(game_key) or game_key == (MATCH, 1)
    assert runtime._oops_concede_active((MATCH, 1)) and not runtime._oops_concede_active((MATCH, 2))
    assert asked == [(MATCH, 1), (MATCH, 2)]


def test_a_log_fact_read_late_or_on_a_later_turn_is_not_judged():
    """The coaching loop stalled ~30 s today (17:49:57, 18:05:04); a late fact must not emote on a later turn."""
    h = Harness()
    h.observe(live_state(turn=14))
    h.now += 45
    late = {**stingerquill_fact(), "at": h.now - 45}
    h.observe(live_state(turn=15), facts=[late], evidence={"targets": [stingerquill_target()]})
    assert h.sent == []
    assert "read on turn 15" in h.controller.snapshot()["recent"][-1]["why"]

    stale = Harness()
    stale.observe()
    old = {**stingerquill_fact(), "at": stale.now - (STALE_S + 5)}
    stale.observe(facts=[old], evidence={"targets": [stingerquill_target()]})
    assert stale.sent == []

    fresh = Harness()
    fresh.observe()
    fresh.observe(
        facts=[{**stingerquill_fact(), "at": fresh.now - 2}], evidence={"targets": [stingerquill_target()]}
    )
    assert len(fresh.sent) == 1


def test_a_stale_trick_fact_still_counts_for_its_combat():
    """Too old to judge is not too old to read: a trick seen 30 s earlier in the same combat still excuses it."""
    h = Harness()
    state = live_state(turn=6, match=FBLTHP_MATCH)
    h.observe(state, game=(FBLTHP_MATCH, 1))
    trick = facts_of(
        [ann("UserActionTaken", [310], OPP, actionType=1, abilityGrpId=0)],
        FBLTHP_OBJECTS,
        turn=6,
        phase="Phase_Combat",
        step="Step_DeclareBlock",
        match=FBLTHP_MATCH,
    )
    trick = [{**fact, "at": h.now - 30} for fact in trick]
    damage = [{**fact, "at": h.now - 1} for fact in fblthp_facts()]
    h.observe(state, game=(FBLTHP_MATCH, 1), facts=trick + damage, evidence={"attacks": [fblthp_attack()]})
    assert h.sent == []


def test_a_recurring_self_cancel_is_one_incident_per_game(monkeypatch):
    """Theoretical Necromancer was activated and cancelled for the same reason at 17:51, 17:52 and 18:57."""
    from test_graveyard_recursion_and_modal_targets import (
        NECRO_IDS,
        _actions_poll,
        _engine,
        _loop_state,
        _targets_poll,
    )

    engine, bridge, _ = _engine(monkeypatch)
    reports: list[tuple] = []
    engine._oops_fn = lambda *args: reports.append(args)
    for turn, gsid in ((16, 459), (18, 500)):
        bridge.get_pending_actions.return_value = _actions_poll(gsid)
        assert engine._try_typed_decision_path(_loop_state(turn=turn), "decision_required")
        bridge.get_pending_actions.return_value = _targets_poll(NECRO_IDS, game_state_id=gsid + 1, msg_id=648)
        assert engine._try_typed_decision_path(_loop_state(turn=turn), "decision_required")
    assert [r[0] for r in reports] == [SELF_CANCEL, SELF_CANCEL] and reports[0][1] == reports[1][1]

    h = Harness()
    h.observe(live_state(turn=16))
    h.report(SELF_CANCEL, reports[0][1], reports[0][2], live_state(turn=16))
    h.now += 75
    h.observe(live_state(turn=18))
    h.report(SELF_CANCEL, reports[1][1], reports[1][2], live_state(turn=18))
    assert len(h.sent) == 1


def _duelist_cast_poll() -> dict:
    """18:11:40 the ActionsAvailable cast of Divining Duelist (302)."""
    return {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "game_state_id": 356,
        "can_pass": True,
        "actions": [
            {
                "actionType": "ActionType_Cast",
                "grpId": 106255,
                "instanceId": 302,
                "manaCost": [{"color": '[ "Generic" ]', "count": 2}, {"color": '[ "Blue" ]', "count": 1}],
                "hasAutoTap": True,
                "autoTapActions": [{"instanceId": 223}],
            },
            {"actionType": "ActionType_Pass"},
        ],
    }


def test_declining_a_resolved_creatures_trigger_target_is_no_self_cancel(monkeypatch):
    """18:11:40 Divining Duelist was cast (302, on the stack as 316) and resolved; Player.log shows its ETB
    trigger 320 (AbilityInstanceCreated, parent 316) asking for a mode, then SelectTargetsReq sourceId 320.
    18:11:46 the autopilot declined tgt:243 and cancelled: the GRE re-sent the trigger's
    CastingTimeOptionsReq; the cast itself stood. No Oops, and the cast is not withheld."""
    from test_graveyard_recursion_and_modal_targets import _engine, _loop_state, _targets_poll

    engine, bridge, _ = _engine(monkeypatch)
    reports: list[tuple] = []
    engine._oops_fn = lambda *args: reports.append(args)
    state = _loop_state(turn=15)
    bridge.get_pending_actions.return_value = _duelist_cast_poll()
    assert engine._try_typed_decision_path(state, "decision_required")
    assert bridge.submit_action_by_index.call_count == 1
    trigger = dict(state)
    trigger["stack"] = [
        {
            "instance_id": 320,
            "grp_id": 208112,
            "name": "Divining Duelist ability",
            "object_kind": "ABILITY",
            "controller_seat_id": LOCAL,
            "parent_instance_id": 316,
        }
    ]
    trigger["decision_context"] = {
        "type": "target_selection",
        "source_id": 320,
        "source_parent_instance_id": 316,
    }
    bridge.get_pending_actions.return_value = _targets_poll((214, 243, 278), game_state_id=360, msg_id=470)
    assert engine._try_typed_decision_path(trigger, "decision_required")
    bridge.cancel_action.assert_called_once()
    assert reports == []
    assert not engine._self_cancel_withheld(trigger, "ActionType_Cast", 302, 0, "divining duelist")

    # The source is an ability on the stack even when the context lacks the parent id.
    no_parent = {**trigger, "decision_context": {"type": "target_selection", "source_id": 320}}
    assert engine._request_source_is_ability(no_parent, no_parent["decision_context"])


def test_declining_the_spells_own_targets_still_counts_as_a_self_cancelled_cast(monkeypatch):
    from test_graveyard_recursion_and_modal_targets import _engine, _loop_state, _targets_poll

    engine, bridge, _ = _engine(monkeypatch)
    reports: list[tuple] = []
    engine._oops_fn = lambda *args: reports.append(args)
    state = _loop_state(turn=15)
    bridge.get_pending_actions.return_value = _duelist_cast_poll()
    assert engine._try_typed_decision_path(state, "decision_required")
    spell = dict(state)
    spell["stack"] = [
        {
            "instance_id": 316,
            "grp_id": 106255,
            "name": "Divining Duelist",
            "object_kind": "CARD",
            "controller_seat_id": LOCAL,
        }
    ]
    spell["decision_context"] = {"type": "target_selection", "source_id": 316}
    bridge.get_pending_actions.return_value = _targets_poll((214, 243, 278), game_state_id=360, msg_id=470)
    assert engine._try_typed_decision_path(spell, "decision_required")
    ((kind, key, text, _state),) = reports
    assert kind == SELF_CANCEL and key == ("ActionType_Cast", "divining duelist", 0)
    assert "casting 'divining duelist' and cancelled it" in text
    assert engine._self_cancel_withheld(spell, "ActionType_Cast", 302, 0, "divining duelist")


def test_the_regular_damage_step_always_yields_a_combat_fact():
    (fact,) = facts_of(
        [ann("PhaseOrStepModified", [LOCAL], phase=3, step=7)],
        {},
        turn=6,
        phase="Phase_Combat",
        step="Step_CombatDamage",
    )
    assert (fact["kind"], fact["damage"], fact["deaths"]) == ("combat", [], {})
    # Later messages of the step without damage, deaths or a step change add nothing.
    assert (
        facts_of(
            [ann("ResolutionComplete", [5], 5)], {}, turn=6, phase="Phase_Combat", step="Step_CombatDamage"
        )
        == []
    )


def test_an_attacker_killed_by_a_first_striker_is_judged():
    """A 2/2 into an untapped 2/3 first striker: it dies in the first-strike step, the regular step is quiet."""
    board = live_state(turn=6)
    board["turn"].update(phase="Phase_Combat", step="Step_DeclareAttack")
    board["battlefield"] = [
        creature(501, "Bear", LOCAL, 2, 2),
        creature(601, "Fencer", OPP, 2, 3, "First strike"),
    ]
    objects = {
        501: {"controller": LOCAL, "grp_id": 9, "types": ["CardType_Creature"]},
        502: {"controller": LOCAL, "grp_id": 9, "types": ["CardType_Creature"]},
        601: {"controller": OPP, "grp_id": 8, "types": ["CardType_Creature"]},
    }
    first = facts_of(
        [
            ann("PhaseOrStepModified", [LOCAL], phase=3, step=11),
            ann("DamageDealt", [501], 601, damage=2, type=1, markDamage=1),
            ann("ObjectIdChanged", [501], orig_id=501, new_id=502),
            ann("ZoneTransfer", [502], zone_src=28, zone_dest=37, category="SBA_Damage"),
        ],
        objects,
        turn=6,
        phase="Phase_Combat",
        step="Step_FirstStrikeDamage",
    )
    regular = facts_of(
        [ann("PhaseOrStepModified", [LOCAL], phase=3, step=7)],
        objects,
        turn=6,
        phase="Phase_Combat",
        step="Step_CombatDamage",
    )
    assert [f["step"] for f in first + regular] == ["Step_FirstStrikeDamage", "Step_CombatDamage"]
    h = Harness()
    h.observe(live_state(turn=6))
    h.observe(live_state(turn=6), facts=first)
    assert h.sent == []  # not judged before the regular damage step
    h.observe(live_state(turn=6), facts=regular, evidence={"attacks": [combat_record(board, [501])]})
    assert (
        len(h.sent) == 1
        and "Bear 2/2 attacked into an untapped Fencer 2/3" in h.controller.snapshot()["recent"][-1]["detail"]
    )


def test_a_single_candidate_auto_target_is_recorded_as_evidence(monkeypatch):
    """autopilot.py's single-candidate auto-submit passes the target as a bare int."""
    from test_graveyard_recursion_and_modal_targets import _engine

    engine, bridge, _ = _engine(monkeypatch)
    bridge.submit_targets.return_value = True
    state = live_state(decision_context={"type": "target_selection", "source_id": 395})
    assert engine._progress_bridge(state).submit_targets(266)
    (record,) = engine.oops_evidence()["targets"]
    assert (record["source_id"], record["targets"]) == (395, [266])
