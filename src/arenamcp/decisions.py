"""Typed PendingDecision pipeline (fable-improvements.md item 1).

One structured object flows from the bridge poll to the planner to the
executor. The planner chooses among ``option_id``s; submission happens by
id; display strings are rendered *from* the structure and never parsed
back *into* it.

Option-id scheme (family-agnostic; the executor dispatches on prefix):

    idx:<n>        — ActionsAvailable action at index n (submit_action_by_index)
    tgt:<iid>      — SelectTargets candidate instance id (submit_targets)
    sel:<id>       — SelectN / Search id (submit_selection, multi-select)
    mull:keep|mull — Mulligan decision (submit_mulligan)
    grp:<iid>      — London-mulligan card to bottom (submit_group)
    grp:<iid>:<to> — scry/surveil/split: send card <iid> to destination <to>
                     (one per card; submit_group answers every GroupSpec)
    pass           — pass priority (submit_pass)
"""

from __future__ import annotations

import itertools
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from arenamcp.card_db import is_unknown_card_name
from arenamcp.mana import has_autotap_solution

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DecisionOption:
    option_id: str
    label: str  # display only — NEVER parsed for semantics
    # Casts: autotap solution exists. Activations: True with an autotap
    # solution, False when the log's mana check proved the cost unpayable,
    # else None (free or unknown).
    payable: bool | None = None
    meta: dict = field(default_factory=dict)  # prompt enrichment only


@dataclass(frozen=True)
class TargetSlot:
    """One TargetSelection slot of a SelectTargetsRequest.

    MTGA target requests can carry MULTIPLE slots (e.g. an Aura that
    enchants your creature AND exiles an opponent's permanent on enter).
    Each slot has its own legal-candidate set; a submit that fills only
    one slot when several are required is silently rejected and the
    request re-presents — the multi-target wedge (Sheltered by Ghosts,
    Ethereal Armor). Submission must cover every unsatisfied slot.
    """

    target_idx: int
    min_targets: int
    max_targets: int
    selected: int
    candidate_ids: tuple[int, ...]

    @property
    def needs(self) -> int:
        """How many more targets this slot still requires (>=0)."""
        return max(0, self.min_targets - self.selected)


@dataclass(frozen=True)
class PendingDecision:
    request_id: tuple[int, int]  # (gameStateId, msgId); zeros when unknown
    request_type: str  # bridge enum/class name ("SelectTargets", ...)
    options: tuple[DecisionOption, ...]
    min_select: int = 1
    max_select: int = 1
    can_pass: bool = False
    can_cancel: bool = False
    source_label: str = ""
    # SelectTargets only: per-slot structure (empty for other families and
    # for single-slot requests built from older plugin builds).
    slots: tuple[TargetSlot, ...] = ()
    min_weight: int | None = None
    max_weight: int | None = None

    def option_ids(self) -> set[str]:
        return {o.option_id for o in self.options}

    def find(self, option_id: str) -> DecisionOption | None:
        for o in self.options:
            if o.option_id == option_id:
                return o
        return None

    def selection_is_valid(self, chosen: list[str]) -> bool:
        """Validate distinct selection ids and the engine's count/weight bounds."""
        if len(set(chosen)) != len(chosen) or not set(chosen).issubset(self.option_ids()):
            return False
        if not self.min_select <= len(chosen) <= self.max_select:
            return False
        if self.request_type == "CastingTimeOptions":
            selected = [self.find(option_id) for option_id in chosen]
            if not selected:
                return False
            first = selected[0].meta
            group = (first.get("childIndex"), first.get("choiceKind"))
            if any(
                (option.meta.get("childIndex"), option.meta.get("choiceKind")) != group for option in selected
            ):
                return False
            minimum, maximum = casting_selection_bounds(first)
            if not minimum <= len(selected) <= maximum:
                return False
        if self.request_type == "Group" and any("spec_index" in o.meta for o in self.options):
            return destination_groups(self, chosen) is not None
        weight = sum(int(self.find(option_id).meta.get("weight", 1)) for option_id in chosen)
        return (self.min_weight is None or weight >= self.min_weight) and (
            self.max_weight is None or weight <= self.max_weight
        )


def casting_selection_bounds(meta: dict[str, Any]) -> tuple[int, int]:
    """Counts belong to one casting child, not the flattened parent menu."""
    if meta.get("choiceKind") not in {"modal", "choose_or_cost"}:
        return 1, 1
    minimum = int(meta.get("min", 1))
    maximum = int(meta.get("max", max(1, minimum)))
    return minimum, maximum


def _default_card_resolver(grp_id: int) -> dict:
    try:
        from arenamcp import server

        return server.get_card_info(grp_id)
    except Exception:
        return {}


def _default_name_resolver(grp_id: int) -> str:
    return str(_default_card_resolver(grp_id).get("name") or "")


# --- Log-derived identity of bridge instance ids ------------------------------
# The bridge poll carries bare instance ids. Player.log (the source of truth
# for observation) already revealed what they are — e.g. the surveilled card
# is a Visibility_Private library object in the same GRE event as the
# GroupReq — so labels resolve them from this process's log-fed GameState.


def _live_game_state() -> Any:
    try:
        from arenamcp import server

        return server.game_state
    except Exception:
        return None


def _live_object(instance_id: int) -> Any:
    state = _live_game_state()
    try:
        return state.game_objects.get(int(instance_id)) if state is not None else None
    except Exception:
        return None


def _default_instance_zone(instance_id: int) -> str:
    """'Hand' / 'Battlefield' / ... for an instance, or '' when unknown."""
    obj = _live_object(instance_id)
    if obj is None:
        return ""
    try:
        zone = _live_game_state().zones.get(obj.zone_id)
        return _zone_name(getattr(zone.zone_type, "value", "")) if zone is not None else ""
    except Exception:
        return ""


def _default_instance_card(instance_id: int) -> dict[str, Any]:
    """{grp_id, name, type_line, mana_cost, cmc} for an instance, or {}."""
    grp_id = int(getattr(_live_object(instance_id), "grp_id", 0) or 0)
    if not grp_id:
        return {}
    info = _default_card_resolver(grp_id) or {}
    card: dict[str, Any] = {"grp_id": grp_id}
    if not info.get("error"):
        card.update(
            {k: info[k] for k in ("name", "type_line", "mana_cost", "cmc") if info.get(k) not in (None, "")}
        )
    return card


def _default_activation_unaffordable(action: dict[str, Any]) -> bool:
    """The log's mana check proved this activation unpayable now.

    Player.log's ActionsAvailableReq carries the same actions as the bridge
    poll. When Arena attaches no autotap solution to an activation,
    gamestate_decisions checks the ability's mana cost against our untapped
    sources and flags the action ``_unaffordable`` (the coach's
    "[NEED:{2}{G}]" tag). False when the log has no verdict for it.
    """
    instance_id = int(action.get("instanceId") or 0)
    if not instance_id:
        return False
    ability_id = int(action.get("abilityGrpId") or 0)
    state = _live_game_state()
    try:
        raw_actions = list(getattr(state, "legal_actions_raw", None) or [])
    except Exception:
        return False
    for raw in raw_actions:
        if (
            isinstance(raw, dict)
            and str(raw.get("actionType") or "").removeprefix("ActionType_") == "Activate"
            and int(raw.get("instanceId") or 0) == instance_id
            and int(raw.get("abilityGrpId") or 0) == ability_id
        ):
            return bool(raw.get("_unaffordable"))
    return False


_ZONE_NAMES = {
    "battlefield",
    "hand",
    "graveyard",
    "exile",
    "library",
    "stack",
    "command",
    "limbo",
    "pending",
    "revealed",
}


def _enum_text(raw: Any) -> str:
    if isinstance(raw, dict):  # mac adapter enum dump {"e": name, "v": value}
        raw = raw.get("e") or ""
    return str(raw or "").strip()


