"""Arena's "Oops" emote when the autopilot gets stuck or clearly blunders.

With autoplay on, the coach sends Arena's own "Oops" emote to the opponent
(``GREBridge.send_emote``: the emote wheel's click handler) when the
autopilot gets stuck on something it can't do, or makes a mistake the log
proves. The emote reaches a real person, so every trigger is deterministic
and strict: a missed Oops is fine, a wrong one is not.

Incidents:

* ``stuck_loop``: the semantic-progress guard paused autoplay. The same
  choice was submitted 3 or more times over 8 s or more and the game did not
  advance.
* ``self_cancel``: the autopilot started a cast or activation, then backed
  out of it at a follow-up step (the self-cancel guard).
* ``stuck_manual``: MANUAL REQUIRED for a reason that means "the autopilot
  tried and can't do this" (``manual_reason_counts``), and the same decision
  is still waiting MANUAL_DWELL_S later.
* ``self_harm`` (log fact): our own spell or ability resolved and dealt
  damage to, or destroyed, one of our permanents that the autopilot had
  targeted with it, and that permanent died.
* ``attack_blunder`` (log fact): a creature the autopilot attacked with died
  to a single blocker that survived, and the losing-attack guard's own
  whole-attack verdict (``combat_strategy.losing_attackers``) on the board the
  autopilot declared against says the opponent could answer the whole attack
  for free. That blocker alone was untapped, could legally block it, and
  would kill it and survive. Combat is judged whole, once the regular damage
  step starts: no combat damage reached the opponent or a noncreature
  permanent, nothing but our own attackers died (no exchange), and nothing
  was cast, activated or resolved during combat.

Log facts carry their match, game number (a best-of-three's games share the
match id and restart turn numbers), turn and the time they were seen. A fact
is judged only on its own turn and within STALE_S of happening, and only
against the autopilot's evidence from the same game.

Rate limits: one Oops per incident, MAX_PER_GAME per game, MIN_GAP_S apart.
None while autoplay is off, in dry run or land-only, while a concede offer
or countdown runs or once this game's concede was sent, after the game is
over, or during a draft. Every decision is logged with an ``[OOPS]`` prefix.
"""

from __future__ import annotations

import copy
import logging
import re
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

EMOTE = "oops"
MAX_PER_GAME = 2
MIN_GAP_S = 60.0
# MANUAL REQUIRED must still be waiting on the same decision this long later.
MANUAL_DWELL_S = 10.0
# An incident that could not be sent this long after it became due is dropped.
STALE_S = 20.0

STUCK_LOOP = "stuck_loop"
SELF_CANCEL = "self_cancel"
STUCK_MANUAL = "stuck_manual"
SELF_HARM = "self_harm"
ATTACK_BLUNDER = "attack_blunder"
KINDS = (STUCK_LOOP, SELF_CANCEL, STUCK_MANUAL, SELF_HARM, ATTACK_BLUNDER)

# MANUAL REQUIRED reasons that mean "the autopilot tried and can't do this".
# This is an allowlist, so a new or unknown reason never sends an emote. Left
# out on purpose: choosing for the opponent on a controlled turn, an unmapped
# GRE interaction (seen only between games), the planner or LLM failing, the
# game moving past a planned action, an offline or unavailable bridge,
# repeated passes in an ActionsAvailable window, and choosing who goes first.
_MANUAL_STUCK = re.compile(
    r"^(?:"
    r"Bridge couldn't handle (?!choose_starting_player)"
    r"|Action repeatedly failed \(\d+x\)"
    r"|Arena offers attackers but their choices were not resolved"
    r"|Safe-default submission failed"
    r"|Blocked action repeated in the same priority window"
    r"|Runaway protection"
    r"|Autopilot is stuck and needs a manual"
    r"|Search choices are still unreadable"
    r"|Non-mana payment (?:choices are incomplete|submission failed)"
    r"|No valid automatic non-mana payment"
    r"|(?!ActionsAvailable\b)\w+ not accepted after \d+ submissions"
    r"|\w+: no safe automatic choice"
    r"|\w+: submission was not accepted"
    r"|SelectTargets: choose no targets manually"
    r"|Target selection was not verified"
    r"|Pay for .+ manually"
    r"|GRE bridge submit_auto_tap did not advance"
    r"|X value has no useful tutor target"
    r")"
)
_BRIDGE_DOWN = re.compile(r"bridge (?:is )?(?:offline|unavailable)|not connected", re.IGNORECASE)


