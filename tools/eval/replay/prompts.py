"""Build coach prompts from a replay snapshot + a pending request.

V1 covers `ActionsAvailableReq` — the most common decision type — using
a numbered-action scheme so coach responses parse unambiguously.

Format philosophy: keep enough state to make the call (life, hand, board,
mana, phase, turn) without exploding the token budget. Resolve grpIds to
card names via the project's existing card_db.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from arenamcp.card_db import get_card_database  # noqa: E402

from .decisions import GroundTruth  # noqa: E402
from .reader import ReplayMessage  # noqa: E402
from .state import GameStateSnapshot  # noqa: E402


SYSTEM_PROMPT = (
    "You are a Magic: The Gathering Arena coach. The user will describe "
    "a game state and a list of legal numbered actions. Your job is to "
    "pick the best one.\n\n"
    "Reply on the FIRST LINE with exactly: ACTION: <N> "
    "(where <N> is one of the action numbers). Then on subsequent lines "
    "give one short sentence of reasoning. Do not pick a number that's "
    "not in the list."
)


@dataclass
class ActionChoice:
    """One legal action enumerated for the coach's choice list."""

    number: int             # 1-based
    action_type: str        # ActionType_Cast / Pass / Activate / PlayLand / ...
    grp_id: Optional[int]
    instance_id: Optional[int]
    label: str              # human-readable: "Cast Lightning Strike"
    raw: dict               # original action dict from the request


_CARD_DB = None


def _cdb():
    global _CARD_DB
    if _CARD_DB is None:
        _CARD_DB = get_card_database()
    return _CARD_DB


def _name_for_grpid(grp_id: Optional[int]) -> str:
    if grp_id is None:
        return "(no card)"
    info = _cdb().get_card_by_arena_id(int(grp_id))
    if info is None:
        return f"#{grp_id}"
    return getattr(info, "name", None) or f"#{grp_id}"


def _format_mana_cost(cost: list[dict]) -> str:
    """Render a manaCost list as MTG-style braces e.g. '{1}{R}'."""
    if not cost:
        return ""
    parts: list[str] = []
    color_short = {
        "ManaColor_White": "W", "ManaColor_Blue": "U", "ManaColor_Black": "B",
        "ManaColor_Red": "R", "ManaColor_Green": "G",
        "ManaColor_Generic": None, "ManaColor_Colorless": "C",
    }
    for entry in cost:
        n = int(entry.get("count") or 0)
        colors = entry.get("color") or []
        if not colors or "ManaColor_Generic" in colors:
            if n > 0:
                parts.append("{" + str(n) + "}")
            continue
        symbol = next((color_short.get(c) for c in colors if color_short.get(c)), None)
        if symbol:
            parts.extend(["{" + symbol + "}"] * n)
    return "".join(parts) or "(free)"


# Action types we filter out of the choice list — these are mechanical
# moves a coach shouldn't be reasoning about explicitly, and including
# them blows the prompt up to 30+ choices on Main1.
_FILTERED_ACTION_TYPES = {
    "ActionType_Activate_Mana",   # tapping a single land for a single color
    "ActionType_FloatMana",       # auto-float at phase end
}