def _zone_name(raw: Any) -> str:
    """'ZoneType_Library' / 'Library' -> 'Library'; '' when unrecognized."""
    text = _enum_text(raw).removeprefix("ZoneType_")
    return text.capitalize() if text.lower() in _ZONE_NAMES else ""


def _sub_zone_name(raw: Any) -> str:
    """'SubZoneType_Top' / 'Top' -> 'Top'; None/absent -> ''."""
    text = _enum_text(raw).removeprefix("SubZoneType_")
    return "" if text.lower() in ("", "none") else text.capitalize()


# --- Rules text -------------------------------------------------------------

_ARENA_MANA = re.compile(r"\{(o[^{}]*)\}")


def _arena_symbols(group: str) -> str:
    """Arena's '{o1o(U/R)o(U/R)}' -> '{1}{U/R}{U/R}'."""
    symbols = re.findall(r"o(\([^)]*\)|\d+|[A-Za-z])", group)
    return "".join("{" + s.strip("()") + "}" for s in symbols) if symbols else "{" + group + "}"


def clean_rules_text(text: str, name: str = "") -> str:
    """Arena localization text as players read it: no markup, {2} not {o2}."""
    text = re.sub(r"<[^>]*>", "", text or "")
    text = _ARENA_MANA.sub(lambda m: _arena_symbols(m.group(1)), text)
    text = text.replace("CARDNAME", name or "this card")
    return " ".join(text.split())


def _default_ability_description(ability_grp_id: int) -> tuple[str, str]:
    """(loyalty cost, rules text) — the same lookup the sibling-activation
    labels in autopilot use, so both label paths agree."""
    if not ability_grp_id:
        return "", ""
    try:
        from arenamcp import gre_action_matcher

        cost, text = gre_action_matcher.describe_ability(int(ability_grp_id))
        return str(cost or ""), str(text or "")
    except Exception:
        pass
    try:
        from arenamcp.card_db import get_card_database

        return "", get_card_database().get_ability_text(int(ability_grp_id)) or ""
    except Exception:
        return "", ""


# Keyword abilities activated away from the battlefield. The model read
# "Activate: Undulating Witness" (Basic landcycling, from hand) as a pump, as
# impossible "since it's still in hand", or as a graveyard ability
# (2026-10-06). The zone is implied when the log can't place the card.
_KEYWORD_ACTIVATIONS: tuple[tuple[str, str, str], ...] = (
    (r"basic landcycling", "discard it to search for a basic land card and put it into your hand", "Hand"),
    (r"(\w+)cycling", "discard it to search for a {0} card and put it into your hand", "Hand"),
    (r"cycling", "discard it to draw a card", "Hand"),
    (r"channel", "", "Hand"),
    (r"forecast", "", "Hand"),
    (
        r"(?:commander )?ninjutsu",
        "return an unblocked attacker you control to hand to put this onto the battlefield tapped and attacking",
        "Hand",
    ),
    (r"transmute", "discard it to search for a card with the same mana value; sorcery speed", "Hand"),
    (
        r"bloodrush",
        "discard it: target attacking creature gets this card's power, toughness and abilities",
        "Hand",
    ),
    (r"reinforce", "discard it to put +1/+1 counters on target creature", "Hand"),
    (
        r"unearth",
        "return it to the battlefield with haste; it's exiled at end of turn; sorcery speed",
        "Graveyard",
    ),
    (r"embalm", "exile it to create a token copy of it; sorcery speed", "Graveyard"),
    (r"eternalize", "exile it to create a 4/4 black Zombie token copy of it; sorcery speed", "Graveyard"),
    (
        r"scavenge",
        "exile it to put +1/+1 counters equal to its power on target creature; sorcery speed",
        "Graveyard",
    ),
    (r"encore", "exile it to create token copies that attack this turn; sorcery speed", "Graveyard"),
)

_FROM_ZONE = {
    "Hand": "from hand",
    "Graveyard": "from graveyard",
    "Exile": "from exile",
    "Command": "from command zone",
    "Library": "from library",
}


def _keyword_activation(text: str) -> tuple[str, str]:
    """(short reminder, implied source zone) for a keyword ability's text."""
    lowered = text.lower()
    for pattern, reminder, zone in _KEYWORD_ACTIVATIONS:
        match = re.match(pattern + r"\b", lowered)
        if match:
            kind = match.group(1).capitalize() if match.groups() else ""
            return reminder.format(kind), zone
    return "", ""


def _activation_label(name: str, ability_grp_id: int, zone: str, *, describe: bool) -> str:
    """'Activate: <Name> [from hand: <cost>: <ability text> — <reminder>]'.

    The source name stays right after "Activate:" and everything else is in
    one bracket, so autopilot's _activation_source_name (which stops at "[")
    and the coach's bracket stripping keep reading the bare name. ``describe``
    is False for a permanent offering several abilities: autopilot's
    _label_sibling_activations appends their "[cost: text]" itself.
    """
    cost, raw_text = _default_ability_description(ability_grp_id) if ability_grp_id else ("", "")
    text = clean_rules_text(raw_text, name).replace("[", "(").replace("]", ")")
    reminder, implied_zone = _keyword_activation(text)
    where = _FROM_ZONE.get(_zone_name(zone) or implied_zone, "")
    detail = ""
    if describe and text:
        detail = text if len(text) <= 160 else text[:157].rstrip() + "…"
        if cost:
            detail = f"{cost}: {detail}"
        if reminder and "(" not in text:
            detail += f" — {reminder}"
    inner = ": ".join(part for part in (where, detail) if part)
    base = f"Activate: {name or 'ability'}"
    return f"{base} [{inner}]" if inner else base


_ACTIONS_AVAILABLE_TYPES = {
    "ActionsAvailable",
    "ActionsAvailableReq",
    "ActionsAvailableRequest",
}
_SELECT_TARGETS_TYPES = {"SelectTargets", "SelectTargetsRequest"}
_SELECT_N_TYPES = {"SelectN", "SelectNRequest", "Search", "SearchRequest"}
_MULLIGAN_TYPES = {"Mulligan", "MulliganReq", "MulliganRequest"}
_GROUP_TYPES = {"Group", "GroupReq", "GroupRequest"}
_CASTING_TYPES = {"CastingTimeOptions", "CastingTimeOptionsReq", "CastingTimeOptionRequest"}
_OPTIONAL_TYPES = {
    "OptionalAction",
    "OptionalActionReq",
    "OptionalActionRequest",
    "OptionalActionMessage",
    "OptionalActionMessageReq",
    "OptionalActionMessageRequest",
}