def manual_reason_counts(reason: str) -> bool:
    """True when a MANUAL REQUIRED reason means the autopilot is stuck on its own move."""
    text = str(reason or "").strip()
    return bool(_MANUAL_STUCK.match(text)) and not _BRIDGE_DOWN.search(text)


# --- log facts (one GameStateMessage at a time) ------------------------------------

# Zone transfers that mean a permanent died to damage or a destroy effect.
_DEATHS = {"SBA_Damage", "SBA_Deathtouch", "Destroy"}
_COMBAT_DEATHS = {"SBA_Damage", "SBA_Deathtouch"}
_DAMAGE_STEPS = {"Step_FirstStrikeDamage", "Step_CombatDamage"}
# Combat steps after attackers are declared: anything done here can change
# how the combat goes, so the board at declaration no longer predicts it.
_AFTER_DECLARE_STEPS = {
    "Step_DeclareAttack",
    "Step_DeclareBlock",
    "Step_FirstStrikeDamage",
    "Step_CombatDamage",
}
# UserActionTaken action types (GRE ActionType) that change nothing on the
# board: None, Play (a land), mana abilities, Pass, ActivateTest,
# ResolutionCost, MakePayment, CombatCost, OpeningHandAction, FloatMana,
# PlayMdfc, SpecialPayment. Every other action type is a cast or activation.
_NEUTRAL_ACTIONS = {0, 3, 4, 5, 6, 9, 12, 14, 15, 17, 19, 20}


def _int(value: Any) -> int:
    if isinstance(value, list):
        value = value[0] if value else 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _details(annotation: dict) -> dict[str, Any]:
    details: dict[str, Any] = {}
    for detail in annotation.get("details") or []:
        if not isinstance(detail, dict) or not detail.get("key"):
            continue
        for kind in ("valueInt32", "valueString", "valueInt64"):
            raw = detail.get(kind)
            if raw not in (None, [], ""):
                details[detail["key"]] = raw[0] if isinstance(raw, list) and len(raw) == 1 else raw
                break
    return details


def message_facts(
    annotations: Iterable[Any],
    *,
    describe: Callable[[int], dict | None],
    name_of: Callable[[int], str] = lambda grp_id: "",
    local_seat: int | None,
    seats: Iterable[int],
    match_id: Any,
    turn: int,
    phase: str,
    step: str,
    active_player: int,
    game_number: int | None = None,
) -> list[dict[str, Any]]:
    """Facts the Oops blunder checks need, from one message's transient annotations.

    ``describe(instance_id)`` returns ``{"controller", "grp_id", "types"}``
    for a known object (an ability reports its source card's grp_id), or None;
    ``name_of(grp_id)`` is asked only for facts that are kept. Persistent
    annotations repeat on every message, so callers pass only the transient
    ``annotations``. A combat damage step's first message (it carries
    PhaseOrStepModified) always yields a "combat" fact, even an empty one:
    the regular damage step is where the attack is judged, and an attacker
    killed by a first striker leaves that step with nothing in it.
    """
    if not local_seat:
        return []
    seats = {int(seat) for seat in seats if seat}
    renamed_from: dict[int, int] = {}
    resolving: list[int] = []
    damage: list[tuple[int, int, int]] = []
    destroyed: dict[int, int] = {}
    deaths: dict[int, str] = {}
    actions: list[dict[str, Any]] = []
    step_started = False
    in_combat = phase == "Phase_Combat" and step in _AFTER_DECLARE_STEPS
    for annotation in annotations:
        if not isinstance(annotation, dict):
            continue
        types = {str(t).removeprefix("AnnotationType_") for t in annotation.get("type") or []}
        affector = _int(annotation.get("affectorId"))
        affected = [_int(i) for i in annotation.get("affectedIds") or [] if _int(i)]
        details = _details(annotation)
        if "PhaseOrStepModified" in types:
            step_started = True
        if "ObjectIdChanged" in types:
            original, new = _int(details.get("orig_id")), _int(details.get("new_id"))
            if original and new:
                renamed_from[new] = renamed_from.get(original, original)
        if "ResolutionStart" in types and affector:
            resolving.append(affector)
            if in_combat:
                actions.append({"resolution": affector})
        if "DamageDealt" in types and affector:
            amount = _int(details.get("damage"))
            damage.extend((affector, target, amount) for target in affected)
        if "ZoneTransfer" in types:
            category = str(details.get("category") or "")
            for instance in affected:
                original = renamed_from.get(instance, instance)
                if category in _DEATHS:
                    deaths[original] = category
                if category == "Destroy" and affector:
                    destroyed[original] = affector
        if "UserActionTaken" in types and in_combat:
            action_type = _int(details.get("actionType"))
            if action_type not in _NEUTRAL_ACTIONS:
                actions.append({"seat": affector, "action_type": action_type})

    base = {
        "match_id": match_id,
        "game_number": game_number,
        "turn": int(turn or 0),
        "phase": phase,
        "step": step,
        "active_player": int(active_player or 0),
        "local_seat": int(local_seat),
    }
    facts: list[dict[str, Any]] = []

    def info(instance: int) -> dict:
        return describe(instance) or {}

    # Our own resolving spell or ability killed one of our own permanents.
    for source in resolving:
        source_info = info(source)
        if source_info.get("controller") != local_seat:
            continue
        hits: dict[int, int] = {}
        for origin, target, amount in damage:
            if origin == source and target not in seats:
                hits[target] = hits.get(target, 0) + amount
        for target, origin in destroyed.items():
            if origin == source:
                hits.setdefault(target, 0)
        for target, amount in hits.items():
            target_info = info(target)
            if target not in deaths or target_info.get("controller") != local_seat:
                continue
            facts.append(
                {
                    **base,
                    "kind": SELF_HARM,
                    "source_id": source,
                    "source_grp": source_info.get("grp_id"),
                    "source_name": name_of(_int(source_info.get("grp_id"))) or f"#{source}",
                    "target_id": target,
                    "target_grp": target_info.get("grp_id"),
                    "target_name": name_of(_int(target_info.get("grp_id"))) or f"#{target}",
                    "category": deaths[target],
                    "damage": amount,
                }
            )

    if phase == "Phase_Combat" and step in _DAMAGE_STEPS and (damage or deaths or step_started):
        entries = []
        for origin, target, amount in damage:
            target_info = info(target)
            if target in seats:
                kind = "player"
            elif "creature" in " ".join(str(t) for t in target_info.get("types") or []).lower():
                kind = "creature"
            else:
                kind = "other"
            entries.append(
                {
                    "source": origin,
                    "target": target,
                    "amount": amount,
                    "target_kind": kind,
                    "target_controller": target if kind == "player" else target_info.get("controller"),
                }
            )
        facts.append({**base, "kind": "combat", "damage": entries, "deaths": dict(deaths)})
    if actions:
        facts.append({**base, "kind": "combat_action", "actions": actions})
    return facts


