"""GRE decision-message handlers for arenamcp.

Extracted from arenamcp.gamestate (2026-07-23) — module-level handlers that
translate GRE request messages (mulligan, targets, combat declaration,
pay costs, actions available, etc.) into GameState pending-decision state.
Re-exported from arenamcp.gamestate for backwards compatibility.
"""

import copy
import logging
from typing import TYPE_CHECKING, Any

from arenamcp.gamestate_persistence import mark_match_ended
from arenamcp.gamestate_transforms import (
    _coerce_int,
    _coerce_str_list,
    _ensure_dict_list,
    _ensure_int_list,
    _ensure_list,
)

if TYPE_CHECKING:
    from arenamcp.gamestate import GameState

logger = logging.getLogger(__name__)


def _auto_clear_stale_decision(game_state: "GameState") -> bool:
    """Auto-clear stale decisions whose source is no longer on the stack.

    Returns True if the decision was cleared (snapshot_dirty should be set).
    """
    MIN_DECISION_HOLD_S = 5
    _decision_type = (game_state.decision_context or {}).get("type", "")
    _skip_auto_clear = _decision_type in ("actions_available", "pay_costs")
    if not (
        game_state.pending_decision
        and game_state.pending_decision != "Mulligan"
        and game_state.decision_context
        and not _skip_auto_clear
    ):
        return False

    import time as _time

    decision_age = _time.time() - game_state.decision_timestamp if game_state.decision_timestamp else 999
    source_id = game_state.decision_context.get("source_id")
    should_clear = False
    if source_id is not None:
        still_on_stack = any(obj.instance_id == source_id for obj in game_state.stack)
        if not still_on_stack:
            if decision_age >= MIN_DECISION_HOLD_S:
                should_clear = True
                logger.info(
                    f"Auto-clearing stale decision '{game_state.pending_decision}' "
                    f"(source {source_id} no longer on stack, age={decision_age:.1f}s)"
                )
            else:
                logger.debug(
                    f"Decision '{game_state.pending_decision}' source left stack "
                    f"but holding ({decision_age:.1f}s < {MIN_DECISION_HOLD_S}s)"
                )
    elif game_state.decision_timestamp:
        is_busy = "Combat" in game_state.turn_info.phase or len(game_state.stack) > 0
        no_source_timeout = 25 if is_busy else 15
        if decision_age > no_source_timeout:
            should_clear = True
            logger.info(
                f"Auto-clearing stale decision '{game_state.pending_decision}' "
                f"(no source_id, age={decision_age:.0f}s, "
                f"timeout={no_source_timeout}s)"
            )
    if should_clear:
        game_state.pending_decision = None
        game_state.decision_seat_id = None
        game_state.decision_context = None
        game_state.decision_timestamp = 0
        return True
    return False


def _set_simple_decision(game_state: "GameState", label: str, dec_type: str, raw: dict | None = None) -> None:
    """Set a simple pending decision with optional raw data."""
    import time as _time

    logger.info(f"Captured Decision: {label}")
    game_state.pending_decision = label
    game_state.decision_seat_id = game_state.local_seat_id
    game_state.decision_timestamp = _time.time()
    ctx: dict[str, Any] = {"type": dec_type}
    if raw is not None:
        ctx["raw"] = raw
    game_state.decision_context = ctx


def _resolve_request_source_context(game_state: "GameState", source_id: int) -> dict[str, Any]:
    """Resolve source-card and oracle context for a pending GRE request."""
    if not source_id:
        return {}

    stack_obj = next(
        (obj for obj in game_state.stack if obj.instance_id == source_id),
        None,
    )
    if stack_obj is None:
        stack_obj = game_state.game_objects.get(source_id)
    if stack_obj is None:
        return {}

    resolved: dict[str, Any] = {}
    if stack_obj.parent_instance_id is not None:
        resolved["source_parent_instance_id"] = stack_obj.parent_instance_id
        parent_obj = game_state.game_objects.get(stack_obj.parent_instance_id)
        if parent_obj is not None:
            resolved["source_card"] = game_state._resolve_card_name(parent_obj.grp_id)
            try:
                from arenamcp import server

                parent_info = server.enrich_with_oracle_text(parent_obj.grp_id)
                parent_oracle = str(parent_info.get("oracle_text", "") or "")
                if parent_oracle:
                    resolved["source_card_oracle_text"] = parent_oracle
            except Exception:
                pass

    try:
        from arenamcp import server

        source_info = server.enrich_with_oracle_text(stack_obj.grp_id)
        source_name = str(source_info.get("name", "") or "")
        source_type = str(source_info.get("type_line", "") or "")
        source_oracle = str(source_info.get("oracle_text", "") or "")

        if source_oracle:
            resolved["source_oracle_text"] = source_oracle

        if (
            not resolved.get("source_card")
            and source_name
            and not source_name.lower().startswith("unknown")
            and source_type.lower() != "ability"
        ):
            resolved["source_card"] = source_name
    except Exception:
        pass

    if not resolved.get("source_card"):
        resolved_name = game_state._resolve_card_name(stack_obj.grp_id)
        if resolved_name and not resolved_name.startswith("Card#"):
            resolved["source_card"] = resolved_name

    return resolved