def enumerate_actions(request: ReplayMessage) -> list[ActionChoice]:
    """Pull the actions[] from an ActionsAvailableReq and enumerate."""
    aa = (request.payload.get("actionsAvailableReq") or {}).get("actions") or []
    out: list[ActionChoice] = []
    saw_pass = False
    for a in aa:
        atype = (a.get("actionType") or "?")
        if atype in _FILTERED_ACTION_TYPES:
            continue
        gid = a.get("grpId")
        iid = a.get("instanceId")
        cost = a.get("manaCost") or []
        cost_s = _format_mana_cost(cost)
        if atype == "ActionType_Pass":
            saw_pass = True
            label = "Pass priority"
        elif atype == "ActionType_Cast":
            name = _name_for_grpid(gid)
            label = f"Cast {name}" + (f" (cost {cost_s})" if cost_s else "")
        elif atype == "ActionType_Play":
            name = _name_for_grpid(gid)
            label = f"Play {name}"
        elif atype == "ActionType_Activate" or atype.startswith("ActionType_Activate"):
            name = _name_for_grpid(gid)
            label = f"Activate {name}" + (f" (cost {cost_s})" if cost_s else "")
        elif atype == "ActionType_Activate_Mana":
            name = _name_for_grpid(gid)
            label = f"Tap {name} for mana"
        elif atype == "ActionType_PlayMDFC":
            name = _name_for_grpid(gid)
            label = f"Play {name} (MDFC back)"
        else:
            label = atype.replace("ActionType_", "")
            if gid is not None:
                label += f" {_name_for_grpid(gid)}"
        out.append(ActionChoice(
            number=len(out) + 1, action_type=atype,
            grp_id=int(gid) if gid is not None else None,
            instance_id=int(iid) if iid is not None else None,
            label=label, raw=a,
        ))
    if not saw_pass:
        out.append(ActionChoice(
            number=len(out) + 1, action_type="ActionType_Pass",
            grp_id=None, instance_id=None,
            label="Pass priority (auto)", raw={"actionType": "ActionType_Pass"},
        ))
    return out


def _pt_value(field) -> Optional[int]:
    """The proto stores power/toughness as a wrapper object {value: n}."""
    if field is None:
        return None
    if isinstance(field, dict):
        v = field.get("value")
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    try:
        return int(field)
    except (TypeError, ValueError):
        return None


def _format_card_list(cards: list[dict], max_items: int = 30) -> str:
    if not cards:
        return "(none)"
    names = []
    for c in cards[:max_items]:
        n = _name_for_grpid(c.get("grpId"))
        p = _pt_value(c.get("power"))
        t = _pt_value(c.get("toughness"))
        if p is not None and t is not None:
            n += f" ({p}/{t})"
        names.append(n)
    suffix = f" +{len(cards) - max_items} more" if len(cards) > max_items else ""
    return ", ".join(names) + suffix


def build_actions_available_prompt(
    snap: GameStateSnapshot,
    request: ReplayMessage,
    actions: list[ActionChoice],
) -> str:
    """Produce the coach user prompt for an ActionsAvailable decision."""
    seat = snap.local_seat_id
    opp_seat = 1 if seat == 2 else 2

    you_life = snap.life(seat)
    opp_life = snap.life(opp_seat)
    you_hand = snap.hand(seat)
    you_bf = snap.battlefield(seat)
    opp_bf = snap.battlefield(opp_seat)

    phase = snap.phase.replace("Phase_", "")
    on_priority = "you" if snap.priority_player == seat else "opp"
    is_your_turn = snap.active_player == seat

    lines: list[str] = []
    whose = "Your" if is_your_turn else "Opponent's"
    lines.append(f"It is turn {snap.turn_number}, phase {phase}. "
                 f"{whose} turn. Priority: {on_priority}.")
    lines.append("")
    lines.append(f"Life — you {you_life}, opp {opp_life}")
    lines.append(f"Your hand ({len(you_hand)}): {_format_card_list(you_hand)}")
    lines.append(f"Your battlefield ({len(you_bf)}): {_format_card_list(you_bf)}")
    lines.append(f"Opp battlefield ({len(opp_bf)}): {_format_card_list(opp_bf)}")
    lines.append("")
    lines.append("Legal actions (pick ONE by number):")
    for a in actions:
        lines.append(f"  {a.number}. {a.label}")
    lines.append("")
    lines.append("Reply with `ACTION: <N>` on the first line, then a brief reason.")
    return "\n".join(lines)


def _card_to_json_dict(c: dict) -> dict:
    """Render a battlefield/hand card as a small dict with resolved name."""
    out: dict = {"grpId": c.get("grpId"), "name": _name_for_grpid(c.get("grpId"))}
    p = _pt_value(c.get("power"))
    t = _pt_value(c.get("toughness"))
    if p is not None: out["power"] = p
    if t is not None: out["toughness"] = t
    return out


