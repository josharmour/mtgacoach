"""Priority play and line-search fallback picks without an LLM response."""

from __future__ import annotations

import logging
import re
from typing import Any

from arenamcp.action_types import (
    FALLBACK_LLM_UNAVAILABLE,
    ActionPlan,
    ActionType,
    GameAction,
    _as_int,
)
from arenamcp.cast_notes import unpayable_graveyard_flashback
from arenamcp.decisions import SELECT_N_DECLINE
from arenamcp.ward import untapped_land_drop

logger = logging.getLogger("arenamcp.action_planner")

# Cast roles that need a target or a moment only the model would pick
# (counterspells, combat tricks): never cast blind on our own main phase.
_REACTIVE_CAST_ROLES = frozenset({"counter", "pump"})


def _legal_payable(game_state: dict[str, Any], legal_actions: list[str]) -> dict[int, bool | None]:
    """Hand instance id -> Arena's [OK] verdict for each "Cast X" in the legacy legal-action strings."""
    by_name: dict[str, int] = {}
    for card in game_state.get("hand") or []:
        if isinstance(card, dict) and card.get("name") and isinstance(card.get("instance_id"), int):
            by_name.setdefault(str(card["name"]).casefold(), card["instance_id"])
    verdicts: dict[int, bool | None] = {}
    for entry in legal_actions:
        text = str(entry or "").strip()
        if not text.lower().startswith("cast "):
            continue
        name = re.sub(r"\s*\[[^\]]*\]", "", text[5:]).strip().casefold()
        iid = by_name.get(name)
        if iid is not None:
            verdicts[iid] = "[ok]" in text.lower()
    return verdicts


def _option_prints(decision: Any, game_state: dict[str, Any] | None = None) -> tuple[list[int], bool]:
    """Keep all offered prints; only known non-card buttons or resolved targets can omit one."""
    out: list[int] = []
    all_bound = True
    zone_by_iid: dict[int, dict[str, Any]] = {}
    if isinstance(game_state, dict):
        for zone_name in ("battlefield", "stack", "graveyard", "exile", "hand", "command"):
            for card in game_state.get(zone_name) or ():
                if isinstance(card, dict) and type(card.get("instance_id")) is int:
                    zone_by_iid.setdefault(card["instance_id"], card)
    for option in getattr(decision, "options", ()) or ():
        meta = getattr(option, "meta", None) or {}
        oid = str(getattr(option, "option_id", "") or "")
        bound = oid in {"pass", "cancel", "done", SELECT_N_DECLINE} or oid.startswith(("mull:", "optional:"))
        for key in ("grpId", "grp_id", "card_grp_id"):
            value = meta.get(key)
            if type(value) is int and value > 4:
                bound = True
                if value not in out:
                    out.append(value)
        if not bound and (tgt_match := re.fullmatch(r"tgt:(\d+)", oid)) is not None:
            obj = zone_by_iid.get(int(tgt_match.group(1)))
            if obj is not None:
                if obj.get("is_token"):
                    bound = True
                else:
                    gid = obj.get("grp_id")
                    if type(gid) is int and gid > 4:
                        bound = True
                        if gid not in out:
                            out.append(gid)
            elif oid in {"tgt:1", "tgt:2"}:
                bound = True
        all_bound = all_bound and bound
    return out, all_bound


def _local_seat(state: dict[str, Any]) -> Any:
    seat = state.get("local_seat_id")
    if seat is None:
        seat = next(
            (
                player.get("seat_id")
                for player in state.get("players") or []
                if isinstance(player, dict) and player.get("is_local")
            ),
            None,
        )
    return seat


def _opponent_tapped_out(state: dict[str, Any]) -> bool:
    """Every land the opponent controls is tapped (they hold no mana for a trick or a flash blocker)."""
    local = _local_seat(state)
    lands = [
        card
        for card in state.get("battlefield") or []
        if local is not None
        and _controller_seat(card) not in (None, local)
        and (
            "land" in str(card.get("type_line") or "").lower()
            or "CardType_Land" in (card.get("card_types") or [])
        )
    ]
    return bool(lands) and all(card.get("is_tapped") for card in lands)