def _handle_decision_message(game_state: "GameState", msg_type: str, msg: dict) -> bool:
    """Handle a GRE decision message. Returns True if snapshot_dirty should be set."""
    import time as _time

    if msg_type in ("GREMessageType_MulliganReq", "GREMessageType_SubmitDeckReq"):
        logger.info(f"Captured Decision: Mulligan Check ({msg_type})")
        if game_state.turn_info.turn_number > 1:
            logger.info(
                f"Mulligan Request detected at Turn {game_state.turn_info.turn_number} -> Resetting Ghost State."
            )
            game_state.reset()
        # ── Phase 1: Extract sideboard from SubmitDeckReq (BO3 between games) ──
        if msg_type == "GREMessageType_SubmitDeckReq":
            submit_req = msg.get("submitDeckReq", {})
            deck_data = submit_req.get("deck", {})
            sideboard = deck_data.get("sideboardCards", deck_data.get("sideboard", []))
            if sideboard:
                game_state.sideboard_cards = list(sideboard)
                logger.info(f"Captured sideboard: {len(sideboard)} cards")
        game_state.pending_decision = "Mulligan"
        game_state.decision_seat_id = game_state.local_seat_id
        game_state.decision_timestamp = _time.time()
        game_state.decision_context = {"type": "mulligan"}
        return False

    elif msg_type == "GREMessageType_IntermissionReq":
        logger.info(
            f"IntermissionReq received (turn {game_state.turn_info.turn_number}) - game over/transition"
        )
        intermission_result = msg.get("intermissionReq", {}).get("result")
        if intermission_result:
            game_state.set_result_from_payload(intermission_result, "IntermissionReq")
        game_state.prepare_for_game_end()
        # Only mark the saved match ended for actual match-end intermissions.
        # In BO3, IntermissionReq also fires between games (scope=MatchScope_Game)
        # and we want resume to keep working through sideboard. Match-end is
        # authoritatively confirmed in server.py's finalMatchResult handler;
        # gate here on scope when present, otherwise treat as match-end (BO1).
        intermission_scope = (intermission_result or {}).get("scope")
        if intermission_scope in (None, "", "MatchScope_Match"):
            mark_match_ended()
        game_state.reset()
        game_state.pending_decision = None
        game_state.decision_seat_id = None
        game_state.decision_context = None
        game_state.decision_timestamp = 0
        return False

    elif msg_type == "GREMessageType_PromptReq":
        prompt_text = msg.get("promptReq", {}).get("prompt", {}).get("text", "Action Required")
        logger.info(f"Captured Decision: Prompt ({prompt_text})")
        if game_state.pending_decision == "Mulligan":
            logger.info("Skipping PromptReq — Mulligan decision already active")
        else:
            game_state.pending_decision = prompt_text
            game_state.decision_timestamp = _time.time()
            game_state.decision_context = {"type": "prompt", "text": prompt_text}
        return False

    elif msg_type == "GREMessageType_SelectTargetsReq":
        req = msg.get("selectTargetsReq", {})
        source_id = _coerce_int(req.get("sourceId", 0), 0)
        source_ctx = _resolve_request_source_context(game_state, source_id)
        source_card = source_ctx.get("source_card") or (f"Ability #{source_id}" if source_id else "spell")
        # The GRE re-sends SelectTargetsReq on every re-present (a rejected
        # submit loop produced 850+ identical messages/min live 2026-06-09).
        # Log repeats at DEBUG so a runaway elsewhere can't flood the log.
        _st_sig = ("SelectTargets", source_id)
        if getattr(game_state, "_last_select_targets_log_sig", None) == _st_sig:
            logger.debug(f"Captured Decision: Select Targets (source: {source_card}) [repeat]")
        else:
            game_state._last_select_targets_log_sig = _st_sig
            logger.info(f"Captured Decision: Select Targets (source: {source_card})")
        game_state.pending_decision = "Select Targets"
        game_state.decision_timestamp = _time.time()
        game_state.decision_context = {
            "type": "target_selection",
            "source_card": source_card,
            "source_id": source_id,
            "targets": req.get("targets", []),
            "raw": dict(req),
            **source_ctx,
        }
        return False

    elif msg_type == "GREMessageType_SelectNReq":
        return _handle_select_n_req(game_state, msg)

    elif msg_type == "GREMessageType_GroupReq":
        req = msg.get("groupReq", {})
        if game_state.turn_info.turn_number < 1:
            logger.info(f"Captured Decision: Mulligan Bottom Cards ({msg_type})")
            game_state.pending_decision = "Mulligan Bottom"
            game_state.decision_seat_id = game_state.local_seat_id
            game_state.decision_timestamp = _time.time()
            game_state.decision_context = {"type": "mulligan_bottom", "raw": dict(req)}
        else:
            _set_simple_decision(game_state, "Group Selection", "group_selection", dict(req))
        return False

    elif msg_type == "GREMessageType_GroupOptionReq":
        req = msg.get("groupOptionReq", {})
        options = _ensure_list(req.get("options", []))
        logger.info(f"Captured Decision: Choose Mode ({len(options)} options)")
        game_state.pending_decision = "Choose Mode"
        game_state.decision_timestamp = _time.time()
        game_state.decision_context = {
            "type": "modal_choice",
            "num_options": len(options),
            "options": options,
        }
        return False

    elif msg_type == "GREMessageType_ActionsAvailableReq":
        return _handle_actions_available(game_state, msg)

    elif msg_type == "GREMessageType_ConnectResp":
        connect_resp = msg.get("connectResp", {})
        deck_msg = connect_resp.get("deckMessage", {})
        deck_cards = _ensure_int_list(deck_msg.get("deckCards", []))
        sideboard_cards = _ensure_int_list(deck_msg.get("sideboardCards", []))
        commander_cards = _ensure_int_list(deck_msg.get("commanderGrpIds", []))
        if deck_cards:
            game_state.deck_cards = deck_cards
            logger.info(f"Captured deck list from ConnectResp: {len(deck_cards)} cards")
        if sideboard_cards:
            game_state.sideboard_cards = sideboard_cards

        all_match_cards = list(set(deck_cards + sideboard_cards + commander_cards))
        if all_match_cards:
            prewarmed_count = game_state.prewarm_card_cache(all_match_cards)
            logger.info(f"Pre-warmed {prewarmed_count} grp_id card lookups from ConnectResp")
        return False

    elif msg_type == "GREMessageType_DeclareAttackersReq":
        req = msg.get("declareAttackersReq", {})
        legal_attackers = _ensure_list(req.get("attackers", req.get("qualifiedAttackers", [])))
        attacker_names, attacker_ids = [], []
        for atk in legal_attackers:
            obj_id = (
                atk
                if isinstance(atk, int)
                else _coerce_int(
                    atk.get("instanceId", atk.get("attackerInstanceId", 0)),
                    0,
                )
            )
            obj = game_state.game_objects.get(obj_id)
            if obj:
                attacker_names.append(game_state._resolve_card_name(obj.grp_id))
                attacker_ids.append(obj_id)
        logger.info(f"Captured Decision: Declare Attackers ({len(attacker_names)} legal)")
        game_state.pending_decision = "Declare Attackers"
        game_state.decision_timestamp = _time.time()
        game_state.decision_context = {
            "type": "declare_attackers",
            "legal_attackers": attacker_names,
            "legal_attacker_ids": attacker_ids,
            "raw_attackers": legal_attackers,
        }
        return False

    elif msg_type == "GREMessageType_DeclareBlockersReq":
        req = msg.get("declareBlockersReq", {})
        legal_blockers = _ensure_list(req.get("blockers", req.get("qualifiedBlockers", [])))
        blocker_names, blocker_ids = [], []
        attacker_id_set: set[int] = set()
        for blk in legal_blockers:
            obj_id = (
                blk
                if isinstance(blk, int)
                else _coerce_int(
                    blk.get("instanceId", blk.get("blockerInstanceId", 0)),
                    0,
                )
            )
            obj = game_state.game_objects.get(obj_id)
            if obj:
                blocker_names.append(game_state._resolve_card_name(obj.grp_id))
                blocker_ids.append(obj_id)
            # Each Blocker entry names the attackers it may legally block
            # (GreProtobuf Blocker.attackerInstanceIds). Their union is the
            # authoritative attacker set for this combat — the only place
            # the log states it during DeclareBlockers (issue #420).
            if isinstance(blk, dict):
                attacker_id_set.update(_ensure_int_list(blk.get("attackerInstanceIds", [])))

        # Clear stale is_attacking on all objects before flagging current attackers
        for obj in game_state.game_objects.values():
            if obj.is_attacking and obj.instance_id not in attacker_id_set:
                obj.is_attacking = False

        attacker_names, attacker_ids = [], []
        for atk_id in sorted(attacker_id_set):
            atk_obj = game_state.game_objects.get(atk_id)
            if atk_obj:
                attacker_names.append(game_state._resolve_card_name(atk_obj.grp_id))
                attacker_ids.append(atk_id)
                # Flag on the object so snapshots/combat analysis see the
                # attacker even if the attackState annotation was missed.
                atk_obj.is_attacking = True
        logger.info(
            f"Captured Decision: Declare Blockers ({len(blocker_names)} legal, {len(attacker_ids)} attackers)"
        )
        game_state.pending_decision = "Declare Blockers"
        game_state.decision_timestamp = _time.time()
        game_state.decision_context = {
            "type": "declare_blockers",
            "legal_blockers": blocker_names,
            "legal_blocker_ids": blocker_ids,
            "raw_blockers": legal_blockers,
            "attacker_ids": attacker_ids,
            "attackers": attacker_names,
        }
        return False

    elif msg_type == "GREMessageType_AssignDamageReq":
        _set_simple_decision(
            game_state, "Assign Damage", "assign_damage", dict(msg.get("assignDamageReq", {}))
        )
        return False

    elif msg_type == "GREMessageType_OrderCombatDamageReq":
        req = msg.get("orderCombatDamageReq", msg.get("orderDamageReq", {}))
        _set_simple_decision(game_state, "Order Combat Damage", "order_combat_damage", dict(req))
        return False

    elif msg_type == "GREMessageType_PayCostsReq":
        return _handle_pay_costs(game_state, msg)

    elif msg_type == "GREMessageType_SearchReq":
        req = msg.get("searchReq", {})
        logger.info(f"Captured Decision: Search (zone {req.get('zoneId', 0)})")
        game_state.pending_decision = "Search Library"
        game_state.decision_timestamp = _time.time()
        game_state.decision_context = {"type": "search", "zone_id": req.get("zoneId", 0), "raw": dict(req)}
        return False

    elif msg_type == "GREMessageType_DistributionReq":
        req = msg.get("distributionReq", {})
        total = req.get("amount", req.get("total", req.get("max", 0)))
        source_id = req.get("sourceId", 0)
        source_obj = game_state.game_objects.get(source_id)
        source_name = "Unknown"
        if source_obj:
            source_name = game_state._resolve_card_name(source_obj.grp_id)
        if not source_name or source_name.startswith("Card") or source_name == "Unknown":
            for zone in ("battlefield", "hand", "stack", "graveyard"):
                for c in getattr(game_state, zone, []):
                    if c.get("instance_id") == source_id:
                        source_name = c.get("name", source_name)
                        break
        logger.info(f"Captured Decision: Distribute {total} (source: {source_name})")
        game_state.pending_decision = "Distribute"
        game_state.decision_timestamp = _time.time()
        game_state.decision_context = {
            "type": "distribution",
            "source_card": source_name,
            "source_id": source_id,
            "total": total,
            "raw": dict(req),
        }
        return False

    elif msg_type == "GREMessageType_NumericInputReq":
        req = msg.get("numericInputReq", {})
        source_id = req.get("sourceId", 0)
        source_obj = game_state.game_objects.get(source_id)
        source_name = "Unknown"
        if source_obj:
            source_name = game_state._resolve_card_name(source_obj.grp_id)
        if not source_name or source_name.startswith("Card") or source_name == "Unknown":
            for zone in ("battlefield", "hand", "stack", "graveyard"):
                for c in getattr(game_state, zone, []):
                    if c.get("instance_id") == source_id:
                        source_name = c.get("name", source_name)
                        break
        logger.info(
            f"Captured Decision: Numeric Input for {source_name} ({req.get('min', 0)}-{req.get('max', 0)})"
        )
        game_state.pending_decision = "Choose Number"
        game_state.decision_timestamp = _time.time()
        game_state.decision_context = {
            "type": "numeric_input",
            "source_card": source_name,
            "source_id": source_id,
            "min": req.get("min", 0),
            "max": req.get("max", 0),
        }
        return False

    elif msg_type == "GREMessageType_ChooseStartingPlayerReq":
        _set_simple_decision(game_state, "Choose Play/Draw", "choose_starting_player")
        return False

    elif msg_type == "GREMessageType_SelectReplacementReq":
        _set_simple_decision(
            game_state, "Select Replacement", "select_replacement", dict(msg.get("selectReplacementReq", {}))
        )
        return False

    elif msg_type == "GREMessageType_SelectNGroupReq":
        _set_simple_decision(
            game_state, "Select from Group", "select_n_group", dict(msg.get("selectNGroupReq", {}))
        )
        return False

    elif msg_type == "GREMessageType_SelectFromGroupsReq":
        _set_simple_decision(
            game_state, "Select from Groups", "select_from_groups", dict(msg.get("selectFromGroupsReq", {}))
        )
        return False

    elif msg_type == "GREMessageType_SearchFromGroupsReq":
        _set_simple_decision(
            game_state, "Search from Groups", "search_from_groups", dict(msg.get("searchFromGroupsReq", {}))
        )
        return False

    elif msg_type == "GREMessageType_CastingTimeOptionsReq":
        _set_simple_decision(
            game_state,
            "Choose Casting Option",
            "casting_time_options",
            dict(msg.get("castingTimeOptionsReq", {})),
        )
        return False

    elif msg_type == "GREMessageType_SelectCountersReq":
        _set_simple_decision(
            game_state, "Select Counters", "select_counters", dict(msg.get("selectCountersReq", {}))
        )
        return False

    elif msg_type == "GREMessageType_RevealHandReq":
        req = msg.get("revealHandReq", {})
        logger.info(f"Hand reveal for seats: {req.get('systemSeatIds', [])}")
        return False

    elif msg_type == "GREMessageType_OrderReq":
        _set_simple_decision(game_state, "Order Triggers", "order_triggers", dict(msg.get("orderReq", {})))
        return False

    elif msg_type == "GREMessageType_GatherReq":
        _set_simple_decision(game_state, "Gather", "gather", dict(msg.get("gatherReq", {})))
        return False

    elif msg_type == "GREMessageType_OptionalActionMessage":
        opt = msg.get("optionalActionMessage", msg.get("prompt", {}))
        prompt_text = ""
        if isinstance(opt, dict):
            prompt_text = (
                opt.get("prompt", {}).get("text", "")
                if isinstance(opt.get("prompt"), dict)
                else str(opt.get("prompt", ""))
            )
        logger.info(f"Captured Decision: Optional Action ({prompt_text[:60]})")
        game_state.pending_decision = "Optional Action"
        game_state.decision_timestamp = _time.time()
        game_state.decision_context = {
            "type": "optional_action",
            "prompt": prompt_text,
            "raw": {k: v for k, v in msg.items() if k != "type"},
        }
        return False

    return False  # Unhandled message type


