#!/usr/bin/env python3
"""Mine the replay corpus for positions that exercise four named MTG skills.

Reads
-----
* ``tools/eval/data/replay_strategic_groundtruth.jsonl`` — 2,878 replay decisions
  (real menu, board state, human pick).
* ``tools/training/data/dsv4_labels_v1_core.jsonl`` — dsv4 teacher picks plus the
  RENDERED menu (``meta.menu_keys``) that the model actually saw.
* ``<scratch>/cards_full.json`` — Scryfall oracle text / type / colour / mana
  production data for every card name that appears anywhere in the corpus.

Writes
------
``<scratch>/tripwire_candidates.json`` — four ranked, capped (60) candidate lists.
This script emits CANDIDATES + EVIDENCE ONLY. It never asserts a correct answer;
``dsv4_pick`` / ``gold_pick`` are carried through verbatim for a human to judge.

Castability
-----------
IMPORTANT: the rendered menu is NOT a castable-only list — MTGA's ActionsAvailable
(and hence the prompt) offers casts the player cannot currently pay for (verified:
"Mana: 0", every land tapped, yet a {4}{G} spell appears as menu option 1). So a
card counts as CASTABLE NOW only when all three hold:

1. its ``Cast:<grpId>`` key is in the rendered menu (the model actually saw it),
2. ``cmc <= total untapped mana`` (untapped lands + untapped nonland mana rocks;
   untapped mana creatures are counted too, best effort — summoning sickness is
   not observable in this corpus), and
3. every coloured pip is satisfiable by the colours those untapped sources make
   (hybrid pips count as either half; a source with unknown production — e.g. a
   fetchland — is treated as a wildcard).

Cost reductions / alternative costs are ignored, so castability is mildly
conservative. ``available_mana`` in the evidence is the spec definition
(``len(lands_in_play) - lands_tapped``); ``available_mana_total`` adds rocks.

Global filter
-------------
A position is dropped from every category when its rendered menu has fewer than
3 distinct entries once ``Pass``, ``FloatMana`` and ``Activate_Mana:*`` are
removed — forced/near-forced moves, useless as puzzles.

Categories and their qualification rules
----------------------------------------
CURVE       own turn, Main1, turn 1-5, >=1 untapped land, and the menu offers a
            castable-now *permanent* (creature/artifact/enchantment/battle/
            planeswalker) whose cmc exactly equals available mana, plus at least
            one alternative (cheaper spell, other spell, or Pass).
MANA        the menu offers >=2 land plays with DISTINCT card names AND the hand
            holds >=1 coloured spell that is not castable right now.
SEQUENCE    >=2 distinct spells castable now AND >=1 card involved in the menu
            carries an order-sensitive trigger (ETB / landfall / cast trigger /
            "if you control").
INTERACTION a card in HAND is removal or a sweeper AND is castable now AND the
            opponent has >=1 creature (removal) / >=2 creatures (sweeper).

Two deliberate deviations from a literal regex match, both because the literal
version produced garbage candidates:

* Sweeper: a bare ``each creature`` match flags pump auras ("+1/+1 counter on
  each creature you control"). An ``each creature`` hit only counts when the
  SAME SENTENCE also carries a hostile effect (damage / destroy / exile /
  sacrifice / ``-X/-X`` / "loses").
* Removal: a bare ``exile target`` match flags blink spells (Ephemerate,
  Cloudshift, Splash Portal — "Exile target creature *you control*, then return
  it"). A removal sentence is rejected when its target is qualified with "you
  control" and it does not also mention an opponent.

The surviving sentence is recorded per card in ``matched_text``.

A third refinement: spot removal must be able to point at something the opponent
actually has. Artifact/enchantment-only removal ("Destroy target artifact or
enchantment") is not counted as creature interaction unless the opponent also
controls a nonland noncreature permanent — otherwise a naturalize in hand made
every board an "interaction" position.

Diversity
---------
Consecutive priority windows inside one turn produce near-identical positions,
so ranking alone returns 60 copies of the same puzzle. Selection therefore takes
at most 1 candidate per (replay_file, turn) and at most 6 per replay_file first;
if a category is still under the cap, the remaining slots are filled from the
leftovers in score order.

Ranking rules (best-first, "cleanly isolates the skill")
--------------------------------------------------------
CURVE       Reward a real curve dilemma: +3 per on-curve (mana-exact) permanent
            (cap 2), +2 per strictly cheaper castable alternative (cap 3), +1 if
            Pass is offered. Penalise confounds: -3 if a land play is still in
            the menu (the decision is then partly a land drop, not pure
            development), -2 if removal/a sweeper is castable (an INTERACTION
            decision wearing a curve costume), -0.6*|turn-3| to favour the turns
            where curve discipline actually decides games.
MANA        Reward colour-relevant choice: +5 if the candidate lands produce
            DIFFERENT colour sets (a real sequencing decision, not two copies of
            the same land), +2 if at least one hand spell is specifically
            COLOUR-blocked (not merely too expensive), +2 per distinct land name
            (cap 3, i.e. +6), +1 per colour-blocked/uncastable coloured hand
            spell (cap 4), +2 if the lands differ on enters-tapped, +2 if exactly
            one land maximises next-turn castability (a uniquely best answer
            exists), -3 if every candidate land produces the same colours,
            -0.5 per turn past turn 6 (land sequencing decides games early; by
            turn 10 with eight lands out it is mostly noise).
SEQUENCE    Weight trigger strength: +3 per STRONG order-sensitive trigger
            (ETB / landfall / cast trigger) on a castable-now or playable card,
            cap 9; +1 per WEAK trigger ("if you control" static checks), cap 2;
            +1.5 per castable spell beyond the first (cap 4.5); +2 if a permanent
            already on YOUR battlefield has a strong trigger that this turn's
            ordering feeds; +2 if a land play is also available (land-vs-spell
            ordering is the crispest sequencing test); -2 if only weak triggers
            matched.
INTERACTION Reward pressure and choice: +4 if a sweeper qualifies, +1 per
            opposing creature (cap 4), +2 per distinct castable interaction card
            (cap 4), +2 if the menu also offers a real non-interaction line (the
            "answer now vs develop" tension), -2 if interaction is the only
            thing available.

Usage
-----
    python3 tools/training/mine_strategic_tripwires.py [--scratch DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(_HERE))  # tools/training -> repo root
GT_PATH = os.path.join(REPO, "tools", "eval", "data", "replay_strategic_groundtruth.jsonl")
LABELS_PATH = os.path.join(REPO, "tools", "training", "data", "dsv4_labels_v1_core.jsonl")

CAP = 60
PERMANENT_WORDS = ("creature", "artifact", "enchantment", "planeswalker", "battle")

REMOVAL_RE = [
    re.compile(r"destroy target", re.I),
    re.compile(r"exile target", re.I),
    re.compile(r"deals \d+ damage to target", re.I),
    re.compile(r"target creature gets -", re.I),
]
SWEEPER_STRICT_RE = [
    re.compile(r"destroy all", re.I),
    re.compile(r"exile all", re.I),
    re.compile(r"all creatures get -", re.I),
]
EACH_CREATURE_RE = re.compile(r"each creature", re.I)
HOSTILE_RE = re.compile(r"(damage|destroy|exile|sacrific|loses|gets -|-\d+/-\d+)", re.I)
SELF_TARGET_RE = re.compile(r"target [^.\n]*you control", re.I)
OPPONENT_RE = re.compile(r"(an opponent controls|target opponent|opponent's)", re.I)

STRONG_TRIGGER_RE = [
    re.compile(r"whenever an? [^.\n]* you control enters", re.I),
    re.compile(r"whenever [^.\n]*enters", re.I),
    re.compile(r"when [^.\n]*enters", re.I),
    re.compile(r"landfall", re.I),
    re.compile(r"whenever you cast", re.I),
]
WEAK_TRIGGER_RE = [re.compile(r"if you control", re.I)]

PIP_RE = re.compile(r"\{([^}]+)\}")
ADD_MANA_RE = re.compile(r"add [{(]|add one mana|add two mana|add three mana", re.I)


# ------------------------------------------------------------------ helpers
def load_jsonl(path):
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def colored_pips(mana_cost: str) -> set:
    out = set()
    for sym in PIP_RE.findall(mana_cost or ""):
        s = sym.upper()
        if "/" in s:
            out |= {p for p in s.split("/") if p in "WUBRG"}
        elif s in "WUBRG":
            out.add(s)
    return out


def required_colors(mana_cost: str) -> list:
    """Each item is the SET of colours that can satisfy one coloured pip."""
    reqs = []
    for sym in PIP_RE.findall(mana_cost or ""):
        s = sym.upper()
        if "/" in s:
            opts = {p for p in s.split("/") if p in "WUBRG"}
            if opts:
                reqs.append(opts)
        elif s in "WUBRG":
            reqs.append({s})
    return reqs


def satisfiable(mana_cost: str, avail_colors: set) -> bool:
    if "*" in avail_colors:
        return True
    return all(req & avail_colors for req in required_colors(mana_cost))


def strip_forced(menu_keys):
    out = []
    for k in menu_keys:
        if k in ("Pass", "FloatMana") or k.startswith("Activate_Mana"):
            continue
        if k not in out:
            out.append(k)
    return out


def match_any(regexes, text):
    for rx in regexes:
        m = rx.search(text or "")
        if m:
            return m.group(0)
    return None


def removal_match(text: str):
    """Removal that points at the OPPONENT's stuff (blink spells rejected)."""
    for sent in re.split(r"[.\n]", text or ""):
        m = match_any(REMOVAL_RE, sent)
        if not m:
            continue
        if SELF_TARGET_RE.search(sent) and not OPPONENT_RE.search(sent):
            continue
        return sent.strip()[:140]
    return None