def build_actions_available_prompt_raw_json(
    snap: GameStateSnapshot,
    request: ReplayMessage,
    actions: list[ActionChoice],
) -> str:
    """Same fields as build_actions_available_prompt, rendered as JSON.

    Ablation control: tests whether the structured-English scaffolding
    is load-bearing for small models or if they parse raw JSON state
    directly. Card names are still resolved (free given the card_db
    cache); no GRE-internal fields are added — this is pure formatting.
    """
    import json as _json
    seat = snap.local_seat_id
    opp_seat = 1 if seat == 2 else 2

    state = {
        "turn": snap.turn_number,
        "phase": snap.phase.replace("Phase_", ""),
        "active_player": "you" if snap.active_player == seat else "opp",
        "priority": "you" if snap.priority_player == seat else "opp",
        "life": {"you": snap.life(seat), "opp": snap.life(opp_seat)},
        "your_hand": [_card_to_json_dict(c) for c in snap.hand(seat)],
        "your_battlefield": [_card_to_json_dict(c) for c in snap.battlefield(seat)],
        "opp_battlefield": [_card_to_json_dict(c) for c in snap.battlefield(opp_seat)],
        "legal_actions": [
            {"number": a.number, "label": a.label,
             "action_type": a.action_type, "grp_id": a.grp_id}
            for a in actions
        ],
    }
    body = _json.dumps(state, separators=(",", ":"))
    return (
        "Game state (JSON):\n"
        f"{body}\n\n"
        "Pick ONE legal_action by number. Reply with `ACTION: <N>` on the "
        "first line, then a brief reason."
    )


# ---------------------------------------------------------------------------
# DeclareAttackers prompt
# ---------------------------------------------------------------------------


@dataclass
class CreatureChoice:
    """A creature listed for attacker/blocker selection."""

    number: int
    instance_id: int
    grp_id: Optional[int]
    name: str
    power: Optional[int]
    toughness: Optional[int]


def _creature_label(c: CreatureChoice) -> str:
    pt = ""
    if c.power is not None and c.toughness is not None:
        pt = f" ({c.power}/{c.toughness})"
    return f"{c.name}{pt}"


def _creatures_from_ids(snap, instance_ids, fallback_objs=None):
    """Build CreatureChoice objects for a set of instance IDs."""
    out = []
    for i, iid in enumerate(instance_ids):
        obj = snap.game_objects.get(int(iid))
        if obj is None and fallback_objs:
            obj = next((o for o in fallback_objs if o.get("instanceId") == iid), None)
        gid = obj.get("grpId") if obj else None
        name = _name_for_grpid(gid)
        power = _pt_value(obj.get("power")) if obj else None
        toughness = _pt_value(obj.get("toughness")) if obj else None
        out.append(CreatureChoice(
            number=i + 1, instance_id=int(iid),
            grp_id=int(gid) if gid is not None else None,
            name=name, power=power, toughness=toughness,
        ))
    return out


DA_SYSTEM_PROMPT = (
    "You are a Magic: The Gathering Arena coach. Combat phase — pick which "
    "of your creatures should attack. Reply on the FIRST LINE with exactly: "
    "ATTACK: <comma-separated numbers>, or ATTACK: NONE. Then a brief reason."
)


def build_declare_attackers_prompt(snap, request, qualified) -> str:
    """``qualified`` is the list of CreatureChoice for legal attackers."""
    seat = snap.local_seat_id
    opp_seat = 1 if seat == 2 else 2
    opp_bf = snap.battlefield(opp_seat)
    opp_creatures_strs = [_format_card_list([c]) for c in opp_bf if "creature" in str(c.get("cardTypes", "")).lower()]
    if not opp_creatures_strs:
        opp_creatures_strs = [_format_card_list([c]) for c in opp_bf]

    lines = [
        f"Combat. Turn {snap.turn_number}. Your turn.",
        f"Life — you {snap.life(seat)}, opp {snap.life(opp_seat)}",
        f"",
        f"Your creatures that can attack:",
    ]
    for c in qualified:
        lines.append(f"  {c.number}. {_creature_label(c)}")
    lines.append("")
    lines.append("Opponent's battlefield:")
    if opp_bf:
        for o in opp_bf[:30]:
            n = _name_for_grpid(o.get("grpId"))
            p = _pt_value(o.get("power"))
            t = _pt_value(o.get("toughness"))
            label = n + (f" ({p}/{t})" if p is not None and t is not None else "")
            lines.append(f"  - {label}")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("Reply with: `ATTACK: <comma-sep numbers>` (or `ATTACK: NONE`).")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# DeclareBlockers prompt
