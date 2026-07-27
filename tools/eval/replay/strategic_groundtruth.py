"""Ground-truth extractor: EVERY strategic decision in an MTGA ``.rply`` replay.

Why this module exists
----------------------
``menu_groundtruth.py`` extracts one decision per turn: the *first* priority
window of the player's own precombat main phase. That scope was correct for
what it was built for — measuring a 17lands-synthesised menu against a real
one, where only that window is reconstructable from turn-granular data — but
it is the wrong scope for training. Measured on the owner's 104-replay
corpus it yields 665 records of which **447 (67%) are land drops**, and a
model trained on it learned the reflex rather than the game: the tuned LoRA
scored 13/63 on strategic decisions, exactly the same as the untuned base,
while playing the first land in 84% of strategic positions (base: 60%).

The decisions were not missing from the replays. They were being discarded
by scope. The same 104 files contain 2,418 ``ActionsAvailableReq`` (only 665
of which are start-of-main-1), 595 ``DeclareAttackersReq``, 179
``DeclareBlockersReq``, 334 ``SelectNReq``, 208 ``PayCostsReq``, 166
``SearchReq`` and 66,638 ``SelectTargetsReq``.

This module walks the same replays and emits a decision for **every priority
window and every request type it can faithfully reconstruct**, then applies
an explicit strategic filter and reports the funnel at each stage. It does
not modify ``menu_groundtruth.py`` — the existing gate corpus keeps building
exactly as before.

What counts as a decision here
------------------------------
=========================  ==================================================
Request                    Menu / label
=========================  ==================================================
``ActionsAvailable``       verbatim ``actionsAvailableReq.actions[]``; label
                           is the human's ``performActionResp``. Every phase,
                           every priority window, own turn and opponent's.
``DeclareAttackers``       one row per ``qualifiedAttackers[]`` entry plus
                           "declare no attackers"; label is the human's
                           ``declareAttackersResp.selectedAttackers``.
``DeclareBlockers``        one row per legal (blocker, attacker) pair plus
                           "decline to block"; label is the human's
                           ``declareBlockersResp.selectedBlockers``, or the
                           decline when the episode committed with no
                           assignment.
``SelectTargets``          one row per candidate in a single-slot,
                           ``maxTargets==1`` request; label is
                           ``selectTargetsResp.target.targets[0]``.
``SelectN``                one row per selectable id when ``maxSel==1``;
                           label is ``selectNResp.ids[0]``.
``Search``                 one row per ``itemsSought`` entry (plus fail-to-find
                           when allowed) when ``maxFind==1``; label is
                           ``searchResp.itemsFound[0]``.
``PayCosts``               rows from ``payCostsReq.paymentActions.actions``.
                           These are all ``ActionType_Activate_Mana`` — mana
                           plumbing, not strategy — so the strategic filter
                           removes them. Extracted anyway so the funnel says
                           so out loud instead of the corpus quietly omitting
                           a request type.
=========================  ==================================================

``MulliganReq`` is deliberately **not** extracted. Mulligans are excluded from
this corpus by design.

Wire-protocol facts this depends on (measured on the corpus, 2026-07-26)
-----------------------------------------------------------------------
* ``declareAttackersReq.attackers[]`` is **NOT** the player's chosen set. It
  is identical to ``qualifiedAttackers[]`` in 307 of 307 attack episodes and
  never changes within an episode. ``decisions.py::_extract_declare_attackers``
  reads it as the player's answer and is wrong. The answer lives in the
  client's ``DeclareAttackersResp``: either ``autoDeclare`` (attack with
  everything — 182 cases) or an explicit ``selectedAttackers`` list (92).
* ``declareBlockersResp`` carries exactly one blocker per message (55/55), so
  a multi-block turn is several messages inside one episode.
* 63,895 of 66,607 ``SelectTargetsReq`` slots hold a single candidate — a
  forced target, not a decision — and 2,386 of the multi-candidate ones are
  a client retry loop (``SubmitTargetsReq`` -> ``IllegalRequest`` -> re-issue)
  inside three Brawl replays. Requiring a real ``SelectTargetsResp`` removes
  the loop; the strategic filter removes the forced ones.

Ambiguity is dropped, never guessed
-----------------------------------
Only single-answer decisions are emitted, because the gate scores one
``{"pick": N}``. An attack that declared three creatures, a turn that blocked
with two, a ``SelectN`` with ``maxSel > 1`` — each has several correct
answers, none of which is "the" answer, so they are dropped and counted
(``multi_answer_ambiguous``) rather than collapsed onto an arbitrary pick.

Output
------
The **same record schema** ``tools/training/gate_play_decisions.py`` already
consumes, so the file is a drop-in superset of
``replay_menu_groundtruth.jsonl`` (it contains those decisions too — see
``--main1-only`` for the compatibility check). Added on top, all ignored by
older consumers:

  ``request_type``, ``decision_kind``, ``production_trigger``,
  ``strategic`` (verdict + option-class counts), ``decision_context``,
  and per-entry ``menu_text`` / ``equiv_key``.

Usage
-----
    python -m tools.eval.replay.strategic_groundtruth \
        --replay-dir /path/to/StreamingAssets/Tests \
        --out tools/eval/data/replay_strategic_groundtruth.jsonl \
        --summary-out tools/eval/data/replay_strategic_summary.json

CPU only. No network beyond the card database's own local caches.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from .decisions import pair_request_response  # noqa: E402
from .menu_groundtruth import (  # noqa: E402
    MANA_LEVEL_ACTION_TYPES,
    _battlefield_split,
    _iter_state_messages,
    card_facts,
    format_gre_mana_cost,
    gre_mana_value,
    menu_key,
    normalize_menu,
    resolve_pick,
)
from .reader import ReplayMessage, parse_replay_path  # noqa: E402
from .state import (  # noqa: E402
    GameStateSnapshot,
    LocalSeatUndetermined,
    _apply_state_message,
    opponent_seat,
    resolve_local_seat,
)

DEFAULT_OUT = REPO / "tools" / "eval" / "data" / "replay_strategic_groundtruth.jsonl"

# ---------------------------------------------------------------------------
# Synthetic action types
#
# ``ActionsAvailable`` menus carry MTGA's own ``ActionType_*`` strings. The
# other request types have no menu on the wire at all — the menu is the set of
# legal answers, which we render into rows. Those rows need action types too,
# and they are named in MTGA's style so a downstream consumer can branch on
# ``action_type`` uniformly. They are OURS, not MTGA's; ``decision_kind`` says
# which request produced them.
# ---------------------------------------------------------------------------
AT_ATTACK = "ActionType_Attack"
AT_NO_ATTACK = "ActionType_NoAttack"
AT_BLOCK = "ActionType_Block"
AT_NO_BLOCK = "ActionType_NoBlock"
AT_TARGET = "ActionType_Target"
AT_SELECT = "ActionType_Select"
AT_FIND = "ActionType_Find"
AT_FAIL_TO_FIND = "ActionType_FailToFind"

# Options that are not a choice about *what to do with the board*: they end the
# decision without committing a resource.
PASS_LEVEL_ACTION_TYPES = frozenset(
    {
        "ActionType_Pass",
        AT_NO_ATTACK,
        AT_NO_BLOCK,
        AT_FAIL_TO_FIND,
    }
)

# MTGA's land-drop action types. ``ActionType_Play`` is *usually* a land, but
# the card database has the last word (see ``classify_entry``) — the whole
# point of require_card_database() is that a Plains is a land because the
# database says so, not because an enum name suggests it.
PLAY_ACTION_TYPES = frozenset({"ActionType_Play", "ActionType_PlayMDFC"})

# Keys of ``action_planner.ActionPlanner._build_action_prompt``'s own
# ``trigger_descriptions`` map — verified against that dict, because an unknown
# key silently renders as ``Trigger: <key>`` instead of the production
# sentence. Carried on every record so a prompt builder stops hardcoding
# "new_turn" ("Your turn started (Main Phase 1). Plan your plays.") on top of
# a block decision taken during the opponent's combat.
TRIGGER_BY_KIND = {
    "attack_declaration": "combat_attackers",
    "block_assignment": "combat_blockers",
    "target_choice": "decision_required",
    "select_n": "decision_required",
    "search": "search_library",
    "pay_costs": "pay_costs",
}


def production_trigger(kind: str, *, is_own_turn: bool, is_start_of_main1: bool) -> str:
    """Which production trigger fires at this decision point."""
    if kind != "priority_action":
        return TRIGGER_BY_KIND.get(kind, "decision_required")
    if not is_own_turn:
        return "opponent_turn"
    return "new_turn" if is_start_of_main1 else "priority_gained"


# ---------------------------------------------------------------------------
# Strategic filter
# ---------------------------------------------------------------------------


def classify_entry(entry: dict) -> str:
    """One of ``mana`` / ``pass`` / ``land`` / ``meaningful``.

    ``land`` is decided by the resolved type line, not by the action type
    alone: a numeric MTGA type code or an unresolved grpId must not silently
    turn a land into a "meaningful" option and smuggle a land drop past the
    filter. An ``ActionType_Play`` whose card the database does not call a
    land is reported as ``land`` anyway (Play is MTGA's land-drop verb) —
    failing towards the stricter classification is the safe direction here.
    """
    atype = entry.get("action_type") or "?"
    if atype in MANA_LEVEL_ACTION_TYPES:
        return "mana"
    if atype in PASS_LEVEL_ACTION_TYPES:
        return "pass"
    if atype in PLAY_ACTION_TYPES:
        return "land"
    if "land" in (entry.get("type_line") or "").lower() and atype == "ActionType_Play":
        return "land"
    return "meaningful"


# Requests that force you to choose *among candidates* — the GRE will not let
# you do nothing, so the only variation is which one. One candidate is a
# forced move, not a decision ("fetch your only Forest"). Contrast the
# action-style kinds below, where declining is itself a real commitment
# ("chump-block or take 5", "cast it now or hold it up").
SELECTION_STYLE_KINDS = frozenset({"target_choice", "select_n", "search"})


@dataclass
class StrategicVerdict:
    """Why a decision was kept or dropped, with the counts behind it."""

    is_strategic: bool
    reason: str
    classes: Counter = field(default_factory=Counter)
    n_options: int = 0  # menu size after mana-level entries are removed

    @property
    def n_meaningful(self) -> int:
        return self.classes.get("meaningful", 0)

    def as_dict(self) -> dict:
        return {
            "is_strategic": self.is_strategic,
            "reason": self.reason,
            "n_options_excl_mana": self.n_options,
            "n_meaningful_options": self.n_meaningful,
            "option_classes": dict(self.classes),
        }


def strategic_verdict(menu: list[dict], kind: str = "priority_action") -> StrategicVerdict:
    """Apply the owner's filter, in his order, and say which rule fired.

    1. mana-level rows (``Activate_Mana`` / ``FloatMana``) never count as
       options — production strips them from the numbered menu too;
    2. fewer than two remaining rows is not a choice;
    3. a menu whose only rows are land drops and/or pass is not a strategic
       choice, however many rows it has ("play your only land, or don't");
    4. otherwise it must offer at least one cast / attack / block /
       activation / target-style row;
    5. and for a selection-style request (target / select-n / search) it needs
       at least TWO of them, because a single candidate plus "fail to find" is
       a forced move dressed up as a menu.
    """
    classes = Counter(classify_entry(e) for e in menu)
    n_options = sum(v for k, v in classes.items() if k != "mana")
    verdict = StrategicVerdict(True, "kept", classes, n_options)
    if n_options < 2:
        verdict.is_strategic, verdict.reason = False, "single_option_menu"
    elif verdict.n_meaningful == 0:
        verdict.is_strategic, verdict.reason = False, "land_or_pass_only"
    elif kind in SELECTION_STYLE_KINDS and verdict.n_meaningful < 2:
        verdict.is_strategic, verdict.reason = False, "single_candidate_forced"
    return verdict


# ---------------------------------------------------------------------------
# Menu-row construction for the non-ActionsAvailable request types
# ---------------------------------------------------------------------------


def _row(
    action_type: str,
    *,
    grp_id: Optional[int] = None,
    instance_id: Optional[int] = None,
    ability_grp_id: Optional[int] = None,
    menu_text: str,
    equiv_key: Optional[str] = None,
) -> dict:
    """A menu row in the shape the gate's ``build_decision`` already reads.

    ``menu_text`` is the production-style line for this row; ``equiv_key``
    declares which rows are the *same choice* (two identical 2/2s blocking the
    same attacker are one choice; the same 2/2 blocking two different
    attackers are two). Both are additive — a consumer that ignores them falls
    back to ``action_type``/``grp_id``, which is what the old records carry.
    """
    facts = card_facts(grp_id)
    return {
        "action_type": action_type,
        "grp_id": int(grp_id) if grp_id is not None else None,
        "instance_id": int(instance_id) if instance_id is not None else None,
        "ability_grp_id": ability_grp_id,
        "name": facts["name"],
        "type_line": facts["type_line"],
        "mana_cost": facts["mana_cost"],
        "mana_cost_from_gre": False,
        "mana_value": facts["cmc"],
        "menu_text": menu_text,
        "equiv_key": equiv_key or f"{action_type.replace('ActionType_', '')}:{grp_id}",
    }


def _resolved(name: Optional[str]) -> bool:
    """``card_facts`` returns ``#<grpId>`` when the database has no such card."""
    return bool(name) and not str(name).startswith("#")


def _obj_facts(
    snap: GameStateSnapshot, instance_id: Optional[int], *, _depth: int = 0
) -> tuple[Optional[int], str, Optional[int]]:
    """(grpId, display name, controller seat) for a live instance id.

    An ability on the stack is its own game object with
    ``type == GameObjectType_Ability`` and a grpId that is an *ability* id, not
    a card id — 392 is the commonest, and the card database has no such card.
    Rendering it verbatim produced menu lines like "Target Soul Warden with
    #392" in 72% of targeting decisions: a prompt that names the target but not
    what is targeting it teaches a model to ignore card identity, which is the
    opposite of the point. The ability object carries ``objectSourceGrpId``
    (the permanent whose ability it is) and ``parentId`` (the object that put
    it on the stack); both are followed before giving up.
    """
    if instance_id is None:
        return None, "?", None
    obj = snap.game_objects.get(int(instance_id)) or {}
    grp = obj.get("grpId")
    ctrl = obj.get("controllerSeatId")
    name = card_facts(grp)["name"]
    if _resolved(name):
        return (int(grp) if grp is not None else None), name, ctrl

    source_grp = obj.get("objectSourceGrpId")
    if source_grp is not None:
        source_name = card_facts(source_grp)["name"]
        if _resolved(source_name):
            suffix = "'s ability" if obj.get("type") == "GameObjectType_Ability" else ""
            return int(source_grp), f"{source_name}{suffix}", ctrl

    parent = obj.get("parentId")
    if parent is not None and _depth < 4:
        pgrp, pname, pctrl = _obj_facts(snap, parent, _depth=_depth + 1)
        if _resolved(pname):
            return pgrp, pname, ctrl if ctrl is not None else pctrl

    return (int(grp) if grp is not None else None), name, ctrl


def _obj_name(snap: GameStateSnapshot, instance_id: Optional[int]) -> tuple[Optional[int], str]:
    """(grpId, display name) for a live instance id, via the game object map."""
    grp, name, _ = _obj_facts(snap, instance_id)
    return grp, name


def collapse_menu(menu: list[dict]) -> tuple[list[dict], int]:
    """Merge rows that are the SAME CHOICE, keeping the first of each.

    A Brawl token swarm produced a block menu with 238 rows reading "Block
    Raccoon with Michelangelo, Improviser" — 238 different attacker instances
    of one token. A fetchland produced a search menu with 33 rows, 20 of them
    "Search out Forest". Asking a model to pick one of twenty identical
    Forests is not a strategic decision, it is a lottery, and it would make
    every accuracy number on those records meaningless.

    Rows are merged on ``equiv_key``, which each builder defines as the
    identity of the *choice* rather than of the object: card + role, not
    instance. The fidelity cost is real and is stated here rather than hidden
    — two instances of one card can differ in counters, damage marked, and
    auras, and after collapsing the menu can no longer distinguish them. That
    is the right trade for a training corpus and the wrong one for an
    execution path, which must always re-derive its target from live state.

    Never applied to ``ActionsAvailable``: that menu is MTGA's own, and the
    old corpus (which downstream gates hash) carries it verbatim.
    """
    seen: dict[str, dict] = {}
    out: list[dict] = []
    collapsed = 0
    for entry in menu:
        key = entry["equiv_key"]
        first = seen.get(key)
        if first is not None:
            first["equiv_collapsed_rows"] = first.get("equiv_collapsed_rows", 1) + 1
            collapsed += 1
            continue
        seen[key] = entry
        out.append(entry)
    return out, collapsed


def _index_of_key(menu: list[dict], key: Optional[str]) -> Optional[int]:
    if key is None:
        return None
    return next((i for i, e in enumerate(menu) if e["equiv_key"] == key), None)


def _pick_record(
    menu: list[dict],
    index: Optional[int],
    *,
    status: str = "ok",
    match: str = "exact",
    n_actions: int = 1,
) -> dict:
    """``real_pick`` in the schema the gate reads."""
    if index is None:
        return {"status": "not_in_menu", "match": "not_in_menu", "menu_index": None}
    entry = menu[index]
    return {
        "status": status,
        "match": match,
        "menu_index": index,
        "action_type": entry["action_type"],
        "grp_id": entry["grp_id"],
        "instance_id": entry["instance_id"],
        "name": entry["name"],
        "n_actions_submitted": n_actions,
    }


def build_attack_decision(
    request: ReplayMessage, response: Optional[ReplayMessage], snap: GameStateSnapshot
) -> Optional[dict]:
    """"Which creature attacks?" — only when the answer is a single creature.

    ``autoDeclare`` (attack with everything) and multi-creature declarations
    have several equally-correct picks, so they are reported as ambiguous
    instead of being collapsed onto one arbitrary attacker.
    """
    da = request.payload.get("declareAttackersReq") or {}
    qualified = da.get("qualifiedAttackers") or []
    if not qualified:
        return {"drop_reason": "no_qualified_attackers"}
    if response is None or response.msg_type != "ClientMessageType_DeclareAttackersResp":
        return {"drop_reason": "no_declaration_response"}
    body = response.payload.get("declareAttackersResp") or {}
    if body.get("autoDeclare"):
        return {"drop_reason": "multi_answer_ambiguous", "detail": "auto_declare_all"}
    selected = body.get("selectedAttackers") or []
    if len(selected) != 1:
        return {
            "drop_reason": "multi_answer_ambiguous" if selected else "empty_declaration",
            "detail": f"selected={len(selected)}",
        }

    menu: list[dict] = []
    for a in qualified:
        iid = a.get("attackerInstanceId")
        grp, name = _obj_name(snap, iid)
        recipients = a.get("legalDamageRecipients") or []
        where = f" ({len(recipients)} possible damage recipients)" if len(recipients) > 1 else ""
        menu.append(
            _row(
                AT_ATTACK,
                grp_id=grp,
                instance_id=iid,
                menu_text=f"Attack with {name}{where}",
                # Attacking with either of two identical creatures is one
                # choice — see collapse_menu for the fidelity trade.
                equiv_key=f"Attack:{grp}",
            )
        )
    menu.append(_row(AT_NO_ATTACK, menu_text="Declare no attackers", equiv_key="NoAttack"))
    menu, collapsed = collapse_menu(menu)

    chosen_iid = selected[0].get("attackerInstanceId")
    chosen_grp, _ = _obj_name(snap, chosen_iid)
    idx = _index_of_key(menu, f"Attack:{chosen_grp}")
    return {
        "menu": menu,
        "pick": _pick_record(menu, idx),
        "kind": "attack_declaration",
        "context": {
            "n_qualified_attackers": len(qualified),
            "selected_attacker_instance_id": chosen_iid,
            "selected_damage_recipient": (selected[0].get("selectedDamageRecipient") or {}) or None,
            "menu_rows_collapsed": collapsed,
        },
    }


def build_block_menu(
    request: ReplayMessage, snap: GameStateSnapshot
) -> tuple[list[dict], dict, dict[tuple[int, int], str]]:
    """Every legal (blocker, attacker) pair, plus "decline to block".

    Built from the FIRST ``DeclareBlockersReq`` of a combat, against the
    snapshot *at that moment* — a blocker that dies mid-combat is gone from
    ``game_objects`` by the end of the walk, so resolving names later would
    silently render them as ``#<grpId>``.
    """
    db = request.payload.get("declareBlockersReq") or {}
    candidates = db.get("blockers") or []
    menu: list[dict] = []
    # (blocker instance, attacker instance) -> the row that represents that
    # pairing after collapsing. The label arrives later, as instance ids in a
    # DeclareBlockersResp, and by then the blocker may already be dead.
    pair_keys: dict[tuple[int, int], str] = {}
    for b in candidates:
        bid = b.get("blockerInstanceId")
        bgrp, bname = _obj_name(snap, bid)
        for aid in b.get("attackerInstanceIds") or []:
            agrp, aname = _obj_name(snap, aid)
            # Same blocker card in front of the same attacker card is one
            # choice, whichever copies supply it; a different attacker card is
            # a different choice.
            key = f"Block:{bgrp}:{agrp}"
            pair_keys[(int(bid), int(aid))] = key
            menu.append(
                _row(
                    AT_BLOCK,
                    grp_id=bgrp,
                    instance_id=bid,
                    menu_text=f"Block {aname} with {bname}",
                    equiv_key=key,
                )
            )
    collapsed = 0
    if menu:
        menu.append(_row(AT_NO_BLOCK, menu_text="Decline to block", equiv_key="NoBlock"))
        menu, collapsed = collapse_menu(menu)
    context = {
        "n_candidate_blockers": len(candidates),
        "n_legal_pairs": max(0, len(menu) - 1),
        "n_legal_pairs_before_collapse": max(0, len(menu) - 1 + collapsed),
        "menu_rows_collapsed": collapsed,
        "attackers": sorted({int(a) for b in candidates for a in (b.get("attackerInstanceIds") or [])}),
        "has_restrictions": bool(db.get("hasRestrictions")),
    }
    return menu, context, pair_keys


def build_block_decision(
    menu: list[dict],
    context: dict,
    pair_keys: dict[tuple[int, int], str],
    episode: list[tuple[ReplayMessage, Optional[ReplayMessage]]],
) -> dict:
    """"Which blocker goes in front of which attacker?" for one combat.

    ``episode`` is every ``DeclareBlockersReq`` of one combat, in order, each
    with its paired response — MTGA re-issues the request after each
    assignment, so one combat is several messages. Exactly one assignment is a
    pick; zero assignments plus a commit is a deliberate "no blocks"; two or
    more has several correct answers and is dropped.
    """
    if not menu:
        return {"drop_reason": "no_legal_blocks"}

    assignments: list[tuple[int, int]] = []
    committed = False
    for _, resp in episode:
        if resp is None:
            continue
        if resp.msg_type == "ClientMessageType_SubmitBlockersReq":
            committed = True
            continue
        if resp.msg_type != "ClientMessageType_DeclareBlockersResp":
            continue
        for b in (resp.payload.get("declareBlockersResp") or {}).get("selectedBlockers") or []:
            bid = b.get("blockerInstanceId")
            for aid in b.get("selectedAttackerInstanceIds") or []:
                assignments.append((int(bid), int(aid)))

    if len(assignments) > 1:
        return {"drop_reason": "multi_answer_ambiguous", "detail": f"blocks={len(assignments)}"}
    if not assignments:
        if not committed:
            return {"drop_reason": "no_declaration_response"}
        idx = len(menu) - 1  # the decline row
    else:
        idx = _index_of_key(menu, pair_keys.get(assignments[0]))
    ctx = dict(context)
    ctx["requests_in_episode"] = len(episode)
    ctx["declined"] = not assignments
    return {"menu": menu, "pick": _pick_record(menu, idx), "kind": "block_assignment", "context": ctx}


def build_target_decision(
    request: ReplayMessage, response: Optional[ReplayMessage], snap: GameStateSnapshot
) -> Optional[dict]:
    """"What does this spell or ability point at?" — single slot, one target."""
    st = request.payload.get("selectTargetsReq") or {}
    slots = st.get("targets") or []
    if len(slots) != 1:
        return {"drop_reason": "multi_slot_targeting"}
    slot = slots[0]
    if int(slot.get("maxTargets") or 1) != 1:
        return {"drop_reason": "multi_answer_ambiguous", "detail": "maxTargets>1"}
    candidates = slot.get("targets") or []
    if response is None or response.msg_type != "ClientMessageType_SelectTargetsResp":
        # The remainder are the client's retry loop: SubmitTargetsReq answered
        # by an IllegalRequest, with the same request re-issued. No human
        # choice is recorded there.
        return {"drop_reason": "no_selection_response"}
    chosen = ((response.payload.get("selectTargetsResp") or {}).get("target") or {}).get("targets") or []
    if len(chosen) != 1:
        return {"drop_reason": "multi_answer_ambiguous", "detail": f"chose={len(chosen)}"}

    source_grp, source_name = _obj_name(snap, st.get("sourceId"))
    menu = []
    keys: dict[int, str] = {}
    for c in candidates:
        iid = c.get("targetInstanceId")
        grp, name, ctrl = _obj_facts(snap, iid)
        # Whose creature it is matters (killing yours is not killing theirs);
        # which identical copy of it does not.
        key = f"Target:{grp}:{ctrl}"
        keys[int(iid)] = key
        menu.append(
            _row(
                AT_TARGET,
                grp_id=grp,
                instance_id=iid,
                menu_text=f"Target {name} with {source_name}",
                equiv_key=key,
            )
        )
    menu, collapsed = collapse_menu(menu)
    chosen_iid = chosen[0].get("targetInstanceId")
    idx = _index_of_key(menu, keys.get(int(chosen_iid)) if chosen_iid is not None else None)
    return {
        "menu": menu,
        "pick": _pick_record(menu, idx),
        "kind": "target_choice",
        "context": {
            "source_instance_id": st.get("sourceId"),
            "source_grp_id": source_grp,
            "source_name": source_name,
            "ability_grp_id": st.get("abilityGrpId"),
            "min_targets": slot.get("minTargets"),
            "max_targets": slot.get("maxTargets"),
            "n_candidates": len(candidates),
            "menu_rows_collapsed": collapsed,
        },
    }


def build_select_n_decision(
    request: ReplayMessage, response: Optional[ReplayMessage], snap: GameStateSnapshot
) -> Optional[dict]:
    """"Choose one of these." — the GRE's general single-selection request."""
    sn = request.payload.get("selectNReq") or {}
    ids = sn.get("ids") or []
    if not ids:
        return {"drop_reason": "no_selectable_ids"}
    if sn.get("idType") != "IdType_InstanceId":
        # ZoneInstanceId / PromptParameterIndex selections cannot be rendered
        # as cards without the prompt catalogue, so they are not reconstructed.
        return {"drop_reason": "unrenderable_id_type", "detail": str(sn.get("idType"))}
    max_sel = sn.get("maxSel")
    if max_sel is None or int(max_sel) != 1:
        return {"drop_reason": "multi_answer_ambiguous", "detail": f"maxSel={max_sel}"}
    if response is None or response.msg_type != "ClientMessageType_SelectNResp":
        return {"drop_reason": "no_selection_response"}
    chosen = (response.payload.get("selectNResp") or {}).get("ids") or []
    if len(chosen) != 1:
        return {"drop_reason": "multi_answer_ambiguous", "detail": f"chose={len(chosen)}"}

    source_grp, source_name = _obj_name(snap, sn.get("sourceId"))
    menu = []
    keys: dict[int, str] = {}
    for iid in ids:
        grp, name = _obj_name(snap, iid)
        keys[int(iid)] = f"Select:{grp}"
        menu.append(
            _row(
                AT_SELECT,
                grp_id=grp,
                instance_id=iid,
                menu_text=f"Choose {name}",
                equiv_key=f"Select:{grp}",
            )
        )
    menu, collapsed = collapse_menu(menu)
    idx = _index_of_key(menu, keys.get(int(chosen[0])))
    return {
        "menu": menu,
        "pick": _pick_record(menu, idx),
        "kind": "select_n",
        "context": {
            "source_instance_id": sn.get("sourceId"),
            "source_grp_id": source_grp,
            "source_name": source_name,
            "selection_context": sn.get("context"),
            "min_sel": sn.get("minSel"),
            "max_sel": max_sel,
            "n_candidates": len(ids),
            "menu_rows_collapsed": collapsed,
        },
    }


def build_search_decision(
    request: ReplayMessage, response: Optional[ReplayMessage], snap: GameStateSnapshot
) -> Optional[dict]:
    """"What do you fetch?" — a tutor's target, the purest strategic choice."""
    sr = request.payload.get("searchReq") or {}
    sought = sr.get("itemsSought") or []
    if not sought:
        return {"drop_reason": "nothing_to_find"}
    max_find = sr.get("maxFind")
    if max_find is None or int(max_find) != 1:
        return {"drop_reason": "multi_answer_ambiguous", "detail": f"maxFind={max_find}"}
    if response is None or response.msg_type != "ClientMessageType_SearchResp":
        return {"drop_reason": "no_selection_response"}
    found = (response.payload.get("searchResp") or {}).get("itemsFound") or []
    if len(found) > 1:
        return {"drop_reason": "multi_answer_ambiguous", "detail": f"found={len(found)}"}

    source_grp, source_name = _obj_name(snap, sr.get("sourceId"))
    menu = []
    keys: dict[int, str] = {}
    for iid in sought:
        grp, name = _obj_name(snap, iid)
        # Twenty Forests in a library are one choice, not twenty.
        keys[int(iid)] = f"Find:{grp}"
        menu.append(
            _row(
                AT_FIND,
                grp_id=grp,
                instance_id=iid,
                menu_text=f"Search out {name}",
                equiv_key=f"Find:{grp}",
            )
        )
    allow_fail = str(sr.get("allowFailToFind") or "")
    if allow_fail and allow_fail != "AllowFailToFind_No":
        menu.append(_row(AT_FAIL_TO_FIND, menu_text="Fail to find", equiv_key="FailToFind"))
    menu, collapsed = collapse_menu(menu)

    if not found:
        idx = _index_of_key(menu, "FailToFind")
        if idx is None:
            return {"drop_reason": "fail_to_find_not_offered"}
    else:
        idx = _index_of_key(menu, keys.get(int(found[0])))
    return {
        "menu": menu,
        "pick": _pick_record(menu, idx),
        "kind": "search",
        "context": {
            "source_instance_id": sr.get("sourceId"),
            "source_grp_id": source_grp,
            "source_name": source_name,
            "zones_to_search": sr.get("zonesToSearch"),
            "n_candidates": len(sought),
            "n_distinct_candidates": len(menu) - (1 if allow_fail and allow_fail != "AllowFailToFind_No" else 0),
            "menu_rows_collapsed": collapsed,
            "library_size_searched": len(sr.get("itemsToSearch") or []),
            "allow_fail_to_find": allow_fail,
        },
    }


def build_pay_costs_decision(
    request: ReplayMessage, response: Optional[ReplayMessage], snap: GameStateSnapshot
) -> Optional[dict]:
    """"Which permanents pay for this?" — extracted so the funnel can say it.

    Every row is ``ActionType_Activate_Mana``, so ``strategic_verdict`` drops
    the whole request as ``single_option_menu``/``land_or_pass_only``. That is
    the correct answer: choosing which Forest to tap is mana plumbing, and the
    autotapper answers 60 of these with an empty ``PerformAutoTapActionsResp``
    without the human touching anything. Keeping the extraction path means the
    report shows a measured zero rather than an unexamined gap.
    """
    pc = request.payload.get("payCostsReq") or {}
    actions = ((pc.get("paymentActions") or {}).get("actions")) or []
    if not actions:
        return {"drop_reason": "no_payment_actions"}
    menu = []
    for a in actions:
        grp = a.get("grpId")
        menu.append(
            _row(
                a.get("actionType") or "ActionType_Activate_Mana",
                grp_id=grp,
                instance_id=a.get("instanceId"),
                ability_grp_id=a.get("abilityGrpId"),
                menu_text=f"Tap {card_facts(grp)['name']} for mana",
                equiv_key=f"Mana:{grp}:{a.get('instanceId')}",
            )
        )
    return {
        "menu": menu,
        "pick": _pick_record(menu, None),
        "kind": "pay_costs",
        "context": {
            "mana_cost": format_gre_mana_cost(pc.get("manaCost") or []),
            "mana_value": gre_mana_value(pc.get("manaCost") or []),
            "n_payment_actions": len(actions),
            "response": response.msg_type if response else None,
        },
    }


# ---------------------------------------------------------------------------
# Replay walking
# ---------------------------------------------------------------------------

REQUEST_KINDS = {
    "GREMessageType_ActionsAvailableReq": "priority_action",
    "GREMessageType_DeclareAttackersReq": "attack_declaration",
    "GREMessageType_DeclareBlockersReq": "block_assignment",
    "GREMessageType_SelectTargetsReq": "target_choice",
    "GREMessageType_SelectNReq": "select_n",
    "GREMessageType_SearchReq": "search",
    "GREMessageType_PayCostsReq": "pay_costs",
}


@dataclass
class WalkStats:
    """Funnel counters for one replay. Summed across the corpus by ``summarize``."""

    file: str = ""
    local_seat: Optional[int] = None
    super_format: str = ""
    variant: str = ""
    # stage 1: every request of a handled type, any seat
    seen: Counter = field(default_factory=Counter)
    # stage 2: addressed to the recording player
    addressed: Counter = field(default_factory=Counter)
    # stage 2b: combat requests are re-issued after every toggle/assignment, so
    # several messages describe ONE decision; this counts the grouped episodes
    episodes: Counter = field(default_factory=Counter)
    # stage 3: a single unambiguous human answer could be reconstructed
    reconstructed: Counter = field(default_factory=Counter)
    # stage 4: survived the strategic filter
    strategic: Counter = field(default_factory=Counter)
    # why things fell out, by (request_type, reason)
    drops: Counter = field(default_factory=Counter)
    strategic_by_phase: Counter = field(default_factory=Counter)
    reconstructed_by_phase: Counter = field(default_factory=Counter)
    pick_types: Counter = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)