def _handle_select_n_req(game_state: "GameState", msg: dict) -> bool:
    """Handle SelectNReq messages (scry, discard, choose creature, etc.)."""
    import time as _time

    req = msg.get("selectNReq", {})
    context_data = req.get("context", {})
    num_to_select = _coerce_int(req.get("count", 1), 1)
    min_select = _coerce_int(req.get("minCount", num_to_select), num_to_select)
    max_select = _coerce_int(req.get("maxCount", num_to_select), num_to_select)
    option_ids = _ensure_int_list(req.get("ids", []))

    context_str = str(context_data).lower()
    prior_prompt = ""
    if (
        game_state.pending_decision
        and game_state.decision_context
        and game_state.decision_context.get("type") == "prompt"
    ):
        prior_prompt = game_state.decision_context.get("text", "").lower()
        context_str = f"{context_str} {prior_prompt}"

    # Determine selection type
    _type_map = [
        ("discard", "discard", "Discard"),
        ("sacrifice", "sacrifice", "Sacrifice"),
        ("exile", "exile", "Exile"),
        ("destroy", "destroy", "Destroy"),
        ("return", "return", "Return"),
        ("scry", "scry", "Scry"),
        ("surveil", "surveil", "Surveil"),
        ("mill", "mill", "Mill"),
        ("explore", "explore", "Explore"),
        ("creature", "choose_creature", "Choose Creature"),
        ("land", "choose_land", "Choose Land"),
        ("enchantment", "choose_enchantment", "Choose Enchantment"),
        ("artifact", "choose_artifact", "Choose Artifact"),
        ("permanent", "choose_permanent", "Choose Permanent"),
        ("choose", "choose", "Choose"),
    ]
    selection_type = "select_n"
    decision_text = "Select Items"
    for keyword, sel_type, dec_text in _type_map:
        if keyword in context_str:
            selection_type = sel_type
            decision_text = dec_text
            break
    else:
        if prior_prompt:
            decision_text = game_state.decision_context.get("text", "Select Items")

    # Resolve option IDs to card names (handles both game objects and direct grp_ids from library searches)
    option_cards: list[str] = []
    for oid in option_ids[:20]:
        grp_id = 0
        obj = game_state.game_objects.get(oid)
        if obj and obj.grp_id:
            grp_id = obj.grp_id
        else:
            grp_id = oid

        if grp_id:
            try:
                from arenamcp import server

                info = server.get_card_info(grp_id)
                name = info.get("name") if info else None
                if name and not name.startswith("Card#"):
                    option_cards.append(name)
                else:
                    resolved_name = game_state._resolve_card_name(grp_id)
                    option_cards.append(resolved_name if resolved_name else f"Card#{grp_id}")
            except Exception:
                option_cards.append(f"Card#{grp_id}")

    logger.info(
        f"Captured Decision: {decision_text} ({num_to_select} items, type={selection_type}, options={option_cards or option_ids[:5]})"
    )
    game_state.pending_decision = decision_text
    game_state.decision_timestamp = _time.time()
    game_state.decision_context = {
        "type": selection_type,
        "count": num_to_select,
        "min": min_select,
        "max": max_select,
        "context_raw": context_data,
        "prior_prompt": prior_prompt or None,
        "option_cards": option_cards or None,
        # Preserve raw option IDs and the request's IdType so the
        # autopilot's GRE bridge path knows whether to submit instance
        # IDs (e.g. milled-card pick from a library reveal) or grp IDs
        # (e.g. printing-based choice).
        "option_ids": option_ids,
        "id_type": req.get("idType") or "",
    }
    return False