# --- the blunder checks --------------------------------------------------------------

# The dying permanent pays off dying: killing it can be the plan.
_DEATH_PAYOFF = re.compile(
    r"\bwhen(?:ever)?\b[^.]*\b(?:dies|is put into (?:a|your) graveyard|leaves the battlefield)\b"
    r"|\b(?:undying|persist|afterlife|blitz|encore)\b"
)
# The spell or ability is meant to target our own permanents.
_TARGETS_OWN = re.compile(
    r"target (?:[a-z-]+ )*?(?:creatures?|permanents?|artifacts?|enchantments?|planeswalkers?) you control"
)


def same_game(a: dict, b: dict) -> bool:
    """Both belong to one game: the same match and, when both know it, the same game number.

    A best-of-three's games share the match id and restart turn numbers, so
    match and turn alone would mix game 1's facts and evidence into game 2.
    """
    match_a, match_b = a.get("match_id"), b.get("match_id")
    if match_a and match_b and match_a != match_b:
        return False
    game_a, game_b = a.get("game_number"), b.get("game_number")
    return game_a is None or game_b is None or game_a == game_b


def self_harm_reason(fact: dict, targets: Iterable[dict], card_text: Callable[[int], str]) -> str:
    """Why this self-harm fact is a blunder ("" when it is not clearly one).

    The autopilot must have chosen that very target for that very source this
    turn; a target with a death payoff, or a source made to target our own
    permanents, is never treated as a mistake.
    """
    chosen = any(
        record.get("turn") == fact.get("turn")
        and same_game(record, fact)
        and _int(record.get("source_id")) == fact.get("source_id")
        and fact.get("target_id") in {_int(t) for t in record.get("targets") or []}
        for record in targets
    )
    if not chosen:
        return ""
    target_text = (card_text(_int(fact.get("target_grp"))) or "").lower()
    source_text = (card_text(_int(fact.get("source_grp"))) or "").lower()
    if _DEATH_PAYOFF.search(target_text) or _TARGETS_OWN.search(source_text):
        return ""
    how = "destroyed" if fact.get("category") == "Destroy" else f"dealt {fact.get('damage') or 0} damage to"
    return (
        f"our {fact.get('source_name')} {how} our own {fact.get('target_name')}, "
        "which the autopilot had targeted with it, and it died"
    )


