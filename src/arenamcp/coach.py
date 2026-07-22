"""Coach engine with pluggable LLM backends for MTG game coaching.

This module provides the CoachEngine for getting strategic advice from LLMs,
with support for online (mtgacoach.com) and local (Ollama/LM Studio) modes.
"""

import json
import logging
import os
import re
import time
from collections import Counter
from typing import Any, Optional

from arenamcp.backends import LLMBackend, ProxyBackend

logger = logging.getLogger(__name__)


def _compact_gre_target(target: Any) -> Any:
    """Reduce GRE target payloads to compact prompt-friendly fields."""
    if not isinstance(target, dict):
        return target

    compact = {}
    for key in ("targetType", "instanceId", "grpId", "zoneId", "seatId", "selection", "index"):
        value = target.get(key)
        if value not in (None, "", [], {}):
            compact[key] = value
    return compact or target


def _compact_legal_action_for_prompt(action: Any) -> Any:
    """Reduce raw GRE legal actions to the fields most useful to the model."""
    if not isinstance(action, dict):
        return action

    compact = {}
    for key in (
        "actionType",
        "grpId",
        "instanceId",
        "abilityGrpId",
        "sourceId",
        "alternativeGrpId",
        "selectionType",
        "selection",
        "shouldStop",
        "maxActivations",
        "isBatchable",
        "highlight",
    ):
        value = action.get(key)
        if value not in (None, "", [], {}):
            compact[key] = value

    targets = action.get("targets")
    if isinstance(targets, list) and targets:
        compact["targets"] = [_compact_gre_target(t) for t in targets[:4]]

    mana_options = action.get("manaPaymentOptions")
    if isinstance(mana_options, list) and mana_options:
        compact["manaPaymentOptionsCount"] = len(mana_options)

    costs = action.get("costs")
    if isinstance(costs, list) and costs:
        compact["costCount"] = len(costs)

    return compact or action


def _format_legal_actions_raw_for_prompt(
    actions: list[dict[str, Any]],
    max_actions: int = 12,
) -> str:
    """Format raw GRE legal actions compactly for prompt context."""
    if not actions:
        return "[]"

    compact_actions = [
        _compact_legal_action_for_prompt(action)
        for action in actions[:max_actions]
    ]
    suffix = " …" if len(actions) > max_actions else ""
    return json.dumps(compact_actions, separators=(",", ":")) + suffix


_ACTIONS_AVAILABLE_BRIDGE_REQUESTS = {
    "ActionsAvailable",
    "ActionsAvailableReq",
    "ActionsAvailableRequest",
}


def _compact_prompt_value(
    value: Any,
    *,
    max_depth: int = 4,
    max_list_items: int = 10,
    max_dict_items: int = 16,
    max_string_length: int = 240,
    _depth: int = 0,
) -> Any:
    """Compact nested JSON-like data into a bounded prompt-friendly structure."""
    if _depth >= max_depth:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        text = value if isinstance(value, str) else repr(value)
        return text if len(text) <= max_string_length else text[: max_string_length - 3] + "..."

    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, str):
        return value if len(value) <= max_string_length else value[: max_string_length - 3] + "..."

    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        for idx, (key, child) in enumerate(value.items()):
            if idx >= max_dict_items:
                compact["_truncated"] = True
                break
            compact[str(key)] = _compact_prompt_value(
                child,
                max_depth=max_depth,
                max_list_items=max_list_items,
                max_dict_items=max_dict_items,
                max_string_length=max_string_length,
                _depth=_depth + 1,
            )
        return compact

    if isinstance(value, (list, tuple)):
        compact = [
            _compact_prompt_value(
                child,
                max_depth=max_depth,
                max_list_items=max_list_items,
                max_dict_items=max_dict_items,
                max_string_length=max_string_length,
                _depth=_depth + 1,
            )
            for child in value[:max_list_items]
        ]
        if len(value) > max_list_items:
            compact.append({"_truncated": True})
        return compact

    text = repr(value)
    return text if len(text) <= max_string_length else text[: max_string_length - 3] + "..."


def _format_bounded_json_for_prompt(
    value: Any,
    *,
    max_chars: int = 5000,
) -> str:
    """Format bounded JSON data into a single prompt line."""
    text = json.dumps(_compact_prompt_value(value), separators=(",", ":"))
    return text if len(text) <= max_chars else text[: max_chars - 3] + "..."


def _format_raw_gre_events_for_prompt(
    events: list[dict[str, Any]],
    *,
    max_events: int = 4,
) -> str:
    """Format a bounded tail of raw GRE events for richer online prompts."""
    if not events:
        return "[]"

    compact_events: list[dict[str, Any]] = []
    for event in events[-max_events:]:
        compact: dict[str, Any] = {}
        for key in ("seq", "type", "turn", "phase", "seat_id", "message_index", "payload_truncated"):
            value = event.get(key)
            if value not in (None, "", [], {}):
                compact[key] = value
        payload = event.get("payload")
        if payload not in (None, "", [], {}):
            compact["payload"] = _compact_prompt_value(payload, max_depth=3, max_list_items=8, max_dict_items=12)
        if compact:
            compact_events.append(compact)

    return json.dumps(compact_events, separators=(",", ":"))


def _build_bridge_context_lines(
    game_state: dict[str, Any],
    raw_legal_actions: list[dict[str, Any]],
    *,
    for_planner: bool = False,
) -> list[str]:
    """Render bounded bridge/GRE context into prompt lines.

    Args:
        for_planner: If True, omit heavy raw GRE JSON dumps (LegalGRE,
            GRE_RequestPayload, GRE_Recent). The planner only needs the
            request type to route decisions; the bridge action matcher
            consumes raw legal actions out-of-band, not via the prompt.
    """
    lines: list[str] = []
    bridge_req = game_state.get("_bridge_request_type")
    bridge_request_class = game_state.get("_bridge_request_class")
    bridge_request_payload = game_state.get("_bridge_request_payload")
    raw_gre_events = game_state.get("raw_gre_events") or []

    if raw_legal_actions and not for_planner:
        lines.append("LegalGRE: " + _format_legal_actions_raw_for_prompt(raw_legal_actions))
    if bridge_req:
        lines.append(f"GRE_Request: {bridge_req}")
    if bridge_request_class and bridge_request_class != bridge_req:
        lines.append(f"GRE_RequestClass: {bridge_request_class}")
    if bridge_request_payload and not for_planner:
        lines.append("GRE_RequestPayload: " + _format_bounded_json_for_prompt(bridge_request_payload))
    if raw_gre_events and not for_planner:
        lines.append("GRE_Recent: " + _format_raw_gre_events_for_prompt(raw_gre_events))

    return lines


_NON_PASSABLE_REQUEST_CLASSES = {
    "SelectTargetsRequest",
    "SelectNRequest",
    "SearchRequest",
    "GroupRequest",
    "DistributionRequest",
    "CastingTimeOptionRequest",
    "CastingTimeOption_ModalRequest",
    "CastingTimeOption_ChooseOrCostRequest",
    "CastingTimeOption_NumericInputRequest",
    "CastingTimeOption_Replicate",
    "CastingTimeOption_SelectNRequest",
    "CastingTimeOption_SpecializeRequest",
    "CastingTimeOption_KickerRequest",
    "CastingTimeOption_AdditionalCostRequest",
    "CastingTimeOption_CostKeywordRequest",
    "PayCostsRequest",
    "MulliganRequest",
}

_NON_PASSABLE_REQUEST_TYPES = {
    "SelectTargets",
    "SelectN",
    "Search",
    "Group",
    "Distribution",
    "PayCosts",
    "Mulligan",
}


def _fallback_non_action_advice(game_state: dict[str, Any]) -> str:
    """Pick a sensible fallback advice when legal_actions is empty.

    `pass priority` is the right answer for an idle Action request, but
    non-passable requests (target/mode/search/cast-time) need manual
    intervention instead. Surface that clearly rather than issuing a
    literal "pass priority" the user can't actually submit.
    """
    req_type = str(game_state.get("_bridge_request_type") or "")
    req_class = str(game_state.get("_bridge_request_class") or "")
    in_intermission = bool(game_state.get("_bridge_in_intermission"))
    can_pass = game_state.get("_bridge_can_pass")

    if in_intermission:
        return "Match ending — no action needed."

    non_passable = (
        req_class in _NON_PASSABLE_REQUEST_CLASSES
        or req_type in _NON_PASSABLE_REQUEST_TYPES
        or (can_pass is False and req_class)
    )
    if non_passable:
        if "Target" in req_class or req_type == "SelectTargets":
            return "Pick a target manually."
        if "Search" in req_class:
            return "Pick a card manually."
        if req_class.startswith("CastingTimeOption") or "Modal" in req_class:
            return "Choose a mode manually."
        if "PayCosts" in req_class or req_type == "PayCosts":
            return "Confirm the mana payment."
        if "Mulligan" in req_class:
            return "Make the mulligan call."
        return "Make this decision manually."

    return "pass priority"


# LLM Backend Protocol and Implementations


def _is_local_backend(be: Any) -> bool:
    """True when `be` is a ProxyBackend pointed at a local LLM server.

    Detects vLLM (port 8000), Ollama (11434), LM Studio (1234), and the
    legacy api_key markers we wrote out before the vLLM migration. Used to
    pick the longer LLM timeouts that local inference needs.
    """
    if not isinstance(be, ProxyBackend):
        return False
    url = (getattr(be, '_base_url', '') or '').lower()
    if any(host in url for host in ("localhost", "127.0.0.1", "0.0.0.0")):
        return True
    key = (getattr(be, '_api_key', '') or '').lower()
    return key in ("vllm", "ollama", "lm-studio")


def get_available_modes() -> list[tuple[str, str]]:
    """Return available backend modes.

    Returns list of ``(display_name, mode_id)`` tuples.
    Only online mode is available.
    """
    return [
        ("Online", "online"),
    ]


def get_models_for_mode(mode: str) -> list[tuple[str, Optional[str]]]:
    """Return models available for the given mode.

    Returns list of ``(display_name, model_id_or_None)`` tuples.
    ``None`` means "use the mode's default model".

    Queries the endpoint's /v1/models dynamically and falls back to
    a sensible default.
    """
    import urllib.request as _urlreq

    mode = mode.lower()

    if mode == "online":
        try:
            from arenamcp.settings import get_settings
            from arenamcp.backends.proxy import ONLINE_BASE_URL
            license_key = get_settings().get("license_key", "")
            headers = {"User-Agent": "mtgacoach-client/1.0"}
            if license_key:
                headers["Authorization"] = f"Bearer {license_key}"
            req = _urlreq.Request(f"{ONLINE_BASE_URL}/models", headers=headers)
            with _urlreq.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
            models: list[tuple[str, Optional[str]]] = []
            for m in data.get("data", []):
                mid = m["id"]
                models.append((mid, mid))
            if models:
                return models
        except Exception:
            pass
        return [("Default", None)]

    if mode == "local":
        try:
            from arenamcp.settings import get_settings
            local_url = get_settings().get("local_url") or "http://localhost:8000/v1"
        except Exception:
            local_url = "http://localhost:8000/v1"
        # Try OpenAI-compatible /v1/models
        try:
            req = _urlreq.Request(f"{local_url}/models")
            with _urlreq.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
            models = [(m["id"], m["id"]) for m in data.get("data", []) if m.get("id")]
            if models:
                return models
        except Exception:
            pass
        # Try Ollama-specific /api/tags
        if "11434" in local_url:
            try:
                req = _urlreq.Request("http://localhost:11434/api/tags")
                with _urlreq.urlopen(req, timeout=3) as resp:
                    data = json.loads(resp.read())
                models = [(m["name"], m["name"]) for m in data.get("models", []) if m.get("name")]
                if models:
                    return models
            except Exception:
                pass
        return [("llama3.2", "llama3.2")]

    return [("Default", None)]


THINKING_MODEL_PREFERENCE = [
    "deepseek-v4-flash",
    "claude-opus-4-6",
    "claude-sonnet-4-5-20250929",
    "gemini-2.5-pro",
    "gpt-5.3-codex",
]