_MANA_COLOR_SYMBOL = {
    "ManaColor_White": "W",
    "ManaColor_Blue": "U",
    "ManaColor_Black": "B",
    "ManaColor_Red": "R",
    "ManaColor_Green": "G",
    "ManaColor_Colorless": "C",
}


def _braced_mana_cost(mana_cost_entries: list[dict]) -> str:
    """Render a GRE structured ``manaCost`` array as a Scryfall-style cost.

    ``[{color: [Green], count: 1}, {count: 4}]`` becomes ``"{4}{G}"`` so it
    can be fed to ``RulesEngine._can_afford``. Returns "" when the action
    carries no mana cost at all (tap/sacrifice-only abilities), which callers
    must treat as free rather than unaffordable.
    """
    generic = 0
    colored: list[str] = []
    for entry in mana_cost_entries:
        colors = _coerce_str_list(entry.get("color", []))
        count = _coerce_int(entry.get("count", 1), 1)
        if count <= 0:
            continue
        symbols = [_MANA_COLOR_SYMBOL[c] for c in colors if c in _MANA_COLOR_SYMBOL]
        if not symbols:
            # Generic, ManaColor_Any, or an unmapped colour — payable by
            # anything, so it only contributes to the generic portion.
            generic += count
            continue
        if len(symbols) == 1:
            colored.extend([f"{{{symbols[0]}}}"] * count)
        else:
            colored.extend([f"{{{'/'.join(symbols)}}}"] * count)
    parts = ([f"{{{generic}}}"] if generic else []) + colored
    return "".join(parts)


