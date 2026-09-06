"""MageZero 128-slot policy prior mapping and legal action normalization.

Reproduces XMage's ActionEncoder and MCTSNode.setPriors:
1. Maps action strings ('Play <name>', 'Cast <name>', rule text, 'Pass') to slots 0..127.
2. Applies temperature 1.5 softmax restricted ONLY to legal candidate actions.
3. Applies a +0.10 exploration bonus to non-mana, non-pass actions.
4. Renormalizes to produce calibrated action priors.
"""

from __future__ import annotations

import math
from typing import Sequence

PINNED: dict[str, int] = {
    "Pass": 0,
    "{T}: Add {B}.": 1,
    "{T}: Add {G}.": 2,
    "{T}: Add {R}.": 3,
    "{T}: Add {U}.": 4,
    "{T}: Add {W}.": 5,
    "{T}: Add {C}.": 6,
}

TARGET_PINNED: dict[str, int] = {
    "Stop Choosing": 0,
    "PlayerA": 1,
    "PlayerB": 2,
}


def java_string_hash_code(s: str) -> int:
    """java.lang.String.hashCode(): s = 31*s + c over UTF-16 code units, signed 32-bit."""
    h = 0
    data = s.encode("utf-16-be")
    for i in range(0, len(data), 2):
        h = (31 * h + int.from_bytes(data[i : i + 2], "big")) & 0xFFFFFFFF
    return h - (1 << 32) if h >> 31 else h


def action_index(text: str) -> int:
    """Slot in the 128-wide player/opponent priority heads for an action string."""
    text_clean = text.strip()
    if text_clean in PINNED:
        return PINNED[text_clean]
    return (abs(java_string_hash_code(text_clean)) % 127) + 1


def target_index(entity_name: str) -> int:
    """Slot in the 128-wide target head for an entity name."""
    entity_clean = entity_name.strip()
    if entity_clean in TARGET_PINNED:
        return TARGET_PINNED[entity_clean]
    return (abs(java_string_hash_code(entity_clean)) % 127) + 1


def map_action_to_xmage_text(action_name: str, action_type: str = "") -> str:
    """Translate high-level MTGA candidate action into XMage action representation."""
    act = action_name.strip()
    a_type = action_type.lower()

    if a_type == "pass" or act.lower().startswith("pass"):
        return "Pass"

    # Play Land
    if a_type == "land" or act.startswith("Play Land:"):
        land_name = act.replace("Play Land:", "").strip()
        return f"Play {land_name}"

    # Cast Spell
    if a_type == "cast" or act.startswith("Cast:"):
        spell_name = act.replace("Cast Commander:", "").replace("Cast:", "").strip()
        # Strip trailing brackets like [Cost: ...] if present
        if "[" in spell_name:
            spell_name = spell_name.split("[")[0].strip()
        return f"Cast {spell_name}"

    if act.startswith("Sequence:"):
        # e.g. "Sequence: Play Plains -> Cast Malcolm..."
        # Extract main cast or play
        if "->" in act:
            sub = act.split("->")[1].strip()
            if "Cast" in sub:
                c_name = sub.replace("Cast", "").strip()
                return f"Cast {c_name}"
        return act

    return act


def compute_legal_action_priors(
    candidate_actions: Sequence[tuple[str, str]],  # list of (action_identifier, xmage_text)
    policy_logits: Sequence[float],
    temperature: float = 1.5,
) -> dict[str, float]:
    """Compute calibrated priors for legal candidate actions matching XMage MCTSNode.setPriors.

    Args:
        candidate_actions: Pairs of (candidate_key, xmage_action_string).
        policy_logits: 128-wide raw logits vector from model's policy_player.
        temperature: Logit scaling temperature (default 1.5).

    Returns:
        Mapping from candidate_key to normalized probability.
    """
    if not candidate_actions:
        return {}
    if not policy_logits or len(policy_logits) < 128:
        # Uniform distribution fallback
        u = 1.0 / len(candidate_actions)
        return {cand_id: u for cand_id, _ in candidate_actions}

    # 1. Look up logits for each candidate's slot
    slots: list[int] = []
    scaled_logits: list[float] = []
    for _, text in candidate_actions:
        slot = action_index(text)
        slots.append(slot)
        logit = float(policy_logits[slot])
        scaled_logits.append(logit / temperature)

    # 2. Softmax over legal actions only
    max_logit = max(scaled_logits)
    exp_vals = [math.exp(l - max_logit) for l in scaled_logits]
    sum_exp = sum(exp_vals) or 1.0
    raw_probs = [v / sum_exp for v in exp_vals]

    # 3. Add 0.10 exploration bonus to non-mana, non-pass actions
    boosted_probs: list[float] = []
    for (_, text), p in zip(candidate_actions, raw_probs):
        is_pass = text == "Pass"
        is_mana = text.startswith("{T}: Add") or "Add {" in text
        if not is_pass and not is_mana:
            boosted_probs.append(p + 0.10)
        else:
            boosted_probs.append(p)

    # 4. Renormalize
    sum_boosted = sum(boosted_probs) or 1.0
    final_priors = [round(b / sum_boosted, 4) for b in boosted_probs]

    return {cand_id: pri for (cand_id, _), pri in zip(candidate_actions, final_priors)}


def decode_opponent_threats(
    policy_opponent_logits: Sequence[float],
    candidate_card_pool: Sequence[str] | None = None,
    top_k: int = 3,
) -> list[str]:
    """Decode highest-scoring opponent counterplay actions from policy_opponent head.

    Args:
        policy_opponent_logits: 128-wide logits from model's policy_opponent head.
        candidate_card_pool: Optional subset of cards to consider (e.g. from opponent archetype).
        top_k: Number of threat actions to return.

    Returns:
        List of formatted threat descriptions (e.g. ['Cast Lightning Bolt', 'Cast Spell Pierce']).
    """
    if not policy_opponent_logits or len(policy_opponent_logits) < 128:
        return []

    card_list = list(candidate_card_pool) if candidate_card_pool else []
    if not card_list:
        from arenamcp.magezero_gating import _GAUNTLET_POOLS

        for p_list in _GAUNTLET_POOLS.values():
            card_list.extend(p_list)

    scored_threats: list[tuple[float, str]] = []
    seen_acts: set[str] = set()

    for card in set(card_list):
        act = f"Cast {card}"
        slot = action_index(act)
        logit = float(policy_opponent_logits[slot])
        if act not in seen_acts:
            seen_acts.add(act)
            scored_threats.append((logit, act))

    scored_threats.sort(key=lambda t: t[0], reverse=True)
    return [act for _, act in scored_threats[:top_k]]

