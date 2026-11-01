"""#308/G04 mechanic regressions, not a full historical game replay."""

import pytest
from tests.test_tactical_timing_backlog import card

from arenamcp.board_model import build_board_model
from arenamcp.line_search import _Search, search_lines
from arenamcp.line_search_moves import unmodelled_effect
from arenamcp.public_response import UnsupportedResponse, expire, protected_body, response

PROTECT = "Discard a card: This creature gains indestructible until end of turn. Tap it."


def state(*, hand_count=1, gain=3, protector_power=5, ours=()):
    burn = "Measured Bolt deals 3 damage to target creature."
    if gain:
        burn += f" You gain {gain} life."
    return dict(
        local_seat_id=1,
        opponent_hand_count=hand_count,
        turn=dict(turn_number=12, active_player=1, phase="Phase_Main1"),
        players=[
            dict(seat_id=1, is_local=True, life_total=10, lands_played=1),
            dict(seat_id=2, life_total=20),
        ],
        battlefield=[
            card(1, "Mountain", "{T}: Add {R}.", "Basic Land — Mountain"),
            card(2, "Mountain", "{T}: Add {R}.", "Basic Land — Mountain"),
            card(271, "Discard Protector", PROTECT, "Creature", seat=2, power=protector_power, toughness=1),
            card(320, "Unprotected threat", "", "Creature", seat=2, power=3, toughness=2),
            *ours,
        ],
        hand=[card(50, "Measured Bolt", burn, "Instant", "{1}{R}")],
        stack=[],
    )


def search(s):
    return search_lines(build_board_model(s), survival=True, lethal_now=False, hard_ms=1e9, budget=100000)


def moves(s):
    return _Search(
        build_board_model(s),
        survival=True,
        lethal_now=False,
        hard_ms=1e9,
        groups=16,
        width=2,
        k_next=4,
        opponent=None,
    )


def aimed(m, iid):
    root = m.root_node()
    body = next(c for c in root.theirs if c["instance_id"] == iid)
    var = next(v for v in m.spells[0].variants if v.reach and not v.chip)
    return root, ((var, body),)


def test_known_response_changes_best_target_but_zero_hand_does_not():
    unprotected = search(state(hand_count=0))
    protected = search(state())
    assert dict(unprotected.best.steps[0].targets)["Measured Bolt"] == "Discard Protector"
    assert dict(protected.best.steps[0].targets)["Measured Bolt"] == "Unprotected threat"
    assert protected.truncated and not protected.exact_first_attack
    assert not protected.only_survivor and not protected.all_dead_at_first
    assert not protected.best.complete


def test_spell_spent_gain_and_discard_preserved_and_protection_expires():
    s = state()
    m = moves(s)
    root, casts = aimed(m, 271)
    mid = m.begin(root, None, casts)
    assert len(mid.responses) == 2
    decline, protect = mid.responses
    assert decline.our_life == protect.our_life == 13
    assert not decline.hand and not protect.hand
    assert protect.opponent_discards == 1 and decline.opponent_discards == 0
    assert 271 in protect.their_tapped
    saved = next(b for b in protect.theirs if b["instance_id"] == 271)
    assert saved["_combat_damage"] == 3 and "indestructible" in saved["keywords"]
    m._end_step(protect)
    saved = next(b for b in protect.theirs if b["instance_id"] == 271)
    assert "indestructible" not in saved["keywords"] and saved["_combat_damage"] == 0
    assert s["battlefield"][2]["oracle_text"] == PROTECT and "keywords" not in s["battlefield"][2]


def test_lifegain_trade_remains_useful_even_when_target_can_survive():
    s = state(gain=8)
    s["battlefield"] = s["battlefield"][:3]
    s["players"][0]["life_total"] = 2
    result = search(s)
    assert result.best.steps[0].casts == ("Measured Bolt",)
    assert result.best.steps[0].opponent_discards == 1
    assert result.best.steps[0].life_after == 5


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: s.update(opponent_hand_count=None),
        lambda s: s["battlefield"][2].update(oracle_text="{1}, " + PROTECT),
        lambda s: s["battlefield"][2].update(oracle_text=PROTECT + " Activate only once each turn."),
        lambda s: s["battlefield"].append(
            card(80, "Lock", "Players cannot activate abilities.", "Enchantment", seat=2)
        ),
        lambda s: s["battlefield"].append(
            card(80, "Discard trigger", "Whenever you discard a card, draw a card.", "Enchantment", seat=2)
        ),
        lambda s: s["battlefield"][2].update(damage=None),
    ],
)
def test_unknown_or_unmodelled_current_response_abstains(mutation):
    s = state()
    mutation(s)
    r = search(s)
    assert r.unsupported and not r.first_action and not r.exact_first_attack


