"""Extensible 1-Ply Synergy Afterstate Engine for MTGA Coach.

Simulates 1-ply action resolution and cascading triggers (ETB tokens, Food sacrifices,
commander zone re-routing) over lightweight immutable BoardState views, and evaluates
true value deltas via uniform V(s) scoring without arbitrary additive constants.
"""

from __future__ import annotations

import copy
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal

from arenamcp.ability_synthesizer import (
    AbilitySynthesizer,
    ActivatedAbility,
    AddMana,
    CardAbilities,
    Cost,
    CreateToken,
    GainLife,
    ReturnFromGraveyard,
    StaticAbility,
    TokenSpec,
    TriggeredAbility,
)
from arenamcp.format_profile import FormatEvaluatorConfig, FormatProfile

logger = logging.getLogger(__name__)


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
                is_tok = bool(c.get("is_token") or "token" in str(c.get("type_line") or "").lower())
                oracle = str(c.get("oracle_text") or "")
                parsed_abs = AbilitySynthesizer.parse_card(name, oracle)
                subtypes = tuple(str(c.get("type_line") or "").split("—")[-1].split()) if "—" in str(c.get("type_line") or "") else ()
                perms.append(
                    Permanent(
                        name=name,
                        power=p,
                        toughness=t,
                        is_tapped=is_tap,
                        is_token=is_tok,
                        subtypes=subtypes,
                        abilities=parsed_abs.abilities,
                        oracle_text=oracle,
                        is_land="land" in str(c.get("type_line") or "").lower(),
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
        casts = int((game_state.get("command") or [{}])[0].get("commander_casts") or 0) if command_cards else 0

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

    # 4. Hand advantage
    hand_adv = max(-0.15, min(0.15, (len(board.hand) - 3) * 0.04))

    # 5. Mana development. Without it a land drop only registered as a card
    # leaving hand (-0.04), so "Pass" outscored "Play Forest" every turn.
    # Worth more than a hand card up to ~6 lands, then flattens (flood).
    land_count = sum(1 for p in board.permanents if p.is_land)
    # Centred on 3 lands so absolute values don't saturate the 0.95 clip.
    mana_dev = (min(land_count, 6) - 3) * 0.05 + max(0, land_count - 6) * 0.01

    val = 0.50 + life_adv + board_adv + food_reserve + hand_adv + mana_dev
    return max(0.05, min(0.95, round(val, 3)))


# ── Simulator & Trigger Cascade ──


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
        hero_life = board.hero_life
        opp_life = board.opp_life
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
            land_perm = Permanent(name=action.land_name, is_tapped=False, subtypes=("Land",), is_land=True)
            new_perms.append(land_perm)
            delta.bodies_added += 1
            trigger_trace.append(f"Play {action.land_name} (mana +1)")
            events_queue.append(("ENTERS", land_perm))

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

            # Parse spell abilities
            oracle = getattr(action, "oracle_text", "")
            parsed = AbilitySynthesizer.parse_card(card_name, oracle)
            # If oracle text was empty, look up in cache or synthesize standard creature stats
            power = 2 if "Creature" in str(getattr(action, "type_line", "Creature")) else 2
            toughness = 2

            new_perm = Permanent(
                name=card_name,
                power=power,
                toughness=toughness,
                is_commander=action.is_commander,
                abilities=parsed.abilities,
            )
            new_perms.append(new_perm)
            delta.bodies_added += 1
            delta.power_added += power
            trigger_trace.append(f"Cast {card_name}")

            # Enqueue ETB events
            events_queue.append(("ENTERS", new_perm))
            events_queue.append(("ANOTHER_CREATURE_ENTERS", new_perm))

        elif isinstance(action, ActivateAbility):
            if action.is_food_sacrifice:
                # Sacrifice 1 Food -> gain 3 life
                food_idx = next((i for i, p in enumerate(new_perms) if p.name == "Food"), None)
                if food_idx is not None:
                    new_perms.pop(food_idx)
                    delta.tokens_sacrificed["Food"] += 1
                    hero_life += 3
                    delta.life_gained += 3
                    mana_left = max(0, mana_left - 2)
                    trigger_trace.append("Sacrifice Food → Gain 3 Life")
                    events_queue.append(("LIFE_GAINED", 3))

        elif isinstance(action, DeclareAttack):
            opp_life = max(0, opp_life - action.expected_damage)
            delta.damage_dealt += action.expected_damage
            trigger_trace.append(f"Attack with {', '.join(action.attackers)} (-{action.expected_damage} Opp Life)")

        # 2. Trigger resolution loop
        depth = 0
        while events_queue and depth < cls.MAX_CASCADE_DEPTH:
            depth += 1
            curr_event, obj = events_queue.pop(0)

            # Check all hero permanents for matching TriggeredAbility
            for perm in list(new_perms):
                for ab in perm.abilities:
                    if not isinstance(ab, TriggeredAbility):
                        continue

                    trig = ab.trigger
                    if trig.event == "ETB" and curr_event == "ENTERS" and obj.name == perm.name:
                        # Direct ETB on self
                        cls._resolve_ability_effects(
                            ab, perm, new_perms, delta, trigger_trace, events_queue, unmodeled
                        )

                    elif trig.event == "ANOTHER_CREATURE_ETB" and curr_event == "ANOTHER_CREATURE_ENTERS":
                        # Check filter conditions (e.g. nontoken)
                        if trig.condition == "nontoken" and getattr(obj, "is_token", False):
                            continue
                        if obj.name != perm.name:
                            cls._resolve_ability_effects(
                                ab, perm, new_perms, delta, trigger_trace, events_queue, unmodeled
                            )

        new_board = BoardState(
            hero_life=hero_life,
            opp_life=opp_life,
            available_mana=mana_left,
            lands_played_this_turn=lands_played,
            permanents=tuple(new_perms),
            hand=tuple(new_hand),
            command=tuple(new_command),
            commander_casts=cmd_casts,
            config=board.config,
        )

        return Afterstate(
            board=new_board,
            delta=delta,
            trigger_trace=trigger_trace,
            unmodeled=unmodeled,
            depth=depth,
        )

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
                trace.append(f"{source_perm.name} → Gain {eff.amount} Life")
            else:
                unmodeled.append(str(eff))