def build_pending_decision(
    poll: dict[str, Any] | None,
    *,
    resolve_name: Callable[[int], str] = _default_name_resolver,
    resolve_instance: Callable[[int], str] | None = None,
    resolve_zone: Callable[[int], str] | None = None,
    resolve_card: Callable[[int], dict[str, Any]] | None = None,
    activation_unaffordable: Callable[[dict[str, Any]], bool] | None = None,
) -> PendingDecision | None:
    """Build a PendingDecision from a raw get_pending_actions() response.

    Returns None when nothing is pending or the request family isn't
    structurally mapped yet (callers keep their legacy path as fallback —
    fable-improvements.md migration note).

    ``resolve_zone`` / ``resolve_card`` map a bridge instance id to its zone
    and card identity for labels; both default to the log-fed GameState.
    ``activation_unaffordable`` answers whether an activation Arena gave no
    autotap solution failed the mana check (default: the log's verdict).
    """
    resolve_zone = resolve_zone or _default_instance_zone
    resolve_card = resolve_card or _default_instance_card
    activation_unaffordable = activation_unaffordable or _default_activation_unaffordable
    if not poll or not poll.get("has_pending"):
        return None

    request_type = str(poll.get("request_type") or "")
    request_class = str(poll.get("request_class") or "")
    rtype = request_type or request_class
    request_id = (
        int(poll.get("game_state_id") or 0),
        int(poll.get("msg_id") or 0),
    )
    can_pass = bool(poll.get("can_pass"))
    can_cancel = bool(poll.get("can_cancel"))
    source_label = str(poll.get("source_card") or poll.get("prompt") or "")

    if rtype in _CASTING_TYPES or request_class in _CASTING_TYPES:
        options = tuple(
            DecisionOption(f"idx:{index}", _casting_label(action), meta=dict(action))
            for index, action in enumerate(poll.get("actions") or [])
            if action.get("actionType") == "CastingTimeOption"
        )
        return (
            PendingDecision(
                request_id,
                "CastingTimeOptions",
                options,
                min_select=min(casting_selection_bounds(option.meta)[0] for option in options),
                max_select=max(casting_selection_bounds(option.meta)[1] for option in options),
                can_cancel=can_cancel,
                source_label=source_label,
            )
            if options
            else None
        )
    if rtype in _OPTIONAL_TYPES or request_class in _OPTIONAL_TYPES:
        source_id = int(poll.get("source_instance_id") or 0)
        if not source_label and resolve_instance and source_id:
            source_label = resolve_instance(source_id)
        details = {
            "sourceId": source_id,
            "mechanics": poll.get("optional_mechanics") or [],
            "recipients": poll.get("optional_recipients") or [],
        }
        return PendingDecision(
            request_id,
            "OptionalAction",
            (
                DecisionOption("optional:accept", "Accept the optional effect", meta=details),
                DecisionOption("optional:decline", "Decline the optional effect", meta=details),
            ),
            source_label=source_label,
        )

    if rtype in _ACTIONS_AVAILABLE_TYPES or (not rtype and poll.get("actions")):
        return _build_actions_available(
            poll,
            request_id,
            can_pass,
            can_cancel,
            source_label,
            resolve_name,
            resolve_zone,
            activation_unaffordable,
        )
    if rtype in _SELECT_TARGETS_TYPES or request_class in _SELECT_TARGETS_TYPES:
        source_id = int(
            poll.get("source_instance_id") or (poll.get("request_payload") or {}).get("sourceId") or 0
        )
        if not source_label and resolve_instance and source_id:
            source_label = resolve_instance(source_id)
        return _build_select_targets(
            poll, request_id, can_cancel, source_label, resolve_name, resolve_instance
        )
    if rtype in _SELECT_N_TYPES or request_class in _SELECT_N_TYPES:
        return _build_select_n(
            poll, request_id, rtype, can_cancel, source_label, resolve_name, resolve_instance
        )
    if rtype in _GROUP_TYPES or request_class in _GROUP_TYPES:
        return _build_group(poll, request_id, can_cancel, source_label, resolve_instance, resolve_card)
    if rtype in _MULLIGAN_TYPES or request_class in _MULLIGAN_TYPES:
        return PendingDecision(
            request_id=request_id,
            request_type="Mulligan",
            options=(
                DecisionOption("mull:keep", "Keep this hand"),
                DecisionOption("mull:mull", "Mulligan"),
            ),
            can_pass=False,
            can_cancel=False,
        )
    return None


def _casting_label(action: dict) -> str:
    label = str(action.get("label") or action.get("choiceKind") or "Casting choice")
    if action.get("choiceKind") == "modal" and action.get("grpId"):
        from arenamcp.card_db import get_card_database

        rules = get_card_database().get_ability_text(int(action["grpId"]))
        if rules:
            import re

            label += ": " + re.sub(r"<[^>]*>", "", rules)
    return label


def _build_actions_available(
    poll: dict[str, Any],
    request_id: tuple[int, int],
    can_pass: bool,
    can_cancel: bool,
    source_label: str,
    resolve_name: Callable[[int], str],
    resolve_zone: Callable[[int], str] | None = None,
    activation_unaffordable: Callable[[dict[str, Any]], bool] | None = None,
) -> PendingDecision | None:
    options: list[DecisionOption] = []
    saw_pass = False
    actions = poll.get("actions") or []
    # Permanents offering several different abilities (Jace's loyalty
    # abilities): autopilot's _label_sibling_activations names those.
    abilities_by_source: dict[int, set[int]] = {}
    for action in actions:
        if str(action.get("actionType") or "").removeprefix("ActionType_") == "Activate":
            abilities_by_source.setdefault(int(action.get("instanceId") or 0), set()).add(
                int(action.get("abilityGrpId") or 0)
            )
    siblings = {iid for iid, ids in abilities_by_source.items() if iid and len(ids) > 1}
    for i, action in enumerate(actions):
        atype = str(action.get("actionType") or "")
        if atype and not atype.startswith("ActionType_"):
            atype = f"ActionType_{atype}"
        grp_id = int(action.get("grpId") or 0)
        name = resolve_name(grp_id) if grp_id else ""
        payable: bool | None = None
        if atype == "ActionType_Pass":
            saw_pass = True
            options.append(DecisionOption("pass", "Pass"))
            continue
        if atype in (
            "ActionType_Activate_Mana",
            "ActionType_ActivateMana",
            "ActionType_FloatMana",
        ):
            # Mana abilities tap sources to float mana; MTGA automatically taps
            # mana when casting spells or paying costs. Exposing them in
            # ActionsAvailable causes autopilot to tap lands one by one for no spell.
            continue
        if atype == "ActionType_Cast":
            payable = has_autotap_solution(action)
            label = f"Cast {name or 'spell'}" + ("" if payable else " (cannot auto-pay)")
        elif atype == "ActionType_Play":
            label = f"Play land: {name or 'land'}"
        elif atype == "ActionType_Activate":
            # Arena offers an activation whenever it is legal to announce; it
            # does not promise the cost is payable. No autotap solution plus a
            # failed mana check means announcing it only reaches a PayCosts
            # nothing can pay (2026-10-07 18:03:46: Kami of Bamboo Groves'
            # {2}{G} channel with every land tapped, picked and cancelled
            # twice). Free activations (tap/sacrifice only) stay None.
            payable = True if has_autotap_solution(action) else None
            if payable is None and activation_unaffordable is not None:
                try:
                    if activation_unaffordable(action):
                        payable = False
                except Exception:
                    logger.debug("activation affordability lookup failed", exc_info=True)
            source_id = int(action.get("instanceId") or 0)
            zone = ""
            if source_id and resolve_zone is not None:
                try:
                    zone = resolve_zone(source_id) or ""
                except Exception:
                    zone = ""
            label = _activation_label(
                name,
                int(action.get("abilityGrpId") or 0),
                zone,
                describe=source_id not in siblings,
            )
        else:
            label = atype.replace("ActionType_", "") or f"Action {i}"
        options.append(
            DecisionOption(
                option_id=f"idx:{i}",
                label=label,
                payable=payable,
                meta={
                    "actionType": atype,
                    "grpId": grp_id,
                    "instanceId": int(action.get("instanceId") or 0),
                    "abilityGrpId": int(action.get("abilityGrpId") or 0),
                    "manaCost": action.get("manaCost"),
                    "autoTapActions": action.get("autoTapActions") or [],
                    "hasAutoTap": has_autotap_solution(action),
                },
            )
        )
    if can_pass and not saw_pass:
        options.append(DecisionOption("pass", "Pass"))
    if not options:
        return None
    return PendingDecision(
        request_id=request_id,
        request_type="ActionsAvailable",
        options=tuple(options),
        can_pass=can_pass or saw_pass,
        can_cancel=can_cancel,
        source_label=source_label,
    )