# ---------------------------------------------------------------------------


DB_SYSTEM_PROMPT = (
    "You are a Magic: The Gathering Arena coach. Combat blockers — pick "
    "which of your creatures block which attackers. Reply on the FIRST LINE: "
    "`BLOCKS: <blocker_n>->A, <blocker_n>->B, ...` (e.g. '1->A, 2->A' for "
    "double-blocking attacker A), or `BLOCKS: NONE`. Then a brief reason."
)


def build_declare_blockers_prompt(snap, request,
                                  attackers: list[CreatureChoice],
                                  blockers: list[CreatureChoice]) -> str:
    seat = snap.local_seat_id
    opp_seat = 1 if seat == 2 else 2
    lines = [
        f"Combat (declare blockers). Turn {snap.turn_number}.",
        f"Life — you {snap.life(seat)}, opp {snap.life(opp_seat)}",
        f"",
        "Opponent's attackers (letters):",
    ]
    for i, a in enumerate(attackers):
        letter = chr(ord("A") + i) if i < 26 else f"X{i}"
        lines.append(f"  {letter}. {_creature_label(a)} -> dealing {a.power or 0} damage")
    lines.append("")
    lines.append("Your potential blockers (numbers):")
    for c in blockers:
        lines.append(f"  {c.number}. {_creature_label(c)}")
    lines.append("")
    lines.append("Reply with: `BLOCKS: <n>-><letter>, ...` (or `BLOCKS: NONE`).")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Mulligan prompt
# ---------------------------------------------------------------------------


MULL_SYSTEM_PROMPT = (
    "Binary classifier for MTG Arena opening hands: KEEP or MULL.\n"
    "\n"
    "Default to KEEP. Pro players keep ~85-95% of opening hands. Only MULL "
    "if the hand is clearly unkeepable:\n"
    "  - 0 lands or 1 land (mana flood-out risk)\n"
    "  - 5+ lands out of 7 (mana screw risk)\n"
    "  - Zero plays before turn 4 (slow-rolled out)\n"
    "Otherwise KEEP, even if the hand is mediocre — a 6-card mulligan is "
    "usually weaker.\n"
    "\n"
    "Reply on the FIRST LINE with exactly one word: `KEEP` or `MULL`."
)


def _classify_hand(hand_cards: list[dict]) -> tuple[int, int, int]:
    """Return (lands, spells, low_drops) where low_drops = #cards with cmc<=3.

    Land detection uses the card DB type_line; failures count as spells
    (conservative — a missing type_line is more often a non-land card).
    """
    lands = 0
    spells = 0
    low_drops = 0
    cdb = _cdb()
    for c in hand_cards:
        gid = c.get("grpId")
        info = cdb.get_card_by_arena_id(int(gid)) if gid is not None else None
        type_line = (getattr(info, "type_line", None) or "").lower() if info else ""
        cmc = float(getattr(info, "cmc", 0) or 0) if info else 0.0
        if "land" in type_line:
            lands += 1
        else:
            spells += 1
            if cmc and cmc <= 3:
                low_drops += 1
    return lands, spells, low_drops


