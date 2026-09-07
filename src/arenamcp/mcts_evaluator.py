"""MCTS Search & Multi-Ply Tactical Lookahead Evaluator for MTGA Coach.

Evaluates available game state decisions, simulates forward state rollouts
(including combat permutations, mana curves, spell resolutions, and opponent
counterplay reaction envelopes), integrates MageZero neural network position values,
and leverages the OpponentModel for metagame-aware tactical synthesis.
"""

from __future__ import annotations

import logging
import math
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from arenamcp.combat_solver import optimal_attacks, optimal_blocks
from arenamcp.magezero_client import MageZeroClient
from arenamcp.opponent_model import OpponentModel, OpponentProfile

logger = logging.getLogger(__name__)


def _freeze(value: Any, _depth: int = 0) -> Any:
    """Recursively freeze JSON-like data into an immutable, hashable, sortable form.

    Scalars are type-tagged so mixed optional fields (``None`` vs ``int`` vs
    ``str``) always compare and hash cleanly; lists/tuples/dicts nest as
    tuples. Lists preserve order; dicts sort by key. Cyclic or overly deep
    structures terminate at a depth guard.
    """
    if _depth > 16:
        return ("depth-guard",)
    if value is None:
        return ("z",)
    if isinstance(value, bool):
        return ("b", value)
    if isinstance(value, (int, float)):
        return ("n", round(float(value), 9))
    if isinstance(value, str):
        return ("s", value)
    if isinstance(value, dict):
        return ("m", tuple(sorted((str(k), _freeze(v, _depth + 1)) for k, v in value.items())))
    if isinstance(value, (list, tuple)):
        return ("l", tuple(_freeze(v, _depth + 1) for v in value))
    return ("o", repr(value))


@dataclass
class MCTSBranch:
    """A single simulated action branch or sequence in the MCTS search tree."""

    action: str
    action_type: str  # "cast", "attack", "block", "ability", "pass", "land", "sequence"
    mana_cost: str = ""
    sequence_steps: list[str] = field(default_factory=list)
    win_probability: float = 0.50  # V(s') in [0.0, 1.0]
    value_delta: float = 0.0  # Delta vs baseline root win probability
    simulated_visits: int = 0  # Simulation count (0 for 1-ply / heuristic)
    prior_probability: float = 0.0  # P(a | s) policy prior (0.0 unless neural prior available)
    tag: str = "NORMAL"  # "⭐ BEST LINE", "🛡️ SAFE", "⚡ TEMPO", "⚠️ BLUNDER TRAP"
    outcome_summary: str = ""
    simulated_counterplay: str = ""
    worst_case_reaction: str = ""
    projected_state: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MCTSTreePayload:
    """Complete tactical decision packet, position evaluation, and candidate lines."""

    root_win_probability: float = 0.50
    total_simulations: int = 0
    turn_number: int = 1
    phase: str = ""
    best_action: str = ""
    hero_life: int = 20
    opp_life: int = 20
    available_mana: int = 0
    eval_source: str = "Tactical Heuristic Lookahead"
    opponent_threat_summary: str = ""
    opponent_profile: OpponentProfile = field(default_factory=OpponentProfile)
    expected_opponent_actions: list[str] = field(default_factory=list)
    format_summary: str = ""
    branches: list[MCTSBranch] = field(default_factory=list)
    blunder_traps: list[MCTSBranch] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_win_probability": round(self.root_win_probability, 3),
            "total_simulations": self.total_simulations,
            "turn_number": self.turn_number,
            "phase": self.phase,
            "best_action": self.best_action,
            "hero_life": self.hero_life,
            "opp_life": self.opp_life,
            "available_mana": self.available_mana,
            "eval_source": self.eval_source,
            "opponent_threat_summary": self.opponent_threat_summary,
            "opponent_profile": self.opponent_profile.to_dict(),
            "expected_opponent_actions": self.expected_opponent_actions,
            "format_summary": self.format_summary,
            "branches": [b.to_dict() for b in self.branches],
            "blunder_traps": [b.to_dict() for b in self.blunder_traps],
        }

    def format_for_llm_prompt(self) -> str:
        """Format the search tree and opponent model into a rich context block for the LLM."""
        root_pct = int(round(self.root_win_probability * 100))
        eval_desc = (
            f"{self.total_simulations} candidates · {self.eval_source}"
            if self.total_simulations > 0
            else self.eval_source
        )
        lines = [
            "=== MCTS MULTI-PLY TACTICAL SEARCH ===",
            f"• Root Win Expectancy: {root_pct}% ({eval_desc} · T{self.turn_number} {self.phase})",
            f"• HERO: {self.hero_life} Life | OPP: {self.opp_life} Life | Mana Available: {self.available_mana}",
        ]
        if self.format_summary:
            lines.append(f"• Format: {self.format_summary}")
        if self.expected_opponent_actions:
            lines.append(f"• Expected Opponent Counterplay: {', '.join(self.expected_opponent_actions)}")
        elif self.opponent_profile and self.opponent_profile.revealed_cards:
            lines.append(f"• Opponent Archetype: {self.opponent_profile.format_summary()}")
        elif self.opponent_threat_summary:
            lines.append(f"• Opponent Threat / Interaction Envelope: {self.opponent_threat_summary}")
        lines.append("")

        if self.branches:
            best = self.branches[0]
            b_pct = int(round(best.win_probability * 100))
            delta_str = (
                f"+{best.value_delta * 100:.1f}%"
                if best.value_delta > 0
                else f"{best.value_delta * 100:.1f}%"
            )
            lines.append(f"⭐ BEST LINE (Win: {b_pct}%, Value Delta: {delta_str}):")
            if best.sequence_steps:
                for idx, step in enumerate(best.sequence_steps, start=1):
                    lines.append(f"  {idx}. {step}")
            else:
                lines.append(f"  • {best.action}")
            if best.outcome_summary:
                lines.append(f"  ↳ Tactical Rationale: {best.outcome_summary}")
            if best.simulated_counterplay:
                lines.append(f"  ↳ Anticipated Counterplay: {best.simulated_counterplay}")
            if best.projected_state:
                p = best.projected_state
                lines.append(
                    f"  ↳ Projected State Next Turn: Hero {p.get('hero_life', self.hero_life)} Life, "
                    f"Opp {p.get('opp_life', self.opp_life)} Life, Hero Power {p.get('hero_power', 0)}"
                )
            lines.append("")

            # Additional candidate alternatives (ranks 2-3)
            if len(self.branches) > 1:
                lines.append("ALTERNATIVE LINES CONSIDERED:")
                for b in self.branches[1:3]:
                    alt_pct = int(round(b.win_probability * 100))
                    alt_delta = (
                        f"+{b.value_delta * 100:.1f}%" if b.value_delta > 0 else f"{b.value_delta * 100:.1f}%"
                    )
                    lines.append(
                        f"  • [{b.tag}] {b.action} (Win: {alt_pct}%, {alt_delta}): {b.outcome_summary}"
                    )
                lines.append("")

        if self.blunder_traps:
            lines.append("⚠️ BLUNDER TRAP DETECTED:")
            for trap in self.blunder_traps[:2]:
                trap_pct = int(round(trap.win_probability * 100))
                trap_delta = f"{trap.value_delta * 100:.1f}%"
                lines.append(f"  • Line: {trap.action} (Win: {trap_pct}%, {trap_delta})")
                if trap.outcome_summary:
                    lines.append(f"  • Trap Warning: {trap.outcome_summary}")
            lines.append("")

        lines.append("======================================")
        return "\n".join(lines)


