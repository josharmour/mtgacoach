"""State Encoder bridging MTGA GameState snapshots into MageZero feature vectors.

Produces feature indices mathematically identical to XMage's Features.java
and StateEncoder.java (WillWroble/mage fork), ensuring emitted tokens survive
the server's ignore.roar filter and reach the trained neural network.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from arenamcp.magezero_hash import path_index

logger = logging.getLogger(__name__)

# Template path resolution
_TEMPLATE_PATHS = [
    Path(__file__).parent / "data" / "magezero_card_templates.json",
    Path(__file__).parents[2] / "tools" / "magezero" / "card_templates.json",
]


def _load_card_templates() -> dict[str, dict[str, list[str]]]:
    for p in _TEMPLATE_PATHS:
        if p.is_file():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.debug("Failed loading card templates from %s: %s", p, e)
    return {}


_CARD_TEMPLATES = _load_card_templates()

# Step/Phase mapping from GRE names to XMage TurnStepType enums
_STEP_MAP = {
    "Phase_Main1": "PRECOMBAT_MAIN",
    "Phase_Main2": "POSTCOMBAT_MAIN",
    "Step_BeginCombat": "BEGIN_COMBAT",
    "Step_DeclareAttack": "DECLARE_ATTACKERS",
    "Step_DeclareBlock": "DECLARE_BLOCKERS",
    "Step_CombatDamage": "COMBAT_DAMAGE",
    "Step_EndCombat": "END_COMBAT",
    "Step_Upkeep": "UPKEEP",
    "Step_Draw": "DRAW",
    "Step_End": "END_TURN",
    "Step_Cleanup": "CLEANUP",
}

_NUMERIC_BREAKPOINTS = (32, 64, 128, 256, 512)


def thermometer_tokens(prefix: str, val: int, max_range: int = 20) -> list[str]:
    """Emit thermometer tokens prefix@0..prefix@k-1 plus powers-of-two breakpoints."""
    tokens = [f"{prefix}@{i}#1" for i in range(min(max(0, val), max_range))]
    for bp in _NUMERIC_BREAKPOINTS:
        if val >= bp:
            tokens.append(f"{prefix}@{bp}#1")
    return tokens


class MageZeroStateEncoder:
    """Encodes MTGA GameState snapshots into verified MageZero feature vectors."""

    @classmethod
    def encode(
        cls,
        game_state: dict[str, Any],
        opponent_hand_cards: list[str] | None = None,
    ) -> list[int]:
        """Convert game state snapshot into a bag of 64-bit feature indices."""
        if not isinstance(game_state, dict):
            return []

        indices: list[int] = []

        turn = game_state.get("turn") or {}
        raw_phase = str(turn.get("phase") or "")
        raw_step = str(turn.get("step") or "")

        # 1. Root Step & Decision Tokens
        step_name = _STEP_MAP.get(raw_step) or _STEP_MAP.get(raw_phase) or "PRECOMBAT_MAIN"
        indices.append(path_index([f"{step_name}#1"]))
        indices.append(path_index(["PRIORITY#1"]))
        indices.append(path_index(["priority#1"]))

        # 2. Resolve Players & Turn Context
        players = game_state.get("players") or []
        local_seat = game_state.get("local_seat_id")
        if local_seat is None:
            for p in players:
                if isinstance(p, dict) and p.get("is_local"):
                    local_seat = p.get("seat_id")
                    break
        if local_seat is None:
            local_seat = 1

        active_seat = turn.get("active_player", local_seat)
        priority_seat = turn.get("priority_player", local_seat)

        hero_p: dict[str, Any] = {}
        opp_p: dict[str, Any] = {}
        for p in players:
            if not isinstance(p, dict):
                continue
            if p.get("is_local") or p.get("seat_id") == local_seat:
                hero_p = p
            else:
                opp_p = p

        # 3. Encode Hero (Player#1) and Opponent (Opponent#1)
        cls._encode_player(
            namespace="Player#1",
            player_dict=hero_p,
            is_active=(active_seat == local_seat),
            has_priority=(priority_seat == local_seat),
            battlefield=[
                c
                for c in (game_state.get("battlefield") or [])
                if isinstance(c, dict)
                and (c.get("controller_seat_id") == local_seat or c.get("owner_seat_id") == local_seat)
            ],
            hand_cards=[c.get("name", "") for c in (game_state.get("hand") or []) if isinstance(c, dict)],
            graveyard_cards=[
                c.get("name", "")
                for c in (game_state.get("graveyard") or [])
                if isinstance(c, dict)
                and (c.get("controller_seat_id") == local_seat or c.get("owner_seat_id") == local_seat)
            ],
            command_cards=[
                c
                for c in (game_state.get("command") or [])
                if isinstance(c, dict)
                and (c.get("controller_seat_id") == local_seat or c.get("owner_seat_id") == local_seat)
            ],
            indices=indices,
        )

        cls._encode_player(
            namespace="Opponent#1",
            player_dict=opp_p,
            is_active=(active_seat != local_seat),
            has_priority=(priority_seat != local_seat),
            battlefield=[
                c
                for c in (game_state.get("battlefield") or [])
                if isinstance(c, dict)
                and (c.get("controller_seat_id") != local_seat and c.get("owner_seat_id") != local_seat)
            ],
            hand_cards=opponent_hand_cards or [],
            graveyard_cards=[
                c.get("name", "")
                for c in (game_state.get("graveyard") or [])
                if isinstance(c, dict)
                and (c.get("controller_seat_id") != local_seat and c.get("owner_seat_id") != local_seat)
            ],
            command_cards=[
                c
                for c in (game_state.get("command") or [])
                if isinstance(c, dict)
                and (c.get("controller_seat_id") != local_seat and c.get("owner_seat_id") != local_seat)
            ],
            indices=indices,
        )

        # 4. Stack namespace
        stack = game_state.get("stack") or []
        for idx, item in enumerate(stack, start=1):
            if isinstance(item, dict):
                s_name = str(item.get("name") or "Spell")
                indices.append(path_index(["Stack#1", f"Cast {s_name}#{idx}", "targets#1"]))

        return sorted(set(indices))

    @classmethod
    def _encode_player(
        cls,
        namespace: str,
        player_dict: dict[str, Any],
        is_active: bool,
        has_priority: bool,
        battlefield: list[dict[str, Any]],
        hand_cards: list[str],
        graveyard_cards: list[str],
        command_cards: list[dict[str, Any]] | None,
        indices: list[int],
    ) -> None:
        # Player state flags
        if is_active:
            indices.append(path_index([namespace, "IsActivePlayer#1"]))
        if has_priority:
            indices.append(path_index([namespace, "IsDecisionPlayer#1"]))

        lands_played = player_dict.get("lands_played", 0)
        if is_active and lands_played == 0:
            indices.append(path_index([namespace, "CanPlayLand#1"]))

        # Life total thermometer
        life = int(player_dict.get("life_total") if player_dict.get("life_total") is not None else 20)
        for t in thermometer_tokens("LifeTotal", life, max_range=20):
            indices.append(path_index([namespace, t]))

        # Library count thermometer
        lib_count = int(player_dict.get("library_count") if player_dict.get("library_count") is not None else 40)
        for t in thermometer_tokens("LibraryCount", lib_count, max_range=20):
            indices.append(path_index([namespace, t]))

        # Mana pool counts
        mana_pool = player_dict.get("mana_pool") or {}
        if isinstance(mana_pool, dict):
            for color, count in mana_pool.items():
                c_clean = str(color).upper()
                for k in range(1, int(count) + 1):
                    indices.append(path_index([namespace, "ManaPool#1", f"add {{{c_clean}}}#{k}"]))

        # Battlefield
        cls._encode_battlefield(namespace, battlefield, indices)

        # Hand
        cls._encode_hand(namespace, hand_cards, indices)

        # Graveyard
        cls._encode_graveyard(namespace, graveyard_cards, indices)

        # Command Zone for Brawl / Commander
        if command_cards:
            for c in command_cards:
                c_name = str(c.get("name") or "Commander")
                indices.append(path_index([namespace, "CommandZone#1", f"{c_name}#1"]))
                casts = int(c.get("commander_casts") or 0)
                if casts > 0:
                    indices.append(path_index([namespace, "CommandZone#1", f"Casts@{casts}#1"]))

    @classmethod
    def _encode_battlefield(
        cls,
        player_ns: str,
        battlefield: list[dict[str, Any]],
        indices: list[int],
    ) -> None:
        bf_ns = [player_ns, "Battlefield#1"]
        card_counts: dict[str, int] = {}
        perm_counter = 0

        for perm in battlefield:
            name = str(perm.get("name") or "").strip()
            if not name:
                continue

            perm_counter += 1
            card_counts[name] = card_counts.get(name, 0) + 1
            inst_num = card_counts[name]

            # Zone-pooled features
            indices.append(path_index(bf_ns + [f"Card#{perm_counter}"]))
            indices.append(path_index(bf_ns + [f"Permanent#{perm_counter}"]))

            t_line = str(perm.get("type_line") or "").lower()
            if "creature" in t_line:
                indices.append(path_index(bf_ns + [f"CREATURE#{inst_num}"]))
            elif "land" in t_line:
                indices.append(path_index(bf_ns + [f"LAND#{inst_num}"]))
            elif "enchantment" in t_line:
                indices.append(path_index(bf_ns + [f"ENCHANTMENT#{inst_num}"]))
            elif "artifact" in t_line:
                indices.append(path_index(bf_ns + [f"ARTIFACT#{inst_num}"]))

            # Card-level namespace
            card_ns = bf_ns + [f"{name}#{inst_num}"]

            is_tapped = bool(perm.get("is_tapped"))
            if is_tapped:
                indices.append(path_index(card_ns + ["Tapped#1"]))

            is_sick = bool(perm.get("is_summoning_sick"))
            if is_sick:
                indices.append(path_index(card_ns + ["SummoningSick#1"]))

            is_attacking = bool(perm.get("is_attacking"))
            if is_attacking:
                indices.append(path_index(card_ns + ["Attacking#1"]))

            # Power/Toughness thermometer for creatures
            if "creature" in t_line:
                power = int(perm.get("power") or 0)
                toughness = int(perm.get("toughness") or 0)
                for t in thermometer_tokens("Power", power, max_range=10):
                    indices.append(path_index(card_ns + [t]))
                for t in thermometer_tokens("Toughness", toughness, max_range=10):
                    indices.append(path_index(card_ns + [t]))

            # Precomputed static & ability feature templates from XMage training
            tmpl = _CARD_TEMPLATES.get(name, {}).get("battlefield", [])
            if tmpl:
                for feat in tmpl:
                    indices.append(path_index(card_ns + [f"{feat}#1"]))
            else:
                from arenamcp.ability_synthesizer import AbilitySynthesizer

                oracle = str(perm.get("oracle_text") or "")
                card_abs = AbilitySynthesizer.parse_card(name, oracle)
                for feat in card_abs.to_xmage_features():
                    indices.append(path_index(card_ns + [feat]))

    @classmethod
    def _encode_hand(
        cls,
        player_ns: str,
        hand_cards: list[str],
        indices: list[int],
    ) -> None:
        hand_ns = [player_ns, "Hand#1"]
        card_counts: dict[str, int] = {}
        for name in hand_cards:
            name = name.strip()
            if not name:
                continue
            card_counts[name] = card_counts.get(name, 0) + 1
            inst_num = card_counts[name]
            card_ns = hand_ns + [f"{name}#{inst_num}"]

            tmpl = _CARD_TEMPLATES.get(name, {}).get("hand", [])
            for feat in tmpl:
                indices.append(path_index(card_ns + [f"{feat}#1"]))

    @classmethod
    def _encode_graveyard(
        cls,
        player_ns: str,
        graveyard_cards: list[str],
        indices: list[int],
    ) -> None:
        gy_ns = [player_ns, "Graveyard#1"]
        card_counts: dict[str, int] = {}
        for name in graveyard_cards:
            name = name.strip()
            if not name:
                continue
            card_counts[name] = card_counts.get(name, 0) + 1
            inst_num = card_counts[name]
            card_ns = gy_ns + [f"{name}#{inst_num}"]
            indices.append(path_index(card_ns + ["Card#1"]))