def build_mulligan_prompt(snap, request, hand_cards: list[dict],
                          mulligan_count: int) -> str:
    """Stripped-down mulligan prompt for small models.

    Designed for E2B-class models that get lost in heavy context. Surfaces
    the three signals that drive the decision (lands count, spells count,
    early plays) so the model doesn't have to derive them from card names.
    """
    nums_remaining = 7 - mulligan_count
    lands, spells, low_drops = _classify_hand(hand_cards)
    lines = [
        f"Mulligan #{mulligan_count}. {nums_remaining}-card hand. London format "
        f"(mulligan = bottom {mulligan_count + 1} after seeing a fresh {nums_remaining}).",
        "",
        f"Lands: {lands}/{nums_remaining}.  Spells: {spells} (with {low_drops} that cost 3 or less).",
        "",
        "Hand:",
    ]
    for i, c in enumerate(hand_cards):
        n = _name_for_grpid(c.get("grpId"))
        lines.append(f"  {i + 1}. {n}")
    lines.append("")
    lines.append("KEEP or MULL?")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Coach response parsing
# ---------------------------------------------------------------------------

import re

_ACTION_LINE_RE = re.compile(r"\bACTION\s*:\s*(\d+)\b", re.IGNORECASE)
_ATTACK_LINE_RE = re.compile(r"\bATTACK\s*:\s*([^\n\r]+)", re.IGNORECASE)
_BLOCKS_LINE_RE = re.compile(r"\bBLOCKS\s*:\s*([^\n\r]+)", re.IGNORECASE)
_MULL_LINE_RE = re.compile(r"\b(KEEP|MULL(?:IGAN)?)\b", re.IGNORECASE)
_NUMBERS_RE = re.compile(r"\d+")
_PAIR_RE = re.compile(r"(\d+)\s*->\s*([A-Za-z])")


def parse_coach_choice(response: str, actions: list[ActionChoice]) -> Optional[ActionChoice]:
    """Extract the numbered choice from a coach response.

    Returns the matching ActionChoice or None if unparseable / out of
    range. Parses the first ACTION: line found anywhere in the response
    (some models put preamble before).
    """
    if not response:
        return None
    m = _ACTION_LINE_RE.search(response)
    if not m:
        # Permissive fallback: a bare integer on the first line
        first = response.strip().splitlines()[0] if response.strip() else ""
        m2 = re.match(r"^\s*#?(\d+)\b", first)
        if not m2:
            return None
        n = int(m2.group(1))
    else:
        n = int(m.group(1))
    for a in actions:
        if a.number == n:
            return a
    return None


def parse_attack_set(response: str, qualified: list[CreatureChoice]) -> Optional[set[int]]:
    """Parse `ATTACK: 1, 3` -> set of instance IDs of chosen attackers.

    Returns an empty set for `ATTACK: NONE`. Returns None if unparseable.
    """
    if not response:
        return None
    m = _ATTACK_LINE_RE.search(response)
    if not m:
        return None
    body = m.group(1).strip()
    if re.match(r"^\s*NONE\s*$", body, re.IGNORECASE):
        return set()
    by_num = {c.number: c.instance_id for c in qualified}
    chosen = set()
    for n in _NUMBERS_RE.findall(body):
        ni = int(n)
        if ni in by_num:
            chosen.add(by_num[ni])
    return chosen


def parse_block_assignment(
    response: str,
    blockers: list[CreatureChoice],
    attackers: list[CreatureChoice],
) -> Optional[dict[int, int]]:
    """Parse `BLOCKS: 1->A, 2->A` -> {blocker_iid: attacker_iid}.

    Returns empty dict for `BLOCKS: NONE`, None if unparseable.
    """
    if not response:
        return None
    m = _BLOCKS_LINE_RE.search(response)
    if not m:
        return None
    body = m.group(1).strip()
    if re.match(r"^\s*NONE\s*$", body, re.IGNORECASE):
        return {}
    by_num = {c.number: c.instance_id for c in blockers}
    by_letter = {chr(ord("A") + i): a.instance_id for i, a in enumerate(attackers)}
    out: dict[int, int] = {}
    for nb, la in _PAIR_RE.findall(body):
        ni, letter = int(nb), la.upper()
        if ni in by_num and letter in by_letter:
            out[by_num[ni]] = by_letter[letter]
    return out


