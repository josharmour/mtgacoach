"""Deterministic combat-decision solver.

Enumerates legal block assignments (and attacker subsets) and picks the
best outcome under a simple scoring model. Combat is one of the few MTG
decisions that's fully deterministic once declarations are made, so a
targeted solver is more reliable than the LLM for the mechanical part.

Used two ways:
  1. `optimal_blocks()` / `optimal_attacks()` produce a recommended
     declaration that can be injected into the coach prompt as grounded
     "Computed optimal ..." advice.
  2. When autopilot is driving, the planner can compare the LLM's chosen
     declaration against the solver's pick and override if the LLM's
     choice is materially worse.

Scoring is a weighted sum over (life_preserved, material_gained,
material_lost) with life dominant when it's low.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Any, Iterable, Optional


# --- Helpers -----------------------------------------------------------


def _text(c: dict) -> str:
    return (c.get("oracle_text") or "").lower()


def _has(card: dict, keyword: str) -> bool:
    """Check for a keyword ability on a creature, case-insensitive."""
    return keyword.lower() in _text(card)


def _pt(card: dict) -> tuple[int, int]:
    return int(card.get("power") or 0), int(card.get("toughness") or 0)


def _material(card: dict) -> int:
    """Crude material value: P+T, floored at 1 so tokens aren't zero."""
    p, t = _pt(card)
    return max(1, p + t)


def _can_block(attacker: dict, blocker: dict) -> bool:
    """Return True iff the blocker can legally block the attacker.

    Only checks keyword-level legality (flying/reach + menace partial).
    Defender-imposed restrictions (e.g. skulk, can-only-be-blocked-by)
    are not modeled — callers should filter those externally via the
    GRE-provided `attackerInstanceIds` list.
    """
    if _has(attacker, "flying"):
        if not (_has(blocker, "flying") or _has(blocker, "reach")):
            return False
    # Menace: attacker can't be blocked except by two or more creatures.
    # We model this as a per-attacker constraint enforced later (>=2 blockers).
    return True


# --- Combat resolution for a single attacker + assigned blockers -------


@dataclass
class CombatOutcome:
    damage_through: int = 0
    attacker_died: bool = False
    blockers_died: list[dict] = field(default_factory=list)


def _resolve_attacker(attacker: dict, assigned: list[dict]) -> CombatOutcome:
    """Resolve one attacker against its assigned blockers.

    Simplified damage model:
      - If no blockers: all power goes through.
      - If blockers: damage is assigned to blockers in the order given
        until each has received lethal, then remaining spills to player
        iff attacker has trample.
      - First strike: attacker strikes first. Blockers killed in the FS
        step don't deal damage back.
      - Deathtouch: any damage to a blocker is lethal; when choosing how
        to spread damage with deathtouch+trample, 1 point per blocker is
        "lethal" for spillover math.
    """
    atk_p, atk_t = _pt(attacker)
    atk_dth = _has(attacker, "deathtouch")
    atk_trample = _has(attacker, "trample")
    atk_fs = _has(attacker, "first strike") or _has(attacker, "double strike")
    atk_ds = _has(attacker, "double strike")
    atk_lifelink = _has(attacker, "lifelink")  # noqa: F841 (for future)
    atk_indestructible = _has(attacker, "indestructible")

    out = CombatOutcome()

    if not assigned:
        out.damage_through = atk_p
        return out

    # Pre-fight state — attacker takes damage from blockers unless
    # one-sided first strike kills attacker before they fight.
    attacker_alive = True

    # --- First-strike step (attacker only, if applicable) ---
    if atk_fs and atk_p > 0:
        dmg_left = atk_p
        for blk in list(assigned):
            blk_p, blk_t = _pt(blk)
            blk_indestructible = _has(blk, "indestructible")
            lethal = 1 if atk_dth else blk_t
            take = min(dmg_left, lethal)
            if take >= lethal and not blk_indestructible:
                out.blockers_died.append(blk)
            dmg_left -= take
            if dmg_left <= 0:
                break
        if atk_trample:
            out.damage_through += max(0, dmg_left)

    # --- Regular damage step ---
    # Blockers that died in FS can't deal damage back.
    alive_after_fs = [b for b in assigned if b not in out.blockers_died]
    blocker_has_fs = any(
        _has(b, "first strike") or _has(b, "double strike") for b in alive_after_fs
    )

    # If blockers with first strike would kill a non-FS attacker before
    # it strikes, attacker dies and never deals its regular damage.
    if blocker_has_fs and not atk_fs:
        fs_blockers = [
            b for b in alive_after_fs
            if _has(b, "first strike") or _has(b, "double strike")
        ]
        fs_power = sum(_pt(b)[0] for b in fs_blockers)
        fs_dth = any(_has(b, "deathtouch") for b in fs_blockers)
        if ((fs_dth and fs_power > 0) or fs_power >= atk_t) and not atk_indestructible:
            out.attacker_died = True
            attacker_alive = False

    # Attacker deals damage (if still alive and this is the regular or DS step).
    if attacker_alive and atk_p > 0 and (atk_ds or not atk_fs):
        dmg_left = atk_p
        for blk in list(alive_after_fs):
            if blk in out.blockers_died:
                continue
            blk_p, blk_t = _pt(blk)
            blk_indestructible = _has(blk, "indestructible")
            lethal = 1 if atk_dth else blk_t
            take = min(dmg_left, lethal)
            if take >= lethal and not blk_indestructible:
                out.blockers_died.append(blk)
            dmg_left -= take
            if dmg_left <= 0:
                break
        if atk_trample:
            out.damage_through += max(0, dmg_left)

    # Blockers deal damage back to attacker. In normal combat this is
    # simultaneous with the attacker's damage — even blockers that die
    # THIS step still deal their damage. Only blockers that died in the
    # FS step are excluded.
    if attacker_alive and not atk_indestructible and not out.attacker_died:
        # Exclude FS-step kills but INCLUDE regular-step kills (simultaneous).
        non_fs_dead = [b for b in out.blockers_died if b in alive_after_fs]
        striking_back = alive_after_fs  # all blockers alive after FS
        ret_power = sum(_pt(b)[0] for b in striking_back)
        ret_dth = any(_has(b, "deathtouch") for b in striking_back)
        if (ret_dth and ret_power > 0) or ret_power >= atk_t:
            out.attacker_died = True

    return out