def _build_rules_engine_snapshot(game_state: "GameState") -> dict:
    """Build the minimal snapshot dict shape RulesEngine mana helpers expect."""
    from arenamcp import server

    battlefield = []
    for obj in game_state.battlefield:
        entry = {
            "owner_seat_id": obj.owner_seat_id,
            "is_tapped": obj.is_tapped,
            "turn_entered_battlefield": obj.turn_entered_battlefield,
            "name": "",
            "type_line": "",
            "oracle_text": "",
        }
        if obj.grp_id:
            try:
                info = server.get_card_info(obj.grp_id)
                entry["name"] = info.get("name", "") or ""
                entry["type_line"] = info.get("type_line", "") or ""
                entry["oracle_text"] = info.get("oracle_text", "") or ""
            except Exception:
                pass
        battlefield.append(entry)
    local_player = game_state.players.get(game_state.local_seat_id)
    floating = dict(local_player.mana_pool) if local_player and local_player.mana_pool else {}
    return {
        "battlefield": battlefield,
        "turn": {"turn_number": game_state.turn_info.turn_number},
        "floating_mana": floating,
    }


def _handle_actions_available(game_state: "GameState", msg: dict) -> bool:
    """Handle ActionsAvailableReq messages. Returns True if snapshot_dirty."""
    import time as _time

    req = msg.get("actionsAvailableReq", {})
    raw_actions = _ensure_dict_list(req.get("actions", []))

    # ── Phase 1: Enrich raw actions with AutoTap + ability metadata ──
    enriched_actions = []
    for action in raw_actions:
        enriched = copy.deepcopy(action)
        # Extract AutoTap castability — the game engine's own mana solver
        autotap = action.get("autoTapSolution")
        if isinstance(autotap, dict):
            enriched["_castable"] = True
            # Parse tap actions if present
            tap_actions = _ensure_dict_list(autotap.get("autoTapActions", []))
            if tap_actions:
                enriched["_autotap_lands"] = [
                    {"instanceId": ta.get("instanceId"), "mana": ta.get("manaProduced", "")}
                    for ta in tap_actions
                ]
        elif action.get("actionType") in ("ActionType_Cast", "ActionType_Activate"):
            # No autotap solution but it's a spell/ability — may need manual mana
            enriched["_castable"] = action.get("assumeCanBePaidFor", False)

        # Extract ability metadata
        if action.get("abilityGrpId"):
            enriched["_ability_grp_id"] = action["abilityGrpId"]
        if action.get("sourceId"):
            enriched["_source_id"] = action["sourceId"]
        if action.get("alternativeGrpId"):
            enriched["_alternative_grp_id"] = action["alternativeGrpId"]

        # Mana cost structured extraction
        mana_cost = _ensure_dict_list(action.get("manaCost", []))
        if mana_cost:
            _MANA_ABBREV = {
                "ManaColor_White": "W",
                "ManaColor_Blue": "U",
                "ManaColor_Black": "B",
                "ManaColor_Red": "R",
                "ManaColor_Green": "G",
                "ManaColor_Colorless": "C",
                "ManaColor_Any": "X",
            }
            cost_parts = []
            for mc in mana_cost:
                colors = _coerce_str_list(mc.get("color", []))
                count = _coerce_int(mc.get("count", 1), 1)
                if colors:
                    symbols = [_MANA_ABBREV.get(c, c) for c in colors]
                    cost_parts.append(f"{count}{''.join(symbols)}")
                else:
                    cost_parts.append(str(count))
            enriched["_mana_cost_str"] = "".join(cost_parts)

        enriched_actions.append(enriched)

    game_state.legal_actions_raw = enriched_actions

    legal_list = []
    rules_mana_pool = None
    for action_idx, action in enumerate(raw_actions):
        atype = action.get("actionType", "")
        if atype == "ActionType_Pass":
            legal_list.append("Pass")
        elif atype == "ActionType_Play":
            name = "Land"
            if action.get("grpId"):
                try:
                    from arenamcp import server

                    info = server.get_card_info(action["grpId"])
                    name = info.get("name", "Land")
                except Exception:
                    pass
            legal_list.append(f"Play Land: {name}")
        elif atype == "ActionType_Cast":
            name = "Spell"
            info = {}
            if action.get("grpId"):
                try:
                    from arenamcp import server

                    info = server.get_card_info(action["grpId"])
                    name = info.get("name", "Spell")
                except Exception:
                    pass
            # Include castability from AutoTap, manaPaymentOptions, or RulesEngine affordability
            castable = (
                action.get("autoTapSolution") is not None
                or action.get("manaPaymentOptions") is not None
                or action.get("manaPaymentOptionsCount", 0) > 0
            )
            if not castable and info.get("mana_cost"):
                try:
                    from arenamcp.rules_engine import RulesEngine

                    if rules_mana_pool is None:
                        rules_mana_pool = RulesEngine._get_mana_pool(
                            _build_rules_engine_snapshot(game_state),
                            game_state.local_seat_id,
                        )
                    castable = RulesEngine._can_afford(info.get("mana_cost", ""), rules_mana_pool)
                except Exception:
                    logger.debug("RulesEngine affordability fallback failed", exc_info=True)

            suffix = " [OK]" if castable else ""
            legal_list.append(f"Cast {name}{suffix}")
        elif atype == "ActionType_Activate":
            name = ""
            if action.get("grpId"):
                try:
                    from arenamcp import server

                    info = server.get_card_info(action["grpId"])
                    name = info.get("name", "")
                except Exception:
                    pass
            payable = (
                action.get("autoTapSolution") is not None
                or action.get("manaPaymentOptions") is not None
                or action.get("manaPaymentOptionsCount", 0) > 0
            )
            # The GRE offers an activation whenever the ability is legal to
            # announce — it does not promise the cost is payable, and aborting
            # a half-paid activation is a real tempo loss. When it hands us no
            # payment solution, verify the ability's own mana cost ourselves.
            # An ability with no mana cost (tap/sacrifice only, e.g. cracking a
            # fetch land) stays unflagged: it is free, not unaffordable.
            suffix = " [OK]" if payable else ""
            if not payable:
                ability_cost = _braced_mana_cost(_ensure_dict_list(action.get("manaCost", [])))
                if ability_cost:
                    try:
                        from arenamcp.rules_engine import RulesEngine

                        if rules_mana_pool is None:
                            rules_mana_pool = RulesEngine._get_mana_pool(
                                _build_rules_engine_snapshot(game_state),
                                game_state.local_seat_id,
                            )
                        if RulesEngine._can_afford(ability_cost, rules_mana_pool):
                            suffix = " [OK]"
                        else:
                            suffix = f" [NEED:{ability_cost}]"
                            enriched_actions[action_idx]["_unaffordable"] = True
                    except Exception:
                        logger.debug("Ability affordability check failed", exc_info=True)
            if name:
                legal_list.append(f"Activate Ability: {name}{suffix}")
            else:
                legal_list.append(f"Activate Ability{suffix}")
        else:
            legal_list.append(f"Action: {atype.replace('ActionType_', '')}")

    if legal_list:
        game_state.legal_actions = legal_list
        logger.info(f"Captured {len(legal_list)} legal actions from GRE: {legal_list}")

    if raw_actions:
        if game_state.local_seat_id is not None:
            game_state.decision_seat_id = game_state.local_seat_id
        action_types = {a.get("actionType", "") for a in raw_actions}
        if action_types == {"ActionType_Pass"}:
            decision_label = "Priority (Pass Only)"
        elif "ActionType_Cast" in action_types or "ActionType_Play" in action_types:
            decision_label = "Priority"
        elif "ActionType_AttackWithGroup" in action_types or "ActionType_Attack" in action_types:
            decision_label = "Declare Attackers"
        elif "ActionType_BlockWithGroup" in action_types or "ActionType_Block" in action_types:
            decision_label = "Declare Blockers"
        else:
            decision_label = "Choose Action"
        game_state.pending_decision = decision_label
        game_state.decision_timestamp = _time.time()
        game_state.decision_context = {
            "type": "actions_available",
            "num_actions": len(raw_actions),
            "action_types": sorted(action_types),
        }
        return True
    return False