def sweeper_match(text: str):
    m = match_any(SWEEPER_STRICT_RE, text)
    if m:
        return m
    for sent in re.split(r"[.\n]", text or ""):
        if EACH_CREATURE_RE.search(sent) and HOSTILE_RE.search(sent):
            return sent.strip()[:120]
    return None


def board_name_list(entries):
    return [e.get("name") for e in (entries or [])]


# ------------------------------------------------------------------- mining
class Miner:
    def __init__(self, cards):
        self.cards = cards

    def card(self, name):
        return self.cards.get(name) or {}

    def oracle(self, name):
        return self.card(name).get("oracle", "")

    def mana_pool(self, st):
        """(total_untapped_mana, colours those untapped sources can make)."""
        total = 0
        colors = set()
        sources = []
        for l in st.get("lands_in_play") or []:
            if l.get("is_tapped"):
                continue
            total += 1
            p = self.card(l.get("name", "")).get("produces") or ""
            colors |= set(p) if p else {"*"}
            sources.append(l.get("name"))
        for p in (st.get("other_permanents") or []) + (st.get("creatures_in_play") or []):
            if p.get("is_tapped"):
                continue
            c = self.card(p.get("name", ""))
            txt = c.get("oracle", "")
            if not ADD_MANA_RE.search(txt):
                continue
            total += 1
            prod = c.get("produces") or ""
            colors |= set(prod) if prod else {"*"}
            sources.append(p.get("name"))
        return total, colors, sources

    ALT_HALF_VERBS = ("CastAdventure", "CastLeftRoom", "CastRightRoom", "PlayMDFC")

    def alt_half_offered(self, menu_keys, grp_id):
        """Adventure / Room / MDFC halves are cheaper alternate casts of the
        same grpId; the corpus records their hand cmc as 0."""
        return any(f"{v}:{grp_id}" in menu_keys for v in self.ALT_HALF_VERBS)

    def castable(self, name, cmc, mana_cost, total, colors, menu_keys, grp_id):
        if grp_id is not None and f"Cast:{grp_id}" not in menu_keys:
            return False
        if not cmc:  # corpus records 0.0/None for adventure & DFC hand cards
            real = self.card(name).get("cmc")
            if real and not (grp_id is not None and self.alt_half_offered(menu_keys, grp_id)):
                cmc = real
        if cmc is None:
            return False
        if float(cmc) > total:
            return False
        cost = mana_cost or self.card(name).get("cost", "")
        return satisfiable(cost, colors)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch", default=os.environ.get(
        "MTGACOACH_SCRATCH",
        "/tmp/claude-1000/-mnt-repos-mtgacoach/11a24df8-cb20-416e-93f2-b9a111105f29/scratchpad"))
    ap.add_argument("--no-menu-filter", action="store_true",
                    help="diagnostic: keep positions with <3 real menu entries "
                         "(shows how much the forced-move filter removes); does "
                         "NOT write the candidate file")
    args = ap.parse_args()

    cards = json.load(open(os.path.join(args.scratch, "cards_full.json")))
    M = Miner(cards)

    labels = {r["id"]: r for r in load_jsonl(LABELS_PATH)}
    gt = {}
    grp_name = {}
    for r in load_jsonl(GT_PATH):
        gt[f"{r['decision_uid']}:{r['replay_file']}"] = r
        for a in r["real_menu"] + (r.get("real_menu_inactive") or []):
            if a.get("grp_id") and a.get("name"):
                grp_name.setdefault(a["grp_id"], a["name"])
        for c in r["state"]["hand"]:
            if c.get("grp_id") and c.get("name"):
                grp_name.setdefault(c["grp_id"], c["name"])

    stats = defaultdict(int)
    stats["gt_records"] = len(gt)
    stats["labelled"] = len(set(gt) & set(labels))
    out = {"CURVE": [], "MANA": [], "SEQUENCE": [], "INTERACTION": []}
    dropped_small_menu = 0

    for pid, lab in labels.items():
        rec = gt.get(pid)
        if rec is None:
            stats["label_without_gt"] += 1
            continue
        menu_keys = lab["meta"]["menu_keys"]
        real_entries = strip_forced(menu_keys)
        if len(real_entries) < 3:
            dropped_small_menu += 1
            if not args.no_menu_filter:
                continue
        stats["passed_menu_filter"] += 1

        key_set = set(menu_keys)
        pass_available = "Pass" in menu_keys
        st = rec["state"]
        hand = st["hand"]
        lands_in_play = st.get("lands_in_play") or []
        avail_lands = max(0, len(lands_in_play) - int(st.get("lands_tapped") or 0))
        total_mana, avail_colors, sources = M.mana_pool(st)
        turn = rec.get("turn_number")
        phase = rec.get("phase")
        own = bool(rec.get("is_own_turn"))

        by_key = {}
        for a in rec["real_menu"]:
            at = (a.get("action_type") or "").replace("ActionType_", "")
            k = f"{at}:{a['grp_id']}" if a.get("grp_id") is not None else at
            by_key.setdefault(k, a)

        def entry(k):
            a = by_key.get(k)
            if a:
                return a
            if ":" in k:
                gid = k.split(":", 1)[1]
                if gid.isdigit() and int(gid) in grp_name:
                    nm = grp_name[int(gid)]
                    return {"name": nm, "grp_id": int(gid),
                            "type_line": M.card(nm).get("type", ""),
                            "mana_value": M.card(nm).get("cmc"),
                            "mana_cost": M.card(nm).get("cost", "")}
            return None

        # ---- menu options, split into castable-now vs shown-but-unaffordable
        castable_casts, unaffordable_casts, land_opts = {}, [], []
        for k in real_entries:
            a = entry(k)
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
                if M.castable(nm, cmc, cost, total_mana, avail_colors, key_set, a.get("grp_id")):
                    castable_casts.setdefault(nm, info)
                else:
                    unaffordable_casts.append(info)
            elif k.startswith("Play:") or k.startswith("PlayMDFC:"):
                land_opts.append({"key": k, "name": nm})

        interaction_cards = _interaction_cards(hand, key_set, M, total_mana, avail_colors)

        def key_label(k):
            """'Cast:92081' -> 'Cast Optimistic Scavenger' for human review."""
            if not k or ":" not in k:
                return k
            verb, gid = k.split(":", 1)
            a = entry(k)
            nm = (a or {}).get("name") or (grp_name.get(int(gid)) if gid.isdigit() else None)
            return f"{verb} {nm}" if nm else k

        base = {
            "id": pid,
            "decision_uid": rec["decision_uid"],
            "replay_file": rec["replay_file"],
            "turn": turn,
            "phase": phase,
            "step": rec.get("step"),
            "decision_kind": rec.get("decision_kind"),
            "menu_keys": menu_keys,
            "menu_labels": [key_label(k) for k in menu_keys],
            "dsv4_pick": lab.get("teacher_pick"),
            "dsv4_pick_name": key_label(lab.get("teacher_pick")),
            "dsv4_rationale": lab.get("teacher_rationale"),
            "gold_pick": lab.get("gold_pick"),
            "gold_pick_name": key_label(lab.get("gold_pick")),
            "agrees": lab.get("agrees_with_gold"),
        }
        mana_ev = {
            "available_mana": avail_lands,
            "available_mana_total": total_mana,
            "available_colors": "".join(sorted(avail_colors)),
            "untapped_sources": sources,
        }

        # ============================================================ 1. CURVE
        if own and phase == "Phase_Main1" and turn and 1 <= turn <= 5 and avail_lands >= 1:
            on_curve, cheaper, other = [], [], []
            for c in castable_casts.values():
                tl = (c["type_line"] or "").lower()
                is_perm = (any(w in tl for w in PERMANENT_WORDS)
                           and "instant" not in tl and "sorcery" not in tl)
                if c["cmc"] is None:
                    continue
                if is_perm and float(c["cmc"]) == float(avail_lands):
                    on_curve.append(c)
                elif float(c["cmc"]) < float(avail_lands):
                    cheaper.append(c)
                else:
                    other.append(c)
            if on_curve and (len(castable_casts) - len(on_curve) >= 1 or pass_available):
                stats["CURVE_matched"] += 1
                score = (3 * min(len(on_curve), 2)
                         + 2 * min(len(cheaper), 3)
                         + (1 if pass_available else 0)
                         - (3 if land_opts else 0)
                         - (2 if interaction_cards else 0)
                         - 0.6 * abs(turn - 3))
                e = dict(base)
                e["score"] = round(score, 2)
                e["evidence"] = dict(mana_ev, **{
                    "lands_in_play": board_name_list(lands_in_play),
                    "lands_tapped": st.get("lands_tapped"),
                    "lands_played_this_turn": st.get("lands_played_this_turn"),
                    "mana_after_land_drop": avail_lands + (1 if land_opts and not st.get("lands_played_this_turn") else 0),
                    "on_curve_options": on_curve,
                    "cheaper_options": cheaper,
                    "other_castable_options": other,
                    "shown_but_unaffordable": unaffordable_casts,
                    "pass_available": pass_available,
                    "land_play_still_in_menu": [l["name"] for l in land_opts],
                    "interaction_also_castable": [c["name"] for c in interaction_cards],
                    "your_board": _board(st, "creatures_in_play", "other_permanents"),
                    "opp_board": _board(st, "opp_creatures_in_play", "opp_other_permanents"),
                    "life": st["life"],
                })
                out["CURVE"].append(e)

        # ============================================================= 2. MANA
        distinct_land_names = sorted({l["name"] for l in land_opts})
        if len(distinct_land_names) >= 2:
            blocked, mana_short = [], []
            for h in hand:
                tl = (h.get("type_line") or "").lower()
                if "land" in tl:
                    continue
                pips = colored_pips(h.get("mana_cost", ""))
                if not pips:
                    continue
                if M.castable(h["name"], h.get("cmc"), h.get("mana_cost"),
                              total_mana, avail_colors, key_set, h.get("grp_id")):
                    continue
                item = {"name": h["name"], "mana_cost": h.get("mana_cost"),
                        "cmc": h.get("cmc"), "pips": "".join(sorted(pips))}
                if not satisfiable(h.get("mana_cost", ""), avail_colors):
                    item["reason"] = "colour_blocked"
                    blocked.append(item)
                else:
                    item["reason"] = "too_expensive"
                    mana_short.append(item)
            needs = blocked + mana_short
            if needs:
                stats["MANA_matched"] += 1
                lo = []
                for nm in distinct_land_names:
                    ci = M.card(nm)
                    lo.append({"name": nm,
                               "produces": ci.get("produces") or "*",
                               "enters_tapped": bool(ci.get("enters_tapped")),
                               "type": ci.get("type", "")})
                next_land_count = len(lands_in_play) + 1
                # colours available next turn if this land is the one played
                base_colors = set()
                for l in lands_in_play:
                    p = M.card(l.get("name", "")).get("produces") or ""
                    base_colors |= set(p) if p else {"*"}
                enables = {}
                for l in lo:
                    prod = set(l["produces"]) if l["produces"] != "*" else {"*"}
                    avail = base_colors | prod
                    unlocked = []
                    for h in hand:
                        tl = (h.get("type_line") or "").lower()
                        if "land" in tl:
                            continue
                        if (h.get("cmc") or 0) > next_land_count:
                            continue
                        if satisfiable(h.get("mana_cost", ""), avail):
                            unlocked.append(h["name"])
                    enables[l["name"]] = {"n_castable_next_turn": len(unlocked), "cards": unlocked}
                best = max(v["n_castable_next_turn"] for v in enables.values())
                best_lands = [k for k, v in enables.items() if v["n_castable_next_turn"] == best]
                produce_sets = {l["produces"] for l in lo}
                tapped_sets = {l["enters_tapped"] for l in lo}
                score = (5 * (len(produce_sets) > 1)
                         + 2 * bool(blocked)
                         + 2 * min(len(distinct_land_names), 3)
                         + min(len(needs), 4)
                         + 2 * (len(tapped_sets) > 1)
                         + 2 * (len(best_lands) == 1)
                         - (3 if len(produce_sets) == 1 else 0)
                         - 0.5 * max(0, (turn or 0) - 6))
                e = dict(base)
                e["score"] = round(score, 2)
                e["evidence"] = dict(mana_ev, **{
                    "lands_in_play": board_name_list(lands_in_play),
                    "land_options": lo,
                    "hand_colour_needs": blocked,
                    "hand_too_expensive": mana_short,
                    "hand_all": [{"name": h["name"], "mana_cost": h.get("mana_cost"),
                                  "cmc": h.get("cmc"), "type_line": h.get("type_line")} for h in hand],
                    "enables_next_turn": enables,
                    "best_land_next_turn": best_lands,
                    "castable_now": sorted(castable_casts),
                    "your_board": _board(st, "creatures_in_play", "other_permanents"),
                    "opp_board": _board(st, "opp_creatures_in_play", "opp_other_permanents"),
                    "life": st["life"],
                })
                out["MANA"].append(e)

        # ========================================================= 3. SEQUENCE
        if len(castable_casts) >= 2:
            strong, weak = [], []
            for c in castable_casts.values():
                txt = M.oracle(c["name"])
                m = match_any(STRONG_TRIGGER_RE, txt)
                if m:
                    strong.append({"name": c["name"], "cmc": c["cmc"], "trigger": m})
                    continue
                m = match_any(WEAK_TRIGGER_RE, txt)
                if m:
                    weak.append({"name": c["name"], "cmc": c["cmc"], "trigger": m})
            for nm in {l["name"] for l in land_opts}:
                m = match_any(STRONG_TRIGGER_RE, M.oracle(nm))
                if m:
                    strong.append({"name": nm, "cmc": 0, "trigger": m, "is_land_play": True})
            board_trigs = []
            for p in (st.get("creatures_in_play") or []) + (st.get("other_permanents") or []):
                m = match_any(STRONG_TRIGGER_RE, M.oracle(p.get("name", "")))
                if m:
                    board_trigs.append({"name": p.get("name"), "trigger": m})
            if strong or weak:
                stats["SEQUENCE_matched"] += 1
                score = (min(3 * len(strong), 9)
                         + min(len(weak), 2)
                         + min(1.5 * (len(castable_casts) - 1), 4.5)
                         + (2 if board_trigs else 0)
                         + (2 if land_opts else 0)
                         - (2 if (weak and not strong) else 0))
                e = dict(base)
                e["score"] = round(score, 2)
                e["evidence"] = dict(mana_ev, **{
                    "castable_now": list(castable_casts.values()),
                    "land_plays_available": sorted({l["name"] for l in land_opts}),
                    "trigger_cards": strong,
                    "weak_trigger_cards": weak,
                    "board_trigger_cards": board_trigs,
                    "shown_but_unaffordable": unaffordable_casts,
                    "your_board": _board(st, "creatures_in_play", "other_permanents"),
                    "opp_board": _board(st, "opp_creatures_in_play", "opp_other_permanents"),
                    "life": st["life"],
                })
                out["SEQUENCE"].append(e)

        # ====================================================== 4. INTERACTION
        if interaction_cards:
            opp_cr = st.get("opp_creatures_in_play") or []
            opp_other = st.get("opp_other_permanents") or []
            n_opp = len(opp_cr)
            qualifying = []
            for c in interaction_cards:
                if c["is_sweeper"]:
                    if n_opp >= 2:
                        qualifying.append(c)
                    continue
                # spot removal must be able to point at something the opponent has
                if c["hits_creature"] and n_opp >= 1:
                    qualifying.append(c)
                elif c["hits_noncreature"] and opp_other and n_opp >= 1:
                    qualifying.append(c)
            if qualifying:
                stats["INTERACTION_matched"] += 1
                is_sweeper = any(c["is_sweeper"] for c in qualifying)
                if is_sweeper:
                    stats["INTERACTION_sweeper"] += 1
                inter_keys = {c["key"] for c in qualifying}
                other_lines = [k for k in real_entries if k not in inter_keys]
                score = (4 * is_sweeper
                         + min(n_opp, 4)
                         + min(2 * len(qualifying), 4)
                         + (2 if other_lines else -2))
                e = dict(base)
                e["score"] = round(score, 2)
                e["evidence"] = dict(mana_ev, **{
                    "interaction_cards": qualifying,
                    "is_sweeper": is_sweeper,
                    "opp_creature_count": n_opp,
                    "opp_board": board_name_list(opp_cr),
                    "opp_other_permanents": board_name_list(st.get("opp_other_permanents")),
                    "your_creatures": board_name_list(st.get("creatures_in_play")),
                    "other_castable_lines": [c["name"] for c in castable_casts.values()
                                             if c["key"] not in inter_keys],
                    "other_menu_lines": other_lines,
                    "life": st["life"],
                })
                out["INTERACTION"].append(e)

    summary = {}
    for cat, items in out.items():
        items.sort(key=lambda e: (-e["score"], e["id"]))
        summary[cat] = {"matched": len(items)}
        out[cat] = _select_diverse(items, CAP)
        summary[cat]["kept"] = len(out[cat])

    dest = os.path.join(args.scratch, "tripwire_candidates.json")
    if args.no_menu_filter:
        dest = "(diagnostic run — nothing written)"
    else:
        json.dump(out, open(dest, "w"), indent=1)

    print(f"gt records          : {stats['gt_records']}")
    print(f"labelled (joined)   : {stats['labelled']}")
    print(f"dropped <3 options  : {dropped_small_menu}")
    print(f"passed menu filter  : {stats['passed_menu_filter']}")
    for cat in ("CURVE", "MANA", "SEQUENCE", "INTERACTION"):
        replays = len({e["replay_file"] for e in out[cat]})
        print(f"{cat:12s} matched={stats[cat + '_matched']:5d}  kept={summary[cat]['kept']:3d}"
              f"  distinct_replays={replays}")
    print(f"  (INTERACTION positions that are true SWEEPERS: {stats['INTERACTION_sweeper']})")
    print("wrote", dest)