def test_later_or_multi_casts_are_marked_unmodelled_and_never_guarantee_kill():
    from dataclasses import replace

    s = state()
    m = moves(s)
    root, casts = aimed(m, 271)
    assert not m.supported_casts(replace(root, k=1), casts)
    assert m.truncated and not m.exact_first
    assert "Measured Bolt" in m.public_unmodelled
    assert m.choice_caveats
    with pytest.raises(UnsupportedResponse):
        m._public_case(root, casts + casts)


def test_unprotected_target_survives_top_two_target_cap():
    s = state()
    s["battlefield"].append(card(300, "Second protected name", "", "Creature", seat=2, power=4, toughness=2))
    m = moves(s)
    root, casts = aimed(m, 271)
    var = casts[0][0]
    targets = m.targets(var.reach, root.theirs, root.their_tapped, root.ours)
    assert {271, 300, 320} == {c["instance_id"] for c in targets}


def test_negative_counter_controls_do_not_invent_indestructible_against_other_removal():
    s = state()
    model = build_board_model(s)
    target = model.theirs[0]
    assert (
        response(model, card(55, "Exile", "Exile target creature.", "Instant", "{R}"), target, current=True)
        is None
    )
    assert (
        response(
            model,
            card(55, "Shrink", "Target creature gets -3/-3 until end of turn.", "Instant", "{R}"),
            target,
            current=True,
        )
        is None
    )
    assert response(build_board_model(state(hand_count=0)), s["hand"][0], target, current=True) is None


def test_opponent_can_decline_tap_protection_when_death_token_blocks_lethal():
    # Decline: death creates a blocker. Discard: protected body taps, allowing
    # our attacker through. The opponent is not forced to activate protection.
    attacker = card(40, "Attacker", "", "Creature", power=3, toughness=3, turn_entered_battlefield=10)
    s = state(ours=[attacker])
    s["battlefield"] = s["battlefield"][:3] + [attacker]
    s["battlefield"][2]["oracle_text"] += (
        "\nWhen this creature dies, create a 3/3 green Beast creature token."
    )
    s["players"][1]["life_total"] = 3
    m = moves(s)
    root, casts = aimed(m, 271)
    mid = m.begin(root, None, casts)
    chosen = m.finish(mid, ("none", ()))
    outcomes = m.public_responses[m.choice_text(casts)]
    assert outcomes[1].win_at == 1
    assert outcomes[1].history[0].attack == ("Attacker",)  # adaptation after tap
    assert chosen.win_at is None
    assert chosen.history[0].public_response == "opponent declines protection"
    assert chosen.opponent_discards == 0


def test_adaptive_attack_is_maximized_inside_each_response_not_before_it():
    attacker = card(40, "Attacker", "", "Creature", power=2, toughness=2, turn_entered_battlefield=10)
    s = state(ours=[attacker])
    s["battlefield"] = s["battlefield"][:3] + [attacker]
    # Without tap, discard leaves a lethal blocker; decline removes it.
    s["battlefield"][2]["oracle_text"] = PROTECT.replace(" Tap it.", "")
    s["players"][1]["life_total"] = 2
    m = moves(s)
    root, casts = aimed(m, 271)
    mid = m.begin(root, None, casts)
    chosen = m.finish(mid, ("all", (40,)))
    decline, discard = m.public_responses[m.choice_text(casts)]
    assert decline.win_at == 1 and decline.history[0].attack == ("Attacker",)
    assert not discard.history[0].attack
    assert chosen is discard
    assert chosen.history[0].public_response == "opponent discards for indestructible"


def test_one_turn_response_policy_does_not_claim_horizon_minimax_win():
    attacker = card(40, "Attacker", "", "Creature", power=1, toughness=1, turn_entered_battlefield=10)
    s = state(protector_power=0, ours=[attacker])
    s["battlefield"] = s["battlefield"][:3] + [attacker]
    s["players"][1]["life_total"] = 2
    m = moves(s)
    root, casts = aimed(m, 271)
    mid = m.begin(root, None, casts)
    chosen = m.finish(mid, ("none", ()))
    projected = m.line(m.rollout(chosen))
    assert not projected.complete
    assert projected.outcome == "unknown"
    assert projected.outcome_text().startswith("conditional on the one-turn response policy:")
    assert m.truncated and not m.exact_first