def pick_thinking_model() -> Optional[str]:
    """Auto-select the best available thinking model.

    In online mode, queries the mtgacoach.com /v1/models endpoint.
    Returns the first match from THINKING_MODEL_PREFERENCE, or None.
    """
    import urllib.request

    try:
        from arenamcp.settings import get_settings
        from arenamcp.backends.proxy import ONLINE_BASE_URL
        s = get_settings()
        license_key = s.get("license_key", "")
        if not license_key or s.get("mode") != "online":
            return None

        req = urllib.request.Request(
            f"{ONLINE_BASE_URL}/models",
            headers={
                "Authorization": f"Bearer {license_key}",
                "User-Agent": "mtgacoach-client/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())

        available_ids = {m["id"] for m in data.get("data", [])}
        for model_id in THINKING_MODEL_PREFERENCE:
            if model_id in available_ids:
                logger.info(f"Thinking model selected: {model_id}")
                return model_id

        logger.info(f"No preferred thinking model found among {len(available_ids)} models")
        return None
    except Exception as e:
        logger.warning(f"Could not pick thinking model: {e}")
        return None


def create_backend(
    mode: str,
    model: Optional[str] = None,
    progress_callback: Optional[Any] = None,
) -> LLMBackend:
    """Factory function to create LLM backends by mode.

    Args:
        mode: "online" or "local" (or "auto" for auto-detection)
        model: Optional model override (uses mode default if not specified)
        progress_callback: Optional callback(status: str) for real-time subtask updates

    Returns:
        Configured LLMBackend instance

    Raises:
        ValueError: If mode is not recognized
    """
    mode = mode.lower()

    if mode == "auto":
        from arenamcp.backend_detect import auto_select_mode
        auto_mode, auto_model = auto_select_mode()
        logger.info(f"Auto-selected mode: {auto_mode} (model={auto_model})")
        return create_backend(
            auto_mode,
            model=model or auto_model,
            progress_callback=progress_callback,
        )

    if mode == "online":
        from arenamcp.settings import get_settings
        license_key = get_settings().get("license_key", "")
        return ProxyBackend.create_online(model=model, license_key=license_key)

    if mode == "local":
        from arenamcp.settings import get_settings
        s = get_settings()
        local_url = s.get("local_url") or "http://localhost:8000/v1"
        local_api_key = s.get("local_api_key") or "vllm"
        local_model = model or s.get("local_model")

        # If no model specified, try to auto-detect from the endpoint
        if not local_model:
            try:
                import urllib.request as _urlreq
                req = _urlreq.Request(f"{local_url}/models")
                with _urlreq.urlopen(req, timeout=3) as resp:
                    data = json.loads(resp.read())
                models_list = [m["id"] for m in data.get("data", []) if m.get("id")]
                if models_list:
                    local_model = models_list[0]
            except Exception:
                pass

        return ProxyBackend.create_local(
            model=local_model,
            url=local_url,
            api_key=local_api_key,
        )

    raise ValueError(
        f"Unknown mode: {mode}. Use 'auto', 'online', or 'local'."
    )


def create_local_fallback(
    model: Optional[str] = None,
    progress_callback: Optional[Any] = None,
) -> "ProxyBackend":
    """Create a local backend as a fallback when online mode fails."""
    from arenamcp.backend_detect import DEFAULT_LOCAL_MODEL
    try:
        from arenamcp.settings import get_settings
        s = get_settings()
        local_url = s.get("local_url") or "http://localhost:8000/v1"
        local_api_key = s.get("local_api_key") or "vllm"
    except Exception:
        local_url = "http://localhost:8000/v1"
        local_api_key = "vllm"
    return ProxyBackend.create_local(
        model=model or DEFAULT_LOCAL_MODEL,
        url=local_url,
        api_key=local_api_key,
    )


# Default MTG coach system prompt
DEFAULT_SYSTEM_PROMPT = """You are an expert MTG coach providing real-time advice during Arena games.

Keep responses concise (2-3 sentences max) since they'll be spoken aloud.
Focus ONLY on the final strategic recommendation.
Do NOT show your thinking process, "reasoning", or "corrections".
Do NOT use internal monologue tags like [plan] or [thought].
Do NOT second-guess yourself in the text (e.g., "Wait, I need to check...").
Be authoritative and decisive. Start your response immediately with the command.

CRITICAL GAME RULES:
- "=== NEW GAME ===" means a brand new match started. FORGET all previous board state, cards, and strategies from prior games. Only reference what is shown in the current game state.
- The "Legal:" line lists ALL valid actions. ONLY suggest actions listed there.
- NEVER suggest actions not in the Legal: line. If you want to cast a spell, it MUST appear as "Cast [card name]" in Legal:.
- Do NOT hallucinate actions like "flash in" or "hold up" unless they are explicitly legal actions.
- Creatures tagged [SS] have SUMMONING SICKNESS — they CANNOT attack or use tap abilities this turn.
- Creatures tagged [LOCKED] are enchanted by an opponent aura that PREVENTS UNTAPPING. They are permanently tapped and CANNOT attack, block, or use tap abilities until the aura is removed. Do NOT suggest using LOCKED creatures. The ">>" lines below a creature show what auras are attached to it.
- Do NOT suggest attacking with [SS] or [LOCKED] creatures. Check the "Declare Attackers:" list for legal attackers.
- DEFAULT: You can only play ONE LAND per turn unless a card grants additional land drops.
- Check the LAND DROP status to see if a land can still be played this turn.
- LAND DROP EVALUATION: If a land drop is AVAILABLE and you have lands in hand, consider playing a land to develop your mana. You may play the land first, or hold it/play it post-combat for strategic reasons (e.g., hiding information or holding up mana for interaction).
- THEN LINE: If a "THEN:" line appears after Legal, it shows what spells become castable after playing each land. You may recommend "Play [land], then cast [spell]", or hold up mana if holding up interaction is strategically superior.
- Cards marked [INSTANT] or [I] can be cast anytime you have priority
- Cards marked [SORCERY SPEED] or [S] can ONLY be cast during YOUR Main phase with empty stack
- During opponent's turn or combat: ONLY suggest instants/flash cards or activated abilities
- If it's not your Main phase, do NOT suggest casting creatures or sorceries (unless they have flash)

CRITICAL MANA RULES:
- Cards tagged [OK] or [CAN CAST] are castable RIGHT NOW with available mana - no additional mana needed!
- Cards WITHOUT [OK] CANNOT be cast right now — NEVER recommend casting them!
- Cards tagged [NEED:{G}] need GREEN mana specifically — adding non-green sources won't help!
- Cards tagged [NEED:{R}{R}] need TWO RED mana — check which lands produce that color.
- Cards tagged [NEED:3] need 3 more TOTAL mana from any source.
- Cards tagged [NEED X] CANNOT be cast - do NOT suggest or mention them! Focus only on playable options.
- Do NOT perform your own mana calculations - trust the tags completely.
- The "Mana: X" line shows ONLY mana from UNTAPPED LANDS ON THE BATTLEFIELD. Lands in hand are NOT mana.
- NEVER count lands in hand as available mana. A Plains in hand produces 0 mana until played.
- If a card shows [OK], you already have enough mana. Don't suggest paying extra life/resources for more mana.
- RESOURCE EFFICIENCY: Don't waste life or mana. If you can cast a spell with current mana, don't pay extra.
- NEVER advise "developing mana toward" a spell that is already [OK] — if it's castable now, say to cast it NOW (after the free land drop if available).

STRATEGIC PRIORITIES:
- When the opponent's board is wider or grew by multiple creatures this turn, prioritize interaction (removal, profitable blocks, combat tricks) over advancing your own plan.
- At low life (below ~15), block with large or indestructible creatures to cut incoming damage — an indestructible blocker loses nothing by blocking.
- The "sources:" display shows what mana EACH source can produce (e.g., "{U/G}" means one source producing U OR G, not both).
- If ALL cards show [NEED X], say "pass priority" - you cannot cast anything.

CRITICAL MATH RULES:
- When suggesting removal, check the creature's TOUGHNESS (second number, e.g., 4/5 has 5 toughness).
- -2/-2 or 2 damage ONLY kills toughness 2 or less (unless damaged).
- Do NOT suggest removal that won't kill the target unless it enables a profitable attack.
- Cards tagged [NO TARGETS] have NO VALID TARGETS right now. Do NOT cast them — it wastes the card for no effect. Even if the card appears in the Legal: line, casting it without targets is a mistake.
- Cards tagged [OK,X=0] are X-cost spells where you can only pay X=0. This means the X effect does NOTHING (0 targets, 0 damage, 0 counters). Do NOT suggest casting these unless the non-X part of the spell is still valuable on its own. Usually it's better to wait until you have more mana so X > 0.

STRATEGIC VALUE — BEFORE suggesting any spell, evaluate whether it advances your game plan:
- Is the RESULT worth the mana/life/card cost? Removing a 0/4 wall with premium removal is usually a waste.
- Could this card be more impactful later? Hold removal for real threats, don't waste it on marginal targets.
- Does casting this spell advance your win condition or just react? Proactive plays that build your board or set up combos are usually better than reactive plays against non-threatening permanents.
- If a spell has a downside (lose life, sacrifice, discard), the payoff must be worth it. "Can cast" does not mean "should cast."
- Do NOT cast auras, buffs, or combat tricks on opponent's creatures unless it outright kills them. Buffing an opponent's creature just to trigger a "draw a card" effect is a terrible trade.
- PROTECTIVE / LIFE-PAYMENT ABILITIES: Do NOT pay life (or any resource) to give a creature indestructible, hexproof, protection, or a temporary defensive buff unless there is a CONCRETE threat to that creature RIGHT NOW — it is blocked by something that would kill it, it is the target of removal/burn on the stack, or it must survive incoming combat/damage this step. An unblocked attacker or a creature facing no removal does NOT need protection. Paying 4 life for indestructible "just in case" is pure life loss for zero benefit. "Can activate" does not mean "should activate" — if nothing threatens the creature, PASS instead.

STRATEGIC EVALUATION & DECISIONS:
- LETHAL CHECK: Before anything else, count your total attack power vs opponent life and blockers.
  If you can deal lethal, go aggressive — remove a blocker or just attack. Don't play defensively!
- ONLY claim "lethal" if the combat summary line shows "Atk: ... vs LETHAL".
- TRADE CHECK: Read the "If X blocks Y:" lines below the Atk: summary. Lines marked "BAD" mean the attacker dies for free or bounces off. Do NOT attack into a BAD trade unless it enables lethal or a critical strategy. If every possible block is BAD, don't attack with that creature.
- WORST-CASE BLOCKING: The opponent WILL choose the block that's best for THEM. If ANY "If X blocks Y:" line shows BAD for your attacker, assume the opponent will make that block. Don't suggest attacking because one blocker gives a GOOD trade when another blocker kills your creature — the opponent won't cooperate with your plan.
- ATTACK EVALUATION: Evaluate attacks dynamically. Attack with profitable attackers, hold back blockers if needed to survive crackback, and attack with your full team when favorable or for lethal.
- MAIN PHASE EVALUATION: Evaluate whether to advance your board state with [OK] spells/land drops OR to pass priority to hold up mana for instant-speed interaction, activated abilities, or combat tricks on opponent's turn. Choose whichever path gives the higher strategic advantage.
- PASS PRIORITY is correct when: (a) it's NOT your turn and you have no instant-speed plays, (b) holding up mana/interaction for opponent's turn is strategically superior to tapping out, (c) you have NO [OK] cards or abilities, or (d) end-of-turn/upkeep with no triggers or responses to make.
- CRACKBACK CHECK: Before attacking, count opponent's total power on board vs YOUR life total.
  If opponent can kill you on their next attack and you need creatures to block, do NOT attack with them.
  Holding back blockers to survive is more important than dealing a few damage.
  The "Crackback:" line already accounts for your blockers — trust its damage-through number.
- BLOCKING MATH: The "Best blocks → X dmg" line shows MINIMUM damage after optimal blocking. Trust this number, not the raw attacker power.
  Use the "Best blocks" life total for survival math, not the "No blocks" total.
  Do NOT re-derive blocking math yourself — the computed numbers already account for flying, trample, and blocker assignment.
- DOUBLE-BLOCK RULE: Assigning two blockers to one attacker wastes a creature UNLESS (a) the attacker has trample, (b) killing this specific attacker is critical and a single blocker can't do it, or (c) there is only one attacker and you'd rather trade 2-for-1 than take the damage. Without trample, the attacker only deals its power in damage regardless of how many blockers it faces — a chump block with one creature achieves the same damage prevention as a double-block, while keeping your second creature alive. Default to spreading blockers across multiple attackers or chump-blocking, not stacking them on one.
- COMBAT SOLVER: When you see a "Computed optimal blocks: ..." or "Computed optimal attack: ..." line in the game state, that is a deterministic enumeration of every legal block/attack assignment scored by life preserved + material traded. Follow it unless you have a specific reason the solver couldn't see (e.g. a combat trick in hand that swings the math, a removal spell on the stack about to kill the attacker, or a synergy that makes one creature more valuable than its P+T). Do NOT pick a different assignment without naming the specific reason.
- IMPENDING: Cards flagged [IMPENDING] are enchantments with time counters — they are NOT creatures yet and cannot attack, block, or be counted as combat threats. Ignore them in damage/lethal math until the counters are gone.

SECRETS OF STRIXHAVEN (SOS) MECHANICS — the five colleges each have their own theme; instants/sorceries are the mechanical backbone of the set:
- PREPARE (keyword on creatures): The creature enters with an exiled copy of its "prepare spell." While the creature is on the battlefield AND prepared, you may cast that copy by paying its mana cost — doing so unprepares the creature. Think "Adventure that lives on the battlefield." The spell copy is only castable when the creature is prepared.
- INCREMENT (keyword, Quandrix U/G): Whenever you cast a spell, if the mana spent to cast it is greater than this creature's power OR its toughness, put a +1/+1 counter on it. Rewards curving into progressively bigger spells; a 1/1 with increment can snowball fast.
- PARADIGM (keyword on instants/sorceries): After the spell resolves, exile it. At the beginning of each of your first main phases for the REST OF THE GAME, you may cast a free copy from exile. These are the highest-impact cards in the set — resolving one usually wins the game if it's not answered immediately.
- INFUSION (Witherbloom B/G ability word): The ability triggers or gains an enhanced mode if you GAINED ANY LIFE this turn. Amount doesn't matter — 1 life is enough. Pair with any lifegain trigger.
- OPUS (Prismari U/R ability word): Triggers whenever you cast an instant or sorcery; the effect is BIGGER if you spent 5+ mana casting that spell. Reward for going tall on spells.
- REPARTEE (Silverquill W/B ability word): Triggers whenever you cast an instant or sorcery that TARGETS A CREATURE (including your own creatures). Auras, pump spells, targeted removal, and targeted buffs all count.
- FLASHBACK (returning, Lorehold R/W): Cast an instant/sorcery from your graveyard for its flashback cost, then exile it. Treats graveyard instants/sorceries as a second cast.
- CONVERGE (returning): The effect scales with the number of DIFFERENT colors of mana spent to cast the spell (max 5). Paying {1}{W}{U}{B}{R}{G} on a converge spell gives the full effect.

- Bounce/removal spells can target OPPONENT creatures too. Bouncing a blocker for lethal > saving your creature.
- When opponent has a removal spell on the stack, weigh "save my creature" vs "ignore it and go for the kill."
- Creatures have power/toughness (e.g. 5/5). Don't call creatures "planeswalkers."
- ORACLE TEXT: Only reference card abilities that are explicitly shown in the game state. Do NOT guess or infer oracle text from memory — if the text isn't shown, say so.

Analyze: phase (critical for timing!), board state, life totals, cards in hand, mana available.
Output directly as the coach. No preamble, no meta-commentary.
Do NOT mention cards you can't cast yet due to mana — focus only on playable options. The player can see their hand."""

CONCISE_SYSTEM_PROMPT = """You are an expert MTG coach giving real-time spoken advice.
Give ONE action for the CURRENT phase only. You will be re-consulted as the turn progresses.

PHASE GUIDE:
- Main phase: Suggest ONE play (land OR spell). You'll advise again after it resolves.
- Combat/DeclareAttack: Say who to attack with (or "don't attack").
- Combat/DeclareBlock: Name each assignment — "Block [attacker] with [blocker]" (or "don't block, take the damage"). Never say "block with X" without naming which attacker X blocks.
- Opponent's turn: React to what's happening (instants/abilities only).
- Stack: Say whether to respond or let it resolve.

After your ONE action, you may add a brief reason or hint at the next step.

Examples:
"Play Mountain. Sets up Geological Appraiser next turn."
"Cast Etali's Favor on Laelia — triggers discover for the cascade chain."
"Attack with Laelia, the Blade Reforged. She exiles and grows."
"Don't block. Take the 3 damage, you're at 20."
"Let it resolve. Nothing worth countering."
"Pass priority."

STRATEGY:
- LETHAL CHECK: Before anything else, count your total attack power vs opponent life and blockers.
  If you can deal lethal, go aggressive — remove a blocker or just attack. Don't play defensively!
- ONLY claim "lethal" if the combat summary line shows "Atk: ... vs LETHAL".
- TRADE CHECK: Read "If X blocks Y:" lines. "BAD" = attacker dies for free. Don't attack into BAD trades unless it enables lethal.
- WORST-CASE BLOCKING: The opponent chooses which creature blocks. If ANY blocker gives a BAD result for your attacker, assume that's what happens — don't attack hoping the opponent picks the favorable block.
- ATTACK EVALUATION: Evaluate attacks dynamically. Attack with profitable attackers, hold back blockers if needed to survive crackback, and attack with your full team when favorable or for lethal.
- CRACKBACK CHECK: Before attacking, count opponent's total power vs YOUR life. If they can kill you next turn and you need blockers to survive, do NOT attack with those creatures. The "Crackback:" line already accounts for your blockers — trust its damage-through number.
- BLOCKING MATH: The "Best blocks → X dmg" line shows MINIMUM damage after optimal blocking. Use this number for survival math, not the "No blocks" total. Do NOT re-derive blocking math yourself.
- DOUBLE-BLOCK RULE: A non-trample attacker deals its power in damage regardless of how many blockers face it. Double-blocking only makes sense when (a) attacker has trample, (b) you MUST kill this specific attacker and a single blocker can't, or (c) a 2-for-1 trade is explicitly worth it. Otherwise chump-block with ONE creature and save the other — the damage prevented is the same.
- COMBAT SOLVER: "Computed optimal blocks:" / "Computed optimal attack:" lines are deterministic enumerations of every legal assignment scored by life + material. Follow them unless you have a specific reason they miss (combat trick in hand, removal on the stack, synergy that makes one creature worth more than P+T).
- IMPENDING: Cards flagged [IMPENDING] are NOT creatures yet — ignore them in combat/lethal math.
- SOS MECHANICS: PREPARE = creature has an exile-copy spell castable only while prepared; casting the copy unprepares it. INCREMENT = +1/+1 counter whenever you cast a spell costlier than this creature's power or toughness. PARADIGM = after resolving, free copy at every one of your main phases FOREVER — top-priority threats. INFUSION = triggers if you gained any life this turn. OPUS = instant/sorcery trigger, bigger at 5+ mana spent. REPARTEE = instant/sorcery targeting a creature. FLASHBACK = cast from graveyard, then exile. CONVERGE = scales with distinct colors paid.
- Bounce/removal spells can target OPPONENT creatures too. Bouncing a blocker for lethal > saving your creature.
- When opponent has a removal spell on the stack, weigh "save my creature" vs "ignore it and go for the kill."
- ORACLE TEXT: Only reference abilities explicitly shown. Do NOT guess card text from memory.

RULES:
- The "Legal:" line lists ALL valid actions. ONLY suggest actions listed there. No exceptions!
- NEVER suggest actions not in Legal:. If you want to "flash in" a creature, it MUST show "Cast [creature]" in Legal:.
- Creatures tagged [SS] have SUMMONING SICKNESS — they CANNOT attack. Check "Declare Attackers:" for legal attackers.
- Cards tagged [OK] are castable NOW with current mana - no additional mana needed! Don't waste life for more mana.
- Cards WITHOUT [OK] CANNOT be cast right now — NEVER recommend casting them! Only suggest [OK] cards.
- Cards tagged [NEED X] CANNOT be cast - do NOT suggest or mention them! Focus only on playable options.
- Cards tagged [OK,X=0] have X=0 — the X effect does nothing. Don't cast unless the non-X part alone is valuable.
- Cards tagged [NO TARGETS] have no valid targets — do NOT cast them.
- RESOURCE EFFICIENCY: If a card shows [OK], you already have enough. Don't pay extra life/mana unnecessarily.
- STRATEGIC VALUE: "Can cast" ≠ "should cast." Hold removal for real threats. Proactive plays that advance your win condition beat reactive plays against weak targets. Consider if the card would be better saved for later.
- PROTECTIVE ABILITIES: Don't pay life for indestructible/hexproof/protection unless a concrete threat exists NOW (blocked by a killer, removal on the stack, lethal damage incoming). An unblocked attacker needs no protection — paying life "just in case" is wasted life. PASS instead.
- LAND DROP EVALUATION: If LAND status shows 'AVAILABLE', consider playing a land to develop mana, or hold it if strategic.
- THEN LINE: If "THEN:" appears, you may recommend "Play [land], then cast [spell]" or hold up interaction.
- Use exact FULL card names from the game state. Never abbreviate.
- Only suggest lands shown in HAND. If no land in hand, don't suggest playing one.
- Say "pass priority" not just "pass" to avoid sounding like a card name.
- Creatures have power/toughness (e.g. 5/5). Don't call creatures "planeswalkers."
- [FLYING] attackers can only be blocked by [FLYING] or [REACH]. But flyers CAN block ground creatures — flying restricts what blocks them, not what they block.
- This is spoken aloud — keep it natural and under 30 words.
"""

# PHASE 2: Decision-specific prompt guidance
DECISION_PROMPTS = {
    "mulligan": """
MULLIGAN DECISION: Evaluate this hand and decide KEEP or MULLIGAN.
Consider: land count (2-3 ideal), mana curve (can you cast spells turns 1-3?), synergy with deck plan.
- KEEP if: Playable lands + early plays that advance the game plan
- MULLIGAN if: 0-1 lands, 5+ lands, no plays before turn 3, completely off-plan
Answer: "KEEP" or "MULLIGAN" with a one-sentence reason.
""",
    "mulligan_bottom": """
MULLIGAN BOTTOM: Choose which card(s) to put on the bottom of your library.
You must put cards on bottom to go down to your mulligan hand size.
Priority (put on BOTTOM first):
1. Highest-cost cards you can't cast in the first 3 turns
2. Duplicate effects when you already have one in hand
3. Off-color or uncastable spells
4. KEEP: Lands (you need mana!), cheap creatures, removal, key combo pieces
Name the specific card(s) to bottom with a brief reason.
""",
    "scry": """
SCRY DECISION: Decide whether to keep the card on top or put it on bottom.
- KEEP if: It's a land and you need mana, OR it's a threat you can cast soon
- BOTTOM if: It's redundant/dead right now, or you need to dig for answers
Evaluate based on: current mana, hand quality, board state urgency.
Answer: "Keep" or "Bottom" with brief reason (1 sentence).
""",
    "surveil": """
SURVEIL DECISION: Decide whether to keep cards on top or put in graveyard.
- KEEP if: You want to draw them next (lands if ramping, threats if you have mana)
- GRAVEYARD if: Enables graveyard synergies OR you want to dig deeper
Answer: "Keep [card names]" or "Graveyard [card names]" with brief reason.
""",
    "discard": """
DISCARD DECISION: Choose which card(s) to discard.
Priority (discard FIRST):
1. Excess lands if you have 4+ in hand
2. Highest CMC card you can't cast this turn or next
3. Redundant copies of cards already in play
4. KEEP: Removal, counters, win conditions
Answer: "Discard [card name]" with brief reason (1 sentence).
""",
    "declare_blockers": """
DECLARE BLOCKERS: Decide the exact block assignments.
- For EACH blocker you use, name the attacker it blocks: "Block [attacker] with [blocker]".
- Or say "No blocks" / "Don't block, take the damage" with the life math.
- Follow the "Computed optimal blocks" line unless a combat trick or removal changes the math.
Answer with explicit attacker->blocker assignments only — NEVER "block with X" without naming the attacker X blocks.
""",
    "target_selection": """
TARGET SELECTION: Choose the best target for this spell/ability.
Evaluate each potential target:
- Which target solves the biggest immediate threat?
- Which target advances your win condition?
- Consider opponent's likely responses (do they have protection?)
Answer: "Target [card name]" with brief tactical reason.
""",
    "modal_choice": """
MODAL SPELL: Choose which mode to use.
Compare each mode's impact:
- Which mode answers the most pressing threat?
- Which mode creates the best advantage?
- Consider mana efficiency and follow-up plays
Answer: "Choose mode [X]" with brief reason (1 sentence).
""",
    "sacrifice": """
SACRIFICE DECISION: Choose which permanent(s) to sacrifice.
- Sacrifice the LEAST valuable permanent for the current board state
- Keep: key synergy pieces, win conditions, blockers you need
- Sacrifice: redundant creatures, tokens, low-impact permanents
Answer: "Sacrifice [card name]" with brief reason (1 sentence).
""",
    "exile": """
EXILE DECISION: Choose which card(s) to exile.
- Consider: exiled cards are much harder to recover than destroyed/discarded ones
- Exile: least impactful or already-used cards
- Keep: anything with graveyard synergy or future utility
Answer: "Exile [card name]" with brief reason (1 sentence).
""",
    "destroy": """
DESTROY DECISION: Choose which permanent(s) to destroy.
- Target the biggest threat or most impactful permanent
- Consider: indestructible, regeneration, death triggers
Answer: "Destroy [card name]" with brief reason (1 sentence).
""",
    "return": """
RETURN DECISION: Choose which permanent(s) to return.
- Return: least impactful or cheapest to replay
- Keep: expensive/critical permanents on the battlefield
Answer: "Return [card name]" with brief reason (1 sentence).
""",
    "choose_creature": """
CHOOSE CREATURE: Select a creature.
- Evaluate board impact: which creature matters most right now?
- Consider power/toughness, abilities, synergies
Answer: "Choose [card name]" with brief reason (1 sentence).
""",
    "choose_permanent": """
CHOOSE PERMANENT: Select a permanent.
- Evaluate which permanent has the most board impact
- Consider card types, abilities, and current game state
Answer: "Choose [card name]" with brief reason (1 sentence).
""",
    "choose": """
CHOOSE: Make a selection from the available options.
- Evaluate which option best advances your game plan
- Consider immediate impact and future implications
Answer: "Choose [option]" with brief reason (1 sentence).
""",
}

WIN_PLAN_PROMPT = """You are a Magic: The Gathering strategic planner. Given the board state, hand, mana, and library summary, outline a concrete plan to win in {n} turns.

Be EXTREMELY concise — the plan must be speakable in under 20 seconds (~50 words max).
Use shorthand: "T1:" for Turn 1, card names only (no mana costs), "swing all" for full attack.
Skip land drops and obvious plays. Focus ONLY on the key sequencing that wins.

CRITICAL: Only reference cards shown in the provided game state or library summary.

Start your response with exactly one of:
  VIABLE: YES — if this plan can realistically win in {n} turns using mostly cards in hand/on board
  VIABLE: NO — if it requires specific draws or opponent misplays

Then give the plan in 2-4 short lines max."""

DECK_ANALYSIS_PROMPT = """Analyze this Magic: The Gathering deck list. Provide a strategic guide that will be injected into every turn's coaching context.

1. ARCHETYPE: One-line (e.g. "Gruul Counters Aggro", "Dimir Control")
2. WIN CONDITION: How does this deck close games?
3. KEY COMBOS & SYNERGIES: Identify 2-4 powerful card interactions. Name the specific cards and explain the payoff. Example: "Kodama of the West Tree + any modified creature = free land ramp + trample."
4. KEY CARDS: 3-5 most important cards. For each, note when to play it and what it enables.
5. PLAY PATTERN: Ideal sequencing by game phase (early/mid/late). What to prioritize on curve, when to hold mana open, when to be aggressive vs defensive.
6. WATCH OUT: Key weaknesses, what removal to play around, when you're vulnerable.

Be specific to THIS deck's cards. Name card names, not generic advice. Keep under 600 characters total."""

DECK_STRATEGY_BRIEF_PROMPT = """You are an expert MTG coach. Given a deck list, provide a brief spoken strategy summary in 3-5 sentences.

Cover: the deck's archetype, primary win condition, and the 1-2 most important sequencing tips.

Be specific — name actual cards from the list. Keep it conversational and under 200 characters. This will be read aloud via TTS."""

POST_MATCH_ANALYSIS_PROMPT = """You are an expert Magic: The Gathering coach providing a post-match debrief. You are also reviewing your OWN coaching performance — the advice log shows what YOU told the player to do during the match.

Given a chronological log of coaching advice given during the match, the match result, game event data, and optionally a REPLAY DATA section with authoritative GRE decision history, provide a strategic analysis:

1. RESULT: One sentence on the match outcome and how it was decided.
2. KEY TURNING POINTS: 2-3 moments that most influenced the outcome (reference specific turns and cards).
3. WHAT WENT WELL: 1-2 things the player/autopilot did correctly.
4. COACHING ERRORS: Identify moments where YOUR advice was wrong, illegal, or suboptimal. For each:
   - What you advised and why it was wrong
   - What the correct play was
   - Root cause (e.g. "didn't account for mana cost", "ignored opponent's open mana", "recommended a card not in hand")
5. AUTOPILOT ERRORS: If REPLAY DATA is present, identify where the autopilot executed the wrong action (e.g. submitted wrong card, failed to pay costs, got stuck in a loop). Note the turn and what actually happened vs. what was intended.
6. OPPONENT STRATEGY: Brief assessment of the opponent's game plan and how it could be countered next time.
7. COACHING IMPROVEMENTS: 1-3 concrete, actionable improvements to the coaching AI. These should be specific rules or heuristics, not vague suggestions. Examples:
   - "Always verify mana availability before recommending a cast — check both total mana and color requirements"
   - "When multiple cast actions share the same type, verify card identity before submitting"
   - "Don't recommend attacking with the only blocker when opponent has lethal on board"

Keep the full analysis under 500 words. Be specific — reference actual cards and turns from the match log.
Do NOT be generic. Use the advice history to identify where the player followed or ignored coaching advice.
CRITICAL: ONLY reference card names that appear in the provided match log. Do NOT substitute, guess, or invent card names from your general MTG knowledge. If you cannot find a card name in the log, describe it by its effect instead.

At the very end, on its own line, add a short TTS summary prefixed with "SPOKEN:" (2-3 sentences, under 40 words). This will be read aloud."""


SIDEBOARD_RECOMMENDATION_PROMPT = """You are a Magic: The Gathering competitive sideboarding expert.
Analyze the user's maindeck, 15-card sideboard, and the opponent's cards revealed in previous game(s) of this Best-of-Three (Bo3) match.

Provide clear, actionable sideboarding recommendations:
1. **Cards to Swap Out (Maindeck -> Sideboard)**: Identify 1 to 5 cards from the maindeck that are slow, inefficient, or ineffective in this matchup.
2. **Cards to Swap In (Sideboard -> Maindeck)**: Select specific cards from the sideboard that directly answer the opponent's strategy, improve efficiency, or counter key threats.
3. **Strategic Rationale**: Provide 2-3 concise sentences explaining why these swaps give the player a strategic advantage in Game 2 or 3.
4. **Card Count Balance**: Ensure the exact number of cards swapped in equals the number swapped out (e.g. +3 IN, -3 OUT) so maindeck size remains valid.

Format the output clearly using these exact section headers:
**IN**:
- [quantity]x [Card Name] — [reason]
**OUT**:
- [quantity]x [Card Name] — [reason]
**PLAN**:
[Strategic rationale]
"""


# Words that tend to be overused by LLMs in coaching contexts
OVERUSE_CANDIDATES = {
    "consider",
    "considering",
    "important",
    "crucial",
    "critical",
    "definitely",
    "absolutely",
    "certainly",
    "essentially",
    "basically",
    "potentially",
    "priority",
    "prioritize",
    "focus",
    "key",
}

# Threshold for blacklisting (uses in window)
OVERUSE_THRESHOLD = 3
OVERUSE_WINDOW_SECONDS = 120


class WordUsageTracker:
    """Tracks word usage over time to detect overused words."""

    def __init__(
        self,
        threshold: int = OVERUSE_THRESHOLD,
        window_seconds: float = OVERUSE_WINDOW_SECONDS,
    ):
        self._threshold = threshold
        self._window = window_seconds
        self._usage: list[tuple[float, str]] = []  # (timestamp, word)

    def record(self, text: str, exclude_words: Optional[set[str]] = None) -> None:
        """Record words from a response.

        Args:
            text: The response text to analyze
            exclude_words: Set of words to ignore (e.g., card names)
        """
        import time
        import re

        now = time.time()

        exclude = exclude_words or set()

        # Extract words, lowercase
        words = re.findall(r"\b[a-z]+\b", text.lower())

        # Only track candidate words that aren't excluded
        for word in words:
            if word in OVERUSE_CANDIDATES and word not in exclude:
                self._usage.append((now, word))

        # Prune old entries
        cutoff = now - self._window
        self._usage = [(t, w) for t, w in self._usage if t > cutoff]

    def get_blacklisted(self, exclude_words: Optional[set[str]] = None) -> list[str]:
        """Get words that have been overused in the current window.

        Args:
            exclude_words: Set of words to never blacklist (e.g., card names)
        """
        import time
        from collections import Counter

        exclude = exclude_words or set()
        now = time.time()
        cutoff = now - self._window

        # Count words in window
        recent_words = [w for t, w in self._usage if t > cutoff]
        counts = Counter(recent_words)

        # Return words over threshold, excluding protected words
        return [
            word
            for word, count in counts.items()
            if count >= self._threshold and word not in exclude
        ]


class CoachEngine:
    """Engine for getting MTG coaching advice from an LLM backend."""

    def __init__(
        self, backend: Optional[LLMBackend] = None, system_prompt: Optional[str] = None
    ):
        """Initialize the coach engine.

        Args:
            backend: LLM backend to use (default: ProxyBackend)
            system_prompt: Custom system prompt (default: MTG coach persona)
        """
        self._backend = backend if backend is not None else ProxyBackend()
        self._system_prompt = (
            system_prompt if system_prompt is not None else DEFAULT_SYSTEM_PROMPT
        )
        self._word_tracker = WordUsageTracker()
        self._deck_strategy: Optional[str] = None
        self._deck_strategy_pending = False
        self._rules_db: Optional["RulesDB"] = None
        # Persistent, adaptive strategic plan. Lazily constructed (game_plan.py
        # imports CoachEngine, so a module-level import here would cycle).
        self._game_plan_mgr = None

    def _ensure_game_plan_mgr(self):
        """Lazily construct and return the GamePlanManager (or None on failure)."""
        if self._game_plan_mgr is None:
            try:
                from arenamcp.game_plan import GamePlanManager

                self._game_plan_mgr = GamePlanManager(self._backend)
            except Exception as e:
                logger.debug(f"GamePlanManager unavailable: {e}")
                return None
        return self._game_plan_mgr

    def get_backend_info(self) -> dict[str, Any]:
        """Return diagnostic info about the current LLM backend.

        Returns:
            Dict with backend_type, model, status, and other details.
        """
        be = self._backend
        info: dict[str, Any] = {
            "backend_type": type(be).__name__,
            "model": getattr(be, "model", None) or "(default)",
        }

        if isinstance(be, ProxyBackend):
            from arenamcp.backends.proxy import ONLINE_BASE_URL
            base_url = getattr(be, "_base_url", "")
            if base_url and ONLINE_BASE_URL in base_url:
                info["backend_name"] = "online"
            else:
                info["backend_name"] = "local"
            info["base_url"] = base_url
        else:
            info["backend_name"] = "unknown"

        return info

    def _zone_cards(self, game_state: dict[str, Any], zone_name: str) -> list[dict[str, Any]]:
        zones = game_state.get("zones")
        if isinstance(zones, dict):
            zone_value = zones.get(zone_name)
            if isinstance(zone_value, list):
                return zone_value

        zone_value = game_state.get(zone_name)
        return zone_value if isinstance(zone_value, list) else []

    def _get_local_seat_id(self, game_state: dict[str, Any]) -> Optional[int]:
        for player in game_state.get("players", []):
            if player.get("is_local"):
                return player.get("seat_id")
        return None

    def _parse_mana_value(self, mana_cost: str) -> int:
        import re

        cmc = 0
        for symbol in re.findall(r"\{([^}]+)\}", mana_cost or ""):
            if symbol.isdigit():
                cmc += int(symbol)
            elif "/" in symbol:
                cmc += 1
            elif symbol.upper() in {"W", "U", "B", "R", "G", "C", "X"}:
                cmc += 1 if symbol.upper() != "X" else 0
        return cmc

    def _available_mana_now(self, game_state: dict[str, Any]) -> int:
        local_seat = self._get_local_seat_id(game_state)
        if local_seat is None:
            return 0

        available = 0
        for card in self._zone_cards(game_state, "battlefield"):
            controller = card.get("controller_seat_id") or card.get("owner_seat_id")
            if controller != local_seat:
                continue
            if "land" not in str(card.get("type_line", "")).lower():
                continue
            if card.get("is_tapped"):
                continue
            available += 1
        return available

    def _summarize_threat_card(self, threat: dict[str, Any]) -> str:
        card = threat.get("card") if isinstance(threat.get("card"), dict) else threat
        if not isinstance(card, dict):
            return ""

        parts: list[str] = []
        type_line = str(card.get("type_line", "") or "").strip()
        if type_line:
            parts.append(type_line)

        power = card.get("power")
        toughness = card.get("toughness")
        if power not in (None, "") and toughness not in (None, ""):
            parts.append(f"{power}/{toughness}")

        loyalty = card.get("counters", {}).get("Loyalty") if isinstance(card.get("counters"), dict) else None
        if loyalty not in (None, ""):
            parts.append(f"Loyalty {loyalty}")

        oracle_text = str(card.get("oracle_text", "") or "").replace("\n", " ").strip()
        if oracle_text:
            parts.append(oracle_text[:220] + ("..." if len(oracle_text) > 220 else ""))

        return " | ".join(parts)

    def _identify_threat_answers(
        self,
        game_state: dict[str, Any],
        threat: dict[str, Any],
    ) -> list[str]:
        threat_card = threat.get("card") if isinstance(threat.get("card"), dict) else threat
        threat_type = str(threat_card.get("type_line", "") or "").lower()
        threat_name = str(threat.get("name", threat_card.get("name", "that threat")) or "that threat")
        available_mana = self._available_mana_now(game_state)

        answers: list[str] = []
        for card in self._zone_cards(game_state, "hand"):
            name = str(card.get("name", "") or "").strip()
            if not name:
                continue

            mana_cost = str(card.get("mana_cost", "") or "")
            if mana_cost and self._parse_mana_value(mana_cost) > available_mana:
                continue

            oracle = str(card.get("oracle_text", "") or "").lower()
            if not oracle:
                continue

            reason = ""
            if "creature" in threat_type:
                if (
                    "destroy target creature" in oracle
                    or "destroy target nonartifact creature" in oracle
                    or "destroy target attacking creature" in oracle
                    or "exile target creature" in oracle
                ):
                    reason = "clean creature removal"
                elif "target creature gets -" in oracle or "deals" in oracle and "target creature" in oracle:
                    reason = "can kill or shrink it"
                elif "fight target creature" in oracle:
                    reason = "can fight it off the board"
            elif "planeswalker" in threat_type:
                if "target planeswalker" in oracle or "any target" in oracle or "target permanent" in oracle:
                    reason = "can answer the planeswalker directly"
            elif "artifact" in threat_type or "enchantment" in threat_type:
                if "target artifact" in oracle or "target enchantment" in oracle or "target nonland permanent" in oracle or "target permanent" in oracle:
                    reason = "can remove that permanent type"

            if not reason and ("target permanent" in oracle or "target nonland permanent" in oracle):
                reason = f"can answer {threat_name}"

            if reason:
                answers.append(f"{name} ({reason})")

        return answers[:4]

    def _threat_pressure_summary(self, game_state: dict[str, Any], threat: dict[str, Any]) -> str:
        local_seat = self._get_local_seat_id(game_state)
        if local_seat is None:
            return ""

        attackers: list[str] = []
        total_power = 0
        for card in self._zone_cards(game_state, "battlefield"):
            controller = card.get("controller_seat_id") or card.get("owner_seat_id")
            if controller != local_seat:
                continue
            if card.get("is_tapped"):
                continue
            if "creature" not in str(card.get("type_line", "")).lower():
                continue
            name = str(card.get("name", "") or "?")
            power = card.get("power")
            toughness = card.get("toughness")
            if power not in (None, ""):
                try:
                    total_power += int(power)
                except (TypeError, ValueError):
                    pass
            attackers.append(f"{name} ({power}/{toughness})" if power not in (None, "") and toughness not in (None, "") else name)

        if not attackers:
            return "No untapped creatures available to pressure it right now."
        return f"Untapped pressure available: {', '.join(attackers[:4])} | total power {total_power}."

    def _build_threat_trigger_description(
        self,
        game_state: dict[str, Any],
        threat: dict[str, Any],
        *,
        is_verbose: bool,
    ) -> str:
        name = str(threat.get("name", "that threat") or "that threat")
        warning = str(threat.get("warning", "") or "").strip()
        summary = self._summarize_threat_card(threat)
        answers = self._identify_threat_answers(game_state, threat)
        pressure = self._threat_pressure_summary(game_state, threat)

        lines = [
            f"THREAT ALERT: {name}",
            "Requirements:",
            f"- Name {name} explicitly in the first sentence.",
            "- Explain why it matters in this exact board state, not in general.",
            "- Give the best concrete line using our current hand, battlefield, and deck plan.",
            "- If removal is available now, say which card answers it.",
            "- If removal is not available, give the best containment plan for this turn.",
            "- Do not give generic lines like 'consider attacking it' without naming attackers or the actual plan.",
        ]
        if warning:
            lines.append(f"Threat note: {warning}")
        if summary:
            lines.append(f"Threat details: {summary}")
        if answers:
            lines.append("Available answers now: " + ", ".join(answers))
        else:
            lines.append("Available answers now: none obvious in hand.")
        if pressure:
            lines.append(pressure)

        if is_verbose:
            lines.append("Explain the trade-off if the best line is to race, block, or hold interaction.")

        return "\n".join(lines)

    def _build_threat_fallback(self, game_state: dict[str, Any], threat: dict[str, Any]) -> str:
        name = str(threat.get("name", "That card") or "That card")
        warning = str(threat.get("warning", "") or "").strip()
        answers = self._identify_threat_answers(game_state, threat)
        pressure = self._threat_pressure_summary(game_state, threat)
        threat_card = threat.get("card") if isinstance(threat.get("card"), dict) else threat
        threat_type = str(threat_card.get("type_line", "") or "").lower()

        if answers:
            return f"{name} is the key threat. Best line: use {answers[0].split(' (', 1)[0]} on it now, because {warning.lower() if warning else 'it will snowball if it stays in play'}."

        if "planeswalker" in threat_type and "No untapped creatures" not in pressure:
            return f"{name} is the problem. Attack it this turn with the creatures you can spare and keep it from snowballing. {pressure}"

        if "creature" in threat_type:
            return f"{name} is the threat to plan around. You do not have clean instant removal up, so preserve blockers, avoid bad attacks into it, and dig toward an answer."

        return f"{name} is the card to answer. {warning if warning else 'It will generate value if left alone.'} If you cannot remove it now, play to contain it and protect your life total."

    def clear_deck_strategy(self) -> None:
        """Reset deck strategy for a new match."""
        self._deck_strategy = None
        self._deck_strategy_pending = False

    def analyze_deck(
        self, deck_cards: list[tuple[str, str, str]], backend=None
    ) -> Optional[str]:
        """Analyze a deck list and store the strategy summary.

        Args:
            deck_cards: List of (card_name, card_type, oracle_text) tuples
            backend: Optional separate backend instance (avoids lock contention
                     with advice calls when run on a background thread)

        Returns:
            Strategy string, or None on failure
        """
        import time

        start = time.perf_counter()
        self._deck_strategy_pending = True

        # Use dedicated backend if provided, otherwise fall back to shared one
        be = backend or self._backend

        try:
            # Group duplicates compactly: "4x Mountain (Basic Land)"
            from collections import Counter

            # Group by (name, type) for counting, but keep oracle text
            oracle_by_name: dict[str, str] = {}
            count_key = Counter()
            for name, card_type, oracle in deck_cards:
                count_key[(name, card_type)] += 1
                if oracle and name not in oracle_by_name:
                    oracle_by_name[name] = oracle

            deck_lines = []
            for (name, card_type), count in count_key.most_common():
                type_short = card_type.split("—")[0].strip() if card_type else "Unknown"
                line = f"{count}x {name} ({type_short})"
                # Include oracle text for non-basic-land spells so the LLM
                # knows what the card actually does instead of guessing
                oracle = oracle_by_name.get(name, "")
                is_basic = "basic" in (card_type or "").lower()
                if oracle and not is_basic:
                    oracle_short = self._remove_reminder_text(oracle).strip()
                    if oracle_short:
                        line += f" — {oracle_short}"
                deck_lines.append(line)

            deck_text = "\n".join(deck_lines)
            user_message = f"DECK LIST ({len(deck_cards)} cards):\n{deck_text}"

            # Deck analysis benefits from thinking (one-time, not real-time).
            # Also needs more tokens than game advice for the full strategy output.
            try:
                strategy = be.complete(
                    DECK_ANALYSIS_PROMPT,
                    user_message,
                    max_tokens=4096,
                    use_thinking=True,
                )
            except TypeError:
                # Backend doesn't support max_tokens parameter
                strategy = be.complete(DECK_ANALYSIS_PROMPT, user_message)

            # Don't store error/fallback messages as deck strategy
            if not strategy:
                logger.warning("Deck analysis returned empty response")
                return None
            # Check for backend auth/billing errors (e.g. "Credit balance is too low")
            from arenamcp.backend_detect import is_query_failure_retriable
            if (
                strategy.startswith("Error")
                or "didn't catch that" in strategy
                or is_query_failure_retriable(strategy)
            ):
                logger.warning(
                    f"Deck analysis returned error-like response: {strategy[:80]}"
                )
                return None

            self._deck_strategy = strategy
            # Seed the persistent game plan from the deck archetype.
            try:
                mgr = self._ensure_game_plan_mgr()
                if mgr is not None:
                    mgr.seed(self._deck_strategy)
            except Exception as e:
                logger.debug(f"Game-plan seed failed (non-fatal): {e}")
            elapsed = (time.perf_counter() - start) * 1000
            logger.info(
                f"Deck analysis complete: {elapsed:.0f}ms, {len(strategy)} chars"
            )
            return strategy
        except Exception as e:
            logger.error(f"Deck analysis failed: {e}")
            return None
        finally:
            self._deck_strategy_pending = False

    def get_deck_strategy_brief(
        self, deck_cards: list[tuple[str, str, str]], backend=None
    ) -> Optional[str]:
        """Generate a brief 3-5 sentence spoken strategy for a deck.

        Uses a conversational prompt suited for TTS output after a draft
        or when the user asks for /deck-strategy.

        Args:
            deck_cards: List of (card_name, card_type, oracle_text) tuples
            backend: Optional separate backend instance

        Returns:
            Brief strategy string, or None on failure
        """
        import time

        start = time.perf_counter()
        be = backend or self._backend

        try:
            from collections import Counter

            oracle_by_name: dict[str, str] = {}
            count_key = Counter()
            for name, card_type, oracle in deck_cards:
                count_key[(name, card_type)] += 1
                if oracle and name not in oracle_by_name:
                    oracle_by_name[name] = oracle

            deck_lines = []
            for (name, card_type), count in count_key.most_common():
                type_short = card_type.split("—")[0].strip() if card_type else "Unknown"
                line = f"{count}x {name} ({type_short})"
                oracle = oracle_by_name.get(name, "")
                is_basic = "basic" in (card_type or "").lower()
                if oracle and not is_basic:
                    oracle_short = self._remove_reminder_text(oracle).strip()
                    if oracle_short:
                        line += f" — {oracle_short}"
                deck_lines.append(line)

            deck_text = "\n".join(deck_lines)
            user_message = f"DECK LIST ({len(deck_cards)} cards):\n{deck_text}"

            strategy = be.complete(DECK_STRATEGY_BRIEF_PROMPT, user_message)

            if not strategy or strategy.startswith("Error"):
                logger.warning(f"Deck strategy brief failed: {strategy and strategy[:80]}")
                return None

            elapsed = (time.perf_counter() - start) * 1000
            logger.info(f"Deck strategy brief: {elapsed:.0f}ms, {len(strategy)} chars")
            return strategy
        except Exception as e:
            logger.error(f"Deck strategy brief failed: {e}")
            return None

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Rough token estimate for logging: ~4 chars per token.

        OPTIMIZATION: Added for prompt size monitoring.
        """
        return len(text) // 4

    def _remove_reminder_text(self, text: str) -> str:
        """Remove reminder text (text in parentheses) from oracle text."""
        import re

        # Handle nested parens if possible, but simple greedy match usually works for MTG
        # Use simple non-greedy match for multiple parens
        return re.sub(r"\(.*?\)", "", text)

    @staticmethod
    def _is_impending(card: dict) -> bool:
        """Check if a creature is in impending state (enchantment with time counters).

        When cast with impending, a card enters as an enchantment with time
        counters.  It is NOT a creature until the last counter is removed, so
        it should not be counted as an attacker, blocker, or combat threat.
        """
        counters = card.get("counters", {})
        has_time = any("time" in k.lower() for k in counters) if counters else False
        if not has_time:
            return False
        # Confirm oracle text mentions impending (avoids false positives on
        # other cards with time counters like suspend/vanishing)
        oracle = card.get("oracle_text", "").lower()
        return "impending" in oracle

    @staticmethod
    def _get_cmc(mana_cost: str) -> int:
        """Calculate converted mana cost from a mana cost string like '{1}{W}{W}'."""
        import re
        if not mana_cost:
            return 0
        cmc = 0
        generic = re.findall(r"\{(\d+)\}", mana_cost)
        cmc += sum(int(g) for g in generic)
        for color in "WUBRGC":
            cmc += len(re.findall(rf"\{{{color}\}}", mana_cost))
        hybrid = re.findall(r"\{[^}]+/[^}]+\}", mana_cost)
        cmc += len(hybrid)
        return cmc

    # ------------------------------------------------------------------
    # Helpers extracted from _format_game_context
    # ------------------------------------------------------------------

    def _compute_combat_trade(self, atk: dict, blk: dict) -> Optional[tuple[str, bool, bool]]:
        """Compute the combat trade result between an attacker and a blocker.

        Returns (result_string, atk_dies, blk_dies), or None if the blocker
        cannot legally block the attacker (e.g. flying vs no fly/reach).
        """
        atk_name = atk.get("name", "?")
        atk_pow = atk.get("power") or 0
        atk_tgh = atk.get("toughness") or 0
        atk_oracle = self._remove_reminder_text(atk.get("oracle_text", "")).lower()
        atk_has_fly = "flying" in atk_oracle
        atk_has_dth = "deathtouch" in atk_oracle
        atk_has_trample = "trample" in atk_oracle
        atk_has_fs = "first strike" in atk_oracle or "double strike" in atk_oracle

        blk_name = blk.get("name", "?")
        blk_pow = blk.get("power") or 0
        blk_tgh = blk.get("toughness") or 0
        blk_oracle = self._remove_reminder_text(blk.get("oracle_text", "")).lower()
        blk_has_fly = "flying" in blk_oracle
        blk_has_reach = "reach" in blk_oracle
        blk_has_dth = "deathtouch" in blk_oracle
        blk_has_fs = "first strike" in blk_oracle or "double strike" in blk_oracle

        if atk_has_fly and not blk_has_fly and not blk_has_reach:
            return None

        atk_dies = (blk_pow >= atk_tgh) or blk_has_dth
        blk_dies = (atk_pow >= blk_tgh) or atk_has_dth
        if atk_has_fs and not blk_has_fs:
            if atk_pow >= blk_tgh or atk_has_dth:
                atk_dies = False
        elif blk_has_fs and not atk_has_fs:
            if blk_pow >= atk_tgh or blk_has_dth:
                blk_dies = False

        if atk_dies and blk_dies:
            return "TRADE (both die)", True, True
        elif atk_dies:
            return f"{atk_name} dies, {blk_name} lives ({blk_tgh - atk_pow} left)", True, False
        elif blk_dies:
            trample_note = ""
            if atk_has_trample:
                spillover = atk_pow - blk_tgh
                if spillover > 0:
                    trample_note = f", {spillover} trample through"
            return f"{blk_name} dies, {atk_name} lives ({atk_tgh - blk_pow} left){trample_note}", False, True
        else:
            return "both live", False, False

    def _compute_optimal_blocking_damage(self, attackers: list[dict],
                                         blockers: list[dict]) -> int:
        """Compute minimum damage through after optimal blocking assignment."""
        available_blk = list(blockers)
        damage_through = 0
        sorted_atk = sorted(attackers, key=lambda c: c.get("power") or 0, reverse=True)
        for atk in sorted_atk:
            atk_pow = atk.get("power") or 0
            atk_oracle = self._remove_reminder_text(atk.get("oracle_text", "")).lower()
            atk_has_fly = "flying" in atk_oracle
            atk_has_trample = "trample" in atk_oracle
            valid = []
            for i, blk in enumerate(available_blk):
                blk_oracle = self._remove_reminder_text(blk.get("oracle_text", "")).lower()
                if atk_has_fly and "flying" not in blk_oracle and "reach" not in blk_oracle:
                    continue
                valid.append((i, blk))
            if valid:
                if atk_has_trample:
                    idx, blocker = max(valid, key=lambda x: x[1].get("toughness") or 0)
                else:
                    idx, blocker = min(valid, key=lambda x: x[1].get("toughness") or 0)
                available_blk.pop(idx)
                if atk_has_trample:
                    spillover = max(0, atk_pow - (blocker.get("toughness") or 0))
                    damage_through += spillover
            else:
                damage_through += atk_pow
        return damage_through

    def _format_legal_moves(self, game_state: dict[str, Any],
                            local_seat: int) -> tuple[list[str], str]:
        """Determine the legal moves and return (valid_moves, valid_moves_str)."""
        pending = game_state.get("pending_decision")
        if pending == "Mulligan":
            return ["KEEP", "MULLIGAN"], "KEEP, MULLIGAN"
        elif pending == "Mulligan Bottom":
            hand_cards = game_state.get("hand", [])
            card_names = [c.get("name", "Unknown") for c in hand_cards]
            return [f"Bottom: {n}" for n in card_names], ", ".join(card_names)
        else:
            try:
                from arenamcp.rules_engine import RulesEngine
                valid_moves = RulesEngine.get_legal_actions(game_state)

                # Override generic casting_time_options legal actions with
                # resolved modal option names from bridge data
                dec_ctx = game_state.get("decision_context") or {}
                if dec_ctx.get("type") == "casting_time_options":
                    modal_moves = self._resolve_modal_legal_actions(game_state)
                    if modal_moves:
                        valid_moves = modal_moves

                if not valid_moves:
                    return [], 'NONE \u2014 say "pass priority"'
                else:
                    return valid_moves, ", ".join(valid_moves)
            except Exception as e:
                logger.error(f"RulesEngine error: {e}")
                return [], "Error"

    def _resolve_modal_legal_actions(self, game_state: dict[str, Any]) -> list[str]:
        """Resolve bridge CastingTimeOption modal entries to readable legal actions."""
        bridge_actions = game_state.get("_bridge_actions") or []
        modal_actions: list[tuple[int, str]] = []

        for ba in bridge_actions:
            if ba.get("actionType") != "CastingTimeOption":
                continue
            kind = ba.get("choiceKind", "")
            opt_idx = ba.get("optionIndex", 0)
            grp_id = ba.get("grpId", 0)

            if kind == "modal" and grp_id:
                try:
                    from arenamcp import server
                    info = server.get_card_info(grp_id)
                    oracle = info.get("oracle_text", "")
                    # Modal option oracle texts are typically short single-line effects
                    label = oracle.split("\n")[0].strip() if oracle else info.get("name", f"Mode {opt_idx + 1}")
                except Exception:
                    label = f"Mode {opt_idx + 1}"
                modal_actions.append((opt_idx, f"Mode {opt_idx}: {label}"))
            elif kind == "numeric_input":
                # X chooser entries (P3-1): the plugin enumerates legal X
                # values as per-entry SubmitX actions.
                val = ba.get("numericValue")
                if val is not None:
                    modal_actions.append(
                        (500 + int(val), f"X = {val}")
                    )
            elif kind == "done":
                modal_actions.append((999, "Done (confirm cast)"))

        if not modal_actions:
            return []

        modal_actions.sort(key=lambda x: x[0])
        return [label for _, label in modal_actions]

    def _format_post_land_planning(self, game_state: dict[str, Any],
                                   local_seat: int, valid_moves: list[str],
                                   is_my_turn: bool, phase: str) -> list[str]:
        """Compute post-land-drop planning lines."""
        import re as _re_plan
        from arenamcp.rules_engine import RulesEngine

        lines: list[str] = []
        local_player = next(
            (p for p in game_state.get("players", []) if p.get("is_local")), None
        )
        lands_played_count = local_player.get("lands_played", 0) if local_player else 0
        _stack = game_state.get("stack", [])
        has_land_drop = (
            is_my_turn and "Main" in phase and len(_stack) == 0
            and lands_played_count == 0
        )
        if not (has_land_drop and valid_moves):
            return lines

        hand_cards = game_state.get("hand", [])
        bf = game_state.get("battlefield", [])
        cur_mana = RulesEngine._count_available_mana(game_state, local_seat)

        hand_lands: dict[str, dict] = {}
        for c in hand_cards:
            if "Land" in c.get("type_line", ""):
                name = c.get("name", "")
                if name not in hand_lands:
                    hand_lands[name] = c

        if not hand_lands:
            return lines

        has_spelunking = any(
            c.get("owner_seat_id") == local_seat and "spelunking" in (c.get("name") or "").lower()
            for c in bf
        )

        post_land_parts = []
        for land_name, land_card in hand_lands.items():
            land_oracle = land_card.get("oracle_text", "")
            oracle_low = land_oracle.lower()
            enters_tapped = (
                "enters tapped" in oracle_low or "enters the battlefield tapped" in oracle_low
            ) and not has_spelunking

            post_mana = cur_mana if enters_tapped else cur_mana + 1
            land_colors: set[str] = set()
            for color, basic in [("W", "Plains"), ("U", "Island"), ("B", "Swamp"),
                                 ("R", "Mountain"), ("G", "Forest")]:
                if basic in land_name or f"{{{color}}}" in land_oracle:
                    land_colors.add(color)
            if "any color" in oracle_low:
                land_colors = {"W", "U", "B", "R", "G"}

            # Pre-compute whether we have any creatures for targeting checks
            my_creatures = [c for c in bf
                            if c.get("owner_seat_id") == local_seat
                            and c.get("power") is not None
                            and "land" not in c.get("type_line", "").lower()]

            new_casts = []
            for c in hand_cards:
                if "Land" in c.get("type_line", ""):
                    continue
                cost = c.get("mana_cost", "")
                cmc = RulesEngine._parse_cmc(cost)
                if cur_mana < cmc <= post_mana:
                    colored_pips = set(_re_plan.findall(r"\{([WUBRG])\}", cost))
                    existing_colors: set[str] = set()
                    for bf_card in bf:
                        if bf_card.get("owner_seat_id") == local_seat and not bf_card.get("is_tapped"):
                            bf_oracle = bf_card.get("oracle_text", "")
                            bf_name = bf_card.get("name", "")
                            for clr, bsc in [("W", "Plains"), ("U", "Island"), ("B", "Swamp"),
                                             ("R", "Mountain"), ("G", "Forest")]:
                                if bsc in bf_name or f"{{{clr}}}" in bf_oracle:
                                    existing_colors.add(clr)
                    available_colors = land_colors | existing_colors
                    if not colored_pips or colored_pips.issubset(available_colors):
                        # Skip spells that need creature targets we don't have
                        c_oracle = (c.get("oracle_text", "") or "").lower()
                        needs_my_creature = (
                            "target creature you control" in c_oracle
                            or "creature you control fights" in c_oracle
                        )
                        if needs_my_creature and not my_creatures:
                            continue
                        new_casts.append(c.get("name", "?"))
            if new_casts:
                post_land_parts.append(f"Play {land_name} \u2192 Cast {', '.join(new_casts)}")
        if post_land_parts:
            lines.append(f"THEN: {'; '.join(post_land_parts)}")
        return lines

    def _format_casting_time_options(
        self, game_state: dict[str, Any], decision_context: dict[str, Any]
    ) -> list[str]:
        """Format casting-time options with resolved modal option names.

        When bridge actions contain CastingTimeOption entries with choiceKind="modal",
        resolve each option's grpId to a card name so the LLM knows exactly what
        modal_index 0 vs 1 vs 2 means (e.g. "Search library" vs "Proliferate").
        """
        lines: list[str] = []

        # Try to extract modal options from bridge actions
        bridge_actions = game_state.get("_bridge_actions") or []
        modal_options: list[tuple[int, str]] = []  # (optionIndex, resolved_name)

        for ba in bridge_actions:
            if ba.get("actionType") != "CastingTimeOption":
                continue
            if ba.get("choiceKind") != "modal":
                continue
            opt_idx = ba.get("optionIndex", 0)
            grp_id = ba.get("grpId", 0)
            if grp_id:
                try:
                    from arenamcp import server
                    info = server.get_card_info(grp_id)
                    name = info.get("name", f"Option {opt_idx}")
                    oracle = info.get("oracle_text", "")
                    # For modal options, the grpId resolves to the mode's
                    # oracle text (e.g. "Search your library for a basic land...")
                    # Use the oracle text if short enough, otherwise just the name
                    if oracle and len(oracle) < 120:
                        label = oracle.split("\n")[0].strip()
                    else:
                        label = name
                except Exception:
                    label = f"Option {opt_idx}"
            else:
                label = ba.get("label", f"Option {opt_idx}")
            modal_options.append((opt_idx, label))

        if modal_options:
            modal_options.sort(key=lambda x: x[0])
            lines.append(f"!!! DECISION: CHOOSE MODE ({len(modal_options)} options) !!!")
            for opt_idx, label in modal_options:
                lines.append(f"  modal_index={opt_idx}: {label}")
            lines.append("Set modal_index to the number of the best option.")
        else:
            # Fallback: no bridge data, generic casting-time prompt
            lines.append("!!! DECISION: CHOOSE CASTING OPTION !!!")
            lines.append("Evaluate: alternative cost vs normal cost (Foretell, Flashback, Escape)")

        return lines

    def _format_decision_lines(self, game_state: dict[str, Any]) -> list[str]:
        """Format decision context into display lines for the LLM prompt."""
        lines: list[str] = []
        pending_decision = game_state.get("pending_decision")
        decision_context = game_state.get("decision_context")
        if not pending_decision:
            return lines

        if decision_context:
            dec_type = decision_context.get("type", "unknown")
            # Bridge request type can disambiguate generic decision types
            # (e.g., "group_selection" might be scry, surveil, or mulligan_bottom)
            bridge_req = game_state.get("_bridge_request_type")
            if dec_type == "unknown_req" and bridge_req:
                from arenamcp.gre_bridge import _BRIDGE_REQUEST_TO_DECISION_TYPE
                mapped = _BRIDGE_REQUEST_TO_DECISION_TYPE.get(bridge_req)
                if mapped:
                    dec_type = mapped
            _simple = {
                "mulligan_bottom": lambda ctx: [
                    f"!!! DECISION: MULLIGAN - PUT {max(1, 7 - len(game_state.get('hand', [])) + 1)} CARD(S) ON BOTTOM !!!",
                    "Keep: lands + on-curve plays | Bottom: expensive/off-color/redundant"],
                "assign_damage": lambda ctx: ["!!! DECISION: ASSIGN COMBAT DAMAGE !!!",
                    "Order: kill most important blocker/attacker first"],
                "order_combat_damage": lambda ctx: ["!!! DECISION: ORDER COMBAT DAMAGE !!!",
                    "Order: prioritize killing the biggest threat"],
                "search": lambda ctx: ["!!! DECISION: SEARCH LIBRARY !!!",
                    "Choose: what you need most \u2014 land, removal, threat, or answer"],
                "choose_starting_player": lambda ctx: ["!!! DECISION: PLAY OR DRAW !!!",
                    "Aggro decks: PLAY (tempo). Control/limited: DRAW (card advantage)"],
                "explore": lambda ctx: ["!!! DECISION: EXPLORE !!!",
                    "Keep land on top if needed, otherwise bottom for a better draw"],
                "select_replacement": lambda ctx: ["!!! DECISION: ORDER REPLACEMENT EFFECTS !!!",
                    "Choose: apply the replacement that gives most advantage first"],
                "casting_time_options": None,  # Handled below with modal option resolution
                "select_counters": lambda ctx: ["!!! DECISION: SELECT COUNTERS !!!",
                    "Choose: remove least valuable counters, keep most impactful"],
                "order_triggers": lambda ctx: ["!!! DECISION: ORDER TRIGGERED ABILITIES !!!",
                    "Order: resolve most impactful trigger last (it resolves first)"],
                "select_n_group": lambda ctx: ["!!! DECISION: SELECT FROM GROUP !!!"],
                "select_from_groups": lambda ctx: ["!!! DECISION: SELECT FROM GROUPS !!!"],
                "search_from_groups": lambda ctx: ["!!! DECISION: SEARCH FROM GROUPS !!!"],
                "gather": lambda ctx: ["!!! DECISION: GATHER !!!"],
            }
            if dec_type in _simple and _simple[dec_type] is not None:
                lines.extend(_simple[dec_type](decision_context))
            elif dec_type == "casting_time_options":
                lines.extend(self._format_casting_time_options(game_state, decision_context))
            elif dec_type == "discard":
                lines.append(f"!!! DECISION: DISCARD {decision_context.get('count', 1)} card(s) !!!")
                lines.append("Choose: excess lands > high CMC uncastables > redundant copies")
            elif dec_type == "scry":
                lines.append(f"!!! DECISION: SCRY {decision_context.get('count', 1)} !!!")
                lines.append("Keep: needed lands/threats | Bottom: dead cards")
            elif dec_type == "surveil":
                lines.append(f"!!! DECISION: SURVEIL {decision_context.get('count', 1)} !!!")
                lines.append("Keep: want to draw | Graveyard: synergy or digging")
            elif dec_type == "target_selection":
                lines.append(f"!!! DECISION: TARGET for {decision_context.get('source_card', 'spell')} !!!")
                lines.append(
                    "FIRST decide whether this spell HELPS its target (ramp, "
                    "pump, protection, value auras) or HARMS it (damage, "
                    "destroy, exile, debuff, tax). Helpful effects target "
                    "YOUR permanents (YOURS); harmful ones target the "
                    "opponent's (OPP). Then pick the best candidate of that "
                    "side. Never put a beneficial aura on an opponent's "
                    "permanent."
                )
            elif dec_type == "modal_choice":
                lines.append(f"!!! DECISION: CHOOSE MODE ({decision_context.get('num_options', '?')} options) !!!")
                lines.append("Evaluate: which mode solves current problem best")
            elif dec_type == "declare_attackers":
                legal = self._filter_legal_attacker_names(
                    game_state, decision_context.get("legal_attackers", [])
                )
                lines.append(f"!!! DECISION: DECLARE ATTACKERS ({len(legal)} legal) !!!")
                if legal:
                    lines.append(f"Can attack: {', '.join(legal[:8])}")
                try:
                    local_seat = next(
                        (p.get("seat_id") for p in game_state.get("players", [])
                         if p.get("is_local")), None,
                    )
                    opp_cards = [
                        c for c in game_state.get("battlefield", [])
                        if c.get("owner_seat_id") != local_seat
                    ]
                    lines.extend(self._attack_tax_lines(opp_cards, game_state))
                except Exception:
                    pass
                lines.append("Choose: maximize damage while keeping safe blockers back")
            elif dec_type == "declare_blockers":
                legal = decision_context.get("legal_blockers", [])
                lines.append(f"!!! DECISION: DECLARE BLOCKERS ({len(legal)} legal) !!!")
                if legal:
                    lines.append(f"Can block: {', '.join(legal[:8])}")
                lines.extend(self._format_block_decision_details(game_state, decision_context))
                lines.append("Choose: trade up, double-block threats, protect life total")
                lines.append(
                    'ANSWER FORMAT: name every assignment — "Block [attacker] with '
                    '[blocker]" for each blocker you use — or say "No blocks" with '
                    'the damage you accept. NEVER say "block with X" without naming '
                    "which attacker X blocks."
                )
            elif dec_type == "pay_costs":
                source = decision_context.get("source_card", "spell")
                mana_cost = decision_context.get("mana_cost", "")
                cost_str = f" ({mana_cost})" if mana_cost else ""
                lines.append(f"!!! DECISION: PAY COSTS for {source}{cost_str} !!!")
                if decision_context.get("has_autotap", False):
                    lines.append("Auto-tap available \u2014 confirm or tap manually for better mana efficiency")
                else:
                    lines.append("Choose: tap lands that leave best mana open for responses")
            elif dec_type == "distribution":
                lines.append(f"!!! DECISION: DISTRIBUTE {decision_context.get('total', '?')} from {decision_context.get('source_card', 'effect')} !!!")
                lines.append("Distribute: maximize kills, finish off wounded targets first")
            elif dec_type == "numeric_input":
                source = decision_context.get("source_card", "effect")
                lines.append(f"!!! DECISION: CHOOSE NUMBER for {source} ({decision_context.get('min', 0)}-{decision_context.get('max', '?')}) !!!")
                lines.append("Choose: balance value vs. cost (life, mana, etc.)")
            elif dec_type == "mill":
                lines.append(f"!!! DECISION: MILL {decision_context.get('count', 1)} !!!")
            elif dec_type in ("sacrifice", "exile", "destroy", "return"):
                count = decision_context.get("count", 1)
                opts = decision_context.get("option_cards")
                lines.append(f"!!! DECISION: {dec_type.upper()} {count} !!!")
                if opts:
                    lines.append(f"Options: {', '.join(opts[:8])}")
                _advice = {"sacrifice": "Choose: sacrifice least valuable permanent for the board state",
                           "exile": "Choose: exile least impactful card",
                           "destroy": "Choose: destroy biggest threat on the board",
                           "return": "Choose: return least important permanent"}
                lines.append(_advice[dec_type])
            elif dec_type in ("choose_creature", "choose_land", "choose_enchantment",
                              "choose_artifact", "choose_permanent", "choose"):
                count = decision_context.get("count", 1)
                label = dec_type.replace("choose_", "").upper() or "ITEM"
                opts = decision_context.get("option_cards")
                lines.append(f"!!! DECISION: CHOOSE {label} ({count}) !!!")
                if opts:
                    lines.append(f"Options: {', '.join(opts[:8])}")
                lines.append("Choose: pick the option that best advances your game plan")
            elif dec_type == "actions_available":
                lines.append(f"!!! YOUR PRIORITY \u2014 {decision_context.get('num_actions', '?')} legal actions available !!!")
            else:
                lines.append(f"!!! DECISION: {pending_decision} !!!")
        else:
            lines.append(f"!!! DECISION: {pending_decision} !!!")

        if pending_decision == "Mulligan":
            lines.extend(self._format_mulligan_hand(game_state))
        return lines

    def _format_mulligan_hand(self, game_state: dict[str, Any]) -> list[str]:
        """Format mulligan hand summary lines."""
        import re as _re
        lines: list[str] = []
        my_hand = game_state.get("hand", [])
        if not my_hand:
            lines.append("Waiting for hand...")
            return lines
        lands = [c for c in my_hand if "land" in c.get("type_line", "").lower()]
        creatures = [c for c in my_hand if "creature" in c.get("type_line", "").lower()]
        spells = [c for c in my_hand if c not in lands and c not in creatures]
        cmcs = []
        for c in my_hand:
            cost = c.get("mana_cost", "")
            if cost:
                generic = sum(int(g) for g in _re.findall(r"\{(\d+)\}", cost))
                pips = len(_re.findall(r"\{[WUBRGC]\}", cost))
                cmcs.append(generic + pips)
            else:
                cmcs.append(0)
        avg_cmc = sum(cmcs) / len(cmcs) if cmcs else 0
        land_names = [c.get("name", "?") for c in lands]
        nonland_names = [f"{c.get('name', '?')} ({c.get('mana_cost', '')})" for c in my_hand if c not in lands]
        lines.append(f"MULLIGAN HAND: {len(lands)} lands, {len(creatures)} creatures, {len(spells)} spells, avg CMC {avg_cmc:.1f}")
        lines.append(f"  Lands: {', '.join(land_names) if land_names else 'NONE'}")
        lines.append(f"  Nonland: {', '.join(nonland_names) if nonland_names else 'NONE'}")
        lines.append("Decide: KEEP or MULLIGAN based on curve, colors, and land count")
        return lines

    def _format_mana_info(self, your_cards: list[dict], turn_num: int) -> tuple[list[str], int, dict[str, int]]:
        """Compute mana pool info. Returns (lines, total_mana, mana_pool)."""
        import re
        lines: list[str] = []
        mana_pool = {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0, "Any": 0}
        mana_sources: list[str] = []
        total_mana = 0
        creature_mana_source_count = 0

        for card in your_cards:
            type_line = card.get("type_line", "").lower()
            oracle = card.get("oracle_text", "")
            is_creature = "creature" in type_line
            is_land = "land" in type_line
            has_haste = "haste" in self._remove_reminder_text(oracle).lower()
            is_summoning_sick = (is_creature and card.get("turn_entered_battlefield") == turn_num and not has_haste)
            has_mana_ability = ("add {" in oracle.lower() or "add one mana" in oracle.lower())
            if is_land and not has_mana_ability:
                for basic in ("plains", "island", "swamp", "mountain", "forest"):
                    if basic in type_line:
                        has_mana_ability = True
                        break
            if not card.get("is_tapped"):
                if is_land or (is_creature and has_mana_ability and not is_summoning_sick):
                    total_mana += 1
                    name = card.get("name", "")
                    if is_creature and has_mana_ability and not is_summoning_sick:
                        creature_mana_source_count += 1
                    source_colors: list[str] = []
                    if "Plains" in name or "plains" in type_line or "{W}" in oracle:
                        mana_pool["W"] += 1; source_colors.append("W")
                    if "Island" in name or "island" in type_line or "{U}" in oracle:
                        mana_pool["U"] += 1; source_colors.append("U")
                    if "Swamp" in name or "swamp" in type_line or "{B}" in oracle:
                        mana_pool["B"] += 1; source_colors.append("B")
                    if "Mountain" in name or "mountain" in type_line or "{R}" in oracle:
                        mana_pool["R"] += 1; source_colors.append("R")
                    if "Forest" in name or "forest" in type_line or "{G}" in oracle:
                        mana_pool["G"] += 1; source_colors.append("G")
                    if "{C}" in oracle:
                        mana_pool["C"] += 1; source_colors.append("C")
                    if "any color" in oracle.lower():
                        mana_pool["Any"] += 1; source_colors.append("Any")
                    if len(source_colors) > 1:
                        mana_sources.append("/".join(source_colors))
                    elif len(source_colors) == 1:
                        mana_sources.append(source_colors[0])

        mana_bonus_notes: list[str] = []
        for card in your_cards:
            oracle_lower = card.get("oracle_text", "").lower()
            name = card.get("name", "")
            bonus_match = re.search(r"whenever you tap a creature for mana,?\s*add an additional \{(\w)\}", oracle_lower)
            if bonus_match and creature_mana_source_count > 0:
                bonus_color = bonus_match.group(1).upper()
                bonus_total = creature_mana_source_count
                total_mana += bonus_total
                if bonus_color in mana_pool:
                    mana_pool[bonus_color] += bonus_total
                for _ in range(bonus_total):
                    mana_sources.append(f"+{bonus_color}")
                logger.info(f"Mana bonus from {name}: +{bonus_total} {{{bonus_color}}} ({creature_mana_source_count} creature sources)")
            if "untap" in oracle_lower and ("mana value" in oracle_lower or "converted mana cost" in oracle_lower):
                untap_match = re.search(r"(?:mana value|converted mana cost)\s*(\d+)\s*or greater.*untap|cast.*(?:mana value|converted mana cost)\s*(\d+).*untap|untap.*(?:mana value|converted mana cost)\s*(\d+)", oracle_lower)
                if untap_match:
                    threshold = untap_match.group(1) or untap_match.group(2) or untap_match.group(3)
                    mana_bonus_notes.append(f"{name} untaps on MV{threshold}+ cast \u2192 tap again for extra mana")

        logger.info(f"Mana: {mana_pool} (Total: {total_mana})")
        if mana_sources:
            source_display = " ".join(f"{{{s}}}" if "/" in s else s for s in mana_sources)
            lines.append(f"Mana: {total_mana} (sources: {source_display})")
        else:
            lines.append("Mana: 0")
        for note in mana_bonus_notes:
            lines.append(f"\u26a0\ufe0f {note}")
        return lines, total_mana, mana_pool

    def _format_board_card(self, card: dict, local_seat: int, turn_num: int,
                           attachments: dict[int, list[dict]],
                           name_counts: Counter, name_seen: dict[str, int],
                           is_local: bool, *, for_planner: bool = False) -> list[str]:
        """Format a single battlefield card into display lines.

        Args:
            for_planner: If True, omit full oracle text for permanents that
                have been on the battlefield for more than one turn — the
                ability flags (FLY, RCH, DTH, etc.) already summarize what
                the planner needs. Cards that just entered keep oracle text
                so ETB triggers stay visible.
        """
        lines: list[str] = []
        name = card.get("name", "Unknown")
        type_line = card.get("type_line", "").lower()
        is_creature = "creature" in type_line
        is_land = "land" in type_line

        if name_counts[name] > 1:
            name_seen[name] = name_seen.get(name, 0) + 1
            display_name = f"{name} #{name_seen[name]}"
        else:
            display_name = name

        pt = (f" {card.get('power') or 0}/{card.get('toughness') or 0}"
              if is_creature or card.get("power") is not None else "")

        flags: list[str] = []
        if not is_creature and not is_land:
            if "equipment" in type_line: flags.append("EQUIPMENT")
            elif "artifact" in type_line: flags.append("ARTIFACT")
            if "enchantment" in type_line: flags.append("ENCHANT")
            if "planeswalker" in type_line: flags.append("PW")
        if card.get("is_tapped"): flags.append("T")

        oracle_text = self._remove_reminder_text(card.get("oracle_text", "")).lower()
        if "flying" in oracle_text: flags.append("FLY")
        if "reach" in oracle_text: flags.append("RCH")
        if is_local and "haste" in oracle_text: flags.append("HST")
        if "vigilance" in oracle_text: flags.append("VIG")
        if "trample" in oracle_text: flags.append("TRM")
        if "first strike" in oracle_text: flags.append("FS")
        if "deathtouch" in oracle_text: flags.append("DTH")
        if is_creature and card.get("turn_entered_battlefield") == turn_num and "haste" not in oracle_text:
            flags.append("SS")
        if self._is_impending(card): flags.append("IMPENDING")
        if card.get("is_attacking"): flags.append("ATK")
        if card.get("is_blocking"): flags.append("BLK")

        inst_id = card.get("instance_id")
        attached = attachments.get(inst_id, [])
        if any("doesn't untap" in (a.get("oracle_text") or "").lower() for a in attached):
            flags.append("LOCKED")

        obj_kind = card.get("object_kind", "")
        if obj_kind == "TOKEN":
            display_name = f"*{display_name}"
        counters = card.get("counters", {})
        counter_str = ""
        if counters:
            cparts = [f"{cc}{ct.replace('CounterType_', '')[:4]}" for ct, cc in counters.items()]
            counter_str = f" ({','.join(cparts)})"

        flag_str = f" [{','.join(flags)}]" if flags else ""
        lines.append(f"  {display_name}{pt}{counter_str}{flag_str}")

        raw_oracle = card.get("oracle_text", "")
        if raw_oracle and not is_land:
            stripped = self._remove_reminder_text(raw_oracle).strip()
            keyword_only = all(
                w in {"flying", "reach", "haste", "vigilance", "trample", "first", "strike",
                      "double", "deathtouch", "lifelink", "menace", "ward", "hexproof",
                      "indestructible", "defender"}
                for w in stripped.lower().replace(",", " ").replace("\n", " ").split() if w
            )
            # Planner skips full oracle text on long-resident permanents — the
            # flags already summarize relevant abilities. Recent ETBs keep
            # oracle text so triggered abilities stay visible.
            entered_recently = (turn_num - (card.get("turn_entered_battlefield") or 0)) <= 1
            if for_planner and not entered_recently:
                pass
            elif not keyword_only and len(stripped) > 0:
                lines.append(f"    {stripped}")

        if attached:
            for att in attached:
                att_name = att.get("name", "Unknown")
                att_oracle = self._remove_reminder_text(att.get("oracle_text", "")).strip()
                if is_local:
                    att_owner = "OPP" if att.get("owner_seat_id") != local_seat else "YOUR"
                else:
                    att_owner = "YOUR" if att.get("owner_seat_id") == local_seat else "OPP"
                lines.append(f"    >> {att_owner} AURA: {att_name}")
                if att_oracle:
                    lines.append(f"       {att_oracle}")
        return lines

    # Matches attack-tax permanents (Ghostly Prison, Propaganda, War Tax
    # activations aside): "...can't attack you unless their controller pays
    # {2} for each creature...". Arena oracle text encodes costs as {o2}.
    _ATTACK_TAX_RE = re.compile(
        r"can't attack(?: you| you or planeswalkers you control)? unless"
        r".{0,80}?pays?[^.{]*\{o?(\d+)\}",
        re.IGNORECASE | re.DOTALL,
    )

    def _detect_attack_taxes(self, opp_cards: list[dict]) -> list[tuple[str, int]]:
        """Find opponent permanents taxing each attacker, with cost each."""
        taxes: list[tuple[str, int]] = []
        for card in opp_cards:
            oracle = card.get("oracle_text", "") or ""
            m = self._ATTACK_TAX_RE.search(oracle)
            if m:
                try:
                    taxes.append((card.get("name", "Unknown"), int(m.group(1))))
                except ValueError:
                    continue
        return taxes

    def _attack_tax_lines(
        self, opp_cards: list[dict], game_state: Optional[dict[str, Any]]
    ) -> list[str]:
        """Warning lines when attacking costs extra mana per creature.

        Field report 2026-07-16: opponent had Ghostly Prison and the coach
        advised casting spells until empty, then attacking with six
        creatures — a {12} tax it never counted. This is calculator work,
        not LLM judgment: state the price and the affordable attacker count.
        """
        taxes = self._detect_attack_taxes(opp_cards)
        if not taxes:
            return []
        per_attacker = sum(cost for _, cost in taxes)
        names = ", ".join(f"{n} (+{{{c}}} per attacker)" for n, c in taxes)
        lines = [f"!! ATTACK TAX: {names} — attacking costs {{{per_attacker}}} PER CREATURE."]
        if game_state is not None:
            try:
                mana = self._available_mana_now(game_state)
                afford = mana // per_attacker if per_attacker else 0
                lines.append(
                    f"   With {mana} mana available you can pay for at most "
                    f"{afford} attacker(s). BUDGET MANA BEFORE CASTING SPELLS "
                    "if you plan to attack this turn."
                )
            except Exception:
                lines.append(
                    "   Reserve mana for the tax before casting spells if you "
                    "plan to attack this turn."
                )
        return lines

    def _format_attack_combat(self, your_cards: list[dict], opp_cards: list[dict],
                              local_player: Optional[dict], opponent_player: Optional[dict],
                              turn_num: int, valid_attackers: list[dict],
                              game_state: Optional[dict[str, Any]] = None) -> list[str]:
        """Format the attack-side combat analysis (your turn attacking)."""
        lines: list[str] = []
        lines.extend(self._attack_tax_lines(opp_cards, game_state))
        your_creatures = [c for c in your_cards if "creature" in c.get("type_line", "").lower() and not self._is_impending(c)]
        opp_creatures = [c for c in opp_cards if "creature" in c.get("type_line", "").lower() and not self._is_impending(c)]
        opp_blockers = [c for c in opp_creatures if not c.get("is_tapped")]
        opp_block_count = len(opp_blockers)
        opp_life = opponent_player.get("life_total", 20) if opponent_player else 20
        your_attack_power = sum(c.get("power") or 0 for c in valid_attackers)

        if valid_attackers:
            lethal = "LETHAL" if (opp_block_count == 0 and your_attack_power >= opp_life) else f"{opp_block_count}blk"
            attacker_names = [c.get("name", "?") for c in valid_attackers]
            atk_name_counts = Counter(attacker_names)
            atk_name_seen: dict[str, int] = {}
            deduped_names = []
            for n in attacker_names:
                if atk_name_counts[n] > 1:
                    atk_name_seen[n] = atk_name_seen.get(n, 0) + 1
                    deduped_names.append(f"{n} #{atk_name_seen[n]}")
                else:
                    deduped_names.append(n)
            lines.append(f"Atk: {len(valid_attackers)}cr/{your_attack_power}pwr vs {lethal} \u2014 can attack: {', '.join(deduped_names)}")
            if valid_attackers and opp_blockers:
                for atk in valid_attackers:
                    for blk in opp_blockers:
                        trade = self._compute_combat_trade(atk, blk)
                        if trade is None:
                            continue
                        result, atk_dies, blk_dies = trade
                        atk_name = atk.get("name", "?"); atk_pow = atk.get("power") or 0; atk_tgh = atk.get("toughness") or 0
                        blk_name = blk.get("name", "?"); blk_pow = blk.get("power") or 0; blk_tgh = blk.get("toughness") or 0
                        if atk_dies and blk_dies:
                            display_result = result
                        elif atk_dies:
                            display_result = f"BAD \u2014 {result}"
                        elif blk_dies:
                            display_result = f"GOOD \u2014 {result}"
                        else:
                            display_result = result
                        lines.append(f"  If {blk_name} {blk_pow}/{blk_tgh} blocks {atk_name} {atk_pow}/{atk_tgh}: {display_result}")
        else:
            lines.append("Atk: None (T/SS)")

        opp_attack_power = sum(c.get("power") or 0 for c in opp_creatures)
        your_life = local_player.get("life_total", 20) if local_player else 20
        if opp_attack_power > 0:
            non_attackers = [c for c in your_creatures if c not in valid_attackers]
            allout_dmg = self._compute_optimal_blocking_damage(opp_creatures, non_attackers)
            life_after_allout = your_life - allout_dmg
            noatk_dmg = self._compute_optimal_blocking_damage(opp_creatures, your_creatures)
            life_after_noatk = your_life - noatk_dmg
            life_margin = your_life - opp_attack_power
            if life_after_allout <= 0:
                if life_after_noatk > 0:
                    lines.append(f"\u26a0\ufe0f Crackback: opp {opp_attack_power}pwr \u2014 ALL-OUT lethal ({allout_dmg} through vs {your_life} life), but holding all {len(your_creatures)} blockers \u2192 only {noatk_dmg} through \u2192 SAFE at {life_after_noatk} life. Attack selectively!")
                else:
                    lines.append(f"\u26a0\ufe0f Crackback: opp {opp_attack_power}pwr \u2192 LETHAL even with all {len(your_creatures)} blockers ({noatk_dmg} through vs {your_life} life)! Must race or remove threats!")
            elif life_margin <= 0:
                if allout_dmg < opp_attack_power and len(non_attackers) > 0:
                    lines.append(f"Crackback: opp {opp_attack_power}pwr, but your {len(non_attackers)} blocker(s) absorb {opp_attack_power - allout_dmg} \u2192 only {allout_dmg} through vs {your_life} life \u2014 {'safe' if life_after_allout > 3 else 'tight'}")
                else:
                    lines.append(f"Crackback: {opp_attack_power}pwr vs your {your_life} life \u2014 LETHAL if no blockers held!")
            elif life_margin <= 3:
                lines.append(f"Crackback: {opp_attack_power}pwr vs your {your_life} life \u2014 DANGER (only {life_margin} margin!)")
            else:
                lines.append(f"Crackback: {opp_attack_power}pwr vs your {your_life} life \u2014 safe")

        # Deterministic attack solver — picks the attacker subset that
        # maximizes expected damage through while surviving worst-case
        # crackback. Surface its pick so the LLM can follow it.
        try:
            from arenamcp.combat_solver import optimal_attacks
            opp_next_turn_attackers = [c for c in opp_creatures]
            your_remaining_blockers = [
                c for c in your_creatures
                if c not in valid_attackers and not c.get("is_tapped")
            ]
            solver_plan = optimal_attacks(
                valid_attackers,
                opp_blockers,
                opp_life,
                your_life,
                opp_next_turn_attackers,
                your_remaining_blockers,
            )
            if solver_plan is not None:
                lines.append(f"Computed optimal attack: {solver_plan.explanation}")
        except Exception as e:
            logger.debug(f"combat solver (attack) failed: {e}")

        return lines

    def _format_block_combat(self, your_cards: list[dict], opp_cards: list[dict],
                             local_player: Optional[dict], turn_num: int,
                             phase: str, _inferred_atk_ids: set[int],
                             decision_context: Optional[dict[str, Any]] = None) -> list[str]:
        """Format the block-side combat analysis (opponent's turn)."""
        lines: list[str] = []
        ctx = decision_context or {}
        attacking = [c for c in opp_cards if c.get("is_attacking")]
        if not attacking and _inferred_atk_ids:
            attacking = [c for c in opp_cards if c.get("instance_id") in _inferred_atk_ids]
        if not attacking:
            # GRE-authoritative attacker ids from the DeclareBlockersReq
            # decision context (log path parity, issue #420).
            ctx_atk_ids = self._attacker_ids_from_decision_context(ctx)
            if ctx_atk_ids:
                attacking = [
                    c for c in opp_cards
                    if int(c.get("instance_id") or 0) in ctx_atk_ids
                ]
        flying_atk = [c for c in attacking if "flying" in self._remove_reminder_text(c.get("oracle_text", "")).lower()]
        ground_atk = [c for c in attacking if c not in flying_atk]
        your_creatures = [c for c in your_cards if "creature" in c.get("type_line", "").lower() and not c.get("is_tapped") and not self._is_impending(c)]
        flyer_blockers = [c for c in your_creatures if any(kw in self._remove_reminder_text(c.get("oracle_text", "")).lower() for kw in ["flying", "reach"])]

        if not attacking:
            return lines

        fly_dmg = sum(c.get("power") or 0 for c in flying_atk)
        gnd_dmg = sum(c.get("power") or 0 for c in ground_atk)
        total_incoming = fly_dmg + gnd_dmg
        your_life = local_player.get("life_total", 20) if local_player else 20

        # Explicit attacker list so the LLM knows exactly which creatures
        # are attacking (avoids confusion with same-named non-attackers).
        atk_names_raw = [c.get("name", "?") for c in attacking]
        _atk_counts = Counter(atk_names_raw)
        _atk_seen: dict[str, int] = {}
        atk_labels = []
        for c, n in zip(attacking, atk_names_raw):
            p = c.get("power") or 0; t = c.get("toughness") or 0
            if _atk_counts[n] > 1:
                _atk_seen[n] = _atk_seen.get(n, 0) + 1
                atk_labels.append(f"{n} #{_atk_seen[n]} {p}/{t}")
            else:
                atk_labels.append(f"{n} {p}/{t}")
        lines.append(f"Blk: {fly_dmg}fly/{gnd_dmg}gnd dmg | {len(flyer_blockers)}FLY-blk avail")
        lines.append(f"Attackers: {', '.join(atk_labels)}")
        life_after_no_blocks = your_life - total_incoming
        if life_after_no_blocks <= 0:
            lines.append(f"\u26a0\ufe0f No blocks \u2192 {total_incoming} dmg \u2192 DEAD (from {your_life} life)! Must block!")
        else:
            lines.append(f"No blocks \u2192 take {total_incoming} dmg \u2192 {life_after_no_blocks} life remaining")
        if flying_atk and not flyer_blockers:
            lines.append(f"\u26a0\ufe0f {fly_dmg} UNBLOCKABLE!")
        dth_atk = [c for c in attacking if "deathtouch" in self._remove_reminder_text(c.get("oracle_text", "")).lower()]
        if dth_atk:
            lines.append(f"\u26a0\ufe0f DEATHTOUCH: {', '.join(c.get('name', '?') for c in dth_atk)} \u2014 any blocker DIES regardless of toughness!")

        damage_through = self._compute_optimal_blocking_damage(attacking, your_creatures)
        life_after_blocks = your_life - damage_through
        if damage_through < total_incoming:
            if life_after_blocks <= 0:
                lines.append(f"\u26a0\ufe0f Best blocks \u2192 take {damage_through} dmg \u2192 DEAD (from {your_life} life)! Not enough blockers!")
            else:
                lines.append(f"Best blocks \u2192 take {damage_through} dmg \u2192 {life_after_blocks} life")
        else:
            life_after_blocks = life_after_no_blocks

        if your_creatures and attacking:
            for atk in attacking:
                for blk in your_creatures:
                    trade = self._compute_combat_trade(atk, blk)
                    if trade is None:
                        continue
                    result, _atk_dies, _blk_dies = trade
                    atk_name = atk.get("name", "?"); atk_pow = atk.get("power") or 0; atk_tgh = atk.get("toughness") or 0
                    blk_name = blk.get("name", "?"); blk_pow = blk.get("power") or 0; blk_tgh = blk.get("toughness") or 0
                    lines.append(f"  If {blk_name} {blk_pow}/{blk_tgh} blocks {atk_name} {atk_pow}/{atk_tgh}: {result}")

        opp_non_attacking = [c for c in opp_cards if "creature" in c.get("type_line", "").lower() and c not in attacking and not self._is_impending(c)]
        opp_next_turn_power = sum(c.get("power") or 0 for c in attacking) + sum(c.get("power") or 0 for c in opp_non_attacking)
        if opp_next_turn_power > 0 and life_after_blocks > 0:
            if opp_next_turn_power >= life_after_blocks:
                lines.append(f"\u26a0\ufe0f Next turn: opp can attack for up to {opp_next_turn_power}pwr \u2014 LETHAL if you're at {life_after_blocks} life after this combat! Preserve blockers!")

        # Deterministic block solver — grounds the LLM in the actual
        # material/life outcome of every legal block assignment rather
        # than letting it guess. The LLM should follow this unless it
        # has a specific reason (e.g. a combat trick in hand).
        try:
            from arenamcp.combat_solver import (
                blocker_allowed_attackers_map,
                optimal_blocks,
            )
            usable_blockers = [c for c in your_creatures if not c.get("is_tapped")]
            # Restrict to the GRE's legal blockers when the decision context
            # names them (creatures that can't block are excluded upstream).
            legal_blocker_ids: set[int] = set()
            if str(ctx.get("type") or "") == "declare_blockers":
                for bid in ctx.get("legal_blocker_ids") or []:
                    try:
                        legal_blocker_ids.add(int(bid))
                    except (TypeError, ValueError):
                        continue
            if legal_blocker_ids:
                gre_blockers = [
                    c for c in usable_blockers
                    if int(c.get("instance_id") or 0) in legal_blocker_ids
                ]
                if gre_blockers:
                    usable_blockers = gre_blockers
            allowed_map = blocker_allowed_attackers_map(
                ctx.get("raw_blockers") or []
            )
            solver_plan = optimal_blocks(
                attacking, usable_blockers, your_life,
                blocker_allowed_attackers=allowed_map or None,
            )
            if solver_plan is not None:
                lines.append(f"Computed optimal blocks: {solver_plan.explanation}")
        except Exception as e:
            logger.debug(f"combat solver (blocks) failed: {e}")

        return lines

    def _check_castability(self, type_line: str, cost: str, cmc: int,
                           reqs: dict[str, int], total_mana: int,
                           mana_pool: dict[str, int], can_play_land: bool) -> str:
        """Determine castability status string for a hand card."""
        if "land" in type_line:
            return "LAND" if can_play_land else "HOLD"
        elif total_mana >= cmc:
            color_ok = all(mana_pool.get(c, 0) + mana_pool.get("Any", 0) >= reqs[c] for c in "WUBRGC" if reqs[c] > 0)
            if color_ok:
                return "OK"
            missing_pips = "".join(f"{{{c}}}" * max(0, reqs[c] - mana_pool.get(c, 0) - mana_pool.get("Any", 0)) for c in "WUBRGC" if reqs[c] > 0)
            return f"NEED:{missing_pips}" if missing_pips else f"NEED:{max(1, cmc - total_mana)}"
        else:
            missing_pips = "".join(f"{{{c}}}" * max(0, reqs[c] - mana_pool.get(c, 0) - mana_pool.get("Any", 0)) for c in "WUBRGC" if reqs[c] > 0)
            generic_short = cmc - total_mana
            return f"NEED:{generic_short}+{missing_pips}" if missing_pips else f"NEED:{generic_short}"

    def _analyze_removal(self, oracle_lower: str, opp_creatures: list[dict],
                         opp_nonland: list[dict], all_creatures: list[dict],
                         battlefield: list[dict], card_name: str,
                         no_target_card_names: set[str]) -> str:
        """Analyze removal capabilities of a card. Mutates no_target_card_names."""
        import re
        removal_info = ""
        damage_match = re.search(r"deals?\s+(\d+)\s+damage", oracle_lower)
        minus_match = re.search(r"gets?\s+(-\d+)/(-\d+)", oracle_lower)
        is_destroy_creature = "destroy target creature" in oracle_lower
        is_exile_creature = "exile target creature" in oracle_lower
        # "destroy target creature or enchantment" / "or planeswalker" — broader than just creature
        is_destroy_creature_or = is_destroy_creature and ("or enchantment" in oracle_lower or "or planeswalker" in oracle_lower)
        is_exile_creature_or = is_exile_creature and ("or enchantment" in oracle_lower or "or planeswalker" in oracle_lower)
        is_destroy_permanent = "destroy target permanent" in oracle_lower or "destroy target nonland permanent" in oracle_lower
        is_destroy_art_ench = "destroy target artifact" in oracle_lower or "destroy target enchantment" in oracle_lower or "naturalize" in oracle_lower
        is_exile_permanent = "exile target permanent" in oracle_lower or "exile target nonland permanent" in oracle_lower or "exile target artifact" in oracle_lower or "exile target enchantment" in oracle_lower
        is_bounce_creature = "return target creature" in oracle_lower or ("put target creature" in oracle_lower and "top" in oracle_lower)
        is_bounce_permanent = "return target nonland permanent" in oracle_lower or "return target permanent" in oracle_lower

        if not (damage_match or minus_match or is_destroy_creature or is_exile_creature or is_destroy_permanent or is_destroy_art_ench or is_exile_permanent or is_bounce_creature or is_bounce_permanent):
            return removal_info

        if is_bounce_creature or is_bounce_permanent: removal_info = " [RM:bounce]"
        elif is_destroy_permanent or is_exile_permanent: removal_info = " [RM:perm]"
        elif is_destroy_creature_or or is_exile_creature_or: removal_info = " [RM:creat/ench]"
        elif is_destroy_art_ench: removal_info = " [RM:art/ench]"
        elif is_destroy_creature or is_exile_creature: removal_info = " [RM:creat]"
        elif damage_match: removal_info = f" [RM:<={int(damage_match.group(1))}T]"
        elif minus_match: removal_info = f" [RM:<={abs(int(minus_match.group(2)))}T]"

        if is_bounce_creature: target_pool = all_creatures
        elif is_bounce_permanent: target_pool = [c for c in battlefield if "land" not in c.get("type_line", "").lower()]
        elif is_destroy_creature_or or is_exile_creature_or:
            # "destroy target creature or enchantment" — check opponent creatures + enchantments
            target_pool = opp_creatures + [c for c in opp_nonland if "enchantment" in c.get("type_line", "").lower() or "planeswalker" in c.get("type_line", "").lower()]
        elif is_destroy_creature or is_exile_creature: target_pool = opp_creatures
        elif "nonland" in oracle_lower or is_destroy_permanent or is_exile_permanent: target_pool = opp_nonland
        elif is_destroy_art_ench: target_pool = [c for c in opp_nonland if any(t in c.get("type_line", "").lower() for t in ["artifact", "enchantment"])]
        else: target_pool = opp_creatures

        mv_match = re.search(r"mana value (\d+) or less", oracle_lower)
        if mv_match and target_pool:
            mv_limit = int(mv_match.group(1))
            target_pool = [c for c in target_pool if self._get_cmc(c.get("mana_cost", "")) <= mv_limit]

        if not target_pool:
            removal_info += " [NO TARGETS]"
            no_target_card_names.add(card_name)
        return removal_info

    def _format_hand_cards(self, game_state: dict[str, Any], local_seat: int,
                           total_mana: int, mana_pool: dict[str, int],
                           opp_cards: list[dict], battlefield: list[dict],
                           is_my_turn: bool, phase: str, turn_num: int,
                           valid_moves: list[str]) -> tuple[list[str], set[str], set[str]]:
        """Format the hand section. Returns (lines, no_target_card_names, uncastable_card_names)."""
        import re
        lines: list[str] = []
        no_target_card_names: set[str] = set()
        uncastable_card_names: set[str] = set()
        hand = game_state.get("hand", [])
        lines.append("")
        lines.append("HAND:")

        opp_creatures = [c for c in opp_cards if "creature" in c.get("type_line", "").lower() and not self._is_impending(c)]
        opp_nonland = [c for c in opp_cards if "land" not in c.get("type_line", "").lower()]
        all_creatures = [c for c in battlefield if c.get("power") is not None and "land" not in c.get("type_line", "").lower()]

        if not hand:
            lines.append("  (empty)")
            return lines, no_target_card_names, uncastable_card_names

        local_player = next((p for p in game_state.get("players", []) if p.get("is_local")), None)
        lands_played = local_player.get("lands_played", 0) if local_player else 0
        stack = game_state.get("stack", [])
        can_play_land = (lands_played == 0) and is_my_turn and "Main" in phase and len(stack) == 0
        hand_name_counts = Counter(c.get("name", "Unknown") for c in hand)
        hand_name_seen: dict[str, int] = {}

        for card in hand:
            name = card.get("name", "Unknown")
            cost = card.get("mana_cost", "")
            type_line = card.get("type_line", "").lower()
            oracle_text = card.get("oracle_text", "")
            oracle_lower = oracle_text.lower()
            is_instant = "instant" in type_line or "flash" in oracle_lower
            timing = "I" if is_instant else "S"

            cmc = 0
            reqs = {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0}
            if cost:
                generic = re.findall(r"\{(\d+)\}", cost)
                cmc += sum(int(g) for g in generic)
                for color in "WUBRGC":
                    count = len(re.findall(rf"\{{{color}\}}", cost))
                    reqs[color] += count
                    cmc += count
                hybrid = re.findall(r"\{[^}]+/[^}]+\}", cost)
                cmc += len(hybrid)

            castable = self._check_castability(type_line, cost, cmc, reqs, total_mana, mana_pool, can_play_land)

            # Track cards the player can't afford so they're filtered from Legal
            if castable.startswith("NEED"):
                uncastable_card_names.add(name)

            # Flag X-cost spells where X would be 0 — usually worthless
            has_x = "{X}" in cost or "{x}" in cost
            if has_x and "land" not in type_line:
                non_x_cost = cmc  # cmc already excludes X (parsed from {digit} and {color})
                x_value = max(0, total_mana - non_x_cost)
                if castable == "OK" and x_value == 0:
                    castable = "OK,X=0"

            removal_info = self._analyze_removal(oracle_lower, opp_creatures, opp_nonland, all_creatures, battlefield, name, no_target_card_names)

            # Detect spells that require creatures we don't have
            # Sagas are exempt: Chapter I typically creates tokens or has
            # non-targeted effects, so casting is still valuable even when
            # later chapters need "target creature you control".
            is_saga = "saga" in type_line
            if "land" not in type_line and "creature" not in type_line and not is_saga:
                my_creatures = [c for c in battlefield
                                if c.get("owner_seat_id") == local_seat
                                and c.get("power") is not None
                                and "land" not in c.get("type_line", "").lower()]
                needs_my_creature = (
                    "target creature you control" in oracle_lower
                    or "creature you control fights" in oracle_lower
                )
                if needs_my_creature and not my_creatures:
                    removal_info += " [NO TARGETS]"
                    no_target_card_names.add(name)

            # Auras must enchant a creature. A buff/protective Aura is only
            # worth casting on a creature YOU control; if you control none, the
            # only legal target is an opponent's creature and casting it just
            # hands the enemy a free buff. A debuff/removal Aura (Pacifism-style)
            # is the opposite — it needs an OPPONENT creature. Detect both so we
            # don't recommend an Aura that can only hit the wrong side.
            is_aura = "aura" in type_line and "enchantment" in type_line
            if is_aura and "enchant creature" in oracle_lower and "[NO TARGETS]" not in removal_info:
                def _is_creature(c: dict) -> bool:
                    tl = c.get("type_line", "").lower()
                    return (("creature" in tl or "CardType_Creature" in c.get("card_types", []))
                            and "land" not in tl)
                my_creatures = [c for c in battlefield
                                if c.get("controller_seat_id") == local_seat and _is_creature(c)]
                enemy_creatures = [c for c in battlefield
                                   if c.get("controller_seat_id") not in (None, local_seat) and _is_creature(c)]
                debuff_markers = (
                    "loses all abilities", "can't attack", "can't block",
                    "doesn't untap", "base power and toughness", "is a coward",
                    "can't be blocked by", "as long as enchanted",
                )
                is_debuff_aura = (
                    bool(re.search(r"gets? -\d+/-?\d+", oracle_lower))
                    or any(m in oracle_lower for m in debuff_markers)
                )
                relevant = enemy_creatures if is_debuff_aura else my_creatures
                if not relevant:
                    removal_info += " [NO TARGETS]"
                    no_target_card_names.add(name)

            is_basic_land = "land" in type_line and ("basic" in type_line or name in ["Plains", "Island", "Swamp", "Mountain", "Forest"])
            oracle_stripped = self._remove_reminder_text(oracle_text) if oracle_text else ""
            show_oracle = bool(oracle_text) and not is_basic_land

            type_tag = ""
            if "creature" not in type_line and "land" not in type_line:
                if "enchantment" in type_line and "aura" in type_line: type_tag = " (AURA)"
                elif "enchantment" in type_line: type_tag = " (ENCHANT)"
                elif "equipment" in type_line: type_tag = " (EQUIP)"
                elif "artifact" in type_line: type_tag = " (ART)"
                elif "planeswalker" in type_line: type_tag = " (PW)"

            if hand_name_counts[name] > 1:
                hand_name_seen[name] = hand_name_seen.get(name, 0) + 1
                display_name = f"{name} #{hand_name_seen[name]}"
            else:
                display_name = name

            lines.append(f"  {display_name}{type_tag} {cost} [{timing},{castable}]{removal_info}")
            if show_oracle:
                lines.append(f"    {oracle_stripped}")
        return lines, no_target_card_names, uncastable_card_names

    def _format_zones_and_events(self, game_state: dict[str, Any], local_seat: int, opp_seat: Optional[int]) -> list[str]:
        """Format recent events, revealed cards, stack, graveyard, command zone, and library."""
        lines: list[str] = []
        recent_events = game_state.get("recent_events", [])
        if recent_events:
            event_strs = []
            for evt in recent_events[-15:]:
                etype = evt.get("type", "")
                if etype == "damage_dealt": event_strs.append(f"{evt.get('source','?')} dealt {evt.get('amount',0)} to {evt.get('target','?')}")
                elif etype == "zone_transfer": event_strs.append(f"{evt.get('card','?')} moved zones")
                elif etype == "counter_added": event_strs.append(f"+{evt.get('amount',1)} counter on {evt.get('card','?')}")
                elif etype == "counter_removed": event_strs.append(f"-{evt.get('amount',1)} counter from {evt.get('card','?')}")
                elif etype == "token_created": event_strs.append(f"Token: {evt.get('card','?')}")
                elif etype == "card_revealed": event_strs.append(f"Revealed: {evt.get('card','?')}")
                elif etype == "controller_changed": event_strs.append(f"{evt.get('card','?')} changed controller")
            if event_strs:
                lines.append(f"Recent: {'; '.join(event_strs)}")

        revealed = game_state.get("revealed_cards", {})
        if revealed and opp_seat is not None:
            opp_revealed = revealed.get(str(opp_seat), revealed.get(opp_seat, []))
            if opp_revealed:
                lines.append(f"Opp revealed {len(opp_revealed)} card(s) this game")

        stack = game_state.get("stack", [])
        if stack:
            stack_items = [f"{'Y' if c.get('owner_seat_id') == local_seat else 'O'}:{c.get('name', 'Unknown')}" for c in stack]
            lines.append(f"Stack: {' > '.join(stack_items)}")

        graveyard = game_state.get("graveyard", [])
        if graveyard:
            your_gy = [c for c in graveyard if c.get("owner_seat_id") == local_seat]
            opp_gy = [c for c in graveyard if c.get("owner_seat_id") != local_seat]
            if your_gy or opp_gy:
                gy_parts = []
                if your_gy: gy_parts.append(f"Y={len(your_gy)} ({', '.join(c.get('name', '?') for c in your_gy[:8])})")
                if opp_gy: gy_parts.append(f"O={len(opp_gy)} ({', '.join(c.get('name', '?') for c in opp_gy[:8])})")
                lines.append(f"GY: {' '.join(gy_parts)}")

        command = game_state.get("command", [])
        if command:
            your_cmds = [c for c in command if c.get("owner_seat_id") == local_seat]
            opp_cmds = [c for c in command if c.get("owner_seat_id") != local_seat]
            cmd_parts = []
            for c in your_cmds:
                cost_str = f" {c.get('mana_cost', '')}" if c.get("mana_cost") else ""
                cmd_parts.append(f"  YOUR CMD: {c.get('name', 'Unknown')}{cost_str}")
                oracle = (c.get("oracle_text", "") or "").replace("\n", " ").strip()
                if oracle: cmd_parts.append(f"    {oracle}")
            for c in opp_cmds:
                cost_str = f" {c.get('mana_cost', '')}" if c.get("mana_cost") else ""
                cmd_parts.append(f"  OPP CMD: {c.get('name', 'Unknown')}{cost_str}")
                oracle = (c.get("oracle_text", "") or "").replace("\n", " ").strip()
                if oracle: cmd_parts.append(f"    {oracle}")
            lines.append("COMMAND ZONE:")
            lines.extend(cmd_parts)

        library_summary = game_state.get("library_summary", "")
        if library_summary:
            lines.append("")
            lines.append(library_summary)
        return lines

    def _resolve_raw_legal_actions(
        self, game_state: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Pick the freshest raw-action list available.

        Bridge actions are the most authoritative source (they reflect live
        castability and autotap solutions). For non-ActionsAvailable bridge
        requests (target-select, search, casting-time options, etc.) an empty
        bridge action list is itself authoritative — we must NOT fall back
        to stale `legal_actions_raw` from a previous priority window.
        """
        bridge_req = game_state.get("_bridge_request_type")
        bridge_request_class = game_state.get("_bridge_request_class")
        bridge_actions = game_state.get("_bridge_actions")
        is_actions_available_bridge_request = (
            bridge_req in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS
            or bridge_request_class in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS
        )
        if bridge_req and not is_actions_available_bridge_request:
            return bridge_actions or []
        return bridge_actions or game_state.get("legal_actions_raw") or []

    def _post_filter_uncastable_legal_moves(
        self,
        lines: list[str],
        valid_moves: list[str],
        raw_legal_actions: list[dict[str, Any]],
        no_target_card_names: set[str],
        uncastable_card_names: set[str],
        game_state: dict[str, Any],
    ) -> None:
        """Strip uncastable spells from the Legal: and LegalGRE: lines in-place.

        The GRE may report a spell as legal because it considers potential
        mana abilities, but our mana / target analysis can prove the spell
        actually can't be cast right now. Showing it as legal anyway makes
        the LLM suggest spells the engine will reject. This rewrites both
        lines[1] (the human-readable Legal: line) and any LegalGRE: line in
        place so the LLM only ever sees actionable options.
        """
        cards_to_filter = no_target_card_names | uncastable_card_names
        non_ok_cast_names = {
            m[5:].split("[", 1)[0].strip()
            for m in valid_moves
            if isinstance(m, str)
            and m.lower().startswith("cast ")
            and "[ok]" not in m.lower()
        }
        cards_to_filter |= non_ok_cast_names
        if not valid_moves:
            return

        filtered_moves = [
            m for m in valid_moves
            if not (
                isinstance(m, str)
                and m.lower().startswith("cast ")
                and (
                    "[ok]" not in m.lower()
                    or any(f"Cast {nt}" in m for nt in cards_to_filter)
                )
            )
        ]
        if filtered_moves == valid_moves:
            return

        if not filtered_moves:
            new_legal = 'NONE — say "pass priority"'
        else:
            new_legal = ", ".join(filtered_moves[:8])
            if len(filtered_moves) > 8:
                new_legal += f"... (+{len(filtered_moves) - 8})"
        lines[1] = f"Legal: {new_legal}"

        if not raw_legal_actions:
            return

        filter_grp_ids: set[int] = set()
        for zone_name in ("hand", "command"):
            for card in game_state.get(zone_name, []):
                if card.get("name") in cards_to_filter:
                    gid = card.get("grp_id")
                    if gid:
                        filter_grp_ids.add(gid)
        filtered_raw = [
            a for a in raw_legal_actions
            if not (
                a.get("actionType") == "ActionType_Cast"
                and (
                    a.get("grpId") in filter_grp_ids
                    or not a.get("autoTapSolution")
                )
            )
        ]
        for i, line in enumerate(lines):
            if isinstance(line, str) and line.startswith("LegalGRE:"):
                lines[i] = "LegalGRE: " + _format_legal_actions_raw_for_prompt(filtered_raw)
                break

    def _format_game_context(
        self, game_state: dict[str, Any], question: str = "",
        *, for_planner: bool = False,
    ) -> str:
        """Format the game state into a COMPACT context for the LLM.

        Orchestrator that delegates to focused helper methods for each section.

        Args:
            for_planner: If True, produce a leaner context for the autopilot
                action planner — drops heavy GRE JSON dumps and trims oracle
                text on long-resident permanents (the flags already summarize
                their relevant abilities). Coach advice path keeps full
                fidelity by default.
        """

        # Determine local player seat and active turn
        players = game_state.get("players", [])
        local_player = next((p for p in players if p.get("is_local")), None)
        local_seat = local_player.get("seat_id") if local_player else 1

        turn = game_state.get("turn", {})
        active_seat = turn.get("active_player", 0)
        priority_seat = turn.get("priority_player", 0)
        is_my_turn = active_seat == local_seat
        has_priority = priority_seat == local_seat

        phase = turn.get("phase", "Unknown").replace("Phase_", "")
        step = turn.get("step", "").replace("Step_", "")
        turn_num = turn.get("turn_number", 0)

        # Legal moves
        valid_moves, valid_moves_str = self._format_legal_moves(game_state, local_seat)

        lines = []
        match_num = game_state.get("_match_number")
        match_id = game_state.get("match_id") or ""
        match_tag = ""
        if match_num is not None:
            short_id = match_id[:8] if match_id else "?"
            match_tag = f" [Match #{match_num} id={short_id}]"
        lines.append(f"=== NEW GAME ==={match_tag}" if turn_num <= 1 and match_tag else f"=== GAME ==={match_tag}")
        lines.append(f"Legal: {valid_moves_str}")
        raw_legal_actions = self._resolve_raw_legal_actions(game_state)
        lines.extend(_build_bridge_context_lines(game_state, raw_legal_actions, for_planner=for_planner))

        # Post-land planning
        lines.extend(self._format_post_land_planning(game_state, local_seat, valid_moves, is_my_turn, phase))

        # Get player info
        opponent_player = None
        for p in players:
            if not p.get("is_local"):
                opponent_player = p
                break
        opp_seat = opponent_player.get("seat_id") if opponent_player else None

        active_label = "YOUR" if active_seat == local_seat else "OPP"
        priority_label = "You" if priority_seat == local_seat else "Opp"
        is_main_phase = "Main" in phase
        is_your_turn = active_seat == local_seat
        stack = game_state.get("stack", [])
        stack_empty = len(stack) == 0
        can_cast_sorcery = (is_your_turn and is_main_phase and stack_empty and has_priority)
        is_blocking = "DeclareBlock" in step and not is_your_turn

        # Decision context
        pending_decision = game_state.get("pending_decision")
        if pending_decision:
            lines.extend(self._format_decision_lines(game_state))

        # Turn/phase/priority line
        if pending_decision in ("Mulligan", "Mulligan Bottom"):
            lines.append("YOUR MULLIGAN DECISION")
        else:
            phase_str = f"{phase}/{step}" if step else phase
            lines.append(f"T{turn_num} {active_label} | {phase_str} | Pri:{priority_label}")

        # Timing rules
        if pending_decision not in ("Mulligan", "Mulligan Bottom"):
            if can_cast_sorcery:
                lines.append("Timing: ALL SPELLS")
            elif is_blocking:
                lines.append("ACTION: DECLARE BLOCKERS")
            elif is_your_turn and is_main_phase and not stack_empty:
                lines.append("Timing: ALL SPELLS (after stack resolves)")
            else:
                lines.append("Timing: INSTANTS ONLY")

        # Life totals
        your_life = local_player.get("life_total", "?") if local_player else "?"
        opp_life = opponent_player.get("life_total", "?") if opponent_player else "?"
        damage_taken = game_state.get("damage_taken", {})
        your_dmg = damage_taken.get(str(local_seat), damage_taken.get(local_seat, 0))
        opp_dmg = damage_taken.get(str(opp_seat), damage_taken.get(opp_seat, 0)) if opp_seat else 0
        your_dmg_str = f" (taken {your_dmg})" if your_dmg else ""
        opp_dmg_str = f" (taken {opp_dmg})" if opp_dmg else ""
        lines.append(f"Life: You={your_life}{your_dmg_str} Opp={opp_life}{opp_dmg_str}")

        # Battlefield
        battlefield = game_state.get("battlefield", [])
        your_cards = [c for c in battlefield if c.get("owner_seat_id") == local_seat and c.get("type_line", "").lower() != "ability"]
        opp_cards = [c for c in battlefield if c.get("owner_seat_id") != local_seat and c.get("type_line", "").lower() != "ability"]

        # Mana info
        mana_lines, total_mana, mana_pool = self._format_mana_info(your_cards, turn_num)
        lines.extend(mana_lines)

        # Land drop status. P2-9: lands_played is inferred post-message and
        # lags for seconds after a drop — the CURRENT window's menu is the
        # authority. "Land: AVAILABLE" with no "Play Land:" entry produced
        # play_land hallucinations (Forest #3, 2026-07-05 22:50).
        lands_played = local_player.get("lands_played", 0) if local_player else 0
        has_land_entry = any(
            str(la).strip().lower().startswith(("play land:", "action: playmdfc"))
            for la in (game_state.get("legal_actions") or [])
        )
        if is_your_turn and lands_played == 0 and has_land_entry:
            lines.append("Land: AVAILABLE")
        elif is_your_turn and lands_played == 0:
            lines.append("Land: not playable in this window (no Play Land action)")
        elif is_your_turn:
            lines.append(f"Land: USED ({lands_played})")
        else:
            lines.append("Land: N/A (opp turn)")

        # Build attachment map
        _attachments: dict[int, list[dict]] = {}
        for card in battlefield:
            parent_id = card.get("parent_instance_id")
            if parent_id is not None:
                _attachments.setdefault(parent_id, []).append(card)

        # Battlefield display
        if battlefield:
            lines.append("")
            lines.append("YOUR BOARD:")
            if your_cards:
                your_name_counts = Counter(c.get("name", "Unknown") for c in your_cards)
                your_name_seen: dict[str, int] = {}
                for card in your_cards:
                    lines.extend(self._format_board_card(
                        card, local_seat, turn_num, _attachments,
                        your_name_counts, your_name_seen, is_local=True,
                        for_planner=for_planner,
                    ))
            else:
                lines.append("  (empty)")

            # Pre-compute inferred attackers for DeclareBlock display
            _inferred_atk_ids: set[int] = set()
            _dec_ctx = game_state.get("decision_context") or {}
            _in_block_decision = (
                ("Combat" in phase and not is_your_turn and "DeclareBlock" in step)
                or str(_dec_ctx.get("type") or "") == "declare_blockers"
            )
            if _in_block_decision:
                has_explicit_atk = any(c.get("is_attacking") for c in opp_cards)
                if not has_explicit_atk:
                    # GRE-authoritative attacker ids from the DeclareBlockersReq
                    # (log path) / bridge blockers payload beat the tapped-
                    # creature heuristic — vigilance attackers stay untapped and
                    # mana-tapped creatures aren't attacking (issue #420).
                    _inferred_atk_ids |= self._attacker_ids_from_decision_context(_dec_ctx)
                if not has_explicit_atk and not _inferred_atk_ids:
                    for c in opp_cards:
                        c_type = c.get("type_line", "").lower()
                        c_oracle = self._remove_reminder_text(c.get("oracle_text", "")).lower()
                        is_ss = (c.get("turn_entered_battlefield") == turn_num and "haste" not in c_oracle)
                        if c.get("is_tapped") and "creature" in c_type and not is_ss:
                            _inferred_atk_ids.add(c.get("instance_id"))

            lines.append("OPP BOARD:")
            if opp_cards:
                opp_name_counts = Counter(c.get("name", "Unknown") for c in opp_cards)
                opp_name_seen: dict[str, int] = {}
                for card in opp_cards:
                    # Add inferred ATK flag before formatting
                    if card.get("instance_id") in _inferred_atk_ids and not card.get("is_attacking"):
                        card = dict(card)
                        card["is_attacking"] = True
                    lines.extend(self._format_board_card(
                        card, local_seat, turn_num, _attachments,
                        opp_name_counts, opp_name_seen, is_local=False,
                        for_planner=for_planner,
                    ))
            else:
                lines.append("  (empty)")

            # Combat analysis
            if ("Combat" in phase or "Main" in phase) and is_your_turn:
                your_creatures = [c for c in your_cards if "creature" in c.get("type_line", "").lower() and not self._is_impending(c)]
                valid_attackers = [
                    c for c in your_creatures
                    if not c.get("is_tapped")
                    and not (c.get("turn_entered_battlefield") == turn_num
                             and "haste" not in self._remove_reminder_text(c.get("oracle_text", "")).lower())
                ]
                lines.extend(self._format_attack_combat(
                    your_cards, opp_cards, local_player, opponent_player,
                    turn_num, valid_attackers, game_state=game_state
                ))
            elif "Combat" in phase and not is_your_turn:
                lines.extend(self._format_block_combat(
                    your_cards, opp_cards, local_player, turn_num, phase, _inferred_atk_ids,
                    decision_context=game_state.get("decision_context"),
                ))
        else:
            lines.append("")
            lines.append("BOARD: Empty")

        # Recent events and revealed cards
        lines.extend(self._format_zones_and_events(game_state, local_seat, opp_seat))

        # Hand cards
        hand_lines, no_target_card_names, uncastable_card_names = self._format_hand_cards(
            game_state, local_seat, total_mana, mana_pool,
            opp_cards, battlefield, is_my_turn, phase, turn_num, valid_moves
        )
        lines.extend(hand_lines)

        # Post-filter: drop spells the GRE thought were legal but our mana /
        # target analysis says aren't actually castable, and rewrite the
        # Legal: line and LegalGRE: raw-action line to match.
        self._post_filter_uncastable_legal_moves(
            lines,
            valid_moves,
            raw_legal_actions,
            no_target_card_names,
            uncastable_card_names,
            game_state,
        )

        return "\n".join(lines)

    def _filter_legal_attacker_names(
        self, game_state: dict[str, Any], legal_attackers: list[str]
    ) -> list[str]:
        """Filter declared attacker names against the GRE-authoritative list.

        The GRE protocol's ``qualifiedAttackers`` already enforces summoning
        sickness, tap state, defender, etc. Trusting it avoids false negatives
        when our local ``turn_entered_battlefield`` is stale (e.g. instance ID
        changes after ETB triggers reset the entered-turn tracker).

        Falls through to the input list if no GRE-authoritative declare-
        attackers context is available — that path only runs as a fallback-
        advice sanity check on hallucinated LLM names, where letting the names
        through unchanged is safer than incorrectly filtering them out.
        """
        if not legal_attackers:
            return []

        decision_ctx = game_state.get("decision_context") or {}
        if str(decision_ctx.get("type", "") or "").lower() == "declare_attackers":
            gre_names = decision_ctx.get("legal_attackers") or []
            if gre_names:
                valid = Counter(gre_names)
                filtered: list[str] = []
                for name in legal_attackers:
                    if valid[name] > 0:
                        filtered.append(name)
                        valid[name] -= 1
                if len(filtered) != len(legal_attackers):
                    logger.info(
                        "Filtered declare attackers vs GRE qualifiedAttackers: %s -> %s",
                        legal_attackers,
                        filtered,
                    )
                return filtered

        return list(legal_attackers)

    @staticmethod
    def _attacker_ids_from_decision_context(
        decision_context: Optional[dict[str, Any]],
    ) -> set[int]:
        """Attacker instance ids named by a declare_blockers decision context.

        The GRE DeclareBlockersReq is the authoritative statement of who is
        attacking: each blockers[] entry lists the attackerInstanceIds it may
        block. Both the log path (gamestate.py DeclareBlockersReq handler) and
        the bridge enrichment (gre_bridge._apply_bridge_blockers) surface that
        as ``attacker_ids`` + ``raw_blockers`` on decision_context.
        """
        ctx = decision_context or {}
        if str(ctx.get("type") or "") != "declare_blockers":
            return set()
        ids: set[int] = set()
        for aid in ctx.get("attacker_ids") or []:
            try:
                ids.add(int(aid))
            except (TypeError, ValueError):
                continue
        for blk in ctx.get("raw_blockers") or []:
            if not isinstance(blk, dict):
                continue
            for aid in blk.get("attackerInstanceIds") or []:
                try:
                    ids.add(int(aid))
                except (TypeError, ValueError):
                    continue
        return ids

    def _collect_block_decision_attackers(
        self, game_state: dict[str, Any]
    ) -> list[dict]:
        """Resolve the attacking creatures for the current block decision.

        Prefers battlefield ``is_attacking`` flags (attackState from the log,
        or bridge enrichment); falls back to the decision_context attacker ids
        so the log path stays precise even if the attackState annotation was
        missed (issue #420).
        """
        battlefield = game_state.get("battlefield", []) or []
        local_seat = None
        for p in game_state.get("players", []) or []:
            if p.get("is_local"):
                local_seat = p.get("seat_id")
                break
        opp_cards = [c for c in battlefield if c.get("owner_seat_id") != local_seat]
        attacking = [c for c in opp_cards if c.get("is_attacking")]
        if attacking:
            return attacking
        ctx_ids = self._attacker_ids_from_decision_context(
            game_state.get("decision_context")
        )
        if ctx_ids:
            return [
                c for c in opp_cards
                if int(c.get("instance_id") or 0) in ctx_ids
            ]
        return []

    _COMBAT_KEYWORDS = (
        "flying", "deathtouch", "trample", "first strike", "double strike",
        "menace", "lifelink", "vigilance", "indestructible",
    )

    def _combat_keyword_flags(self, card: dict) -> str:
        """Compact ``[FLYING,DEATHTOUCH]``-style suffix for combat listings."""
        oracle = self._remove_reminder_text(card.get("oracle_text", "")).lower()
        found = [
            kw.upper().replace(" ", "-")
            for kw in self._COMBAT_KEYWORDS
            if kw in oracle
        ]
        return f" [{','.join(found)}]" if found else ""

    def _attacker_label_map(self, attackers: list[dict]) -> dict[int, str]:
        """instance_id -> ``Name #N P/T [KEYWORDS]`` labels, deduped by name."""
        names = [c.get("name", "?") for c in attackers]
        counts = Counter(names)
        seen: dict[str, int] = {}
        out: dict[int, str] = {}
        for c, n in zip(attackers, names):
            p = c.get("power") or 0
            t = c.get("toughness") or 0
            label = n
            if counts[n] > 1:
                seen[n] = seen.get(n, 0) + 1
                label = f"{n} #{seen[n]}"
            out[int(c.get("instance_id") or 0)] = (
                f"{label} {p}/{t}{self._combat_keyword_flags(c)}"
            )
        return out

    def _format_block_decision_details(
        self, game_state: dict[str, Any], decision_context: dict[str, Any]
    ) -> list[str]:
        """Enumerate attackers (name, P/T, keywords) for a block decision.

        Also lists per-blocker legal-attacker candidates when the GRE says a
        blocker is restricted to a subset of the attackers (covers menace /
        skulk-style restrictions that keyword scans can't see). Log-path
        parity for issue #420: without these lines the LLM saw only blocker
        names and could not name which attacker to block.
        """
        lines: list[str] = []
        attackers = self._collect_block_decision_attackers(game_state)
        if not attackers:
            return lines
        label_by_id = self._attacker_label_map(attackers)
        lines.append(
            "Attackers: "
            + ", ".join(
                label_by_id[int(a.get("instance_id") or 0)] for a in attackers
            )
        )

        # Per-blocker candidate restrictions from the raw GRE blockers payload.
        raw_blockers = decision_context.get("raw_blockers") or []
        blocker_ids = decision_context.get("legal_blocker_ids") or []
        blocker_names = decision_context.get("legal_blockers") or []
        name_by_blocker_id: dict[int, str] = {}
        if len(blocker_ids) == len(blocker_names):
            for bid, bname in zip(blocker_ids, blocker_names):
                try:
                    name_by_blocker_id[int(bid)] = bname
                except (TypeError, ValueError):
                    continue
        try:
            from arenamcp.combat_solver import blocker_allowed_attackers_map
            allowed_map = blocker_allowed_attackers_map(raw_blockers)
        except Exception:
            allowed_map = {}
        all_attacker_ids = set(label_by_id)
        for bid, allowed in allowed_map.items():
            if not allowed or allowed >= all_attacker_ids:
                continue  # unrestricted — no extra line needed
            bname = name_by_blocker_id.get(bid, f"Creature {bid}")
            atk_labels = [
                label_by_id[a] for a in sorted(allowed) if a in label_by_id
            ]
            if atk_labels:
                lines.append(f"  {bname} can ONLY block: {', '.join(atk_labels)}")
        return lines

    def _extract_card_name_words(self, game_state: dict[str, Any]) -> set[str]:
        """Extract all words from card names in the current game state.

        These words are excluded from overuse tracking since they're card names.
        """
        import re

        card_words: set[str] = set()

        # Collect card names from all zones
        for zone in ["battlefield", "hand", "graveyard", "stack", "exile", "command"]:
            for card in game_state.get(zone, []):
                name = card.get("name", "")
                # Extract words from card name
                words = re.findall(r"\b[a-z]+\b", name.lower())
                card_words.update(words)

        return card_words

    # Fields trimmed from raw_json output: large, noisy, or internal. Includes
    # raw_gre_events (megabytes per turn), legal_actions_raw (the bridge action
    # list which is already surfaced via decision_context), and underscore-
    # prefixed fields used for internal bookkeeping. See _format_game_context_raw_json.
    _RAW_JSON_TRIM_FIELDS = frozenset({
        "raw_gre_events",
        "legal_actions_raw",
        "_match_number",
        "_pending_request_raw",
        "annotations",
    })

    def _normalize_game_state_cards(self, game_state: dict[str, Any]) -> None:
        """In-place normalize all card dictionaries in the game state to ensure
        they have a type_line, synthesizing one if type_line is empty but
        card_types or subtypes are present.
        """
        def normalize_card(card: dict[str, Any]) -> None:
            if not isinstance(card, dict):
                return
            type_line = card.get("type_line")
            if not type_line or not str(type_line).strip():
                card_types = card.get("card_types") or []
                subtypes = card.get("subtypes") or []
                if isinstance(card_types, str):
                    card_types = [card_types]
                if isinstance(subtypes, str):
                    subtypes = [subtypes]
                
                type_parts = []
                if card_types:
                    type_parts.append(" ".join(card_types))
                if subtypes:
                    type_parts.append("—")
                    type_parts.append(" ".join(subtypes))
                card["type_line"] = " ".join(type_parts).strip()

        # Normalize cards in list-based zones at the top level
        for key, value in game_state.items():
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and "instance_id" in item:
                        normalize_card(item)
            elif isinstance(value, dict):
                # Also normalize cards in nested dictionary lists (like game_state["zones"])
                for sub_key, sub_val in value.items():
                    if isinstance(sub_val, list):
                        for item in sub_val:
                            if isinstance(item, dict) and "instance_id" in item:
                                normalize_card(item)

    def _build_context(
        self, game_state: dict[str, Any], question: str = "",
        *, for_planner: bool = False,
    ) -> str:
        self._normalize_game_state_cards(game_state)
        """Pick the active prompt variant for the user-message context.

        Reads MTGACOACH_PROMPT_VARIANT once per call. Honors:
          - 'raw_json' -> _format_game_context_raw_json
          - anything else (or unset) -> _format_game_context (compressed)

        Forwards optional kwargs only when they're non-default to preserve
        the calling convention of legacy callers and tests that patch
        _format_game_context with a simple lambda(state).
        """
        kwargs: dict[str, Any] = {}
        if question:
            kwargs["question"] = question
        if for_planner:
            kwargs["for_planner"] = for_planner
        if os.environ.get("MTGACOACH_PROMPT_VARIANT", "default").lower() == "raw_json":
            return self._format_game_context_raw_json(game_state, **kwargs)
        return self._format_game_context(game_state, **kwargs)

    def _format_game_context_raw_json(
        self, game_state: dict[str, Any], question: str = "",
        *, for_planner: bool = False,
    ) -> str:
        """Ablation variant: emit the game_state dict as JSON, no compression.

        Tests Gemini's claim that the structured-English formatting in
        _format_game_context is obsolete for small models like gemma4:e2b.
        Strips obviously-noisy fields (raw_gre_events, internal markers)
        but does NO derivation — the model gets the same dict the rest of
        the coach pipeline sees. Round-trippable, content-faithful, and
        directly comparable to the compressed builder when both are run
        on the same game_state.

        for_planner is accepted for API parity with the compressed builder
        but currently has no effect here.
        """
        cleaned = {k: v for k, v in game_state.items() if k not in self._RAW_JSON_TRIM_FIELDS}
        body = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"), default=str)
        suffix = f"\n\nThe player asks: {question}" if question else ""
        return f"Game state (JSON):\n{body}{suffix}"

    @staticmethod
    def _plan_framing_instruction(
        plan_block: str, *, our_turn: bool, plan_changed: bool
    ) -> str:
        """Return the GAME PLAN prompt suffix, gated by when to recite it aloud.

        The plan stays in the prompt as context either way (so advice is
        plan-aware), but it's only *spoken* on the opponent's turn or when the
        win strategy just changed. On our own turn with an unchanged plan it's
        silent background and the model gives just the concrete play. Returns ""
        when there is no plan yet.
        """
        if not plan_block:
            return ""
        recite = (not our_turn) or plan_changed
        if recite:
            return (
                "\n\n" + plan_block
                + "\n\nLead with the concrete recommended move FIRST, then briefly "
                "name the plan and how this move advances it."
            )
        return (
            "\n\n" + plan_block
            + "\n\nUse this game plan as SILENT background only. Do NOT name, recite, "
            "or summarize the plan or win condition in your answer. Give ONLY the "
            "concrete next play and its immediate tactical reason."
        )

    def get_advice(
        self,
        game_state: dict[str, Any],
        question: Optional[str] = None,
        trigger: Optional[str] = None,
        style: Optional[str] = None,
        threat: Optional[dict[str, Any]] = None,
    ) -> str:
        """Get coaching advice for the current game state.

        Args:
            game_state: Dict from get_game_state() MCP tool
            question: Optional user question to answer
            trigger: Optional trigger name (e.g., "combat_attackers", "low_life")
            style: Advice style ("concise" or "verbose")

        Returns:
            Advice string from the LLM
        """
        import time

        total_start = time.perf_counter()

        # Build context. _build_context honors MTGACOACH_PROMPT_VARIANT
        # (default | raw_json) — see _format_game_context_raw_json for the
        # compression-ablation rationale.
        context_start = time.perf_counter()
        context = self._build_context(game_state)
        context_time = (time.perf_counter() - context_start) * 1000

        # Get card name words to exclude from overuse tracking
        card_words = self._extract_card_name_words(game_state)

        # Check for overused words to avoid (excluding card names)
        blacklisted = self._word_tracker.get_blacklisted(exclude_words=card_words)

        # Build dynamic system prompt
        system_prompt = self._system_prompt

        if blacklisted:
            avoid_list = ", ".join(blacklisted)
            system_prompt += f"\n\nIMPORTANT: Avoid using these overused words: {avoid_list}. Use different phrasing."
            logger.debug(f"Blacklisted words: {blacklisted}")

        # PHASE 2: Inject decision-specific guidance when a decision is pending
        decision_context = game_state.get("decision_context")
        if decision_context:
            dec_type = decision_context.get("type", "unknown")
            decision_guidance = DECISION_PROMPTS.get(dec_type)
            if decision_guidance:
                system_prompt += f"\n\n{decision_guidance}"
                logger.debug(f"Injected decision prompt for type: {dec_type}")

        # Build user message
        # Priority: explicit arg > object property > default.
        # Accept both new names (quick/chatty) and legacy (concise/verbose).
        selected_style = style if style else getattr(self, "advice_style", "quick")
        raw_key = selected_style.lower()
        if raw_key in ("chatty", "verbose"):
            style_key = "chatty"
        elif raw_key in ("quick", "concise"):
            style_key = "quick"
        else:
            style_key = raw_key
        is_verbose = style_key == "chatty"  # retained name for legacy code below

        if question:
            if is_verbose:
                user_message = f"{context}\n\nThe player asks: {question}\nProvide a thorough answer with reasoning."
            else:
                user_message = f"{context}\n\nThe player asks: {question}"
        elif trigger:
            if is_verbose:
                trigger_descriptions = {
                    "new_turn": "Your turn just started (Main 1). What is the best play and why? Consider alternatives.",
                    "opponent_turn": (
                        "Opponent's turn just started. Analyze their board, strategy, and game plan. "
                        "What threats should we prepare for? "
                        "What should we do on our next turn to counter them? "
                        "Explain your reasoning."
                    ),
                    "land_played": "A land was just played. What is the best next play? Explain why.",
                    "spell_resolved": "A spell just resolved. What is the best next play? Explain why.",
                    "priority_gained": "You have priority. Should you respond or pass? Explain your reasoning.",
                    "combat_attackers": "Combat: Declare attackers. Which creatures should attack and why? Default: attack with ALL eligible creatures unless you have a specific reason to hold one back (e.g., need a blocker to survive crackback). Explain the combat math.",
                    "combat_blockers": "Combat: Opponent is attacking. How should you block and why? Name the attacker each blocker blocks (\"Block [attacker] with [blocker]\") and explain the trade-offs.",
                    "low_life": "Your life is dangerously low! What's the survival plan? Explain the reasoning.",
                    "opponent_low_life": "Opponent's life is low — can you finish them? Explain the line.",
                    "stack_spell": "Something was just cast. Should you respond or let it resolve? Explain why.",
                    "stack_spell_yours": "Your spell is on the stack. Pass priority or hold? Explain your reasoning.",
                    "stack_spell_opponent": "Opponent just cast something. Should you respond or let it resolve? Explain why.",
                    "user_request": "Give detailed strategic advice for this moment with reasoning.",
                    "decision_required": "Decision required (scry, discard, target, mulligan, etc). What should the player choose and why?",
                    "threat_detected": "ALERT: A dangerous card just hit the battlefield! Explain the threat and how to deal with it.",
                    "losing_badly": "The board state looks very bad. Assess honestly: can we come back, or should we concede and save time?",
                }
            else:
                trigger_descriptions = {
                    "new_turn": "Your turn just started (Main 1). What is the ONE best play right now?",
                    "opponent_turn": (
                        "Opponent's turn just started. Briefly analyze their board and strategy. "
                        "What is their game plan? What threats should we prepare for? "
                        "What should we do on our next turn to counter them? "
                        "Keep it to 2-3 sentences focused on opponent's strategy and your plan."
                    ),
                    "land_played": "A land was just played. What is the ONE next play?",
                    "spell_resolved": "A spell just resolved. What is the ONE next play?",
                    "priority_gained": "You have priority. Respond or pass?",
                    "combat_attackers": "Combat: Declare attackers. Which creatures should attack? Default: attack with ALL eligible creatures unless you have a specific reason to hold one back (e.g., need a blocker to survive crackback).",
                    "combat_blockers": "Combat: Opponent is attacking. How should you block? Name the attacker each blocker blocks (\"Block [attacker] with [blocker]\").",
                    "low_life": "Your life is dangerously low! What's the survival plan?",
                    "opponent_low_life": "Opponent's life is low — can you finish them?",
                    "stack_spell": "Something was just cast. Respond or let it resolve?",
                    "stack_spell_yours": "Your spell is on the stack. Pass priority or hold?",
                    "stack_spell_opponent": "Opponent just cast something. Respond or let it resolve?",
                    "user_request": "Give quick strategic advice for this moment.",
                    "decision_required": "Decision required (scry, discard, target, mulligan, etc). What should the player choose?",
                    "threat_detected": "ALERT: A dangerous card just hit the battlefield!",
                    "losing_badly": "Board looks dire. Can we come back or should we concede?",
                }
            if trigger == "threat_detected" and threat:
                trigger_desc = self._build_threat_trigger_description(
                    game_state,
                    threat,
                    is_verbose=is_verbose,
                )
            else:
                trigger_desc = trigger_descriptions.get(trigger, f"Trigger: {trigger}")
            user_message = f"{context}\n\n{trigger_desc}"
        else:
            if is_verbose:
                user_message = f"{context}\n\nWhat's the best play right now? Explain your reasoning."
            else:
                user_message = f"{context}\n\nWhat's the best play right now?"

        # OPTIMIZATION: Log prompt size with token estimate
        prompt_chars = len(system_prompt) + len(user_message)
        prompt_tokens_est = self._estimate_tokens(system_prompt + user_message)
        context_lines = context.count("\n") + 1
        logger.info(
            f"[PROMPT] {context_lines} lines, {prompt_chars} chars, ~{prompt_tokens_est} tokens | context: {context_time:.1f}ms"
        )

        # Log backend diagnostics
        backend_info = self.get_backend_info()
        logger.info(
            f"[BACKEND] {backend_info['backend_name']} | model={backend_info['model']} | style={style_key}"
        )

        # style_key and is_verbose were already computed above for trigger descriptions

        # ── QUICK prompt ──────────────────────────────────────────────────
        # Single sentence, imperative, speakable in under 5 seconds.
        _quick_prompt = (
            DEFAULT_SYSTEM_PROMPT
            .replace(
                "Keep responses concise (2-3 sentences max) since they'll be spoken aloud.\n"
                "Focus ONLY on the final strategic recommendation.\n"
                "Do NOT show your thinking process, \"reasoning\", or \"corrections\".\n"
                "Do NOT use internal monologue tags like [plan] or [thought].\n"
                "Do NOT second-guess yourself in the text (e.g., \"Wait, I need to check...\").\n"
                "Be authoritative and decisive. Start your response immediately with the command.",

                "QUICK MODE: respond in ONE short imperative sentence, under 15 words. "
                "Just the action — no reasoning, no alternatives, no hedging. "
                "Examples: \"Play Forest.\" \"Cast Lightning Bolt on the dragon.\" "
                "\"Attack with all creatures.\" \"Pass priority.\" "
                "If the play truly requires context, use 2 short sentences max — "
                "but prefer one. Never exceed 20 words total.",
            )
            .replace(
                "Output directly as the coach. No preamble, no meta-commentary.",
                "Output directly as the coach. No preamble. No meta-commentary. One sentence.",
            )
        )

        # ── CHATTY prompt ─────────────────────────────────────────────────
        # Multiple sentences, explain the WHY, mention alternatives/tradeoffs,
        # feel conversational. Still capped so TTS doesn't run forever.
        _chatty_prompt = (
            DEFAULT_SYSTEM_PROMPT
            .replace(
                "Keep responses concise (2-3 sentences max) since they'll be spoken aloud.\n"
                "Focus ONLY on the final strategic recommendation.\n"
                "Do NOT show your thinking process, \"reasoning\", or \"corrections\".\n"
                "Do NOT use internal monologue tags like [plan] or [thought].\n"
                "Do NOT second-guess yourself in the text (e.g., \"Wait, I need to check...\").\n"
                "Be authoritative and decisive. Start your response immediately with the command.",

                "CHATTY MODE: give a conversational, natural-sounding recommendation "
                "of 3 to 5 sentences. Lead with the recommended play, then explain "
                "WHY it's right: the game state reasoning, combat math, or the "
                "tradeoff vs the most obvious alternative. Mention any relevant "
                "threat you're playing around. Speak like a friend watching over "
                "your shoulder — warm but focused. Cap it at ~80 words so speech "
                "stays under ~25 seconds. Still no internal monologue tags or "
                "self-correction — just deliver the reasoning cleanly.",
            )
            .replace(
                "Output directly as the coach. No preamble, no meta-commentary.",
                "Output directly as the coach. No preamble or meta-commentary — "
                "just lead with the play and explain the thinking.",
            )
        )

        prompts = {
            "quick":   _quick_prompt,
            "chatty":  _chatty_prompt,
            # Legacy aliases still work
            "concise": _quick_prompt,
            "verbose": _chatty_prompt,
            "normal":  DEFAULT_SYSTEM_PROMPT,
            "explain": DEFAULT_SYSTEM_PROMPT.replace(
                "Keep responses concise (2-3 sentences max)",
                "Explain your reasoning clearly but briefly.",
            )
            + "\nInclude a short explanation of WHY this is the best line.",
            "pirate": "You are a ruthless pirate captain coaching a swabby! Speak like a pirate! Yarr! Keep it short!",
        }

        effective_system_prompt = prompts.get(style_key, _quick_prompt)

        # Inject deck strategy if available — instruct model to reference it
        if self._deck_strategy:
            effective_system_prompt += (
                f"\n\nDECK STRATEGY:\n{self._deck_strategy}"
                "\n\nALWAYS consider this strategy when advising. Prioritize plays that:"
                "\n- Set up or execute the combos/synergies listed above"
                "\n- Advance the deck's win condition"
                "\n- Follow the ideal play pattern for the current game phase"
                "\nBriefly explain WHY a play matters for the deck's plan "
                "(e.g. 'Cast X — triggers Kodama for a free land, setting up combo next turn')."
            )

        # Persistent GAME PLAN: refresh on our own active turn, then frame the
        # advice as the next STEP in that plan. Fully guarded — a plan failure
        # must never break advice generation.
        #
        # Reciting the hierarchical plan/win aloud on EVERY turn is repetitive.
        # Speak it only on the opponent's turn (less to do — strategy recap is
        # useful) or when the win strategy just changed; on our own turn with an
        # unchanged plan, keep it as silent background and just give the play.
        # (When autopilot drives, advice goes through the planner, which narrates
        # the plan there.)
        try:
            mgr = self._ensure_game_plan_mgr()
            if mgr is not None:
                # Only reform on our own active turn. If the local seat is
                # unknown, treat the turn as ours (conservative — still refreshes).
                local_seat = None
                for p in game_state.get("players", []) or []:
                    if p.get("is_local"):
                        local_seat = p.get("seat_id")
                        break
                active_player = (game_state.get("turn", {}) or {}).get("active_player")
                our_turn = local_seat is None or active_player == local_seat
                intro_before = mgr.coach_intro()
                if our_turn:
                    mgr.maybe_reform(game_state)
                intro_after = mgr.coach_intro()
                plan_changed = bool(intro_after) and intro_after != intro_before
                effective_system_prompt += self._plan_framing_instruction(
                    mgr.plan_text() or "", our_turn=our_turn, plan_changed=plan_changed
                )
        except Exception as e:
            logger.debug(f"Game-plan injection failed (non-fatal): {e}")

        # Re-inject blacklisted words and decision guidance into effective prompt
        if blacklisted:
            avoid_list = ", ".join(blacklisted)
            effective_system_prompt += f"\n\nIMPORTANT: Avoid using these overused words: {avoid_list}. Use different phrasing."

        if decision_context:
            dec_type = decision_context.get("type", "unknown")
            decision_guidance = DECISION_PROMPTS.get(dec_type)
            if decision_guidance:
                effective_system_prompt += f"\n\n{decision_guidance}"

        # RAG: Inject relevant MTG rules for this situation
        try:
            if self._rules_db is None:
                from arenamcp.rules_db import RulesDB

                self._rules_db = RulesDB()
            rules = self._rules_db.get_rules_for_situation(game_state, trigger, limit=5)
            if rules:
                rules_lines = [f"- Rule {r['number']}: {r['text']}" for r in rules]
                effective_system_prompt += (
                    "\n\nRELEVANT MTG RULES (official — these override any conflicting assumptions):\n"
                    + "\n".join(rules_lines)
                )
                logger.debug(
                    f"Injected {len(rules)} rules: {[r['number'] for r in rules]}"
                )
        except Exception as e:
            logger.warning(f"Rules RAG error (non-fatal): {e}")

        # Get response with timeout to prevent hanging on slow models.
        # IMPORTANT: The external timeout MUST be longer than the backend's
        # internal timeout (timeout_s) so the backend releases its lock first.
        # If the external timeout fires first, the backend thread still holds
        # the lock, causing cascading lock-busy failures on subsequent calls
        # which triggers unnecessary restarts.
        backend_timeout = getattr(self._backend, 'timeout_s', 12.0)
        is_local = _is_local_backend(self._backend)
        if is_local:
            api_timeout = max(backend_timeout + 5, 45)  # Local models need more time
        else:
            api_timeout = max(backend_timeout + 5, 15)
        api_start = time.perf_counter()
        import concurrent.futures

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        complete_kwargs = (
            {"request_timeout_s": api_timeout}
            if isinstance(self._backend, ProxyBackend)
            else {}
        )
        future = executor.submit(
            self._backend.complete,
            effective_system_prompt,
            user_message,
            1000,
            **complete_kwargs,
        )
        try:
            response = future.result(timeout=api_timeout)
        except concurrent.futures.TimeoutError:
            is_local = _is_local_backend(self._backend)
            hint = " — try a smaller model or use a cloud backend" if is_local else ""
            logger.warning(
                f"LLM API call timed out after {api_timeout}s (model may be too slow for real-time coaching){hint}"
            )
            # Return error string (not empty) to avoid triggering the
            # consecutive-empty-response restart counter in standalone.py
            response = f"Error: LLM timed out after {api_timeout}s"
        # shutdown(wait=False) only abandons the thread; the SDK-level
        # request_timeout_s above is what actually unblocks it. Without
        # that, every hung backend call leaks a thread forever.
        executor.shutdown(wait=False)
        api_time = (time.perf_counter() - api_start) * 1000

        if trigger == "threat_detected" and threat and (not response or response.startswith("Error")):
            response = self._build_threat_fallback(game_state, threat)

        # Prepend a short plan framing for longer styles only (the "quick" style
        # has a very tight word cap — prepending would blow it). Fully guarded.
        try:
            if (
                style_key in ("normal", "chatty", "explain")
                and response
                and not response.startswith("Error")
                and not response.lstrip().lower().startswith("plan")
            ):
                mgr = self._game_plan_mgr
                intro = mgr.coach_intro() if mgr is not None else ""
                if intro:
                    response = f"{intro}. {response}"
        except Exception as e:
            logger.debug(f"Game-plan intro prepend failed (non-fatal): {e}")

        # POST-PROCESSING: Validate and fix common LLM issues (especially for smaller models)
        response = self._postprocess_advice(response, game_state, style=style_key)

        if trigger == "threat_detected" and threat:
            threat_name = str(threat.get("name", "") or "").strip()
            if threat_name and threat_name.lower() not in response.lower():
                response = f"{threat_name} is the key threat. {response}"

        self._word_tracker.record(response, exclude_words=card_words)

        total_time = (time.perf_counter() - total_start) * 1000
        logger.info(
            f"[TIMING] API call: {api_time:.0f}ms, total: {total_time:.0f}ms, response: {len(response)} chars"
        )

        return response

    def get_win_plan(
        self,
        game_state: dict[str, Any],
        turns: int,
        library_summary: str = "",
        backend=None,
    ) -> str:
        """Get a multi-turn strategic plan for winning in N turns.

        Args:
            game_state: Dict from get_game_state() MCP tool
            turns: Number of turns to plan for (2-8)
            library_summary: Compact summary of remaining library cards
            backend: Optional separate backend instance (e.g. thinking-enabled).
                     If provided, used instead of self._backend.

        Returns:
            Strategic plan string from the LLM
        """
        import time
        import concurrent.futures

        total_start = time.perf_counter()
        be = backend or self._backend

        # Build context (honors MTGACOACH_PROMPT_VARIANT)
        context = self._build_context(game_state)

        # Build system prompt with turn count injected
        system_prompt = WIN_PLAN_PROMPT.format(n=turns)

        # Inject deck strategy if available
        if self._deck_strategy:
            system_prompt += (
                f"\n\nDECK STRATEGY:\n{self._deck_strategy}"
                "\n\nAlign the plan with this deck's win conditions and play patterns."
            )

        # Build user message with game context and library
        user_message = context
        if library_summary:
            user_message += f"\n\nLIBRARY REMAINING:\n{library_summary}"
        user_message += f"\n\nCreate a plan to win in exactly {turns} turns."

        # Longer timeout for strategic plans (more tokens to generate).
        is_thinking = isinstance(be, ProxyBackend) and be.enable_thinking
        is_local = _is_local_backend(be)
        if is_thinking:
            api_timeout = 90
        elif is_local:
            api_timeout = 90  # Local models need much more time
        else:
            api_timeout = 45

        api_start = time.perf_counter()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        # Win plans need more tokens than standard advice (400).
        # Only ProxyBackend supports the max_tokens / request_timeout_s kwargs.
        if isinstance(be, ProxyBackend):
            future = executor.submit(
                be.complete,
                system_prompt,
                user_message,
                4096,
                request_timeout_s=api_timeout,
            )
        else:
            future = executor.submit(
                be.complete, system_prompt, user_message
            )
        try:
            response = future.result(timeout=api_timeout)
        except concurrent.futures.TimeoutError:
            logger.warning(
                f"Win plan API call timed out after {api_timeout}s"
            )
            response = ""
        executor.shutdown(wait=False)
        api_time = (time.perf_counter() - api_start) * 1000

        total_time = (time.perf_counter() - total_start) * 1000
        logger.info(
            f"[TIMING] Win plan API: {api_time:.0f}ms, total: {total_time:.0f}ms, "
            f"turns={turns}, response: {len(response)} chars"
        )

        return response

    def generate_post_match_analysis(
        self,
        advice_history: list[dict[str, Any]],
        match_result: str,
        match_duration_turns: int,
        deck_strategy: str = "",
        final_life_totals: Optional[dict] = None,
        opponent_played_cards: Optional[list[str]] = None,
        backend: Optional[Any] = None,
        missed_decisions: Optional[list[dict]] = None,
        replay_context: Optional[str] = None,
    ) -> str:
        """Generate a post-match strategic analysis from the advice log.

        Args:
            advice_history: Chronological list of advice dicts from the match
            match_result: "win", "loss", "draw", or "unknown"
            match_duration_turns: Total turn count of the match
            deck_strategy: Deck strategy summary (from analyze_deck)
            final_life_totals: {seat_id: life} at match end
            opponent_played_cards: Card names the opponent revealed
            backend: Optional dedicated backend (avoids lock contention)
            missed_decisions: Vision watchdog detections (unmapped decision points)
            replay_context: Parsed replay decision-point summary (from .rply file)

        Returns:
            Analysis string from the LLM, or "" on failure.
        """
        import time
        import concurrent.futures

        be = backend or self._backend

        # Build chronological match narrative
        lines = []
        result_label = (
            "VICTORY" if match_result == "win"
            else "DEFEAT" if match_result == "loss"
            else "DRAW" if match_result == "draw"
            else "UNKNOWN"
        )
        if result_label == "UNKNOWN":
            lines.append("MATCH RESULT: UNKNOWN — the result could not be determined automatically. "
                         "The player may have conceded, disconnected, or the opponent won by an "
                         "undetected mechanism. Do NOT assume the player won. If life totals suggest "
                         "the player was ahead, they likely conceded.")
        else:
            lines.append(f"MATCH RESULT: {result_label}")
        lines.append(f"MATCH LENGTH: {match_duration_turns} turns")

        if final_life_totals:
            for seat, life in final_life_totals.items():
                lines.append(f"Final life (Seat {seat}): {life}")

        if deck_strategy:
            lines.append(f"\nDECK STRATEGY:\n{deck_strategy}")

        if opponent_played_cards:
            lines.append(f"\nOPPONENT CARDS SEEN: {', '.join(opponent_played_cards[:30])}")

        lines.append("\nCHRONOLOGICAL ADVICE LOG:")
        for entry in advice_history:
            snap = entry.get("game_snapshot") or {}
            turn = snap.get("turn_number", "?")
            phase = snap.get("phase", "?")
            trigger = entry.get("trigger", "unknown")
            advice_text = entry.get("advice", "")
            ctx = entry.get("game_context", "") or ""

            # Include life totals from snapshot for each entry
            life_str = ""
            players = snap.get("players", [])
            if players:
                parts = [f"Seat{p.get('seat_id')}={p.get('life_total')}" for p in players]
                life_str = f" Life: {', '.join(parts)}"

            board_info = ""
            if snap.get("battlefield_count"):
                board_info = f" Board:{snap['battlefield_count']} Hand:{snap.get('hand_count', '?')}"

            # Strip library search targets and trim context for post-match
            # analysis — the full board state per turn is useful but the
            # 90+ card library list bloats the prompt for no analytic value.
            ctx_snippet = ctx
            if "\nLIBRARY SEARCH TARGETS" in ctx_snippet:
                ctx_snippet = ctx_snippet[:ctx_snippet.index("\nLIBRARY SEARCH TARGETS")]
            # Cap each entry's context to avoid huge prompts in long games
            if len(ctx_snippet) > 2000:
                ctx_snippet = ctx_snippet[:2000] + "\n[...truncated]"

            lines.append(f"\n--- Turn {turn}, {phase} [{trigger}]{life_str}{board_info} ---")
            if ctx_snippet:
                lines.append(f"Context: {ctx_snippet}")
            lines.append(f"Advice: {advice_text}")

        if missed_decisions:
            lines.append(f"\nVISION WATCHDOG DETECTIONS ({len(missed_decisions)} missed decision points):")
            lines.append("These are moments where the game was waiting for player input")
            lines.append("but no trigger fired — detected by tempo anomaly + VLM screen analysis.")
            for i, md in enumerate(missed_decisions, 1):
                lines.append(
                    f"  {i}. Turn {md.get('turn', '?')}, {md.get('phase', '?')}: "
                    f"{md.get('decision_type', 'unknown')} — "
                    f"\"{md.get('prompt_text', '')}\" "
                    f"(stall={md.get('stall_duration_s', '?')}s, conf={md.get('confidence', '?')})"
                )

        if replay_context:
            lines.append(f"\nREPLAY DATA (authoritative GRE decision history):")
            lines.append(replay_context)

        user_message = "\n".join(lines)

        logger.info(
            f"[POST-MATCH] Generating analysis: {len(advice_history)} entries, "
            f"result={match_result}, turns={match_duration_turns}, "
            f"replay={'yes' if replay_context else 'no'}, "
            f"prompt={len(user_message)} chars"
        )

        # Scale timeout with prompt size — Opus on large prompts needs time
        api_timeout = 60
        if isinstance(be, ProxyBackend):
            api_timeout = 90
        if len(user_message) > 30000:
            api_timeout = max(api_timeout, 120)

        api_start = time.perf_counter()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

        # Try with max_tokens first, fall back to 2-arg call.
        import inspect
        sig = inspect.signature(be.complete)
        accepts_kwargs = len(sig.parameters) > 2 or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        if accepts_kwargs:
            submit_kwargs = (
                {"request_timeout_s": api_timeout}
                if isinstance(be, ProxyBackend)
                else {}
            )
            future = executor.submit(
                be.complete,
                POST_MATCH_ANALYSIS_PROMPT,
                user_message,
                4096,
                **submit_kwargs,
            )
        else:
            future = executor.submit(
                be.complete, POST_MATCH_ANALYSIS_PROMPT, user_message
            )
        try:
            response = future.result(timeout=api_timeout)
        except concurrent.futures.TimeoutError:
            logger.warning(f"Post-match analysis timed out after {api_timeout}s")
            response = ""
        executor.shutdown(wait=False)

        api_time = (time.perf_counter() - api_start) * 1000
        logger.info(f"[POST-MATCH] API: {api_time:.0f}ms, response: {len(response)} chars")

        if not response or response.startswith("Error"):
            return ""

        return response

    def recommend_sideboard(
        self,
        maindeck_cards: list[Any],
        sideboard_cards: list[Any],
        opponent_cards_seen: list[Any],
        game_history: Optional[list[dict[str, Any]]] = None,
        backend: Optional[Any] = None,
    ) -> Optional[str]:
        """Generate Best-of-Three (Bo3) sideboarding recommendations.

        Args:
            maindeck_cards: Maindeck card list (tuples, dicts, or strings)
            sideboard_cards: 15-card sideboard list (tuples, dicts, or strings)
            opponent_cards_seen: Opponent cards revealed in previous game(s)
            game_history: Match context (e.g. Game 1 result, turn count)
            backend: Optional backend override

        Returns:
            Recommended swaps and strategic reasoning, or None on failure.
        """
        import time

        be = backend or self._backend
        if not be:
            return None

        start = time.perf_counter()

        def _format_card_list(cards: list[Any]) -> str:
            if not cards:
                return "(None revealed or listed)"
            from collections import Counter
            counts = Counter()
            details: dict[str, tuple[str, str]] = {}

            for item in cards:
                if isinstance(item, tuple) and len(item) >= 2:
                    name = item[0]
                    card_type = item[1] if len(item) > 1 else ""
                    oracle = item[2] if len(item) > 2 else ""
                elif isinstance(item, dict):
                    name = item.get("name", "Unknown")
                    card_type = item.get("type_line", item.get("type", ""))
                    oracle = item.get("oracle_text", item.get("text", ""))
                elif isinstance(item, str):
                    name = item
                    card_type = ""
                    oracle = ""
                else:
                    name = str(item)
                    card_type = ""
                    oracle = ""

                counts[name] += 1
                if name not in details:
                    details[name] = (card_type, oracle)

            lines = []
            for name, count in counts.most_common():
                card_type, oracle = details.get(name, ("", ""))
                type_short = card_type.split("—")[0].strip() if card_type else ""
                line = f"{count}x {name}"
                if type_short:
                    line += f" ({type_short})"
                if oracle and "basic" not in card_type.lower():
                    short_oracle = self._remove_reminder_text(oracle).strip()
                    if short_oracle:
                        if len(short_oracle) > 100:
                            short_oracle = short_oracle[:97] + "..."
                        line += f" — {short_oracle}"
                lines.append(line)
            return "\n".join(lines)

        maindeck_text = _format_card_list(maindeck_cards)
        sideboard_text = _format_card_list(sideboard_cards)
        opp_text = _format_card_list(opponent_cards_seen)

        prompt_lines = [
            "BEST-OF-THREE (Bo3) MATCH SIDEBOARDING CONTEXT:",
        ]

        if game_history:
            history_parts = []
            for i, g in enumerate(game_history, 1):
                res = g.get("result", "unknown")
                turns = g.get("turns", "?")
                history_parts.append(f"Game {i}: {res} ({turns} turns)")
            prompt_lines.append(f"Match History: {', '.join(history_parts)}")

        prompt_lines.extend([
            f"\nPLAYER MAINDECK:\n{maindeck_text}",
            f"\nPLAYER SIDEBOARD:\n{sideboard_text}",
            f"\nOPPONENT CARDS SEEN:\n{opp_text}",
        ])

        user_message = "\n".join(prompt_lines)

        try:
            import inspect
            sig = inspect.signature(be.complete)
            accepts_kwargs = len(sig.parameters) > 2 or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            )
            if accepts_kwargs:
                rec = be.complete(SIDEBOARD_RECOMMENDATION_PROMPT, user_message, max_tokens=2048)
            else:
                rec = be.complete(SIDEBOARD_RECOMMENDATION_PROMPT, user_message)

            if not rec or rec.startswith("Error"):
                logger.warning(f"Sideboard recommendation failed: {rec[:80] if rec else 'empty'}")
                return None

            elapsed = (time.perf_counter() - start) * 1000
            logger.info(f"Sideboard recommendation complete: {elapsed:.0f}ms, {len(rec)} chars")
            return rec
        except Exception as e:
            logger.error(f"Sideboard recommendation error: {e}")
            return None

    def generate_win_probability(self, game_state: dict[str, Any],
                                  opponent_played_cards: list[dict] = None) -> str:
        """Estimate win probability based on current board state.

        Returns a short analysis with a win percentage and recommendation.
        If loss probability exceeds 75%, includes a concede suggestion.
        """
        be = self._backend
        if be is None:
            return ""

        context = self._build_context(game_state)

        system_prompt = (
            "You are an expert MTG analyst. Evaluate the current game state and estimate "
            "the probability that the local player wins this game.\n\n"
            "Consider:\n"
            "- Board presence: creature count, total power/toughness, keywords\n"
            "- Life totals and life trajectory\n"
            "- Cards in hand vs opponent's likely hand size\n"
            "- Mana development (lands in play)\n"
            "- Opponent's revealed cards and likely strategy\n"
            "- Tempo and card advantage\n"
            "- Whether the local player is the beatdown or the control\n\n"
            "Output format (STRICT — follow exactly):\n"
            "Line 1: WIN: XX% (a single integer 0-100)\n"
            "Line 2-3: Brief explanation (2 sentences max) of why.\n"
            "Line 4: If WIN is 25% or below, add: RECOMMEND: Concede — [1-sentence reason]\n\n"
            "Be realistic, not optimistic. A hopeless board is 5-15%, not 30%."
        )

        opp_cards_str = ""
        if opponent_played_cards:
            names = [c.get("name", "?") for c in opponent_played_cards if c.get("name")]
            if names:
                opp_cards_str = f"\nOpponent's revealed cards this game: {', '.join(names)}"

        user_message = f"{context}{opp_cards_str}\n\nEstimate win probability."

        import concurrent.futures
        api_timeout = 30
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        submit_kwargs = (
            {"request_timeout_s": api_timeout}
            if isinstance(be, ProxyBackend)
            else {}
        )
        future = executor.submit(
            be.complete, system_prompt, user_message, 1000, **submit_kwargs
        )
        try:
            response = future.result(timeout=api_timeout)
        except concurrent.futures.TimeoutError:
            logger.warning("Win probability timed out")
            response = ""
        executor.shutdown(wait=False)

        if not response or response.startswith("Error"):
            return ""

        logger.info(f"[WIN-PROB] {response[:100]}")
        return response

    def _postprocess_advice(self, advice: str, game_state: dict[str, Any], style: str = "quick") -> str:
        """Post-process LLM advice to fix common issues with smaller models.

        1. Strip markdown formatting (headers, bold, bullets) for spoken output
        2. Truncate overly long responses when style is concise
        3. Remove 'Play [Land]' suggestions when no land is in hand
        4. Fix typos in card names using fuzzy matching against the game state

        This is a band-aid layer over freeform LLM prose. The cleaner long-term
        fix is to switch the coach to structured JSON output (action + say
        fields) the way the autopilot planner does — that way the action gets
        validated against legal actions before TTS reads it, and most of these
        regex passes go away. Tracked separately; for now we keep the cleanup
        targeted and data-driven rather than hardcoded.
        """
        if not advice:
            return ""

        import re

        # 0a. Strip markdown formatting — this is spoken aloud, not rendered
        # Remove headers (# Header or ##Header — with or without space)
        advice = re.sub(r"^#{1,6}\s*", "", advice, flags=re.MULTILINE)
        # Remove bold/italic markers
        advice = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", advice)
        # Remove bullet markers at start of line (•, -, *)
        advice = re.sub(r"^\s*[•\-\*]\s+", "", advice, flags=re.MULTILINE)
        # Remove inline bullet characters (•)
        advice = advice.replace("•", "")
        # Collapse multiple newlines into single space
        advice = re.sub(r"\n+", " ", advice)
        # Clean up resulting whitespace
        advice = re.sub(r"\s+", " ", advice).strip()

        # 0b. Enforce style-specific length limits.
        # Quick: under 20 words (LLM sometimes ignores the prompt).
        # Chatty: under ~80 words so TTS stays under ~25 seconds.
        # Legacy names accepted: concise==quick, verbose==chatty.
        style_norm = (style or "").lower()
        if style_norm in ("quick", "concise"):
            word_cap, sent_cap = 22, 2
        elif style_norm in ("chatty", "verbose"):
            word_cap, sent_cap = 80, 5
        else:
            word_cap, sent_cap = 60, 4  # normal/default

        words = advice.split()
        if len(words) > word_cap + 5:  # small slack before truncating
            sentences = re.split(r'(?<=[.!?])\s+', advice)
            truncated = []
            count = 0
            for sent in sentences[:sent_cap]:
                sw = sent.split()
                if count + len(sw) > word_cap and truncated:
                    break
                truncated.append(sent)
                count += len(sw)
            advice = " ".join(truncated).strip()
            if advice and advice[-1] not in ".!?":
                advice += "."

        def _combat_attack_summary() -> Optional[tuple[int, int, int]]:
            """Return (attack_power, opp_life, opp_blockers) if computable."""
            turn = game_state.get("turn", {})
            turn_num = turn.get("turn_number", 0)
            phase = turn.get("phase", "")

            players = game_state.get("players", [])
            local_player = next((p for p in players if p.get("is_local")), None)
            if not local_player:
                return None
            local_seat = local_player.get("seat_id")
            opponent_player = next(
                (p for p in players if p.get("seat_id") != local_seat), None
            )
            if not opponent_player:
                return None

            if turn.get("active_player") != local_seat:
                return None
            if "Main" not in phase and "Combat" not in phase:
                return None

            battlefield = game_state.get("battlefield", [])
            your_creatures = [
                c
                for c in battlefield
                if c.get("owner_seat_id") == local_seat
                and "creature" in c.get("type_line", "").lower()
                and not self._is_impending(c)
            ]

            def _has_haste(card: dict[str, Any]) -> bool:
                return (
                    "haste"
                    in self._remove_reminder_text(card.get("oracle_text", "")).lower()
                )

            valid_attackers = [
                c
                for c in your_creatures
                if not c.get("is_tapped")
                and not (
                    c.get("turn_entered_battlefield") == turn_num and not _has_haste(c)
                )
            ]
            attack_power = sum(c.get("power") or 0 for c in valid_attackers)

            opp_creatures = [
                c
                for c in battlefield
                if c.get("owner_seat_id") != local_seat
                and "creature" in c.get("type_line", "").lower()
                and not self._is_impending(c)
            ]
            opp_blockers = len([c for c in opp_creatures if not c.get("is_tapped")])
            opp_life = opponent_player.get("life_total", 20)

            return attack_power, opp_life, opp_blockers

        # Get cards in hand
        hand_cards = game_state.get("hand", [])
        hand_names = {c.get("name", "").lower() for c in hand_cards}

        # Get all card names in game state for fuzzy matching
        all_cards = []
        for zone in ["hand", "battlefield", "graveyard", "stack", "exile"]:
            all_cards.extend(game_state.get(zone, []))
        all_card_names = {c.get("name", "") for c in all_cards if c.get("name")}

        # Build the set of land card names known to this game. Anything with
        # "Land" in its type_line counts — covers basics, snow, Triomes,
        # shocks, fetch, Cavern of Souls, etc. The hardcoded basic-only list
        # we used to keep here missed every modern land.
        known_land_names = {
            (c.get("name") or "").lower()
            for c in all_cards
            if "land" in (c.get("type_line") or "").lower() and c.get("name")
        }
        # Generic words the LLM might use even when no specific land card is
        # actually visible. Kept here (not in a regex literal) so the set is
        # easy to extend.
        generic_land_terms = {"a land", "any land", "land"}

        # Check whether any land sits in the local player's hand.
        local_seat = None
        for p in game_state.get("players", []):
            if p.get("is_local"):
                local_seat = p.get("seat_id")
                break
        lands_in_hand: set[str] = set()
        for c in hand_cards:
            type_line = (c.get("type_line") or "").lower()
            name = (c.get("name") or "").lower()
            if "land" in type_line and name:
                lands_in_hand.add(name)

        # 1. Remove "Play <X>" suggestions when there's no land in hand.
        # We match X against actual land card names from this game's state
        # plus a small set of generic land phrases. This is data-driven
        # instead of a hardcoded basic-lands list, so Triomes / shocks /
        # snow basics all get caught too.
        if not lands_in_hand:
            removable_land_names = known_land_names | generic_land_terms
            if removable_land_names:
                # Sort longest first so multi-word names match before substrings
                land_alternatives = sorted(removable_land_names, key=len, reverse=True)
                land_pattern = re.compile(
                    r"Play\s+(?:"
                    + "|".join(re.escape(n) for n in land_alternatives)
                    + r")[.,]?\s*",
                    flags=re.IGNORECASE,
                )
                advice = land_pattern.sub("", advice)
                # Clean up any resulting double spaces or leading/trailing spaces
                advice = re.sub(r"\s+", " ", advice).strip()

        # 2. Fix typos in card names by fuzzy-matching against the actual
        # card names in this game's state. The previous hardcoded
        # typo-fixes dict (Gemma 3N misreads of specific card names) is
        # gone — it never scaled past the handful of cards we'd seen
        # break, and the fuzzy pass below catches the same misspellings
        # generically as long as the real card is somewhere in state.
        # Split advice into words and check for near-matches
        for card_name in all_card_names:
            if len(card_name) < 4:
                continue  # Skip short names to avoid false matches
            # Check if card name appears with typos (simple Levenshtein-like check)
            card_words = card_name.lower().split()
            for word in card_words:
                if len(word) < 4:
                    continue
                # Look for similar words in advice
                advice_words = advice.lower().split()
                for i, advice_word in enumerate(advice_words):
                    if len(advice_word) >= 4 and self._is_similar(word, advice_word):
                        # Replace the typo with correct spelling
                        # Find the actual word in original advice and replace
                        original_words = advice.split()
                        if i < len(original_words):
                            # Only replace if first letter matches (to avoid false positives)
                            if original_words[i][0].lower() == word[0].lower():
                                original_words[i] = (
                                    word.capitalize()
                                    if original_words[i][0].isupper()
                                    else word
                                )
                                advice = " ".join(original_words)

        # 3. Remove Cast suggestions for cards that cost more mana than available
        # Calculate available mana (lands on battlefield + land drop potential)
        battlefield = game_state.get("battlefield", [])
        # local_seat already resolved above when computing land state.

        # Count untapped lands we control
        untapped_lands = 0
        for card in battlefield:
            if (
                card.get("controller_seat_id") == local_seat
                or card.get("owner_seat_id") == local_seat
            ):
                type_line = card.get("type_line", "").lower()
                if "land" in type_line and not card.get("is_tapped"):
                    untapped_lands += 1

        # Check if we have a land in hand (potential +1 mana)
        has_land_in_hand = lands_in_hand  # already computed above
        potential_mana = untapped_lands + (1 if has_land_in_hand else 0)

        # Check each card in hand for mana cost violations
        seen_card_names = set()
        for card in hand_cards:
            card_name = card.get("name", "")
            mana_cost = card.get("mana_cost", "")
            if card_name in seen_card_names:
                continue
            seen_card_names.add(card_name)
            if not card_name or not mana_cost:
                continue

            # Parse CMC from mana cost (simple heuristic)
            cmc = 0
            import re as re_inner

            # Count {X} symbols
            symbols = re_inner.findall(r"\{([^}]+)\}", mana_cost)
            for sym in symbols:
                if sym.isdigit():
                    cmc += int(sym)
                elif sym in ["W", "U", "B", "R", "G", "C"]:
                    cmc += 1
                elif "/" in sym:  # Hybrid like {R/G}
                    cmc += 1

            # If this card costs more than we can have, remove Cast suggestions for it
            if cmc > potential_mana:
                # Remove "then [cast] Card" sequences (e.g. "Play land then Earthbender Ascension")
                then_pattern = re.compile(
                    rf",?\s*then\s+(?:cast\s+)?{re.escape(card_name)}[.,]?\s*",
                    re.IGNORECASE,
                )
                if then_pattern.search(advice):
                    advice = then_pattern.sub(". ", advice).strip()
                    logger.debug(
                        f"Removed uncastable 'then' sequence: {card_name} (needs {cmc}, have {potential_mana})"
                    )
                    continue

                # Remove "Cast [Card Name]" as a standalone command (e.g. "Cast X." or "Cast X,")
                # but NOT when the card name appears mid-sentence (e.g. "find mana to cast X or Y")
                # to avoid leaving garbled text like "find mana to or Y"
                standalone_pattern = re.compile(
                    rf"(?:^|(?<=\.\s)|(?<=\n))Cast\s+{re.escape(card_name)}[.,]?\s*",
                    re.IGNORECASE,
                )
                if standalone_pattern.search(advice):
                    advice = standalone_pattern.sub("", advice)
                    logger.debug(
                        f"Removed uncastable suggestion: {card_name} (needs {cmc}, have {potential_mana})"
                    )
                else:
                    # Card mentioned mid-sentence — replace name with "[uncastable]" hint
                    # so the sentence stays grammatical
                    mid_pattern = re.compile(
                        rf"(?:cast\s+)?{re.escape(card_name)}", re.IGNORECASE
                    )
                    if mid_pattern.search(advice):
                        advice = mid_pattern.sub(
                            f"{card_name} (not enough mana)", advice, count=1
                        )
                        logger.debug(
                            f"Annotated uncastable mid-sentence: {card_name} (needs {cmc}, have {potential_mana})"
                        )

        # 4. Remove incorrect lethal/win claims when math doesn't support it
        if re.search(
            r"(?i)\blethal\b|\bfor the win\b|\bthat'?s the win\b|\bwin!\b", advice
        ):
            summary = _combat_attack_summary()
            if summary:
                attack_power, opp_life, opp_blockers = summary
                if opp_blockers > 0 or attack_power < opp_life:
                    advice = re.sub(r"(?i)\blethal\b", "damage", advice)
                    advice = re.sub(r"(?i)\bfor the win\b", "for damage", advice)
                    advice = re.sub(r"(?i)\bthat'?s the win\b", "", advice)
                    advice = re.sub(r"(?i)\bwin!\b", "", advice)
                    advice = advice.replace("lethal on board", "pressure on board")

        # Clean up double spaces
        advice = re.sub(r"\s+", " ", advice).strip()

        def _augment_legal_actions_from_decision_context(
            actions: list[str],
        ) -> list[str]:
            """Add high-signal combat actions from decision context when missing.

            RulesEngine legal actions can lag behind GRE decision context during
            declare-attack/block windows. In those states, prefer the concrete
            attacker/blocker sets from decision_context over generic activate/cast
            options so fallback advice remains action-appropriate.
            """
            augmented = list(actions)
            decision_context = game_state.get("decision_context") or {}
            dec_type = str(decision_context.get("type", "") or "").lower()

            if dec_type == "declare_attackers":
                legal_attackers = self._filter_legal_attacker_names(
                    game_state, decision_context.get("legal_attackers") or []
                )
                if legal_attackers:
                    attack_action = f"Declare Attackers: {', '.join(legal_attackers)}"
                    if all(a.lower() != attack_action.lower() for a in augmented):
                        augmented.append(attack_action)

            if dec_type == "declare_blockers":
                legal_blockers = decision_context.get("legal_blockers") or []
                if legal_blockers:
                    block_action = f"Block with: {', '.join(legal_blockers)}"
                    if all(a.lower() != block_action.lower() for a in augmented):
                        augmented.append(block_action)

            return augmented

        # 5. Enforce Legal actions only (hard filter)
        # MULLIGAN OVERRIDE: During mulligan, RulesEngine returns "Wait (Opponent
        # has priority)" because priority_player != local_seat. Override here just
        # like _format_game_context does (line ~1384).
        pending = game_state.get("pending_decision")
        if pending == "Mulligan":
            legal_actions = ["KEEP", "MULLIGAN"]
        elif pending == "Mulligan Bottom":
            # During bottom-card selection, any card name advice is valid
            legal_actions = []
        else:
            try:
                from arenamcp.rules_engine import RulesEngine

                legal_actions = RulesEngine.get_legal_actions(game_state) or []
                legal_actions = _augment_legal_actions_from_decision_context(legal_actions)
            except Exception as e:
                logger.warning(f"RulesEngine error in postprocess: {e}")
                legal_actions = []

        if legal_actions:

            def _score_action(action: str) -> int:
                """Heuristic score for legal actions (higher is better)."""
                score = 0
                act = action.lower()
                turn = game_state.get("turn", {})
                phase = turn.get("phase", "").lower()
                step = turn.get("step", "").lower()
                pending_decision = str(game_state.get("pending_decision", "") or "").lower()
                players = game_state.get("players", [])
                local_player = next((p for p in players if p.get("is_local")), None)
                local_seat = local_player.get("seat_id") if local_player else None

                # Prefer land drop if available
                if act.startswith("play land:"):
                    score += 80

                # Combat step priorities
                if (
                    "declare attackers" in act
                    and "combat" in phase
                    and "declareattack" in step
                ):
                    score += 90
                if "declare attackers" in act and "declare attackers" in pending_decision:
                    score += 120
                if "block with" in act and "combat" in phase and "declareblock" in step:
                    score += 120
                if "block with" in act and "declare blockers" in pending_decision:
                    score += 120

                # Strongly prefer actions confirmed castable by the game engine
                if "[ok]" in act:
                    score += 50

                # Casting is generally higher priority than activating
                if act.startswith("cast "):
                    if "[ok]" in act:
                        score += 60  # confirmed castable
                    else:
                        score += 10  # may not have mana — low priority
                if act.startswith("activate "):
                    score += 40
                if act.startswith("activate ") and (
                    ("combat" in phase and "declareblock" in step)
                    or ("declare blockers" in pending_decision)
                ):
                    # During blocker declaration, avoid replacing with activations.
                    score -= 100

                # During combat, "Pass" (the Next button) is usually correct
                # when no cast/play/declare actions are available
                if act == "pass" and "combat" in phase:
                    score += 10

                # Penalize mana-only actions (not real decisions)
                if act.startswith("action: "):
                    score -= 10

                # If we can detect a legal "Play Land" and lands available, boost it
                if "play land" in act and local_seat is not None:
                    # If a land is in hand, it's likely valid to play
                    hand = game_state.get("hand", [])
                    if any("land" in c.get("type_line", "").lower() for c in hand):
                        score += 15

                return score

            def _normalize_best_legal_action(action: str) -> str:
                """Normalize fallback combat actions against visible legality."""
                act_lower = action.lower()

                if act_lower.startswith("declare attackers:"):
                    names = [n.strip() for n in action.split(":", 1)[1].split(",") if n.strip()]
                    filtered_names = self._filter_legal_attacker_names(game_state, names)
                    if not filtered_names:
                        return "Don't attack"
                    return f"Declare Attackers: {', '.join(filtered_names)}"

                if act_lower.startswith("block with:"):
                    names = [n.strip() for n in action.split(":", 1)[1].split(",") if n.strip()]
                    if not names:
                        return "Don't block"

                return action

            def _get_legal_pass_action(actions: list[str]) -> Optional[str]:
                """Return the concrete legal Pass action when available."""
                for action in actions:
                    if action.strip().lower() == "pass":
                        return action
                return None

            def _has_pass_intent(text: str) -> bool:
                """Detect advice that means "do nothing now and let play proceed"."""
                lead_clause = re.split(r"(?<=[.!?;])\s+", text.strip(), maxsplit=1)[0].lower()
                pass_intent_patterns = (
                    r"\blet (?:it|that|this|them) resolve\b",
                    r"\bpass priority\b",
                    r"^\s*pass\b",
                    r"^\s*wait\b",
                    r"\bno response\b",
                    r"\bdon['’]?t respond\b",
                    r"\bdo not respond\b",
                    r"\blet them have it\b",
                    r"\bnothing to do\b",
                )
                return any(re.search(pattern, lead_clause) for pattern in pass_intent_patterns)

            advice_lower = advice.lower()
            legal_lower = [a.lower() for a in legal_actions]
            # Strip [OK], [NEED:x], [NO TARGETS] etc. markers before matching so
            # "Cast Northern Air Temple" matches "Cast Northern Air Temple [NEED:B]"
            legal_lower_stripped = [
                re.sub(r'\s*\[[^\]]+\]', '', a).strip()
                for a in legal_lower
            ]
            matches = (
                any(l in advice_lower for l in legal_lower)
                or any(l in advice_lower for l in legal_lower_stripped)
            )
            legal_pass_action = _get_legal_pass_action(legal_actions)

            if not matches and legal_pass_action and _has_pass_intent(advice):
                advice = legal_pass_action
                advice_lower = advice.lower()
                matches = True

            # "Don't attack", "don't block", "pass priority", "no attacks" are
            # always valid strategic choices — the player can decline to act.
            PASSTHROUGH_PHRASES = [
                "don't attack", "don\u2019t attack", "do not attack", "no attack",
                "don't block", "don\u2019t block", "do not block", "no block",
                "pass priority", "take the damage",
                "let it resolve", "let them resolve", "let that resolve",
                "wait", "no response", "don't respond", "don\u2019t respond",
                "nothing to do", "pass", "resolve",
            ]
            has_ok_actions = any(
                "[ok]" in act.lower() for act in legal_actions if not act.lower().startswith("pass")
            )
            false_no_mana_claim = any(
                claim in advice_lower for claim in [
                    "lack the mana", "lacks the mana", "don't have the mana", "dont have the mana",
                    "not enough mana", "no castable spells", "cannot cast any", "can't cast any",
                    "no legal spells", "no playable spells"
                ]
            )

            if not matches and any(p in advice_lower for p in PASSTHROUGH_PHRASES):
                if not (has_ok_actions and false_no_mana_claim):
                    matches = True

            # Enhanced advice matching for partial card names, generic attacks/blocks, activations
            if not matches:
                for act in legal_actions:
                    act_lower = act.lower()
                    act_clean = re.sub(r'\s*\[[^\]]+\]', '', act_lower).strip()
                    
                    # 1. Cast actions (e.g., "cast michelangelo, weirdness to 11")
                    if act_clean.startswith("cast "):
                        card_name = act_clean[5:].strip()
                        short_name = re.split(r'[,—/]', card_name)[0].strip()
                        if short_name and short_name in advice_lower:
                            matches = True
                            break
                    
                    # 2. Play land actions (e.g., "play land: forest")
                    elif act_clean.startswith("play land:"):
                        card_name = act_clean[10:].strip()
                        short_name = re.split(r'[,—/]', card_name)[0].strip()
                        if short_name and (short_name in advice_lower or "play land" in advice_lower or "play a land" in advice_lower):
                            matches = True
                            break
                    
                    # 3. Activate actions (e.g., "activate bristly bill, spine sower")
                    elif act_clean.startswith("activate "):
                        card_name = act_clean[9:].strip()
                        short_name = re.split(r'[,—/]', card_name)[0].strip()
                        if short_name and short_name in advice_lower:
                            matches = True
                            break
                    
                    # 4. Declare attackers (e.g., "declare attackers: bristly bill, spine sower...")
                    elif act_clean.startswith("declare attackers:"):
                        names_str = act_clean.split(":", 1)[1]
                        names = [n.strip() for n in re.split(r'[,#\d]', names_str) if n.strip()]
                        name_matched = any(name in advice_lower for name in names if len(name) > 2)
                        
                        is_negative = any(neg in advice_lower for neg in [
                            "don't", "dont", "do not", "no attack", "not attack", "hold back",
                            "never attack", "avoid attacking", "decline to attack"
                        ])
                        generic_attack = any(phrase in advice_lower for phrase in [
                            "attack with all", "attack with everything", "all attack",
                            "swing with all", "swing with everything", "attack all",
                            "swing all", "attack with everyone", "swing with everyone",
                            "all in", "all-in", "attack with all creatures",
                            "swing with all creatures", "all creatures attack",
                            "attack with your creatures", "swing with your creatures",
                            "attack with all of your", "swing with all of your",
                            "attack with all available", "swing with all available",
                            "attack with your team", "swing with your team",
                            "attack with the team", "swing with the team",
                            "attack!", "attack.", "swing!", "swing."
                        ]) or advice_lower.strip() in ("attack", "swing")
                        
                        if (name_matched or generic_attack) and not is_negative:
                            matches = True
                            break
                    
                    # 5. Block actions (e.g., "block with: ...")
                    elif act_clean.startswith("block with:"):
                        is_negative = any(neg in advice_lower for neg in [
                            "don't", "dont", "do not", "no block", "not block", "never block",
                            "avoid blocking", "decline to block", "no blocks"
                        ])
                        generic_block = any(phrase in advice_lower for phrase in [
                            "block with all", "block with everything", "all block",
                            "block all", "block with everyone", "block with all creatures",
                            "block with your creatures", "block with all available",
                            "block with your team", "block with the team",
                            "block!", "block."
                        ]) or advice_lower.strip() in ("block", "blocking")
                        
                        if generic_block and not is_negative:
                            matches = True
                            break

                    # 6. Done (confirm attackers) - matches if LLM recommends attacking
                    elif act_clean == "done (confirm attackers)":
                        is_negative = any(neg in advice_lower for neg in [
                            "don't", "dont", "do not", "no attack", "not attack", "hold back",
                            "never attack", "avoid attacking", "decline to attack"
                        ])
                        has_attack_intent = any(phrase in advice_lower for phrase in [
                            "attack", "swing", "lethal", "all in", "all-in", "combat"
                        ])
                        if has_attack_intent and not is_negative:
                            matches = True
                            break

                    # 7. Done (confirm blockers) - matches if LLM recommends blocking
                    elif act_clean == "done (confirm blockers)":
                        is_negative = any(neg in advice_lower for neg in [
                            "don't", "dont", "do not", "no block", "not block", "never block",
                            "avoid blocking", "decline to block", "no blocks"
                        ])
                        has_block_intent = any(phrase in advice_lower for phrase in [
                            "block", "chump", "trade"
                        ])
                        if has_block_intent and not is_negative:
                            matches = True
                            break

            if not matches and advice.lstrip().startswith("Error getting advice:"):
                # Transport/auth failure — NEVER mask it as coaching. On
                # 2026-07-16 an empty license key produced 435 silent 401s
                # while the fallback scorer replaced every response with a
                # plausible legal action; the user debugged "bad advice"
                # for hours when the truth was "the LLM never spoke once".
                low = advice.lower()
                if "401" in advice or "403" in advice or "virtual key" in low or "auth" in low:
                    advice = (
                        "No coaching available: the gateway rejected your "
                        "license key. Open the Repair tab and run Fix "
                        "Everything."
                    )
                else:
                    advice = (
                        "No coaching available: cannot reach the AI service "
                        "right now."
                    )
                logger.error(f"LLM failure surfaced to user (not masked): {advice}")
            elif not matches:
                # Force to best legal action to avoid illegal recommendations
                turn = game_state.get("turn", {})
                phase = str(turn.get("phase", "") or "").lower()
                step = str(turn.get("step", "") or "").lower()
                pending_decision = str(game_state.get("pending_decision", "") or "").lower()

                # Filter out [NO TARGETS] cards — casting them wastes the card.
                # Recompute from game state: spells needing "target creature you
                # control" when we have no creatures (Sagas exempt).
                _no_target_names: set[str] = set()
                _hand = game_state.get("hand", [])
                _bf = game_state.get("battlefield", [])
                _lp = next((p for p in game_state.get("players", []) if p.get("is_local")), None)
                _ls = _lp.get("seat_id") if _lp else None
                _my_creatures = [c for c in _bf
                                 if c.get("owner_seat_id") == _ls
                                 and c.get("power") is not None
                                 and "land" not in c.get("type_line", "").lower()]
                if not _my_creatures:
                    for _hc in _hand:
                        _oracle = (_hc.get("oracle_text") or "").lower()
                        _tl = (_hc.get("type_line") or "").lower()
                        if ("land" not in _tl and "creature" not in _tl
                                and "saga" not in _tl):
                            if ("target creature you control" in _oracle
                                    or "creature you control fights" in _oracle):
                                _hname = _hc.get("name")
                                if _hname:
                                    _no_target_names.add(_hname)

                # Build candidate pool excluding [NO TARGETS] cards
                if _no_target_names:
                    _candidates = [
                        a for a in legal_actions
                        if not any(f"Cast {nt}".lower() in a.lower() for nt in _no_target_names)
                    ]
                else:
                    _candidates = legal_actions
                if not _candidates:
                    _candidates = legal_actions  # fallback to unfiltered

                in_declare_blockers = (
                    ("combat" in phase and "declareblock" in step)
                    or ("declare blockers" in pending_decision)
                )
                if in_declare_blockers:
                    blocker_actions = [
                        a for a in _candidates if a.lower().startswith("block with:")
                    ]
                    if blocker_actions:
                        best = max(blocker_actions, key=_score_action)
                    else:
                        best = max(_candidates, key=_score_action)
                else:
                    if legal_pass_action and ("need:" in advice.lower() or "[need:" in advice.lower()):
                        best = legal_pass_action
                    else:
                        best = max(_candidates, key=_score_action)
                best = _normalize_best_legal_action(best)
                logger.info(f"Replaced illegal advice with legal action: {best} (original: {advice[:80]})")
                advice = best
        else:
            # No legal_actions reported. For passable idle windows this
            # means "pass priority" — but for SelectTargets/Search/Modal/
            # PayCosts the LLM's targeted answer is the best signal we
            # have (RulesEngine can't enumerate candidates for these).
            # Keep the model's advice unless it's clearly useless; only
            # then fall back to the context-appropriate manual prompt.
            req_class = str(game_state.get("_bridge_request_class") or "")
            req_type = str(game_state.get("_bridge_request_type") or "")
            non_passable = (
                req_class in _NON_PASSABLE_REQUEST_CLASSES
                or req_type in _NON_PASSABLE_REQUEST_TYPES
                or game_state.get("_bridge_can_pass") is False
            )
            stripped = (advice or "").strip()
            looks_useful = (
                bool(stripped)
                and len(stripped) >= 3
                and "pass priority" not in stripped.lower()
                and "pass" not in stripped.lower().split()[:1]
            )
            if non_passable and looks_useful:
                # Trust the LLM's targeted advice on non-passable requests.
                pass
            else:
                advice = _fallback_non_action_advice(game_state)

        # Clean up internal action format for spoken output:
        # "Play Land: Plains" → "Play Plains"
        advice = re.sub(r"(?i)^Play Land:\s*", "Play ", advice)
        advice = re.sub(r"(?i)Play Land:\s*", "Play ", advice)
        if str(game_state.get("pending_decision", "") or "").lower() == "declare attackers":
            advice = re.sub(r"(?i)^Done \(confirm attackers\)$", "Don't attack", advice)
        if str(game_state.get("pending_decision", "") or "").lower() == "declare blockers":
            advice = re.sub(r"(?i)^Done \(confirm blockers\)$", "Don't block", advice)

        # Sequence validator: If advice says "Play [land] then cast [spell]" or "Play [land] and cast [spell]"
        # but [spell] is illegal and not in post-land THEN options, strip the illegal spell clause.
        if " then cast " in advice.lower() or " and cast " in advice.lower():
            match_seq = re.search(r"(?i)^(Play\s+[\w\s'—]+?)(?:\s+(?:then|and)\s+cast\s+([\w\s'—]+))$", advice.strip())
            if match_seq:
                land_part = match_seq.group(1).strip()
                spell_part = match_seq.group(2).strip()
                spell_short = re.split(r'[,—/]', spell_part)[0].strip().lower()
                
                spell_is_legal = any(
                    spell_short in act.lower() for act in legal_actions if act.lower().startswith("cast ")
                )
                then_lines = [l for l in legal_actions if l.startswith("THEN:")]
                if not then_lines and isinstance(game_state, dict):
                    prompt_lines = game_state.get("_last_prompt_lines", [])
                    then_lines = [l for l in prompt_lines if l.startswith("THEN:")]

                spell_in_then = any(spell_short in tl.lower() for tl in then_lines)

                if not spell_is_legal and not spell_in_then:
                    logger.info(f"Stripped illegal post-land spell '{spell_part}' from advice '{advice}' -> '{land_part}'")
                    advice = land_part

        # 6. Block advice must name the attacker. "Block with Veteran Survivor"
        # is useless with multiple attackers on board (issue #420) — repair it
        # with the deterministic solver's assignment so the spoken line is
        # always actionable.
        advice = self._ensure_block_advice_names_attacker(advice, game_state)

        return advice

    _NEGATIVE_BLOCK_PHRASES = (
        "don't block", "don’t block", "do not block", "no block",
        "take the damage", "take the hit",
    )

    def _collect_block_decision_blockers(
        self, game_state: dict[str, Any]
    ) -> list[dict]:
        """Resolve the legal blockers for the current block decision.

        Prefers the GRE-authoritative ``legal_blocker_ids`` from the
        decision context; falls back to our untapped creatures.
        """
        battlefield = game_state.get("battlefield", []) or []
        ctx = game_state.get("decision_context") or {}
        ids: set[int] = set()
        if str(ctx.get("type") or "") == "declare_blockers":
            for bid in ctx.get("legal_blocker_ids") or []:
                try:
                    ids.add(int(bid))
                except (TypeError, ValueError):
                    continue
        if ids:
            cards = [
                c for c in battlefield
                if int(c.get("instance_id") or 0) in ids
            ]
            if cards:
                return cards
        local_seat = None
        for p in game_state.get("players", []) or []:
            if p.get("is_local"):
                local_seat = p.get("seat_id")
                break
        return [
            c for c in battlefield
            if c.get("owner_seat_id") == local_seat
            and "creature" in (c.get("type_line") or "").lower()
            and not c.get("is_tapped")
            and not self._is_impending(c)
        ]

    @staticmethod
    def _spoken_name_map(cards: list[dict]) -> dict[int, str]:
        """instance_id -> plain spoken name, ``#N``-deduped for duplicates."""
        names = [c.get("name") or "?" for c in cards]
        counts = Counter(names)
        seen: dict[str, int] = {}
        out: dict[int, str] = {}
        for c, n in zip(cards, names):
            label = n
            if counts[n] > 1:
                seen[n] = seen.get(n, 0) + 1
                label = f"{n} #{seen[n]}"
            out[int(c.get("instance_id") or 0)] = label
        return out

    def _solver_block_assignment_sentence(
        self,
        game_state: dict[str, Any],
        attackers: list[dict],
        blockers: list[dict],
        advice: str,
    ) -> str:
        """Deterministic "block A with X; block B with Y" sentence.

        Uses combat_solver.optimal_blocks (honoring the GRE per-blocker
        candidate restrictions when available). When the solver prefers no
        blocks but the advice insists on blocking, points the mentioned (or
        first) blocker at the biggest attacker it can legally block so the
        spoken line stays actionable.
        """
        if not attackers or not blockers:
            return ""
        try:
            from arenamcp.combat_solver import (
                blocker_allowed_attackers_map,
                optimal_blocks,
            )
        except Exception:
            return ""
        players = game_state.get("players", []) or []
        local_player = next((p for p in players if p.get("is_local")), None)
        your_life = local_player.get("life_total", 20) if local_player else 20
        ctx = game_state.get("decision_context") or {}
        allowed_map = blocker_allowed_attackers_map(ctx.get("raw_blockers") or [])

        atk_names = self._spoken_name_map(attackers)
        blk_names = self._spoken_name_map(blockers)

        # When the advice already names specific blockers, repair THOSE lines
        # (the LLM may have a reason the solver can't see — Calculator+Coach:
        # the solver supplies the missing attacker, it doesn't override the
        # pick). Fall back to the full blocker pool if that yields nothing.
        advice_lower = advice.lower()
        mentioned = [
            b for b in blockers
            if (b.get("name") or "").lower()
            and (b.get("name") or "").lower() in advice_lower
        ]
        candidate_pools: list[list[dict]] = []
        if mentioned:
            candidate_pools.append(mentioned)
        candidate_pools.append(blockers)

        assignments: dict[int, int] = {}
        for pool in candidate_pools:
            try:
                plan = optimal_blocks(
                    attackers, pool, your_life,
                    blocker_allowed_attackers=allowed_map or None,
                )
            except Exception as e:
                logger.debug(f"combat solver (block repair) failed: {e}")
                continue
            if plan is not None and plan.assignments:
                assignments = plan.assignments
                break

        clauses: list[str] = []
        if assignments:
            by_attacker: dict[int, list[int]] = {}
            for bid, aid in assignments.items():
                by_attacker.setdefault(int(aid), []).append(int(bid))
            for aid in sorted(by_attacker):
                a_label = atk_names.get(aid)
                b_labels = [
                    blk_names.get(bid, f"creature {bid}")
                    for bid in sorted(by_attacker[aid])
                ]
                if a_label:
                    clauses.append(f"block {a_label} with {' and '.join(b_labels)}")
        else:
            # Solver says no blocks, but the advice recommends blocking —
            # keep the line actionable: aim the mentioned (or first) blocker
            # at the biggest attacker it can legally block.
            for b in (mentioned or blockers[:1]):
                bid = int(b.get("instance_id") or 0)
                allowed = allowed_map.get(bid) if allowed_map else None
                candidates = []
                for a in attackers:
                    aid = int(a.get("instance_id") or 0)
                    if allowed is not None and aid not in allowed:
                        continue
                    if self._compute_combat_trade(a, b) is None:
                        continue  # can't legally block it (e.g. flying)
                    candidates.append(a)
                if not candidates:
                    continue
                biggest = max(candidates, key=lambda c: c.get("power") or 0)
                aid = int(biggest.get("instance_id") or 0)
                a_label = atk_names.get(aid)
                if a_label:
                    clauses.append(
                        f"block {a_label} with {blk_names.get(bid, b.get('name', '?'))}"
                    )
        if not clauses:
            return ""
        return "Assignment: " + "; ".join(clauses) + "."

    def _ensure_block_advice_names_attacker(
        self, advice: str, game_state: dict[str, Any]
    ) -> str:
        """Repair DeclareBlockers advice that names a blocker but no attacker.

        "Block with Veteran Survivor" is useless when multiple creatures are
        attacking (issue #420, first Mac match). If the advice recommends
        blocking but names no attacker from the current combat, append the
        deterministic solver's attacker->blocker assignment so the spoken
        line is always actionable. Negative advice ("don't block") and advice
        that already names an attacker pass through untouched.
        """
        if not advice:
            return advice
        pending = str(game_state.get("pending_decision") or "").lower()
        ctx = game_state.get("decision_context") or {}
        if (
            pending != "declare blockers"
            and str(ctx.get("type") or "") != "declare_blockers"
        ):
            return advice

        import re

        advice_lower = advice.lower()
        if any(p in advice_lower for p in self._NEGATIVE_BLOCK_PHRASES):
            return advice

        attackers = self._collect_block_decision_attackers(game_state)
        if not attackers:
            return advice

        # Already names an attacker? Full-name match, or any distinctive
        # (len >= 4) word from an attacker's name appearing in the advice.
        advice_words = set(re.findall(r"[a-z'’]+", advice_lower))
        for atk in attackers:
            name = (atk.get("name") or "").lower()
            if not name:
                continue
            if name in advice_lower:
                return advice
            for word in re.findall(r"[a-z'’]+", name):
                if len(word) >= 4 and word in advice_words:
                    return advice

        # Only repair advice that is actually recommending a block.
        blockers = self._collect_block_decision_blockers(game_state)
        blocker_named = any(
            (b.get("name") or "").lower() in advice_lower
            for b in blockers
            if b.get("name")
        )
        if "block" not in advice_lower and not blocker_named:
            return advice

        assignment = self._solver_block_assignment_sentence(
            game_state, attackers, blockers, advice
        )
        if not assignment:
            return advice
        base = advice.rstrip()
        if base and base[-1] not in ".!?":
            base += "."
        logger.info(
            "Block advice named no attacker; appended solver assignment: %s",
            assignment,
        )
        return f"{base} {assignment}"

    def _is_similar(self, a: str, b: str, threshold: float = 0.7) -> bool:
        """Check if two strings are similar using simple character overlap."""
        if a == b:
            return True
        if abs(len(a) - len(b)) > 3:
            return False
        # Count matching characters
        matches = sum(1 for c1, c2 in zip(a.lower(), b.lower()) if c1 == c2)
        similarity = matches / max(len(a), len(b))
        return similarity >= threshold

    def complete_with_image(
        self, system_prompt: str, user_message: str, image_data: bytes
    ) -> str:
        """Call complete_with_image on backend if supported."""
        if hasattr(self._backend, "complete_with_image"):
            return self._backend.complete_with_image(
                system_prompt, user_message, image_data
            )
        logger.error(
            f"Backend {type(self._backend).__name__} does not support complete_with_image"
        )
        return "Image analysis not supported by current backend."


class GameStateTrigger:
    """Detects trigger conditions by comparing game states."""

    # Tier list of dangerous cards that warrant immediate warning
    # Format: card_name -> brief description of the threat
    THREAT_CARDS = {
        # Board wipes
        "Wrath of God": "Board wipe! Destroys all creatures.",
        "Damnation": "Board wipe! Destroys all creatures.",
        "Farewell": "Exiles ALL permanents of chosen types!",
        "Sunfall": "Exiles all creatures, makes a big token.",
        "Depopulate": "Board wipe, draws if you have multicolor.",
        "Temporary Lockdown": "Exiles all permanents MV 2 or less!",
        "Meticulous Archive": "Can find board wipes or removal.",
        # Combo pieces / Must-answer threats
        "Sheoldred, the Apocalypse": "Drains 2 on your draws, heals on theirs!",
        "Atraxa, Grand Unifier": "Draws 10+ cards on ETB, lifelink flyer.",
        "Raffine, Scheming Seer": "Grows attackers and filters cards.",
        "The Wandering Emperor": "Flash! Can exile or make blockers anytime.",
        "Teferi, Time Raveler": "Shuts off your instant-speed plays!",
        "Narset, Parter of Veils": "You can only draw 1 card per turn!",
        "Omnath, Locus of Creation": "Massive value engine, gains life.",
        "Vorinclex, Voice of Hunger": "Doubles their counters, halves yours.",
        # Powerful planeswalkers
        "Oko, Thief of Crowns": "Elks your best creatures!",
        "Karn, the Great Creator": "Shuts off artifacts, grabs from sideboard.",
        "Wrenn and Six": "Recurring lands and pinging creatures.",
        # Lock pieces
        "Drannith Magistrate": "You can't cast from graveyard/exile!",
        "Archon of Emeria": "Only 1 spell per turn, lands ETB tapped.",
        "Thalia, Guardian of Thraben": "Noncreature spells cost 1 more.",
        "Authority of the Consuls": "Your creatures ETB tapped.",
        "High Noon": "Only 1 spell per turn for everyone.",
        # Removal magnets
        "Questing Beast": "Can't be chumped, damages walkers!",
        "Elder Gargaroth": "Massive value every combat.",
        "Cruelty of Gix": "3-mode saga, steals creatures!",
        # Enchantment threats
        "Monument to Endurance": "Grows huge with counters, gains deathtouch + indestructible!",
    }

    def __init__(self, life_threshold: int = 5):
        """Initialize trigger detector.

        Args:
            life_threshold: Life total below which "low_life" triggers (default: 5)
        """
        self.life_threshold = life_threshold
        # Track threats we've already warned about (by instance_id)
        self._seen_threats: set[int] = set()
        # Track whether we've already fired the losing_badly trigger this game
        self._losing_badly_fired = False
        self._last_threat: Optional[dict] = None

    def _get_local_player(self, state: dict[str, Any]) -> Optional[dict]:
        """Get the local player dict from game state."""
        for p in state.get("players", []):
            if p.get("is_local"):
                return p
        return None

    def _get_opponent_player(self, state: dict[str, Any]) -> Optional[dict]:
        """Get the opponent player dict from game state."""
        for p in state.get("players", []):
            if not p.get("is_local"):
                return p
        return None

    def _has_castable_instants(self, state: dict[str, Any]) -> bool:
        """Check if player has any instant-speed cards they can cast.

        Returns True if hand contains instants or flash cards that can be
        cast with the current available mana.
        """
        import re

        # Count untapped lands for mana
        local_seat = None
        for p in state.get("players", []):
            if p.get("is_local"):
                local_seat = p.get("seat_id")
                break

        if local_seat is None:
            return False

        battlefield = state.get("battlefield", [])
        untapped_lands = sum(
            1
            for c in battlefield
            if c.get("owner_seat_id") == local_seat
            and "land" in c.get("type_line", "").lower()
            and not c.get("is_tapped")
        )

        # Check hand for castable instants/flash
        hand = state.get("hand", [])
        for card in hand:
            type_line = card.get("type_line", "").lower()
            oracle_text = card.get("oracle_text", "").lower()

            # Check if instant speed
            is_instant_speed = "instant" in type_line or "flash" in oracle_text
            if not is_instant_speed:
                continue

            # Calculate CMC
            cost = card.get("mana_cost", "")
            cmc = 0
            if cost:
                generic = re.findall(r"\{(\d+)\}", cost)
                cmc += sum(int(g) for g in generic)
                colored = re.findall(r"\{[WUBRGC]\}", cost)
                cmc += len(colored)
                hybrid = re.findall(r"\{[^}]+/[^}]+\}", cost)
                cmc += len(hybrid)

            if untapped_lands >= cmc:
                return True

        return False

    def check_triggers(
        self, prev_state: dict[str, Any], curr_state: dict[str, Any]
    ) -> list[str]:
        """Compare two game states and return triggered condition names.

        Args:
            prev_state: Previous game state dict
            curr_state: Current game state dict

        Returns:
            List of trigger names that fired (may be empty)
        """
        triggers = []

        prev_turn = prev_state.get("turn", {})
        curr_turn = curr_state.get("turn", {})

        # Retrieve phase and step early (fix scoping issues)
        curr_phase = curr_turn.get("phase", "")
        curr_step = curr_turn.get("step", "")

        # Get local player info first (needed for turn detection)
        prev_local = self._get_local_player(prev_state)
        curr_local = self._get_local_player(curr_state)
        local_seat = curr_local.get("seat_id") if curr_local else None

        # FIRST CONNECTION: If prev_state has no turn info but curr_state does,
        # we just connected mid-game. Fire a trigger to give immediate advice.
        prev_turn_num = prev_turn.get("turn_number", 0)
        curr_turn_num = curr_turn.get("turn_number", 0)
        curr_active = curr_turn.get("active_player", 0)

        first_connection = prev_turn_num == 0 and curr_turn_num > 0
        if first_connection:
            # Just connected to an active game
            is_your_turn = curr_active == local_seat
            if is_your_turn:
                logger.info(
                    f"First connection mid-game, triggering new_turn (turn {curr_turn_num})"
                )
                triggers.append("new_turn")
            # Also check for pending decision on first connection
            pending = curr_state.get("pending_decision")
            if pending:
                logger.info(f"First connection with pending decision: {pending}")
                triggers.append("decision_required")

        # New turn detection. Skip on first connection — that path above already
        # owns the new_turn decision (gated on whether it's the local player's
        # turn); without this guard a first-connection-on-your-turn fires
        # new_turn twice for the same turn.
        if curr_turn_num > prev_turn_num and not first_connection:
            triggers.append("new_turn")

        # Check if it's your turn or opponent's turn
        is_your_turn = curr_active == local_seat

        # Priority gained - trigger when priority shifts to you
        prev_priority = prev_turn.get("priority_player", 0)
        curr_priority = curr_turn.get("priority_player", 0)
        if local_seat and curr_priority == local_seat and prev_priority != local_seat:
            # Always trigger on your turn
            # On opponent's turn, trigger if:
            #   1. You have castable instants
            #   2. There's something on the stack to consider
            #   3. We're in a significant phase (combat, main)
            has_options = self._has_castable_instants(curr_state)
            has_stack = len(curr_state.get("stack", [])) > 0
            # Retrieve phase and step early
            curr_phase = curr_turn.get("phase", "")
            curr_step = curr_turn.get("step", "")

            if (
                is_your_turn
                or has_options
                or has_stack
                or (any(p in curr_phase for p in ["Main", "Combat", "Beginning"]))
            ):
                triggers.append("priority_gained")

        # --- Detect land_played and spell_resolved EARLY ---
        # These must run before the legal_actions decision_required check
        # so the suppression at line ~3445 can see them and avoid firing
        # a duplicate decision_required that contradicts multi-step advice.
        prev_stack = prev_state.get("stack", [])
        curr_stack = curr_state.get("stack", [])

        # Land played detection - only on your turn, only in main phases
        if is_your_turn and "Main" in curr_phase:
            prev_battlefield = prev_state.get("battlefield", [])
            curr_battlefield = curr_state.get("battlefield", [])

            prev_land_count = sum(
                1
                for obj in prev_battlefield
                if obj.get("owner_seat_id") == local_seat
                and "land" in obj.get("type_line", "").lower()
            )
            curr_land_count = sum(
                1
                for obj in curr_battlefield
                if obj.get("owner_seat_id") == local_seat
                and "land" in obj.get("type_line", "").lower()
            )

            if curr_land_count > prev_land_count:
                logger.info(
                    f"Land played trigger: {prev_land_count} -> {curr_land_count}"
                )
                triggers.append("land_played")

        # Spell resolved detection - your spell left the stack on your turn
        if is_your_turn and len(curr_stack) < len(prev_stack):
            prev_your_spells = [
                s for s in prev_stack if s.get("owner_seat_id") == local_seat
            ]
            curr_your_spells = [
                s for s in curr_stack if s.get("owner_seat_id") == local_seat
            ]
            if len(curr_your_spells) < len(prev_your_spells):
                logger.info("Spell resolved trigger: your spell left the stack")
                triggers.append("spell_resolved")

            if (
                len(curr_stack) == 0
                and "spell_resolved" not in triggers
                and "Main" in curr_phase
            ):
                logger.info(
                    "Stack cleared trigger: opponent spell/ability resolved on your main phase"
                )
                triggers.append("spell_resolved")

        # Check explicit pending decisions (like Mulligan) or legal action changes
        pending_decision = curr_state.get("pending_decision")
        legal_actions = curr_state.get("legal_actions", [])
        prev_legal = prev_state.get("legal_actions", [])

        # Trigger if decision label changed OR if we got a new list of legal actions from GRE
        if pending_decision and pending_decision != prev_state.get("pending_decision"):
            logger.info(f"Triggering decision: {pending_decision}")
            triggers.append("decision_required")
        elif legal_actions and legal_actions != prev_legal:
            # Don't re-trigger decision_required when the legal actions changed
            # because we just played a land or resolved a spell — those have
            # their own triggers and we already gave advice for the turn.
            if "decision_required" not in triggers and "land_played" not in triggers and "spell_resolved" not in triggers:
                logger.info(f"Triggering decision due to legal_actions update: {legal_actions}")
                triggers.append("decision_required")
        elif pending_decision in ("Mulligan", "Mulligan Bottom"):
                # Mulligan re-fire cases:
                # 1. Hand wasn't populated yet (SubmitDeckReq before GameState)
                # 2. Player chose to mulligan → new hand dealt (same decision
                #    label "Mulligan" but different hand contents/count)
                prev_hand = prev_state.get("hand", [])
                curr_hand = curr_state.get("hand", [])
                prev_hand_ids = {c.get("instance_id") for c in prev_hand}
                curr_hand_ids = {c.get("instance_id") for c in curr_hand}
                hand_changed = curr_hand_ids != prev_hand_ids
                if curr_hand and (not prev_hand or hand_changed):
                    logger.info(
                        f"Re-triggering Mulligan decision "
                        f"(hand {'appeared' if not prev_hand else 'changed'}: "
                        f"{len(curr_hand)} cards)"
                    )
                    triggers.append("decision_required")

        # Combat phase detection - use pending steps to catch fast combat phases
        pending_steps = curr_turn.get("pending_combat_steps", [])

        # P2-10: queued steps drain one poll late — once the snapshot phase
        # has moved PAST combat (Main2/Ending) the trigger's advice is
        # unsatisfiable before the LLM call is even made (2 guaranteed-wasted
        # calls, 4.7s + 1.9s, on 2026-07-05).
        if pending_steps and any(
            marker in curr_phase for marker in ("Main2", "Second", "Ending", "End")
        ):
            logger.debug(
                f"Dropping {len(pending_steps)} queued combat step(s) — "
                f"phase already {curr_phase}"
            )
            pending_steps = []

        for step_info in pending_steps:
            step = step_info.get("step", "")
            step_active = step_info.get("active_player", 0)
            step_is_your_turn = step_active == local_seat

            logger.debug(
                f"Processing pending combat step: {step}, active={step_active}, step_is_your_turn={step_is_your_turn}, current_is_your_turn={is_your_turn}"
            )

            # Double-check both the step's active player AND current turn state
            # This prevents stale pending steps from firing triggers after turn changes
            if "DeclareAttack" in step and step_is_your_turn and is_your_turn:
                if "combat_attackers" not in triggers:
                    logger.info(f"Combat attackers trigger from pending: {step}")
                    triggers.append("combat_attackers")
            elif "DeclareBlock" in step and not step_is_your_turn and not is_your_turn:
                if "combat_blockers" not in triggers:
                    logger.info(f"Combat blockers trigger from pending: {step}")
                    triggers.append("combat_blockers")

        # Also check current step (in case we're still in combat)
        # curr_phase and curr_step are already defined above

        if "Combat" in curr_phase:
            prev_step = prev_turn.get("step", "")
            # Only trigger on STEP CHANGE to avoid spamming every polling cycle
            if curr_step != prev_step:
                if (
                    "DeclareAttack" in curr_step
                    and is_your_turn
                    and "combat_attackers" not in triggers
                ):
                    logger.info(f"Combat attackers trigger: step={curr_step}")
                    triggers.append("combat_attackers")
                elif (
                    "DeclareBlock" in curr_step
                    and not is_your_turn
                    and "combat_blockers" not in triggers
                ):
                    logger.info(f"Combat blockers trigger: step={curr_step}")
                    triggers.append("combat_blockers")

        # Low life detection - always important
        if curr_local:
            curr_life = curr_local.get("life_total", 20)
            prev_life = prev_local.get("life_total", 20) if prev_local else 20
            if curr_life < self.life_threshold and prev_life >= self.life_threshold:
                triggers.append("low_life")

        # Opponent low life detection - always important
        prev_opp = self._get_opponent_player(prev_state)
        curr_opp = self._get_opponent_player(curr_state)
        if curr_opp:
            curr_opp_life = curr_opp.get("life_total", 20)
            prev_opp_life = prev_opp.get("life_total", 20) if prev_opp else 20
            if (
                curr_opp_life < self.life_threshold
                and prev_opp_life >= self.life_threshold
            ):
                triggers.append("opponent_low_life")

        # Stack spell detection - differentiate between your spells and opponent's
        if len(curr_stack) > len(prev_stack):
            # Check who owns the newest spell on the stack
            newest_spell = curr_stack[-1] if curr_stack else None
            if newest_spell:
                spell_owner = newest_spell.get("owner_seat_id")
                if spell_owner == local_seat:
                    triggers.append("stack_spell_yours")
                else:
                    triggers.append("stack_spell_opponent")

        # NOTE: land_played and spell_resolved are detected earlier (before
        # the legal_actions check) so that decision_required suppression works.

        # THREAT DETECTION - warn about dangerous opponent cards
        opp_seat = curr_opp.get("seat_id") if curr_opp else None
        if opp_seat:
            curr_battlefield = curr_state.get("battlefield", [])
            for card in curr_battlefield:
                # Only check opponent's permanents
                controller = card.get("controller_seat_id") or card.get("owner_seat_id")
                if controller != opp_seat:
                    continue

                instance_id = card.get("instance_id")
                card_name = card.get("name", "")

                # Check if this is a threat card we haven't warned about
                if (
                    card_name in self.THREAT_CARDS
                    and instance_id not in self._seen_threats
                ):
                    self._seen_threats.add(instance_id)
                    # Store threat info for the standalone coach to retrieve
                    self._last_threat = {
                        "name": card_name,
                        "warning": self.THREAT_CARDS[card_name],
                        "card": {
                            "name": card.get("name"),
                            "type_line": card.get("type_line"),
                            "oracle_text": card.get("oracle_text"),
                            "power": card.get("power"),
                            "toughness": card.get("toughness"),
                            "mana_cost": card.get("mana_cost"),
                            "counters": card.get("counters"),
                        },
                    }
                    logger.info(
                        f"Threat detected: {card_name} - {self.THREAT_CARDS[card_name]}"
                    )
                    triggers.append("threat_detected")

                # Generic planeswalker detection fallback
                elif (
                    card_name not in self.THREAT_CARDS
                    and "planeswalker" in card.get("type_line", "").lower()
                    and instance_id not in self._seen_threats
                ):
                    self._seen_threats.add(instance_id)
                    self._last_threat = {
                        "name": card_name,
                        "warning": f"Opponent played planeswalker {card_name} — generates value every turn, consider attacking it.",
                        "card": {
                            "name": card.get("name"),
                            "type_line": card.get("type_line"),
                            "oracle_text": card.get("oracle_text"),
                            "power": card.get("power"),
                            "toughness": card.get("toughness"),
                            "mana_cost": card.get("mana_cost"),
                            "counters": card.get("counters"),
                        },
                    }
                    logger.info(f"Threat detected (planeswalker): {card_name}")
                    triggers.append("threat_detected")

        # LOSING BADLY detection — proactive concede suggestion
        # Fires once per game when multiple signals indicate a hopeless position.
        # Only check on new turns to avoid spamming during combat math.
        # Skip entirely if the match has ended or the bridge reports an
        # intermission state — otherwise a resumed coach process will
        # re-trigger losing_badly against post-match state and fire the
        # win-probability LLM call well after the user lost.
        pending_decision = str(curr_state.get("pending_decision") or "")
        bridge_req_type = str(curr_state.get("_bridge_request_type") or "")
        # _bridge_in_intermission is the durable signal — the bridge
        # zeroes _bridge_request_type when it sees an Intermission request,
        # so checking startswith("Intermission") on its own is dead code.
        match_in_intermission = (
            pending_decision.lower() == "intermission"
            or bridge_req_type.startswith("Intermission")
            or bool(curr_state.get("_bridge_in_intermission"))
            or bool(curr_state.get("match_ended"))
        )

        if (
            not self._losing_badly_fired
            and not match_in_intermission
            and curr_local and curr_opp
            and curr_turn_num >= 4  # too early to judge before turn 4
            and "new_turn" in triggers
        ):
            your_life = curr_local.get("life_total", 20)
            opp_life = curr_opp.get("life_total", 20)
            curr_bf = curr_state.get("battlefield", [])
            your_creatures = [
                c for c in curr_bf
                if (c.get("controller_seat_id") or c.get("owner_seat_id")) == local_seat
                and c.get("power") is not None
                and "land" not in c.get("type_line", "").lower()
            ]
            opp_creatures = [
                c for c in curr_bf
                if (c.get("controller_seat_id") or c.get("owner_seat_id")) != local_seat
                and c.get("power") is not None
                and "land" not in c.get("type_line", "").lower()
            ]
            your_power = sum(c.get("power") or 0 for c in your_creatures)
            opp_power = sum(c.get("power") or 0 for c in opp_creatures)
            hand_size = len(curr_state.get("hand", []))

            # Heuristic: multiple bad signals stacking up
            signals = 0
            if your_life <= 5:
                signals += 2
            elif your_life <= 10 and opp_life >= 15:
                signals += 1
            if opp_power >= your_life:  # opponent can lethal us
                signals += 2
            if len(opp_creatures) >= len(your_creatures) + 3:
                signals += 1
            if opp_power >= your_power + 8:
                signals += 1
            if hand_size == 0 and len(your_creatures) <= 1:
                signals += 1
            if your_life <= 3 and opp_power > 0:
                signals += 2  # almost certainly dead

            if signals >= 4:
                self._losing_badly_fired = True
                triggers.append("losing_badly")
                logger.info(
                    f"Losing badly detected: life={your_life} vs {opp_life}, "
                    f"power={your_power} vs {opp_power}, "
                    f"creatures={len(your_creatures)} vs {len(opp_creatures)}, "
                    f"hand={hand_size}, signals={signals}"
                )

        # Reset losing_badly flag on new game (turn resets to 0/1)
        if curr_turn_num <= 1 and prev_turn_num > 1:
            self._losing_badly_fired = False

        return triggers
