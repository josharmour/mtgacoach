"""E2: prompt sections are ordered so the server's prefix cache keeps matching.

Measured 2026-11-10 on the blackwell serving stack (coach -> LiteLLM :8444 ->
vLLM ``glm53-flash-2x-rtxpro6000``): vLLM's block-level prefix cache matches
the longest common leading bytes across calls, so every prompt must lead with
what consecutive calls share and push what changes to the back. Details in
fable-november-reports/E2.md.

Real logged case: match 41b07b15, T19 main-phase typed decision
(``swarm_41b07b15_t19_main1_20261019.json``) — one board, several decisions
in a row, so every prompt after the first should reuse the previous call's
cached prefix. Every prompt here is built exactly as the coach builds it:
``prepare_match_context`` regenerates the deck reference from the fixture's own
``deck_cards`` on each call (no preloaded reference), so the assertions cover
what the coach really sends as cards are seen and as the commander tax grows.
"""

import json
import os
from copy import deepcopy
from unittest.mock import patch

import pytest

from arenamcp.action_planner import (
    AUTOPILOT_SYSTEM_PROMPT,
    ActionPlanner,
    autopilot_system_prompt,
)
from arenamcp.decisions import DecisionOption, PendingDecision, build_pending_decision
from arenamcp.deck_strategy import DeckPlaybook
from arenamcp.game_plan import GamePlanManager
from arenamcp.match_context import (
    prepare_match_context,
    reset_seen_rules,
    split_deck_reference,
    strategic_policy,
)
from arenamcp.situational_rules import SUBJECTS as SITUATIONAL_SUBJECTS
from arenamcp.situational_rules import compose as compose_rules
from deck_fakes import playbook_for
from test_prompt_compaction_20261019 import T19, _Backend, _t19_decision
from test_prompt_compaction_20261019 import _state as _fixture_state

REFERENCE_HEAD_END = "- Card 300: rules"
LIBRARY_GRP_ID = 999999