def _handle_pay_costs(game_state: "GameState", msg: dict) -> bool:
    """Handle PayCostsReq messages."""
    import time as _time

    req = msg.get("payCostsReq", {})
    mana_cost = _ensure_dict_list(req.get("manaCost", []))
    source_id = 0
    for mc in mana_cost:
        oid = _coerce_int(mc.get("objectId", 0), 0)
        if oid:
            source_id = oid
            break
    source_obj = game_state.game_objects.get(source_id) if source_id else None
    # PayCosts for an activated ability names the *ability* object, whose
    # grp_id is an ability id — resolve through it to the parent permanent
    # rather than emitting "Pay costs for Card#136588" (#407, #483).
    source_name = game_state._resolve_object_display_name(source_obj) if source_obj else "Unknown"

    _MANA_ABBREV = {
        "ManaColor_White": "W",
        "ManaColor_Blue": "U",
        "ManaColor_Black": "B",
        "ManaColor_Red": "R",
        "ManaColor_Green": "G",
        "ManaColor_Colorless": "C",
        "ManaColor_Any": "Any",
        "ManaColor_Generic": "Generic",
    }
    mana_requirements = {
        "generic": 0,
        "W": 0,
        "U": 0,
        "B": 0,
        "R": 0,
        "G": 0,
        "C": 0,
        "Any": 0,
    }
    mana_parts = []
    for mc in mana_cost:
        colors = _coerce_str_list(mc.get("color", []))
        count = _coerce_int(mc.get("count", 1), 1)
        if colors:
            symbols = [_MANA_ABBREV.get(c, c) for c in colors]
            if all(symbol == "Generic" for symbol in symbols):
                mana_requirements["generic"] += count
            elif len(symbols) == 1 and symbols[0] in mana_requirements:
                mana_requirements[symbols[0]] += count
            mana_parts.append(f"{count}x{''.join(symbols)}")
        else:
            mana_requirements["generic"] += count
            mana_parts.append(f"{count}")
    mana_str = ", ".join(mana_parts) if mana_parts else "unknown"

    # ── Phase 1: Extract full AutoTap solution ──
    autotap_req = req.get("autoTapActionsReq", {})
    if not isinstance(autotap_req, dict):
        autotap_req = {}
    autotap_solutions = _ensure_dict_list(autotap_req.get("autoTapSolutions", []))
    has_autotap = bool(autotap_solutions)
    autotap_info = None
    if has_autotap:
        first_solution = autotap_solutions[0]
        tap_actions = _ensure_dict_list(first_solution.get("autoTapActions", []))
        autotap_info = {
            "lands_to_tap": [
                {"instanceId": ta.get("instanceId"), "mana": ta.get("manaProduced", "")} for ta in tap_actions
            ],
            "num_lands": len(tap_actions),
        }

    # PayCostsReq replaces the previous priority window. Keeping the old
    # ActionsAvailable data around causes the planner to try to cast again.
    if game_state.legal_actions or game_state.legal_actions_raw:
        logger.info(
            "Clearing stale legal actions for Pay Costs (%d summarized, %d raw)",
            len(game_state.legal_actions),
            len(game_state.legal_actions_raw),
        )
        game_state.legal_actions = []
        game_state.legal_actions_raw = []

    logger.info(
        f"Captured Decision: Pay Costs (source: {source_name}, mana: {mana_str}, autotap={has_autotap})"
    )
    game_state.pending_decision = "Pay Costs"
    game_state.decision_seat_id = game_state.local_seat_id
    game_state.decision_timestamp = _time.time()
    game_state.decision_context = {
        "type": "pay_costs",
        "source_card": source_name,
        "source_id": source_id if source_id else None,
        "mana_cost": mana_str,
        "has_autotap": has_autotap,
        "mana_requirements": {k: v for k, v in mana_requirements.items() if v},
        "autotap_solution": autotap_info,
    }
    return False