def _select_diverse(items, cap, per_turn=1, per_replay=6):
    """Score-ordered selection, but at most one position per (replay, turn) and
    a few per replay; leftovers backfill if the category is short."""
    chosen, leftover = [], []
    seen_turn, per_file = set(), defaultdict(int)
    for e in items:
        k = (e["replay_file"], e["turn"])
        if k in seen_turn or per_file[e["replay_file"]] >= per_replay:
            leftover.append(e)
            continue
        seen_turn.add(k)
        per_file[e["replay_file"]] += 1
        chosen.append(e)
        if len(chosen) >= cap:
            return chosen
    for e in leftover:
        if len(chosen) >= cap:
            break
        chosen.append(e)
    return chosen


def _board(st, ckey, okey):
    return {"creatures": board_name_list(st.get(ckey)),
            "other": board_name_list(st.get(okey))}


def _interaction_cards(hand, key_set, M, total_mana, avail_colors):
    """Removal / sweeper cards in HAND that are castable right now."""
    found, seen = [], set()
    for h in hand:
        nm = h.get("name")
        if nm in seen:
            continue
        txt = M.oracle(nm)
        if not txt:
            continue
        sw = sweeper_match(txt)
        rm = removal_match(txt)
        if not (sw or rm):
            continue
        if not M.castable(nm, h.get("cmc"), h.get("mana_cost"),
                          total_mana, avail_colors, key_set, h.get("grp_id")):
            continue
        sent = sw or rm
        # scope = the words right after "target ..." / "all ...", NOT the whole
        # sentence ("When this creature enters, destroy target artifact" is not
        # creature removal).
        m = re.search(r"\b(?:target|all)\s+([^.,;:]{0,60})", sent, re.I)
        low = (m.group(1) if m else sent).lower()
        seen.add(nm)
        found.append({"key": f"Cast:{h.get('grp_id')}", "name": nm,
                      "mana_cost": h.get("mana_cost"), "cmc": h.get("cmc"),
                      "type_line": h.get("type_line"),
                      "is_sweeper": bool(sw), "matched_text": sent,
                      "hits_creature": ("creature" in low or "permanent" in low),
                      "hits_noncreature": any(w in low for w in
                                              ("artifact", "enchantment", "permanent", "planeswalker"))})
    return found


if __name__ == "__main__":
    main()
