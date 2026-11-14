"""Default-off DeepSeek tactical experiment; one bounded thinker per process."""

from __future__ import annotations

import concurrent.futures
import os
import threading
import time

_THINKER = threading.BoundedSemaphore(1)
POLICY = "deepseek-effort1-cap256-v1"


def enabled(model):
    """Whether the selective-thinking experiment may run for this model.

    Follows the model family, not the alias string: the proxy passes either
    the served model name or, before the first response, the configured alias.
    """
    from arenamcp.model_call_settings import (
        SELECTIVE_FAMILIES,
        family_from_alias,
        family_from_served,
    )

    return (
        os.environ.get("ARENAMCP_SELECTIVE_THINKING", "").lower() in {"1", "true"}
        and isinstance(model, str)
        and (family_from_served(model) or family_from_alias(model)) in SELECTIVE_FAMILIES
    )


def selection_reason(decision, state, critical):
    """Only consequential typed choices; no menus, mulligans, or routine passing."""
    if decision.request_type not in {"ActionsAvailable", "SelectTargets"}:
        return ""
    if critical:
        return str(critical)
    local = state.get("local_seat_id")
    if local is None:
        return ""
    hostile = any(
        c.get("controller_seat_id") not in (None, local) and c.get("targeting")
        for c in state.get("stack", []) or []
    )
    can_respond = decision.request_type == "SelectTargets" or any(
        (o.meta or {}).get("actionType") in {"ActionType_Cast", "ActionType_Activate"}
        and o.payable is not False
        for o in decision.options
    )
    return "response to a targeted opposing spell/ability" if hostile and can_respond else ""