# Known decision Req types handled by _handle_decision_message
_KNOWN_REQ_TYPES = frozenset(
    [
        "GREMessageType_MulliganReq",
        "GREMessageType_SubmitDeckReq",
        "GREMessageType_IntermissionReq",
        "GREMessageType_PromptReq",
        "GREMessageType_SelectTargetsReq",
        "GREMessageType_SelectNReq",
        "GREMessageType_GroupReq",
        "GREMessageType_GroupOptionReq",
        "GREMessageType_ActionsAvailableReq",
        "GREMessageType_DeclareAttackersReq",
        "GREMessageType_DeclareBlockersReq",
        "GREMessageType_AssignDamageReq",
        "GREMessageType_OrderCombatDamageReq",
        "GREMessageType_PayCostsReq",
        "GREMessageType_SearchReq",
        "GREMessageType_DistributionReq",
        "GREMessageType_NumericInputReq",
        "GREMessageType_ChooseStartingPlayerReq",
        "GREMessageType_SelectReplacementReq",
        "GREMessageType_SelectNGroupReq",
        "GREMessageType_SelectFromGroupsReq",
        "GREMessageType_SearchFromGroupsReq",
        "GREMessageType_CastingTimeOptionsReq",
        "GREMessageType_SelectCountersReq",
        "GREMessageType_RevealHandReq",
        "GREMessageType_OrderReq",
        "GREMessageType_GatherReq",
        "GREMessageType_ConnectResp",
        "GREMessageType_OptionalActionMessage",
    ]
)

_DECISION_RESPONSE_TYPES = frozenset(
    [
        "GREMessageType_SelectTargetsResp",
        "GREMessageType_SubmitTargetsResp",
        "GREMessageType_SelectNResp",
        "GREMessageType_GroupOptionResp",
        "GREMessageType_GroupResp",
        "GREMessageType_OptionalActionResp",
        "GREMessageType_SubmitDeckResp",
        "GREMessageType_PromptResp",
        "GREMessageType_SubmitAttackersResp",
        "GREMessageType_SubmitBlockersResp",
        "GREMessageType_AssignDamageConfirmation",
        "GREMessageType_OrderDamageConfirmation",
    ]
)