@pytest.mark.parametrize("oracle", ["Exile target creature.", "Return target creature to its owner's hand."])
def test_held_exile_and_bounce_do_not_require_protection_response(oracle):
    s = state()
    s["hand"][0]["oracle_text"] = oracle
    m = moves(s)
    root = m.root_node()
    removal, _, _ = m._held(root.hand, (list(m.model.sources_now), 0, ()), root.our_life, root.theirs)
    assert len(removal) == 1 and not m.public_unmodelled


def test_current_empty_hand_does_not_prove_future_held_burn_kills():
    m = moves(state(hand_count=0))
    root = m.root_node()
    removal, _, _ = m._held(root.hand, (list(m.model.sources_now), 0, ()), root.our_life, root.theirs)
    assert not removal and m.public_unmodelled
    root, casts = aimed(m, 271)
    assert m._public_case(root, casts) is None


def test_expiry_preserves_permanent_counter_changes_and_existing_keyword():
    original = dict(instance_id=1, power=4, toughness=4, keywords=["flying", "indestructible"])
    protected = protected_body(original, 3)
    protected.update(power=3, toughness=3, _counter_shift=-1)
    ended = expire(protected)
    assert ended["power"] == ended["toughness"] == 3 and ended["_counter_shift"] == -1
    assert ended["keywords"] == ["flying", "indestructible"] and ended["_combat_damage"] == 0


def test_unmodelled_choices_disable_guard_and_mark_the_spell():
    from arenamcp.line_guard import _usable

    s = state()
    r = search(s)
    assert not _usable(r)
    assert "unmodelled" in unmodelled_effect(s["hand"][0], r) or not r.best.complete


@pytest.mark.parametrize(
    "lock",
    [
        "During your turn, your opponents can't cast spells or activate abilities of artifacts, creatures, or enchantments.",
        "Each player can cast spells and activate abilities only during their own turn.",
    ],
)
def test_coordinated_activation_locks_are_not_called_available(lock):
    s = state()
    s["battlefield"].append(card(900, "Activation restriction", lock, "Enchantment"))
    assert search(s).unsupported


def test_no_life_gain_control_on_ordinary_target_with_empty_opponent_hand():
    s = state(hand_count=0)
    s["battlefield"].append(card(900, "Life lock", "Players can't gain life.", "Enchantment"))
    m = moves(s)
    root, casts = aimed(m, 320)
    mid = m.begin(root, None, casts)
    assert mid.our_life == 10 and casts[0][0].gain == 0


def test_no_favorable_response_cached_when_second_branch_hits_deadline(monkeypatch):
    from arenamcp.line_search_moves import Deadline

    m = moves(state())
    root, casts = aimed(m, 271)
    mid = m.begin(root, None, casts)
    original = m.attack_choices

    def choices(branch):
        if branch.public_response == "opponent discards for indestructible":
            raise Deadline
        return original(branch)

    monkeypatch.setattr(m, "attack_choices", choices)
    with pytest.raises(Deadline):
        m.finish(mid, ("none", ()))
    assert not mid.response_cache and not m.public_responses


def test_public_hand_consumption_changes_transposition_signature_and_value():
    from dataclasses import replace

    from arenamcp.line_search_moves import PUNISH_CARD_VALUE

    m = moves(state())
    root = m.root_node()
    spent = replace(root, opponent_discards=1)
    assert m._sig(root) != m._sig(spent)
    assert m.score(spent)[2] - m.score(root)[2] == pytest.approx(PUNISH_CARD_VALUE)


def test_observed_counters_use_current_toughness_not_printed_base():
    s = state()
    s["battlefield"][2].update(toughness=4, counters={"CounterType_P1P1": 3})
    m = moves(s)
    root = m.root_node()
    var = m.spells[0].plain
    assert 271 not in {
        b["instance_id"] for b in m.targets(var.reach, root.theirs, root.their_tapped, root.ours)
    }


def test_hold_then_burn_does_not_carry_zero_hand_past_a_draw_step():
    from arenamcp.line_search import evaluate_plan

    s = state(hand_count=0)
    result = search(s)
    evaluation = evaluate_plan(
        result,
        [
            {"label": "T", "cast": [], "attack": "none"},
            {"label": "T+1", "cast": ["Measured Bolt (on Discard Protector)"], "attack": "none"},
        ],
    )
    assert evaluation.line is None
    assert any("later-turn" in note for note in evaluation.unmodelled)


def test_empty_hand_now_does_not_cover_unknown_multi_spell_ordering():
    m = moves(state(hand_count=0))
    root, casts = aimed(m, 271)
    assert m._public_case(root, casts) is None
    assert not m.supported_casts(root, casts + casts)
    assert m.public_unmodelled and m.truncated