# --- Declare blockers solver -------------------------------------------


@dataclass
class BlockPlan:
    # blocker_instance_id -> attacker_instance_id (0 = do not block)
    assignments: dict[int, int] = field(default_factory=dict)
    damage_through: int = 0
    attackers_killed_material: int = 0
    blockers_lost_material: int = 0
    explanation: str = ""
    score: float = 0.0


def _can_block_this_attacker(
    attacker: dict, blocker: dict, allowed_ids: Optional[set[int]]
) -> bool:
    if allowed_ids is not None and int(attacker.get("instance_id") or 0) not in allowed_ids:
        return False
    return _can_block(attacker, blocker)


def optimal_blocks(
    attackers: list[dict],
    blockers: list[dict],
    your_life: int = 20,
    *,
    blocker_allowed_attackers: Optional[dict[int, set[int]]] = None,
) -> Optional[BlockPlan]:
    """Enumerate legal block assignments and pick the best.

    `blocker_allowed_attackers` maps a blocker's instance_id to the set
    of attacker instance_ids it is allowed to block (from the GRE
    `DeclareBlockersReq.blockers[].attackerInstanceIds` list). When None,
    any attacker the blocker can legally block by keyword is allowed.

    Returns None if there are no attackers.
    """
    if not attackers:
        return None

    # Cap search space — 4 attackers x 6 blockers = 5^6 = 15625 which is
    # cheap; but 8 attackers × 8 blockers = 9^8 ≈ 43M which is not. Fall
    # back to a greedy heuristic when combinatorial search would be slow.
    n_attackers = len(attackers)
    n_blockers = len(blockers)
    n_options = (n_attackers + 1) ** n_blockers
    if n_options > 50_000:
        return _greedy_block_plan(
            attackers, blockers, your_life,
            blocker_allowed_attackers=blocker_allowed_attackers,
        )

    # Pre-compute per-blocker option list: indices into `attackers` plus
    # "None" for no-block.
    blocker_options: list[list[Optional[int]]] = []
    for blk in blockers:
        blk_iid = int(blk.get("instance_id") or 0)
        allowed = None
        if blocker_allowed_attackers is not None:
            allowed = blocker_allowed_attackers.get(blk_iid)
        opts: list[Optional[int]] = [None]  # do not block
        for i, atk in enumerate(attackers):
            if _can_block_this_attacker(atk, blk, allowed):
                opts.append(i)
        blocker_options.append(opts)

    incoming_damage = sum(_pt(a)[0] for a in attackers)

    best: Optional[BlockPlan] = None
    for choice in product(*blocker_options):
        # choice[b] is either None or an attacker index
        assigned_to_atk: dict[int, list[dict]] = {}
        for b_idx, atk_idx in enumerate(choice):
            if atk_idx is None:
                continue
            assigned_to_atk.setdefault(atk_idx, []).append(blockers[b_idx])

        # Menace check — attackers with menace need >=2 blockers or none.
        menace_violation = False
        for atk_idx, blks in assigned_to_atk.items():
            if _has(attackers[atk_idx], "menace") and len(blks) == 1:
                menace_violation = True
                break
        if menace_violation:
            continue

        total_damage = 0
        atk_killed_material = 0
        blocker_lost_material = 0
        killed_attackers: list[dict] = []
        lost_blockers: list[dict] = []

        for a_idx, atk in enumerate(attackers):
            assigned = assigned_to_atk.get(a_idx, [])
            outcome = _resolve_attacker(atk, assigned)
            if not assigned:
                # No blocker: all power goes through.
                total_damage += outcome.damage_through
            else:
                total_damage += outcome.damage_through
                if outcome.attacker_died:
                    atk_killed_material += _material(atk)
                    killed_attackers.append(atk)
                for dead in outcome.blockers_died:
                    blocker_lost_material += _material(dead)
                    lost_blockers.append(dead)

        plan = BlockPlan(
            assignments={
                int(blockers[b_idx].get("instance_id") or 0):
                int(attackers[atk_idx].get("instance_id") or 0)
                for b_idx, atk_idx in enumerate(choice)
                if atk_idx is not None
            },
            damage_through=total_damage,
            attackers_killed_material=atk_killed_material,
            blockers_lost_material=blocker_lost_material,
        )
        plan.score = _score_block_plan(plan, your_life)
        plan.explanation = _explain_block_plan(
            plan, attackers, blockers, choice, killed_attackers, lost_blockers
        )
        if best is None or plan.score > best.score:
            best = plan

    return best