def _model_reasoning(plan: ActionPlan, action: GameAction) -> str:
    """What the plan said for ``action`` before a guard rewrites it: its reasoning and the plan's strategy."""
    if action.model_reasoning:
        return action.model_reasoning
    parts = [str(action.reasoning or "").strip(), str(plan.overall_strategy or "").strip()]
    return " | ".join(dict.fromkeys(part for part in parts if part))


def _lethal_this_turn(state: dict[str, Any]) -> bool | None:
    """Whether the board math finds our kill this turn; None when the board cannot be assessed."""
    try:
        from arenamcp import board_assessment

        facts = board_assessment.assess(state)
    except Exception as error:
        logger.debug("lethal check: no assessment (%s)", error)
        return None
    if facts is None:
        return None
    best = facts.lines[0] if getattr(facts, "lines", None) else None
    return bool(facts.lethal_now or facts.posture == "lethal" or (best is not None and best.win_at == 1))


def _local_turn(state: dict[str, Any]) -> bool:
    turn = state.get("turn") or {}
    local = _local_seat(state)
    return local is not None and turn.get("active_player") == local


def _own_main_phase_with_empty_stack(state: dict[str, Any]) -> bool:
    turn = state.get("turn") or {}
    local = _local_seat(state)
    if local is None or turn.get("active_player") != local:
        return False
    return "main" in str(turn.get("phase") or "").lower() and not (state.get("stack") or [])


def own_stack_only(decision: Any, state: dict[str, Any]) -> str:
    """The name of our spell on top when everything on the stack is ours; '' otherwise."""
    import arenamcp.action_planner as _ap
    from arenamcp.mulligan_policy import hand_card

    local = _local_seat(state)
    stack = state.get("stack") or []
    if local is None or not stack:
        return ""
    if any(not isinstance(entry, dict) or _controller_seat(entry) != local for entry in stack):
        return ""
    for option in getattr(decision, "options", ()) or ():
        meta = option.meta or {}
        kind = str(meta.get("actionType") or "").removeprefix("ActionType_")
        if kind == "Play":
            return ""
        if kind == "Cast":
            card = _ap.find_source(state, meta)
            if card.get("type_line") and not hand_card(card).instant_speed:
                return ""
    return str(stack[0].get("name") or "spell")


def _controller_seat(card: dict[str, Any]) -> Any:
    return card.get("controller_seat_id") or card.get("owner_seat_id")


def _no_board_math_reason(decision: Any, state: dict[str, Any]) -> str:
    """Why the board-math fallback only drops a land or passes in this window."""
    turn = state.get("turn") or {}
    local = _local_seat(state)
    main = "main" in str(turn.get("phase") or "").lower()
    if local is not None and turn.get("active_player") == local and main:
        own = own_stack_only(decision, state)
        if own:
            return f"our own {own} is on the stack, so pass and let it resolve"
        if state.get("stack"):
            return "with a spell on the stack only a pass is safe"
    return "outside our main phase only a land drop or a pass is safe"


def _line_text(step: Any) -> str:
    """Speakable board-math line for this turn ("plays Island, then casts A and B")."""
    parts = []
    if step.land:
        parts.append(f"plays {step.land}")
    casts = list(step.casts)
    if casts:
        parts.append("casts " + (", ".join(casts[:-1]) + " and " + casts[-1] if len(casts) > 1 else casts[0]))
    return "the board-math line " + (", then ".join(parts) if parts else "casts nothing payable this turn")