class MCTSEvaluator:
    """Simulates and scores forward decision trees for live MTGA game states."""

    _last_sig: tuple[Any, ...] | None = None
    _last_payload: MCTSTreePayload | None = None
    _last_payload_at: float = 0.0

    # Cached heuristic evaluations expire so a stale tactical payload is not
    # retained forever if serving state changes underneath it. Override the
    # class attribute (or monkeypatch it in tests) to tune. TTL=0 disables
    # expiry only — the payload cache itself stays active (use force=True or
    # reset_cache() to bypass/clear it).
    CACHE_TTL_SECONDS: float = 15.0

    @classmethod
    def reset_cache(cls) -> None:
        """Reset the cached evaluation state."""
        cls._last_sig = None
        cls._last_payload = None
        cls._last_payload_at = 0.0

    @classmethod
    def _cache_fresh(cls) -> bool:
        """True while the cached payload is inside its bounded expiry window."""
        ttl = cls.CACHE_TTL_SECONDS
        if ttl <= 0.0:
            return True
        return (time.monotonic() - cls._last_payload_at) < ttl

    @classmethod
    def _resolve_opponent_hand_count(
        cls, game_state: dict[str, Any], local_seat: Any
    ) -> tuple[Any, str]:
        """Single source of truth for opponent-hand-count resolution.

        SHARED by the cache signature and the evaluation body, so their
        precedence rules cannot drift. Precedence (mirroring evaluate):
        top-level ``opponent_hand_count`` -> ``zones.opponent_hand_count`` ->
        the non-local player row, using ``or`` fallback semantics where a
        present-but-None ``hand_count`` falls through to ``cards_in_hand``.
        Real zero is preserved (0 is a known count, distinct from unknown).
        Returns ``(count_or_None, tier)`` — tier is the fallback level that
        produced the value ('top_level'|'zones'|'player'|'unknown'), so the
        cache can distinguish unknown from any known count including 0.
        """
        count = game_state.get("opponent_hand_count")
        if count is not None:
            return cls._coerce_hand_count(count), "top_level"
        zones = game_state.get("zones") or {}
        if isinstance(zones, dict):
            count = zones.get("opponent_hand_count")
            if count is not None:
                return cls._coerce_hand_count(count), "zones"
        players = game_state.get("players") or []
        for p in players:
            if not isinstance(p, dict):
                continue
            if p.get("is_local") or p.get("seat_id") == local_seat:
                continue
            for key in ("hand_count", "hand_size", "cards_in_hand"):
                val = p.get(key)
                if val is not None:
                    return cls._coerce_hand_count(val), "player"
        return None, "unknown"

    @staticmethod
    def _coerce_hand_count(value: Any) -> int | None:
        """Coerce a producer hand-count field to a non-negative int, else None."""
        if isinstance(value, bool):
            return None
        try:
            count = int(value)
        except (TypeError, ValueError):
            return None
        return max(0, count)

    @staticmethod
    def _zone_identity(zone: Any, *, ordered: bool = False) -> tuple:
        """Semantically complete identity of a zone's cards, as a hashable tuple.

        Every entry is built from type-tagged, scalar-normalized values, so
        mixed or missing optional fields (e.g. one Forest with an integer
        instance_id and another without) can never raise during comparison.

        Covers name, instance id, controller/owner seat, tapped AND attacking
        state, summoning-sickness-signal (turn_entered_battlefield), power and
        toughness, mana cost, type line, and oracle text — the card facts that
        candidate generation, afterstate construction, and encoding consume.

        ``ordered=False`` (default) treats the zone as a multiset (hand/board
        order is not semantically consumed); ``ordered=True`` preserves card
        sequence — required for the stack, where the top spell is what a
        response interacts with.
        """
        if not isinstance(zone, (list, tuple)):
            return ()
        entries: list[tuple] = []
        # Every variant (canonical card tuple, untyped raw value) shares one
        # outer representation — a 2-tuple starting with a string tag — so the
        # sort below can never compare a str against a tuple.
        for c in zone:
            if not isinstance(c, dict):
                entries.append(("raw", 0, _freeze(c)))
                continue
            name = c.get("name")
            if not name:
                continue
            entries.append(
                (
                    "card",
                    1,
                    (
                        ("name", str(name)),
                        ("id", _freeze(c.get("instance_id"))),
                        ("ctrl", _freeze(c.get("controller_seat_id"))),
                        ("owner", _freeze(c.get("owner_seat_id"))),
                        ("tapped", bool(c.get("is_tapped"))),
                        ("attacking", bool(c.get("is_attacking"))),
                        ("sick", bool(c.get("is_summoning_sick"))),
                        ("etb", _freeze(c.get("turn_entered_battlefield"))),
                        ("pt", _freeze((c.get("power") if "power" in c else None,
                                        c.get("toughness") if "toughness" in c else None))),
                        ("cost", str(c.get("mana_cost") or "")),
                        ("types", str(c.get("type_line") or "")),
                        ("oracle", str(c.get("oracle_text") or "")),
                    ),
                )
            )
        if not ordered:
            entries.sort()
        return tuple(entries)

    @classmethod
    def _decision_signature(cls, game_state: dict[str, Any], local_seat: Any) -> tuple:
        """Immutable semantic fingerprint of the fields consumed by evaluation.

        Includes actor/phase context, full card identity of all zones (with
        controller and tapped state), mana pool, life totals, opponent hand
        count, stack identity, pending decision, and match identity scoped to
        the current game. Cached payloads must never be reused across different
        decisions merely because zone *lengths* agree.
        """
        turn = game_state.get("turn") or {}
        players = game_state.get("players") or []
        zones = game_state.get("zones") or {}

        mana: tuple = ()
        for p in players:
            if not isinstance(p, dict):
                continue
            is_local = p.get("is_local") or p.get("seat_id") == local_seat
            if is_local:
                mana = tuple(sorted((k, _freeze(v)) for k, v in (p.get("mana_pool") or {}).items()))

        # Opponent-hand count through the SHARED helper with the EXACT same
        # precedence and or-semantics as the evaluation body. The (count,
        # tier) pair is the semantic input: unknown counts are distinguished
        # from every known value (including 0) via the tier, not by baking a
        # default into the signature.
        opp_hand_count, opp_hand_tier = cls._resolve_opponent_hand_count(
            game_state, local_seat
        )

        opp_life = next(
            (p.get("life_total") for p in players
             if isinstance(p, dict) and not (p.get("is_local") or p.get("seat_id") == local_seat)
             and p.get("life_total") is not None),
            None,
        )

        hero_player = next(
            (p for p in players if isinstance(p, dict) and p.get("is_local")), None
        )
        hero_life = next(
            (p.get("life_total") for p in players
             if isinstance(p, dict) and (p.get("is_local") or p.get("seat_id") == local_seat)),
            None,
        )
        hero_lands_played = hero_player.get("lands_played") if hero_player else None

        # Format identity: resolve_format is a semantic consumer (numerical
        # config, model eligibility, visible output). Fingerprint the RESOLVED
        # profile plus the raw inputs used to derive it, so both an explicit
        # format_profile/format field change and an underlying board-shape
        # change that flips detection produce fresh evaluations.
        from arenamcp.format_profile import detect_format_profile

        raw_fmt = game_state.get("format_profile") or game_state.get("format")
        fmt_fingerprint = _freeze(
            {
                "resolved": (
                    {
                        "family": getattr(raw_fmt, "family", None),
                        "variant": getattr(raw_fmt, "variant", None),
                        "deck_size": getattr(raw_fmt, "deck_size", None),
                        "singleton": getattr(raw_fmt, "singleton", None),
                        "commander": getattr(raw_fmt, "commander_names", None),
                        "has_command_zone": getattr(raw_fmt, "has_command_zone", None),
                    }
                    if hasattr(raw_fmt, "family")
                    else raw_fmt if isinstance(raw_fmt, dict) else None
                ),
                "detected": _freeze(detect_format_profile(game_state).__dict__)
                if not (isinstance(raw_fmt, dict) and raw_fmt.get("family"))
                and not hasattr(raw_fmt, "family")
                else None,
            }
        )

        # Deck-selection identity: ModelZooClient.select consumes hero deck
        # identity via consolidated extract_hero_deck plus format profile
        # — fingerprint the deck source exactly as selection sees it.
        from arenamcp.magezero_gating import extract_hero_deck

        extraction = extract_hero_deck(game_state)
        hero_deck_source = (
            extraction.compatibility_reason,
            extraction.is_full_deck,
            tuple(sorted(Counter(extraction.cards).items())),
        )

        return (
            # Actor / decision context
            turn.get("turn_number"),
            str(turn.get("phase") or game_state.get("phase", "")),
            turn.get("step"),
            turn.get("active_player"),
            turn.get("priority_player"),
            # pending_decision is nested semantic state; deep-freeze so an
            # in-place options mutation above (e.g. options.append) invalidates.
            _freeze(game_state.get("pending_decision")),
            # Actor + actor-level state
            local_seat,
            hero_life,
            opp_life,
            mana,
            hero_lands_played,
            # Opponent information — (count, tier) pair: unknown ≠ 0 ≠ N
            opp_hand_count,
            opp_hand_tier,
            # Match identity
            game_state.get("match_id") or game_state.get("arena_match_id") or None,
            # Format identity (resolved + detection inputs)
            fmt_fingerprint,
            # Deck-selection identity (what ModelZooClient.select consumes)
            hero_deck_source,
            # Zone identities (semantic card facts, fully). The stack is the
            # one order-sensitive zone: the top spell is what a response
            # interacts with, so its sequence is preserved.
            cls._zone_identity(game_state.get("hand")),
            cls._zone_identity(game_state.get("battlefield")),
            cls._zone_identity(game_state.get("stack"), ordered=True),
            cls._zone_identity(game_state.get("graveyard")),
            cls._zone_identity(game_state.get("exile")),
            cls._zone_identity(game_state.get("command")),
            cls._zone_identity(zones.get("command") if isinstance(zones, dict) else None),
            # Model/checkpoint identity when exposed by upstream tasks (04)
            game_state.get("magezero_model_id"),
        )


    @classmethod
    def evaluate(cls, game_state: dict[str, Any], force: bool = False) -> MCTSTreePayload:
        """Run multi-ply outcome evaluation on the current game state snapshot."""
        if not isinstance(game_state, dict):
            return MCTSTreePayload()

        turn = game_state.get("turn") or {}
        turn_num = turn.get("turn_number") or game_state.get("turn_number", 1)
        phase = str(turn.get("phase") or game_state.get("phase", "Main1")).replace("Phase_", "")

        # Resolve local seat reliably
        local_seat = game_state.get("local_seat_id")
        players = game_state.get("players") or []
        if local_seat is None:
            for p in players:
                if isinstance(p, dict) and p.get("is_local"):
                    local_seat = p.get("seat_id")
                    break
        if local_seat is None:
            local_seat = 1

        hand = game_state.get("hand") or []
        battlefield = game_state.get("battlefield") or []
        stack = game_state.get("stack") or []
        sig = cls._decision_signature(game_state, local_seat)
        if (
            not force
            and cls._last_sig == sig
            and cls._last_payload is not None
            and cls._cache_fresh()
        ):
            return cls._last_payload

        hero_life, opp_life = 20, 20
        hero_mana_dict: dict[str, int] = {}
        for p in players:
            if isinstance(p, dict):
                if p.get("is_local") or p.get("seat_id") == local_seat:
                    hero_life = int(p.get("life_total") if p.get("life_total") is not None else 20)
                    hero_mana_dict = p.get("mana_pool") or {}
                else:
                    opp_life = int(p.get("life_total") if p.get("life_total") is not None else 20)

        # Count untapped lands / mana sources / board objects
        battlefield = game_state.get("battlefield") or []
        hand = game_state.get("hand") or []

        untapped_lands = 0
        opp_untapped_lands = 0
        hero_mana_dorks = 0
        hero_mana_rocks = 0
        hero_creatures = []
        opp_creatures = []

        for obj in battlefield:
            if not isinstance(obj, dict):
                continue
            controller = obj.get("controller_seat_id") or obj.get("owner_seat_id")
            is_hero = controller == local_seat
            is_tapped = bool(obj.get("is_tapped"))
            t_line = str(obj.get("type_line") or "").lower()
            oracle = str(obj.get("oracle_text") or "").lower()

            if is_hero:
                if "land" in t_line and not is_tapped:
                    untapped_lands += 1
                elif "creature" in t_line:
                    hero_creatures.append(obj)
                    has_haste = "haste" in oracle
                    is_sick = (obj.get("turn_entered_battlefield") == turn_num and not has_haste)
                    has_mana = "add {" in oracle or "add one mana" in oracle or "{ot}: add" in oracle or "taps for" in oracle
                    if not is_tapped and has_mana and not is_sick:
                        hero_mana_dorks += 1
                elif "artifact" in t_line and not is_tapped:
                    if "add {" in oracle or "add one mana" in oracle or "{ot}: add" in oracle or "treasure" in t_line:
                        hero_mana_rocks += 1
            else:
                if "land" in t_line and not is_tapped:
                    opp_untapped_lands += 1
                elif "creature" in t_line:
                    opp_creatures.append(obj)

        # Total available mana accounting for lands, dorks, rocks, and floated mana pool
        perm_mana = untapped_lands + hero_mana_dorks + hero_mana_rocks
        pool_mana = sum(hero_mana_dict.values()) if hero_mana_dict else 0
        available_mana = max(perm_mana, pool_mana)

        # Opponent-hand count via the SHARED normalization helper, so the
        # signature and the evaluation can never drift apart again.
        # Returns the raw (possibly None) resolved count plus the fallback tier
        # that produced it — both are semantic inputs (a synthesized default
        # for unknown is NOT the same state as a known count).
        zones = game_state.get("zones") or {}
        opp_hand_count, opp_hand_count_tier = cls._resolve_opponent_hand_count(
            game_state, local_seat
        )
        if opp_hand_count is None:
            # Evaluation synthesizes 4 for unknown; the signature tiers on the
            # (count, tier) pair so the cache distinguishes unknown from any
            # known value including 0.
            opp_hand_count = 4

        # Resolve format evaluator config
        from arenamcp.format_profile import FormatEvaluatorConfig, FormatProfile, detect_format_profile

        raw_fmt = game_state.get("format_profile") or game_state.get("format")
        if hasattr(raw_fmt, "family"):
            fmt_profile = raw_fmt
        elif isinstance(raw_fmt, dict) and raw_fmt.get("family"):
            fmt_profile = FormatProfile(**{k: v for k, v in raw_fmt.items() if k in FormatProfile.__annotations__})
        else:
            fmt_profile = detect_format_profile(game_state)

        fmt_config = FormatEvaluatorConfig.from_profile(fmt_profile)

        # Non-linear MTG Root State Value Function V(s) in [0.05, 0.95]
        hero_power = sum(int(c.get("power") or 0) for c in hero_creatures)
        opp_power = sum(int(c.get("power") or 0) for c in opp_creatures)

        # 1. Life Advantage with non-linear lethal threshold pressure
        life_delta = (hero_life - opp_life) / float(fmt_config.life_norm)
        life_adv = max(-1.0, min(1.0, life_delta)) * 0.22
        if opp_life <= fmt_config.lethal_zone:
            life_adv += max(0.0, ((fmt_config.lethal_zone + 1) - opp_life) * 0.04)
        if hero_life <= fmt_config.lethal_zone and opp_power >= hero_life:
            life_adv -= 0.15

        # 2. Board Power & Presence Advantage
        power_delta = (hero_power - opp_power) / float(fmt_config.board_norm)
        board_adv = max(-1.0, min(1.0, power_delta)) * 0.25
        count_delta = (len(hero_creatures) - len(opp_creatures)) / 5.0
        board_adv += max(-0.10, min(0.10, count_delta * 0.08))

        # 3. Card Advantage (Hero hand vs Opponent hand)
        card_delta = (len(hand) - int(opp_hand_count)) / float(fmt_config.hand_norm)
        hand_adv = max(-1.0, min(1.0, card_delta)) * 0.12

        heuristic_val = 0.50 + life_adv + board_adv + hand_adv
        heuristic_val = max(0.05, min(0.95, heuristic_val))

        # Phase 4: Position Value Evaluation (Heuristic Baseline)
        eval_source = "Tactical Heuristic Lookahead"
        base_val = heuristic_val

        # Phase 5: Opponent Metagame Classifier & Hand Belief State
        opp_profile = OpponentModel.classify(game_state)
        opp_threat = opp_profile.format_summary()

        branches: list[MCTSBranch] = []
        blunder_traps: list[MCTSBranch] = []

        # Find playable lands (only if land drop has not been used this turn)
        hero_player = next((p for p in game_state.get("players", []) if p.get("is_local")), None)
        hero_lands_played = hero_player.get("lands_played", 0) if hero_player else 0
        current_turn = int(game_state.get("turn", {}).get("turn_number") or 1)
        hero_seat = int(hero_player.get("seat_id") or 1) if hero_player else 1
        lands_entered_this_turn = sum(
            1
            for c in (game_state.get("battlefield") or [])
            if isinstance(c, dict)
            and c.get("owner_seat_id") == hero_seat
            and c.get("turn_entered_battlefield") == current_turn
            and "land" in str(c.get("type_line") or "").lower()
        )

        playable_lands = []
        if hero_lands_played == 0 and lands_entered_this_turn == 0:
            playable_lands = [
                c for c in hand if isinstance(c, dict) and "land" in str(c.get("type_line") or "").lower()
            ]

        # Find playable spells in hand and command zone
        playable_spells: list[dict[str, Any]] = []
        command = (
            game_state.get("command")
            or game_state.get("zones", {}).get("command")
            or game_state.get("command_zone")
            or []
        )
        command_spells = [
            c
            for c in command
            if isinstance(c, dict)
            and (
                c.get("owner_seat_id") in (hero_seat, None, 0)
                or c.get("controller_seat_id") in (hero_seat, None, 0)
                or c.get("is_local")
            )
        ]

        # Check GRE legal actions for directly confirmed castable cards
        legal_actions = (
            game_state.get("legal_actions") or game_state.get("raw_legal_actions") or []
        )
        legal_cast_names: set[str] = set()
        for act in legal_actions:
            act_str = str(act.get("name") if isinstance(act, dict) else act).strip()
            if act_str.startswith("Cast "):
                clean_name = act_str[5:].split("[")[0].strip().lower()
                if clean_name:
                    legal_cast_names.add(clean_name)

        all_candidate_cards = [(c, False) for c in hand] + [(c, True) for c in command_spells]
        for card, is_commander in all_candidate_cards:
            if not isinstance(card, dict):
                continue
            t_line = str(card.get("type_line") or "").lower()
            name = str(card.get("name") or "").strip()
            if "land" not in t_line:
                cost_str = str(card.get("mana_cost") or card.get("cost") or "")
                cmc = 0
                for sym in cost_str.replace("{", " ").replace("}", " ").split():
                    if sym.isdigit():
                        cmc += int(sym)
                    elif sym.upper() in {"W", "U", "B", "R", "G", "C"}:
                        cmc += 1
                if is_commander:
                    casts = int(card.get("commander_casts") or 0)
                    cmc += casts * fmt_config.commander_tax_step

                is_gre_legal = name.lower() in legal_cast_names
                playable_spells.append(
                    {
                        "card": card,
                        "cmc": cmc,
                        "cost_str": cost_str,
                        "is_commander": is_commander,
                        "is_gre_legal": is_gre_legal,
                    }
                )

        # 1. Evaluate Combat Attacks (with 2-ply crackback lookahead)
        if "main" in phase.lower() or "attack" in phase.lower():
            ready_attackers = [
                c for c in hero_creatures if not c.get("is_tapped") and not c.get("has_summoning_sickness")
            ]
            if ready_attackers:
                opp_blockers = [c for c in opp_creatures if not c.get("is_tapped")]
                attack_plan = optimal_attacks(
                    candidate_attackers=ready_attackers,
                    opponent_blockers=opp_blockers,
                    opponent_life=opp_life,
                    your_life=hero_life,
                    opponent_attackers_next_turn=opp_creatures,
                    your_remaining_blockers=[c for c in hero_creatures if c not in ready_attackers],
                )
                if attack_plan:
                    atk_names = (
                        ", ".join(attack_plan.attacker_names)
                        if attack_plan.attacker_names
                        else "Hold Blockers"
                    )
                    atk_val = base_val + (attack_plan.score / 20.0)
                    atk_val = max(0.05, min(0.98, atk_val))
                    v_delta = atk_val - base_val

                    tag = (
                        "⭐ BEST LINE"
                        if attack_plan.damage_through >= opp_life
                        else ("⚡ TEMPO" if attack_plan.damage_through > 0 else "🛡️ SAFE")
                    )

                    branches.append(
                        MCTSBranch(
                            action=f"Declare Attacks: {atk_names}",
                            action_type="attack",
                            win_probability=round(atk_val, 3),
                            value_delta=round(v_delta, 3),
                            simulated_visits=0,
                            prior_probability=0.35,
                            tag=tag,
                            outcome_summary=f"{attack_plan.damage_through} dmg through; crackback risk {attack_plan.worst_case_crackback} dmg",
                            simulated_counterplay=f"Opponent blocks {len(attack_plan.attacker_names)} attackers; crackback with {len(opp_creatures)} creatures next turn",
                            projected_state={
                                "hero_life": max(0, hero_life - attack_plan.worst_case_crackback),
                                "opp_life": max(0, opp_life - attack_plan.damage_through),
                                "hero_power": hero_power,
                            },
                            details={
                                "damage_through": attack_plan.damage_through,
                                "crackback": attack_plan.worst_case_crackback,
                            },
                        )
                    )

                # Check if an all-out / reckless attack is a lethal blunder trap
                all_atk_names = ", ".join(c.get("name", "?") for c in ready_attackers)
                opp_block = optimal_blocks(ready_attackers, opp_blockers, opp_life)
                all_dmg_through = (
                    opp_block.damage_through
                    if opp_block
                    else sum(int(c.get("power") or 0) for c in ready_attackers)
                )
                crack_block = optimal_blocks(
                    opp_creatures, [c for c in hero_creatures if c not in ready_attackers], hero_life
                )
                all_crackback = (
                    crack_block.damage_through
                    if crack_block
                    else sum(int(c.get("power") or 0) for c in opp_creatures)
                )

                if (
                    all_crackback >= hero_life
                    and all_dmg_through < opp_life
                    and (
                        not attack_plan
                        or set(attack_plan.attacker_names) != set(c.get("name") for c in ready_attackers)
                    )
                ):
                    blunder_traps.append(
                        MCTSBranch(
                            action=f"All-out Attack: {all_atk_names}",
                            action_type="attack",
                            win_probability=round(max(0.05, base_val - 0.35), 3),
                            value_delta=round(-0.35, 3),
                            simulated_visits=0,
                            prior_probability=0.05,
                            tag="⚠️ BLUNDER TRAP",
                            outcome_summary=f"Attacking with all defenders leaves Hero open to {all_crackback} lethal crackback damage next turn.",
                            simulated_counterplay=f"Opponent blocks profitably and swings back for lethal ({all_crackback} dmg) on their turn.",
                            projected_state={
                                "hero_life": max(0, hero_life - all_crackback),
                                "opp_life": max(0, opp_life - all_dmg_through),
                                "hero_power": 0,
                            },
                        )
                    )

        from arenamcp.afterstate import (
            Action,
            ActivateAbility,
            AfterstateSimulator,
            BoardState,
            CastSpell,
            PlayLand,
            score_board_state,
        )

        board_view = BoardState.from_game_state(game_state, config=fmt_config)

        # 2. Evaluate Multi-Step Sequences (Land Drop + Primary Spell + Interaction Hold)
        for land in playable_lands:
            land_name = land.get("name") or "Land"
            mana_after_land = available_mana + 1

            # Find all spells affordable after the land drop (or confirmed legal by GRE)
            afford_after_land = [
                s for s in playable_spells if s["cmc"] <= mana_after_land or s["is_gre_legal"]
            ]
            if afford_after_land:
                # Prioritize commanders, power, and CMC
                afford_after_land.sort(
                    key=lambda s: (
                        10 if s["is_commander"] else 0,
                        s["cmc"],
                        int(s["card"].get("power") or 0),
                    ),
                    reverse=True,
                )
                for top_entry in afford_after_land[:3]:
                    top_spell = top_entry["card"]
                    spell_cmc = top_entry["cmc"]
                    cost_str = top_entry["cost_str"]
                    is_cmd = top_entry["is_commander"]
                    spell_name = top_spell.get("name") or "Spell"
                    power_add = int(top_spell.get("power") or 0)
                    tough_add = int(top_spell.get("toughness") or 0)
                    oracle = str(top_spell.get("oracle_text") or "")

                    # Simulate 1-ply afterstate through AfterstateSimulator
                    land_act = PlayLand(land_name=land_name)
                    land_after = AfterstateSimulator.apply(board_view, land_act)
                    spell_act = CastSpell(
                        card_name=spell_name,
                        from_zone="command" if is_cmd else "hand",
                        cmc=spell_cmc,
                        is_commander=is_cmd,
                        oracle_text=oracle,
                    )
                    seq_after = AfterstateSimulator.apply(land_after.board, spell_act)
                    seq_val = score_board_state(seq_after.board)
                    v_delta = seq_val - base_val
                    power_add = seq_after.delta.power_added or power_add

                    rem_mana = max(0, mana_after_land - spell_cmc)
                    cast_label = f"Cast Commander: {spell_name}" if is_cmd else f"Cast: {spell_name}"
                    steps = [
                        f"Play Land: {land_name}",
                        f"{cast_label} [{cost_str}]",
                    ]
                    if rem_mana > 0:
                        steps.append(f"Hold Priority with {rem_mana} open mana")

                    # Factor in Opponent archetype counterplay prediction
                    counterplay_note = "Opponent must respect development; priority passes to opponent."
                    if opp_profile and opp_profile.open_mana_threats:
                        counterplay_note = (
                            f"Playing around {opp_profile.open_mana_threats[0]} with {rem_mana} mana up."
                        )

                    tag = "⭐ BEST LINE" if is_cmd or power_add >= 3 else "⚡ TEMPO"
                    action_title = f"Sequence: Play {land_name} -> {cast_label}"
                    outcome_msg = (
                        " → ".join(seq_after.trigger_trace)
                        if seq_after.trigger_trace
                        else f"Curves mana smoothly; develops {spell_name} while maintaining {rem_mana} open mana."
                    )
                    branches.append(
                        MCTSBranch(
                            action=action_title,
                            action_type="sequence",
                            mana_cost=cost_str,
                            sequence_steps=steps,
                            win_probability=round(seq_val, 3),
                            value_delta=round(v_delta, 3),
                            simulated_visits=0,
                            prior_probability=0.45 if is_cmd else 0.35,
                            tag=tag,
                            outcome_summary=outcome_msg,
                            simulated_counterplay=counterplay_note,
                            projected_state={
                                "hero_life": hero_life,
                                "opp_life": opp_life,
                                "hero_power": hero_power + power_add,
                            },
                        )
                    )

            # Standalone land drop candidate
            land_act = PlayLand(land_name=land_name)
            land_after = AfterstateSimulator.apply(board_view, land_act)
            land_val = score_board_state(land_after.board)
            land_delta = land_val - base_val
            branches.append(
                MCTSBranch(
                    action=f"Play Land: {land_name}",
                    action_type="land",
                    sequence_steps=[f"Play Land: {land_name}"],
                    win_probability=round(land_val, 3),
                    value_delta=round(land_delta, 3),
                    simulated_visits=0,
                    prior_probability=0.25,
                    tag="NORMAL",
                    outcome_summary=f"Plays {land_name}; develops mana base.",
                    simulated_counterplay="Opponent receives priority.",
                    projected_state={
                        "hero_life": hero_life,
                        "opp_life": opp_life,
                        "hero_power": hero_power,
                    },
                )
            )

        # 3. Evaluate Individual Spells Castable Now
        for entry in playable_spells:
            card = entry["card"]
            cmc = entry["cmc"]
            cost_str = entry["cost_str"]
            is_cmd = entry["is_commander"]
            is_gre_legal = entry["is_gre_legal"]

            name = card.get("name") or "Card"
            t_line = str(card.get("type_line") or "").lower()
            oracle = str(card.get("oracle_text") or "")

            if cmc <= available_mana or is_gre_legal:
                power_add = int(card.get("power") or 0)
                tough_add = int(card.get("toughness") or 0)

                # Simulate spell afterstate
                spell_act = CastSpell(
                    card_name=name,
                    from_zone="command" if is_cmd else "hand",
                    cmc=cmc,
                    is_commander=is_cmd,
                    oracle_text=oracle,
                )
                spell_after = AfterstateSimulator.apply(board_view, spell_act)
                spell_val = score_board_state(spell_after.board)
                v_delta = spell_val - base_val
                power_add = spell_after.delta.power_added or power_add

                if spell_after.trigger_trace:
                    outcome_msg = " → ".join(spell_after.trigger_trace)
                else:
                    outcome_msg = f"Casts {name}; leaves {max(0, available_mana - cmc)} open mana."

                is_removal = any(w in oracle.lower() for w in ("destroy", "exile", "deals", "return target"))
                is_counter = "counter target" in oracle.lower()

                if "creature" in t_line:
                    card_impact = (power_add + tough_add) * 0.03
                    if is_cmd:
                        card_impact += 0.08
                    outcome_msg = f"Adds {power_add}/{tough_add} creature presence; leaves {max(0, available_mana - cmc)} mana open"
                elif is_removal:
                    card_impact = 0.12
                    outcome_msg = "Removes top opponent threat; swings board power delta"
                elif is_counter:
                    card_impact = 0.09
                    outcome_msg = "Holds counterspell permission for opponent's key threat"
                else:
                    card_impact = 0.05
                    outcome_msg = f"Resolves spell effect; utilizes {cmc} mana"

                spell_val = base_val + card_impact
                spell_val = max(0.05, min(0.95, spell_val))
                v_delta = spell_val - base_val

                # Check if tapping out creates a blunder trap
                if available_mana - cmc == 0 and opp_power >= hero_life and hero_life <= 10:
                    blunder_traps.append(
                        MCTSBranch(
                            action=f"Tap Out: Cast {name}",
                            action_type="cast",
                            mana_cost=cost_str,
                            win_probability=round(max(0.05, base_val - 0.25), 3),
                            value_delta=round(-0.25, 3),
                            simulated_visits=0,
                            prior_probability=0.08,
                            tag="⚠️ BLUNDER TRAP",
                            outcome_summary=f"Tapping out for {name} leaves zero defensive mana against opponent's {opp_power} lethal board power.",
                            simulated_counterplay="Opponent swings with full board on their turn for lethal.",
                            projected_state={
                                "hero_life": max(0, hero_life - opp_power),
                                "opp_life": opp_life,
                                "hero_power": hero_power + power_add,
                            },
                        )
                    )

                cast_action = f"Cast Commander: {name}" if is_cmd else f"Cast: {name}"
                tag = "👑 COMMANDER" if is_cmd else ("⚡ TEMPO" if v_delta > 0.05 else "NORMAL")

                branches.append(
                    MCTSBranch(
                        action=cast_action,
                        action_type="cast",
                        mana_cost=cost_str,
                        sequence_steps=[f"{cast_action} [{cost_str}]"],
                        win_probability=round(spell_val, 3),
                        value_delta=round(v_delta, 3),
                        simulated_visits=0,
                        prior_probability=0.40 if is_cmd else 0.20,
                        tag=tag,
                        outcome_summary=outcome_msg,
                        simulated_counterplay="Opponent considers priority window; potential counter or removal response",
                        projected_state={
                            "hero_life": hero_life,
                            "opp_life": opp_life,
                            "hero_power": hero_power + power_add,
                        },
                    )
                )

        # 3b. Evaluate Activated Abilities (e.g. Food sacrifice)
        if board_view.token_counts.get("Food", 0) > 0 and available_mana >= 2:
            food_act = ActivateAbility(permanent_name="Food", is_food_sacrifice=True)
            food_after = AfterstateSimulator.apply(board_view, food_act)
            food_val = score_board_state(food_after.board)
            food_delta = food_val - base_val
            branches.append(
                MCTSBranch(
                    action="Sacrifice Food: Gain 3 Life",
                    action_type="activate",
                    sequence_steps=["Pay {2}, {T}, Sacrifice Food → Gain 3 Life"],
                    win_probability=round(food_val, 3),
                    value_delta=round(food_delta, 3),
                    simulated_visits=0,
                    prior_probability=0.25,
                    tag="🛡️ RECOVERY" if hero_life <= fmt_config.lethal_zone else "NORMAL",
                    outcome_summary=f"Gains 3 life (Hero life {hero_life} -> {hero_life + 3}); hedges against lethal crackback.",
                    simulated_counterplay="Opponent responds to lifegain trigger or receives priority.",
                    projected_state={
                        "hero_life": hero_life + 3,
                        "opp_life": opp_life,
                        "hero_power": hero_power,
                    },
                )
            )

        # 4. Evaluate Pass Priority / Hold Open Mana
        has_instants_or_flashes = any("instant" in str(c.get("type_line") or "").lower() for c in hand)
        if available_mana > 0 and has_instants_or_flashes:
            pass_val = base_val + fmt_config.hold_mana_value
            branches.append(
                MCTSBranch(
                    action=f"Pass Priority (Hold {available_mana} Open Mana)",
                    action_type="pass",
                    sequence_steps=[f"Pass Priority / Hold {available_mana} open mana"],
                    win_probability=round(min(0.95, pass_val), 3),
                    value_delta=round(pass_val - base_val, 3),
                    simulated_visits=0,
                    prior_probability=0.20,
                    tag="🛡️ SAFE",
                    outcome_summary=f"Bluffs/holds interaction ({available_mana} mana up); forces opponent to play around open mana",
                    simulated_counterplay="Opponent must decide whether to cast into open mana or pass",
                    projected_state={
                        "hero_life": hero_life,
                        "opp_life": opp_life,
                        "hero_power": hero_power,
                    },
                )
            )
        else:
            pass_val = base_val - (0.05 if available_mana >= 3 else 0.0)
            branches.append(
                MCTSBranch(
                    action="Pass Priority",
                    action_type="pass",
                    sequence_steps=["Pass Priority"],
                    win_probability=round(max(0.05, pass_val), 3),
                    value_delta=round(pass_val - base_val, 3),
                    simulated_visits=0,
                    prior_probability=0.10,
                    tag="NORMAL",
                    outcome_summary="Yields priority without action; passes turn step",
                    simulated_counterplay="Opponent receives priority.",
                    projected_state={
                        "hero_life": hero_life,
                        "opp_life": opp_life,
                        "hero_power": hero_power,
                    },
                )
            )

        # Check gating and apply 1-ply batched MageZero RL lookahead if in-distribution
        expected_opp_actions: list[str] = []
        base_val, eval_source, expected_opp_actions = cls._apply_magezero_lookahead(
            game_state=game_state,
            base_val=base_val,
            branches=branches,
            opp_profile=opp_profile,
        )

        # Sort branches by win probability descending
        branches.sort(key=lambda b: b.win_probability, reverse=True)

        # Mark top branch as Best Line
        if branches:
            branches[0].tag = "⭐ BEST LINE"
            best_action = branches[0].action
        else:
            best_action = "Pass Priority"

        payload = MCTSTreePayload(
            root_win_probability=round(base_val, 3),
            total_simulations=len(branches),
            turn_number=turn_num,
            phase=phase,
            best_action=best_action,
            hero_life=hero_life,
            opp_life=opp_life,
            available_mana=available_mana,
            eval_source=eval_source,
            opponent_threat_summary=opp_threat,
            opponent_profile=opp_profile,
            expected_opponent_actions=expected_opp_actions,
            format_summary=fmt_profile.format_summary(),
            branches=branches,
            blunder_traps=blunder_traps,
        )
        cls._last_sig = sig
        cls._last_payload = payload
        cls._last_payload_at = time.monotonic()
        return payload

    @classmethod
    def _create_mechanical_afterstate(
        cls,
        game_state: dict[str, Any],
        branch: MCTSBranch,
    ) -> dict[str, Any] | None:
        """Build the state after a SUPPORTED mechanical transition, else None.

        Explicitly supported set (everything else is unsupported/unverified
        and returns None so the branch falls back to prior-based pseudo
        values instead of a fabricated "verified" afterstate):
        - PLAY LAND: the land is in the top-level hand, no land already
          entered this turn and the land drop is unused; entry honors the
          card's own tapped-entry signal: tapped entry when the
          producer/transcript indicates ETB tapped, untapped only for basic
          lands or when no indicator says otherwise.
        - CAST: generic permanent (creature/enchantment/artifact) whose FULL
          mana cost is payable from available mana — generic-only cost from
          the mana pool, or colored cost confirmed payable by GRE legality.
        Unsupported (always None): instants/sorceries requiring stack
        resolution, targets, ETB triggers, combat-damage projection as combat
        state, convoke/alternative costs, X-spells.
        """
        import copy
        import re

        local_seat = game_state.get("local_seat_id") or 1
        act_type = branch.action_type.lower()

        if act_type == "land":
            land_name = branch.action.replace("Play Land:", "").strip()
            state_copy = copy.deepcopy(game_state)
            hand = state_copy.get("hand") or []
            found_idx = next(
                (i for i, c in enumerate(hand) if isinstance(c, dict) and c.get("name") == land_name),
                None,
            )
            if found_idx is None:
                return None
            # Land-drop coherence: at most one land per turn.
            hero_player = next(
                (p for p in state_copy.get("players", []) if isinstance(p, dict)
                 and (p.get("is_local") or p.get("seat_id") == local_seat)),
                None,
            )
            if hero_player and int(hero_player.get("lands_played") or 0) > 0:
                return None
            current_turn = int((state_copy.get("turn") or {}).get("turn_number") or 1)
            lands_this_turn = sum(
                1 for c in state_copy.get("battlefield") or []
                if isinstance(c, dict)
                and c.get("owner_seat_id") == local_seat
                and c.get("turn_entered_battlefield") == current_turn
                and "land" in str(c.get("type_line") or "").lower()
            )
            if lands_this_turn > 0:
                return None
            card = hand.pop(found_idx)
            bf = state_copy.setdefault("battlefield", [])
            card["controller_seat_id"] = local_seat
            card["owner_seat_id"] = local_seat
            # Tapped-entry coherence: trust the producer's ETB-tapped signal
            # when present; otherwise basic lands enter untapped. Unknown
            # typed lands (no signal) fall back to tapped entry — a tapped
            # source is the conservative assumption and is never presented
            # as a verified untapped one.
            etb_tapped = card.get("enters_tapped")
            if etb_tapped is None and str(card.get("oracle_text") or ""):
                etb_tapped = bool(
                    re.search(r"enters the battlefield tapped", str(card["oracle_text"]), re.I)
                )
            if etb_tapped is None:
                t_line = str(card.get("type_line") or "").lower()
                etb_tapped = "basic" not in t_line and bool(t_line)
            card["is_tapped"] = bool(etb_tapped)
            card["is_summoning_sick"] = False
            turn_value = current_turn if current_turn else None
            if turn_value is not None:
                card["turn_entered_battlefield"] = turn_value
            bf.append(card)
            for p in state_copy.get("players", []):
                if isinstance(p, dict) and (p.get("is_local") or p.get("seat_id") == local_seat):
                    p["lands_played"] = int(p.get("lands_played") or 0) + 1
            return state_copy

        if act_type == "attack":
            # Attack "afterstates" are life projections, not resolved combat
            # states: no tapped attackers, no blockers, no damage ordering,
            # no first-strike/trample rules. Never certify them.
            return None

        if act_type == "cast":
            spell_name = branch.action.replace("Cast:", "").split("[")[0].strip()
            state_copy = copy.deepcopy(game_state)
            hand = state_copy.get("hand") or []
            found_idx = next(
                (i for i, c in enumerate(hand) if isinstance(c, dict) and c.get("name") == spell_name),
                None,
            )
            if found_idx is None:
                return None
            card = hand.pop(found_idx)
            t_line = str(card.get("type_line") or "").lower()
            if not ("creature" in t_line or "enchantment" in t_line or "artifact" in t_line):
                # Instants/sorceries resolve through the stack: targets,
                # counters, replacement effects — none of that is verified
                # by this adapter.
                return None

            # Reject cards with ETB triggers or target requirements (cannot be modeled as simple resolution)
            oracle_text = str(card.get("oracle_text") or "").lower()
            if oracle_text:
                if re.search(r"\b(when(ever)?|as)\b[^.\n]*\benters(\s+the\s+battlefield)?\b", oracle_text):
                    return None
                if "target" in oracle_text:
                    return None

            # Full-cost payment check: parse the card's mana cost and confirm
            # it is payable from the local player's mana pool.
            cost_raw = card.get("mana_cost") if card.get("mana_cost") is not None else card.get("cost")
            if cost_raw is None:
                return None
            cost_str = str(cost_raw).strip()
            if not cost_str:
                return None

            symbols = re.findall(r"\{([^}]+)\}", cost_str)
            if not symbols:
                return None
            remainder = re.sub(r"\{[^}]+\}", "", cost_str).strip()
            if remainder:
                return None

            generics = 0
            colored: dict[str, int] = {}
            for sym in symbols:
                sym = sym.strip()
                if sym.isdigit():
                    generics += int(sym)
                elif sym.upper() in {"W", "U", "B", "R", "G", "C"}:
                    colored[sym.upper()] = colored.get(sym.upper(), 0) + 1
                else:
                    # Nonstandard symbols ({W/U}, {W/P}, {X}, etc.) are unsupported
                    return None

            mana_pool: dict[str, int] = {}
            for p in state_copy.get("players", []):
                if isinstance(p, dict) and (p.get("is_local") or p.get("seat_id") == local_seat):
                    raw_pool = p.get("mana_pool") or {}
                    if isinstance(raw_pool, dict):
                        mana_pool = {str(k): int(v or 0) for k, v in raw_pool.items()}
                    break

            # Colored requirements must be satisfied
            for c, req in colored.items():
                if mana_pool.get(c, 0) < req:
                    return None

            # Generic requirements must be satisfied
            total_pool = sum(mana_pool.values())
            total_colored_req = sum(colored.values())
            if total_pool < total_colored_req + generics:
                return None

            bf = state_copy.setdefault("battlefield", [])
            # Deduct the paid mana from the local player's pool (colored
            # requirements first, then generic greedily largest-color).
            for p in state_copy.get("players", []):
                if isinstance(p, dict) and (p.get("is_local") or p.get("seat_id") == local_seat):
                    pool = {str(k): int(v or 0) for k, v in (p.get("mana_pool") or {}).items()}
                    for c, n in colored.items():
                        pool[c] = pool.get(c, 0) - n
                    p["mana_pool"] = cls._deduct_mana(pool, generics)
                    break
            card["controller_seat_id"] = local_seat
            card["owner_seat_id"] = local_seat
            card["is_tapped"] = False
            card["is_summoning_sick"] = "creature" in t_line
            bf.append(card)
            return state_copy

        return None

    @staticmethod
    def _deduct_mana(pool: dict[str, int], amount: int) -> dict[str, int]:
        """Deduct ``amount`` total mana from a pool, largest colors first."""
        remaining = amount
        out = dict(pool)
        for key in sorted(out, key=lambda k: -out[k]):
            if remaining <= 0:
                break
            take = min(int(out.get(key, 0) or 0), remaining)
            if take > 0:
                out[key] = int(out[key]) - take
                remaining -= take
        return {k: max(0, int(v)) for k, v in out.items()}

    @classmethod
    def _apply_magezero_lookahead(
        cls,
        game_state: dict[str, Any],
        base_val: float,
        branches: list[MCTSBranch],
        opp_profile: OpponentProfile,
    ) -> tuple[float, str, list[str]]:
        """Apply 1-ply batched afterstate evaluation and policy prior calibration if gated."""
        from arenamcp.format_profile import detect_format_profile
        from arenamcp.magezero_client import MageZeroClient
        from arenamcp.magezero_gating import sample_opponent_hands_meta
        from arenamcp.magezero_policy import (
            compute_legal_action_priors,
            decode_opponent_threats,
            map_action_to_xmage_text,
        )
        from arenamcp.model_zoo import ModelZooClient

        fmt_profile = detect_format_profile(game_state)
        # Extract hero deck cards via consolidated extract_hero_deck helper
        from arenamcp.magezero_gating import extract_hero_deck

        extraction = extract_hero_deck(game_state)
        if not extraction.is_compatible or not extraction.cards:
            return base_val, "Tactical Heuristic Lookahead", []

        selection = ModelZooClient.select(fmt_profile, extraction.cards)
        if not selection or not MageZeroClient.check_health():
            return base_val, "Tactical Heuristic Lookahead", []

        eval_label = f"{selection.label} ({selection.similarity:.0%})"
        model_id = selection.model_spec.model_id
        meta = sample_opponent_hands_meta(game_state, num_samples=8)
        if not meta.hand_count_known:
            # Unknown opponent hand count cannot be represented as known empty hands;
            # fall back explicitly to heuristic lookahead.
            return base_val, "Tactical Heuristic Lookahead", []
        opp_hands = meta.samples

        # Build batch items: root state (with 8 sampled hands) + candidate afterstates
        items: list[tuple[dict[str, Any], list[str] | None]] = [
            (game_state, hand) for hand in opp_hands
        ]

        evaluated_branch_map: dict[int, int] = {}
        for b_idx, branch in enumerate(branches):
            afterstate = cls._create_mechanical_afterstate(game_state, branch)
            if afterstate is not None:
                start_offset = len(items)
                evaluated_branch_map[b_idx] = start_offset
                for hand in opp_hands:
                    items.append((afterstate, hand))
            else:
                # Unsupported/unverifiable transition (task08): the branch
                # carries NO verified neural afterstate. Attribution is kept
                # here so ranking/presentation (task09) can distinguish
                # prior-only branches from measured ones.
                branch.details["afterstate_supported"] = False
                branch.details["afterstate_unavailable_reason"] = (
                    "unsupported mechanical transition "
                    "(stack/ETB/combat not representable reliably)"
                )

        batch_results = MageZeroClient.evaluate_batch(items, model_id=model_id)
        n_rows = len(items)
        row_results = cls._validate_batch_rows(batch_results, n_rows)
        if row_results is None:
            logger.info(
                "MageZero batch failed validation; preserving heuristic result "
                "(reject reason: %s)",
                MageZeroClient.last_reject_reason(),
            )
            return base_val, "Tactical Heuristic Lookahead", []

        # 1. Average root prediction (first 8 rows, one per sampled opponent hand)
        root_rows = row_results[:8]
        root_nn_val = sum(r["value"] for r in root_rows) / 8.0
        root_win_p = max(0.02, min(0.98, (root_nn_val + 1.0) / 2.0))
        base_val = root_win_p

        # 2. Average policy logits over the 8 root predictions
        avg_policy_player = [
            sum(root_rows[j]["policy_player"][i] for j in range(8)) / 8.0
            for i in range(128)
        ]
        avg_policy_opp = [
            sum(root_rows[j]["policy_opponent"][i] for j in range(8)) / 8.0
            for i in range(128)
        ]

        # 3. Compute legal action priors
        candidate_actions = [
            (b.action, map_action_to_xmage_text(b.action, b.action_type))
            for b in branches
        ]
        priors = compute_legal_action_priors(candidate_actions, avg_policy_player, temperature=1.5)

        # 4. Assign win_probability, value_delta, and priors to branches
        for b_idx, branch in enumerate(branches):
            branch.prior_probability = priors.get(branch.action, 0.0)
            if b_idx in evaluated_branch_map:
                start = evaluated_branch_map[b_idx]
                cand_vals = [r["value"] for r in row_results[start : start + 8]]
                if cand_vals:
                    cand_nn_val = sum(cand_vals) / len(cand_vals)
                    delta_v = cand_nn_val - root_nn_val
                    branch.value_delta = round(delta_v, 3)
                    branch.win_probability = round(max(0.02, min(0.98, (cand_nn_val + 1.0) / 2.0)), 3)
                else:
                    branch.value_delta = 0.0
                    branch.win_probability = round(base_val, 3)
            else:
                p_diff = branch.prior_probability - (1.0 / len(branches) if branches else 1.0)
                delta_p = round(p_diff * 0.12, 3)
                branch.value_delta = delta_p
                branch.win_probability = round(max(0.02, min(0.98, base_val + delta_p)), 3)

        # 5. Decode opponent threats
        opp_threats = decode_opponent_threats(avg_policy_opp, top_k=3)

        return base_val, eval_label, opp_threats

    @classmethod
    def _validate_batch_rows(
        cls,
        batch_results: list[dict[str, Any]] | None,
        n_rows: int,
    ) -> list[dict[str, Any]] | None:
        """Validate every batch row before any value is consumed.

        Requires exactly ``n_rows`` results, bound to request rows by
        request_index when present, with finite in-range values and 128-wide
        policy heads. Any violation returns None (heuristic path preserved).
        """
        if not isinstance(batch_results, list) or len(batch_results) != n_rows:
            return None
        # Ordering policy mirrored from magezero_client._validate_result:
        # partial request_index echo -> reject; full echo -> verify order;
        # no echo -> legacy positional sync WITHOUT a verified-ordering claim.
        echo_flags = [
            isinstance(row, dict) and "request_index" in row for row in batch_results
        ]
        if any(echo_flags) and not all(echo_flags):
            return None
        has_echo = all(echo_flags)
        validated: list[dict[str, Any]] = []
        for row_idx, row in enumerate(batch_results):
            if not isinstance(row, dict):
                return None
            if has_echo:
                req_idx = row.get("request_index")
                if (
                    not isinstance(req_idx, int)
                    or isinstance(req_idx, bool)
                    or req_idx != row_idx
                ):
                    return None
            value = row.get("value")
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or not -1.0 <= float(value) <= 1.0
            ):
                return None
            for key in ("policy_player", "policy_opponent"):
                head = row.get(key)
                if (
                    not isinstance(head, list)
                    or len(head) != 128
                    or any(
                        not isinstance(x, (int, float))
                        or isinstance(x, bool)
                        or not math.isfinite(float(x))
                        for x in head
                    )
                ):
                    return None
            validated.append(row)
        return validated