def _score_block_plan(plan: BlockPlan, your_life: int) -> float:
    """Score a block assignment.

    Life dominates when we're at risk of dying; material dominates when
    we're safe. We don't weight life linearly — taking damage at 20 life
    is nearly free, but taking damage at 4 life is existential.
    """
    # If this block plan kills you, it's worst possible.
    life_after = your_life - plan.damage_through
    if life_after <= 0:
        life_score = -1000.0
    else:
        # Inverse-life penalty: 1 damage at 20 life costs 0.25; 1 damage at
        # 4 life costs 2.5.
        life_score = -plan.damage_through * (5.0 / max(1, life_after))

    material_score = plan.attackers_killed_material - plan.blockers_lost_material
    return life_score + material_score


def _explain_block_plan(
    plan: BlockPlan,
    attackers: list[dict],
    blockers: list[dict],
    choice: tuple,
    killed_attackers: list[dict],
    lost_blockers: list[dict],
) -> str:
    parts: list[str] = []
    for b_idx, atk_idx in enumerate(choice):
        if atk_idx is None:
            continue
        blk = blockers[b_idx]
        atk = attackers[atk_idx]
        parts.append(f"{blk.get('name', '?')} blocks {atk.get('name', '?')}")
    block_desc = ", ".join(parts) if parts else "no blocks"
    kills = [a.get("name", "?") for a in killed_attackers]
    losses = [b.get("name", "?") for b in lost_blockers]
    tail = []
    if plan.damage_through > 0:
        tail.append(f"{plan.damage_through} dmg through")
    if kills:
        tail.append(f"kills {', '.join(kills)}")
    if losses:
        tail.append(f"loses {', '.join(losses)}")
    return f"{block_desc}" + (f" ({'; '.join(tail)})" if tail else "")