class _PlanBackend:
    """Records plan calls; accepts the kwargs GamePlanManager passes."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def complete(self, system_prompt, user_message, max_tokens=4096, **kwargs):
        self.calls.append({"system": system_prompt, "user": user_message, **kwargs})
        return json.dumps(
            {
                "win_conditions": ["aggro beatdown"],
                "path": "race for lethal ~T6",
                "threat": "opponent flyers",
                "develop_next": "deploy a 2-drop",
            }
        )


def card_lookup(grp_id):
    return {
        "name": f"Card {grp_id}",
        "type_line": "Creature — Test",
        "mana_cost": "{2}{G}",
        "cmc": 3,
        "power": "2",
        "toughness": "3",
        "oracle_text": f"Rules for card {grp_id}.",
    }


@pytest.fixture(autouse=True)
def _fresh_seen_rules():
    reset_seen_rules()
    yield
    reset_seen_rules()


@pytest.fixture()
def _regenerated_lookup():
    """Resolve grp ids through the test catalog while prepare_match_context runs."""
    with patch("arenamcp.match_context._local_card", lambda gid, _time: card_lookup(gid)):
        yield


def _t19_state():
    """The logged T19 board; its deck reference is regenerated on every call.

    The fixture predates deck-list logging and ships an empty ``deck_cards``,
    so the list is reconstructed from the grp_ids the logged match itself put
    on the battlefield, in hand and in the command zone, plus one card still
    hiding in the library (grp 999999) — the same reconstruction the review of
    c3b8f8d used. Drawing that hidden card later must not change the deck-list
    head; only its newly visible rules line joins the RULES section.
    """
    state = _fixture_state(T19)
    visible = {
        card.get("grp_id")
        for zone in ("battlefield", "hand", "command", "graveyard", "exile", "stack")
        for card in state.get(zone) or []
        if card.get("grp_id")
    }
    state["deck_cards"] = sorted(visible | {LIBRARY_GRP_ID})
    assert "deck_reference" not in state
    return state


def _drawn_from_library(state):
    """A second state where the library card of the reconstructed list was drawn.

    The card is already on ``deck_cards`` (drawing does not change the deck's
    composition), so the deck-list head stays byte-identical; only its newly
    visible rules line is added to the reference.
    """
    grp_id = LIBRARY_GRP_ID
    assert grp_id in state["deck_cards"]
    assert all(card.get("grp_id") != grp_id for card in state["hand"])
    local = next(p["seat_id"] for p in state["players"] if p.get("is_local"))
    drawn = deepcopy(state)
    drawn["hand"] = [
        *drawn["hand"],
        {
            "grp_id": grp_id,
            "instance_id": 990001,
            "name": f"Card {grp_id}",
            "owner_seat_id": local,
            "controller_seat_id": local,
            "type_line": "Creature — Test",
            "mana_cost": "{2}{G}",
            "power": 2,
            "toughness": 3,
            "oracle_text": f"Rules for card {grp_id}.",
        },
    ]
    return drawn, grp_id


def _cast_decision_for(state, grp_id):
    """A pending decision whose only option names ``grp_id`` (bridge metadata).

    Mirrors how Arena offers a cast: ``{"actionType": "ActionType_Cast",
    "grpId": ..., "instanceId": ...}`` (logged form quoted in decisions.py);
    ``build_pending_decision`` copies the action into the option's meta, which
    is exactly where ``_option_grp_ids`` reads reveal candidates from.
    """
    poll = {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "can_pass": True,
        "actions": [{"actionType": "ActionType_Cast", "grpId": grp_id, "instanceId": 990002}],
    }
    return build_pending_decision(poll, resolve_name=lambda grp: f"Card {grp}")


def test_split_deck_reference_keeps_every_byte_of_each_block():
    reference = (
        "DECK REFERENCE (starting composition)\n"
        "20x Card 100 [grp:100] {2}{G} Creature — Test 2/3\n"
        "\n"
        "COMMANDER REFERENCE\n"
        f"{REFERENCE_HEAD_END}\n"
        "\n"
        "RULES OF THE CARDS SEEN THIS GAME\n"
        "- Card 100: Rules for card 100.\n"
        "\n"
        "DECK DECISION RULES FOR THIS WINDOW\n"
        "- Card 300: Rules for card 300."
    )
    head, rules = split_deck_reference(reference)
    # Rejoining the blocks with the separator callers use reproduces the
    # reference byte for byte: reordering must not rewrite a single byte.
    assert "\n\n".join([head, rules]) == reference
    assert head.endswith(REFERENCE_HEAD_END)
    assert rules.startswith("RULES OF THE CARDS SEEN THIS GAME")
    # No RULES header yet (fresh match): everything is head.
    whole, no_rules = split_deck_reference("just the deck list")
    assert (whole, no_rules) == ("just the deck list", "")


def test_prepare_match_context_splits_candidate_rules_without_changing_a_byte(
    _regenerated_lookup,
):
    """The reference splits into stable bytes + this decision's candidate rules.

    A card still hiding in the library is named by this window's options but is
    not yet among the seen cards, so its rule line is a per-decision candidate:
    it must be separated out of ``deck_reference_stable`` (which every call of
    the match shares) without rewriting any other byte of the reference.
    """
    state = _t19_state()

    prepared = prepare_match_context(deepcopy(state), reveal=[LIBRARY_GRP_ID])
    stable = prepared["deck_reference_stable"]
    candidates = prepared["decision_candidate_rules"]

    # Candidate rules are stripped out of deck_reference_stable while keeping its RULES header invariant.
    assert "and the pending decision's candidates" not in stable
    assert "pending decision's candidates" in prepared["deck_reference"]
    # Every line other than that RULES header matches stable + "\n" + candidates.
    stable_lines = stable.splitlines()
    ref_lines = prepared["deck_reference"].splitlines()
    assert len(stable_lines) + len(candidates.splitlines()) == len(ref_lines)
    assert stable + "\n" + candidates == prepared["deck_reference"].replace(
        ", and the pending decision's candidates", ""
    )
    # The unseen card's rule is a candidate, not part of the shared stable text.
    new_rule = f"Rules for card {LIBRARY_GRP_ID}."
    assert f"Card {LIBRARY_GRP_ID} [grp:{LIBRARY_GRP_ID}]" in candidates
    assert new_rule in candidates
    assert new_rule not in stable
    assert candidates.lstrip().startswith(f"Card {LIBRARY_GRP_ID} [grp:")
    # Without a reveal there are no candidates: the whole reference is stable.
    plain = prepare_match_context(deepcopy(state))
    assert plain["decision_candidate_rules"] == ""
    assert plain["deck_reference_stable"] == plain["deck_reference"]
    assert stable_lines[0:] and stable.startswith(plain["deck_reference_stable"].split("\nRULES")[0])
    assert [ln for ln in stable_lines if ln.startswith("RULES")] == [
        ln for ln in plain["deck_reference_stable"].splitlines() if ln.startswith("RULES")
    ]


def test_two_decisions_regenerate_the_reference_and_keep_the_stable_part_first(
    _regenerated_lookup,
):
    """Done-when (§E2): consecutive typed decisions reuse one cached prefix.

    Both prompts are built exactly as the coach builds them: the deck reference
    is regenerated by ``prepare_match_context`` from ``deck_cards`` before each
    call. Between the two decisions a library card of the logged list is drawn,
    so the RULES section grows — and everything ahead of it (deck list,
    commanders, stable deck strategy) must stay byte-identical and sit entirely
    before anything that changed, with the growing RULES section between the
    strategy and the volatile decision text.
    """
    state = _t19_state()
    backend = _Backend()
    planner = ActionPlanner(backend, timeout=15.0, land_drop_first=False)
    planner._deck_strategy_fn = lambda: "Race for lethal with cheap spells; hold removal for flyers."

    planner.plan_decision_options(_t19_decision(state), deepcopy(state))
    assert backend.calls[-1]["class"] == "decision.typed"
    user_1 = backend.calls[-1]["user"]

    drawn, grp_id = _drawn_from_library(state)
    planner.plan_decision_options(_t19_decision(drawn), deepcopy(drawn))
    user_2 = backend.calls[-1]["user"]

    # Order in BOTH prompts: deck reference head first, then the stable deck
    # strategy, then the growing RULES section, then the volatile decision.
    for user in (user_1, user_2):
        order = [
            user.find("DECK REFERENCE"),
            user.find("DECK STRATEGY"),
            user.find("RULES OF THE CARDS SEEN THIS GAME"),
            user.find("PENDING DECISION"),
            user.find("OPTIONS:"),
        ]
        assert order[0] == 0
        assert -1 not in order, order
        assert order == sorted(order), order

    # Everything ahead of the RULES section survived regeneration byte for
    # byte even though a card was drawn between the calls.
    rules_start_1 = user_1.index("RULES OF THE CARDS SEEN THIS GAME")
    rules_start_2 = user_2.index("RULES OF THE CARDS SEEN THIS GAME")
    assert user_1[:rules_start_1] == user_2[:rules_start_2]

    # The newly drawn card's rules line joins the reference's RULES section —
    # after the strategy block and before any volatile text.
    new_rule = f"Rules for card {grp_id}."
    assert new_rule not in user_1
    pending_2 = user_2.index("PENDING DECISION")
    assert new_rule in user_2[rules_start_2:pending_2]

    # The stable strategy block itself is byte-identical at its offset.
    strategy_start_1 = user_1.index("DECK STRATEGY")
    strategy_start_2 = user_2.index("DECK STRATEGY")
    assert user_1[strategy_start_1:rules_start_1] == user_2[strategy_start_2:rules_start_2], (
        "the strategy block ahead of the RULES section must not drift"
    )

    # The two decisions genuinely differ once their content differs.
    assert user_1 != user_2


def test_playbook_tax_and_window_rules_render_after_the_shared_prefix(
    _regenerated_lookup,
):
    """A real DeckPlaybook: its changing lines start after one shared prefix.

    The review of c3b8f8d showed the real ``DeckPlaybook.decision_context``
    embedding changing content — commander tax growing with command-zone casts and
    rules selected per window — inside what earlier prompts treated as stable. Here
    a parsed playbook drives two regenerated decisions whose commander tax moves
    from {0} to {4} and whose window kinds differ; both changes must land AFTER
    everything both calls share.
    """
    book = playbook_for("replay")
    state = _t19_state()
    state["commander_casts"] = {"1001": 0}
    backend = _Backend()
    planner = ActionPlanner(
        backend,
        timeout=15.0,
        land_drop_first=False,
        deck_playbook_fn=lambda: book,
    )

    planner.plan_decision_options(_t19_decision(state), deepcopy(state))
    user_1 = backend.calls[-1]["user"]

    taxed = deepcopy(state)
    taxed["commander_casts"] = {"1001": 2}
    taxed["decision_context"] = {"type": "declare_blockers"}
    planner.plan_decision_options(_t19_decision(taxed), deepcopy(taxed))
    user_2 = backend.calls[-1]["user"]

    core_header = book.decision_context(state)[:60]
    for user in (user_1, user_2):
        order = [
            user.find("DECK REFERENCE"),
            user.find(core_header),
            user.find("RULES OF THE CARDS SEEN THIS GAME"),
            user.find("Live commander recovery cost"),
            user.find("PENDING DECISION"),
        ]
        assert order[0] == 0
        assert -1 not in order, order
        assert order == sorted(order), order

    # The two prompts are byte-identical up to their first difference — which
    # is exactly where the commander tax line starts. Everything before it
    # (deck list, commanders, RULES section, playbook core) is shared.
    tax_index_1 = user_1.index("Live commander recovery cost")
    tax_index_2 = user_2.index("Live commander recovery cost")
    assert user_1[:tax_index_1] == user_2[:tax_index_2]

    # The commander tax grew between the calls ({0} -> {4}) and both tax lines
    # sit after their RULES section — variable content behind stable content.
    rules_start_1 = user_1.index("RULES OF THE CARDS SEEN THIS GAME")
    rules_start_2 = user_2.index("RULES OF THE CARDS SEEN THIS GAME")
    assert "(0 prior command-zone casts)" in user_1[tax_index_1:]
    assert "(2 prior command-zone casts)" in user_2[tax_index_2:]
    assert tax_index_1 > rules_start_1 and tax_index_2 > rules_start_2

    # No information deleted: every line of the composed decision context
    # appears somewhere in its prompt.
    context_lines = book.decision_context(taxed).split("\n")
    missing = [line for line in context_lines if line not in user_2]
    assert not missing, missing


def test_drawn_card_keeps_the_whole_playbook_prefix_intact(_regenerated_lookup):
    """Draw + window change + tax growth: the shared prefix still reaches RULES.

    The worst realistic case for the old order: between two decisions a card
    is drawn (RULES section grows), the window kind changes and the commander
    tax grows. All three used to invalidate the prompt from the reference's
    RULES section onward, erasing the playbook. With the playbook core ahead
    of that section and its variable lines behind it, everything up to the
    RULES section — deck list, commanders, playbook core — stays byte-identical.
    """
    book = playbook_for("replay")
    state = _t19_state()
    state["commander_casts"] = {"1001": 0}
    backend = _Backend()
    planner = ActionPlanner(
        backend,
        timeout=15.0,
        land_drop_first=False,
        deck_playbook_fn=lambda: book,
    )

    planner.plan_decision_options(_t19_decision(state), deepcopy(state))
    user_1 = backend.calls[-1]["user"]

    drawn, grp_id = _drawn_from_library(state)
    drawn["commander_casts"] = {"1001": 2}
    drawn["decision_context"] = {"type": "declare_blockers"}
    planner.plan_decision_options(_t19_decision(drawn), deepcopy(drawn))
    user_2 = backend.calls[-1]["user"]

    rules_start_1 = user_1.index("RULES OF THE CARDS SEEN THIS GAME")
    rules_start_2 = user_2.index("RULES OF THE CARDS SEEN THIS GAME")
    assert user_1[:rules_start_1] == user_2[:rules_start_2], (
        "drawing a card must not erase the playbook core prefix"
    )
    # The drawn card's rule and the grown tax both land after that prefix.
    assert f"Rules for card {grp_id}." in user_2[rules_start_2:]
    assert "(2 prior command-zone casts)" in user_2[rules_start_2:]
    assert "(0 prior command-zone casts)" in user_1[rules_start_1:]
    # No information deleted between the two compositions.
    for context_state in (state, drawn):
        missing = [
            line
            for line in book.decision_context(context_state).split("\n")
            if line not in (user_1 if context_state is state else user_2)
        ]
        assert not missing, missing


def test_opaque_playbook_context_is_never_split_by_label_text():
    for context in (
        "",
        "Primary plan: value\n  Unless: important qualifier",
        "RULE r1\nPrimary plan: continuation",
        "Future wording",
    ):
        assert ActionPlanner._split_playbook_context(context) == (context, "")


@pytest.mark.parametrize(
    "continuation", ["RULE r1: keep this qualifier", "Mechanism m1: qualifier", "  Unless: lethal"]
)
def test_real_multiline_plan_fields_are_atomic(continuation):
    book = playbook_for("replay")
    data = deepcopy(book.data)
    data["primary_plan"] += "\n" + continuation
    book = DeckPlaybook.parse(json.dumps(data), book.catalog, book.commanders, book.identity)
    original, core, tail = book.decision_context_parts(_t19_state())
    assert original == book.decision_context(_t19_state())
    assert "Primary plan: " + data["primary_plan"] in core
    assert continuation not in tail


def test_real_multiline_rule_field_cannot_escape_into_core():
    book = playbook_for("replay")
    data = deepcopy(book.data)
    rule = data["decision_rules"][0]
    # Select all rules via an otherwise unclassified request.
    rule["when"] += "\nPrimary plan: this is still the rule condition"
    book = DeckPlaybook.parse(json.dumps(data), book.catalog, book.commanders, book.identity)
    original, core, tail = book.decision_context_parts({})
    assert original == book.decision_context({})
    assert "Primary plan: this is still the rule condition" not in core
    assert rule["when"] in tail


@pytest.mark.parametrize(
    "continuation",
    [
        "Develop remaining resources when needed.",
        "  Unless: we need a blocker, keep this qualifier with the primary plan.",
        "RULE r99: this is still primary-plan text.",
        "Mechanism m99: this is still primary-plan text.",
    ],
)
def test_multiline_primary_plan_keeps_variable_tax_behind_the_shared_prefix(
    _regenerated_lookup,
    continuation,
):
    """A schema-valid multiline primary plan must not freeze the tax into the core.

    Logged case: match 41b07b15 T19 (the same fixture every test here uses),
    with the playbook's ``primary_plan`` extended by one wrapped line. The
    playbook schema stores plans as free text (``text()`` in
    ``playbook_response_format``) and ``DeckPlaybook.parse`` accepts the
    multiline value, so this is supported input the coach can really send —
    no logged bundle happens to contain one yet, which is disclosed in
    fable-november-reports/E2.md. Before the fix the wrapped line made
    ``_split_playbook_context`` give up and return the whole block unsplit,
    so the growing commander tax rode inside the supposedly stable core and
    every new cast erased the shared prefix before RULES.
    """
    book = playbook_for("replay")
    data = deepcopy(book.data)
    data["primary_plan"] += "\n" + continuation
    multiline_book = DeckPlaybook.parse(json.dumps(data), book.catalog, book.commanders, book.identity)

    state = _t19_state()
    state["commander_casts"] = {"1001": 0}
    backend = _Backend()
    planner = ActionPlanner(
        backend,
        timeout=15.0,
        land_drop_first=False,
        deck_playbook_fn=lambda: multiline_book,
    )

    planner.plan_decision_options(_t19_decision(state), deepcopy(state))
    user_1 = backend.calls[-1]["user"]

    taxed = deepcopy(state)
    taxed["commander_casts"] = {"1001": 2}
    taxed["decision_context"] = {"type": "declare_blockers"}
    planner.plan_decision_options(_t19_decision(taxed), deepcopy(taxed))
    user_2 = backend.calls[-1]["user"]

    # The wrapped plan line is present in both prompts (nothing dropped).
    for user in (user_1, user_2):
        assert "Primary plan: " + data["primary_plan"] in user

    # The shared prefix still reaches RULES even though the tax grew {0}->{4}.
    tax_index_1 = user_1.index("Live commander recovery cost")
    tax_index_2 = user_2.index("Live commander recovery cost")
    rules_start_1 = user_1.index("RULES OF THE CARDS SEEN THIS GAME")
    rules_start_2 = user_2.index("RULES OF THE CARDS SEEN THIS GAME")
    assert user_1[:tax_index_1] == user_2[:tax_index_2]
    assert tax_index_1 > rules_start_1 and tax_index_2 > rules_start_2
    assert "(0 prior command-zone casts)" in user_1[tax_index_1:]
    assert "(2 prior command-zone casts)" in user_2[tax_index_2:]

    # Information-preserving: every line of the composed context appears.
    context_lines = multiline_book.decision_context(taxed).split("\n")
    missing = [line for line in context_lines if line not in user_2]
    assert not missing, missing


def test_candidate_rules_sit_between_the_strategy_and_the_volatile_text(
    _regenerated_lookup,
):
    """A decision revealing an unseen card quotes its rule late, not early.

    The library card is named by this window's cast option but has never been
    seen, so its rule cannot join the shared reference head; it must ride with
    this decision — after the stable strategy block, before volatile text — so
    consecutive calls still share everything up to it.
    """
    state = _t19_state()
    backend = _Backend()
    planner = ActionPlanner(backend, timeout=15.0, land_drop_first=False)
    planner._deck_strategy_fn = lambda: "Race for lethal with cheap spells; hold removal for flyers."

    planner.plan_decision_options(_cast_decision_for(state, LIBRARY_GRP_ID), deepcopy(state))
    user_message = backend.calls[-1]["user"]

    strategy_start = user_message.index("DECK STRATEGY")
    pending_start = user_message.index("PENDING DECISION")
    new_rule = f"Rules for card {LIBRARY_GRP_ID}."
    # Not in the shared reference head...
    assert new_rule not in user_message[:strategy_start]
    # ...but quoted between the strategy block and the volatile decision text.
    segment = user_message[strategy_start:pending_start]
    assert new_rule in segment


def test_system_prompts_lead_with_the_shared_strategic_policy():
    """Typed-decision and autopilot system prompts share one leading prefix.

    The two system prompts previously put their distinct rule text first and
    appended ``strategic_policy``, so consecutive decision/plan calls shared a
    near-empty system prefix. Leading with ``strategic_policy`` makes both
    prompts begin with identical bytes; each prompt keeps exactly its former
    lines (pure reorder).
    """
    policy = strategic_policy(SITUATIONAL_SUBJECTS)
    decision_prompt = ActionPlanner._decision_system_prompt()
    autopilot_prompt = autopilot_system_prompt()

    assert len(policy) > 2000
    assert decision_prompt.startswith(policy)
    assert autopilot_prompt.startswith(policy)
    # They diverge only after the shared policy — nothing of one prompt's
    # distinctive tail leaks backwards into what both share.
    assert os.path.commonprefix([decision_prompt, autopilot_prompt]).startswith(policy)

    # Pure reorder proof for each prompt: same multiset of lines as its base
    # composition (distinct text first, policy last).
    base_decision_tail = compose_rules(ActionPlanner._DECISION_SYSTEM_PARTS, SITUATIONAL_SUBJECTS)
    assert sorted(decision_prompt.splitlines()) == sorted((base_decision_tail + "\n" + policy).splitlines())
    policy_lines = {line.strip() for line in policy.splitlines() if line.strip()}
    base_autopilot_tail = "\n".join(
        line for line in AUTOPILOT_SYSTEM_PROMPT.splitlines() if line.strip() not in policy_lines
    )
    assert sorted(autopilot_prompt.splitlines()) == sorted((base_autopilot_tail + "\n" + policy).splitlines())


def test_plan_prompt_orders_head_seed_rules_board(_regenerated_lookup):
    """Plan calls follow the same most-shared-to-least-shared order."""
    be = _PlanBackend()
    mgr = GamePlanManager(be)
    mgr._seed = "Race for lethal with cheap spells; hold removal for flyers."
    state = _t19_state()
    plan = mgr.maybe_reform(deepcopy(state))
    assert plan is not None
    assert be.calls, "the first call must form the plan"
    user_message = be.calls[0]["user"]

    order = [
        user_message.find("DECK REFERENCE"),
        user_message.find("DECK STRATEGY:"),
        user_message.find("RULES OF THE CARDS SEEN THIS GAME"),
        user_message.find("YOUR BOARD:"),
        user_message.rfind("Form the GAME PLAN"),
    ]
    assert order[0] == 0
    assert -1 not in order[:4], order
    assert order[:4] == sorted(order[:4]), order

    # The reference was regenerated from this call's own zones: its head is
    # byte-identical to what prepare_match_context renders for this state.
    prepared_head, _rules = split_deck_reference(
        prepare_match_context(
            deepcopy(state), compact_decision=True, compact_starting_deck=True, source_ids_complete=True
        )["deck_reference"]
    )
    assert user_message.startswith(prepared_head.rstrip("\n"))


class _FullCaptureBackend:
    """Records both system prompt and user message for prefix-cache verification."""

    def __init__(self, reply: str, model: str = "other") -> None:
        self.reply = reply
        self.model = model
        self.calls: list[dict] = []

    def complete(self, system_prompt, user_message, max_tokens=4096, **kwargs):
        self.calls.append(
            {
                "system": system_prompt,
                "user": user_message,
                "class": kwargs.get("call_class"),
                "max_tokens": max_tokens,
            }
        )
        return self.reply


def test_nn1_multi_turn_transitions_keep_system_and_shared_prefix_identical(monkeypatch):
    """NN1: multi-turn board/menu transitions never mutate system_prompt or the leading prefix."""
    catalog = {
        101: {
            "name": "Fixture Vanguard",
            "type_line": "Creature — Human Soldier",
            "mana_cost": "{1}{W}",
            "cmc": 2,
            "power": "2",
            "toughness": "2",
            "oracle_text": "Vigilance",
        },
        102: {
            "name": "Fixture Courier",
            "type_line": "Creature — Elf Scout",
            "mana_cost": "{2}{G}",
            "cmc": 3,
            "power": "3",
            "toughness": "2",
            "oracle_text": "When this creature enters, gain 2 life.",
        },
        103: {
            "name": "Fixture Library Card",
            "type_line": "Creature — Beast",
            "mana_cost": "{3}{G}",
            "cmc": 4,
            "power": "4",
            "toughness": "4",
            "oracle_text": "Trample",
        },
    }
    monkeypatch.setattr("arenamcp.match_context._local_card", lambda gid, _epoch: catalog[gid])
    monkeypatch.setattr("arenamcp.card_facts.client.facts_for", lambda *args, **kwargs: {})

    def base_state():
        return {
            "match_id": "nn1-multi-turn",
            "local_seat_id": 1,
            "players": [
                {"seat_id": 1, "is_local": True, "life_total": 20},
                {"seat_id": 2, "is_local": False, "life_total": 20},
            ],
            "turn": {"turn_number": 3, "active_player": 1, "phase": "Phase_Main1"},
            "deck_cards": [101, 102, 103],
            "hand": [
                {
                    **catalog[101],
                    "grp_id": 101,
                    "instance_id": 11,
                    "owner_seat_id": 1,
                    "controller_seat_id": 1,
                }
            ],
            "battlefield": [],
            "graveyard": [],
            "exile": [],
            "stack": [],
            "zones": {"library_count": 2, "opponent_hand_count": 5},
        }

    backend = _FullCaptureBackend('{"option_ids": ["pass"], "reasoning": "hold"}')
    planner = ActionPlanner(backend, timeout=15.0, land_drop_first=False)
    planner._deck_strategy_fn = lambda: "Curve out cleanly and protect key attackers."

    # 1. Turn 3 ActionsAvailable (already-seen hand card in reveal must not churn RULES header)
    s1 = base_state()
    d1 = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "ActionsAvailable",
            "can_pass": True,
            "actions": [
                {"actionType": "ActionType_Cast", "grpId": 101, "instanceId": 11, "hasAutoTap": True}
            ],
        },
        resolve_name=lambda gid: catalog.get(gid, {}).get("name", ""),
    )
    planner._llm_decision_options(d1, deepcopy(s1))
    call_1 = backend.calls[-1]
    assert "and the pending decision's candidates" not in call_1["user"]

    # 2. Turn 4 ActionsAvailable after drawing 102
    s2 = base_state()
    s2["turn"]["turn_number"] = 4
    s2["hand"].append(
        {**catalog[102], "grp_id": 102, "instance_id": 12, "owner_seat_id": 1, "controller_seat_id": 1}
    )
    planner._llm_decision_options(d1, deepcopy(s2))
    call_2 = backend.calls[-1]

    # 3. Turn 5 SelectTargets with Vehicle, Equipment, and Strixhaven permanent entering battlefield
    s3 = deepcopy(s2)
    s3["turn"]["turn_number"] = 5
    s3["battlefield"] = [
        {
            "grp_id": 501,
            "instance_id": 51,
            "name": "Borrowed Skiff",
            "type_line": "Artifact — Vehicle",
            "oracle_text": "Flying\nCrew 2",
            "owner_seat_id": 2,
            "controller_seat_id": 1,
        },
        {
            "grp_id": 502,
            "instance_id": 52,
            "name": "Borrowed Blade",
            "type_line": "Artifact — Equipment",
            "oracle_text": "Equipped creature gets +2/+0 and haste.\nEquip {2}",
            "owner_seat_id": 2,
            "controller_seat_id": 1,
        },
        {
            "grp_id": 503,
            "instance_id": 53,
            "name": "Borrowed Adept",
            "type_line": "Creature — Wizard",
            "oracle_text": "Whenever you cast an instant or sorcery spell, increment.",
            "owner_seat_id": 2,
            "controller_seat_id": 1,
        },
    ]
    d3 = PendingDecision(
        (5, 1),
        "SelectTargets",
        (DecisionOption("tgt:51", "Borrowed Skiff [id:51]", meta={"targetId": 51}),),
    )
    planner._llm_decision_options(d3, deepcopy(s3))
    call_3 = backend.calls[-1]

    # 4. Turn 6 those permanents leave battlefield and a Search menu opens for already-seen 102
    s4 = deepcopy(s2)
    s4["turn"]["turn_number"] = 6
    s4["graveyard"] = deepcopy(s3["battlefield"])
    d4_seen = PendingDecision(
        (6, 1),
        "Search",
        (DecisionOption("sel:102", "Fixture Courier", meta={"grpId": 102}),),
    )
    planner._llm_decision_options(d4_seen, deepcopy(s4))
    call_4 = backend.calls[-1]

    # 5. Turn 6 Search menu offering unseen library card 103
    d5_unseen = PendingDecision(
        (6, 2),
        "Search",
        (DecisionOption("sel:103", "Fixture Library Card", meta={"grpId": 103}),),
    )
    planner._llm_decision_options(d5_unseen, deepcopy(s4))
    call_5 = backend.calls[-1]

    # system_prompt is 100% byte-identical across all 5 calls and includes universal tactical rules
    systems = [c["system"] for c in (call_1, call_2, call_3, call_4, call_5)]
    assert len(set(systems)) == 1
    assert "TEMPORARY EFFECTS:" in call_1["system"]
    assert "Prefer establishing a real engine" in call_1["system"]
    assert "DECISION OUTPUT FORMAT:" in call_1["user"]

    # (reference_head, playbook_core, seen_rules) up to newly drawn card 102 is byte-identical
    line_101_end_1 = call_1["user"].index("\n\nDECISION OUTPUT FORMAT:")
    line_101_end_2 = call_2["user"].index("\nFixture Courier [grp:102]")
    assert call_1["user"][:line_101_end_1] == call_2["user"][:line_101_end_2]

    # Once 102 is seen, calls 2, 3, and 4 share the entire prefix through seen_rules + DECISION OUTPUT FORMAT
    pending_2 = call_2["user"].index("PENDING DECISION")
    assert call_2["user"][:pending_2] == call_3["user"][: call_3["user"].index("SITUATIONAL RULES:")]
    assert call_2["user"][:pending_2] == call_4["user"][: call_4["user"].index("SITUATIONAL RULES:")]

    # Search with unseen library candidate 103 STILL shares the exact invariant prefix
    # all the way through the RULES header and seen cards (up to DECISION OUTPUT FORMAT).
    dec_start_2 = call_2["user"].index("\n\nDECISION OUTPUT FORMAT:")
    dec_start_5 = call_5["user"].index("\n\nDECISION OUTPUT FORMAT:")
    assert call_2["user"][:dec_start_2] == call_5["user"][:dec_start_5]
    assert "RULES OF THE CARDS SEEN THIS GAME" in call_5["user"][:dec_start_5]
    assert (
        "Fixture Library Card [grp:103]"
        in call_5["user"][
            call_5["user"].index("SITUATIONAL RULES:") : call_5["user"].index("PENDING DECISION")
        ]
    )

    # Every visible subject's rule still appears in the prompt when active
    assert "Crew activates a Vehicle" in call_3["user"]
    assert "For haste-granting equipment" in call_3["user"]
    assert "For tutors and library searches" in call_4["user"]
    assert "For library searches" in call_4["user"]
    assert "For library searches" in call_5["user"]


@pytest.mark.parametrize("use_playbook", [False, True])
def test_nn1_decision_and_game_plan_share_system_and_leading_user_prefix(monkeypatch, use_playbook):
    """NN1: decision.typed, decision.plan, decision.pay, and background.game_plan share one prefix."""
    book = playbook_for("replay")
    catalog = deepcopy(book.catalog)
    catalog[1003] = {
        "name": "Fixture Scout",
        "mana_cost": "{1}{G}",
        "type_line": "Creature — Elf Scout",
        "cmc": 2,
        "power": "2",
        "toughness": "1",
        "oracle_text": "Vigilance\nWhen this creature enters, scry 1.",
    }
    monkeypatch.setattr("arenamcp.match_context._local_card", lambda gid, _epoch: catalog[gid])
    monkeypatch.setattr("arenamcp.card_facts.client.facts_for", lambda *args, **kwargs: {})

    state = {
        "match_id": f"nn1-cross-lane-{use_playbook}",
        "local_seat_id": 1,
        "commander_grp_ids": [1001],
        "deck_cards": [1002] * 10 + [1003],
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20, "commander_ids": [50]},
            {"seat_id": 2, "is_local": False, "life_total": 20},
        ],
        "turn": {"turn_number": 4, "active_player": 1, "phase": "Phase_Main1"},
        "hand": [
            {**catalog[1003], "grp_id": 1003, "instance_id": 13, "owner_seat_id": 1, "controller_seat_id": 1}
        ],
        "battlefield": [
            {**catalog[1002], "grp_id": 1002, "instance_id": 21, "owner_seat_id": 1, "controller_seat_id": 1},
            {**catalog[1002], "grp_id": 1002, "instance_id": 22, "owner_seat_id": 1, "controller_seat_id": 1},
        ],
        "command": [
            {**catalog[1001], "grp_id": 1001, "instance_id": 50, "owner_seat_id": 1, "controller_seat_id": 1}
        ],
        "graveyard": [],
        "exile": [],
        "stack": [],
        "zones": {"library_count": 8, "opponent_hand_count": 5},
        "decision_context": {"type": "actions_available"},
    }

    typed_be = _FullCaptureBackend('{"option_ids": ["pass"], "reasoning": "hold"}')
    plan_be = _FullCaptureBackend('{"actions": [{"pick": 1}]}')
    pay_be = _FullCaptureBackend('{"pay": false, "reasoning": "hold mana"}')
    plan_json = json.dumps(
        {
            "role": "aggressor",
            "win_conditions": ["Deploy Fixture Scout"],
            "path": "Develop scout and commander",
            "threat": "opposing removal",
            "develop_next": "cast Fixture Scout",
            "turns": [{"turn": "T", "land": "", "cast": ["Fixture Scout"], "attack": "none", "hold": ""}],
            "switch_if": ["opponent threatens lethal"],
        }
    )
    compact_gp_be = _FullCaptureBackend(plan_json, model="current")
    full_gp_be = _FullCaptureBackend(plan_json, model="other")

    if use_playbook:
        typed_planner = ActionPlanner(
            typed_be, timeout=15.0, land_drop_first=False, deck_playbook_fn=lambda: book
        )
        plan_planner = ActionPlanner(
            plan_be, timeout=15.0, land_drop_first=False, deck_playbook_fn=lambda: book
        )
        pay_planner = ActionPlanner(
            pay_be, timeout=15.0, land_drop_first=False, deck_playbook_fn=lambda: book
        )
        compact_mgr = GamePlanManager(compact_gp_be, separate_strategy_planning=True)
        compact_mgr.set_playbook_source(lambda: book)
        full_mgr = GamePlanManager(full_gp_be, separate_strategy_planning=True)
        full_mgr.set_playbook_source(lambda: book)
    else:
        strategy = "Develop scouts early, protect the commander, and pressure life total."
        typed_planner = ActionPlanner(
            typed_be, timeout=15.0, land_drop_first=False, deck_strategy_fn=lambda: strategy
        )
        plan_planner = ActionPlanner(
            plan_be, timeout=15.0, land_drop_first=False, deck_strategy_fn=lambda: strategy
        )
        pay_planner = ActionPlanner(
            pay_be, timeout=15.0, land_drop_first=False, deck_strategy_fn=lambda: strategy
        )
        compact_mgr = GamePlanManager(compact_gp_be, separate_strategy_planning=True)
        compact_mgr.seed(strategy)
        full_mgr = GamePlanManager(full_gp_be, separate_strategy_planning=True)
        full_mgr.seed(strategy)

    decision = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "ActionsAvailable",
            "can_pass": True,
            "actions": [
                {"actionType": "ActionType_Cast", "grpId": 1003, "instanceId": 13, "hasAutoTap": True}
            ],
        },
        resolve_name=lambda gid: catalog.get(gid, {}).get("name", ""),
    )

    typed_planner._llm_decision_options(decision, deepcopy(state))
    plan_planner.plan_actions(deepcopy(state), "new_turn", ["Cast Fixture Scout [OK]", "Pass"])
    pay_planner.plan_pay_or_decline("Ward Tax", "Pay {1} or counter spell.", deepcopy(state))
    assert compact_mgr.maybe_reform(deepcopy(state)) is not None
    assert full_mgr.maybe_reform(deepcopy(state)) is not None

    calls = [
        typed_be.calls[-1],
        plan_be.calls[-1],
        pay_be.calls[-1],
        compact_gp_be.calls[-1],
        full_gp_be.calls[-1],
    ]
    assert len({c["system"] for c in calls}) == 1
    assert "For tutors and library searches" in calls[0]["system"]

    # Compute exact expected shared prefix: reference_head + playbook_core + seen_rules
    prepared = prepare_match_context(
        deepcopy(state), compact_decision=True, compact_starting_deck=True, source_ids_complete=True
    )
    head, rules = split_deck_reference(prepared["deck_reference_stable"])
    if use_playbook:
        fields, core = DeckPlaybook._decision_context_lines(book, state, request_type=None)
        playbook_core = "\n".join(f for i, f in enumerate(fields) if i in core)
    else:
        playbook_core = (
            "DECK STRATEGY:\nDevelop scouts early, protect the commander, and pressure life total."
        )
    expected_prefix = "\n\n".join((head, playbook_core, rules))
    assert "DECK REFERENCE" in expected_prefix
    assert "RULES OF THE CARDS SEEN THIS GAME" in expected_prefix
    for c in calls:
        assert c["user"].startswith(expected_prefix), f"prefix mismatch for {c['class']}"


def test_nn1_opponent_vehicle_and_strixhaven_keep_rules_without_breaking_prefix(monkeypatch):
    """Opposite control: out-of-deck dynamic cards add situational rules without changing the shared prefix."""
    catalog = {
        101: {
            "name": "Fixture Bear",
            "type_line": "Creature — Bear",
            "mana_cost": "{1}{G}",
            "cmc": 2,
            "power": "2",
            "toughness": "2",
            "oracle_text": "",
        },
        102: {
            "name": "Forest",
            "type_line": "Basic Land — Forest",
            "mana_cost": "",
            "cmc": 0,
            "oracle_text": "({T}: Add {G}.)",
        },
    }
    monkeypatch.setattr("arenamcp.match_context._local_card", lambda gid, _epoch: catalog[gid])
    monkeypatch.setattr("arenamcp.card_facts.client.facts_for", lambda *args, **kwargs: {})

    base = {
        "match_id": "nn1-opp-control",
        "local_seat_id": 1,
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20},
            {"seat_id": 2, "is_local": False, "life_total": 20},
        ],
        "turn": {"turn_number": 3, "active_player": 1, "phase": "Phase_Main1"},
        "deck_cards": [101, 102],
        "hand": [
            {**catalog[101], "grp_id": 101, "instance_id": 11, "owner_seat_id": 1, "controller_seat_id": 1}
        ],
        "battlefield": [],
        "graveyard": [],
        "exile": [],
        "stack": [],
        "zones": {"library_count": 1, "opponent_hand_count": 5},
    }
    decision = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "ActionsAvailable",
            "can_pass": True,
            "actions": [
                {"actionType": "ActionType_Cast", "grpId": 101, "instanceId": 11, "hasAutoTap": True}
            ],
        },
        resolve_name=lambda gid: catalog.get(gid, {}).get("name", ""),
    )

    backend = _FullCaptureBackend('{"option_ids": ["pass"], "reasoning": "hold"}')
    planner = ActionPlanner(backend, timeout=15.0, land_drop_first=False)
    planner._deck_strategy_fn = lambda: "Play bears and hold interaction."
    planner._llm_decision_options(decision, deepcopy(base))
    sys_base, user_base = backend.calls[-1]["system"], backend.calls[-1]["user"]

    # Opponent plays a Strixhaven card and a Vehicle (not in our deck).
    # Also model gaining temporary control of an opponent's Vehicle (owner=2, controller=1)
    # so crew rules are active for us while still outside our deck.
    opp_cards = deepcopy(base)
    opp_cards["battlefield"] = [
        {
            "grp_id": 801,
            "instance_id": 81,
            "name": "Opposing Initiate",
            "type_line": "Creature — Sorcerer",
            "oracle_text": "This creature enters prepared.",
            "owner_seat_id": 2,
            "controller_seat_id": 2,
        },
        {
            "grp_id": 802,
            "instance_id": 82,
            "name": "Borrowed Chariot",
            "type_line": "Artifact — Vehicle",
            "oracle_text": "Flying\nCrew 2",
            "owner_seat_id": 2,
            "controller_seat_id": 1,
        },
    ]
    from arenamcp.situational_rules import deck_subjects, dynamic_subjects

    match_sub = deck_subjects(opp_cards)
    assert match_sub == frozenset()
    dyn_sub = dynamic_subjects(opp_cards, decision, match_subjects=match_sub)
    assert {"set_mechanics", "vehicle", "animation"}.issubset(dyn_sub)

    planner._llm_decision_options(decision, deepcopy(opp_cards))
    sys_opp, user_opp = backend.calls[-1]["system"], backend.calls[-1]["user"]

    assert sys_opp == sys_base
    shared_prefix = user_base[: user_base.index("PENDING DECISION")]
    assert user_opp.startswith(shared_prefix)
    assert "Crew activates a Vehicle" in user_opp[len(shared_prefix) :]
    assert "Crew activates a Vehicle" not in shared_prefix