def parse_mulligan_choice(response: str) -> Optional[bool]:
    """Returns True for keep, False for mull, None if unparseable."""
    if not response:
        return None
    m = _MULL_LINE_RE.search(response)
    if not m:
        return None
    word = m.group(1).upper()
    if word.startswith("KEEP"):
        return True
    if word.startswith("MULL"):
        return False
    return None


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    return (len(a & b) / len(union)) if union else 1.0


def matches_ground_truth(choice: Optional[ActionChoice], gt: Optional[GroundTruth]) -> bool:
    """Did the coach pick the same action category as the player did?

    Two-tier match:
      1. action_type matches (Cast/Pass/Activate/Play/...) AND
      2. grp_id matches if the action is card-specific (Cast/Play); for
         non-card actions (Pass) only the action_type needs to match.

    For multi-action responses (rare; player submits multiple in one
    response), match the FIRST action — the coach is being asked
    sequentially in the replay one decision at a time.
    """
    if choice is None or gt is None:
        return False
    if not gt.action_type:
        return False
    coach_type = choice.action_type
    truth_type = gt.action_type
    # Normalize Activate variants
    if coach_type.startswith("ActionType_Activate") and truth_type.startswith("ActionType_Activate"):
        coach_type = truth_type = "ActionType_Activate"
    if coach_type != truth_type:
        return False
    # Card-specific actions need the same grpId (not just same type).
    card_actions = {"ActionType_Cast", "ActionType_Play", "ActionType_PlayMDFC"}
    if coach_type in card_actions:
        if choice.grp_id is None or not gt.grp_ids:
            return False
        return choice.grp_id == gt.grp_ids[0]
    return True


# ---------------------------------------------------------------------------
# High-signal decision filter
# ---------------------------------------------------------------------------


def is_high_signal_actions_available(snap, request, actions: list[ActionChoice]) -> bool:
    """Skip mechanical AA decisions; keep the strategic ones.

    "Strategic" here means: it's the player's own Main Phase 1 (or Main 2),
    they have priority, and at least one of the legal actions is a Cast or
    Play (i.e., real card-selection — not just instant-speed responses or
    end-of-turn passes). This is where 'what to play' signal lives.
    """
    if snap.priority_player != snap.local_seat_id:
        return False
    if snap.active_player != snap.local_seat_id:
        return False
    if snap.phase not in ("Phase_Main1", "Phase_Main2"):
        return False
    has_card_action = any(
        a.action_type in {"ActionType_Cast", "ActionType_Play", "ActionType_PlayMDFC"}
        for a in actions
    )
    return has_card_action


def main():
    """CLI: print the coach prompt for one decision so you can inspect it."""
    import argparse
    from .reader import parse_replay_path
    from .decisions import extract_decisions
    from .state import snapshot_at_decision

    p = argparse.ArgumentParser()
    p.add_argument("path", type=Path)
    p.add_argument("--decision", type=int, default=3,
                   help="Which decision index to render (default 3, an early Cast)")
    p.add_argument("--seat", type=int, default=2)
    args = p.parse_args()

    meta, messages = parse_replay_path(args.path)
    decisions = extract_decisions(messages)
    aa = [d for d in decisions if d.ground_truth and d.ground_truth.kind == "ActionsAvailable"]
    if args.decision >= len(aa):
        print(f"only {len(aa)} ActionsAvailable decisions in this replay")
        return
    d = aa[args.decision]
    snap = snapshot_at_decision(messages, d.request, local_seat_id=args.seat)
    actions = enumerate_actions(d.request)
    prompt = build_actions_available_prompt(snap, d.request, actions)
    print("=== SYSTEM ===")
    print(SYSTEM_PROMPT)
    print()
    print("=== USER ===")
    print(prompt)
    print()
    print("=== GROUND TRUTH ===")
    print(d.ground_truth.summary)


if __name__ == "__main__":
    main()