def _build_select_targets(
    poll: dict[str, Any],
    request_id: tuple[int, int],
    can_cancel: bool,
    source_label: str,
    resolve_name: Callable[[int], str],
    resolve_instance: Callable[[int], str] | None = None,
) -> PendingDecision | None:
    options: list[DecisionOption] = []
    seen: set[int] = set()
    for cand in poll.get("target_candidates") or []:
        if cand.get("legalAction", "Select") not in ("Select", "SelectAction_Select", 1):
            continue
        iid = int(cand.get("targetInstanceId") or cand.get("instanceId") or 0)
        if not iid or iid in seen:
            continue
        seen.add(iid)
        grp_id = int(cand.get("grpId") or 0)
        name = resolve_instance(iid) if resolve_instance else ""
        name = name or (resolve_name(grp_id) if grp_id else "")
        options.append(
            DecisionOption(
                option_id=f"tgt:{iid}",
                label=name or f"Target #{iid}",
                meta={"instanceId": iid, "grpId": grp_id, "targetIdx": cand.get("targetIdx")},
            )
        )
    if not options:
        return None

    # Reconstruct per-slot structure so submission can cover EVERY slot a
    # multi-target request requires (not just the first). The flat
    # ``options`` list above stays for prompt/LLM presentation.
    slots: list[TargetSlot] = []
    for sel in poll.get("target_selections") or []:
        sel = sel or {}
        cand_ids: list[int] = []
        cseen: set[int] = set()
        slot_targets = sel.get("targets")
        if slot_targets is None:
            slot_targets = [
                candidate
                for candidate in poll.get("target_candidates") or []
                if candidate.get("targetIdx", sel.get("targetIdx")) == sel.get("targetIdx")
            ]
        for t in slot_targets:
            if t.get("legalAction", "Select") not in ("Select", "SelectAction_Select", 1):
                continue
            iid = int(t.get("targetInstanceId") or t.get("instanceId") or 0)
            if iid and iid not in cseen:
                cseen.add(iid)
                cand_ids.append(iid)
        slots.append(
            TargetSlot(
                target_idx=int(sel.get("targetIdx") or 0),
                min_targets=int(sel.get("minTargets", 1)),
                max_targets=int(sel.get("maxTargets", 1)),
                selected=int(sel.get("selectedTargets") or 0),
                candidate_ids=tuple(cand_ids),
            )
        )

    if slots:
        min_sel = sum(slot.needs for slot in slots)
        max_sel = sum(max(0, slot.max_targets - slot.selected) for slot in slots)
    else:
        min_sel, max_sel = 1, 1
    return PendingDecision(
        request_id=request_id,
        request_type="SelectTargets",
        options=tuple(options),
        min_select=min_sel,
        max_select=max_sel,
        can_cancel=can_cancel,
        source_label=source_label,
        slots=tuple(slots),
    )


def _build_select_n(
    poll: dict[str, Any],
    request_id: tuple[int, int],
    rtype: str,
    can_cancel: bool,
    source_label: str,
    resolve_name: Callable[[int], str],
    resolve_instance: Callable[[int], str] | None = None,
) -> PendingDecision | None:
    ids = poll.get("select_n_ids") or poll.get("search_candidates") or []
    is_search = "Search" in rtype or "Search" in str(poll.get("request_class") or "")
    if not is_search:
        color_decision = _build_color_choice(poll, request_id, can_cancel, source_label, resolve_instance)
        if color_decision is not None:
            return color_decision
    weights = poll.get("select_n_weights") or []
    if weights and len(weights) != len(ids):
        return None
    options: list[DecisionOption] = []
    for index, raw in enumerate(ids):
        if isinstance(raw, dict):
            oid = int(raw.get("id") or raw.get("instanceId") or raw.get("grpId") or 0)
            grp_id = int(raw.get("grpId") or 0)
        else:
            try:
                oid = int(raw)
            except (TypeError, ValueError):
                continue
            grp_id = 0
        if not oid:
            continue
        card_info = _default_card_resolver(grp_id) if is_search and grp_id else {}
        if is_search and isinstance(raw, dict):
            card_info = {**card_info, **raw}
        name = str(card_info.get("name") or "") or (resolve_name(grp_id) if grp_id else "")
        if not name and resolve_instance is not None:
            name = resolve_instance(oid)
        metadata = {"grpId": grp_id}
        # A SelectN choice is usually one of our own hand cards (a loot's
        # discard); the log can trail the bridge by the card just drawn, so an
        # unnamed "Option N" is a transient the typed path waits out.
        metadata["identity_known"] = bool(name) and not is_unknown_card_name(name)
        if is_search:
            metadata["card"] = {
                key: card_info[key]
                for key in ("name", "oracle_text", "type_line", "mana_cost", "cmc", "power", "toughness")
                if key in card_info
            }
        if weights:
            metadata["weight"] = int(weights[index])
        options.append(
            DecisionOption(
                option_id=f"sel:{oid}",
                label=name or f"Option {oid}",
                meta=metadata,
            )
        )
    min_select = int(poll.get("select_n_min", 1))
    max_select = int(poll.get("select_n_max", 0 if is_search else 1))
    if not options and not (is_search and min_select == 0):
        return None
    min_weight, max_weight = None, None
    if weights:
        min_weight = int(poll.get("select_n_min_weight", -(2**31)))
        max_weight = int(poll.get("select_n_max_weight", 2**31 - 1))
        if min_weight == -(2**31) and max_weight == 2**31 - 1:
            min_weight, max_weight = min_select, max_select
            min_select, max_select = 0, len(options)
    return PendingDecision(
        request_id=request_id,
        request_type="Search" if is_search else "SelectN",
        options=tuple(options),
        min_select=min_select,
        max_select=max_select,
        can_cancel=can_cancel,
        source_label=source_label,
        min_weight=min_weight,
        max_weight=max_weight,
    )