def _greedy_block_plan(
    attackers: list[dict],
    blockers: list[dict],
    your_life: int,
    *,
    blocker_allowed_attackers: Optional[dict[int, set[int]]],
) -> BlockPlan:
    """Fallback for large combat — greedy chump-block biggest attackers first."""
    sorted_atk = sorted(
        enumerate(attackers), key=lambda ia: -_pt(ia[1])[0]
    )
    remaining_blockers = list(blockers)
    assignments: dict[int, int] = {}
    total_damage = 0
    atk_killed_material = 0
    blocker_lost_material = 0
    killed_attackers: list[dict] = []
    lost_blockers: list[dict] = []

    for atk_idx, atk in sorted_atk:
        atk_iid = int(atk.get("instance_id") or 0)
        # Pick a blocker that can block this attacker with minimum material.
        candidates = []
        for blk in remaining_blockers:
            blk_iid = int(blk.get("instance_id") or 0)
            allowed = (
                blocker_allowed_attackers.get(blk_iid)
                if blocker_allowed_attackers is not None
                else None
            )
            if _can_block_this_attacker(atk, blk, allowed):
                candidates.append(blk)
        if not candidates:
            total_damage += _pt(atk)[0]
            continue
        # Prefer minimum material blocker that still survives if possible.
        candidates.sort(key=lambda b: (_material(b), -_pt(b)[1]))
        chosen = candidates[0]
        remaining_blockers.remove(chosen)
        assignments[int(chosen.get("instance_id") or 0)] = atk_iid

        outcome = _resolve_attacker(atk, [chosen])
        total_damage += outcome.damage_through
        if outcome.attacker_died:
            atk_killed_material += _material(atk)
            killed_attackers.append(atk)
        for dead in outcome.blockers_died:
            blocker_lost_material += _material(dead)
            lost_blockers.append(dead)

    plan = BlockPlan(
        assignments=assignments,
        damage_through=total_damage,
        attackers_killed_material=atk_killed_material,
        blockers_lost_material=blocker_lost_material,
    )
    plan.score = _score_block_plan(plan, your_life)
    plan.explanation = "greedy: " + ", ".join(
        f"{b.get('name', '?')} -> atk#{assignments[int(b.get('instance_id') or 0)]}"
        for b in blockers
        if int(b.get("instance_id") or 0) in assignments
    )
    return plan


# --- Declare attackers solver ------------------------------------------


@dataclass
class AttackPlan:
    # ordered list of attacker instance_ids
    attacker_ids: list[int] = field(default_factory=list)
    attacker_names: list[str] = field(default_factory=list)
    damage_through: int = 0  # expected damage to opponent after worst-case blocks
    worst_case_crackback: int = 0  # damage we'd take next opponent turn
    attackers_lost_material: int = 0
    blockers_killed_material: int = 0
    explanation: str = ""
    score: float = 0.0


def optimal_attacks(
    candidate_attackers: list[dict],
    opponent_blockers: list[dict],
    opponent_life: int,
    your_life: int,
    opponent_attackers_next_turn: list[dict],
    your_remaining_blockers: list[dict],
) -> Optional[AttackPlan]:
    """Pick the attacker subset that maximizes damage while surviving crackback.

    Worst-case model: assume the opponent blocks to minimize damage
    through using `optimal_blocks` from their perspective.

    Crackback: the creatures you DON'T attack with remain as blockers for
    the opponent's next turn. Opponent will attack with what they have
    (including the candidates still on their board plus next-turn
    attackers we model), and we block optimally with what's left.
    """
    if not candidate_attackers:
        return None

    n = len(candidate_attackers)
    # 2^N subsets. For large N, cap to single-bit toggles as a cheap heuristic.
    if n > 8:
        subsets: Iterable[tuple[int, ...]] = _singleton_and_full_subsets(n)
    else:
        subsets = []
        for mask in range(1 << n):
            subsets.append(tuple(i for i in range(n) if mask & (1 << i)))
        # Include empty set explicitly (no attack).
        subsets.append(())

    best: Optional[AttackPlan] = None

    for subset in subsets:
        attacking = [candidate_attackers[i] for i in subset]
        # Opponent blocks optimally — from THEIR perspective, lower
        # "damage_through" is good; we invert by using their life.
        opp_block = optimal_blocks(attacking, opponent_blockers, opponent_life)
        if opp_block is None:
            damage_through = sum(_pt(a)[0] for a in attacking)
            atk_lost_mat = 0
            blk_killed_mat = 0
        else:
            damage_through = opp_block.damage_through
            atk_lost_mat = opp_block.blockers_lost_material  # from opp POV, "blockers" = our attackers
            blk_killed_mat = opp_block.attackers_killed_material  # their creatures we kill

        # Crackback — the creatures we didn't attack with, plus whatever
        # opponent will attack with next turn (we take this list in
        # verbatim since we can't easily predict it).
        held_back = [
            candidate_attackers[i] for i in range(n) if i not in set(subset)
        ]
        our_defenders = held_back + your_remaining_blockers
        crack_block = optimal_blocks(
            opponent_attackers_next_turn, our_defenders, your_life
        )
        crackback = crack_block.damage_through if crack_block else sum(
            _pt(a)[0] for a in opponent_attackers_next_turn
        )

        plan = AttackPlan(
            attacker_ids=[int(candidate_attackers[i].get("instance_id") or 0) for i in subset],
            attacker_names=[candidate_attackers[i].get("name", "?") for i in subset],
            damage_through=damage_through,
            worst_case_crackback=crackback,
            attackers_lost_material=atk_lost_mat,
            blockers_killed_material=blk_killed_mat,
        )
        plan.score = _score_attack_plan(plan, your_life, opponent_life)
        plan.explanation = (
            f"attack with {', '.join(plan.attacker_names) or 'nobody'}; "
            f"{damage_through} through, crackback {crackback} "
            f"(lose material {atk_lost_mat}, kill {blk_killed_mat})"
        )
        if best is None or plan.score > best.score:
            best = plan

    return best