def _hand_rows(snap: GameStateSnapshot, seat: int) -> list[dict]:
    rows = []
    for o in snap.hand(seat):
        gid = o.get("grpId")
        facts = card_facts(gid)
        rows.append(
            {
                "grp_id": int(gid) if gid is not None else None,
                "instance_id": int(o["instanceId"]) if o.get("instanceId") is not None else None,
                "name": facts["name"],
                "mana_cost": facts["mana_cost"],
                "cmc": facts["cmc"],
                "type_line": facts["type_line"],
            }
        )
    return rows


def _stack_rows(snap: GameStateSnapshot) -> list[dict]:
    """Objects on the stack — the context an instant-speed decision needs.

    ``menu_groundtruth`` never needed this: nothing is on the stack at the
    start of your own main phase. Every decision this module adds *outside*
    that window can have a stack, and "should I respond to this?" is
    unanswerable without knowing what "this" is.
    """
    stack_zones = [z["zoneId"] for z in snap.zones.values() if z.get("type") == "ZoneType_Stack"]
    rows = []
    for obj in snap.game_objects.values():
        if obj.get("zoneId") not in stack_zones:
            continue
        gid = obj.get("grpId")
        facts = card_facts(gid)
        rows.append(
            {
                "grp_id": int(gid) if gid is not None else None,
                "instance_id": int(obj["instanceId"]) if obj.get("instanceId") is not None else None,
                "name": facts["name"],
                "type_line": facts["type_line"],
                "controller_seat_id": obj.get("controllerSeatId"),
            }
        )
    return rows