def race(ask, parse, message, *, deadline, trace, cancel_event=None):
    """First validated answer wins; rejected fast answers leave the thinker a chance.

    Both workers get the SAME absolute deadline. Never join a stalled backend.
    Keep the admission slot until its worker exits, even after cancellation.
    The caller owns board/factual validation in parse, identically for both legs.
    """
    if deadline <= time.monotonic():
        raise TimeoutError("selective decision deadline expired")
    if cancel_event is not None and cancel_event.is_set():
        raise concurrent.futures.CancelledError("decision superseded")
    admission = _THINKER
    if not admission.acquire(blocking=False):
        trace.update(admitted=False, skipped="another thinker is still active", selected_leg="fast")
        return parse(ask(message, effort=None, budget=max(0, deadline - time.monotonic())))
    trace.update(
        policy=POLICY, admitted=True, deadline_monotonic=deadline, effort=1, thinking_token_budget=256
    )
    started = time.monotonic()
    condition = threading.Condition()
    results = {}
    cancels = {leg: threading.Event() for leg in ("fast", "thinking")}

    def run(leg):
        try:
            remaining = deadline - time.monotonic()
            if (
                remaining <= 0
                or cancels[leg].is_set()
                or (cancel_event is not None and cancel_event.is_set())
            ):
                raise concurrent.futures.CancelledError("decision expired or superseded")
            value = parse(
                ask(
                    message,
                    effort="selective" if leg == "thinking" else None,
                    budget=remaining,
                    cancel_event=cancels[leg],
                )
            )
            outcome = (True, value)
        except Exception as exc:
            outcome = (False, exc)
        finally:
            if leg == "thinking":
                admission.release()
        with condition:
            results[leg] = (*outcome, time.monotonic())
            condition.notify_all()

    for leg in ("thinking", "fast"):
        threading.Thread(target=run, args=(leg,), daemon=True, name=f"selective-{leg}").start()
    winner = None
    try:
        with condition:
            while time.monotonic() < deadline:
                if cancel_event is not None and cancel_event.is_set():
                    raise concurrent.futures.CancelledError("decision superseded")
                ready = [
                    (result[2], leg) for leg, result in results.items() if result[0] and result[2] <= deadline
                ]
                if ready:
                    winner = min(ready)[1]
                    break
                if len(results) == 2:
                    raise ValueError("both selective decision answers rejected")
                condition.wait(min(0.02, max(0, deadline - time.monotonic())))
            if winner is None:
                raise TimeoutError("selective decision deadline exhausted")
            return results[winner][1]
    finally:
        for leg, event in cancels.items():
            if leg != winner:
                event.set()
        with condition:
            trace["selected_leg"] = winner
            trace["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
            trace["legs"] = {
                leg: {
                    "status": "valid" if r[0] else "rejected",
                    "elapsed_ms": round((r[2] - started) * 1000, 1),
                    "option_ids": r[1][1] if r[0] else None,
                    "error": None if r[0] else str(r[1])[:200],
                    "cancel_requested": cancels[leg].is_set(),
                }
                if (r := results.get(leg)) is not None
                else {"status": "pending", "cancel_requested": cancels[leg].is_set()}
                for leg in cancels
            }
            fast = results.get("fast")
            trace["changed_action"] = (
                results[winner][1][1] != fast[1][1] if winner == "thinking" and fast and fast[0] else None
            )
            trace["replaced_rejected_fast"] = bool(winner == "thinking" and fast and not fast[0])


# E4 source preparation. Not wired to runtime until the signal adapter and
# independent integration tests are reviewed; E1's real-evidence gate still holds.
def pivotal_reasons(
    *,
    lethal=False,
    dead_soon=False,
    worthwhile_response_targets=0,
    unresolved_mulligan=False,
    commander_or_engine_cast=False,
    close_combat=False,
    preferred_option_unevaluated=False,
):
    """Classify already-established facts, without inventing evaluations.

    The adapter must supply worthwhile legal targets (not raw battlefield size),
    an actually preferred unevaluated option (not merely any unknown option),
    and the combat solver's closeness evidence. Missing facts stay missing.
    This classifier does not infer rules or inspect a card's display name.
    """
    reasons = []
    if lethal:
        reasons.append("lethal opportunity or threat")
    if dead_soon:
        reasons.append("dead-soon warning")
    if worthwhile_response_targets > 1:
        reasons.append("multiple worthwhile removal/counterspell targets")
    if unresolved_mulligan:
        reasons.append("mulligan not settled by the fixed rule")
    if commander_or_engine_cast:
        reasons.append("commander or engine cast")
    if close_combat:
        reasons.append("combat solver rates the choice close")
    if preferred_option_unevaluated:
        reasons.append("preferred option is unevaluated")
    return tuple(reasons)


class PivotalCallBudget:
    """One high call, one shared safe-default deadline, no concurrent hedge.

    This admission object never starts work itself. It preserves correction
    opportunities at low effort after the high call; denied corrections must
    use the caller's established safe fallback rather than accepting a refuted
    action. Load is supplied afresh at each request, not cached at admission.
    """

    def __init__(self, *, deadline, normal_seconds, pivotal_seconds, reasons=()):
        import math

        values = (deadline, normal_seconds, pivotal_seconds)
        if any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values
        ):
            raise ValueError("finite numeric budgets required")
        if normal_seconds <= 0 or pivotal_seconds < normal_seconds:
            raise ValueError("pivotal budget must cover the positive normal budget")
        self.deadline = deadline
        self.normal_seconds = normal_seconds
        self.pivotal_seconds = pivotal_seconds
        self.reasons = tuple(reasons)
        self.high_calls = 0
        self.calls = 0

    def admit(self, *, now, degraded=False, plan_in_flight=False, correction=False):
        """Return (effort, seconds), or raise before an unsafe/extra call.

        ``deadline`` already reserves Arena safe-default time. Neither initial
        thinking nor a later correction extends it. A post-answer contradiction
        may promote a previously routine decision, but still gets at most one
        high call. Degraded/in-flight work permits one low call, never extras.
        """
        import math

        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            raise ValueError("finite monotonic time required")
        remaining = self.deadline - now
        if remaining <= 0:
            raise TimeoutError("Arena clock reserved for safe default")
        busy = bool(degraded or plan_in_flight)
        if busy and self.calls:
            raise TimeoutError("no additional call while server or plan is busy")
        high = bool((self.reasons or correction) and not busy and not self.high_calls)
        self.calls += 1
        self.high_calls += int(high)
        return (
            "high" if high else "low",
            min(remaining, self.pivotal_seconds if high else self.normal_seconds),
        )


def pivotal_enabled():
    """E4 is opt-in until E1 and real-match performance gates are accepted."""
    return os.environ.get("ARENAMCP_PIVOTAL_THINKING", "").lower() in {"1", "true"}