def board_math_option_pick(decision: Any, game_state: dict[str, Any]) -> tuple[list[str], str]:
    """A priority-window choice without the model: land drop, then the board-math line."""
    import arenamcp.action_planner as _ap
    from arenamcp.board_assessment import has_unmodelled_additional_cost
    from arenamcp.mana import mana_cost_to_cmc

    fallback = _ap.ActionPlanner.deterministic_option_pick(decision)
    if decision.request_type != "ActionsAvailable" or not _ap._own_main_phase_with_empty_stack(game_state):
        return fallback, _no_board_math_reason(decision, game_state)
    assessment, option_role = None, None
    step, plan_text, scheduled = None, "", set()
    greedy: set[str] = set()
    try:
        from arenamcp.board_assessment import assess, option_role

        assessment = assess(game_state)
        if assessment is not None and assessment.our_turn and assessment.lookahead:
            first = assessment.lookahead[0]
            plan_text, scheduled = _line_text(first), set(first.casts)
            step = first
            if not scheduled:
                baseline = getattr(getattr(assessment, "line_search", None), "baseline", None)
                steps = getattr(baseline, "steps", None) or ()
                greedy = set(getattr(steps[0], "casts", ()) or ()) if steps else set()
    except Exception as error:
        logger.debug("board-math fallback: no assessment: %s", error)
        assessment = None
    options = [option for option in decision.options if option.payable is not False]
    deferred: set[str] = set()
    phase = str((game_state.get("turn") or {}).get("phase") or "")
    if step is not None and getattr(step, "attack", None) and "main1" in phase.lower().replace("_", ""):
        deferred = set(getattr(step, "post_combat", None) or ())

    def name_of(option: Any) -> str:
        return str(_ap.find_source(game_state, option.meta or {}).get("name") or "")

    lands = [option for option in options if (option.meta or {}).get("actionType") == "ActionType_Play"]
    if lands:
        wanted = str(step.land or "") if step is not None else ""
        pick = (
            next((option for option in lands if wanted and name_of(option) == wanted), None)
            or next((option for option in lands if untapped_land_drop(game_state, [option])), None)
            or lands[0]
        )
        from arenamcp.landfall_order import landfall_first

        first = landfall_first(decision, game_state, pick.option_id, planned_names=scheduled | greedy)
        if first is not None:
            return [first.option_id], first.reason
        return [pick.option_id], plan_text or "no board assessment, so the land drop comes first"

    picks: list[tuple[int, Any]] = []
    backup_picks: list[tuple[int, Any]] = []
    for option in options:
        meta = option.meta or {}
        if option.payable is not True or meta.get("actionType") != "ActionType_Cast":
            continue
        card = _ap.find_source(game_state, meta)
        name = str(card.get("name") or "")
        cost = str(card.get("mana_cost") or "")
        if not name or "{x}" in cost.lower():
            continue
        try:
            role = option_role(option, game_state) if option_role is not None else "other"
        except Exception:
            role = "other"
        if role in _REACTIVE_CAST_ROLES or name in deferred:
            continue
        if (
            "flash" in re.sub(r"\([^()]*\)", "", str(card.get("oracle_text") or "")).lower()
            and unpayable_graveyard_flashback(game_state, card, meta) is not None
        ):
            continue
        if has_unmodelled_additional_cost(card):
            continue
        cmc = mana_cost_to_cmc(cost)
        if role in ("creature", "planeswalker"):
            backup_picks.append((cmc, option))
        if step is not None:
            if name not in scheduled and name not in greedy:
                continue
        elif assessment is not None or role not in ("creature", "planeswalker"):
            continue
        picks.append((cmc, option))
    if picks:
        _, option = max(picks, key=lambda item: item[0])
        if step is not None and not scheduled:
            casts = sorted(greedy)
            plan_text = (
                "the best searched line holds this turn, so the greedy schedule's cast stands in: "
                + (", ".join(casts[:-1]) + " and " + casts[-1] if len(casts) > 1 else casts[0])
            )
        return [option.option_id], plan_text or "no board assessment, so the biggest payable creature"
    if backup_picks and step is not None and (scheduled or greedy) and not deferred:
        _, option = max(backup_picks, key=lambda item: item[0])
        return [option.option_id], (
            f"{plan_text}; scheduled cast unavailable in menu, deploying biggest payable threat instead"
        )
    if any(option.option_id == "pass" for option in options):
        if deferred:
            later = ", ".join(sorted(deferred))
            return ["pass"], f"{plan_text}; attack first, {later} after combat"
        return ["pass"], "passing: no safe payable cast from the searched line is available in this menu"
    return fallback, "no pass option, so the first legal option"


def _option_text(decision: Any, option_id: str) -> str:
    """An option's label for logs, without the ability text in brackets or a final period."""
    option = decision.find(option_id)
    label = str(getattr(option, "label", "") or option_id)
    return label.split(" [", 1)[0].strip().rstrip(".") or option_id


def _unmodelled_options(result: Any, decision: Any, state: dict[str, Any]) -> dict[str, str]:
    """Payable casts whose effect the line search drops: option id -> "card (why)"."""
    from arenamcp.line_guard import unmodelled_cast

    found: dict[str, str] = {}
    for option in getattr(decision, "options", ()) or ():
        meta = option.meta or {}
        if option.payable is False or "cast" not in str(meta.get("actionType") or "").lower():
            continue
        why = unmodelled_cast(result, option, state)
        if why:
            found[option.option_id] = why
    return found