def _build_state_block(snap: GameStateSnapshot, seat: int, lands_played: int) -> dict:
    """The ``state`` block, byte-compatible with ``menu_groundtruth`` plus extras."""
    opp = opponent_seat(seat)
    you = _battlefield_split(snap, seat)
    other = _battlefield_split(snap, opp)
    hand = _hand_rows(snap, seat)
    return {
        "life": {"you": snap.life(seat), "opp": snap.life(opp)},
        "hand": hand,
        "hand_grp_ids": [c["grp_id"] for c in hand],
        "hand_size": len(hand),
        "lands_in_play": you["lands"],
        "lands_in_play_grp_ids": [c["grp_id"] for c in you["lands"]],
        "lands_tapped": sum(1 for c in you["lands"] if c["is_tapped"]),
        "creatures_in_play": you["creatures"],
        "creatures_in_play_grp_ids": [c["grp_id"] for c in you["creatures"]],
        "other_permanents": you["other"],
        "other_permanents_grp_ids": [c["grp_id"] for c in you["other"]],
        "opp_lands_grp_ids": [c["grp_id"] for c in other["lands"]],
        "opp_creatures_grp_ids": [c["grp_id"] for c in other["creatures"]],
        "opp_other_grp_ids": [c["grp_id"] for c in other["other"]],
        "opp_hand_size": snap.hand_size(opp),
        # ---- fields menu_groundtruth never needed (its scope made them constant)
        # Consumers that assume "start of own main 1" hardcode lands_played=0 and
        # "nothing entered this turn". Outside that window both are wrong, so the
        # true values travel with the record.
        "lands_played_this_turn": lands_played,
        "opp_lands_in_play": other["lands"],
        "opp_creatures_in_play": other["creatures"],
        "opp_other_permanents": other["other"],
        "stack": _stack_rows(snap),
    }