def pivotal_evidence(
    decision, state, *, assessment=None, search=None, target_hints=None, preferred_ids=(), unmodelled=()
):
    """Adapt existing evidence without counting unknowns as good choices.

    `preferred_ids` must come from a real prior selector; an arbitrary first
    option is never used. The engine signal reads already-annotated facts only.
    No resolver, disk lookup, model request or extra search occurs here.
    """
    from arenamcp.board_assessment import _source_card, card_role, engine_role

    local = state.get("local_seat_id")
    options = [o for o in decision.options if o.payable is not False]
    playable = {o.option_id for o in options}
    commanders = set(state.get("commander_grp_ids") or [])
    for player in state.get("players") or []:
        if player.get("seat_id") == local:
            commanders.update(player.get("commander_grp_ids") or [])
    opponent = next(
        (p.get("seat_id") for p in state.get("players") or [] if p.get("seat_id") not in (None, local)), None
    )
    commander_or_engine = False
    for option in options:
        if (option.meta or {}).get("actionType") != "ActionType_Cast":
            continue
        card, zone = _source_card(state, option.meta)
        owned = local is not None and card.get("controller_seat_id", card.get("owner_seat_id")) == local
        commander_or_engine |= bool(
            owned
            and (
                zone == "command"
                or card.get("grp_id") in commanders
                or engine_role(card) > 0
                or recognised_engine_cast(card, local, opponent)
            )
        )
    worthwhile = 0
    if decision.request_type == "SelectTargets":
        source = next(
            (c for c in state.get("stack") or [] if c.get("instance_id") == decision.source_instance_id), None
        )
        if source and card_role(source) in {"removal", "bounce", "counter"}:
            worthwhile = sum(
                1
                for o in options
                if str(o.option_id).startswith("tgt:")
                and str(o.option_id)[4:].isdigit()
                and (target_hints or {}).get(int(o.option_id[4:])) == "Hot"
            )
    close = False
    if (
        decision.request_type in {"DeclareAttackers", "DeclareBlockers"}
        and search is not None
        and not search.truncated
        and not search.unsupported
        and not search.choice_caveats
    ):
        lines = sorted(
            (line for line in search.lines if line.complete and not line.unsupported),
            key=lambda line: line.ranked,
            reverse=True,
        )
        if decision.request_type in {"DeclareAttackers", "DeclareBlockers"} and len(lines) >= 2:
            close = (
                lines[0].ranked[:2] == lines[1].ranked[:2]
                and abs(lines[0].ranked[2] - lines[1].ranked[2]) < 5
            )
    return pivotal_reasons(
        lethal=bool(
            assessment
            and (assessment.lethal_now or assessment.lethal_next_turn or assessment.opp_lethal_on_board)
        ),
        dead_soon=bool(assessment and assessment.dead_in is not None and assessment.dead_in <= 1),
        worthwhile_response_targets=worthwhile,
        unresolved_mulligan=decision.request_type == "Mulligan"
        or (decision.request_type == "Group" and "LondonMulligan" in str(decision.source_label or "")),
        commander_or_engine_cast=commander_or_engine,
        close_combat=close,
        preferred_option_unevaluated=bool(set(preferred_ids) & playable & set(unmodelled)),
    )


def close_combat_reason(search):
    """A small value gap between distinct combat choices on the same deployment."""
    if search is None or search.truncated or search.unsupported or search.choice_caveats:
        return ""
    lines = sorted(
        (line for line in search.lines if line.complete and not line.unsupported and line.steps),
        key=lambda line: line.ranked,
        reverse=True,
    )
    if len(lines) < 2:
        return ""
    best = lines[0]
    for other in lines[1:]:
        a, b = best.steps[0], other.steps[0]
        if (a.land, a.casts, a.targets) != (b.land, b.casts, b.targets):
            continue
        different = a.attack != b.attack or (best.block and other.block and best.block != other.block)
        if different and best.ranked[:2] == other.ranked[:2] and 0 <= best.ranked[2] - other.ranked[2] < 5:
            return "complete combat alternatives have a value gap below 5"
    return ""


def recognised_engine_cast(card, local, opponent):
    """Use the existing engine parser on a copy; never add the card to live state."""
    if local is None or opponent is None:
        return False
    from arenamcp.board_assessment import engine_role
    from arenamcp.board_model import annotate_engines, engines_of

    candidate = dict(card)
    # TheirEngines models the side passed as opponent. Swap perspective to
    # inspect this known owned cast's intrinsic engine, without simulation.
    engine = engines_of([candidate], opponent, local)
    if engine is None:
        return False
    body = {"_card": candidate}
    annotate_engines([body], engine)
    return engine_role(body) > 0 or bool(engine.names)