def _free_kill(state: dict, attacker_id: int, blocker_id: int, losing: dict[int, str]) -> str:
    """Why this block was a predictable free kill on the declared board ("" if not).

    ``losing`` is the losing-attack guard's verdict on the whole declared
    attack (``combat_strategy.losing_attackers``): it names an attacker only
    when the opponent could block every attacker at once so that each dies, no
    blocker dies and nothing gets through, and no attack or death payoff,
    forced attack, affordable trick or all-in board says to attack anyway.
    The blocker that actually killed it must also have been able to do so on
    its own.
    """
    from arenamcp.combat_strategy import _answers, _eligible_blockers

    if attacker_id not in losing:
        return ""
    players = state.get("players") or []
    opponent = next((p.get("seat_id") for p in players if not p.get("is_local")), None)
    cards = {card.get("instance_id"): card for card in state.get("battlefield") or []}
    attacker = cards.get(attacker_id)
    blocker = next(
        (c for c in _eligible_blockers(state, opponent) if c.get("instance_id") == blocker_id), None
    )
    if attacker is None or blocker is None or not _answers(attacker, [blocker]):
        return ""
    return (
        f"{attacker.get('name') or attacker_id} {attacker['power']}/{attacker['toughness']} attacked into an "
        f"untapped {blocker.get('name') or blocker_id} {blocker['power']}/{blocker['toughness']}, which blocked, "
        "killed it and survived; nothing got through"
    )


def attack_blunders(fact: dict, record: dict, turn_facts: Iterable[dict]) -> list[tuple[int, str]]:
    """(attacker, reason) for each attacker that died for nothing, judged at the regular damage step.

    Combat is judged whole: the first-strike step's damage and deaths count
    with the regular step's; damage that got through or anything besides our
    own attackers dying (an exchange) means the attack did something; and an
    attacker is flagged only when the losing-attack guard's own whole-attack
    verdict says the opponent could answer every attacker at once for free.
    """
    if fact.get("active_player") != fact.get("local_seat") or fact.get("step") != "Step_CombatDamage":
        return []
    if record.get("turn") != fact.get("turn") or not same_game(record, fact):
        return []
    turn_facts = [f for f in turn_facts if f.get("turn") == fact.get("turn") and same_game(f, fact)]
    if any(f.get("kind") == "combat_action" for f in turn_facts):
        return []  # something was cast, activated or resolved during combat
    attackers = [_int(a) for a in record.get("attackers") or [] if _int(a)]
    combat = [f for f in turn_facts if f.get("kind") == "combat"]
    damage = [entry for f in combat for entry in f.get("damage") or []]
    deaths = {k: v for f in combat for k, v in (f.get("deaths") or {}).items()}
    if any(
        entry.get("source") in attackers and entry.get("target_kind") != "creature" and entry.get("amount")
        for entry in damage
    ):
        return []  # combat damage reached the opponent or a noncreature permanent
    dead = [attacker for attacker in attackers if deaths.get(attacker) in _COMBAT_DEATHS]
    if not dead:
        return []
    if any(instance not in attackers for instance in deaths):
        return []  # an exchange: a blocker (or something else) died in this combat too
    state = copy.deepcopy(record.get("state") or {})
    try:
        from arenamcp.combat_strategy import losing_attackers

        losing = losing_attackers(state, attackers, record.get("pending"))
    except Exception as error:  # never let a board quirk raise into the loop
        logger.debug("[OOPS] attack check failed: %s", error)
        return []
    blunders = []
    for attacker in dead:
        killers = {
            entry.get("source") for entry in damage if entry.get("target") == attacker and entry.get("amount")
        }
        if len(killers) != 1:
            continue
        try:
            why = _free_kill(state, attacker, next(iter(killers)), losing)
        except Exception as error:
            logger.debug("[OOPS] attack check failed: %s", error)
            why = ""
        if why:
            blunders.append((attacker, why))
    return blunders


# --- the controller ---------------------------------------------------------------------


@dataclass
class _Incident:
    kind: str
    key: Any
    detail: str
    match_id: Any
    turn: int
    game_key: Any
    reported_at: float
    due_at: float
    state: dict | None = field(default=None, repr=False)


def _default_start(target: Callable[..., None], *args: Any) -> None:
    threading.Thread(target=target, args=args, daemon=True, name="oops-emote").start()


def _turn_of(state: Any) -> int:
    turn = (state or {}).get("turn") if isinstance(state, dict) else None
    return _int((turn or {}).get("turn_number")) if isinstance(turn, dict) else 0