def _kill_names(names: tuple[str, ...]) -> str:
    """' (kills Healer's Hawk, Soldier ×2)' / ' (kills nothing)' for a mode's ``ModeComparison.kills``."""
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    shown = [name if n == 1 else f"{name} ×{n}" for name, n in counts.items()]
    if len(shown) > 4:
        shown = [*shown[:3], f"+{sum(list(counts.values())[3:])} more"]
    return f" (kills {', '.join(shown)})" if shown else " (kills nothing)"


def _modes_line(comparison: Any, decision: Any, max_chars: int = 400) -> str:
    """The 'MODES ...' line for a modal menu: each mode's searched outcome, or ''."""
    try:
        from arenamcp.line_guard import _life_parts

        if comparison is None or not getattr(comparison, "complete", False):
            return ""
        head = (
            "MODES (each searched with this choice resolved; the role and board facts below were "
            "computed before it resolves): "
        )
        parts = []
        for option in decision.options:
            line = comparison.lines.get(option.option_id)
            if line is None:
                continue
            kind = comparison.modes.get(option.option_id) or _option_text(decision, option.option_id)
            if kind == "other":
                kind = _option_text(decision, option.option_id).split(": ", 1)[-1][:48] + " (not modelled)"
            killed = (getattr(comparison, "kills", None) or {}).get(option.option_id)
            if killed is not None:
                kind += _kill_names(tuple(killed))
            outcome = ", ".join(_life_parts(line, modal=True)) or line.outcome_text()
            parts.append(f"{kind} — {outcome}")
        if len(parts) < 2:
            return ""
        text = head + " | ".join(parts)
        return text if len(text) <= max_chars else text[: max_chars - 1] + "…"
    except Exception as error:
        logger.debug("modes line skipped: %s", error)
        return ""


def line_fallback_option_pick(
    decision: Any, game_state: dict[str, Any], board_pick: list[str]
) -> tuple[list[str], str] | None:
    """The best searched line's first play when the model gave no usable answer, or None."""
    import arenamcp.action_planner as _ap

    if decision.request_type != "ActionsAvailable" or not _ap._own_main_phase_with_empty_stack(game_state):
        return None
    try:
        from arenamcp.board_assessment import assess
        from arenamcp.line_guard import line_fallback_pick
        from arenamcp.line_search import action_key

        assessment = assess(game_state)
        result = getattr(assessment, "line_search", None)
        if assessment is None or result is None:
            return None
        picked = line_fallback_pick(result, decision, game_state)
        if not picked or len(picked) != 1 or picked == board_pick:
            return None
        option = decision.find(picked[0])
        if option is None:
            return None
        meta = option.meta or {}
        if meta.get("actionType") != "ActionType_Play" and option.payable is not True:
            return None
        if meta.get("actionType") == "ActionType_Cast":
            fb_card = _ap.find_source(game_state, meta)
            if (
                "flash" in re.sub(r"\([^()]*\)", "", str(fb_card.get("oracle_text") or "")).lower()
                and unpayable_graveyard_flashback(game_state, fb_card, meta) is not None
            ):
                return None
        best = result.best
        aimed = dict(best.steps[0].targets) if best.steps else {}
        line_aimed = str(_ap.find_source(game_state, meta).get("name") or "") in aimed
        current = decision.find(board_pick[0]) if len(board_pick) == 1 else None
        if current is not None and current.option_id != "pass":
            key = action_key(current, game_state)
            if key is not None and key in best.first_actions:
                if (current.meta or {}).get("actionType") == "ActionType_Play" or not line_aimed:
                    return None
        return picked, f"the best searched line is {best.summary()}"
    except Exception as error:
        logger.debug("line fallback pick skipped: %s", error)
        return None


