#!/usr/bin/env python3
"""Mine the replay corpus for positions that exercise Reid Duke's *Level One*
in-game decision skills (Wizards of the Coast, "Level One" strategy course).

This is the position-mining half of a strategic gate. It emits CANDIDATES +
EVIDENCE ONLY. It never asserts a correct answer: ``dsv4_pick`` / ``gold_pick``
are carried through verbatim for a human (or a grader) to judge.

Taxonomy
--------
Fifteen in-game skills, taken from Level One's table of contents. Deck
construction, limited/draft, sideboarding, metagame and "play or draw" are
excluded because they are not single-position decisions.

    MANA_SEQUENCING   The Basics of Mana + Sequencing
    TEMPO_CURVE       Tempo (spending mana efficiently, developing on curve)
    TEMPO_VS_CARDS    Tempo & Card Advantage, A Delicate Balance
    ATTACK_BLOCK      Attacking and Blocking
    DAMAGE_RACING     Damage Racing
    THREATS_ANSWERS   Threats and Answers
    PERMISSION_TIMING Permission Spells + When to Cast Your Spells
    CREATURE_LANDS    "Creature" Lands
    SYMMETRIC_EFFECTS Symmetric Effects
    INVESTMENT        Investment (how much to commit to the board)
    AHEAD_BEHIND      Playing From Ahead / Behind + Playing Safe and Scared
    ROLE_ASSIGNMENT   Role Assignment ("Who's the Beatdown?")
    FLEXIBILITY       Flexibility
    INEVITABILITY     Inevitability
    SWEEPERS          Board Sweepers

Reads
-----
* ``tools/eval/data/replay_strategic_groundtruth.jsonl`` — 2,878 replay
  decisions (real menu, board state, human pick).
* ``tools/training/data/dsv4_labels_v1_core.jsonl`` — dsv4 teacher picks plus
  the RENDERED menu (``meta.menu_keys``) the model actually saw. This "core"
  file drops all 134 combat decisions.
* ``tools/training/data/dsv4_labels_v1.jsonl`` — the FULL label file (2,878
  rows). Used only as a fallback for the 134 combat rows the core file omits,
  so ATTACK_BLOCK / DAMAGE_RACING candidates still carry a teacher pick and
  rationale. Everything else comes from the core file.
* ``<scratch>/cards_full.json`` — Scryfall oracle/type/pt/colour/production
  data for every card name in the corpus.

Writes
------
``<scratch>/level_one_candidates.json`` — ``{SKILL: [candidate, ...]}``, ranked
best-first, capped at 40 per skill, at most 1 per (replay, turn) and 6 per
replay (STRICT — no backfill; a short list means the corpus is short).

Shared machinery (imported from ``mine_strategic_tripwires.py``)
---------------------------------------------------------------
* card database loading and the ``Miner`` castability model. The rendered menu
  is **not** castability-filtered — MTGA offers casts the player cannot pay
  for — so a card counts as CASTABLE NOW only when (1) its ``Cast:<grpId>`` key
  is in the rendered menu, (2) ``cmc <= untapped mana`` (lands + mana rocks +
  mana creatures, best effort), and (3) every coloured pip is satisfiable by
  the colours those untapped sources make (hybrid counts as either half,
  unknown production is a wildcard).
* ``strip_forced`` + the <3-real-option filter: a position is dropped when its
  rendered menu has fewer than 3 distinct entries once ``Pass``, ``FloatMana``
  and ``Activate_Mana:*`` are removed. Those are forced/near-forced moves and
  useless as puzzles.

Corpus limitations that shape every detector below
--------------------------------------------------
1. **No power/toughness, no counters, no auras attached.** ``state`` lists
   permanents by name/grp_id/is_tapped only. All combat math here uses PRINTED
   P/T from the oracle DB. Any position where either battlefield holds a
   permanent whose oracle mentions counters, auras, or static pump is flagged
   ``pt_uncertain: true`` — treat its combat numbers as advisory.
2. **No summoning-sickness flag.** A creature that entered this turn is
   indistinguishable from one that has been there for five turns, so "clock"
   figures over-count.
3. **The core label file excludes combat.** ``dsv4_labels_v1_core.jsonl`` has
   2,744 records spanning priority_action / target_choice / search / select_n
   and ZERO for ``attack_declaration`` (54 in corpus) or ``block_assignment``
   (80 in corpus). The full ``dsv4_labels_v1.jsonl`` does have all 134, so
   combat rows are labelled from there (``label_source: "dsv4_full"``) and
   still carry a teacher pick and rationale. Combat rows also use
   ``real_menu_key`` as the menu, because the core file's rendered
   ``meta.menu_keys`` is unavailable for them.
4. **Opponent's hand is invisible** (only ``opp_hand_size``), so "does the
   opponent have the sweeper / the trick" is always an inference from their
   untapped mana and their lands' colours.

Usage
-----
    python3 tools/training/mine_level_one_skills.py [--scratch DIR] [--stats-only]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(_HERE))
GT_PATH = os.path.join(REPO, "tools", "eval", "data", "replay_strategic_groundtruth.jsonl")
LABELS_PATH = os.path.join(REPO, "tools", "training", "data", "dsv4_labels_v1_core.jsonl")
LABELS_FULL_PATH = os.path.join(REPO, "tools", "training", "data", "dsv4_labels_v1.jsonl")

CAP = 40
PER_TURN = 1
PER_REPLAY = 6

SKILLS = [
    "MANA_SEQUENCING", "TEMPO_CURVE", "TEMPO_VS_CARDS", "ATTACK_BLOCK",
    "DAMAGE_RACING", "THREATS_ANSWERS", "PERMISSION_TIMING", "CREATURE_LANDS",
    "SYMMETRIC_EFFECTS", "INVESTMENT", "AHEAD_BEHIND", "ROLE_ASSIGNMENT",
    "FLEXIBILITY", "INEVITABILITY", "SWEEPERS",
]

# ---- reuse the pilot miner's card model / castability / menu filter ---------
_spec = importlib.util.spec_from_file_location(
    "_tripwires", os.path.join(_HERE, "mine_strategic_tripwires.py"))
TW = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(TW)

load_jsonl = TW.load_jsonl
strip_forced = TW.strip_forced
satisfiable = TW.satisfiable
colored_pips = TW.colored_pips
match_any = TW.match_any
removal_match = TW.removal_match
sweeper_match = TW.sweeper_match
board_name_list = TW.board_name_list

PERMANENT_WORDS = TW.PERMANENT_WORDS
STRONG_TRIGGER_RE = TW.STRONG_TRIGGER_RE

# ------------------------------------------------------------------ patterns
PT_RE = re.compile(r"^\s*(-?[\dX*+]+)\s*/\s*(-?[\dX*+]+)")
CREATURE_LAND_RE = re.compile(r"becomes? an? [^.\n]*creature", re.I)
FLASH_RE = re.compile(r"\bflash\b", re.I)
COUNTER_SPELL_RE = re.compile(r"counter target", re.I)
MODAL_RE = re.compile(r"choose one", re.I)
DRAW_RE = re.compile(r"draw (a|one|two|three|four|\d+) cards?", re.I)
UPKEEP_ENGINE_RE = re.compile(
    r"at the beginning of your (upkeep|end step|draw step)", re.I)
PT_UNCERTAIN_RE = re.compile(
    r"(\+1/\+1 counter|\-1/\-1 counter|counters? on|enchant creature|"
    r"gets \+\d|creatures you control get \+|equipped creature gets)", re.I)
# "each player" / "all players" are unambiguously two-sided. "each creature" /
# "all creatures" are NOT symmetric when the clause qualifies them with "you
# control" or "an opponent controls" — the pilot run showed a bare "each
# creature" match flags one-sided pump/draw effects ("draw a card for each
# creature you control with a +1/+1 counter"). Those are filtered in
# ``symmetric_sentence`` below.
SYMMETRIC_BOTH_RE = [
    re.compile(r"each player", re.I),
    re.compile(r"all players", re.I),
]
SYMMETRIC_BOARD_RE = [
    re.compile(r"each creature", re.I),
    re.compile(r"all creatures", re.I),
]
SYMMETRIC_ONESIDED_RE = re.compile(r"each opponent", re.I)
SIDED_QUALIFIER_RE = re.compile(r"(you control|an opponent controls|your opponents control)", re.I)
EVASION_RE = re.compile(r"\b(flying|menace|trample|unblockable|can't be blocked|"
                        r"shadow|fear|intimidate|skulk|horsemanship)\b", re.I)
DETERRENT_RE = re.compile(r"\b(deathtouch|first strike|double strike|vigilance|"
                          r"reach|lifelink|indestructible)\b", re.I)
WRATH_COLORS = set("WB")  # crude: most sweepers in Standard-ish pools are W or B


# --------------------------------------------------------------------- utils
def printed_pt(cardinfo):
    """(power, toughness) as ints from the PRINTED type line, or (None, None).

    ``*``/``X`` power (e.g. Tarmogoyf-likes) returns None — such creatures are
    excluded from clock math rather than guessed at.
    """
    m = PT_RE.match((cardinfo or {}).get("pt") or "")
    if not m:
        return None, None
    try:
        return int(m.group(1)), int(m.group(2))
    except ValueError:
        return None, None


def ceil_div(a, b):
    return math.ceil(a / b) if b else None


def symmetric_sentence(text):
    """First oracle sentence that genuinely hits BOTH sides, or None.

    ``(sentence, one_sided)``: ``one_sided`` marks "each opponent" clauses,
    which are recorded but demoted (in a two-player game they are one-sided).
    """
    for s in re.split(r"[.\n]", text or ""):
        if match_any(SYMMETRIC_BOTH_RE, s):
            return s.strip()[:160], False
        if match_any(SYMMETRIC_BOARD_RE, s) and not SIDED_QUALIFIER_RE.search(s):
            return s.strip()[:160], False
        if SYMMETRIC_ONESIDED_RE.search(s):
            return s.strip()[:160], True
    return None, False


class Ctx:
    """Everything a detector needs about one position, computed once."""

    def __init__(self, rec, lab, menu_keys, M, grp_name, label_source):
        self.rec = rec
        self.lab = lab or {}
        self.M = M
        self.grp_name = grp_name
        self.label_source = label_source
        self.menu_keys = menu_keys
        self.key_set = set(menu_keys)
        self.real_entries = strip_forced(menu_keys)
        self.pass_available = "Pass" in menu_keys

        st = rec["state"]
        self.st = st
        self.hand = st["hand"]
        self.turn = rec.get("turn_number")
        self.phase = rec.get("phase")
        self.step = rec.get("step") or ""
        self.kind = rec.get("decision_kind")
        self.own = bool(rec.get("is_own_turn"))
        self.life_you = st["life"]["you"]
        self.life_opp = st["life"]["opp"]
        self.life_diff = self.life_you - self.life_opp

        self.lands = st.get("lands_in_play") or []
        self.lands_tapped = int(st.get("lands_tapped") or 0)
        self.avail_lands = max(0, len(self.lands) - self.lands_tapped)
        self.total_mana, self.avail_colors, self.sources = M.mana_pool(st)

        self.creatures = st.get("creatures_in_play") or []
        self.others = st.get("other_permanents") or []
        self.opp_creatures = st.get("opp_creatures_in_play") or []
        self.opp_others = st.get("opp_other_permanents") or []
        self.opp_lands = st.get("opp_lands_in_play") or []
        self.n_cr = len(self.creatures)
        self.n_opp_cr = len(self.opp_creatures)
        self.board_diff = self.n_cr - self.n_opp_cr

        self.opp_untapped_lands = len([l for l in self.opp_lands if not l.get("is_tapped")])
        oc = set()
        for l in self.opp_lands:
            if l.get("is_tapped"):
                continue
            p = M.card(l.get("name", "")).get("produces") or ""
            oc |= set(p) if p else {"*"}
        self.opp_colors = oc

        # ---- menu decomposition
        self.by_key = {}
        for a in rec["real_menu"]:
            at = (a.get("action_type") or "").replace("ActionType_", "")
            k = f"{at}:{a['grp_id']}" if a.get("grp_id") is not None else at
            self.by_key.setdefault(k, a)

        self.castable = {}       # name -> info, castable right now
        self.unaffordable = []   # shown in menu, cannot pay
        self.land_opts = []
        self.activations = []
        for k in self.real_entries:
            a = self.entry(k)
            if not a or not a.get("name"):
                continue
            nm = a["name"]
            if k.startswith("Cast:"):
                cmc = a.get("mana_value")
                if cmc is None:
                    cmc = M.card(nm).get("cmc")
                cost = a.get("mana_cost") or M.card(nm).get("cost", "")
                info = {"key": k, "name": nm, "cmc": cmc, "mana_cost": cost,
                        "type_line": a.get("type_line") or M.card(nm).get("type", "")}
                if M.castable(nm, cmc, cost, self.total_mana, self.avail_colors,
                              self.key_set, a.get("grp_id")):
                    self.castable.setdefault(nm, info)
                else:
                    self.unaffordable.append(info)
            elif k.startswith("Play:") or k.startswith("PlayMDFC:"):
                self.land_opts.append({"key": k, "name": nm})
            elif k.startswith("Activate:"):
                self.activations.append({"key": k, "name": nm,
                                         "grp_id": a.get("grp_id"),
                                         "type_line": a.get("type_line") or ""})

        # ---- printed-stat board summaries (see limitation 1)
        self.your_bodies = [self._body(c) for c in self.creatures]
        self.opp_bodies = [self._body(c) for c in self.opp_creatures]
        self.your_power = sum(b["power"] or 0 for b in self.your_bodies)
        self.opp_power = sum(b["power"] or 0 for b in self.opp_bodies)
        self.pt_uncertain = self._pt_uncertain()

    # -- helpers -----------------------------------------------------------
    def _body(self, c):
        ci = self.M.card(c.get("name", ""))
        p, t = printed_pt(ci)
        txt = ci.get("oracle", "")
        return {"name": c.get("name"), "power": p, "toughness": t,
                "tapped": bool(c.get("is_tapped")),
                "evasion": bool(EVASION_RE.search(txt)),
                "deterrent": bool(DETERRENT_RE.search(txt))}

    def _pt_uncertain(self):
        """True when printed P/T probably lies: counters/auras/anthems around."""
        for p in (self.creatures + self.others + self.opp_creatures + self.opp_others):
            ci = self.M.card(p.get("name", ""))
            tl = (ci.get("type") or "")
            if "Aura" in tl or "Equipment" in tl:
                return True
            if PT_UNCERTAIN_RE.search(ci.get("oracle", "")):
                return True
        return False

    def entry(self, k):
        a = self.by_key.get(k)
        if a:
            return a
        if ":" in k:
            gid = k.split(":", 1)[1]
            if gid.isdigit() and int(gid) in self.grp_name:
                nm = self.grp_name[int(gid)]
                ci = self.M.card(nm)
                return {"name": nm, "grp_id": int(gid), "type_line": ci.get("type", ""),
                        "mana_value": ci.get("cmc"), "mana_cost": ci.get("cost", "")}
        return None

    def key_label(self, k):
        if not k or ":" not in k:
            return k
        verb, gid = k.split(":", 1)
        a = self.entry(k)
        nm = (a or {}).get("name") or (self.grp_name.get(int(gid)) if gid.isdigit() else None)
        return f"{verb} {nm}" if nm else k

    def oracle(self, name):
        return self.M.oracle(name)

    def hand_castable(self, h):
        return self.M.castable(h.get("name"), h.get("cmc"), h.get("mana_cost"),
                               self.total_mana, self.avail_colors, self.key_set,
                               h.get("grp_id"))

    def hand_cards(self):
        seen = set()
        for h in self.hand:
            nm = h.get("name")
            if nm in seen:
                continue
            seen.add(nm)
            yield h

    def mana_ev(self):
        return {"available_mana": self.avail_lands,
                "available_mana_total": self.total_mana,
                "available_colors": "".join(sorted(self.avail_colors)),
                "untapped_sources": self.sources}

    def board_ev(self):
        return {"life": {"you": self.life_you, "opp": self.life_opp},
                "your_creatures": self.your_bodies,
                "your_other_permanents": board_name_list(self.others),
                "opp_creatures": self.opp_bodies,
                "opp_other_permanents": board_name_list(self.opp_others),
                "opp_untapped_lands": self.opp_untapped_lands,
                "opp_colors": "".join(sorted(self.opp_colors)),
                "opp_hand_size": self.st.get("opp_hand_size"),
                "pt_uncertain": self.pt_uncertain}


# =========================================================================
#  D E T E C T O R S
#  Each returns (qualifies: bool, evidence: dict). ``evidence["score"]`` is
#  the ranking key (higher = better isolates the skill); the driver pops it.
# =========================================================================

def det_mana_sequencing(C):
    """Level One — "The Basics of Mana" + "Sequencing".

    Encodes: which land do I play, and in what order do I play land vs spell,
    so that my colours and my curve line up with the cards I actually hold.

    Qualifies when a land play is offered AND either
      (a) >=2 DISTINCT land names are playable (a real "which land" choice), or
      (b) exactly one land is playable but the hand holds a card that is
          currently colour-blocked, i.e. this land drop is the colour decision,
          AND at least one spell is castable now (so land-vs-spell order is a
          live question).

    Limitations: cost reductions and alternative costs are ignored, so
    "colour-blocked" is mildly over-reported; the corpus does not say which
    land the player would draw next, so "best land" is a one-turn lookahead
    only.

    Ranking: +5 the candidate lands produce DIFFERENT colour sets; +2 per
    distinct land name (cap 6); +2 if some hand card is specifically
    colour-blocked; +1 per blocked/short hand card (cap 4); +2 if the lands
    differ on enters-tapped; +2 if exactly one land maximises next-turn
    castability (a uniquely best answer exists); -3 if every candidate land
    makes the same colours; -0.5 per turn past turn 6.
    """
    M = C.M
    names = sorted({l["name"] for l in C.land_opts})
    if not names:
        return False, {}
    blocked, short = [], []
    for h in C.hand_cards():
        tl = (h.get("type_line") or "").lower()
        if "land" in tl:
            continue
        if not colored_pips(h.get("mana_cost", "")):
            continue
        if C.hand_castable(h):
            continue
        item = {"name": h["name"], "mana_cost": h.get("mana_cost"), "cmc": h.get("cmc")}
        if not satisfiable(h.get("mana_cost", ""), C.avail_colors):
            item["reason"] = "colour_blocked"
            blocked.append(item)
        else:
            item["reason"] = "too_expensive"
            short.append(item)
    if len(names) < 2 and not (blocked and C.castable):
        return False, {}

    lo = []
    for nm in names:
        ci = M.card(nm)
        lo.append({"name": nm, "produces": ci.get("produces") or "*",
                   "enters_tapped": bool(ci.get("enters_tapped")),
                   "type": ci.get("type", "")})
    base_colors = set()
    for l in C.lands:
        p = M.card(l.get("name", "")).get("produces") or ""
        base_colors |= set(p) if p else {"*"}
    next_count = len(C.lands) + 1
    enables = {}
    for l in lo:
        prod = set(l["produces"]) if l["produces"] != "*" else {"*"}
        avail = base_colors | prod
        unlocked = [h["name"] for h in C.hand_cards()
                    if "land" not in (h.get("type_line") or "").lower()
                    and (h.get("cmc") or 0) <= next_count
                    and satisfiable(h.get("mana_cost", ""), avail)]
        enables[l["name"]] = {"n_castable_next_turn": len(unlocked), "cards": unlocked}
    best = max(v["n_castable_next_turn"] for v in enables.values())
    best_lands = [k for k, v in enables.items() if v["n_castable_next_turn"] == best]
    produce_sets = {l["produces"] for l in lo}
    tapped_sets = {l["enters_tapped"] for l in lo}
    score = (5 * (len(produce_sets) > 1)
             + 2 * min(len(names), 3)
             + 2 * bool(blocked)
             + min(len(blocked) + len(short), 4)
             + 2 * (len(tapped_sets) > 1)
             + 2 * (len(best_lands) == 1 and len(names) > 1)
             - (3 if len(produce_sets) == 1 else 0)
             - 0.5 * max(0, (C.turn or 0) - 6))
    ev = dict(C.mana_ev(), **{
        "branch": "which_land" if len(names) >= 2 else "land_vs_spell_order",
        "lands_in_play": board_name_list(C.lands),
        "land_options": lo,
        "hand_colour_blocked": blocked,
        "hand_too_expensive": short,
        "enables_next_turn": enables,
        "best_land_next_turn": best_lands,
        "castable_now": sorted(C.castable),
        "score": score,
    })
    ev.update(C.board_ev())
    return True, ev


def det_tempo_curve(C):
    """Level One — "Tempo": use all your mana every turn, develop on curve.

    Qualifies on your own Main 1, turns 1-6, with >=1 untapped land, when the
    menu offers a castable-now PERMANENT whose cmc exactly equals your
    available mana (a perfect curve play) and at least one alternative exists
    (a cheaper spell, another spell, or Pass).

    Limitations: "mana exactly spent" uses untapped lands only for the
    on-curve test (rocks are counted in total mana, which can make a play look
    off-curve); cost reduction is ignored; a card that is better held is not
    distinguishable from one that is better cast.

    Ranking: +3 per on-curve permanent (cap 2); +2 per strictly cheaper
    castable alternative (cap 3); +1 if Pass is offered; -3 if a land play is
    also in the menu (mixes in a land decision); -2 if removal/a sweeper is
    also castable (that is really a THREATS_ANSWERS position); -0.6*|turn-3|.
    """
    if not (C.own and C.phase == "Phase_Main1" and C.turn and 1 <= C.turn <= 6
            and C.avail_lands >= 1):
        return False, {}
    on_curve, cheaper, other = [], [], []
    for c in C.castable.values():
        tl = (c["type_line"] or "").lower()
        is_perm = (any(w in tl for w in PERMANENT_WORDS)
                   and "instant" not in tl and "sorcery" not in tl)
        if c["cmc"] is None:
            continue
        if is_perm and float(c["cmc"]) == float(C.avail_lands):
            on_curve.append(c)
        elif float(c["cmc"]) < float(C.avail_lands):
            cheaper.append(c)
        else:
            other.append(c)
    if not on_curve:
        return False, {}
    if not (len(C.castable) - len(on_curve) >= 1 or C.pass_available):
        return False, {}
    interaction = [c for c in C.castable.values()
                   if removal_match(C.oracle(c["name"])) or sweeper_match(C.oracle(c["name"]))]
    score = (3 * min(len(on_curve), 2)
             + 2 * min(len(cheaper), 3)
             + (1 if C.pass_available else 0)
             - (3 if C.land_opts else 0)
             - (2 if interaction else 0)
             - 0.6 * abs(C.turn - 3))
    ev = dict(C.mana_ev(), **{
        "on_curve_options": on_curve,
        "cheaper_options": cheaper,
        "other_castable_options": other,
        "shown_but_unaffordable": C.unaffordable,
        "pass_available": C.pass_available,
        "land_play_still_in_menu": sorted({l["name"] for l in C.land_opts}),
        "interaction_also_castable": [c["name"] for c in interaction],
        "lands_played_this_turn": C.st.get("lands_played_this_turn"),
        "score": score,
    })
    ev.update(C.board_ev())
    return True, ev


def det_tempo_vs_cards(C):
    """Level One — "Tempo and Card Advantage, A Delicate Balance".

    The decision is: spend this turn's mana on the board (tempo) or on cards
    (card advantage)? Qualifies when BOTH are castable in the same window —
    a card-advantage spell (draws cards / returns cards / makes an extra card)
    AND a board-affecting spell (a creature, or removal) — and the position has
    a pressure signal: the opponent has >=1 creature, or you are at <=15 life,
    or you are behind on board.

    Limitations: "card advantage" is regex on oracle text, so a creature that
    happens to say "draw a card" on ETB counts as both sides of the tension
    (that is recorded — see ``both_roles``); the corpus cannot show what the
    opponent holds, so the true cost of tapping out is unknown.

    Ranking: +3 if the two options cost the same or the draw spell costs more
    (a genuine either/or for this turn's mana); +2 per opposing creature
    (cap 6); +3 if you are behind on board; +2 if life <= 12; +2 if the
    card-advantage spell is a pure spell (instant/sorcery, no body);
    -2 if the draw effect is attached to a creature (the tension is fake).
    """
    draw, board = [], []
    for h in C.hand_cards():
        if not C.hand_castable(h):
            continue
        txt = C.oracle(h["name"])
        tl = (h.get("type_line") or "")
        item = {"name": h["name"], "cmc": h.get("cmc"), "mana_cost": h.get("mana_cost"),
                "type_line": tl}
        if DRAW_RE.search(txt):
            m = DRAW_RE.search(txt)
            item = dict(item, matched_text=m.group(0))
            draw.append(item)
        if "Creature" in tl or removal_match(txt):
            board.append(item)
    both = {d["name"] for d in draw} & {b["name"] for b in board}
    pure_draw = [d for d in draw if d["name"] not in both]
    if not draw or not board:
        return False, {}
    if not (C.n_opp_cr >= 1 or C.life_you <= 15 or C.board_diff < 0):
        return False, {}
    # is it really the same mana? cheapest board play vs cheapest draw spell
    d_cost = min((d["cmc"] or 0) for d in draw)
    b_cost = min((b["cmc"] or 0) for b in board)
    competes = d_cost + b_cost > C.total_mana  # can't do both this turn
    score = (3 * bool(competes)
             + min(2 * C.n_opp_cr, 6)
             + (3 if C.board_diff < 0 else 0)
             + (2 if C.life_you <= 12 else 0)
             + (2 if pure_draw else 0)
             - (2 if not pure_draw else 0))
    ev = dict(C.mana_ev(), **{
        "card_advantage_options": draw,
        "board_options": board,
        "both_roles": sorted(both),
        "cannot_do_both_this_turn": competes,
        "cheapest_draw_cmc": d_cost,
        "cheapest_board_cmc": b_cost,
        "board_diff": C.board_diff,
        "score": score,
    })
    ev.update(C.board_ev())
    return True, ev


def det_attack_block(C):
    """Level One — "Attacking and Blocking".

    Qualifies on ``attack_declaration`` / ``block_assignment`` decisions that
    survive the <3-real-option filter, i.e. the player had a genuine choice of
    which creatures to send or how to block, not a binary all-in/decline.

    Limitations (important): the corpus has NO power/toughness, NO counters and
    NO attached auras, so all combat math here is PRINTED stats. Positions
    where either battlefield holds a counters/aura/anthem source are flagged
    ``pt_uncertain: true`` — their trade math may be wrong. Summoning sickness
    is not recorded, so a creature that could not have attacked may appear in
    ``your_creatures``. Blocking restrictions (menace, "must be blocked") are
    only visible through ``decision_context.has_restrictions``.

    Also: dsv4 never labelled these decisions, so ``dsv4_pick`` is null. The
    human pick is present.

    Ranking: +2 per distinct real menu entry beyond the first (cap 8); +2 if
    both sides have >=2 creatures (a real trade matrix); +2 if some attacker
    has evasion or some blocker has deathtouch/first strike (the choice has an
    edge); +2 if the block/attack is not a blowout on printed stats (i.e. both
    sides have a creature that survives); -3 if ``pt_uncertain`` (math is
    unreliable); +1 if life <= 10 for either player (the choice is load-bearing).
    """
    if C.kind not in ("attack_declaration", "block_assignment"):
        return False, {}
    n_opts = len(C.real_entries)
    dc = C.rec.get("decision_context") or {}
    atk = [b for b in (C.your_bodies if C.kind == "attack_declaration" else C.opp_bodies)]
    dfn = [b for b in (C.opp_bodies if C.kind == "attack_declaration" else C.your_bodies)]
    edge = (any(b["evasion"] for b in atk) or any(b["deterrent"] for b in dfn))
    survivors = (any((b["toughness"] or 0) > max([x["power"] or 0 for x in dfn] or [0])
                     for b in atk)
                 and any((b["toughness"] or 0) > max([x["power"] or 0 for x in atk] or [0])
                         for b in dfn))
    score = (min(2 * (n_opts - 1), 8)
             + (2 if (len(atk) >= 2 and len(dfn) >= 2) else 0)
             + (2 if edge else 0)
             + (2 if survivors else 0)
             + (1 if min(C.life_you, C.life_opp) <= 10 else 0)
             - (3 if C.pt_uncertain else 0))
    ev = dict(C.board_ev(), **{
        "combat_kind": C.kind,
        "n_menu_options": n_opts,
        "menu_text": [(C.by_key.get(k) or {}).get("menu_text") for k in C.real_entries],
        "n_candidate_blockers": dc.get("n_candidate_blockers"),
        "n_qualified_attackers": dc.get("n_qualified_attackers"),
        "has_restrictions": dc.get("has_restrictions"),
        "menu_rows_collapsed": dc.get("menu_rows_collapsed"),
        "attacker_side_printed_power": sum(b["power"] or 0 for b in atk),
        "defender_side_printed_power": sum(b["power"] or 0 for b in dfn),
        "evasion_or_deterrent_present": edge,
        "score": score,
    })
    return True, ev


def det_damage_racing(C):
    """Level One — "Damage Racing": when to race instead of blocking.

    Qualifies on a combat decision where BOTH players have a clock on the
    board (printed power > 0 on each side) and the race is live: each side can
    kill the other within 6 turns at the current printed rate, and the
    turns-to-kill are within 2 of each other (a close race is exactly when the
    block-vs-race decision is hard). Positions where one side has no clock are
    not races and are left to ATTACK_BLOCK.

    Limitations: printed power only, no summoning sickness, no evasion
    modelling beyond a flag, no life gain / burn from hand. Treat
    ``turns_to_kill`` as an order-of-magnitude signal, not a solved race.

    Ranking: +6 minus 2*|turns_to_kill_you - turns_to_kill_opp| (closest races
    rank first); +3 if the faster clock is <=3 turns (urgent); +2 if the
    attacker side has evasion (racing is actually possible); +2 per real menu
    entry beyond the first (cap 6); -3 if ``pt_uncertain``.
    """
    if C.kind not in ("attack_declaration", "block_assignment"):
        return False, {}
    if C.your_power <= 0 or C.opp_power <= 0:
        return False, {}
    t_opp_dies = ceil_div(C.life_opp, C.your_power)
    t_you_die = ceil_div(C.life_you, C.opp_power)
    if not t_opp_dies or not t_you_die:
        return False, {}
    if t_opp_dies > 6 and t_you_die > 6:
        return False, {}
    if abs(t_opp_dies - t_you_die) > 2:
        return False, {}
    edge = any(b["evasion"] for b in C.your_bodies + C.opp_bodies)
    score = (6 - 2 * abs(t_opp_dies - t_you_die)
             + (3 if min(t_opp_dies, t_you_die) <= 3 else 0)
             + (2 if edge else 0)
             + min(2 * (len(C.real_entries) - 1), 6)
             - (3 if C.pt_uncertain else 0))
    ev = dict(C.board_ev(), **{
        "combat_kind": C.kind,
        "your_printed_power": C.your_power,
        "opp_printed_power": C.opp_power,
        "turns_to_kill_opp": t_opp_dies,
        "turns_until_you_die": t_you_die,
        "race_margin_turns": t_opp_dies - t_you_die,
        "evasion_present": edge,
        "n_menu_options": len(C.real_entries),
        "menu_text": [(C.by_key.get(k) or {}).get("menu_text") for k in C.real_entries],
        "score": score,
    })
    return True, ev


def det_threats_answers(C):
    """Level One — "Threats and Answers": spend the right answer on the right
    threat, and know when to hold it.

    Qualifies when a removal/sweeper card in HAND is castable right now and the
    opponent controls something it can legally point at (>=1 creature for spot
    creature removal, >=2 for a sweeper, a nonland noncreature permanent for
    artifact/enchantment removal).

    Limitations: "which threat is the biggest" is judged on printed P/T; the
    opponent's hand (a bigger threat next turn) is invisible, which is exactly
    the information Level One says should make you hesitate — the grader has
    to accept that both "use it" and "hold it" can be defensible here.

    Ranking: +4 for a sweeper; +1 per opposing creature (cap 4); +2 per
    distinct castable answer (cap 4); +2 if a real non-answer line also exists
    (the "answer now vs develop" tension); -2 if answering is the only thing
    available; +2 if the opponent's board contains a body with printed power
    >= 4 AND one of the castable answers can actually point at a creature
    (an artifact/enchantment-only answer facing a 7/7 is not a
    threats-and-answers decision, so it gets no bonus); -2 when NO castable
    answer hits creatures.
    """
    answers = TW._interaction_cards(C.hand, C.key_set, C.M, C.total_mana, C.avail_colors)
    if not answers:
        return False, {}
    qualifying = []
    for a in answers:
        if a["is_sweeper"]:
            if C.n_opp_cr >= 2:
                qualifying.append(a)
            continue
        if a["hits_creature"] and C.n_opp_cr >= 1:
            qualifying.append(a)
        elif a["hits_noncreature"] and C.opp_others and C.n_opp_cr >= 1:
            qualifying.append(a)
    if not qualifying:
        return False, {}
    keys = {a["key"] for a in qualifying}
    other_lines = [k for k in C.real_entries if k not in keys]
    is_sweeper = any(a["is_sweeper"] for a in qualifying)
    big = [b for b in C.opp_bodies if (b["power"] or 0) >= 4]
    hits_creatures = any(a["is_sweeper"] or a["hits_creature"] for a in qualifying)
    score = (4 * is_sweeper
             + min(C.n_opp_cr, 4)
             + min(2 * len(qualifying), 4)
             + (2 if other_lines else -2)
             + (2 if (big and hits_creatures) else 0)
             - (0 if hits_creatures else 2))
    ev = dict(C.mana_ev(), **{
        "answers_castable": qualifying,
        "answers_can_hit_creatures": hits_creatures,
        "is_sweeper": is_sweeper,
        "biggest_opp_threats": sorted(big, key=lambda b: -(b["power"] or 0))[:3],
        "other_castable_lines": [c["name"] for c in C.castable.values() if c["key"] not in keys],
        "other_menu_lines": [C.key_label(k) for k in other_lines],
        "score": score,
    })
    ev.update(C.board_ev())
    return True, ev


def det_permission_timing(C):
    """Level One — "Permission Spells" + "When to Cast Your Spells".

    Encodes the instant-speed discipline: hold mana, act in the opponent's
    window, cast at the last responsible moment. Qualifies when the priority
    window is NOT your own main phase — the opponent's turn, or an end step,
    or a combat step — and the hand holds an instant (or a flash permanent, or
    a counterspell) that is castable with the mana currently untapped.

    Limitations: the corpus does not record what is on the stack in most
    windows (``state.stack`` is usually empty), so "counter this specific
    spell" cannot be isolated from "flash in a blocker"; and holding up mana
    for a card you did NOT get to cast leaves no trace at all, so the corpus
    can only show the windows where the option existed.

    Ranking: +6 if it is the opponent's turn; +3 if it is an end step; +4 if a
    true counterspell is castable; +2 if something is on the stack; +2 per
    distinct castable instant/flash card (cap 4); +2 if the menu also offers a
    non-instant line (a real "now or later" tension); -0.4*|turn-6|.
    """
    if C.own and C.phase in ("Phase_Main1", "Phase_Main2"):
        return False, {}
    inst = []
    for h in C.hand_cards():
        tl = (h.get("type_line") or "")
        txt = C.oracle(h["name"])
        kind = None
        if "Instant" in tl:
            kind = "instant"
        elif FLASH_RE.search(txt):
            kind = "flash"
        if COUNTER_SPELL_RE.search(txt):
            kind = "counterspell"
        if not kind:
            continue
        if not C.hand_castable(h):
            continue
        inst.append({"name": h["name"], "cmc": h.get("cmc"),
                     "mana_cost": h.get("mana_cost"), "kind": kind,
                     "type_line": tl})
    if not inst:
        return False, {}
    is_end_step = C.step in ("Step_End", "Step_Cleanup") or C.phase == "Phase_Ending"
    has_counter = any(i["kind"] == "counterspell" for i in inst)
    stack = C.st.get("stack") or []
    non_instant = [k for k in C.real_entries
                   if k.startswith("Cast:")
                   and "Instant" not in ((C.entry(k) or {}).get("type_line") or "")]
    score = ((6 if not C.own else 0)
             + (3 if is_end_step else 0)
             + (4 if has_counter else 0)
             + (2 if stack else 0)
             + min(2 * len(inst), 4)
             + (2 if non_instant else 0)
             - 0.4 * abs((C.turn or 6) - 6))
    ev = dict(C.mana_ev(), **{
        "window": ("opponent_turn" if not C.own else "own_turn") + "/" + (C.step or C.phase),
        "is_end_step": is_end_step,
        "instants_castable": inst,
        "counterspell_available": has_counter,
        "stack": stack,
        "other_menu_lines": [C.key_label(k) for k in C.real_entries],
        "mana_held_after_casting_cheapest": C.total_mana - min(i["cmc"] or 0 for i in inst),
        "score": score,
    })
    ev.update(C.board_ev())
    return True, ev


def det_creature_lands(C):
    """Level One — ""Creature" Lands" (manlands).

    Qualifies when you control a land whose oracle text turns it into a
    creature ("becomes an X/X creature") AND its activation is actually in the
    rendered menu, so the model was offered the animate.

    Two sub-kinds are distinguished, because this corpus is dominated by the
    second and they are not the same lesson:
      * ``self_animating`` — a classic manland: "THIS land becomes a 3/3
        creature" (Soulstone Sanctuary, Great Hall of the Biblioplex).
      * ``animates_another_land`` — Earthbend-style ("target land you control
        becomes a 0/0 creature with haste", e.g. Ba Sing Se). Related, but the
        Level One manland lesson (a threat that dodges sorcery-speed removal
        and sweepers) applies only loosely.
    Self-animating hits are ranked far above the other kind.

    Limitations: the corpus does not say whether the land is already animated,
    nor whether the animation is being used to attack, block, or dodge a
    sweeper; activation cost is read from oracle text only, so "can I afford
    it and still cast my spell" is inferred from total untapped mana.

    Ranking: +8 if any hit is self-animating (a real manland); +4 if it is
    your own turn (attack decision) or +5 if it is the opponent's turn
    (block/ambush decision, the sharper manland test); +2 if the opponent has
    >=1 creature; +2 if you also have a castable spell (the "spend mana on the
    land or on the spell" tension); +2 if you would still have mana left after
    animating; +1 per extra distinct manland (cap 2); -0.3*|turn-8| (manlands
    matter in the mid/late game).
    """
    hits = []
    for l in C.lands:
        nm = l.get("name")
        txt = C.oracle(nm)
        m = CREATURE_LAND_RE.search(txt or "")
        if not m:
            continue
        gid = l.get("grp_id")
        if f"Activate:{gid}" not in C.key_set:
            continue
        sent = m.group(0)
        self_anim = bool(re.search(r"this land becomes", txt or "", re.I))
        hits.append({"name": nm, "grp_id": gid, "is_tapped": bool(l.get("is_tapped")),
                     "matched_text": sent, "oracle": txt,
                     "kind": "self_animating" if self_anim else "animates_another_land"})
    if not hits:
        return False, {}
    self_animating = any(h["kind"] == "self_animating" for h in hits)
    score = ((8 if self_animating else 0)
             + (5 if not C.own else 4)
             + (2 if C.n_opp_cr >= 1 else 0)
             + (2 if C.castable else 0)
             + (2 if C.total_mana >= 3 else 0)
             + min(len({h["name"] for h in hits}) - 1, 2)
             - 0.3 * abs((C.turn or 8) - 8))
    ev = dict(C.mana_ev(), **{
        "creature_lands": hits,
        "has_self_animating_manland": self_animating,
        "own_turn": C.own,
        "phase_step": f"{C.phase}/{C.step}",
        "castable_spells_competing_for_mana": sorted(C.castable),
        "other_menu_lines": [C.key_label(k) for k in C.real_entries],
        "score": score,
    })
    ev.update(C.board_ev())
    return True, ev


def det_symmetric_effects(C):
    """Level One — "Symmetric Effects": a card that hits both players is only
    good if the board is asymmetric in YOUR favour.

    Qualifies when a castable-now card's oracle text affects "each player" /
    "all players", or "each creature" / "all creatures" in a clause NOT
    qualified by "you control" / "an opponent controls". That qualifier check
    matters: without it "draw a card for each creature you control with a
    +1/+1 counter on it" is mined as a symmetric effect, which it is not. The
    evidence records the board asymmetry (your creature count and printed
    power vs theirs, hand sizes, life) so a grader can see who benefits.

    Limitations: "each opponent" effects are one-sided in a two-player game and
    are recorded but scored lower; the regex cannot tell an upside symmetric
    card ("each player draws") from a downside one ("each creature gets -1/-1")
    — the matched sentence is recorded verbatim so the grader can.

    Ranking: +2 per point of |creature-count asymmetry| (cap 6); +3 if the
    asymmetry is >=2 in either direction (the effect clearly favours someone);
    +2 if hand sizes differ by >=2 (matters for "each player draws/discards");
    +2 if a real alternative line exists; -3 if the boards are symmetric
    (equal creature counts AND equal life), because then the card is a
    coin-flip and teaches nothing.
    """
    hits = []
    for c in C.castable.values():
        sent, one_sided = symmetric_sentence(C.oracle(c["name"]))
        if sent:
            hits.append(dict(c, matched_text=sent, one_sided=one_sided))
    if not hits:
        return False, {}
    asym = abs(C.board_diff)
    hand_gap = abs((C.st.get("hand_size") or len(C.hand)) - (C.st.get("opp_hand_size") or 0))
    others = [k for k in C.real_entries if k not in {h["key"] for h in hits}]
    symmetric_board = (C.board_diff == 0 and C.life_diff == 0)
    score = (min(2 * asym, 6)
             + (3 if asym >= 2 else 0)
             + (2 if hand_gap >= 2 else 0)
             + (2 if others else 0)
             - (3 if symmetric_board else 0)
             - (2 if all(h["one_sided"] for h in hits) else 0))
    ev = dict(C.mana_ev(), **{
        "symmetric_cards_castable": hits,
        "board_asymmetry": {
            "your_creatures": C.n_cr, "opp_creatures": C.n_opp_cr,
            "creature_diff": C.board_diff,
            "your_printed_power": C.your_power, "opp_printed_power": C.opp_power,
            "your_hand_size": C.st.get("hand_size"), "opp_hand_size": C.st.get("opp_hand_size"),
            "life_diff": C.life_diff,
        },
        "other_menu_lines": [C.key_label(k) for k in others],
        "score": score,
    })
    ev.update(C.board_ev())
    return True, ev


def det_investment(C):
    """Level One — "Investment": how much do you commit to a board that a
    sweeper could punish?

    Qualifies when you already control >=3 creatures AND the menu offers
    another castable creature/permanent — i.e. the overextension question is
    live. Evidence records the opponent's untapped mana and the colours their
    untapped lands can make, as a best-effort read on whether they could have
    a sweeper.

    Limitations: this is the weakest inference in the file. The opponent's hand
    is invisible; "could they have a sweeper" is approximated by
    (untapped mana >= 4) AND (their untapped lands make W or B), which is a
    format-level stereotype, not knowledge. Also, this corpus contains almost
    no sweepers at all (see SWEEPERS), so the punishment these positions fear
    may not exist in the decks being played.

    Ranking: +2 per creature you already control beyond the second (cap 8);
    +4 if the opponent has >=4 untapped mana; +2 if their untapped colours
    include W or B; +2 if you are already ahead on board by >=2 (adding more
    is pure greed); +2 if a non-committing line exists in the menu (holding
    the card is actually an option); -2 if you have no other line at all.
    """
    if C.n_cr < 3:
        return False, {}
    adds = []
    for c in C.castable.values():
        tl = (c["type_line"] or "").lower()
        if "creature" in tl or any(w in tl for w in ("artifact", "enchantment", "planeswalker")):
            if "instant" in tl or "sorcery" in tl:
                continue
            adds.append(c)
    if not adds:
        return False, {}
    opp_sweeper_colors = sorted(C.opp_colors & WRATH_COLORS)
    keys = {a["key"] for a in adds}
    others = [k for k in C.real_entries if k not in keys]
    score = (min(2 * (C.n_cr - 2), 8)
             + (4 if C.opp_untapped_lands >= 4 else 0)
             + (2 if opp_sweeper_colors else 0)
             + (2 if C.board_diff >= 2 else 0)
             + (2 if others else -2))
    ev = dict(C.mana_ev(), **{
        "your_creature_count": C.n_cr,
        "additional_permanents_castable": adds,
        "opp_untapped_lands": C.opp_untapped_lands,
        "opp_untapped_colors": "".join(sorted(C.opp_colors)),
        "opp_could_support_sweeper_colors": "".join(opp_sweeper_colors),
        "sweeper_risk_heuristic": bool(C.opp_untapped_lands >= 4 and opp_sweeper_colors),
        "non_committing_lines": [C.key_label(k) for k in others],
        "score": score,
    })
    ev.update(C.board_ev())
    return True, ev


def det_ahead_behind(C):
    """Level One — "Playing From Ahead" / "Playing From Behind" / "Playing Safe
    and Playing Scared": your risk posture should follow the position.

    Qualifies only where the position is genuinely lopsided IN ONE DIRECTION:
    |life differential| >= 5 AND |creature-count differential| >= 2 AND both
    have the SAME sign (ahead on both, or behind on both). Mixed signals are
    routed to ROLE_ASSIGNMENT instead, so the two skills do not collide.

    Thresholds are stated deliberately: 5 life is about one turn of a real
    clock in this corpus, and 2 creatures is the smallest board gap that
    survives one removal spell. Anything looser reads as "normal game".

    Limitations: creature COUNT is a poor proxy for board strength when printed
    power varies (a 1/1 token counts as much as a 5/5); the printed-power
    totals are recorded alongside so the grader can override.

    Ranking: +|life diff| capped at 10; +2 per point of board diff (cap 8);
    +4 if the losing side is at <=8 life (posture actually matters now); +2 if
    the menu offers >=4 real options (a rich choice of postures); +2 when the
    player is BEHIND (Level One's harder half — playing from behind requires
    taking risks, and there are fewer such positions).
    """
    if abs(C.life_diff) < 5 or abs(C.board_diff) < 2:
        return False, {}
    if (C.life_diff > 0) != (C.board_diff > 0):
        return False, {}
    ahead = C.life_diff > 0
    loser_life = min(C.life_you, C.life_opp)
    score = (min(abs(C.life_diff), 10)
             + min(2 * abs(C.board_diff), 8)
             + (4 if loser_life <= 8 else 0)
             + (2 if len(C.real_entries) >= 4 else 0)
             + (0 if ahead else 2))
    ev = dict(C.mana_ev(), **{
        "posture": "ahead" if ahead else "behind",
        "life_diff": C.life_diff,
        "creature_diff": C.board_diff,
        "your_printed_power": C.your_power,
        "opp_printed_power": C.opp_power,
        "thresholds": "abs(life_diff)>=5 and abs(creature_diff)>=2 and same sign",
        "menu_lines": [C.key_label(k) for k in C.real_entries],
        "score": score,
    })
    ev.update(C.board_ev())
    return True, ev


def det_role_assignment(C):
    """Level One — "Role Assignment" (Who's the Beatdown?).

    The question is only interesting when the signals CONFLICT: you are ahead
    on life but behind on board (you are the control, must stabilise), or
    behind on life but ahead on board (you are the beatdown, must push). So
    this detector requires OPPOSITE signs: |life diff| >= 4 and
    |creature diff| >= 1 pointing the other way, plus at least one creature on
    each side (there must be a board to have a role on) and a real choice in
    the menu.

    Thresholds stated: 4 life (a little over a typical attack step here) and 1
    creature — deliberately looser than AHEAD_BEHIND because the interesting
    thing is the CONTRADICTION, not the magnitude.

    Limitations: real role assignment depends on deck archetypes (the two
    decklists), which this corpus does not carry — only the board is visible.
    Turn number is included as a weak archetype proxy.

    Ranking: +2 per point of |life diff| (cap 10) + 2 per point of |board diff|
    (cap 6); +3 if the position also offers both an aggressive line (a creature
    to cast or an attack) and a defensive line (removal / hold up); +2 if
    turn >= 6 (roles are settled by then, so the contradiction is real, not
    noise); -2 if either player's board is empty.
    """
    if abs(C.life_diff) < 4 or abs(C.board_diff) < 1:
        return False, {}
    if (C.life_diff > 0) == (C.board_diff > 0):
        return False, {}
    aggressive = [c["name"] for c in C.castable.values() if "Creature" in (c["type_line"] or "")]
    defensive = [c["name"] for c in C.castable.values() if removal_match(C.oracle(c["name"]))]
    empty = (C.n_cr == 0 or C.n_opp_cr == 0)
    score = (min(2 * abs(C.life_diff), 10)
             + min(2 * abs(C.board_diff), 6)
             + (3 if (aggressive and defensive) else 0)
             + (2 if (C.turn or 0) >= 6 else 0)
             - (2 if empty else 0))
    ev = dict(C.mana_ev(), **{
        "conflict": ("ahead_on_life_behind_on_board" if C.life_diff > 0
                     else "behind_on_life_ahead_on_board"),
        "life_diff": C.life_diff,
        "creature_diff": C.board_diff,
        "your_printed_power": C.your_power,
        "opp_printed_power": C.opp_power,
        "aggressive_lines": aggressive,
        "defensive_lines": defensive,
        "thresholds": "abs(life_diff)>=4 and abs(creature_diff)>=1 and OPPOSITE sign",
        "menu_lines": [C.key_label(k) for k in C.real_entries],
        "score": score,
    })
    ev.update(C.board_ev())
    return True, ev


def det_flexibility(C):
    """Level One — "Flexibility": prefer the line that keeps options open.

    Two branches:
      (a) MODAL — a castable card whose oracle says "choose one" (or is a
          modal DFC/Room half offered as two different casts of one grpId);
      (b) HOLD  — you can cast an instant now, but doing nothing keeps >=2
          distinct instant-speed lines available, i.e. the menu offers >=2
          castable instants/flash cards.

    Limitations: branch (b) cannot see the future, so "keeping options open"
    is measured only as "more than one instant-speed option exists right now";
    and modal detection is oracle-text regex, so escalate/entwine/"choose two"
    cards are missed.

    Ranking: +6 for a modal card; +3 per extra castable instant beyond the
    first (cap 6); +2 if an alternate half of the same card is also offered
    (Room / Adventure / MDFC — the purest flexibility test in this corpus);
    +2 if a non-flexible commit line also exists; -2 if only one option total.
    """
    modal, alt_halves = [], []
    for c in C.castable.values():
        if MODAL_RE.search(C.oracle(c["name"]) or ""):
            modal.append(c)
    for k in C.real_entries:
        if k.split(":", 1)[0] in ("CastLeftRoom", "CastRightRoom", "CastAdventure", "PlayMDFC"):
            alt_halves.append({"key": k, "label": C.key_label(k)})
    instants = [c for c in C.castable.values() if "Instant" in (c["type_line"] or "")]
    if not modal and len(instants) < 2 and not alt_halves:
        return False, {}
    others = [k for k in C.real_entries if k not in {c["key"] for c in modal}]
    score = ((6 if modal else 0)
             + min(3 * max(0, len(instants) - 1), 6)
             + (2 if alt_halves else 0)
             + (2 if others else 0)
             - (2 if len(C.real_entries) <= 1 else 0))
    ev = dict(C.mana_ev(), **{
        "branch": "modal" if modal else ("alt_half" if alt_halves and not instants else "hold_instants"),
        "modal_cards": [dict(c, matched_text="choose one",
                             oracle=C.oracle(c["name"])[:300]) for c in modal],
        "alternate_halves_offered": alt_halves,
        "instants_castable": instants,
        "other_menu_lines": [C.key_label(k) for k in others],
        "score": score,
    })
    ev.update(C.board_ev())
    return True, ev


def det_inevitability(C):
    """Level One — "Inevitability": who wins if nothing changes, and therefore
    who has to act now.

    Best-effort detector. Inevitability is a property of two DECKS, and this
    corpus carries neither decklist nor any notion of "long game" beyond the
    turn counter, so the only observable proxy is a repeating card-advantage
    or value ENGINE already on the battlefield: a permanent whose oracle text
    triggers "at the beginning of your upkeep / end step / draw step", or a
    repeatable "draw a card" on a permanent.

    Qualifies when exactly ONE side controls such an engine (a contested
    inevitability is not a clean lesson) and the game has gone long enough for
    it to matter (turn >= 7).

    HONEST LIMITATION: this detects "someone has a value engine", which is a
    necessary but nowhere near sufficient condition for inevitability. A
    control deck with a full grip and no permanent has inevitability too, and
    this detector will never see it. Candidates here should be read as "engine
    on board" positions and graded accordingly; do not treat a low count as
    proof the corpus lacks inevitability decisions — it lacks the SIGNAL.

    Ranking: +4 if the engine is the OPPONENT's (you are the one under a clock
    and must act — the sharper lesson); +2 per turn past turn 7 (cap 8); +2 if
    the engine controller is also ahead on life; +2 if a proactive line exists
    in the menu; -3 if both sides have engines (recorded but demoted).
    """
    def engines(perms):
        out = []
        for p in perms:
            txt = C.oracle(p.get("name", "")) or ""
            m = UPKEEP_ENGINE_RE.search(txt)
            if m:
                out.append({"name": p.get("name"), "matched_text": m.group(0)})
        return out

    yours = engines(C.creatures + C.others)
    theirs = engines(C.opp_creatures + C.opp_others)
    if not yours and not theirs:
        return False, {}
    if (C.turn or 0) < 7:
        return False, {}
    contested = bool(yours and theirs)
    opp_engine = bool(theirs and not yours)
    controller_ahead = ((C.life_diff > 0 and yours) or (C.life_diff < 0 and theirs))
    score = ((4 if opp_engine else 0)
             + min(2 * ((C.turn or 7) - 7), 8)
             + (2 if controller_ahead else 0)
             + (2 if C.castable else 0)
             - (3 if contested else 0))
    ev = dict(C.mana_ev(), **{
        "your_engines": yours,
        "opp_engines": theirs,
        "contested": contested,
        "who_has_inevitability_signal": ("opponent" if opp_engine
                                         else "you" if yours and not theirs else "both"),
        "turn": C.turn,
        "detector_confidence": "low — engine-on-board proxy only, see docstring",
        "menu_lines": [C.key_label(k) for k in C.real_entries],
        "score": score,
    })
    ev.update(C.board_ev())
    return True, ev


def det_sweepers(C):
    """Level One — "Board Sweepers": when to wipe.

    Qualifies when a genuine sweeper ("destroy all", "exile all", "all
    creatures get -X/-X", or an "each creature" clause in a hostile sentence)
    is in HAND, castable right now, and the opponent controls >=2 creatures.

    The pilot mining run found ZERO qualifying sweeper positions. This detector
    exists to VERIFY that, not to pad it: the card database contains only three
    sweeper-ish cards across 892 names, and the run below reports the count
    honestly rather than loosening the definition.

    Ranking: +2 per opposing creature (cap 10); +3 if you would lose fewer
    creatures than the opponent (an asymmetric wipe); +2 if a non-sweeper line
    exists.
    """
    sw = []
    for h in C.hand_cards():
        txt = C.oracle(h["name"])
        m = sweeper_match(txt)
        if not m:
            continue
        if not C.hand_castable(h):
            continue
        sw.append({"name": h["name"], "cmc": h.get("cmc"), "mana_cost": h.get("mana_cost"),
                   "matched_text": m})
    if not sw or C.n_opp_cr < 2:
        return False, {}
    score = (min(2 * C.n_opp_cr, 10)
             + (3 if C.n_cr < C.n_opp_cr else 0)
             + (2 if len(C.real_entries) > len(sw) else 0))
    ev = dict(C.mana_ev(), **{
        "sweepers_castable": sw,
        "opp_creature_count": C.n_opp_cr,
        "your_creature_count": C.n_cr,
        "score": score,
    })
    ev.update(C.board_ev())
    return True, ev


DETECTORS = {
    "MANA_SEQUENCING": det_mana_sequencing,
    "TEMPO_CURVE": det_tempo_curve,
    "TEMPO_VS_CARDS": det_tempo_vs_cards,
    "ATTACK_BLOCK": det_attack_block,
    "DAMAGE_RACING": det_damage_racing,
    "THREATS_ANSWERS": det_threats_answers,
    "PERMISSION_TIMING": det_permission_timing,
    "CREATURE_LANDS": det_creature_lands,
    "SYMMETRIC_EFFECTS": det_symmetric_effects,
    "INVESTMENT": det_investment,
    "AHEAD_BEHIND": det_ahead_behind,
    "ROLE_ASSIGNMENT": det_role_assignment,
    "FLEXIBILITY": det_flexibility,
    "INEVITABILITY": det_inevitability,
    "SWEEPERS": det_sweepers,
}

# Skills mined from combat decision kinds, which dsv4 never labelled.
GT_ONLY_SKILLS = {"ATTACK_BLOCK", "DAMAGE_RACING"}


# ------------------------------------------------------------------- driver
def select_diverse(items, cap=CAP, per_turn=PER_TURN, per_replay=PER_REPLAY):
    """Score-ordered, STRICT diversity: at most ``per_turn`` per (replay, turn)
    and ``per_replay`` per replay. No backfill — a short list is information."""
    chosen = []
    seen_turn = defaultdict(int)
    per_file = defaultdict(int)
    for e in items:
        tk = (e["replay_file"], e["turn"])
        if seen_turn[tk] >= per_turn or per_file[e["replay_file"]] >= per_replay:
            continue
        seen_turn[tk] += 1
        per_file[e["replay_file"]] += 1
        chosen.append(e)
        if len(chosen) >= cap:
            break
    return chosen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch", default=os.environ.get(
        "MTGACOACH_SCRATCH",
        "/tmp/claude-1000/-mnt-repos-mtgacoach/11a24df8-cb20-416e-93f2-b9a111105f29/scratchpad"))
    ap.add_argument("--stats-only", action="store_true",
                    help="print the summary table without writing the JSON")
    args = ap.parse_args()

    cards = json.load(open(os.path.join(args.scratch, "cards_full.json")))
    M = TW.Miner(cards)

    labels = {r["id"]: r for r in load_jsonl(LABELS_PATH)}
    # combat rows only: the core file drops them, the full file keeps them
    labels_full = {}
    if os.path.exists(LABELS_FULL_PATH):
        for r in load_jsonl(LABELS_FULL_PATH):
            if r["id"] not in labels:
                labels_full[r["id"]] = r
    gt, grp_name = {}, {}
    for r in load_jsonl(GT_PATH):
        gt[f"{r['decision_uid']}:{r['replay_file']}"] = r
        for a in r["real_menu"] + (r.get("real_menu_inactive") or []):
            if a.get("grp_id") and a.get("name"):
                grp_name.setdefault(a["grp_id"], a["name"])
        for c in r["state"]["hand"]:
            if c.get("grp_id") and c.get("name"):
                grp_name.setdefault(c["grp_id"], c["name"])

    out = {s: [] for s in SKILLS}
    matched = defaultdict(int)          # qualified before the <3-option filter
    kept_prefilter = defaultdict(int)   # qualified after the <3-option filter
    stats = defaultdict(int)
    stats["gt_records"] = len(gt)
    stats["labels"] = len(labels)
    stats["joined"] = len(set(gt) & set(labels))

    for pid, rec in gt.items():
        lab = labels.get(pid)
        combat = rec.get("decision_kind") in ("attack_declaration", "block_assignment")
        if lab is None and not combat:
            stats["gt_without_label"] += 1
            continue
        if combat:
            menu_keys = rec["real_menu_key"]
            lab = labels_full.get(pid)
            label_source = "dsv4_full" if lab else "gt_only"
        else:
            menu_keys = lab["meta"]["menu_keys"]
            label_source = "dsv4"

        C = Ctx(rec, lab, menu_keys, M, grp_name, label_source)
        small_menu = len(C.real_entries) < 3
        if small_menu:
            stats["small_menu"] += 1
        stats["considered"] += 1

        base = {
            "id": pid,
            "decision_uid": rec["decision_uid"],
            "replay_file": rec["replay_file"],
            "turn": rec.get("turn_number"),
            "phase": rec.get("phase"),
            "step": rec.get("step"),
            "decision_kind": rec.get("decision_kind"),
            "menu_keys": menu_keys,
            "menu_labels": [C.key_label(k) for k in menu_keys],
            "label_source": label_source,
            "dsv4_pick": (lab or {}).get("teacher_pick"),
            "dsv4_pick_name": C.key_label((lab or {}).get("teacher_pick")),
            "dsv4_rationale": (lab or {}).get("teacher_rationale"),
            "gold_pick": ((lab or {}).get("gold_pick")
                          or _gold_from_pick(rec)),
            "gold_pick_name": C.key_label((lab or {}).get("gold_pick") or _gold_from_pick(rec)),
            "agrees": (lab or {}).get("agrees_with_gold"),
        }

        for skill, det in DETECTORS.items():
            if (skill in GT_ONLY_SKILLS) != combat:
                continue  # combat skills only on combat rows, and vice versa
            ok, ev = det(C)
            if not ok:
                continue
            matched[skill] += 1
            if small_menu:
                continue
            kept_prefilter[skill] += 1
            score = ev.pop("score", 0)
            e = dict(base)
            e["score"] = round(float(score), 2)
            e["evidence"] = ev
            out[skill].append(e)

    summary = {}
    for skill in SKILLS:
        items = sorted(out[skill], key=lambda e: (-e["score"], e["id"]))
        out[skill] = select_diverse(items)
        summary[skill] = {
            "matched": matched[skill],
            "after_menu_filter": kept_prefilter[skill],
            "kept": len(out[skill]),
            "distinct_replays": len({e["replay_file"] for e in out[skill]}),
        }

    dest = os.path.join(args.scratch, "level_one_candidates.json")
    if not args.stats_only:
        with open(dest, "w") as fh:
            json.dump(out, fh, indent=1)

    print(f"gt records {stats['gt_records']}  labels {stats['labels']}  "
          f"joined {stats['joined']}  considered {stats['considered']}  "
          f"small-menu(<3) {stats['small_menu']}")
    print(f"{'skill':18s} {'matched':>8s} {'post-filter':>12s} {'kept':>6s} {'replays':>8s}")
    for skill in SKILLS:
        s = summary[skill]
        print(f"{skill:18s} {s['matched']:8d} {s['after_menu_filter']:12d} "
              f"{s['kept']:6d} {s['distinct_replays']:8d}")
    print("wrote", dest if not args.stats_only else "(stats only — nothing written)")


def _gold_from_pick(rec):
    """The human's action as a menu key, for rows dsv4 never labelled."""
    p = rec.get("real_pick") or {}
    at = (p.get("action_type") or "").replace("ActionType_", "")
    if not at:
        return None
    return f"{at}:{p['grp_id']}" if p.get("grp_id") is not None else at


if __name__ == "__main__":
    main()
