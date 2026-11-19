"""Extensible 1-Ply Synergy Afterstate Engine for MTGA Coach.

Simulates 1-ply action resolution and cascading triggers (ETB tokens, Food sacrifices,
commander zone re-routing) over lightweight immutable BoardState views, and evaluates
true value deltas via uniform V(s) scoring without arbitrary additive constants.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal

import arenamcp.card_facts.client as _cf_client
from arenamcp.ability_synthesizer import (
    AbilitySynthesizer,
    Cost,
    CreateToken,
    Damage,
    Draw,
    Effect,
    GainLife,
    Pump,
    PutCounters,
    TokenSpec,
    TriggeredAbility,
    UnknownEffect,
)
from arenamcp.card_facts.schema import CardFacts, card_facts_from_dict
from arenamcp.card_facts.source_identity import single_face_source
from arenamcp.format_profile import FormatEvaluatorConfig

logger = logging.getLogger(__name__)


# ── Fail-Closed CardFacts Trigger Extraction ─────────────────────────────────

_ONCE_PER_TURN_RE = re.compile(r"^this ability triggers only once (?:each|per) turn\.?$", re.IGNORECASE)
_ENCHANTMENT_ETB_RE = re.compile(
    r"^(?:(?:eerie|constellation)\s*[—–-]\s*)?"
    r"whenever (?:(?P<self>this (?:creature|enchantment|permanent)|~) or )?"
    r"(?P<another>an|another) enchantment you control enters(?: the battlefield)?"
    r"(?:\s+and whenever you fully unlock a room)?\.?$",
    re.IGNORECASE,
)
_ENCHANTMENT_CAST_RE = re.compile(r"^whenever you cast an enchantment spell\.?$", re.IGNORECASE)
_SPELL_CAST_RE = re.compile(
    r"^(?:prowess\s*[—–-]\s*)?"
    r"whenever you cast (?:or copy )?(?:a|an) "
    r"(?:(?P<kind>noncreature|instant or sorcery|instant|sorcery|creature) )?"
    r"spell(?: from your hand)?\.?$",
    re.IGNORECASE,
)
_LANDFALL_RE = re.compile(
    r"^(?:landfall\s*[—–-]\s*)?"
    r"whenever a land you control enters(?: the battlefield)?(?:\s*\(landfall\))?\.?$",
    re.IGNORECASE,
)
_CREATURE_ETB_RE = re.compile(
    r"^whenever (?:(?P<self>this (?:creature|permanent)|~) or )?"
    r"(?P<another>a|an|another) (?P<nontoken>nontoken )?creature you control enters(?: the battlefield)?\.?$",
    re.IGNORECASE,
)
_CREATURE_DIES_RE = re.compile(
    r"^whenever (?P<another>a|another) creature"
    r"(?: (?P<scope>you control|an opponent controls|you don't control))? dies\.?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class VerifiedTrigger:
    """A fail-closed, bounded permanent/spell trigger verified from CardFacts."""

    event: str
    effect: Effect
    spell_filter: str = ""
    include_self: bool = False
    nontoken: bool = False
    once_per_turn: bool = False
    controller_scope: str = "ours"
    target_hint: str = ""


def has_unknown_fields(value: Any) -> bool:
    """True when ``value`` or any nested dataclass/container carries unknown schema keys."""
    if getattr(value, "_unknown_fields", None):
        return True
    if dataclasses.is_dataclass(value):
        return any(has_unknown_fields(getattr(value, f.name)) for f in dataclasses.fields(value))
    if isinstance(value, (tuple, list)):
        return any(has_unknown_fields(item) for item in value)
    if isinstance(value, dict):
        return any(has_unknown_fields(item) for item in value.values())
    return False


def coerce_card_facts(record: Any) -> CardFacts | None:
    """Convert dict/CardFacts into CardFacts, or None if malformed."""
    if record is None:
        return None
    if isinstance(record, CardFacts):
        return record
    if isinstance(record, dict):
        raw = (
            record.get("record") if "record" in record and isinstance(record.get("record"), dict) else record
        )
        try:
            return card_facts_from_dict(raw)
        except Exception:
            return None
    return None


def is_verified_card_facts(record: Any, card: dict[str, Any] | None = None) -> bool:
    """Strict fail-closed gate for CardFacts records before any trigger/effect use."""
    rec = coerce_card_facts(record)
    if rec is None or has_unknown_fields(rec):
        return False
    if rec.threat or rec.relationships or rec.limited or rec.notes:
        return False
    if rec.confidence not in ("", "high"):
        return False
    if (rec.compiled_by or rec.compiled_at) and rec.confidence != "high":
        return False
    if any(isinstance(entry.effect, UnknownEffect) for entry in rec.effects):
        return False
    if card is not None:
        if not single_face_source(card):
            return False
        cname = str(card.get("name") or "").strip()
        if rec.name and cname and rec.name != cname:
            return False
        tl = str(card.get("type_line") or "").strip()
        if tl and rec.type_line and rec.type_line != tl:
            return False
        if "mana_cost" in card and rec.mana_cost != str(card.get("mana_cost") or ""):
            return False
    return True


def _classify_trigger_spec(
    trigger_raw: Any, conditions: tuple[str, ...], card_name: str
) -> tuple[str, str, bool, bool, bool, str] | None:
    """Map an exact canonical or verified trigger phrase, else None."""
    once_per_turn = False
    if conditions:
        if len(conditions) == 1 and _ONCE_PER_TURN_RE.fullmatch(" ".join(str(conditions[0]).split())):
            once_per_turn = True
        else:
            return None

    t = " ".join(str(trigger_raw or "").strip().casefold().split())
    if not t:
        return None

    # 1. Constellation / Eerie / Enchantment-ETB
    if t in ("eerie", "constellation", "enchantment_etb", "enchantment_enters"):
        return ("ENCHANTMENT_ENTERS", "", False, False, once_per_turn, "ours")
    if m := _ENCHANTMENT_ETB_RE.fullmatch(t):
        return ("ENCHANTMENT_ENTERS", "", bool(m.group("self")), False, once_per_turn, "ours")
    if _ENCHANTMENT_CAST_RE.fullmatch(t):
        return ("CAST_ENCHANTMENT", "", False, False, once_per_turn, "ours")

    # 2. Spell-cast / Prowess / Noncreature-cast
    if t in ("prowess", "cast_noncreature", "noncreature_cast"):
        return ("CAST_SPELL", "noncreature", False, False, once_per_turn, "ours")
    if t in ("cast_instant_or_sorcery", "magecraft"):
        return ("CAST_SPELL", "instant_or_sorcery", False, False, once_per_turn, "ours")
    if t == "cast_spell":
        return ("CAST_SPELL", "any", False, False, once_per_turn, "ours")
    if m := _SPELL_CAST_RE.fullmatch(t):
        kind = (m.group("kind") or "any").replace(" ", "_")
        return ("CAST_SPELL", kind, False, False, once_per_turn, "ours")

    # 3. Landfall
    if t in ("landfall", "land_etb", "land_enters"):
        return ("LANDFALL", "", False, False, once_per_turn, "ours")
    if _LANDFALL_RE.fullmatch(t):
        return ("LANDFALL", "", False, False, once_per_turn, "ours")

    # 4. Creature ETB / Dies
    name_low = card_name.strip().casefold()
    short_low = name_low.split(",", 1)[0].strip() if "," in name_low else ""
    self_names = {
        "this creature",
        "this permanent",
        "this artifact",
        "this enchantment",
        "this card",
        "~",
        *(n for n in (name_low, short_low) if n),
    }
    if t in ("etb", "enters", "enters_the_battlefield", "enters_the_battlefield_or_evoke"):
        return ("SELF_ETB", "", True, False, once_per_turn, "ours")
    if m := re.fullmatch(r"when (?:(?P<who>.+?) )enters(?: the battlefield)?\.?", t):
        if m.group("who").strip() in self_names:
            return ("SELF_ETB", "", True, False, once_per_turn, "ours")
    if t in ("another_creature_etb", "creature_etb"):
        return ("CREATURE_ENTERS", "", False, False, once_per_turn, "ours")
    if m := _CREATURE_ETB_RE.fullmatch(t):
        return (
            "CREATURE_ENTERS",
            "",
            bool(m.group("self")),
            bool(m.group("nontoken")),
            once_per_turn,
            "ours",
        )
    if t == "dies":
        return ("SELF_DIES", "", True, False, once_per_turn, "ours")
    if m := re.fullmatch(r"when(?:ever)? (?P<who>.+?) dies\.?", t):
        if m.group("who").strip() in self_names:
            return ("SELF_DIES", "", True, False, once_per_turn, "ours")
    if t in ("another_creature_dies", "creature_dies"):
        return ("CREATURE_DIES", "", False, False, once_per_turn, "any")
    if m := _CREATURE_DIES_RE.fullmatch(t):
        scope_raw = m.group("scope") or ""
        scope = "ours" if scope_raw == "you control" else "theirs" if scope_raw else "any"
        inc_self = m.group("another") == "a" and scope != "theirs"
        return ("CREATURE_DIES", "", inc_self, False, once_per_turn, scope)

    return None


def _bounded_trigger_effect(effect: Effect, rec: CardFacts, oracle_text: str) -> tuple[Effect, str] | None:
    """Validate that an effect is strictly bounded and return (effect, target_hint)."""
    oracle_low = re.sub(r"<[^>]*>", "", oracle_text or "").casefold()
    if isinstance(effect, PutCounters):
        if effect.counter_type != "+1/+1" or type(effect.count) is not int or not 0 < effect.count <= 10:
            return None
        self_only = (
            "this creature" in oracle_low or "~" in oracle_low
        ) and "target creature" not in oracle_low
        return effect, ("self" if self_only else "target_ours")

    if isinstance(effect, Pump):
        if (
            type(effect.power) is not int
            or type(effect.toughness) is not int
            or not -10 <= effect.power <= 10
            or not -10 <= effect.toughness <= 10
            or (effect.power <= 0 and effect.toughness <= 0)
        ):
            return None
        target = " ".join(str(effect.target or "").casefold().split())
        name_low = rec.name.strip().casefold()
        if target in ("", "self", "this creature", "this permanent", "~", name_low):
            return effect, "self"
        if target in (
            "creatures you control",
            "each creature you control",
            "all creatures you control",
            "team",
        ):
            return effect, "team"
        if target in ("other creatures you control", "others"):
            return effect, "others"
        if target in (
            "target creature",
            "target creature you control",
            "another target creature",
            "another target creature you control",
        ):
            return effect, "target_ours"
        return None

    if isinstance(effect, CreateToken):
        spec = effect.spec
        if (
            type(spec.count) is not int
            or not 0 < spec.count <= 10
            or spec.per_opponent
            or spec.copy_of_self
            or spec.attacking
        ):
            return None
        is_creature_tok = "creature" in [t.casefold() for t in (spec.types or ())]
        if is_creature_tok and (
            type(spec.power) is not int
            or type(spec.toughness) is not int
            or not 0 <= spec.power <= 20
            or not 0 <= spec.toughness <= 20
        ):
            return None
        return effect, "create_token"

    if isinstance(effect, GainLife):
        if type(effect.amount) is not int or not 0 < effect.amount <= 20:
            return None
        return effect, "you"

    if isinstance(effect, Damage):
        if type(effect.amount) is not int or not 0 < effect.amount <= 20:
            return None
        target = " ".join(str(effect.target or "").casefold().split())
        if target in ("each opponent", "target opponent", "target player", "opponent"):
            return effect, "opponent"
        if target == "each player":
            return effect, "each_player"
        if target in ("any target", "any other target"):
            return effect, "any_target"
        if target in (
            "target creature",
            "target creature an opponent controls",
            "target creature you don't control",
            "up to one target creature",
        ):
            return effect, "target_creature"
        if target in (
            "each creature your opponents control",
            "each creature an opponent controls",
            "each creature they control",
        ):
            return effect, "opp_creatures"
        return None

    if isinstance(effect, Draw):
        if type(effect.count) is not int or not 0 < effect.count <= 5:
            return None
        return effect, "you"

    return None


def extract_verified_triggers(record: Any, card: dict[str, Any] | None = None) -> tuple[VerifiedTrigger, ...]:
    """Fail-closed extraction of turn-bounded permanent triggers from a CardFacts record.

    Abstains completely (returns ``()``) on ``None``, ``UnknownEffect``,
    ``_unknown_fields``, non-high compiled confidence, unrepresentable trigger
    phrases/conditions, or unbounded effect parameters.
    """
    rec = coerce_card_facts(record)
    if not is_verified_card_facts(rec, card):
        return ()
    assert rec is not None
    oracle_text = str((card or {}).get("oracle_text") or "")
    out: list[VerifiedTrigger] = []
    has_noncreature_pump = False

    for entry in rec.effects:
        trig_raw = str(entry.trigger or "").strip()
        trig_low = trig_raw.casefold()
        if trig_low in ("", "cast", "resolves", "static", "activated", "activate"):
            continue
        spec = _classify_trigger_spec(trig_raw, tuple(entry.conditions or ()), rec.name)
        if spec is None:
            return ()  # fail closed if any triggered entry cannot be safely represented
        bounded = _bounded_trigger_effect(entry.effect, rec, oracle_text)
        if bounded is None:
            return ()
        eff, target_hint = bounded
        event, spell_filter, include_self, nontoken, once_per_turn, controller_scope = spec
        if (
            event == "CAST_SPELL"
            and spell_filter == "noncreature"
            and isinstance(eff, Pump)
            and target_hint == "self"
        ):
            has_noncreature_pump = True
        out.append(
            VerifiedTrigger(
                event=event,
                effect=eff,
                spell_filter=spell_filter,
                include_self=include_self,
                nontoken=nontoken,
                once_per_turn=once_per_turn,
                controller_scope=controller_scope,
                target_hint=target_hint,
            )
        )

    if not has_noncreature_pump and any(str(k).strip().casefold() == "prowess" for k in rec.body.keywords):
        out.append(
            VerifiedTrigger(
                event="CAST_SPELL",
                effect=Pump(power=1, toughness=1, target="self"),
                spell_filter="noncreature",
                target_hint="self",
            )
        )

    return tuple(out)


def lookup_verified_card_facts(card: dict[str, Any]) -> CardFacts | None:
    """Look up a verified CardFacts record for ``card`` from client cache (never raises)."""
    if not isinstance(card, dict) or not card.get("name") or not single_face_source(card):
        return None
    digest = _cf_client.hash_for_card(card)
    if not digest:
        return None
    data = _cf_client.facts_for([card]).get(digest)
    if data is None:
        return None
    rec = coerce_card_facts(data)
    return rec if is_verified_card_facts(rec, card) else None


# ── Action Hierarchy ──


@dataclass(frozen=True)
class Action:
    action_type: str = ""


@dataclass(frozen=True)
class PlayLand(Action):
    land_name: str = ""
    action_type: str = "land"


@dataclass(frozen=True)
class CastSpell(Action):
    card_name: str = ""
    from_zone: Literal["hand", "command"] = "hand"
    mana_cost: str = ""
    cmc: int = 0
    is_commander: bool = False
    oracle_text: str = ""
    action_type: str = "cast"
    type_line: str = ""
    card_types: tuple[str, ...] = ()
    power: int = 0
    toughness: int = 0
    card_facts: Any = None


@dataclass(frozen=True)
class ActivateAbility(Action):
    permanent_name: str = ""
    ability_text: str = ""
    cost: Cost = field(default_factory=Cost)
    is_food_sacrifice: bool = False
    action_type: str = "activate"


@dataclass(frozen=True)
class DeclareAttack(Action):
    attackers: tuple[str, ...] = ()
    expected_damage: int = 0
    action_type: str = "attack"


@dataclass(frozen=True)
class PassTurn(Action):
    action_type: str = "pass"


# ── BoardState & Deltas ──


@dataclass(frozen=True)
class Permanent:
    """A permanent on the battlefield."""

    name: str
    power: int = 0
    toughness: int = 0
    is_tapped: bool = False
    is_token: bool = False
    is_commander: bool = False
    subtypes: tuple[str, ...] = ()
    abilities: tuple[Any, ...] = ()
    oracle_text: str = ""
    is_land: bool = False
    type_line: str = ""
    card_types: tuple[str, ...] = ()
    mana_cost: str = ""
    verified_triggers: tuple[VerifiedTrigger, ...] = ()


@dataclass
class Delta:
    """Metrics tracking change between pre- and post-action state."""

    bodies_added: int = 0
    power_added: int = 0
    tokens_created: Counter[str] = field(default_factory=Counter)
    tokens_sacrificed: Counter[str] = field(default_factory=Counter)
    life_gained: int = 0
    damage_dealt: int = 0
    mana_spent: int = 0
    commander_to_zone: int = 0
    counters_added: int = 0
    cards_drawn: int = 0


@dataclass
class Afterstate:
    """The fully resolved board state resulting from a 1-ply hero action."""

    board: BoardState
    delta: Delta
    trigger_trace: list[str] = field(default_factory=list)
    unmodeled: list[str] = field(default_factory=list)
    depth: int = 0


@dataclass(frozen=True)
class BoardState:
    """Immutable view of game state for 1-ply tactical simulations."""

    hero_life: int = 20
    opp_life: int = 20
    available_mana: int = 0
    lands_played_this_turn: int = 0
    permanents: tuple[Permanent, ...] = ()
    hand: tuple[str, ...] = ()
    command: tuple[str, ...] = ()
    graveyard: tuple[str, ...] = ()
    commander_casts: int = 0
    config: FormatEvaluatorConfig = field(default_factory=FormatEvaluatorConfig)
    draws: int = 0

    @property
    def token_counts(self) -> Counter[str]:
        return Counter(p.name for p in self.permanents if p.is_token)

    @property
    def hero_creatures(self) -> list[Permanent]:
        return [p for p in self.permanents if p.power > 0 or p.toughness > 0]

    @property
    def total_hero_power(self) -> int:
        return sum(p.power for p in self.hero_creatures)

    @classmethod
    def from_game_state(
        cls, game_state: dict[str, Any], config: FormatEvaluatorConfig | None = None
    ) -> BoardState:
        """Create a BoardState view from a MTGA game_state dict."""
        local_seat = int(game_state.get("local_seat_id") or 1)
        players = game_state.get("players") or []
        hero_p = next((p for p in players if p.get("is_local") or p.get("seat_id") == local_seat), {})
        opp_p = next((p for p in players if not p.get("is_local") and p.get("seat_id") != local_seat), {})

        hero_life = int(hero_p.get("life_total") or 20)
        opp_life = int(opp_p.get("life_total") or 20)
        lands_played = int(hero_p.get("lands_played") or 0)

        # Available mana
        mana_pool = hero_p.get("mana_pool") or {}
        available_mana = sum(int(v) for v in mana_pool.values()) if isinstance(mana_pool, dict) else 0

        # Untapped lands
        bf_cards = game_state.get("battlefield") or []
        for c in bf_cards:
            if isinstance(c, dict) and c.get("controller_seat_id") == local_seat:
                t_line = str(c.get("type_line") or "").lower()
                if "land" in t_line and not c.get("is_tapped"):
                    available_mana += 1

        # Permanents
        perms: list[Permanent] = []
        for c in bf_cards:
            if isinstance(c, dict) and c.get("controller_seat_id") == local_seat:
                name = str(c.get("name") or "Permanent").strip()
                p = int(c.get("power") or 0)
                t = int(c.get("toughness") or 0)
                is_tap = bool(c.get("is_tapped"))
                t_line = str(c.get("type_line") or "")
                is_tok = bool(c.get("is_token") or "token" in t_line.lower())
                oracle = str(c.get("oracle_text") or "")
                parsed_abs = AbilitySynthesizer.parse_card(name, oracle)
                subtypes = tuple(t_line.split("—")[-1].split()) if "—" in t_line else ()
                rec = lookup_verified_card_facts(c)
                v_trigs = extract_verified_triggers(rec, c) if rec is not None else ()
                perms.append(
                    Permanent(
                        name=name,
                        power=p,
                        toughness=t,
                        is_tapped=is_tap,
                        is_token=is_tok,
                        subtypes=subtypes,
                        abilities=() if (rec is not None and v_trigs) else parsed_abs.abilities,
                        oracle_text=oracle,
                        is_land="land" in t_line.lower(),
                        type_line=t_line,
                        card_types=tuple(str(ct) for ct in c.get("card_types") or ()),
                        mana_cost=str(c.get("mana_cost") or ""),
                        verified_triggers=v_trigs,
                    )
                )

        hand_cards = tuple(
            str(c.get("name") or "")
            for c in (game_state.get("hand") or [])
            if isinstance(c, dict) and c.get("name")
        )
        command_cards = tuple(
            str(c.get("name") or "")
            for c in (game_state.get("command") or [])
            if isinstance(c, dict) and c.get("name")
        )

        cfg = config or FormatEvaluatorConfig()
        casts = (
            int((game_state.get("command") or [{}])[0].get("commander_casts") or 0) if command_cards else 0
        )

        return cls(
            hero_life=hero_life,
            opp_life=opp_life,
            available_mana=available_mana,
            lands_played_this_turn=lands_played,
            permanents=tuple(perms),
            hand=hand_cards,
            command=command_cards,
            commander_casts=casts,
            config=cfg,
        )


# ── Uniform Value Function V(s) ──


def score_board_state(board: BoardState, config: FormatEvaluatorConfig | None = None) -> float:
    """Uniform root and afterstate value function V(s) in [0.05, 0.95]."""
    cfg = config or board.config

    # 1. Life advantage term
    life_delta = (board.hero_life - board.opp_life) / float(cfg.life_norm)
    life_adv = max(-1.0, min(1.0, life_delta)) * 0.22
    if board.opp_life <= cfg.lethal_zone:
        life_adv += max(0.0, ((cfg.lethal_zone + 1) - board.opp_life) * 0.05)
    if board.hero_life <= cfg.lethal_zone:
        life_adv -= max(0.0, ((cfg.lethal_zone + 1) - board.hero_life) * 0.03)

    # 2. Board power & presence term
    power_delta = board.total_hero_power / float(cfg.board_norm)
    board_adv = max(-0.20, min(0.35, power_delta * 0.20))
    creature_count = len(board.hero_creatures)
    board_adv += max(-0.10, min(0.15, (creature_count - 2) * 0.04))

    # 3. Token & food synergy reserves
    food_count = board.token_counts.get("Food", 0)
    food_reserve = min(4, food_count) * (0.02 * cfg.token_synergy_weight)

    # 4. Hand advantage (includes safe draw count tracking without inventing cards)
    hand_adv = max(-0.15, min(0.15, (len(board.hand) + board.draws - 3) * 0.04))

    # 5. Mana development. Without it a land drop only registered as a card
    # leaving hand (-0.04), so "Pass" outscored "Play Forest" every turn.
    # Worth more than a hand card up to ~6 lands, then flattens (flood).
    land_count = sum(1 for p in board.permanents if p.is_land)
    # Centred on 3 lands so absolute values don't saturate the 0.95 clip.
    mana_dev = (min(land_count, 6) - 3) * 0.05 + max(0, land_count - 6) * 0.01

    val = 0.50 + life_adv + board_adv + food_reserve + hand_adv + mana_dev
    return max(0.05, min(0.95, round(val, 3)))


# ── Simulator & Trigger Cascade ──


def _spell_kind_tags(type_line: str, card_types: tuple[str, ...]) -> frozenset[str]:
    tl = f"{type_line} {' '.join(card_types)}".casefold()
    if "creature" in tl:
        return frozenset({"any", "creature"})
    tags = {"any", "noncreature"}
    for kind in ("instant", "sorcery"):
        if kind in tl:
            tags.add(kind)
            tags.add("instant_or_sorcery")
    return frozenset(tags)


class AfterstateSimulator:
    """Executes actions and cascades triggers to generate verified afterstates."""

    MAX_CASCADE_DEPTH = 3
    MAX_FANOUT = 16

    @classmethod
    def apply(cls, board: BoardState, action: Action) -> Afterstate:
        """Apply an action to board state, cascading triggers."""
        delta = Delta()
        trigger_trace: list[str] = []
        unmodeled: list[str] = []

        new_perms = list(board.permanents)
        new_hand = list(board.hand)
        new_command = list(board.command)
        state_acc = {
            "hero_life": board.hero_life,
            "opp_life": board.opp_life,
            "draws": board.draws,
            "used_once": set(),
        }
        mana_left = board.available_mana
        lands_played = board.lands_played_this_turn
        cmd_casts = board.commander_casts

        events_queue: list[tuple[str, Any]] = []

        # 1. Action direct effects
        if isinstance(action, PlayLand):
            if action.land_name in new_hand:
                new_hand.remove(action.land_name)
            lands_played += 1
            mana_left += 1  # Land drops provide mana for the turn
            land_perm = Permanent(
                name=action.land_name,
                is_tapped=False,
                subtypes=("Land",),
                is_land=True,
                type_line="Land",
            )
            new_perms.append(land_perm)
            delta.bodies_added += 1
            trigger_trace.append(f"Play {action.land_name} (mana +1)")
            events_queue.append(("ENTERS", land_perm))
            events_queue.append(("LANDFALL", land_perm))

        elif isinstance(action, CastSpell):
            card_name = action.card_name
            if action.from_zone == "command":
                if card_name in new_command:
                    new_command.remove(card_name)
                cmd_casts += 1
            elif card_name in new_hand:
                new_hand.remove(card_name)

            mana_left = max(0, mana_left - action.cmc)
            delta.mana_spent += action.cmc

            oracle = getattr(action, "oracle_text", "")
            type_line = str(getattr(action, "type_line", "") or "")
            card_types = tuple(getattr(action, "card_types", ()) or ())
            card_dict = {
                "name": card_name,
                "oracle_text": oracle,
                "type_line": type_line,
                "mana_cost": action.mana_cost,
                "card_types": list(card_types),
            }
            raw_facts = getattr(action, "card_facts", None)
            rec = (
                coerce_card_facts(raw_facts)
                if raw_facts is not None
                else lookup_verified_card_facts(card_dict)
            )
            verified_rec = rec if is_verified_card_facts(rec, card_dict) else None
            v_triggers = extract_verified_triggers(verified_rec, card_dict) if verified_rec else ()

            tl_low = f"{type_line} {' '.join(card_types)}".casefold()
            is_spell_only = (
                bool(type_line or card_types)
                and ("instant" in tl_low or "sorcery" in tl_low)
                and "creature" not in tl_low
            )
            is_enchantment = "enchantment" in tl_low
            is_creature = (not type_line and not card_types) or ("creature" in tl_low)

            trigger_trace.append(f"Cast {card_name}")

            # Cast triggers fire on permanents ALREADY on the battlefield before resolution
            spell_tags = _spell_kind_tags(type_line, card_types)
            cls._fire_cast_triggers(
                list(range(len(new_perms))),
                spell_tags,
                is_enchantment,
                new_perms,
                delta,
                trigger_trace,
                events_queue,
                unmodeled,
                state_acc,
            )

            if verified_rec is not None:
                # Resolve direct CAST / STATIC Aura effects from verified CardFacts
                cls._resolve_spell_card_facts(
                    verified_rec,
                    card_name,
                    is_spell_only,
                    new_perms,
                    delta,
                    trigger_trace,
                    events_queue,
                    unmodeled,
                    state_acc,
                )

            if not is_spell_only:
                if is_creature:
                    if action.power > 0 or action.toughness > 0:
                        power, toughness = action.power, action.toughness
                    elif (
                        verified_rec is not None
                        and verified_rec.body.power
                        and verified_rec.body.power.isdigit()
                    ):
                        power = int(verified_rec.body.power)
                        toughness = (
                            int(verified_rec.body.toughness)
                            if (verified_rec.body.toughness or "").isdigit()
                            else power
                        )
                    else:
                        power, toughness = 2, 2
                else:
                    power, toughness = action.power, action.toughness

                # Avoid double-counting ETB when verified CardFacts already carries triggers
                parsed_abilities = (
                    ()
                    if verified_rec is not None
                    else AbilitySynthesizer.parse_card(card_name, oracle).abilities
                )
                subtypes = tuple(type_line.split("—")[-1].split()) if "—" in type_line else ()

                new_perm = Permanent(
                    name=card_name,
                    power=power,
                    toughness=toughness,
                    is_commander=action.is_commander,
                    subtypes=subtypes,
                    abilities=parsed_abilities,
                    oracle_text=oracle,
                    type_line=type_line,
                    card_types=card_types,
                    mana_cost=action.mana_cost,
                    verified_triggers=v_triggers,
                )
                new_perms.append(new_perm)
                delta.bodies_added += 1
                delta.power_added += power

                events_queue.append(("ENTERS", new_perm))
                if is_creature:
                    events_queue.append(("ANOTHER_CREATURE_ENTERS", new_perm))
                    events_queue.append(("CREATURE_ENTERS", new_perm))
                if is_enchantment:
                    events_queue.append(("ENCHANTMENT_ENTERS", new_perm))

        elif isinstance(action, ActivateAbility):
            if action.is_food_sacrifice:
                # Sacrifice 1 Food -> gain 3 life
                food_idx = next((i for i, p in enumerate(new_perms) if p.name == "Food"), None)
                if food_idx is not None:
                    new_perms.pop(food_idx)
                    delta.tokens_sacrificed["Food"] += 1
                    state_acc["hero_life"] += 3
                    delta.life_gained += 3
                    mana_left = max(0, mana_left - 2)
                    trigger_trace.append("Sacrifice Food → Gain 3 Life")
                    events_queue.append(("LIFE_GAINED", 3))

        elif isinstance(action, DeclareAttack):
            state_acc["opp_life"] = max(0, state_acc["opp_life"] - action.expected_damage)
            delta.damage_dealt += action.expected_damage
            trigger_trace.append(
                f"Attack with {', '.join(action.attackers)} (-{action.expected_damage} Opp Life)"
            )

        # 2. Trigger resolution loop
        depth = 0
        while events_queue and depth < cls.MAX_CASCADE_DEPTH:
            depth += 1
            batch = list(events_queue)
            events_queue.clear()
            for curr_event, obj in batch:
                if len(new_perms) >= cls.MAX_FANOUT + len(board.permanents):
                    break
                cls._dispatch_event(
                    curr_event,
                    obj,
                    new_perms,
                    delta,
                    trigger_trace,
                    events_queue,
                    unmodeled,
                    state_acc,
                )

        new_board = BoardState(
            hero_life=state_acc["hero_life"],
            opp_life=state_acc["opp_life"],
            available_mana=mana_left,
            lands_played_this_turn=lands_played,
            permanents=tuple(new_perms),
            hand=tuple(new_hand),
            command=tuple(new_command),
            commander_casts=cmd_casts,
            config=board.config,
            draws=state_acc["draws"],
        )

        return Afterstate(
            board=new_board,
            delta=delta,
            trigger_trace=trigger_trace,
            unmodeled=unmodeled,
            depth=depth,
        )

    @classmethod
    def _fire_cast_triggers(
        cls,
        eligible_indices: list[int],
        spell_tags: frozenset[str],
        is_enchantment: bool,
        new_perms: list[Permanent],
        delta: Delta,
        trace: list[str],
        events_queue: list[tuple[str, Any]],
        unmodeled: list[str],
        state_acc: dict[str, Any],
    ) -> None:
        for idx in eligible_indices:
            perm = new_perms[idx]
            for t_idx, vt in enumerate(perm.verified_triggers):
                if (vt.event == "CAST_SPELL" and vt.spell_filter in spell_tags) or (
                    vt.event == "CAST_ENCHANTMENT" and is_enchantment
                ):
                    cls._apply_verified_trigger(
                        vt, idx, t_idx, new_perms, delta, trace, events_queue, unmodeled, state_acc
                    )

    @classmethod
    def _resolve_spell_card_facts(
        cls,
        rec: CardFacts,
        card_name: str,
        is_spell_only: bool,
        new_perms: list[Permanent],
        delta: Delta,
        trace: list[str],
        events_queue: list[tuple[str, Any]],
        unmodeled: list[str],
        state_acc: dict[str, Any],
    ) -> None:
        is_aura = "aura" in rec.type_line.casefold()
        for entry in rec.effects:
            trig = str(entry.trigger or "").strip().casefold()
            if entry.conditions:
                continue
            if is_spell_only and trig in ("", "cast", "resolves"):
                bounded = _bounded_trigger_effect(entry.effect, rec, "")
                if bounded is not None:
                    eff, hint = bounded
                    cls._apply_effect(
                        eff,
                        hint,
                        None,
                        card_name,
                        new_perms,
                        delta,
                        trace,
                        events_queue,
                        unmodeled,
                        state_acc,
                    )
            elif is_aura and trig == "static" and isinstance(entry.effect, Pump):
                target_norm = " ".join(str(entry.effect.target or "").casefold().split())
                if (
                    target_norm in ("enchanted creature", "enchanted creature you control")
                    and type(entry.effect.power) is int
                    and type(entry.effect.toughness) is int
                    and 0 <= entry.effect.power <= 10
                    and 0 <= entry.effect.toughness <= 10
                    and (entry.effect.power > 0 or entry.effect.toughness > 0)
                ):
                    cls._apply_effect(
                        entry.effect,
                        "target_ours",
                        None,
                        card_name,
                        new_perms,
                        delta,
                        trace,
                        events_queue,
                        unmodeled,
                        state_acc,
                    )

    @classmethod
    def _dispatch_event(
        cls,
        curr_event: str,
        obj: Any,
        new_perms: list[Permanent],
        delta: Delta,
        trace: list[str],
        events_queue: list[tuple[str, Any]],
        unmodeled: list[str],
        state_acc: dict[str, Any],
    ) -> None:
        # 1. Existing AbilitySynthesizer TriggeredAbility dispatch
        for perm in list(new_perms):
            for ab in perm.abilities:
                if not isinstance(ab, TriggeredAbility):
                    continue
                trig = ab.trigger
                if trig.event == "ETB" and curr_event == "ENTERS" and obj is perm:
                    cls._resolve_ability_effects(
                        ab, perm, new_perms, delta, trace, events_queue, unmodeled, state_acc=state_acc
                    )
                elif trig.event == "ANOTHER_CREATURE_ETB" and curr_event == "ANOTHER_CREATURE_ENTERS":
                    if trig.condition == "nontoken" and getattr(obj, "is_token", False):
                        continue
                    if obj is not perm and obj.name != perm.name:
                        cls._resolve_ability_effects(
                            ab, perm, new_perms, delta, trace, events_queue, unmodeled, state_acc=state_acc
                        )

        # 2. Verified CardFacts Trigger dispatch
        for idx in range(len(new_perms)):
            perm = new_perms[idx]
            for t_idx, vt in enumerate(perm.verified_triggers):
                if (vt.event == "SELF_ETB" and curr_event == "ENTERS" and obj is perm) or (
                    vt.event == "LANDFALL" and curr_event == "LANDFALL"
                ):
                    cls._apply_verified_trigger(
                        vt, idx, t_idx, new_perms, delta, trace, events_queue, unmodeled, state_acc
                    )
                elif vt.event == "ENCHANTMENT_ENTERS" and curr_event == "ENCHANTMENT_ENTERS":
                    if not vt.include_self and obj is perm:
                        continue
                    cls._apply_verified_trigger(
                        vt, idx, t_idx, new_perms, delta, trace, events_queue, unmodeled, state_acc
                    )
                elif vt.event == "CREATURE_ENTERS" and curr_event == "CREATURE_ENTERS":
                    if vt.nontoken and getattr(obj, "is_token", False):
                        continue
                    if not vt.include_self and obj is perm:
                        continue
                    cls._apply_verified_trigger(
                        vt, idx, t_idx, new_perms, delta, trace, events_queue, unmodeled, state_acc
                    )
                elif vt.event == "SELF_DIES" and curr_event == "DIES" and obj is perm:
                    cls._apply_verified_trigger(
                        vt, idx, t_idx, new_perms, delta, trace, events_queue, unmodeled, state_acc
                    )
                elif vt.event == "CREATURE_DIES" and curr_event == "CREATURE_DIES":
                    if not vt.include_self and obj is perm:
                        continue
                    cls._apply_verified_trigger(
                        vt, idx, t_idx, new_perms, delta, trace, events_queue, unmodeled, state_acc
                    )

    @classmethod
    def _apply_verified_trigger(
        cls,
        vt: VerifiedTrigger,
        source_idx: int,
        trig_idx: int,
        new_perms: list[Permanent],
        delta: Delta,
        trace: list[str],
        events_queue: list[tuple[str, Any]],
        unmodeled: list[str],
        state_acc: dict[str, Any],
    ) -> None:
        if vt.once_per_turn:
            key = (source_idx, trig_idx)
            if key in state_acc["used_once"]:
                return
            state_acc["used_once"].add(key)
        source_name = new_perms[source_idx].name
        cls._apply_effect(
            vt.effect,
            vt.target_hint,
            source_idx,
            source_name,
            new_perms,
            delta,
            trace,
            events_queue,
            unmodeled,
            state_acc,
        )

    @classmethod
    def _apply_effect(
        cls,
        eff: Effect,
        target_hint: str,
        source_idx: int | None,
        source_name: str,
        new_perms: list[Permanent],
        delta: Delta,
        trace: list[str],
        events_queue: list[tuple[str, Any]],
        unmodeled: list[str],
        state_acc: dict[str, Any],
    ) -> None:
        if isinstance(eff, PutCounters):
            target_idx = cls._pick_creature_index(new_perms, source_idx if target_hint == "self" else None)
            if target_idx is None:
                return
            tp = new_perms[target_idx]
            new_perms[target_idx] = dataclasses.replace(
                tp, power=tp.power + eff.count, toughness=tp.toughness + eff.count
            )
            delta.power_added += eff.count
            delta.counters_added += eff.count
            trace.append(f"{source_name} → +{eff.count}/+{eff.count} counter(s) on {tp.name}")

        elif isinstance(eff, Pump):
            indices: list[int] = []
            if target_hint == "self" and source_idx is not None:
                if new_perms[source_idx].power > 0 or new_perms[source_idx].toughness > 0:
                    indices = [source_idx]
            elif target_hint == "team":
                indices = [i for i, p in enumerate(new_perms) if p.power > 0 or p.toughness > 0]
            elif target_hint == "others":
                indices = [
                    i for i, p in enumerate(new_perms) if i != source_idx and (p.power > 0 or p.toughness > 0)
                ]
            else:
                pick = cls._pick_creature_index(new_perms, None)
                if pick is not None:
                    indices = [pick]
            for i in indices:
                tp = new_perms[i]
                new_perms[i] = dataclasses.replace(
                    tp, power=max(0, tp.power + eff.power), toughness=max(0, tp.toughness + eff.toughness)
                )
                delta.power_added += eff.power
            if indices:
                trace.append(f"{source_name} → {eff.power:+d}/{eff.toughness:+d} pump")

        elif isinstance(eff, CreateToken):
            spec: TokenSpec = eff.spec
            created_count = spec.count if isinstance(spec.count, int) else 1
            types_tuple = tuple(str(t) for t in (spec.types or ("Creature",)))
            is_creature_tok = any(t.casefold() == "creature" for t in types_tuple)
            is_ench_tok = any(t.casefold() == "enchantment" for t in types_tuple)
            for _ in range(created_count):
                tok_perm = Permanent(
                    name=spec.name or "Token",
                    power=spec.power or 0,
                    toughness=spec.toughness or 0,
                    is_tapped=bool(spec.tapped),
                    is_token=True,
                    subtypes=spec.subtypes,
                    type_line=" ".join(types_tuple),
                    card_types=types_tuple,
                )
                new_perms.append(tok_perm)
                delta.bodies_added += 1
                delta.tokens_created[tok_perm.name] += 1
                if spec.power:
                    delta.power_added += spec.power
                if is_creature_tok:
                    events_queue.append(("CREATURE_ENTERS", tok_perm))
                if is_ench_tok:
                    events_queue.append(("ENCHANTMENT_ENTERS", tok_perm))
            trace.append(f"{source_name} Trigger → Create {created_count} {spec.name or 'Token'} token(s)")
            events_queue.append(("TOKEN_CREATED", spec.name))

        elif isinstance(eff, GainLife):
            state_acc["hero_life"] += eff.amount
            delta.life_gained += eff.amount
            trace.append(f"{source_name} → Gain {eff.amount} Life")
            events_queue.append(("LIFE_GAINED", eff.amount))

        elif isinstance(eff, Damage):
            if target_hint in ("opponent", "any_target", "each_player"):
                state_acc["opp_life"] = max(0, state_acc["opp_life"] - eff.amount)
                delta.damage_dealt += eff.amount
                if target_hint == "each_player":
                    state_acc["hero_life"] = max(0, state_acc["hero_life"] - eff.amount)
                trace.append(f"{source_name} → Deal {eff.amount} damage")

        elif isinstance(eff, Draw):
            state_acc["draws"] += eff.count
            delta.cards_drawn += eff.count
            trace.append(f"{source_name} → Draw {eff.count} card(s)")

        else:
            unmodeled.append(str(eff))

    @staticmethod
    def _pick_creature_index(new_perms: list[Permanent], preferred_idx: int | None) -> int | None:
        if preferred_idx is not None and 0 <= preferred_idx < len(new_perms):
            p = new_perms[preferred_idx]
            if p.power > 0 or p.toughness > 0:
                return preferred_idx
        creatures = [(i, p) for i, p in enumerate(new_perms) if p.power > 0 or p.toughness > 0]
        if not creatures:
            return None
        return max(creatures, key=lambda item: (item[1].power, item[1].toughness, -item[0]))[0]

    @classmethod
    def _resolve_ability_effects(
        cls,
        ability: TriggeredAbility,
        source_perm: Permanent,
        new_perms: list[Permanent],
        delta: Delta,
        trace: list[str],
        events_queue: list[tuple[str, Any]],
        unmodeled: list[str],
        *,
        state_acc: dict[str, Any] | None = None,
    ) -> None:
        """Resolve effects of a triggered ability and enqueue cascading events."""
        for eff in ability.effects:
            if isinstance(eff, CreateToken):
                spec: TokenSpec = eff.spec
                created_count = spec.count if isinstance(spec.count, int) else 1
                for _ in range(created_count):
                    tok_perm = Permanent(
                        name=spec.name,
                        power=spec.power or 0,
                        toughness=spec.toughness or 0,
                        is_token=True,
                        subtypes=spec.subtypes,
                    )
                    new_perms.append(tok_perm)
                    delta.bodies_added += 1
                    delta.tokens_created[spec.name] += 1
                    if spec.power:
                        delta.power_added += spec.power

                if spec.copy_of_self:
                    trace.append(f"{source_perm.name} ETB → Create {created_count} copy tokens")
                else:
                    trace.append(f"{source_perm.name} Trigger → Create {created_count} {spec.name} token(s)")

                events_queue.append(("TOKEN_CREATED", spec.name))
            elif isinstance(eff, GainLife):
                delta.life_gained += eff.amount
                if state_acc is not None:
                    state_acc["hero_life"] += eff.amount
                trace.append(f"{source_perm.name} → Gain {eff.amount} Life")
            else:
                unmodeled.append(str(eff))


AfterstateEngine = AfterstateSimulator
