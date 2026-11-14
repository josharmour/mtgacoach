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
from arenamcp.decisions import build_pending_decision
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
        self.calls.append({"user": user_message})
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

    # Splitting loses nothing: stable + candidate reproduce the reference.
    assert stable + "\n" + candidates == prepared["deck_reference"]
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
        user_message.find("DECK PLAYBOOK / STRATEGY:"),
        user_message.find("RULES OF THE CARDS SEEN THIS GAME"),
        user_message.find("YOUR BOARD:"),
        user_message.rfind("Form the GAME PLAN"),
    ]
    assert order[0] == 0
    assert -1 not in order[:4], order
    assert order[:4] == sorted(order[:4]), order

    # The reference was regenerated from this call's own zones: its head is
    # byte-identical to what prepare_match_context renders for this state.
    prepared_head, _rules = split_deck_reference(prepare_match_context(deepcopy(state))["deck_reference"])
    assert user_message.startswith(prepared_head.rstrip("\n"))