def line_casts_option(decision: Any, game_state: dict[str, Any], option_id: str, reasoning: str = "") -> str:
    """Why the searched line casts ``option_id``'s spell this turn, or "" (ledger #86)."""
    import arenamcp.action_planner as _ap

    if decision.request_type != "ActionsAvailable" or not _ap._own_main_phase_with_empty_stack(game_state):
        return ""
    option = decision.find(option_id)
    meta = (option.meta if option is not None else None) or {}
    if option is None or option.payable is not True or meta.get("actionType") != "ActionType_Cast":
        return ""
    name = str(_ap.find_source(game_state, meta).get("name") or "")
    if not name:
        return ""
    try:
        from arenamcp.board_assessment import assess

        assessment = assess(game_state)
        if assessment is None or not assessment.our_turn or not assessment.lookahead:
            return ""
        first = assessment.lookahead[0]
        casts, why = set(first.casts), f"the searched line casts {name} this turn"
        if not casts:
            baseline = getattr(getattr(assessment, "line_search", None), "baseline", None)
            steps = getattr(baseline, "steps", None) or ()
            casts = set(getattr(steps[0], "casts", ()) or ()) if steps else set()
            why = f"the searched line holds, and the greedy schedule casts {name} this turn"
    except Exception as error:
        logger.debug("line cast check skipped: %s", error)
        return ""
    if name not in casts:
        return ""
    target = _ap.ActionPlanner._searched_line_target(game_state, name)[0]
    if target and not _reasoning_names_target(reasoning, target):
        return ""
    return why


def board_math_agreement(
    decision: Any, game_state: dict[str, Any], chosen: list[str], reasoning: str = ""
) -> str:
    """Why the board-math fallback would play exactly ``chosen`` here, or ""."""
    import arenamcp.action_planner as _ap

    chosen = [str(option_id) for option_id in chosen or []]
    if decision.request_type != "ActionsAvailable" or len(chosen) != 1 or chosen == ["pass"]:
        return ""
    if not _ap._own_main_phase_with_empty_stack(game_state):
        return ""
    picked, why = _ap.board_math_option_pick(decision, game_state)
    line = _ap.line_fallback_option_pick(decision, game_state, picked)
    if line is not None:
        picked, why = line
    if picked != chosen:
        return ""
    option = decision.find(chosen[0])
    card = _ap.find_source(game_state, (option.meta if option is not None else None) or {})
    name = str(card.get("name") or "")
    target, target_id = _ap.ActionPlanner._searched_line_target(game_state, name) if name else ("", 0)
    if target and not _reasoning_names_target(reasoning, target, target_id=target_id, game_state=game_state):
        logger.info(
            "typed-decision: the low answer casts the line's %s but not at its target (%s): %r",
            name,
            "the opponent" if target == "opponent" else target,
            str(reasoning or "")[:160],
        )
        return ""
    modal = "•" in str(card.get("oracle_text") or "")
    mode = _ap.ActionPlanner._searched_line_mode(game_state, name) if name and modal else ""
    if mode:
        try:
            from arenamcp.mode_claims import other_modes_named

            others = other_modes_named(reasoning, card, mode)
        except Exception as error:
            logger.debug("line mode check failed: %s", error)
            return ""
        if others:
            logger.info(
                "typed-decision: the low answer casts the line's %s but names %s, "
                "not the line's mode (%s): %r",
                name,
                ", ".join(others),
                mode,
                str(reasoning or "")[:160],
            )
            return ""
    return why or "the board-math play"


_FACE_TARGET_RE = re.compile(
    r"\b(?:face|go(?:es|ing)? face|(?:at|to|into|on|target(?:ing)?) (?:the )?(?:opponent|opponent's (?:face|life)|player)"
    r"|their (?:face|life(?: total)?|head)|(?:opponent|player) directly)\b",
    re.IGNORECASE,
)


_ID_MENTION_RE = re.compile(r"(?:\[id:\s*|\bid[: ]\s*|#)(\d+)", re.IGNORECASE)
_UNDAMAGED_RE = re.compile(r"\b(?:undamaged|unhurt|unwounded)\b", re.IGNORECASE)
_DAMAGED_RE = re.compile(r"\b(?:\d+-)?(?:damaged|wounded|injured)\b", re.IGNORECASE)