def _prompt_card_instance(poll: dict[str, Any]) -> int:
    """The ``CardId`` prompt parameter of the request (the card asking), or 0."""
    payload = poll.get("request_payload") or {}
    prompt = payload.get("prompt") if isinstance(payload, dict) else None
    parameters = prompt.get("parameters") if isinstance(prompt, dict) else None
    for parameter in parameters or []:
        if isinstance(parameter, dict) and str(parameter.get("parameterName") or "") == "CardId":
            try:
                return int(parameter.get("numberValue") or parameter.get("value") or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def _build_color_choice(
    poll: dict[str, Any],
    request_id: tuple[int, int],
    can_cancel: bool,
    source_label: str,
    resolve_instance: Callable[[int], str] | None,
) -> PendingDecision | None:
    """A "choose a color" SelectN: options are the colour ids, not card ids.

    Arena sends no ids for the static colour list (Room of Refuge, live
    2026-10-07 18:39), so the generic builder saw nothing to offer and the
    autopilot fell back to stale priority actions. See color_choice.py.
    """
    from arenamcp.color_choice import CARD_COLORS, COLOR_LETTERS, color_selection_ids

    is_color = bool(poll.get("select_n_is_card_color") or poll.get("select_n_is_mana_color")) or None
    color_ids = color_selection_ids(
        list_type=poll.get("select_n_list_type"),
        static_list=poll.get("select_n_static_list"),
        context=poll.get("select_n_context"),
        ids=poll.get("select_n_ids") or [],
        is_color=is_color,
    )
    if color_ids is None:
        return None
    options = tuple(
        DecisionOption(
            option_id=f"sel:{color_id}",
            label=CARD_COLORS[color_id],
            meta={
                "choice": "color",
                "color": COLOR_LETTERS.get(CARD_COLORS[color_id], "C"),
                "color_id": color_id,
            },
        )
        for color_id in color_ids
    )
    if not source_label and resolve_instance is not None:
        asking = _prompt_card_instance(poll)
        if asking:
            source_label = resolve_instance(asking)
    return PendingDecision(
        request_id=request_id,
        request_type="SelectN",
        options=options,
        min_select=int(poll.get("select_n_min", 1)),
        max_select=max(1, int(poll.get("select_n_max", 1))),
        can_cancel=can_cancel,
        source_label=source_label,
    )


@dataclass(frozen=True)
class _GroupSpec:
    """One GroupSpecification: a destination with its card-count bounds."""

    index: int
    zone: str  # "Library", "Graveyard", "Hand", ... ("" = unrecognized)
    sub_zone: str  # "Top", "Bottom" or ""
    lower: int
    upper: int  # 0 = unbounded


def _bound(spec: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = spec.get(key)
        if isinstance(value, dict):
            value = value.get("v", 0)
        try:
            number = int(value or 0)
        except (TypeError, ValueError):
            number = 0
        if number:
            return number
    return 0


def _group_specs(raw_specs: Iterable[Any] | None) -> list[_GroupSpec] | None:
    specs: list[_GroupSpec] = []
    for index, spec in enumerate(raw_specs or []):
        if not isinstance(spec, dict):
            return None
        specs.append(
            _GroupSpec(
                index=index,
                zone=_zone_name(spec.get("zoneType") or spec.get("zone")),
                sub_zone=_sub_zone_name(
                    spec.get("subZoneType") or spec.get("subZone") or spec.get("sub_zone")
                ),
                lower=_bound(spec, "lowerBound", "lower_bound"),
                upper=_bound(spec, "upperBound", "upper_bound"),
            )
        )
    return specs


def is_london_group(raw_specs: Iterable[Any] | None, context: Any = "") -> bool:
    """London-mulligan bottoming: [Hand keep, Library/Bottom put-back].

    Only this shape means "chosen cards go to the bottom". Scry
    ([Library/Top, Library/Bottom]) and surveil ([Library/Top, Graveyard])
    must be answered spec by spec — treating any Library spec as the bottom
    slot sent 7 of 7 surveilled cards to the graveyard on 2026-10-06.
    """
    if "LondonMulligan" in _enum_text(context):
        return True
    specs = _group_specs(raw_specs) or []
    return (
        len(specs) == 2
        and any(s.zone == "Hand" for s in specs)
        and any(s.zone == "Library" and s.sub_zone == "Bottom" for s in specs)
    )


def _group_context_name(context: str) -> str:
    text = context.removeprefix("GroupingContext_")
    return text if text in ("Scry", "Surveil") else ""


def _build_group(
    poll: dict[str, Any],
    request_id: tuple[int, int],
    can_cancel: bool,
    source_label: str,
    resolve_instance: Callable[[int], str] | None,
    resolve_card: Callable[[int], dict[str, Any]] | None = None,
) -> PendingDecision | None:
    """GroupRequest — London bottoming, scry/surveil and other split windows.

    London mulligan: each option is a card; CHOSEN options go to the bottom
    group (Library/Bottom), the rest keep (Hand/Top), mirroring MTGA's
    LondonWorkflow. Every other request with two or more destination specs
    becomes one ``grp:<iid>:<dest>`` option per card per destination; the
    planner picks exactly one destination per card and the response answers
    the specs in their own order, as MTGA's Scry/Surveil/GroupWorkflow do.
    Single-spec ordering windows fall back to the legacy safe-default handler.
    """
    payload = poll.get("request_payload") or {}
    raw_ids = poll.get("group_instance_ids") or payload.get("instanceIds") or []
    instance_ids: list[int] = []
    for v in raw_ids:
        try:
            iid = int(v)
        except (TypeError, ValueError):
            continue
        if iid and iid not in instance_ids:
            instance_ids.append(iid)
    if not instance_ids:
        return None

    raw_specs = poll.get("group_specs") or payload.get("groupSpecs") or []
    context = _enum_text(poll.get("group_context") or payload.get("context"))
    if not is_london_group(raw_specs, context):
        return _build_destination_group(
            instance_ids,
            raw_specs,
            context,
            request_id,
            can_cancel,
            source_label,
            resolve_instance,
            resolve_card,
        )

    specs = _group_specs(raw_specs) or []
    bottom_count = max(
        (max(s.lower, s.upper) for s in specs if s.zone == "Library" and s.sub_zone == "Bottom"),
        default=0,
    )
    if bottom_count <= 0 and "LondonMulligan" in context:
        bottom_count = max(0, len(instance_ids) - 7)
    if bottom_count <= 0 or bottom_count > len(instance_ids):
        return None  # unknown shape → legacy safe-default path

    options = []
    for iid in instance_ids:
        name = ""
        if resolve_instance is not None:
            try:
                name = resolve_instance(iid) or ""
            except Exception:
                name = ""
        options.append(
            DecisionOption(
                option_id=f"grp:{iid}",
                label=(f"Bottom {name}" if name else f"Bottom card #{iid}"),
                meta={"instance_id": iid},
            )
        )
    return PendingDecision(
        request_id=request_id,
        request_type="Group",
        options=tuple(options),
        min_select=bottom_count,
        max_select=bottom_count,
        can_cancel=can_cancel,
        source_label=source_label or context,
    )


def _group_card(
    iid: int,
    resolve_instance: Callable[[int], str] | None,
    resolve_card: Callable[[int], dict[str, Any]] | None,
) -> dict[str, Any]:
    card: dict[str, Any] = {}
    if resolve_card is not None:
        try:
            card = dict(resolve_card(iid) or {})
        except Exception:
            card = {}
    name = ""
    if resolve_instance is not None:
        try:
            name = resolve_instance(iid) or ""
        except Exception:
            name = ""
    name = name or str(card.get("name") or "")
    known = bool(name) and not is_unknown_card_name(name)
    card["name"] = name if known else ""
    card["display"] = name if known else f"card #{iid}"
    return card


def _destination_slug(spec: _GroupSpec) -> str:
    if spec.zone == "Library":
        return {"Top": "top", "Bottom": "bottom"}.get(spec.sub_zone, "library")
    return spec.zone.lower() + (f"_{spec.sub_zone.lower()}" if spec.sub_zone else "")


def _destination_label(card: dict[str, Any], spec: _GroupSpec, context: str) -> str:
    name = card["display"]
    zone, sub = spec.zone, spec.sub_zone
    if zone == "Library" and sub == "Top":
        # Scry/surveil cards are already on top: leaving them is a real choice.
        text = f"Keep {name} on top of your library" if context else f"Put {name} on top of your library"
    elif zone == "Library" and sub == "Bottom":
        text = f"Put {name} on the bottom of your library"
    elif zone == "Library":
        text = f"Put {name} into your library"
    elif zone == "Graveyard":
        text = f"Put {name} into your graveyard"
    elif zone == "Hand":
        text = f"Put {name} into your hand"
    elif zone == "Exile":
        text = f"Exile {name}"
    elif zone == "Battlefield":
        text = f"Put {name} onto the battlefield"
    else:
        text = f"Put {name} into {zone.lower()}" + (f" ({sub.lower()})" if sub else "")
    facts = " ".join(
        part
        for part in (clean_rules_text(str(card.get("mana_cost") or "")), str(card.get("type_line") or ""))
        if part
    )
    return f"{text} ({facts})" if facts else text


def _build_destination_group(
    instance_ids: list[int],
    raw_specs: Iterable[Any],
    context: str,
    request_id: tuple[int, int],
    can_cancel: bool,
    source_label: str,
    resolve_instance: Callable[[int], str] | None,
    resolve_card: Callable[[int], dict[str, Any]] | None,
) -> PendingDecision | None:
    specs = _group_specs(raw_specs)
    if not specs or len(specs) < 2 or any(not spec.zone for spec in specs):
        return None  # ordering window or unreadable specs → legacy path
    count = len(instance_ids)
    if sum(s.lower for s in specs) > count or (
        all(s.upper for s in specs) and sum(s.upper for s in specs) < count
    ):
        return None
    slugs: list[str] = []
    for spec in specs:
        slug = _destination_slug(spec)
        slugs.append(slug if slug not in slugs else f"{slug}{spec.index}")
    kind = _group_context_name(context)
    cards = {iid: _group_card(iid, resolve_instance, resolve_card) for iid in instance_ids}
    options = []
    # Destination-major: the first N options send every card to the first
    # spec (Library/Top for scry/surveil, i.e. "change nothing"), so a
    # take-the-first-N fallback is always a complete, harmless answer.
    for spec in specs:
        for iid in instance_ids:
            card = cards[iid]
            options.append(
                DecisionOption(
                    option_id=f"grp:{iid}:{slugs[spec.index]}",
                    label=_destination_label(card, spec, kind),
                    meta={
                        "instance_id": iid,
                        "spec_index": spec.index,
                        "zone": spec.zone,
                        "sub_zone": spec.sub_zone,
                        "lower": spec.lower,
                        "upper": spec.upper,
                        "card_name": card["name"],
                        "card_grp_id": int(card.get("grp_id") or 0),
                        "type_line": str(card.get("type_line") or ""),
                        "mana_cost": str(card.get("mana_cost") or ""),
                        "cmc": card.get("cmc"),
                    },
                )
            )
    origin = source_label if source_label and not source_label.startswith("{") else ""
    title = f"{kind} {count}" if kind else "Split cards"
    if origin and origin not in (context, kind):
        title += f" from {origin}"
    rule = "choose exactly one destination for each card"
    if count > 1 and any(s.zone == "Library" and s.sub_zone == "Top" for s in specs):
        rule += "; cards kept on top are stacked in the order listed (first = top)"
    return PendingDecision(
        request_id=request_id,
        request_type="Group",
        options=tuple(options),
        min_select=count,
        max_select=count,
        can_cancel=can_cancel,
        source_label=f"{title} — {rule}",
    )


def destination_groups(decision: PendingDecision, chosen: list[str]) -> list[dict[str, Any]] | None:
    """The GroupResp groups for a destination choice, or None if incomplete.

    Every card must get exactly one destination and every spec's bounds must
    hold. Groups come back in spec order with the spec's own zone/subZone —
    Arena reads them positionally (2026-10-06: a Library/Bottom group in the
    second slot of a surveil was read as the Graveyard slot).
    """
    picked = [decision.find(option_id) for option_id in chosen]
    if not picked or any(o is None or "spec_index" not in o.meta for o in picked):
        return None
    specs = {o.meta["spec_index"]: o.meta for o in decision.options if "spec_index" in o.meta}
    cards = {o.meta["instance_id"] for o in decision.options if "spec_index" in o.meta}
    ids = [o.meta["instance_id"] for o in picked]
    if len(ids) != len(set(ids)) or set(ids) != cards:
        return None
    members: dict[int, list[int]] = {index: [] for index in specs}
    for option in picked:
        members[option.meta["spec_index"]].append(option.meta["instance_id"])
    groups = []
    for index in sorted(specs):
        spec = specs[index]
        size = len(members[index])
        if size < int(spec.get("lower") or 0) or (spec.get("upper") and size > int(spec["upper"])):
            return None
        groups.append({"ids": members[index], "zone": spec["zone"], "sub_zone": spec.get("sub_zone") or None})
    return groups


def _card_mana_value(card: dict[str, Any]) -> float | None:
    value = card.get("cmc")
    if value in (None, ""):
        cost = str(card.get("mana_cost") or "")
        if not cost:
            return None
        value = 0
        for symbol in re.findall(r"\{([^}]*)\}", cost):
            if symbol.isdigit():
                value += int(symbol)
            elif symbol.upper() not in ("X", "Y", "Z"):
                value += 1
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_land_card(card: dict[str, Any]) -> bool:
    front = str(card.get("type_line") or "").split("//")[0]
    return "Land" in front or "Land" in (card.get("card_types") or [])


def _mana_profile(game_state: dict[str, Any] | None) -> tuple[int, int] | None:
    """(lands in play + in hand, biggest nonland mana value in hand)."""
    if not isinstance(game_state, dict) or "battlefield" not in game_state or "hand" not in game_state:
        return None
    seat = game_state.get("local_seat_id") or next(
        (p.get("seat_id") for p in game_state.get("players") or [] if p.get("is_local")), None
    )
    in_play = sum(
        1
        for card in game_state.get("battlefield") or []
        if isinstance(card, dict)
        and _is_land_card(card)
        and (seat is None or (card.get("controller_seat_id") or card.get("owner_seat_id")) == seat)
    )
    hand = [card for card in game_state.get("hand") or [] if isinstance(card, dict)]
    in_hand = sum(1 for card in hand if _is_land_card(card))
    biggest = max(
        (_card_mana_value(card) or 0 for card in hand if not _is_land_card(card)),
        default=0,
    )
    return in_play + in_hand, int(biggest)


def _worth_keeping(meta: dict[str, Any], profile: tuple[int, int] | None) -> bool:
    if profile is None or not meta.get("type_line"):
        return True  # unknown card or board: leaving it on top changes nothing
    lands, biggest = profile
    if _is_land_card(meta):
        return lands < max(4, min(6, biggest))
    mana_value = _card_mana_value(meta)
    return mana_value is None or mana_value <= lands + 1


_BASIC_TYPE_COLORS = {"plains": "W", "island": "U", "swamp": "B", "mountain": "R", "forest": "G"}


def _land_card_colors(card: dict[str, Any]) -> set[str]:
    """Colours a land makes, from its basic land types (good enough for the flood guard)."""
    text = str(card.get("type_line") or "").lower()
    return {color for word, color in _BASIC_TYPE_COLORS.items() if word in text}


def land_flood_group_choice(
    decision: PendingDecision | None, game_state: dict[str, Any] | None
) -> tuple[list[str], str]:
    """Scry/surveil of nothing but lands the board no longer needs: bin them, no model call.

    2026-10-09 08:04 (sealed FRA): fourteen lands in play, a Mountain in hand,
    the surveilled card a Mountain — the model kept it on top "to guarantee a
    land draw". Returns ([], "") whenever anything argues for keeping a card:
    a non-land in the group, an unknown card or board, a land still wanted by
    :func:`_worth_keeping`, or a land adding a colour the hand needs and no
    land of ours makes. The choice itself comes from :func:`default_group_choice`.
    """
    if decision is None or decision.request_type != "Group" or not game_state:
        return [], ""
    cards: dict[int, dict[str, Any]] = {}
    for option in decision.options:
        if "spec_index" not in option.meta or not option.meta.get("type_line"):
            return [], ""
        cards[option.meta["instance_id"]] = option.meta
    if not cards or not all(_is_land_card(meta) for meta in cards.values()):
        return [], ""
    profile = _mana_profile(game_state)
    if profile is None or any(_worth_keeping(meta, profile) for meta in cards.values()):
        return [], ""
    seat = game_state.get("local_seat_id")
    ours = [
        card
        for card in game_state.get("battlefield") or []
        if isinstance(card, dict)
        and _is_land_card(card)
        and (seat is None or (card.get("controller_seat_id") or card.get("owner_seat_id")) == seat)
    ]
    have = set().union(*(_land_card_colors(card) for card in ours)) if ours else set()
    needed = {
        symbol.upper()
        for card in game_state.get("hand") or []
        if isinstance(card, dict) and not _is_land_card(card)
        for symbol in re.findall(r"[WUBRG]", str(card.get("mana_cost") or ""))
    }
    if any(_land_card_colors(meta) & (needed - have) for meta in cards.values()):
        return [], ""
    choice = default_group_choice(decision, game_state)
    tops = {
        option.option_id
        for option in decision.options
        if option.meta.get("zone") == "Library" and option.meta.get("sub_zone") == "Top"
    }
    if not choice or any(option_id in tops for option_id in choice):
        return [], ""
    lands, biggest = profile
    names = ", ".join(str(meta.get("card_name") or "a land") for meta in cards.values())
    reason = (
        f"{names} off the top: {lands} lands in play or hand already"
        + (f" against a biggest spell costing {biggest}" if biggest else "")
        + ", and no colour in it that the hand needs."
    )
    return choice, reason


def default_group_choice(
    decision: PendingDecision | None, game_state: dict[str, Any] | None = None
) -> list[str]:
    """Safe scry/surveil answer when the planner gives no usable choice.

    Lands the hand still needs and spells castable within about a turn stay
    on top; excess lands and spells far above the mana in reach go to the
    other destination (graveyard for surveil, bottom for scry). Unknown cards
    and an unknown board leave everything on top — the same as not
    surveilling. Returns [] for London/ordering decisions.
    """
    if decision is None or decision.request_type != "Group":
        return []
    by_card: dict[int, dict[int, DecisionOption]] = {}
    for option in decision.options:
        if "spec_index" not in option.meta:
            return []
        by_card.setdefault(option.meta["instance_id"], {})[option.meta["spec_index"]] = option
    if not by_card:
        return []
    spec_meta = {o.meta["spec_index"]: o.meta for o in decision.options}
    order = sorted(spec_meta)
    keep_index = next(
        (i for i in order if spec_meta[i]["zone"] == "Library" and spec_meta[i]["sub_zone"] == "Top"),
        order[0],
    )
    away_index = next((i for i in order if i != keep_index), keep_index)
    profile = _mana_profile(game_state)
    keep, away = [], []
    for destinations in by_card.values():
        meta = destinations[keep_index].meta
        if _worth_keeping(meta, profile):
            keep.append(destinations[keep_index].option_id)
        else:
            away.append(destinations[away_index].option_id)
    for choice in (keep + away, [d[keep_index].option_id for d in by_card.values()]):
        if decision.selection_is_valid(choice):
            return choice
    if len(by_card) <= 6:
        for combo in itertools.product(order, repeat=len(by_card)):
            choice = [d[i].option_id for d, i in zip(by_card.values(), combo, strict=True)]
            if decision.selection_is_valid(choice):
                return choice
    return []


_LEAD_VERBS = {
    "cast": "ActionType_Cast",
    "casting": "ActionType_Cast",
    "play": "ActionType_Play",
    "playing": "ActionType_Play",
    "activate": "ActionType_Activate",
    "activating": "ActionType_Activate",
    "cycle": "ActionType_Activate",
    "cycling": "ActionType_Activate",
    "landcycle": "ActionType_Activate",
    "landcycling": "ActionType_Activate",
    "channel": "ActionType_Activate",
    "channeling": "ActionType_Activate",
}


def reasoning_choice_conflict(
    decision: PendingDecision,
    chosen: list[str],
    reasoning: str,
    resolve_name: Callable[[int], str] = _default_name_resolver,
) -> list[str]:
    """Options the reasoning's lead clause describes when the chosen id is another card.

    2026-10-06 G1 T8: "Cycling Undulating Witness for {2} digs for a land
    drop…; Tam's Resistance has no creature to buff." arrived with idx:1 —
    Cast Tam's Resistance — and that is what was submitted. Deliberately
    narrow: only a single ActionsAvailable pick, only when the reasoning
    opens with an action verb (cast/play/activate/cycle…) naming a card the
    chosen option's card is absent from, and only options of that verb's
    action type. Returns [] whenever the intent is unclear.
    """
    if decision.request_type != "ActionsAvailable" or len(chosen) != 1:
        return []
    picked = decision.find(chosen[0])
    if picked is None or not picked.meta.get("grpId"):
        return []
    lead = re.split(r"[.;!?](?:\s|$)|\s[—–]\s", (reasoning or "").strip(), maxsplit=1)[0]
    verb = re.match(r"\s*(?:i(?:'ll| will| plan to)\s+)?([a-z]+)\b", lead, re.I)
    action_type = _LEAD_VERBS.get(verb.group(1).lower()) if verb else None
    if not action_type:
        return []
    rest = lead[verb.end() :].lower()

    def names(option: DecisionOption) -> list[str]:
        try:
            full = (resolve_name(int(option.meta.get("grpId") or 0)) or "").strip().lower()
        except Exception:
            return []
        short = full.split(",")[0].strip()
        return [n for n in dict.fromkeys((full, short)) if len(n) >= 4]

    picked_names = names(picked)
    if not picked_names or any(n in lead.lower() for n in picked_names):
        return []
    first: tuple[int, int] | None = None  # (position, grpId) of the first card the lead names
    for option in decision.options:
        for name in names(option):
            position = rest.find(name)
            if position >= 0 and (first is None or position < first[0]):
                first = (position, int(option.meta.get("grpId") or 0))
    if first is None:
        return []
    return [
        option.option_id
        for option in decision.options
        if int(option.meta.get("grpId") or 0) == first[1]
        and option.meta.get("actionType") == action_type
        and option.payable is not False
        and option.option_id != picked.option_id
    ]


# ---------------------------------------------------------------------------
# (De)serialization — used by the stall corpus (fable item 5)
# ---------------------------------------------------------------------------


def decision_to_dict(decision: PendingDecision) -> dict[str, Any]:
    return {
        "request_id": list(decision.request_id),
        "request_type": decision.request_type,
        "options": [
            {
                "option_id": o.option_id,
                "label": o.label,
                "payable": o.payable,
                "meta": o.meta,
            }
            for o in decision.options
        ],
        "min_select": decision.min_select,
        "max_select": decision.max_select,
        "can_pass": decision.can_pass,
        "can_cancel": decision.can_cancel,
        "source_label": decision.source_label,
        "min_weight": decision.min_weight,
        "max_weight": decision.max_weight,
        "slots": [
            {
                "target_idx": s.target_idx,
                "min_targets": s.min_targets,
                "max_targets": s.max_targets,
                "selected": s.selected,
                "candidate_ids": list(s.candidate_ids),
            }
            for s in decision.slots
        ],
    }


def decision_from_dict(data: dict[str, Any]) -> PendingDecision:
    return PendingDecision(
        request_id=tuple(data.get("request_id") or (0, 0)),  # type: ignore[arg-type]
        request_type=str(data.get("request_type") or ""),
        options=tuple(
            DecisionOption(
                option_id=str(o.get("option_id") or ""),
                label=str(o.get("label") or ""),
                payable=o.get("payable"),
                meta=o.get("meta") or {},
            )
            for o in (data.get("options") or [])
        ),
        min_select=int(data.get("min_select", 1)),
        max_select=int(data.get("max_select", 1)),
        can_pass=bool(data.get("can_pass")),
        can_cancel=bool(data.get("can_cancel")),
        source_label=str(data.get("source_label") or ""),
        min_weight=data.get("min_weight"),
        max_weight=data.get("max_weight"),
        slots=tuple(
            TargetSlot(
                target_idx=int(s.get("target_idx") or 0),
                min_targets=int(s.get("min_targets") or 1),
                max_targets=int(s.get("max_targets") or 1),
                selected=int(s.get("selected") or 0),
                candidate_ids=tuple(int(i) for i in (s.get("candidate_ids") or [])),
            )
            for s in (data.get("slots") or [])
        ),
    )


# ---------------------------------------------------------------------------
# Submission by option id
# ---------------------------------------------------------------------------


def _tgt_iid(option_id: str) -> int | None:
    if not option_id.startswith("tgt:"):
        return None
    try:
        return int(option_id.split(":", 1)[1])
    except (ValueError, IndexError):
        return None


def assign_target_slots(slots: tuple[TargetSlot, ...], preferred: list[int]) -> list[list[int]] | None:
    """Match every explicit choice to slot capacity without inventing targets."""
    if len(set(preferred)) != len(preferred) or sum(slot.needs for slot in slots) > len(preferred):
        return None
    rows = [index for index, slot in enumerate(slots) for _ in range(slot.needs)]
    required = len(rows)
    for index, slot in enumerate(slots):
        capacity = max(0, slot.max_targets - slot.selected)
        if slot.needs > capacity:
            return None
        rows.extend([index] * min(len(preferred), capacity - slot.needs))
    owners: dict[int, int] = {}

    def match(row: int, seen: set[int]) -> bool:
        for instance_id in preferred:
            if instance_id in seen or instance_id not in slots[rows[row]].candidate_ids:
                continue
            seen.add(instance_id)
            if instance_id not in owners or match(owners[instance_id], seen):
                owners[instance_id] = row
                return True
        return False

    for row in range(len(rows)):
        if row >= required and len(owners) == len(preferred):
            break
        if not match(row, set()) and row < required:
            return None
    if len(owners) != len(preferred):
        return None
    result: list[list[int]] = [[] for _ in slots]
    for instance_id in preferred:
        result[rows[owners[instance_id]]].append(instance_id)
    return result


def expand_target_selection(decision: PendingDecision, chosen_ids: list[str]) -> list[int]:
    """Validate complete target coverage and preserve every explicit chosen id."""
    if not decision.selection_is_valid(chosen_ids):
        return []
    preferred = [_tgt_iid(option_id) for option_id in chosen_ids]
    if any(instance_id is None for instance_id in preferred):
        return []
    if not decision.slots:
        return preferred
    assigned = assign_target_slots(decision.slots, preferred)
    if assigned is None:
        return []
    return [instance_id for slot_ids in assigned for instance_id in slot_ids]


def submit_option(
    bridge: Any,
    decision: PendingDecision,
    option_ids: list[str],
) -> bool:
    """Submit the chosen option(s) through the bridge by id.

    Mechanical validation: ids outside the decision's option set are
    rejected here — there is no string matching and no legality heuristic.
    """
    if decision.request_type == "Search":
        if not decision.selection_is_valid(option_ids):
            return False
        return bool(bridge.submit_selection([int(oid.split(":", 1)[1]) for oid in option_ids]))
    if decision.request_type == "CastingTimeOptions":
        if not decision.selection_is_valid(option_ids):
            logger.warning("Refusing incomplete or mixed casting choices: %s", option_ids)
            return False
        if len(option_ids) > 1:
            # One atomic modal response: submitting the first option alone
            # leaves choose-two requests invalid and discards the second mode.
            return bool(
                bridge.submit_casting_options(
                    [int(option_id.split(":", 1)[1]) for option_id in option_ids],
                    expected=[
                        {
                            **decision.find(option_id).meta,
                            "gameStateId": decision.request_id[0],
                            "msgId": decision.request_id[1],
                        }
                        for option_id in option_ids
                    ],
                )
            )
    if decision.request_type == "SelectTargets" and not option_ids:
        # "Up to N" targets answered with none (more): commit the selection as is.
        if not decision.selection_is_valid([]):
            return False
        return bool(bridge.submit_targets([]))
    valid = decision.option_ids()
    chosen = [oid for oid in option_ids if oid in valid]
    if decision.request_type == "ActionsAvailable" and any(
        decision.find(oid).payable is False for oid in chosen
    ):
        return False
    if not chosen:
        logger.warning(
            "submit_option: none of %s are valid for %s (valid: %s)",
            option_ids,
            decision.request_type,
            sorted(valid),
        )
        return False
    if len(chosen) < len(option_ids):
        logger.info(
            "submit_option: dropped invalid ids %s",
            [o for o in option_ids if o not in valid],
        )

    first = chosen[0]
    if first == "pass":
        return bool(bridge.submit_pass())
    if first.startswith("mull:"):
        return bool(bridge.submit_mulligan(first == "mull:keep"))
    if first.startswith("optional:"):
        return bool(
            bridge.submit_optional(first == "optional:accept", expected_request_id=decision.request_id)
        )
    if first.startswith("idx:"):
        # Identity travels with the index so a bridge that checks it (the
        # native Mac bridge) refuses a stale index instead of submitting
        # whatever now sits at that position.
        meta = decision.find(first).meta
        expected = {k: meta[k] for k in ("instanceId", "grpId", "abilityGrpId") if meta.get(k)}
        if decision.request_type == "CastingTimeOptions":
            expected = {**meta, "gameStateId": decision.request_id[0], "msgId": decision.request_id[1]}
        index = int(first.split(":", 1)[1])
        if expected:
            return bool(bridge.submit_action_by_index(index, expected=expected))
        return bool(bridge.submit_action_by_index(index))
    if first.startswith("tgt:"):
        # Cover every required slot, not just the first chosen target.
        target_ids = expand_target_selection(decision, chosen)
        if not target_ids:
            return False
        return bool(bridge.submit_targets(target_ids))
    if first.startswith("sel:"):
        if not decision.selection_is_valid(chosen):
            return False
        ids = [int(o.split(":", 1)[1]) for o in chosen if o.startswith("sel:")]
        return bool(bridge.submit_selection(ids))
    if first.startswith("grp:") and "spec_index" in decision.find(first).meta:
        groups = destination_groups(decision, chosen)
        if groups is None:
            logger.warning(
                "submit_option: refusing incomplete group assignment %s (%s)", chosen, decision.source_label
            )
            return False
        return bool(bridge.submit_group(groups))
    if first.startswith("grp:"):
        # Chosen = bottom; the rest of the option set keeps (LondonWorkflow
        # response shape: [Hand/Top keep group, Library/Bottom group]).
        bottom = [int(o.split(":", 1)[1]) for o in chosen if o.startswith("grp:")]
        all_ids = [
            int(o.option_id.split(":", 1)[1]) for o in decision.options if o.option_id.startswith("grp:")
        ]
        keep = [i for i in all_ids if i not in bottom]
        groups = [
            {"ids": keep, "zone": "Hand", "sub_zone": "Top"},
            {"ids": bottom, "zone": "Library", "sub_zone": "Bottom"},
        ]
        return bool(bridge.submit_group(groups))
    logger.warning("submit_option: unknown option id scheme %r", first)
    return False


_DISCARD_TEXT = re.compile(
    r"\bdiscards? (?:a|one|two|three|that|those|\d+) cards?\b|\bdiscard a card\b", re.IGNORECASE
)


def select_n_discard_note(decision: Any, state: dict[str, Any]) -> str:
    """A prompt line saying that this SelectN picks what we DISCARD, or ''.

    2026-10-08 game 1 (match 07c043d8): Arni's loot ("Draw a card, then
    discard a card") presents a SelectN over our hand with no source label.
    The model read it as "choose a card to keep" five times and discarded
    the Swamp it needed for Garruk (T7), Garruk itself (T11), Screeching
    Soulbreaker (T13) and Plan for All Outcomes (T15), each with a rationale
    praising the card it was throwing away. The framing is deterministic:
    every option is one of our hand cards and the resolving stack object
    (or the decision's source) says "discard".
    """
    if getattr(decision, "request_type", "") != "SelectN":
        return ""
    hand_ids = {
        int(card.get("instance_id") or 0) for card in state.get("hand") or [] if isinstance(card, dict)
    }
    ids = []
    for option in getattr(decision, "options", ()) or ():
        option_id = str(getattr(option, "option_id", "") or "")
        if not option_id.startswith("sel:"):
            return ""
        try:
            ids.append(int(option_id[4:]))
        except ValueError:
            return ""
    if not ids or not hand_ids or any(i not in hand_ids for i in ids):
        return ""
    sources = [str(getattr(decision, "source_label", "") or "")]
    sources += [
        f"{item.get('name') or ''}: {item.get('oracle_text') or ''}"
        for item in state.get("stack") or []
        if isinstance(item, dict)
    ]
    source = next((text for text in sources if _DISCARD_TEXT.search(text)), "")
    if not source:
        return ""
    return (
        "THIS IS A DISCARD: the card you pick goes to your graveyard "
        f"(resolving: {source.strip()[:120]}). Pick the card you need LEAST — keep removal, "
        "planeswalkers, bombs and any land you still need for your colours; discard extra lands or the "
        "weakest spell. The card's value is a reason NOT to pick it."
    )
