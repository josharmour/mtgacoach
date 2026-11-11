"""Per-model call settings: thinking effort follows the model served, not the alias.

One table (:data:`MODEL_ROWS`) holds, per model family, the reasoning effort
used for decisions, plan calls, playbook builds and the announcer. The family
comes from the model the gateway actually served (the response's ``model``
field, logged by the proxy as ``served_model``); until the first response
arrives the configured alias is the best available guess. An unknown served
model leaves the caller's request untouched and logs one line naming it. A
developer can override any row per machine with ``ARENAMCP_THINKING_ROWS``
(JSON) to pit one thinking setting against another without a code change.
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

# Call-class groups a row can set. Everything else (coach.*, draft.*, vision,
# auto.*, background.win_plan/set_primer/postmatch) is left as requested.
GROUPS = ("decision", "plan", "playbook", "announcer", "probe")

# Policy values inside a row:
#   "none"/"low"/"medium"/"high"/"max"  force this effort
#   "cap_low"                           high/max become low, anything else unchanged
#   None                                leave the caller's request unchanged
VALID_POLICIES = frozenset({"none", "low", "medium", "high", "max", "cap_low"})

# GLM (behind the "current" alias since 2026-11-03): exactly what the alias
# path sent before this table existed - decisions without thinking, plan calls
# capped at low, playbook/announcer/probe without thinking.
GLM_ROW = {
    "decision": "none",
    "plan": "cap_low",
    "playbook": "none",
    "announcer": "none",
    "probe": "none",
}
DEEPSEEK_ROW = dict(GLM_ROW)
# OpenRouter test models (or-*): never think, so a head-to-head run compares
# models and not thinking budgets. 2026-11-09 23:24 (match eedf1ae3): Gemma 4
# 26B at "low" spent 4,613 thinking tokens on a plan, 17 s at the median and
# 43 s at worst; five of ten plan calls were cancelled and no plan existed
# until turn 5. The served model spends about 320 tokens and 7 s on the same
# call.
OPENROUTER_TEST_ROW = {
    "decision": "none",
    "plan": "none",
    "playbook": "none",
    "announcer": "none",
    "probe": "none",
}

MODEL_ROWS = {
    "glm": GLM_ROW,
    "deepseek": DEEPSEEK_ROW,
    "openrouter-test": OPENROUTER_TEST_ROW,
}

# Families that get the hosted playbook settings (no thinking, bounded brief).
HOSTED_FAMILIES = frozenset({"glm", "deepseek", "openrouter-test"})

# Families the default-off selective-thinking experiment may run on.
SELECTIVE_FAMILIES = frozenset({"glm", "deepseek"})


def group_for_call_class(call_class) -> str:
    """Map a proxy call class onto its row group; "" when the row does not govern it."""
    cc = str(call_class or "")
    if cc.startswith("decision."):
        return "decision"
    if cc == "background.game_plan":
        return "plan"
    if cc == "background.deck_playbook":
        return "playbook"
    if cc == "background.commentary":
        return "announcer"
    if cc == "probe":
        return "probe"
    return ""


def family_from_served(name) -> str:
    """Model family of a served model name; "" when unknown."""
    n = str(name or "").lower()
    if n.startswith("or-"):
        return "openrouter-test"
    if "glm" in n:
        return "glm"
    if "deepseek" in n or "dsv4" in n:
        return "deepseek"
    return ""


def family_from_alias(name) -> str:
    """Best-guess family from the configured alias, used only before the first response.

    Mirrors what the proxy keyed on before served models were tracked: the
    hosted alias behaved like GLM, deepseek/dsv4 aliases like DeepSeek and
    or-* aliases like OpenRouter test models. Any other alias (including a
    direct model name) stayed as requested.
    """
    n = str(name or "").lower()
    if n.startswith("or-"):
        return "openrouter-test"
    if n == "current" or any(s in n for s in ("deepseek", "dsv4")):
        return "glm" if n == "current" else "deepseek"
    return ""


_OVERRIDES_CACHE: tuple[str, dict] | None = None


def _overrides() -> dict:
    """ARENAMCP_THINKING_ROWS parsed once per distinct value: {family or "*": {group: policy}}."""
    global _OVERRIDES_CACHE
    raw = os.environ.get("ARENAMCP_THINKING_ROWS", "")
    if _OVERRIDES_CACHE is not None and _OVERRIDES_CACHE[0] == raw:
        return _OVERRIDES_CACHE[1]
    parsed: dict[str, dict] = {}
    if raw.strip():
        try:
            data = json.loads(raw)
        except ValueError:
            logger.warning("[SETTINGS] ARENAMCP_THINKING_ROWS is not valid JSON; ignoring it")
            data = None
        if isinstance(data, dict):
            for family, row in data.items():
                if not isinstance(row, dict):
                    continue
                clean = {}
                for group, policy in row.items():
                    if group in GROUPS and policy in VALID_POLICIES:
                        clean[group] = policy
                    else:
                        logger.warning(
                            "[SETTINGS] ARENAMCP_THINKING_ROWS entry %r/%r ignored "
                            "(unknown group or policy)",
                            family,
                            group,
                        )
                if clean:
                    parsed[str(family)] = clean
    _OVERRIDES_CACHE = (raw, parsed)
    return parsed


def effective_row(family: str) -> dict:
    """Base row for a family merged with ARENAMCP_THINKING_ROWS overrides.

    A family-specific override wins over a "*" override; an unknown family has
    no base row, so only overrides apply.
    """
    merged = dict(MODEL_ROWS.get(family) or {})
    overrides = _overrides()
    merged.update(overrides.get("*", {}))
    merged.update(overrides.get(family, {}))
    return merged


def apply_policy(row: dict, group: str, reasoning_effort):
    """Return the effort a row dictates for a group; unchanged when it does not govern it."""
    policy = row.get(group)
    if policy is None:
        return reasoning_effort
    if policy == "cap_low":
        return "low" if reasoning_effort in {"high", "max"} else reasoning_effort
    return policy


def alias_effort(model, call_class: str, reasoning_effort):
    """Effort decided from the alias string alone (the pre-response fallback)."""
    return apply_policy(effective_row(family_from_alias(model)), group_for_call_class(call_class), reasoning_effort)


def _resolve_family(backend) -> tuple[str, str]:
    """(family, name-used) for a backend; served model wins over the alias."""
    served = getattr(backend, "last_served_model", None)
    alias = getattr(backend, "model", "")
    # Test doubles auto-create attributes on access; only real strings classify.
    if not isinstance(served, str) or not served:
        served = None
    if not isinstance(alias, str):
        alias = ""
    if served:
        family = family_from_served(served)
        # Some gateways echo the alias back as the served name ("current");
        # classify that through the alias table instead of calling it unknown.
        if not family and served == alias:
            family = family_from_alias(alias)
        return family, str(served)
    return family_from_alias(alias), str(alias or "")


def family_for_backend(backend) -> str:
    """Family of the model this backend talks to, cached per session.

    Recomputed only when ``last_served_model`` changes, so each session pays
    one classification until the gateway switches models underneath it.
    """
    served = getattr(backend, "last_served_model", None)
    cache = getattr(backend, "_call_settings_family", None)
    if isinstance(cache, tuple) and len(cache) == 2 and cache[0] == served:
        return cache[1]
    family, _ = _resolve_family(backend)
    try:
        backend._call_settings_family = (served, family)
    except Exception:  # pragma: no cover - exotic backend objects
        pass
    return family


def effort_for_backend(backend, call_class: str, reasoning_effort):
    """Effort for a call on ``backend``, decided from the model actually served."""
    family, name = _resolve_family(backend)
    if not family:
        seen = getattr(backend, "_call_settings_unknown_logged", None)
        if seen != name:
            try:
                backend._call_settings_unknown_logged = name
            except Exception:  # pragma: no cover - exotic backend objects
                pass
            logger.info(
                "[SETTINGS] no call-settings row for model '%s'; leaving thinking settings as requested",
                name,
            )
        return reasoning_effort
    return apply_policy(effective_row(family), group_for_call_class(call_class), reasoning_effort)