def _singleton_and_full_subsets(n: int) -> list[tuple[int, ...]]:
    """Cheap heuristic for large attacker pools: no-attack, each solo, all-in."""
    subsets: list[tuple[int, ...]] = [()]
    for i in range(n):
        subsets.append((i,))
    subsets.append(tuple(range(n)))
    return subsets


def _score_attack_plan(plan: AttackPlan, your_life: int, opponent_life: int) -> float:
    """Attack: damage to opponent is great; dying to crackback is worst."""
    # Lethal this swing is the best possible outcome.
    if plan.damage_through >= opponent_life:
        return 10_000.0

    # Dying to crackback is the worst.
    if plan.worst_case_crackback >= your_life:
        return -1_000.0

    # Weight damage dealt by how close it gets us to lethal.
    opp_pressure = plan.damage_through * (5.0 / max(1, opponent_life - plan.damage_through))
    our_pressure = -plan.worst_case_crackback * (
        5.0 / max(1, your_life - plan.worst_case_crackback)
    )
    material = plan.blockers_killed_material - plan.attackers_lost_material
    return opp_pressure + our_pressure + material


# --- Game-state adapters -----------------------------------------------


def collect_attackers(game_state: dict[str, Any]) -> list[dict]:
    """Return creatures currently attacking (is_attacking=True).

    If none are flagged attacking (e.g. during DeclareBlockers before a
    snapshot refresh), fall back to using the raw_blockers structure's
    attackerInstanceIds via `collect_attackers_from_raw_blockers`.
    """
    battlefield = game_state.get("battlefield", [])
    return [c for c in battlefield if c.get("is_attacking")]


def collect_attackers_from_raw_blockers(
    game_state: dict[str, Any], raw_blockers: list[dict]
) -> list[dict]:
    """Derive attacker objects from the GRE declareBlockersReq payload.

    Each entry in raw_blockers exposes `attackerInstanceIds` — the union
    of those IDs is the full attacker set.
    """
    battlefield = game_state.get("battlefield", [])
    attacker_ids: set[int] = set()
    for blk in raw_blockers:
        for aid in blk.get("attackerInstanceIds") or []:
            try:
                attacker_ids.add(int(aid))
            except (TypeError, ValueError):
                continue
    return [c for c in battlefield if int(c.get("instance_id") or 0) in attacker_ids]


def collect_blockers_from_decision(
    game_state: dict[str, Any], decision_context: dict[str, Any]
) -> list[dict]:
    blocker_ids = set(int(x) for x in (decision_context.get("legal_blocker_ids") or []))
    battlefield = game_state.get("battlefield", [])
    return [c for c in battlefield if int(c.get("instance_id") or 0) in blocker_ids]


def blocker_allowed_attackers_map(
    raw_blockers: list[dict],
) -> dict[int, set[int]]:
    """Map blocker_instance_id -> allowed attacker_instance_id set."""
    out: dict[int, set[int]] = {}
    for blk in raw_blockers or []:
        bid = blk.get("blockerInstanceId") or blk.get("instanceId")
        try:
            bid = int(bid)
        except (TypeError, ValueError):
            continue
        ids = set()
        for aid in blk.get("attackerInstanceIds") or []:
            try:
                ids.add(int(aid))
            except (TypeError, ValueError):
                continue
        out[bid] = ids
    return out
