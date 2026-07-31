#!/usr/bin/env python3
"""Forge → mtgacoach prompt-fidelity measurement (WP-2.1 Days 3-4).

GOAL: prove — or precisely refute — that Forge game states captured by the
forge-shim (``PlayerControllerExternal`` → ``forge_adapter.py`` →
``decisions.jsonl``) can be rendered into the EXACT prompt shape the
play-decision gate measures on, and quantify what is missing.

How fidelity is guaranteed
--------------------------
The prompt is rendered by **calling the production path by import**, never by
re-implementing it (the failure mode §8b/§15 documents):

  * ``system`` is the imported ``arenamcp.action_planner.AUTOPILOT_SYSTEM_PROMPT``.
  * ``user`` is produced by ``tools.training.gate_play_decisions.build_user_message``,
    which calls ``ActionPlanner._build_action_prompt`` →
    ``CoachEngine._format_game_context(for_planner=True)`` — the same call the
    gate corpora are built with, with the same degraded-shape guard.
  * Menu rows are worded by the gate's own ``menu_line`` helper.

Nothing in the production renderer is modified; this file is read-only over
those modules and is pure JSON processing (no Forge/GPL linkage).

Honesty rules
-------------
A field the shim JSON does not carry is either:

  * **derived** — only when the derivation is deterministic and the provenance
    is recorded per decision (basic-land name table; net-toughness>0 ⇒
    creature; turn-parity ⇒ own turn, validated per game); or
  * **reported as a gap** — never filled with a guess.

Renders are still emitted for gapped sections because the production renderer
owns the shape, but every decision record carries ``gaps`` / ``fabrications``
so no dataset builder can train on these prompts unaware. The headline
fabrication: hand cards without mana_cost/type_line render as ``[S,OK]`` —
an affordability claim the system prompt tells the model to trust.

Usage
-----
    .venv/bin/python -m tools.eval.forge_fidelity \
        --decisions <decisions.jsonl> \
        [--real-corpus tools/training/data/gate_play_decisions_test.jsonl] \
        [--out-prompts tools/eval/data/forge_rendered_prompts.jsonl] \
        [--report-json tools/eval/data/forge_fidelity_report.json] \
        [--report-md tools/eval/data/forge_fidelity_report.md]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# Production prompt path, reused by IMPORT (hard rule: never copy-paste).
from tools.training.gate_play_decisions import (  # noqa: E402
    TRIGGER,
    _read_jsonl,
    _sha256_file,
    _write_jsonl,
    build_user_message,
    menu_line,
)

DEFAULT_REAL_CORPUS = REPO / "tools" / "training" / "data" / "gate_play_decisions_test.jsonl"
DEFAULT_OUT_DIR = REPO / "tools" / "eval" / "data"

# ---------------------------------------------------------------------------
# Deterministic name/phase tables (MTG rules knowledge, not per-card guessing)
# ---------------------------------------------------------------------------

# The basic land names are reserved by the game rules; mapping them to a type
# line is a translation, not a guess. Everything else stays type-less and is
# counted as a gap.
BASIC_LAND_TYPE = {
    "Plains": "Basic Land — Plains",
    "Island": "Basic Land — Island",
    "Swamp": "Basic Land — Swamp",
    "Mountain": "Basic Land — Mountain",
    "Forest": "Basic Land — Forest",
    "Wastes": "Basic Land — Wastes",
    "Snow-Covered Plains": "Basic Snow Land — Plains",
    "Snow-Covered Island": "Basic Snow Land — Island",
    "Snow-Covered Swamp": "Basic Snow Land — Swamp",
    "Snow-Covered Mountain": "Basic Snow Land — Mountain",
    "Snow-Covered Forest": "Basic Snow Land — Forest",
    "Snow-Covered Wastes": "Basic Snow Land — Wastes",
}

# Forge PhaseType → MTGA GRE (phase, step). The production formatter strips the
# prefixes and string-matches ("Combat" in phase, "DeclareBlock" in step), so
# the exact GRE spellings matter.
FORGE_PHASE_MAP = {
    "UNTAP": ("Phase_Beginning", "Step_Untap"),
    "UPKEEP": ("Phase_Beginning", "Step_Upkeep"),
    "DRAW": ("Phase_Beginning", "Step_Draw"),
    "MAIN1": ("Phase_Main1", ""),
    "COMBAT_BEGIN": ("Phase_Combat", "Step_BeginCombat"),
    "COMBAT_DECLARE_ATTACKERS": ("Phase_Combat", "Step_DeclareAttack"),
    "COMBAT_DECLARE_BLOCKERS": ("Phase_Combat", "Step_DeclareBlock"),
    "COMBAT_FIRST_STRIKE_DAMAGE": ("Phase_Combat", "Step_FirstStrikeDamage"),
    "COMBAT_DAMAGE": ("Phase_Combat", "Step_CombatDamage"),
    "COMBAT_END": ("Phase_Combat", "Step_EndCombat"),
    "MAIN2": ("Phase_Main2", ""),
    "END_OF_TURN": ("Phase_Ending", "Step_End"),
    "CLEANUP": ("Phase_Ending", "Step_Cleanup"),
}

# Option-string grammar (from PlayerControllerExternal.describe()):
#   "<host> | Play land"                      → land drop
#   "<host> | <effect text>"                  → cast (host name absent from SA text)
#   "<name> - [Legendary ...]Creature P / T"  → creature cast (Spell.toString)
#   "{cost}[, ...]: <effect>"                 → activated ability (cost-colon)
#   "<oracle text containing host name>"      → cast (CARDNAME-style text)
#   "<bare name>"                             → cast (e.g. Auras)
_CREATURE_SPELL_RE = re.compile(r"^(?P<name>.+?) - (?:[A-Z][A-Za-z']* )*Creature(?: [\d*]+ / [\d*]+)?\s*$")
_COST_COLON_RE = re.compile(r"^[^:|]*\{[^}]+\}[^:|]*:\s")


def _longest_name_in(text: str, names: list[str]) -> str | None:
    """Longest known card name occurring in ``text`` (specific beats generic)."""
    best = None
    for n in names:
        if n and n in text and (best is None or len(n) > len(best)):
            best = n
    return best


def _opt_text(opt: Any) -> str:
    """Option display text across protocol versions (v1 str, v2 object)."""
    if isinstance(opt, dict):
        return str(opt.get("text") or "")
    return str(opt)


def map_option_v2(opt: dict) -> dict[str, Any]:
    """Protocol-v2 option object → production menu entry, no guessing.

    v2 options are ``{text, kind, host, affordable}`` where ``kind`` is the
    shim's own classification (land drop / spell / activated ability) and
    ``affordable`` is Forge's canPayCost verdict at decision time. Nothing to
    regex-classify: the mapping is a direct translation, recorded as
    ``shim_kind_*`` so the report can tell native mappings from inferred ones.
    """
    text = _opt_text(opt)
    host = str(opt.get("host") or "")
    kind = str(opt.get("kind") or "")
    affordable = bool(opt.get("affordable", False))
    if kind == "land":
        return {
            "kind": "play_land",
            "host": host,
            "entry": {"action_type": "ActionType_Play", "name": host},
            "mapping": "shim_kind_land",
            "affordable": True,
        }
    if kind == "spell":
        return {
            "kind": "cast",
            "host": host,
            "entry": {"action_type": "ActionType_Cast", "name": host},
            "mapping": "shim_kind_spell",
            "affordable": affordable,
        }
    if kind == "ability":
        return {
            "kind": "activate",
            "host": host,
            "entry": {"action_type": "ActionType_Activate", "name": host},
            "mapping": "shim_kind_ability",
            "affordable": affordable,
        }
    return {
        "kind": "unmapped",
        "host": host or None,
        "entry": {"menu_text": text},
        "mapping": "shim_kind_unknown",
        "affordable": affordable,
    }


def map_option(
    text: str, hand_names: list[str], own_board_names: list[str], opp_board_names: list[str]
) -> dict[str, Any]:
    """Classify one shim option string into a production menu entry.

    Returns ``{kind, host, entry, menu_text, mapping}`` where ``entry`` is the
    pseudo-groundtruth row handed to the gate's ``menu_line`` so the wording is
    production's, not ours. ``mapping`` records how confident the
    classification is; ``unmapped`` rows keep the raw string verbatim (flagged)
    rather than guessing an action type.
    """
    known = list(dict.fromkeys(hand_names + own_board_names + opp_board_names))

    if text.endswith(" | Play land"):
        host = text[: -len(" | Play land")]
        return {
            "kind": "play_land",
            "host": host,
            "entry": {"action_type": "ActionType_Play", "name": host},
            "mapping": "pipe_play_land",
        }

    m = _CREATURE_SPELL_RE.match(text)
    if m:
        return {
            "kind": "cast",
            "host": m.group("name"),
            "entry": {"action_type": "ActionType_Cast", "name": m.group("name")},
            "mapping": "creature_spell_pattern",
        }

    if " | " in text:
        host = text.split(" | ", 1)[0]
        return {
            "kind": "cast",
            "host": host,
            "entry": {"action_type": "ActionType_Cast", "name": host},
            "mapping": "pipe_cast",
        }

    if _COST_COLON_RE.match(text):
        host = _longest_name_in(text, known)
        if host:
            return {
                "kind": "activate",
                "host": host,
                "entry": {"action_type": "ActionType_Activate", "name": host},
                "mapping": "cost_colon_activation",
            }
        return {
            "kind": "unmapped",
            "host": None,
            "entry": {"menu_text": text},
            "mapping": "cost_colon_no_host",
        }

    if text in hand_names:
        return {
            "kind": "cast",
            "host": text,
            "entry": {"action_type": "ActionType_Cast", "name": text},
            "mapping": "bare_hand_name",
        }

    host = _longest_name_in(text, hand_names)
    if host:
        return {
            "kind": "cast",
            "host": host,
            "entry": {"action_type": "ActionType_Cast", "name": host},
            "mapping": "cardname_in_text",
        }

    host = _longest_name_in(text, own_board_names)
    if host:
        return {
            "kind": "activate",
            "host": host,
            "entry": {"action_type": "ActionType_Activate", "name": host},
            "mapping": "board_name_in_text",
        }

    return {"kind": "unmapped", "host": None, "entry": {"menu_text": text}, "mapping": "unmapped"}


# ---------------------------------------------------------------------------
# Own-turn derivation (the shim does not say whose turn it is)
# ---------------------------------------------------------------------------


def own_turn_parity_by_game(decisions: list[dict]) -> dict[int, int | None]:
    """Per game: the turn-number parity of the shim player's own turns.

    v1-only fallback — protocol v2 ships ``is_own_turn`` natively and never
    consults this. A ``"| Play land"`` option is proof the deciding player is
    the active player (Forge only offers the drop on your own main phase). All
    such evidence within one game must agree on ``turn % 2`` — Forge
    alternates turns and this card pool has no extra-turn effects. Conflicting
    or absent evidence yields None and every decision in that game reports
    ``is_own_turn`` as unknown rather than guessed.
    """
    evidence: dict[int, set[int]] = defaultdict(set)
    for q in decisions:
        if any(_opt_text(o).endswith(" | Play land") for o in q.get("options") or []):
            evidence[int(q["game"])].add(int(q["turn"]) % 2)
    out: dict[int, int | None] = {}
    for game, parities in evidence.items():
        out[game] = parities.pop() if len(parities) == 1 else None
    return out


# ---------------------------------------------------------------------------
# Shim decision → production game-state dict
# ---------------------------------------------------------------------------


def _board_card(
    obj: dict,
    seat: int,
    turn_num: int,
    instance_id: int,
    derived: set[str],
    gaps: set[str],
    fabrications: set[str],
) -> dict:
    """One battlefield row in the shape ``_format_game_context`` consumes.

    Protocol v2 rows carry native card facts (type_line / oracle_text /
    mana_cost straight from the engine's current state) and an explicit
    ``face_down`` marker; nothing is derived. v1 rows (name + net P/T only)
    keep the old honest derivations and gap flags.

    Face-down rendering matches production's shape for a card the game hides:
    the name renders as ``Unknown`` (production's display name when card
    identity cannot be resolved — ``enrich_with_oracle_text(0)`` /
    ``_serialize_snapshot_obj``), while type/P/T/tapped stay the engine's
    public truth (a face-down permanent IS a 2/2 creature). The deciding
    player's OWN face-down cards render from ``known_face`` — their owner
    legitimately knows the hidden face, exactly as the MTGA client shows it.
    """
    is_v2 = "type_line" in obj or "face_down" in obj
    face_down = bool(obj.get("face_down"))
    known = obj.get("known_face") if face_down else None
    power = obj.get("power", 0)
    toughness = obj.get("toughness", 0)
    counters = obj.get("counters") or {}

    if is_v2:
        if face_down and known:
            name = str(known.get("name") or "")
            type_line = str(known.get("type_line") or "")
            oracle_text = str(known.get("oracle_text") or "")
            mana_cost = str(known.get("mana_cost") or "")
            derived.add("own_face_down_rendered_from_known_face")
        elif face_down:
            name = "Unknown"
            # Forge's current state for a face-down permanent is natively
            # "Creature" (rule 708.2) — shim-native, not derived.
            type_line = str(obj.get("type_line") or "")
            oracle_text = ""
            mana_cost = ""
            derived.add("face_down_rendered_as_unknown")
        else:
            name = str(obj.get("name") or "")
            type_line = str(obj.get("type_line") or "")
            oracle_text = str(obj.get("oracle_text") or "")
            mana_cost = str(obj.get("mana_cost") or "")
            if not type_line:
                gaps.add("board_nonbasic_type_line")
        is_creature = "creature" in type_line.lower()
    else:
        name = str(obj.get("name") or "")
        type_line = ""
        oracle_text = ""
        mana_cost = ""
        if name in BASIC_LAND_TYPE:
            type_line = BASIC_LAND_TYPE[name]
            derived.add("basic_land_type_line")
        # Forge getNetPower/getNetToughness return 0/0 for non-creature
        # permanents; a live creature cannot be 0/0 without counters. Net
        # toughness > 0 (or a 0/0 carrying counters) ⇒ creature. Recorded as
        # derived, not shim-native.
        is_creature = toughness > 0 or (power == 0 and toughness == 0 and bool(counters) and not type_line)
        if is_creature and not type_line:
            type_line = "Creature"
            derived.add("creature_from_net_pt")
        if not type_line and not is_creature:
            gaps.add("board_nonbasic_type_line")
        if name == "":
            gaps.add("board_face_down_names_empty")

    card = {
        "instance_id": obj.get("id", instance_id),
        "grp_id": None,
        "name": name,
        "type_line": type_line,
        "oracle_text": oracle_text,
        "mana_cost": mana_cost,
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "is_tapped": bool(obj.get("tapped", False)),
        # Forge's isSick() is authoritative summoning sickness; the formatter
        # recomputes it as turn_entered == current turn.
        "turn_entered_battlefield": turn_num if obj.get("sick") else -1,
    }
    if obj.get("attached_to") is not None:
        card["parent_instance_id"] = obj["attached_to"]
    if counters:
        card["counters"] = dict(counters)
    if is_creature:
        card["power"] = int(power)
        card["toughness"] = int(toughness)
    return card


def build_game_state_from_shim(
    req: dict, own_parity: int | None, first_own_main1: bool
) -> tuple[dict, list[str], dict]:
    """Map one shim decision request → (game_state, menu_texts, meta).

    ``meta`` records every derivation, gap, and fabrication for this decision
    so the rendered corpus is honest about its own provenance.
    """
    local_seat, opp_seat = 1, 2
    players_in = req.get("players") or []
    if not players_in or players_in[0].get("name") != req.get("priority_player"):
        raise ValueError("players[0] is not the deciding player — shim contract violated")
    local_in = players_in[0]
    opp_in = players_in[1] if len(players_in) > 1 else {"name": "?", "life": None, "board": []}

    turn_num = int(req.get("turn", 0))
    phase_raw = str(req.get("phase") or "")
    phase, step = FORGE_PHASE_MAP.get(phase_raw, (phase_raw, ""))
    protocol = int(req.get("protocol", 1))

    derived: set[str] = set()
    gaps: set[str] = set()
    fabrications: set[str] = set()

    # Whose turn is it? v2 ships the engine's own answer; v1 falls back to
    # direct proof (land-drop option) then parity inference.
    has_land_option = any(_opt_text(o).endswith(" | Play land") for o in req.get("options") or [])
    if "is_own_turn" in req:
        is_own_turn: bool | None = bool(req["is_own_turn"])
        own_turn_source = "shim_native"
    elif has_land_option:
        is_own_turn = True
        own_turn_source = "land_drop_direct"
    elif own_parity is not None:
        is_own_turn = turn_num % 2 == own_parity
        own_turn_source = "turn_parity_inference"
        derived.add("is_own_turn_parity")
    else:
        is_own_turn = None
        own_turn_source = "unknown"
        gaps.add("active_player_unknown")

    # active_player=0 is production's own "unknown" sentinel (renders OPP);
    # decisions in that state are flagged, never silently guessed.
    active_seat = local_seat if is_own_turn else (opp_seat if is_own_turn is False else 0)

    battlefield: list[dict] = []
    for i, obj in enumerate(local_in.get("board") or []):
        battlefield.append(_board_card(obj, local_seat, turn_num, 100000 + i, derived, gaps, fabrications))
    for i, obj in enumerate(opp_in.get("board") or []):
        battlefield.append(_board_card(obj, opp_seat, turn_num, 900000 + i, derived, gaps, fabrications))

    hand: list[dict] = []
    hand_names: list[str] = []
    for i, h in enumerate(req.get("hand") or []):
        if isinstance(h, dict):
            # Protocol v2: native oracle facts. Castability tags are now
            # COMPUTED by the production renderer from real cost/type facts —
            # the v1 '[S,OK]' fabrication is structurally impossible here.
            name = str(h.get("name") or "")
            hand.append(
                {
                    "instance_id": 500000 + i,
                    "grp_id": None,
                    "name": name,
                    "type_line": str(h.get("type_line") or ""),
                    "mana_cost": str(h.get("mana_cost") or ""),
                    "cmc": int(h.get("cmc") or 0),
                    "oracle_text": str(h.get("oracle_text") or ""),
                }
            )
            if not h.get("type_line"):
                gaps.add("hand_card_facts")
                fabrications.add("hand_castability_ok_without_cost")
        else:
            name = str(h)
            type_line = BASIC_LAND_TYPE.get(name, "")
            if type_line:
                derived.add("basic_land_type_line")
            hand.append(
                {
                    "instance_id": 500000 + i,
                    "grp_id": None,
                    "name": name,
                    "type_line": type_line,
                    "mana_cost": "",
                    "cmc": 0,
                    "oracle_text": "",
                }
            )
            if not type_line:
                # No cost/type ⇒ _check_castability returns "OK" ⇒ the prompt
                # asserts castability the shim JSON cannot support. This is the
                # exact placeholder-that-trains-wrong the report exists to flag.
                fabrications.add("hand_castability_ok_without_cost")
                gaps.add("hand_card_facts")  # mana_cost / type_line / oracle_text missing
        hand_names.append(name)

    # Menu: shim options in order, then Pass (the adapter's pick == -1).
    own_names = [c["name"] for c in battlefield if c["owner_seat_id"] == local_seat]
    opp_names = [c["name"] for c in battlefield if c["owner_seat_id"] == opp_seat]
    mappings = []
    for o in req.get("options") or []:
        if isinstance(o, dict):
            mappings.append(map_option_v2(o))
        else:
            mp = map_option(str(o), hand_names, own_names, opp_names)
            # v1 options were affordability-prefiltered Java-side (canPlay +
            # canPayCost), which is exactly the [OK] contract, so Cast rows
            # are affordable=True by construction.
            mp["affordable"] = mp["entry"].get("action_type") == "ActionType_Cast"
            mappings.append(mp)
    menu_rows = []
    for mp in mappings:
        menu_rows.append(menu_line(mp["entry"], bool(mp.get("affordable"))))
    menu_rows.append(menu_line({"action_type": "ActionType_Pass"}, False))
    if any(mp["kind"] == "unmapped" for mp in mappings):
        gaps.add("menu_option_unmapped")
    if protocol < 2:
        # v1 menus cannot contain unaffordable Cast rows (Java prefiltered
        # them); the real-MTGA distribution has 59% of Cast rows unaffordable.
        gaps.add("unaffordable_menu_entries_missing")

    # Sections the shim JSON carries nothing for. The reference gate corpus
    # leaves the same sections empty (its own documented fidelity limit), so
    # renders still match the gate shape — but production parity needs them.
    gaps.update(
        {
            "stack_contents",
            "graveyard_contents",
            "recent_events",
            "damage_taken",
        }
    )
    if "lands_played" not in local_in:
        gaps.add("lands_played_count")
    if "hand_size" not in opp_in:
        gaps.add("opponent_hand_size")

    game_state = {
        "players": [
            {
                "seat_id": local_seat,
                "is_local": True,
                "life_total": local_in.get("life"),
                # v2: native Player.getLandsPlayedThisTurn(). v1: unknown —
                # 0 + the menu's land entry (or lack of one) makes the Land:
                # line read AVAILABLE / "not playable in this window" — never
                # a fabricated USED count.
                "lands_played": int(local_in.get("lands_played", 0)),
                "hand_size": len(hand),
            },
            {
                "seat_id": opp_seat,
                "is_local": False,
                "life_total": opp_in.get("life"),
                "lands_played": int(opp_in.get("lands_played", 0)),
                "hand_size": int(opp_in.get("hand_size", 0)),
            },
        ],
        "turn": {
            "turn_number": turn_num,
            "phase": phase,
            "step": step,
            "active_player": active_seat,
            "priority_player": local_seat,
        },
        "battlefield": battlefield,
        "hand": hand,
        "stack": [],
        "graveyard": [],
        "legal_actions": [],
    }

    # Trigger: production fires new_turn for the first own-Main-1 window of a
    # turn (the gate corpus default). Everything else is a plain priority
    # window; combat/attacker triggers are NOT used because the shim decision
    # is never an attack/block declaration (stock Forge AI handles those).
    if is_own_turn and phase_raw == "MAIN1" and first_own_main1:
        trigger = TRIGGER  # "new_turn"
    elif is_own_turn is False:
        trigger = "opponent_turn"
    else:
        trigger = "priority_gained"

    meta = {
        "protocol": protocol,
        "turn": turn_num,
        "forge_phase": phase_raw,
        "phase": phase,
        "step": step,
        "trigger": trigger,
        "is_own_turn": is_own_turn,
        "own_turn_source": own_turn_source,
        "menu": [
            {
                "index": i + 1,
                "text": t,
                "kind": mp["kind"],
                "host": mp["host"],
                "mapping": mp["mapping"],
                "affordable": bool(mp.get("affordable")),
            }
            for i, (t, mp) in enumerate(zip(menu_rows, mappings, strict=False))
        ]
        + [
            {
                "index": len(menu_rows),
                "text": "Pass",
                "kind": "pass",
                "host": None,
                "mapping": "implicit_pass",
            }
        ],
        "menu_size": len(menu_rows),
        "shim_mana_available": req.get("mana_available"),
        "derived": sorted(derived),
        "gaps": sorted(gaps),
        "fabrications": sorted(fabrications),
    }
    return game_state, menu_rows, meta


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_decisions(decisions: list[dict]) -> tuple[list[dict], list[dict]]:
    """Render every shim decision through the production prompt path."""
    from arenamcp.action_planner import AUTOPILOT_SYSTEM_PROMPT

    parity = own_turn_parity_by_game(decisions)
    rendered: list[dict] = []
    failures: list[dict] = []
    seen_own_main1: set[tuple[int, int]] = set()
    for q in decisions:
        game = int(q.get("game", 0))
        seq = q.get("seq")
        key = (game, int(q.get("turn", 0)))
        first_own_main1 = key not in seen_own_main1
        try:
            game_state, menu_rows, meta = build_game_state_from_shim(q, parity.get(game), first_own_main1)
            if meta["trigger"] == TRIGGER:
                seen_own_main1.add(key)
            user = build_user_message(game_state, menu_rows, meta["trigger"])
        except Exception as e:  # noqa: BLE001 — each failure is itself a finding
            failures.append({"game": game, "seq": seq, "error": f"{type(e).__name__}: {e}"})
            continue
        meta["source"] = {"game": game, "seq": seq, "ts": q.get("_ts")}
        meta["shim_reply_pick"] = (q.get("_reply") or {}).get("pick")
        rendered.append(
            {
                "id": f"forge_g{game}_s{seq}",
                "system": AUTOPILOT_SYSTEM_PROMPT,
                "user": user,
                "max_tokens": 400,
                "temperature": 0.0,
                "meta": meta,
            }
        )
    return rendered, failures


# ---------------------------------------------------------------------------
# Divergence analysis
# ---------------------------------------------------------------------------

# Section anchors, in prompt order. Each is (anchor regex, populated regex or
# None meaning "presence == populated").
SECTION_ANCHORS: dict[str, tuple[str, str | None]] = {
    "trigger": (r"^TRIGGER: ", None),
    "header": (r"^=== (NEW )?GAME ===", None),
    "legal_menu": (r"^Legal: \(pick by number\)", None),
    "turn_line": (r"^T\d+ (YOUR|OPP) \| ", None),
    "timing": (r"^(Timing: |ACTION: DECLARE BLOCKERS)", None),
    "life": (r"^Life: You=", None),
    "mana": (r"^Mana: ", r"^Mana: [1-9]"),
    "land_status": (r"^Land: ", None),
    "your_board": (r"^YOUR BOARD:", r"^YOUR BOARD:\n(?!  \(empty\))"),
    "opp_board": (r"^OPP BOARD:", r"^OPP BOARD:\n(?!  \(empty\))"),
    "combat_solver": (r"^Computed optimal ", None),
    "post_land_plan": (r"^THEN: ", None),
    "stack": (r"^STACK \(top resolves first\):", None),
    "graveyard": (r"^GY: ", None),
    "recent_events": (r"^Recent: ", None),
    "revealed": (r"^Opp revealed ", None),
    "command_zone": (r"^COMMAND ZONE:", None),
    "hand": (r"^HAND:", r"^HAND:\n(?!  \(empty\))"),
    "excluded": (r"^EXCLUDED", None),
    "footer": (r"^Respond with ONLY a JSON action plan matching the schema\.$", None),
}

# Verdict per section for the parent JSON: can the shim JSON fill it?
#   populated       — rendered with real shim-backed content
#   empty           — legitimately empty for these decisions (matches reference)
#   unconstructible — the real corpus populates it, the shim JSON cannot
SECTION_VERDICT_NOTES = {
    "trigger": "mapped from phase + own-turn (v2: engine-native is_own_turn); new_turn only on own Main-1",
    "header": "constant",
    "legal_menu": "options[] + implicit Pass, worded by the gate's menu_line; v2 rows carry "
    "native kind/host/affordable (incl. unaffordable Cast rows, like real MTGA menus)",
    "turn_line": "turn number native; YOUR/OPP from v2 native is_own_turn (v1: parity inference)",
    "timing": "derived from phase/step + own-turn; honest given those inputs",
    "life": "native (players[].life)",
    "mana": "v2 ships per-card type_line/oracle facts so nonbasic lands and mana creatures are "
    "visible to the renderer's own mana counter (v1 saw only name-matched basics)",
    "land_status": "v2: lands_played native (Player.getLandsPlayedThisTurn) so USED(n) renders "
    "truthfully; AVAILABLE still proven by the menu's land entry",
    "your_board": "names/P/T/tapped/sick/counters native; v2 adds native type_line/oracle/"
    "mana_cost (keyword flags, ARTIFACT/ENCHANT tags, aura attachment via attached_to); "
    "face-down rows render as production renders hidden cards (name 'Unknown', engine-true "
    "2/2 Creature; own face-down cards render their known_face)",
    "opp_board": "same as your_board",
    "combat_solver": "v2: creature set from native type_line (v1: net-toughness derivation)",
    "post_land_plan": "constructible with v2 hand mana costs; fires only on own-main "
    "land-drop windows with lands in hand",
    "stack": "shim JSON has no stack; reference gate corpus is also stack-empty by design",
    "graveyard": "shim JSON has no graveyard; reference also empty",
    "recent_events": "shim JSON has no event history; reference also empty",
    "revealed": "not in shim; reference also empty",
    "command_zone": "not in shim; reference also empty",
    "hand": "v2: full native facts (mana_cost/type_line/oracle/cmc) — castability tags are "
    "computed by the production renderer from real facts, so the v1 '[S,OK]' fabrication "
    "is structurally impossible; v1 rows keep the fabrication flag",
    "excluded": "empty by construction (no planner-side exclusions), matches reference",
    "footer": "constant",
}


def _section_counts(prompts: list[str]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for name, (anchor, populated) in SECTION_ANCHORS.items():
        present = sum(1 for u in prompts if re.search(anchor, u, re.M))
        pop = present
        if populated is not None:
            pop = sum(1 for u in prompts if re.search(populated, u, re.M))
        out[name] = {"present": present, "populated": pop}
    return out


def _menu_head(text: str) -> str:
    for head in ("Play Land:", "Activate Ability:", "Cast ", "Action:", "Pass"):
        if text.startswith(head):
            return head.rstrip()
    return "verbatim_unmapped"


def _menu_stats_real(real: list[dict]) -> dict:
    heads = Counter()
    cast = ok = 0
    pass_last = 0
    for r in real:
        menu = r["meta"]["menu"]
        for m in menu:
            h = _menu_head(m["text"])
            heads[h] += 1
            if h == "Cast":
                cast += 1
                ok += "[OK]" in m["text"]
        if menu and menu[-1]["text"] == "Pass":
            pass_last += 1
    return {
        "n_menus": len(real),
        "entry_heads": dict(heads),
        "cast_entries": cast,
        "cast_with_ok": ok,
        "pass_is_last_menu_entry": pass_last,
    }


def _menu_stats_forge(rendered: list[dict]) -> dict:
    heads = Counter()
    mappings = Counter()
    cast = ok = 0
    for r in rendered:
        for m in r["meta"]["menu"]:
            h = _menu_head(m["text"])
            heads[h] += 1
            mappings[m["mapping"]] += 1
            if h == "Cast":
                cast += 1
                ok += "[OK]" in m["text"]
    return {
        "n_menus": len(rendered),
        "entry_heads": dict(heads),
        "cast_entries": cast,
        "cast_with_ok": ok,
        "pass_is_last_menu_entry": len(rendered),  # appended last by construction
        "mapping_breakdown": dict(mappings),
    }


def _mana_fidelity(rendered: list[dict]) -> dict:
    """Rendered Mana: total vs Forge's own mana_available estimate."""
    exact = under = over = compared = 0
    examples: list[dict] = []
    mana_re = re.compile(r"^Mana: (\d+)", re.M)
    for r in rendered:
        shim = r["meta"].get("shim_mana_available")
        m = mana_re.search(r["user"])
        if shim is None or m is None:
            continue
        compared += 1
        got = int(m.group(1))
        if got == int(shim):
            exact += 1
        elif got < int(shim):
            under += 1
            if len(examples) < 5:
                examples.append({"id": r["id"], "rendered": got, "forge_estimate": int(shim)})
        else:
            over += 1
            if len(examples) < 5:
                examples.append({"id": r["id"], "rendered": got, "forge_estimate": int(shim)})
    return {
        "compared": compared,
        "exact_match": exact,
        "rendered_below_forge_estimate": under,
        "rendered_above_forge_estimate": over,
        "exact_match_rate": round(exact / compared, 4) if compared else None,
        "mismatch_examples": examples,
        "note": "Forge's mana_available counts every producible mana (incl. nonbasic lands and "
        "mana creatures). With protocol v2 the renderer sees per-card type_line/oracle facts, "
        "so its own mana counter can see nonbasic lands and mana creatures too; residual "
        "mismatches are the production counter's own heuristics (e.g. 'add {' oracle matching), "
        "not missing shim data. v1 renders saw only name-matched basics.",
    }


def section_verdicts(real_counts: dict, forge_counts: dict, n_real: int, n_forge: int) -> dict:
    """Three-state verdict per section + the evidence for it."""
    out = {}
    for name in SECTION_ANCHORS:
        real_pop = real_counts[name]["populated"]
        forge_pop = forge_counts[name]["populated"]
        if forge_pop > 0:
            status = "populated"
        elif real_pop == 0:
            # Nobody populates it on these decision shapes (reference included).
            status = "empty"
        else:
            status = "unconstructible"
        # Sections whose content is structurally beyond the shim JSON stay
        # unconstructible even though the reference corpus also leaves them
        # empty — the reference's own documented fidelity limits must not
        # launder the shim's gaps into "empty".
        if name in ("stack", "graveyard", "recent_events", "post_land_plan") and forge_pop == 0:
            status = "unconstructible"
        out[name] = {
            "status": status,
            "real_populated": real_pop,
            "real_total": n_real,
            "forge_populated": forge_pop,
            "forge_total": n_forge,
            "notes": SECTION_VERDICT_NOTES.get(name, ""),
        }
    return out


# Tie-break order when two gaps have the same real-corpus frequency: a prompt
# that CLAIMS something false outranks one that renders a degraded truth.
_SEVERITY_RANK = {
    "fabricates_wrong_claim": 0,
    "unstable_derivation": 1,
    "renders_degraded_truth": 2,
    "distribution_shift": 3,
    "missing_section": 4,
}


def ranked_gaps(real: list[dict], rendered: list[dict], real_counts: dict, n_real: int) -> list[dict]:
    """Shim JSON gaps ranked by how often the real corpus populates the
    prompt content that depends on them. This ranking IS the shim TODO order.

    Every ``affected_decisions`` is COUNTED from the rendered sample, so a
    v2 run reports its own closure honestly: a gap whose count fell to zero
    is marked ``closed`` (and sorts after open gaps) rather than deleted —
    the report keeps proving the negative.
    """
    real_menu = _menu_stats_real(real)
    forge_menu = _menu_stats_forge(rendered)
    n_forge = len(rendered)

    def freq(x: int, n: int) -> float:
        return round(x / n, 4) if n else 0.0

    def n_with_gap(flag: str) -> int:
        return sum(1 for r in rendered if flag in r["meta"]["gaps"])

    # v1 menus were affordability-prefiltered; v2 ships the tag instead. The
    # honest closure test is on the DATA: unaffordable Cast rows must actually
    # occur in the rendered menus (real MTGA: 59% of Cast rows lack [OK]).
    n_unaffordable_gap = n_with_gap("unaffordable_menu_entries_missing")
    forge_cast_without_ok = forge_menu["cast_entries"] - forge_menu["cast_with_ok"]

    gaps = [
        {
            "gap": "hand_card_facts",
            "severity": "fabricates_wrong_claim",
            "shim_fields_missing": ["hand[].mana_cost", "hand[].type_line", "hand[].oracle_text"],
            "real_corpus_frequency": freq(real_counts["hand"]["populated"], n_real),
            "impact": "every real prompt renders hand rows with cost + [S/I,OK/NEED/LAND] tags and "
            "oracle text; without facts the renderer FABRICATES '[S,OK]' on every non-basic "
            "hand card (the system prompt says to trust OK) and [NO TARGETS]/THEN analysis "
            "is silently disabled. Closed by protocol v2: hand rows ship native facts and "
            "the tags are computed by the production renderer",
            "affected_decisions": n_with_gap("hand_card_facts"),
        },
        {
            "gap": "board_card_facts",
            "severity": "renders_degraded_truth",
            "shim_fields_missing": ["board[].type_line (or is_land/is_creature)", "board[].oracle_text"],
            "real_corpus_frequency": max(
                freq(real_counts["mana"]["populated"], n_real),
                freq(real_counts["your_board"]["populated"], n_real),
            ),
            "impact": "drives Mana: (nonbasic lands invisible without type facts), keyword flags "
            "(FLY/VIG/DTH...), ARTIFACT/ENCHANT tags, aura attachment, and the combat "
            "solver's creature set. Closed by protocol v2: board rows ship native "
            "type_line/oracle_text/mana_cost/attached_to",
            "affected_decisions": n_with_gap("board_nonbasic_type_line"),
        },
        {
            "gap": "active_player",
            "severity": "unstable_derivation",
            "shim_fields_missing": ["is_own_turn (or active_player name)"],
            "real_corpus_frequency": 1.0,
            "impact": "every real prompt states YOUR/OPP turn, Timing:, Land: and the trigger line; "
            "v1 derived it by land-drop turn-parity inference, which breaks under extra-turn "
            "effects and games with no land drop. Closed by protocol v2: "
            "PhaseHandler.isPlayerTurn ships as is_own_turn",
            "affected_decisions": sum(
                1 for r in rendered if r["meta"]["own_turn_source"] not in ("shim_native", "land_drop_direct")
            ),
        },
        {
            "gap": "lands_played_count",
            "severity": "renders_degraded_truth",
            "shim_fields_missing": ["players[].lands_played_this_turn"],
            "real_corpus_frequency": freq(real_counts["land_status"]["populated"], n_real),
            "impact": "Land: USED(n) unknowable — own-main windows without a land option render "
            "'not playable in this window' even when the truth is 'already used'. Closed by "
            "protocol v2: Player.getLandsPlayedThisTurn ships per player",
            "affected_decisions": n_with_gap("lands_played_count"),
        },
        {
            "gap": "unaffordable_menu_entries",
            "severity": "distribution_shift",
            "shim_fields_missing": ["options[] filtered by canPayCost — no unaffordable Cast rows exist"],
            "real_corpus_frequency": freq(
                real_menu["cast_entries"] - real_menu["cast_with_ok"], real_menu["cast_entries"]
            ),
            "impact": "real MTGA menus are NOT affordability-filtered (59% of real Cast rows lack "
            "[OK]); a model trained only on affordability-prefiltered menus never learns to "
            "skip unaffordable entries. Closed by protocol v2: spells are enumerated by "
            "canPlay() alone and each option carries an affordable tag "
            f"(this sample renders {forge_cast_without_ok} Cast rows without [OK]); "
            "an unaffordable pick is refused Java-side (SHIM-REFUSED, window passes priority)",
            "affected_decisions": n_unaffordable_gap,
        },
        {
            "gap": "stack_contents",
            "severity": "missing_section",
            "shim_fields_missing": ["stack[] (name, controller, targets)"],
            "real_corpus_frequency": freq(real_counts["stack"]["populated"], n_real),
            "impact": "reference gate corpus is stack-empty by design so gate parity holds, but "
            "instant-speed decisions (respond/counter) are untrainable without it",
            "affected_decisions": n_forge,
        },
        {
            "gap": "graveyard_contents",
            "severity": "missing_section",
            "shim_fields_missing": ["graveyard[] per player"],
            "real_corpus_frequency": freq(real_counts["graveyard"]["populated"], n_real),
            "impact": "reference also empty; needed for full production parity (GY: line)",
            "affected_decisions": n_forge,
        },
        {
            "gap": "recent_events",
            "severity": "missing_section",
            "shim_fields_missing": ["event history (casts, damage, zone moves)"],
            "real_corpus_frequency": freq(real_counts["recent_events"]["populated"], n_real),
            "impact": "reference also empty; production live prompts carry a Recent: line",
            "affected_decisions": n_forge,
        },
        {
            "gap": "face_down_names_empty",
            "severity": "renders_degraded_truth",
            "shim_fields_missing": ["board[].face_down marker (morphs serialize as name='')"],
            "real_corpus_frequency": 0.0,
            "impact": "face-down creatures render as a nameless board row. Closed by protocol v2: "
            "face-down permanents ship face_down=true with name null and render the way "
            "production renders hidden cards ('Unknown' + engine-true 2/2 Creature); the "
            "deciding player's own face-down cards ship known_face",
            "affected_decisions": n_with_gap("board_face_down_names_empty"),
        },
    ]
    for g in gaps:
        g["status"] = "closed" if g["affected_decisions"] == 0 else "open"
    gaps.sort(
        key=lambda g: (
            g["status"] == "closed",  # open gaps first — they are the TODO list
            -g["real_corpus_frequency"],
            _SEVERITY_RANK.get(g["severity"], 9),
            -g["affected_decisions"],
        )
    )
    for i, g in enumerate(gaps, 1):
        g["rank"] = i
    return gaps


def build_report(
    decisions_path: Path,
    real_path: Path,
    decisions: list[dict],
    skipped: list[dict],
    real: list[dict],
    rendered: list[dict],
    failures: list[dict],
) -> dict:
    real_prompts = [r["user"] for r in real]
    forge_prompts = [r["user"] for r in rendered]
    real_counts = _section_counts(real_prompts)
    forge_counts = _section_counts(forge_prompts)
    n_real, n_forge = len(real_prompts), len(forge_prompts)

    verdicts = section_verdicts(real_counts, forge_counts, n_real, n_forge)
    own_turn = Counter(r["meta"]["own_turn_source"] for r in rendered)
    trigger_mix = Counter(r["meta"]["trigger"] for r in rendered)
    fabrications = Counter()
    for r in rendered:
        fabrications.update(r["meta"]["fabrications"])

    real_menu = _menu_stats_real(real)
    forge_menu = _menu_stats_forge(rendered)
    unmapped = forge_menu["entry_heads"].get("verbatim_unmapped", 0)
    total_forge_rows = sum(forge_menu["entry_heads"].values())

    # The system prompt is the imported production object, byte-identical to
    # what a corpus built TODAY would carry. The stored real corpus may lag the
    # working tree (it snapshots the constant at its own build time), so both
    # facts are reported instead of one misleading boolean.
    matches_stored = all(r["system"] == real[0]["system"] for r in rendered) if rendered and real else None

    return {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "inputs": {
            "decisions_path": str(decisions_path),
            "decisions_sha256": _sha256_file(decisions_path),
            "decisions_total_lines": len(decisions) + len(skipped),
            "decisions_used": len(decisions),
            "non_decision_records_skipped": len(skipped),
            "real_corpus_path": str(real_path),
            "real_corpus_sha256": _sha256_file(real_path),
            "real_corpus_n": n_real,
        },
        "render": {
            "rendered": len(rendered),
            "failures": len(failures),
            "failure_examples": failures[:10],
            "system_prompt_source": "imported arenamcp.action_planner.AUTOPILOT_SYSTEM_PROMPT "
            "(the same object the gate builder uses — never retyped)",
            "system_prompt_matches_stored_real_corpus": matches_stored,
            "system_prompt_mismatch_reason": None
            if matches_stored
            else "the stored real corpus snapshots the constant at its own build time and the "
            "working tree has changed it since (uncommitted action_planner.py edits); renders "
            "always track the current production constant by import",
            "guard": "build_user_message raises on the degraded fallback shape; every rendered "
            "prompt passed the production-shape guard ('Legal: (pick by number)' + 'Life: You=')",
        },
        "sections": verdicts,
        "section_counts": {"real": real_counts, "forge": forge_counts},
        "mana_fidelity": _mana_fidelity(rendered),
        "own_turn_derivation": dict(own_turn),
        "trigger_mix": dict(trigger_mix),
        "fabrication_risks": {
            "counts": dict(fabrications),
            "detail": (
                "hand_castability_ok_without_cost: hand rows without mana_cost/type_line "
                "render '[S,OK]' — an affordability claim the shim JSON cannot support and the "
                "system prompt instructs the model to trust. Do NOT train on these prompts until "
                "the shim ships hand card facts (or a name→facts resolver is added)."
                if fabrications
                else "none: every rendered claim traces to shim-native facts (protocol v2 ships "
                "hand/board card facts, is_own_turn, lands_played and per-option affordability; "
                "castability tags are computed by the production renderer from real facts)."
            ),
        },
        "menu_format": {
            "real": real_menu,
            "forge": forge_menu,
            "unmapped_rows": unmapped,
            "unmapped_share": round(unmapped / total_forge_rows, 4) if total_forge_rows else None,
            "diff_notes": [
                (
                    f"Forge v2 menus include unaffordable Cast rows like real MTGA menus: "
                    f"{forge_menu['cast_entries'] - forge_menu['cast_with_ok']}/"
                    f"{forge_menu['cast_entries']} Forge Cast rows lack [OK] vs "
                    f"{real_menu['cast_entries'] - real_menu['cast_with_ok']}/"
                    f"{real_menu['cast_entries']} real."
                    if forge_menu["cast_entries"] > forge_menu["cast_with_ok"]
                    else "Forge menus in this sample are affordability-prefiltered (v1 shim: "
                    "canPlay + canPayCost), so every Cast row is tagged [OK]; real MTGA menus "
                    "include unaffordable casts (59% of real Cast rows lack [OK])."
                ),
                "Pass is appended last by construction (adapter pick == -1); in the real corpus Pass "
                f"is last in {real_menu['pass_is_last_menu_entry']}/{real_menu['n_menus']} menus.",
                "Forge dedupes identical option strings (four Mountains = one row); MTGA emits one "
                "row per castable instance/printing.",
                "Real menus contain 'Action: <type>' rows (e.g. ActionType_ActivateMDFC) that the "
                "shim never produces.",
                "Entry mix is inverted: real gate menus are Cast-dominated (937 Cast vs 171 "
                "Activate over 206 menus) while shim windows are activation-dominated — Forge "
                "polls every priority window and repeatable pump abilities recur in most of "
                "them. Corpus composition, not a rendering defect.",
            ],
        },
        "fidelity_advantages_over_reference": [
            "net P/T: Forge ships getNetPower/getNetToughness (buffs, auras, counters applied); "
            "the reference gate corpus can only restore PRINTED P/T from the card DB and "
            "documents that as its own limit",
            "opponent tapped state: live per-permanent; reference opp rows are bare grpIds with "
            "no tapped state",
            "summoning sickness: authoritative engine isSick(); reference assumes start-of-main "
            "so nothing is sick",
            "affordability: every Cast option is solver-verified payable (canPayCost) at "
            "decision time; the reference recomputes affordability heuristically",
            "per-permanent counters: shipped live; reference cannot recover them",
        ],
        "runner_compatibility": "rendered records carry {id, system, user, max_tokens, "
        "temperature, meta} — directly consumable by tools.eval.run like any gate corpus",
        "decision_mix_note": "Forge shim decisions are chooseSpellAbilityToPlay priority windows "
        "in every phase (UPKEEP/DRAW dominate this sample); the real gate corpus is "
        "start-of-own-Main-1 only. Attack/block/target/mulligan decisions never reach the shim "
        "(stock Forge AI resolves them), so the corpus cannot cover those gate decision kinds.",
        "labels_note": "shim_reply_pick in this sample is a uniform-random policy with "
        "pass_prob=0.5 — structural round-trip data, NOT gold labels.",
        "gaps_ranked": ranked_gaps(real, rendered, real_counts, n_real),
    }


def write_md(report: dict, path: Path) -> None:
    s = report["sections"]
    g = report["gaps_ranked"]
    m = report["menu_format"]
    lines = [
        "# Forge → mtgacoach prompt fidelity report (WP-2.1 Days 3-4)",
        "",
        f"Generated: {report['generated_at']}",
        f"Shim decisions: {report['inputs']['decisions_used']} (from {report['inputs']['decisions_path']})",
        f"Reference corpus: {report['inputs']['real_corpus_n']} real MTGA gate prompts "
        f"({report['inputs']['real_corpus_path']})",
        "",
        "## Verdict",
        "",
        f"- Rendered **{report['render']['rendered']}** of "
        f"{report['inputs']['decisions_used']} decisions through the UNMODIFIED production "
        f"prompt path (`AUTOPILOT_SYSTEM_PROMPT` + `_build_action_prompt`), "
        f"{report['render']['failures']} failures.",
        "- System prompt: the imported production constant "
        f"(matches the stored corpus snapshot: "
        f"{report['render']['system_prompt_matches_stored_real_corpus']}"
        + (
            f" — {report['render']['system_prompt_mismatch_reason']}"
            if report["render"]["system_prompt_mismatch_reason"]
            else ""
        )
        + ").",
        "- Every render passed the gate's own degraded-shape guard.",
        (
            "- The prompts are production-SHAPED but carry the fabrication + gap flags below; "
            "do not build a training corpus from them until the top-ranked gaps are closed."
            if report["fabrication_risks"]["counts"]
            else "- The prompts are production-SHAPED with ZERO fabrications: every rendered "
            "claim traces to shim-native facts. Remaining open gaps (stack/graveyard/"
            "recent-events) match sections the reference gate corpus also leaves empty."
        ),
        "",
        "## Per-section status",
        "",
        "| Section | Status | Real corpus | Forge render | Notes |",
        "|---|---|---|---|---|",
    ]
    for name, v in s.items():
        lines.append(
            f"| {name} | {v['status']} | {v['real_populated']}/{v['real_total']} | "
            f"{v['forge_populated']}/{v['forge_total']} | {v['notes']} |"
        )
    mf = report["mana_fidelity"]
    lines += [
        "",
        "## Mana line fidelity",
        "",
        f"- Rendered `Mana:` equals Forge's own `mana_available` in "
        f"**{mf['exact_match']}/{mf['compared']}** decisions "
        f"(rate {mf['exact_match_rate']}); {mf['rendered_below_forge_estimate']} undercounts, "
        f"{mf['rendered_above_forge_estimate']} overcounts.",
        f"- {mf['note']}",
        "",
        "## Menu format diff",
        "",
        f"- Real entry heads: `{m['real']['entry_heads']}` "
        f"({m['real']['cast_with_ok']}/{m['real']['cast_entries']} Cast rows tagged [OK])",
        f"- Forge entry heads: `{m['forge']['entry_heads']}` "
        f"({m['forge']['cast_with_ok']}/{m['forge']['cast_entries']} Cast rows tagged [OK])",
        f"- Unmapped option rows rendered verbatim: {m['unmapped_rows']} ({m['unmapped_share']} of all rows)",
        f"- Mapping breakdown: `{m['forge']['mapping_breakdown']}`",
    ]
    lines += [f"- {n}" for n in m["diff_notes"]]
    lines += [
        "",
        "## Own-turn derivation",
        "",
        f"`{report['own_turn_derivation']}` — trigger mix `{report['trigger_mix']}`",
        "",
        "## Fabrication risks",
        "",
        f"`{report['fabrication_risks']['counts']}`",
        "",
        report["fabrication_risks"]["detail"],
        "",
        "## Fidelity advantages over the reference corpus",
        "",
    ]
    lines += [f"- {n}" for n in report["fidelity_advantages_over_reference"]]
    lines += [
        "",
        "## Decision-mix caveat",
        "",
        report["decision_mix_note"],
        "",
        report["labels_note"],
        "",
        "## Shim TODO (gaps ranked by real-corpus frequency)",
        "",
    ]
    for gap in g:
        lines.append(
            f"{gap['rank']}. **{gap['gap']}** [{gap.get('status', 'open').upper()}] "
            f"(real-corpus frequency {gap['real_corpus_frequency']}, affects "
            f"{gap['affected_decisions']} shim decisions) — "
            f"missing: {', '.join(gap['shim_fields_missing'])}. {gap['impact']}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    # The production formatter logs per-render mana pools at INFO; that is
    # console noise here, not signal.
    import logging

    logging.disable(logging.INFO)

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--decisions", type=Path, required=True, help="forge-shim decisions.jsonl")
    ap.add_argument("--real-corpus", type=Path, default=DEFAULT_REAL_CORPUS)
    ap.add_argument("--out-prompts", type=Path, default=DEFAULT_OUT_DIR / "forge_rendered_prompts.jsonl")
    ap.add_argument("--report-json", type=Path, default=DEFAULT_OUT_DIR / "forge_fidelity_report.json")
    ap.add_argument("--report-md", type=Path, default=DEFAULT_OUT_DIR / "forge_fidelity_report.md")
    args = ap.parse_args()

    rows = list(_read_jsonl(args.decisions))
    decisions: list[dict] = []
    skipped: list[dict] = []
    for row in rows:
        req = row.get("request") or {}
        if req.get("type") == "decision":
            req = dict(req)
            req["_reply"] = row.get("reply") or {}
            req["_ts"] = row.get("ts")
            decisions.append(req)
        else:
            skipped.append(row)

    real = list(_read_jsonl(args.real_corpus))
    rendered, failures = render_decisions(decisions)

    n = _write_jsonl(args.out_prompts, rendered)
    report = build_report(args.decisions, args.real_corpus, decisions, skipped, real, rendered, failures)
    args.report_json.parent.mkdir(parents=True, exist_ok=True)
    args.report_json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_md(report, args.report_md)

    print(
        json.dumps(
            {
                "rendered": n,
                "render_failures": len(failures),
                "out_prompts": str(args.out_prompts),
                "report_json": str(args.report_json),
                "report_md": str(args.report_md),
            },
            indent=2,
        )
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