def _reasoning_names_target(
    reasoning: str, target: str, *, target_id: int = 0, game_state: dict[str, Any] | None = None
) -> bool:
    """Whether ``reasoning`` aims the cast where the searched line does ("opponent" or a creature's name)."""
    text = str(reasoning or "")
    if not text:
        return False
    aimed = re.sub(r"\b(?:instead of|rather than|over|not)\b[^.;,:—–]*", " ", text, flags=re.IGNORECASE)
    face = bool(_FACE_TARGET_RE.search(aimed))
    if target == "opponent":
        return face
    folded = aimed.casefold()
    name = target.casefold()
    words = [word for word in re.findall(r"[a-z0-9']+", name.split(",")[0]) if len(word) >= 4]
    named = name in folded or any(re.search(rf"\b{re.escape(word)}\b", folded) for word in words)
    if not named or face:
        return False
    if not target_id or not isinstance(game_state, dict):
        return True
    local_seat = _local_seat(game_state)
    bodies = {
        _as_int(card.get("instance_id")): card
        for card in game_state.get("battlefield", []) or []
        if card.get("name") == target and card.get("controller_seat_id") != local_seat
    }
    if target_id not in bodies or len(bodies) == 1:
        return True
    mentioned = {int(found) for found in _ID_MENTION_RE.findall(aimed)}
    if mentioned & set(bodies):
        return target_id in mentioned
    damaged = {iid: _as_int(card.get("damage")) > 0 for iid, card in bodies.items()}
    if list(damaged.values()).count(damaged[target_id]) != 1:
        return False
    says_damaged, says_undamaged = bool(_DAMAGED_RE.search(aimed)), bool(_UNDAMAGED_RE.search(aimed))
    if says_damaged == says_undamaged:
        return False
    return says_damaged == damaged[target_id]


def board_math_legacy_plan(
    game_state: dict[str, Any],
    legal_actions: list[str] | None,
    trigger: str = "",
    *,
    lead: str = "Model unavailable",
    fallback_reason: str = FALLBACK_LLM_UNAVAILABLE,
) -> ActionPlan:
    """:func:`board_math_option_pick` for the legacy legal-action-string path."""
    import arenamcp.action_planner as _ap
    from arenamcp.decisions import DecisionOption, PendingDecision

    turn = int(((game_state.get("turn") or {}).get("turn_number")) or 0)
    plan = ActionPlan(trigger=trigger, turn_number=turn, fallback_reason=fallback_reason)
    hand: dict[str, dict] = {}
    cards = list(game_state.get("hand") or [])
    if game_state.get("command"):
        from arenamcp.board_model import our_commanders

        cards += [c.card for c in our_commanders(game_state)]
    for card in cards:
        if isinstance(card, dict) and card.get("name"):
            hand.setdefault(str(card["name"]).casefold(), card)
    options: list[Any] = []
    texts: dict[str, str] = {}
    for index, entry in enumerate(legal_actions or []):
        text = str(entry or "").strip()
        lower = text.lower()
        if lower == "pass":
            if "pass" not in texts:
                options.append(DecisionOption("pass", "Pass"))
                texts["pass"] = text
            continue
        if lower.startswith("play land:"):
            kind, rest, payable = "ActionType_Play", text.split(":", 1)[1], None
        elif lower.startswith("cast "):
            kind, rest, payable = "ActionType_Cast", text[5:], "[ok]" in lower
        else:
            continue
        card = hand.get(re.sub(r"\s*\[[^\]]*\]", "", rest).strip().casefold())
        if card is None:
            continue
        option_id = f"legal:{index}"
        meta = {"actionType": kind, "instanceId": card.get("instance_id"), "grpId": card.get("grp_id")}
        options.append(DecisionOption(option_id, text, payable=payable, meta=meta))
        texts[option_id] = text
    if not options:
        return plan
    decision = PendingDecision((0, 0), "ActionsAvailable", tuple(options), can_pass="pass" in texts)
    picked, why = _ap.board_math_option_pick(decision, game_state)
    if len(picked) != 1 or picked[0] not in texts:
        return plan
    if picked[0] == "pass":
        action = GameAction(action_type=ActionType.PASS_PRIORITY)
    else:
        action = _ap.ActionPlanner.__new__(_ap.ActionPlanner)._legal_action_to_action(texts[picked[0]])
        if action is None:
            return plan
    action.reasoning = f"{lead}; {why}."
    plan.actions = [action]
    plan.overall_strategy = f"[local-fallback] {why}"
    plan.voice_advice = plan.spoken_actions()
    logger.warning("Legacy fallback (%s): %s -> %s", lead.lower(), why, texts[picked[0]])
    return plan