def _episode_key(snap: GameStateSnapshot, step: str) -> tuple:
    """Identity of one combat step, used to group re-issued combat requests.

    MTGA re-issues ``DeclareBlockersReq`` after every assignment and
    ``DeclareAttackersReq`` after every toggle; without grouping, one combat
    would become several near-duplicate records of the same position.
    """
    return (snap.turn_number, snap.phase, step, snap.active_player)


def extract_replay(path: Path, *, seat_override: Optional[int] = None) -> tuple[list[dict], WalkStats]:
    """Walk one ``.rply`` and emit every reconstructable decision + the funnel."""
    stats = WalkStats(file=path.name)
    try:
        meta, messages = parse_replay_path(path)
    except Exception as exc:  # noqa: BLE001
        stats.errors.append(f"parse: {exc}")
        return [], stats

    try:
        # The FIXED detector (state.resolve_local_seat): 55 of the 104 corpus
        # replays are seat 1, and the old local_seat_id=2 default read the
        # opponent's hand and board on every one of them.
        seat = resolve_local_seat(messages, seat_override)
    except LocalSeatUndetermined as exc:
        stats.errors.append(str(exc))
        return [], stats
    stats.local_seat = seat

    by_resp = pair_request_response(messages)
    snap = GameStateSnapshot(local_seat_id=seat)
    step = ""
    game_number: Optional[int] = None
    lands_played = 0
    turn_of_land_count: Optional[int] = None

    main1_windows: Counter[int] = Counter()
    own_turn_seen: list[int] = []
    aa_index = -1  # preserves menu_groundtruth's decision_index for AA records

    # Block requests are re-issued after every assignment; one combat is one
    # decision, so they are collected per (turn, phase) and built at the end.
    block_episodes: dict[tuple, list[tuple[ReplayMessage, Optional[ReplayMessage]]]] = {}
    # (timing, state, menu, context, pair_keys) captured at the FIRST request
    # of the combat, while the blockers are all still alive and resolvable
    block_episode_open: dict[tuple, tuple[dict, dict, list[dict], dict, dict]] = {}
    attack_episode_seen: set[tuple] = set()

    records: list[dict] = []

    def emit(
        request: ReplayMessage,
        built: dict,
        *,
        snap_state: dict,
        timing: dict,
        decision_index: int,
    ) -> None:
        kind = built["kind"]
        menu = built["menu"]
        pick = built["pick"]
        verdict = strategic_verdict(menu, kind)
        rtype = request.msg_type.replace("GREMessageType_", "").replace("Req", "")
        stats.reconstructed[rtype] += 1
        stats.reconstructed_by_phase[(rtype, timing["phase"], "own" if timing["is_own_turn"] else "opp")] += 1
        if not verdict.is_strategic:
            stats.drops[(rtype, verdict.reason)] += 1
            return
        if pick.get("status") != "ok" or pick.get("menu_index") is None:
            stats.drops[(rtype, "pick_not_in_menu")] += 1
            return
        if menu[pick["menu_index"]]["action_type"] in MANA_LEVEL_ACTION_TYPES:
            stats.drops[(rtype, "gold_pick_is_mana_action")] += 1
            return
        stats.strategic[rtype] += 1
        stats.strategic_by_phase[(rtype, timing["phase"], "own" if timing["is_own_turn"] else "opp")] += 1
        stats.pick_types[(rtype, pick.get("action_type"))] += 1

        rec = {
            # ---- provenance (identical keys to menu_groundtruth) ----
            "replay_file": path.name,
            "decision_index": decision_index,
            "msg_id": request.msg_id,
            "game_state_id": request.payload.get("gameStateId"),
            "game_number": game_number,
            "super_format": stats.super_format,
            "variant": stats.variant,
            "local_seat": seat,
            "local_screen_name": meta.local_screen_name,
            # ---- what kind of decision this is (new) ----
            "request_type": rtype,
            "decision_kind": kind,
            "production_trigger": production_trigger(
                kind,
                is_own_turn=timing["is_own_turn"],
                is_start_of_main1=bool(timing["is_start_of_main1"]),
            ),
            "decision_uid": f"{rtype}:{request.msg_id}",
            # ---- timing ----
            **timing,
            # ---- THE MENU ----
            "real_menu": menu,
            "real_menu_size": len(menu),
            "real_menu_key": menu_key(menu),
            "real_menu_key_spell": menu_key(menu, spell_level=True),
            "real_menu_inactive": built.get("inactive", []),
            "real_menu_inactive_key": menu_key(built.get("inactive", [])),
            # ---- THE PICK ----
            "real_pick": pick,
            # ---- the filter's own reasoning, so a reader can audit it ----
            "strategic": verdict.as_dict(),
            "decision_context": built.get("context", {}),
            "state": snap_state,
        }
        records.append(rec)

    for m in messages:
        if m.direction != "IN":
            continue

        for gsm in _iter_state_messages(m):
            gi = gsm.get("gameInfo")
            if gi:
                if gi.get("superFormat"):
                    stats.super_format = gi["superFormat"]
                if gi.get("variant"):
                    stats.variant = gi["variant"]
                gn = gi.get("gameNumber")
                if gn is not None and gn != game_number:
                    game_number = gn
                    main1_windows.clear()
                    own_turn_seen.clear()
                    lands_played = 0
                    turn_of_land_count = None
            _apply_state_message(snap, gsm)
            ti = gsm.get("turnInfo")
            if ti and ("step" in ti or "phase" in ti):
                step = ti.get("step") or ""

        if snap.turn_number != turn_of_land_count:
            turn_of_land_count = snap.turn_number
            lands_played = 0

        kind = REQUEST_KINDS.get(m.msg_type)
        if kind is None:
            continue
        rtype = m.msg_type.replace("GREMessageType_", "").replace("Req", "")
        stats.seen[rtype] += 1

        addressed = [int(x) for x in (m.payload.get("systemSeatIds") or [])]
        if addressed and seat not in addressed:
            continue
        stats.addressed[rtype] += 1

        resp = by_resp.get(m.msg_id) if m.msg_id is not None else None

        is_own_turn = snap.active_player == seat
        is_main1 = snap.phase == "Phase_Main1"
        if is_own_turn and snap.turn_number not in own_turn_seen:
            own_turn_seen.append(snap.turn_number)
        window_ordinal = None
        if m.msg_type == "GREMessageType_ActionsAvailableReq" and is_own_turn and is_main1:
            main1_windows[snap.turn_number] += 1
            window_ordinal = main1_windows[snap.turn_number]

        timing = {
            "turn_number": snap.turn_number,
            "player_turn_index": (
                own_turn_seen.index(snap.turn_number) + 1 if snap.turn_number in own_turn_seen else None
            ),
            "phase": snap.phase,
            "step": step,
            "active_player": snap.active_player,
            "priority_player": snap.priority_player,
            "is_own_turn": is_own_turn,
            "is_start_of_main1": window_ordinal == 1,
            "main1_window_ordinal": window_ordinal,
        }
        state_block = _build_state_block(snap, seat, lands_played)

        if m.msg_type == "GREMessageType_ActionsAvailableReq":
            aa_index += 1
            active, inactive = normalize_menu(m)
            pick = resolve_pick(m, resp, active)
            emit(
                m,
                {"menu": active, "inactive": inactive, "pick": pick, "kind": kind, "context": {}},
                snap_state=state_block,
                timing=timing,
                decision_index=aa_index,
            )
            if pick.get("action_type") in PLAY_ACTION_TYPES:
                lands_played += 1
            continue

        if m.msg_type == "GREMessageType_DeclareBlockersReq":
            key = _episode_key(snap, step)
            if key not in block_episodes:
                block_episodes[key] = []
                stats.episodes[rtype] += 1
                menu, ctx, pair_keys = build_block_menu(m, snap)
                block_episode_open[key] = (dict(timing), state_block, menu, ctx, pair_keys)
            block_episodes[key].append((m, resp))
            continue

        if m.msg_type == "GREMessageType_DeclareAttackersReq":
            key = _episode_key(snap, step)
            if key in attack_episode_seen:
                stats.drops[(rtype, "duplicate_episode_request")] += 1
                continue
            built = build_attack_decision(m, resp, snap)
            if "drop_reason" in built:
                stats.drops[(rtype, built["drop_reason"])] += 1
                continue
            attack_episode_seen.add(key)
            stats.episodes[rtype] += 1
        elif m.msg_type == "GREMessageType_SelectTargetsReq":
            built = build_target_decision(m, resp, snap)
        elif m.msg_type == "GREMessageType_SelectNReq":
            built = build_select_n_decision(m, resp, snap)
        elif m.msg_type == "GREMessageType_SearchReq":
            built = build_search_decision(m, resp, snap)
        elif m.msg_type == "GREMessageType_PayCostsReq":
            built = build_pay_costs_decision(m, resp, snap)
        else:  # pragma: no cover - REQUEST_KINDS is exhaustive
            continue

        if built is None or "drop_reason" in built:
            stats.drops[(rtype, (built or {}).get("drop_reason", "unknown"))] += 1
            continue
        emit(m, built, snap_state=state_block, timing=timing, decision_index=aa_index)

    # Blocker episodes are finished after the walk (the last request of a
    # combat is only known once the next one starts).
    for key, episode in block_episodes.items():
        timing, state_block, menu, ctx, pair_keys = block_episode_open[key]
        built = build_block_decision(menu, ctx, pair_keys, episode)
        rtype = "DeclareBlockers"
        if built is None or "drop_reason" in built:
            stats.drops[(rtype, (built or {}).get("drop_reason", "unknown"))] += 1
            continue
        emit(episode[0][0], built, snap_state=state_block, timing=timing, decision_index=aa_index)

    # decision_index must be unique per replay — the gate derives its record id
    # from it (``pd-<stem>-<index>``) and a collision silently merges two
    # decisions. The AA path reuses menu_groundtruth's numbering, so everything
    # else is renumbered above it, in message order.
    records.sort(key=lambda r: (r["msg_id"] if r["msg_id"] is not None else 0))
    next_index = aa_index + 1
    for rec in records:
        if rec["request_type"] != "ActionsAvailable":
            rec["decision_index"] = next_index
            next_index += 1
    return records, stats