def _handled_id(game_key: Any, kind: str, key: Any) -> tuple[Any, str]:
    return (game_key, repr((kind, key)))


def _remember(store: OrderedDict, key: Any, value: Any = True, limit: int = 64) -> None:
    store[key] = value
    store.move_to_end(key)
    while len(store) > limit:
        store.popitem(last=False)


class OopsController:
    """Turns stuck/blunder incidents into at most a few rate-limited Oops emotes.

    Thread model: ``report`` runs on the autopilot thread, ``observe`` on the
    coaching loop, each send on its own daemon thread (one at a time). State
    lives under one lock; nothing calls the bridge, settings or the engine
    while holding it. Nothing here raises into its callers.
    """

    def __init__(
        self,
        *,
        emote_fn: Callable[[dict], Any],
        settings_get: Callable[..., Any],
        autopilot_on: Callable[[], bool],
        bridge_ready: Callable[[], bool],
        game_over: Callable[[], bool] = lambda: False,
        concede_active: Callable[[Any], bool] = lambda game_key: False,
        still_stuck: Callable[[dict], bool] = lambda state: False,
        draft_active: Callable[[], bool] = lambda: False,
        card_text: Callable[[int], str] = lambda grp_id: "",
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        start_thread: Callable[..., None] = _default_start,
    ) -> None:
        self._emote_fn = emote_fn
        self._settings_get = settings_get
        self._autopilot_on = autopilot_on
        self._bridge_ready = bridge_ready
        self._game_over = game_over
        self._concede_active = concede_active
        self._still_stuck = still_stuck
        self._draft_active = draft_active
        self._card_text = card_text
        self._clock = clock
        self._wall_clock = wall_clock
        self._start_thread = start_thread
        self._lock = threading.Lock()
        self._pending: list[_Incident] = []
        self._handled: OrderedDict = OrderedDict()  # (game_key, repr((kind, key))) -> outcome
        self._sent: OrderedDict = OrderedDict()  # game_key -> Oops emotes that may have gone out
        self._last_sent_at: float | None = None
        self._last_sent_wall: float | None = None
        self._in_flight = False
        self._bridge_unsupported = False
        self._wheel_missing: OrderedDict = OrderedDict()  # game_key -> error
        self._facts: deque = deque(maxlen=64)
        self._decisions: deque = deque(maxlen=12)
        self._game_key: Any = None
        self._match_id: Any = None
        self._turn = 0
        self._resume: dict | None = None

    # -- settings and gates ----------------------------------------------------

    def enabled(self) -> bool:
        return self._settings_get("oops_emote", True) is not False

    @staticmethod
    def _safe(fn: Callable[[], Any], default: Any = None) -> Any:
        try:
            return fn()
        except Exception:
            return default

    def _concede_blocks(self, game_key: Any) -> bool:
        """A concede offer or countdown runs, or this game's concede was claimed or sent."""
        return bool(self._safe(lambda: self._concede_active(game_key), False))

    def _gate(self, state: dict | None, game_key: Any = None) -> str:
        """Why no Oops may go out now ("" when one may)."""
        from arenamcp.concede import game_not_live

        if not self.enabled():
            return "the Oops emote setting is off"
        if not self._safe(self._autopilot_on, False):
            return "autoplay is off (or dry run / land-only)"
        if self._safe(self._draft_active, False):
            return "a draft is active"
        if self._safe(self._game_over, False):
            return "the game is over"
        not_live = game_not_live(state) if state is not None else "no game state"
        if not_live:
            return f"the game is not in progress ({not_live})"
        if self._concede_blocks(game_key):
            return "a concede offer is active or this game was conceded"
        if not self._safe(self._bridge_ready, False):
            self._bridge_unsupported = False  # it went away; an updated client may come back able
            return "the bridge is not connected or can't send emotes"
        if self._bridge_unsupported:
            return "this bridge can't send emotes (update the plugin)"
        return ""

    def _log_decision(
        self, outcome: str, incident: _Incident | None, why: str = "", kind: str = "", detail: str = ""
    ) -> None:
        kind = incident.kind if incident else kind
        detail = incident.detail if incident else detail
        self._decisions.append(
            {
                "at": round(self._wall_clock(), 1),
                "kind": kind,
                "outcome": outcome,
                "why": why,
                "detail": detail[:200],
                "turn": incident.turn if incident else self._turn,
            }
        )
        if outcome == "sent":
            logger.warning(
                "[OOPS] sent for %s (game %r, turn %s): %s", kind, self._game_key, self._turn, detail
            )
        elif outcome in ("failed", "outcome_unknown"):
            logger.warning("[OOPS] %s for %s: %s | %s", outcome.replace("_", " "), kind, why, detail)
        else:
            logger.info("[OOPS] %s %s (%s): %s", outcome, kind, why, detail)

    # -- incidents from the autopilot ----------------------------------------------

    def report(self, kind: str, key: Any, detail: str, state: dict | None = None) -> bool:
        """Note an incident (any thread, never blocks). True when it was queued."""
        try:
            return self._report(kind, key, detail, state)
        except Exception as error:
            logger.warning("[OOPS] report failed: %s", error, exc_info=True)
            return False

    def _report(self, kind: str, key: Any, detail: str, state: dict | None) -> bool:
        detail = str(detail or "")
        if kind not in KINDS:
            logger.info("[OOPS] ignored unknown incident kind %r: %s", kind, detail)
            return False
        if kind == STUCK_MANUAL and not manual_reason_counts(detail):
            self._log_decision(
                "suppressed", None, "this MANUAL REQUIRED reason is not a stuck move", kind, detail
            )
            return False
        if not self.enabled():
            self._log_decision("suppressed", None, "the Oops emote setting is off", kind, detail)
            return False
        if not self._safe(self._autopilot_on, False):
            self._log_decision("suppressed", None, "autoplay is off (or dry run / land-only)", kind, detail)
            return False
        now = self._clock()
        match_id = (state or {}).get("match_id") if isinstance(state, dict) else None
        with self._lock:
            game_key = self._game_key if (match_id is None or match_id == self._match_id) else None
            handled_key = _handled_id(game_key, kind, key)
            if handled_key in self._handled or any(
                _handled_id(i.game_key, i.kind, i.key) == handled_key for i in self._pending
            ):
                logger.debug("[OOPS] incident %s %r already noted", kind, key)
                return False
            dwell = MANUAL_DWELL_S if kind == STUCK_MANUAL else 0.0
            incident = _Incident(
                kind=kind,
                key=key,
                detail=detail,
                match_id=match_id or self._match_id,
                turn=_turn_of(state) or self._turn,
                game_key=game_key,
                reported_at=now,
                due_at=now + dwell,
                state=state if isinstance(state, dict) else None,
            )
            # Immediate incidents of a known game are decided now, on the
            # reporting thread: the stuck-loop capture turns autoplay off right
            # after reporting, so waiting for the coaching loop would see it off.
            immediate = not dwell and game_key is not None
            if not immediate:
                self._pending.append(incident)
                del self._pending[:-16]
        logger.info(
            "[OOPS] incident %s on turn %s%s: %s",
            kind,
            incident.turn,
            f" (checking again in {dwell:.0f}s)" if dwell else "",
            detail,
        )
        if immediate:
            self._consider(incident, state if isinstance(state, dict) else {}, now)
        return True

    def clear_pending(self, reason: str) -> None:
        with self._lock:
            dropped, self._pending = self._pending, []
        for incident in dropped:
            self._log_decision("suppressed", incident, reason)

    # -- the coaching-loop hook ----------------------------------------------------

    def observe(
        self,
        state: dict,
        game_key: Any,
        *,
        facts: Iterable[dict] = (),
        evidence: dict | None = None,
    ) -> None:
        """Track the game, turn log facts into incidents, and send what is due."""
        try:
            self._observe(state, game_key, list(facts or ()), evidence or {})
        except Exception as error:  # never break the coaching loop
            logger.warning("[OOPS] observe failed: %s", error, exc_info=True)

    def _observe(self, state: dict, game_key: Any, facts: list[dict], evidence: dict) -> None:
        state = state if isinstance(state, dict) else {}
        match_id = state.get("match_id")
        turn = _turn_of(state)
        self._apply_resume(game_key, match_id, turn)
        with self._lock:
            changed = game_key != self._game_key
            self._game_key, self._match_id, self._turn = game_key, match_id, turn
            if changed:
                self._facts.clear()  # a Bo3's next game reuses the match id and turn numbers
            stale = [
                i
                for i in self._pending
                if changed or i.game_key not in (None, game_key) or i.match_id not in (None, match_id)
            ]
            self._pending = [i for i in self._pending if i not in stale]
            for incident in self._pending:
                incident.game_key = game_key
        for incident in stale:
            self._log_decision("suppressed", incident, "the game changed")
        if self._pending and self._concede_blocks(game_key):
            self.clear_pending("a concede offer is active or this game was conceded")

        now = self._clock()
        current = {"match_id": match_id, "game_number": state.get("game_number")}
        for fact in facts:
            if fact.get("match_id") != match_id or not same_game(fact, current):
                continue
            # Kept even when too old to judge: a first-strike death or a trick
            # during combat still decides how the rest of that combat is read.
            self._facts.append(fact)
            seen_at = fact.get("at")
            age = now - seen_at if isinstance(seen_at, (int, float)) else 0.0
            if fact.get("turn") != turn or age > STALE_S:
                if fact.get("kind") == SELF_HARM or (
                    fact.get("kind") == "combat"
                    and fact.get("deaths")
                    and fact.get("active_player") == fact.get("local_seat")
                ):
                    self._log_decision(
                        "suppressed",
                        None,
                        f"log fact from turn {fact.get('turn')} read on turn {turn}, {age:.0f}s later",
                        ATTACK_BLUNDER if fact.get("kind") == "combat" else SELF_HARM,
                        str(fact.get("source_name") or fact.get("deaths") or ""),
                    )
                continue
            for kind, key, detail in self._blunders(fact, evidence):
                self.report(kind, key, detail, state)

        with self._lock:
            due = [i for i in self._pending if i.due_at <= now]
            self._pending = [i for i in self._pending if i.due_at > now]
        for incident in due:
            self._consider(incident, state, now)

    def _blunders(self, fact: dict, evidence: dict) -> list[tuple[str, Any, str]]:
        kind = fact.get("kind")
        if kind == SELF_HARM:
            reason = self_harm_reason(fact, evidence.get("targets") or [], self._card_text)
            if reason:
                return [(SELF_HARM, (fact.get("turn"), fact.get("source_id"), fact.get("target_id")), reason)]
            logger.info(
                "[OOPS] not a blunder: %s hit our %s, but the autopilot did not choose that target "
                "or it can be deliberate",
                fact.get("source_name"),
                fact.get("target_name"),
            )
            return []
        if kind != "combat" or fact.get("step") != "Step_CombatDamage":
            return []
        found = []
        turn_facts = [f for f in self._facts if f.get("turn") == fact.get("turn") and same_game(f, fact)]
        for record in evidence.get("attacks") or []:
            if record.get("turn") != fact.get("turn") or not same_game(record, fact):
                continue
            for attacker, reason in attack_blunders(fact, record, turn_facts):
                found.append((ATTACK_BLUNDER, (fact.get("turn"), attacker), reason))
        return found

    def _consider(self, incident: _Incident, state: dict, now: float) -> None:
        if now - incident.due_at > STALE_S:
            self._log_decision("suppressed", incident, f"stale ({now - incident.reported_at:.0f}s old)")
            return
        if incident.kind == STUCK_MANUAL and not self._safe(lambda: self._still_stuck(state), False):
            self._log_decision("suppressed", incident, f"resolved within {MANUAL_DWELL_S:.0f}s")
            return
        why = self._gate(state, incident.game_key)
        if why:
            self._log_decision("suppressed", incident, why)
            return
        game_key = incident.game_key
        previous: tuple[float | None, float | None] = (None, None)
        with self._lock:
            handled_key = _handled_id(game_key, incident.kind, incident.key)
            if handled_key in self._handled:
                why = "already handled this incident"
            elif game_key in self._wheel_missing:
                why = f"no Oops on this deck's emote wheel ({self._wheel_missing[game_key]})"
            elif self._sent.get(game_key, 0) >= MAX_PER_GAME:
                why = f"already sent {MAX_PER_GAME} this game"
            elif self._in_flight:
                why = "another Oops is being sent"
            elif self._last_sent_at is not None and now - self._last_sent_at < MIN_GAP_S:
                why = f"only {now - self._last_sent_at:.0f}s since the last Oops (minimum {MIN_GAP_S:.0f}s)"
            if why:
                _remember(self._handled, handled_key, "suppressed")
            else:
                _remember(self._handled, handled_key, "sending")
                _remember(self._sent, game_key, self._sent.get(game_key, 0) + 1)
                previous = (self._last_sent_at, self._last_sent_wall)
                self._last_sent_at, self._last_sent_wall = now, self._wall_clock()
                self._in_flight = True
        if why:
            self._log_decision("suppressed", incident, why)
            return
        logger.info("[OOPS] sending for %s: %s", incident.kind, incident.detail)
        try:
            self._start_thread(self._send, incident, state, previous)
        except Exception as error:
            with self._lock:
                self._in_flight = False
                self._sent[game_key] = max(0, self._sent.get(game_key, 1) - 1)
                self._last_sent_at, self._last_sent_wall = previous
            self._log_decision("failed", incident, f"could not start the send ({error})")

    def _send(self, incident: _Incident, state: dict, previous: tuple[float | None, float | None]) -> None:
        try:
            try:
                result = self._emote_fn(state)
            except Exception as error:  # the bridge raised after it may have sent: count it
                result = {"ok": False, "outcome_unknown": True, "error": str(error)}
            if not isinstance(result, dict):
                result = {"ok": bool(result)}
            error = str(result.get("error") or "")
            if result.get("ok"):
                self._finish(incident, "sent", previous, sent=True)
                return
            if result.get("outcome_unknown"):
                self._finish(incident, "outcome_unknown", previous, sent=True, why=f"{error} (not retrying)")
                return
            with self._lock:
                if result.get("unsupported"):
                    self._bridge_unsupported = True
                if "emote wheel" in error.lower():
                    _remember(self._wheel_missing, incident.game_key, error, limit=16)
            self._finish(incident, "failed", previous, sent=False, why=f"nothing sent: {error or 'refused'}")
        except Exception as error:
            logger.warning("[OOPS] send failed: %s", error, exc_info=True)
            self._finish(incident, "failed", previous, sent=False, why=f"internal error: {error}")

    def _finish(
        self,
        incident: _Incident,
        outcome: str,
        previous: tuple[float | None, float | None],
        *,
        sent: bool,
        why: str = "",
    ) -> None:
        with self._lock:
            self._in_flight = False
            _remember(self._handled, _handled_id(incident.game_key, incident.kind, incident.key), outcome)
            if not sent:
                # Nothing reached the opponent: free the per-game slot and the gap.
                self._sent[incident.game_key] = max(0, self._sent.get(incident.game_key, 1) - 1)
                self._last_sent_at, self._last_sent_wall = previous
        self._log_decision(outcome, incident, why)

    # -- diagnostics and engine reloads -------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            return {
                "oops_emote": self.enabled(),
                "game_key": repr(self._game_key),
                "sent_this_game": int(self._sent.get(self._game_key, 0)),
                "max_per_game": MAX_PER_GAME,
                "last_sent_s_ago": None if self._last_sent_at is None else round(now - self._last_sent_at, 1),
                "min_gap_s": MIN_GAP_S,
                "in_flight": self._in_flight,
                "bridge_unsupported": self._bridge_unsupported,
                "wheel_missing": self._wheel_missing.get(self._game_key),
                "pending": [
                    {
                        "kind": i.kind,
                        "turn": i.turn,
                        "due_in_s": round(i.due_at - now, 1),
                        "detail": i.detail[:160],
                    }
                    for i in self._pending
                ],
                "recent": list(self._decisions),
            }

    def export_state(self) -> dict[str, Any] | None:
        """This game's Oops record, for an engine reload."""
        with self._lock:
            if not self._match_id:
                return None
            return {
                "match_id": self._match_id,
                "turn": self._turn,
                "sent": int(self._sent.get(self._game_key, 0)),
                "last_sent_wall": self._last_sent_wall,
                "wheel_missing": self._wheel_missing.get(self._game_key),
                "handled": [k[1] for k in self._handled if k[0] == self._game_key],
            }

    def resume_from(self, saved: Any) -> None:
        """Apply a reloaded engine's record to the first observation of the same game."""
        self._resume = dict(saved) if isinstance(saved, dict) and saved.get("match_id") else None

    def _apply_resume(self, game_key: Any, match_id: Any, turn: int) -> None:
        saved = self._resume
        if saved is None or not match_id or turn <= 0:
            return
        self._resume = None
        if match_id != saved.get("match_id") or turn < _int(saved.get("turn")):
            logger.info("[OOPS] reload record discarded: the game changed")
            return
        with self._lock:
            sent = _int(saved.get("sent"))
            if sent:
                _remember(self._sent, game_key, max(sent, self._sent.get(game_key, 0)))
            wall = saved.get("last_sent_wall")
            if isinstance(wall, (int, float)):
                elapsed = max(0.0, self._wall_clock() - float(wall))
                self._last_sent_at = self._clock() - elapsed
                self._last_sent_wall = float(wall)
            if saved.get("wheel_missing"):
                _remember(self._wheel_missing, game_key, str(saved["wheel_missing"]), limit=16)
            for key in saved.get("handled") or []:
                _remember(self._handled, (game_key, str(key)), "restored")
        logger.info(
            "[OOPS] restored after the engine reload (game %r): %d sent", game_key, _int(saved.get("sent"))
        )