# ---------------------------------------------------------------------------
# Corpus driver
# ---------------------------------------------------------------------------


def _merge(counters: list[Counter]) -> Counter:
    out: Counter = Counter()
    for c in counters:
        out.update(c)
    return out


def summarize(all_stats: list[WalkStats], records: list[dict]) -> dict:
    """The funnel, stage by stage, per request type and per phase."""
    seen = _merge([s.seen for s in all_stats])
    addressed = _merge([s.addressed for s in all_stats])
    episodes = _merge([s.episodes for s in all_stats])
    reconstructed = _merge([s.reconstructed for s in all_stats])
    strategic = _merge([s.strategic for s in all_stats])
    drops = _merge([s.drops for s in all_stats])
    by_phase = _merge([s.strategic_by_phase for s in all_stats])
    recon_by_phase = _merge([s.reconstructed_by_phase for s in all_stats])
    pick_types = _merge([s.pick_types for s in all_stats])

    def _funnel(rtype: str) -> dict:
        out = {
            "1_requests_in_replays": seen.get(rtype, 0),
            "2_addressed_to_recording_player": addressed.get(rtype, 0),
        }
        if rtype in episodes:
            # Combat requests are re-issued after every toggle; several
            # messages are ONE decision.
            out["2b_decisions_after_episode_grouping"] = episodes[rtype]
        out["3_single_answer_reconstructed"] = reconstructed.get(rtype, 0)
        out["4_survived_strategic_filter"] = strategic.get(rtype, 0)
        out["dropped"] = {r: n for (t, r), n in sorted(drops.items()) if t == rtype}
        return out

    land_picks = sum(
        1 for r in records if r["real_pick"].get("action_type") in PLAY_ACTION_TYPES
    )
    pass_picks = sum(1 for r in records if r["real_pick"].get("action_type") in PASS_LEVEL_ACTION_TYPES)
    n = len(records)
    menu_sizes = sorted(r["real_menu_size"] for r in records)

    unresolved = total_named = 0
    for r in records:
        for e in r["real_menu"]:
            if e["grp_id"] is None:
                continue
            total_named += 1
            if str(e["name"]).startswith("#"):
                unresolved += 1

    decisive = n - land_picks - pass_picks
    return {
        "READ_THIS_FIRST": (
            "The strategic filter constrains the OPTIONS, not the LABEL. A window "
            "offering three castable spells whose human answer was 'pass' is a real "
            "decision and is kept — but a corpus that is 28% pass can teach a "
            "pass reflex the same way the old one taught a land reflex. "
            f"{decisive} of {n} records ({round(100.0 * decisive / n, 1) if n else 0}%) "
            "commit a resource (cast / activate / attack / block / target / search); "
            f"{land_picks} are land drops and {pass_picks} are pass-style. Weight or "
            "subset accordingly and quote the decisive count, not the total."
        ),
        "replays_parsed": len(all_stats),
        "replays_with_errors": sum(1 for s in all_stats if s.errors),
        "local_seat_by_file": dict(Counter(s.local_seat for s in all_stats).most_common()),
        "format_by_file": {
            f"{k[0]}/{k[1]}": v
            for k, v in Counter((s.super_format, s.variant) for s in all_stats).most_common()
        },
        "funnel_by_request_type": {t: _funnel(t) for t in sorted(seen)},
        "funnel_totals": {
            "1_requests_in_replays": sum(seen.values()),
            "2_addressed_to_recording_player": sum(addressed.values()),
            "3_single_answer_reconstructed": sum(reconstructed.values()),
            "4_survived_strategic_filter": sum(strategic.values()),
        },
        "strategic_by_request_type": dict(strategic.most_common()),
        "strategic_by_phase": {
            f"{t}|{p.replace('Phase_', '')}|{who}": v for (t, p, who), v in sorted(by_phase.items())
        },
        "reconstructed_by_phase": {
            f"{t}|{p.replace('Phase_', '')}|{who}": v for (t, p, who), v in sorted(recon_by_phase.items())
        },
        "label_action_types": {f"{t}|{a}": v for (t, a), v in pick_types.most_common()},
        "composition": {
            "records": n,
            "land_drop_labels": land_picks,
            "land_drop_share": round(land_picks / n, 4) if n else 0.0,
            "pass_style_labels": pass_picks,
            "pass_style_share": round(pass_picks / n, 4) if n else 0.0,
            "decisive_labels": decisive,
            "decisive_share": round(decisive / n, 4) if n else 0.0,
            # The hard-choice tier: two or more resource-committing options on
            # the menu, so neither "pass" nor "play the only land" is available
            # as a reflex.
            "records_with_2plus_meaningful_options": sum(
                1 for r in records if r["strategic"]["n_meaningful_options"] >= 2
            ),
            "menu_size": {
                "min": menu_sizes[0] if menu_sizes else None,
                "median": menu_sizes[len(menu_sizes) // 2] if menu_sizes else None,
                "max": menu_sizes[-1] if menu_sizes else None,
                "mean": round(sum(menu_sizes) / n, 2) if n else None,
            },
            "by_decision_kind": dict(Counter(r["decision_kind"] for r in records).most_common()),
            "records_from_start_of_main1": sum(1 for r in records if r["is_start_of_main1"]),
            "records_on_own_turn": sum(1 for r in records if r["is_own_turn"]),
            "replay_files_contributing": len({r["replay_file"] for r in records}),
        },
        "card_name_resolution": {
            "menu_entries_with_grp_id": total_named,
            "unresolved": unresolved,
            "resolved_pct": round(100.0 * (total_named - unresolved) / total_named, 2)
            if total_named
            else None,
        },
    }


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--replay-dir", type=Path, required=True, help="Directory containing .rply files")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"Output JSONL (default: {DEFAULT_OUT})")
    p.add_argument("--summary-out", type=Path, default=None, help="Optional JSON path for the funnel")
    p.add_argument(
        "--request-types",
        default="",
        help="Comma-separated request types to keep (e.g. ActionsAvailable,SelectTargets). Default: all",
    )
    p.add_argument(
        "--main1-only",
        action="store_true",
        help="Emit only start-of-own-Main-1 ActionsAvailable decisions — the "
        "menu_groundtruth.py scope, for comparing the two extractions",
    )
    p.add_argument("--limit", type=int, default=None, help="Only process the first N replays")
    args = p.parse_args(argv)

    files = sorted(args.replay_dir.glob("*.rply"))
    if args.limit:
        files = files[: args.limit]
    if not files:
        print(f"no .rply files under {args.replay_dir}", file=sys.stderr)
        return 2

    keep_types = {t.strip() for t in args.request_types.split(",") if t.strip()}

    all_records: list[dict] = []
    all_stats: list[WalkStats] = []
    for i, f in enumerate(files, 1):
        recs, stats = extract_replay(f)
        if keep_types:
            recs = [r for r in recs if r["request_type"] in keep_types]
        if args.main1_only:
            recs = [r for r in recs if r["is_start_of_main1"]]
        all_records.extend(recs)
        all_stats.append(stats)
        if i % 10 == 0 or i == len(files):
            print(
                f"  [{i}/{len(files)}] {f.name}: {sum(stats.strategic.values())} strategic / "
                f"{sum(stats.addressed.values())} requests addressed to seat {stats.local_seat}",
                file=sys.stderr,
            )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        for r in all_records:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")

    summary = summarize(all_stats, all_records)
    summary["output_jsonl"] = str(args.out)
    summary["records_written"] = len(all_records)
    print("\n" + json.dumps(summary, indent=2))
    if args.summary_out:
        args.summary_out.parent.mkdir(parents=True, exist_ok=True)
        args.summary_out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"\nsummary -> {args.summary_out}")
    print(f"\nJSONL -> {args.out}  ({len(all_records)} records)")

    errored = [s for s in all_stats if s.errors]
    if errored:
        print(f"\n{len(errored)} replay(s) had errors:", file=sys.stderr)
        for s in errored[:10]:
            print(f"  {s.file}: {s.errors}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
