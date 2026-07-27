"""Stage ``strategic_casts.jsonl`` into a 3-stage complexity curriculum.

WHY THIS EXISTS
---------------
The 31B LoRA trained on ``play_decisions`` passed the gate on hygiene (100%
schema, 100% legality) and then scored *exactly* the untuned base on the 63
strategic decisions (13/63 vs 13/63, paired delta 0.0000) because 69% of that
corpus was land drops: the model learned a reflex, not a policy.
``strategic_casts.jsonl`` removes land drops by construction (214,090 diamond+
cast decisions, land-drop share exactly 0). This module orders those decisions
by measured complexity so training can walk from "which creature do I curve
out with" to "which of six live options is best on a cluttered board".

THE METRIC IS CALIBRATED, NOT ASSERTED
--------------------------------------
Difficulty is not eyeballed. The score is a weighted sum of four structural
components computed *only from the position* (never from the answer), and the
weights were fitted against an objective, label-dependent difficulty proxy:
does the strongest trivial reflex policy — "cast the most expensive affordable
creature, else the most expensive affordable spell" — reproduce the diamond+
player's actual choice?

Fitted on a 40,000-record sample (logistic regression on "reflex was wrong"):

    component   solo AUC   fitted weight   shipped weight
    branch        0.660        2.43            0.45
    cre_amb       0.679        1.41            0.20
    board         0.573        0.77            0.30
    rules         0.543       -0.11            0.05
    (menu_size    0.587  — the naive metric this beats)

Combined AUC 0.732 with the purely fitted weights, 0.722 with the shipped
weights, versus 0.659 for branching alone and 0.587 for menu size alone.

The shipped weights deliberately over-weight ``board`` relative to the fit,
because the 30-per-stage human read found the proxy's blind spot: a late-game
two-option decision on a twelve-permanent board ("which removal spell, and on
what") is *easy to guess* — the reflex is right most of the time when there
are only two options — but it is not an easy decision, and it was landing in
stage 1. Raising board 0.20 -> 0.30 cuts cluttered-board (>=6 nonland
permanents) records in stage 1 from 15.6% to 10.4% for 0.0035 of AUC, with the
stage accuracy ramp unchanged (82.7/59.2/40.8). ``rules`` earns ~nothing once
branching is controlled for and is kept only at a token weight. Every number
here is recomputed by ``--validate`` so the claim can be rechecked, not
trusted.

WHAT THIS CORPUS CANNOT STAGE
-----------------------------
The reference framework stages: creature-centric with reduced triggers ->
stack-based interaction and nuanced timing windows -> full card sets. Stage 2
of that ladder is *unrepresentable here*. Every record in ``strategic_casts``
is your own Main 1 with an empty stack, ``Pass`` is deliberately removed from
the menu, and no instant-speed window is ever offered. Measured consequence:
holding branching fixed, an affordable instant in the menu makes the reflex
*more* accurate, not less (n_aff=3: 62.8% with an instant vs 53.9% without) —
because "play the creature, hold the trick" is exactly what the reflex does.
So the stages below ramp board/branching/discrimination complexity, which this
data does support, and instant-presence is reported as a characteristic rather
than scored. Real timing complexity needs a source with response windows (the
Player.log / bridge decision corpus), not 17lands replay columns.

COMPONENTS
----------
``branch``   min(n_affordable, 6) / 6
    Affordable options, not menu size: an option you cannot pay for is not a
    decision. Affordability is approximate (see AFFORDABILITY MODEL) and falls
    back to the whole menu when it resolves to zero.
``board``    min(own_nonland_permanents + opp_nonland_permanents, 12) / 12
    Evaluation burden. Independently predictive at every branching level
    (4-14 points of reflex accuracy).
``cre_amb``  0.0 if exactly one creature is affordable, 0.5 if two, else 1.0
    Creature-discrimination burden. Exactly one affordable creature is the
    trivially-learnable case (82-92% reflex accuracy); zero creatures (pure
    spell judgment) and 3+ creatures (which body?) are the hard tails.
``rules``    0.5*min(max_triggers/3, 1) + 0.5*min(mean_oracle_chars/250, 1)
    Rules-text burden over the affordable options, from FULL Scryfall oracle
    text — note the prompt itself truncates oracle text at 160 chars, so a
    long-text option is information the model is missing, not just reading.

AFFORDABILITY MODEL (approximate, and measured)
-----------------------------------------------
mana value <= the prompt's "Mana:" figure, plus a greedy colour match of
coloured pips against the listed sources (hybrid = either half, phyrexian =
always payable). On the 40k sample the recorded pick is affordable 89.4% of
the time; 8.8% fail on mana value alone (cost reduction, alternative/warp
costs, unmodelled Treasure) and 1.8% only on colour. Records whose own answer
is unaffordable are counted and reported per stage — they are reconstruction
noise and a candidate filter for a later pass, not silently dropped here.

CORPUS STALENESS (checked, not assumed)
---------------------------------------
``strategic_casts.jsonl`` embeds ``AUTOPILOT_SYSTEM_PROMPT`` in every record.
The stored prompt is 7,433 chars; the current one is 8,983 — the STACK and
COMBAT SOLVER rules landed after the corpus was built. ``--check-staleness``
recomputes that comparison. The staging here is prompt-independent (it reads
the game state, not the system block), so the stage assignment survives a
rebuild, but the corpus itself must be regenerated before it is trained on.

USAGE
-----
    python -m tools.training.build_curriculum --validate
    python -m tools.training.build_curriculum --materialize
    python -m tools.training.build_curriculum --dump-examples 30

CPU only. No network. Reads the cached Scryfall bulk file already on disk.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Iterator, Optional

logger = logging.getLogger("build_curriculum")

_REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_CORPUS = _REPO_ROOT / "tools" / "training" / "data" / "strategic_casts.jsonl"
DEFAULT_OUT_DIR = _REPO_ROOT / "tools" / "training" / "data"
CARD_TEXT_PATH = Path.home() / ".arenamcp" / "cache" / "curriculum_card_text.json"
SCRYFALL_BULK = Path.home() / ".arenamcp" / "cache" / "scryfall" / "default_cards.json"

# Shipped weights. See the module docstring for the fit that produced them and
# for why ``board`` is deliberately above its fitted value.
WEIGHTS = {"branch": 0.45, "board": 0.30, "cre_amb": 0.20, "rules": 0.05}

STAGE_NAMES = {
    1: "curve",     # near-forced, creature-centric, small board
    2: "contest",   # real branching, mixed types, developed board
    3: "tangle",    # wide branching, dense board, spell-vs-creature judgment
}

PRIMARY_TYPES = (
    "creature",
    "instant",
    "sorcery",
    "artifact",
    "enchantment",
    "planeswalker",
    "battle",
    "land",
)

# --- prompt parsing ---------------------------------------------------------

MENU_RE = re.compile(r"^ {2}(\d+)\. (?:Cast (?P<name>.+)|(?P<pass>Pass))$")
TURN_RE = re.compile(r"^T(\d+) YOUR \| ")
MANA_RE = re.compile(r"^Mana: (\d+)(?: \(lands: (\d+), sources: (.*?)\))?")
LIFE_RE = re.compile(r"^Life: You=(-?\d+) Opp=(-?\d+)")
COUNT_RE = re.compile(r" x(\d+)\b")
TYPE_RE = re.compile(r"\(([^()]*)\)")
PIP_RE = re.compile(r"\{([^}]+)\}")

TRIGGER_RE = re.compile(r"\b(whenever|when |at the beginning of)", re.I)
MODAL_RE = re.compile(
    r"(choose one|choose two|choose one or more|kicker|escalate|entwine|"
    r"as an additional cost|you may pay|modes)",
    re.I,
)

ALL_COLOURS = frozenset("WUBRGC")


class ParseError(ValueError):
    """The rendered prompt did not have the shape this module expects."""


def parse_prompt(user: str) -> dict:
    """Pull the structured position back out of a rendered ``user`` message.

    The renderer lives in ``build_strategic_casts.render_user_message``; this
    is its inverse for the fields staging needs. It raises rather than
    silently defaulting, so a renderer change is a loud failure instead of a
    corpus of zeros.
    """
    menu: list[str] = []
    turn = mana = lands = None
    sources = ""
    life = opp_life = None
    section: Optional[str] = None
    board_you: list[str] = []
    board_opp: list[str] = []
    hand: list[str] = []

    for line in user.split("\n"):
        m = MENU_RE.match(line)
        if m:
            if m.group("name"):
                menu.append(m.group("name"))
            continue
        if line.startswith("YOUR BOARD:"):
            section = "you"
            continue
        if line.startswith("OPP BOARD:"):
            section = "opp"
            continue
        if line.startswith("HAND:"):
            section = "hand"
            continue
        if not line.strip():
            section = None
            continue
        if section and line.startswith("  "):
            body = line[2:]
            if body != "(empty)":
                {"you": board_you, "opp": board_opp, "hand": hand}[section].append(body)
            continue
        m = TURN_RE.match(line)
        if m:
            turn = int(m.group(1))
            continue
        m = MANA_RE.match(line)
        if m:
            mana = int(m.group(1))
            lands = int(m.group(2) or 0)
            sources = m.group(3) or ""
            continue
        m = LIFE_RE.match(line)
        if m:
            life, opp_life = int(m.group(1)), int(m.group(2))
            continue

    if not menu or turn is None or mana is None:
        raise ParseError("missing menu/turn/mana — renderer shape changed?")
    return {
        "menu": menu,
        "turn": turn,
        "mana": mana,
        "lands": lands or 0,
        "sources": sources,
        "life": life,
        "opp_life": opp_life,
        "board_you": board_you,
        "board_opp": board_opp,
        "hand": hand,
    }


def permanent_counts(board_lines: Iterable[str]) -> tuple[int, int]:
    """(nonland permanents, lands) — ``xN`` multipliers expanded."""
    nonland = lands = 0
    for line in board_lines:
        m = COUNT_RE.search(line)
        n = int(m.group(1)) if m else 1
        tm = TYPE_RE.search(line)
        type_line = (tm.group(1) if tm else "").lower()
        if "land" in type_line and "creature" not in type_line:
            lands += n
        else:
            nonland += n
    return nonland, lands


def parse_sources(text: str) -> list[set[str]]:
    """``"W U {B/G/R}"`` -> one colour set per untapped source."""
    out: list[set[str]] = []
    for tok in text.split():
        if not tok or tok == "?":
            out.append(set(ALL_COLOURS))
        elif tok.startswith("{"):
            out.append(set(tok.strip("{}").split("/")) or set(ALL_COLOURS))
        else:
            out.append({tok})
    return out


def cost_pips(mana_cost: str) -> list[set[str]]:
    """Coloured pips of a mana cost as colour sets (generic/X ignored)."""
    pips: list[set[str]] = []
    for piece in PIP_RE.findall(mana_cost or ""):
        if piece.isdigit() or piece == "X":
            continue
        if "/" in piece:
            parts = [p for p in piece.split("/") if not p.isdigit()]
            if "P" in parts:  # phyrexian — payable with life
                pips.append(set(ALL_COLOURS))
            else:
                pips.append(set(parts) or set(ALL_COLOURS))
        else:
            pips.append({piece})
    return pips


def is_affordable(mana_cost: str, cmc: Optional[float], mana: int, sources: list[set[str]]) -> bool:
    """Approximate castability. Documented error rate in the module docstring."""
    if cmc is None or cmc > mana:
        return False
    available = [set(s) for s in sources]
    for pip in sorted(cost_pips(mana_cost), key=len):  # most constrained first
        best = None
        for i, src in enumerate(available):
            if src & pip and (best is None or len(src) < len(available[best])):
                best = i
        if best is None:
            return False
        available.pop(best)
    return True


# --- card text index --------------------------------------------------------


def build_card_text_index(bulk: Path = SCRYFALL_BULK) -> dict[str, dict]:
    """name(lower) -> {t: oracle text, k: keywords, tl: type line, cmc, mc}.

    Streams the cached Scryfall bulk file line by line (it is ~600 MB and one
    JSON object per line) so peak memory stays in the tens of MB. Faces of
    split/MDFC cards are indexed under their own names too.
    """
    if not bulk.exists():
        raise FileNotFoundError(
            f"Scryfall bulk cache missing at {bulk}. This tool is offline-only; "
            "run anything that warms ScryfallCache first."
        )
    index: dict[str, dict] = {}
    with bulk.open(encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip().rstrip(",")
            if not raw or raw in ("[", "]"):
                continue
            try:
                card = json.loads(raw)
            except json.JSONDecodeError:
                continue
            name = card.get("name") or ""
            if not name:
                continue
            faces = card.get("card_faces") or []
            text = card.get("oracle_text") or ""
            type_line = card.get("type_line") or ""
            if not text and faces:
                text = " // ".join(f.get("oracle_text") or "" for f in faces).strip(" /")
            if not type_line and faces:
                type_line = " // ".join(f.get("type_line") or "" for f in faces)
            key = name.lower()
            if key not in index:
                index[key] = {
                    "t": " ".join(text.split()),
                    "k": list(card.get("keywords") or []),
                    "tl": type_line,
                    "cmc": card.get("cmc"),
                    "mc": card.get("mana_cost") or "",
                }
            for face in faces:
                fname = (face.get("name") or "").lower()
                if fname and fname not in index:
                    index[fname] = {
                        "t": " ".join((face.get("oracle_text") or "").split()),
                        "k": list(card.get("keywords") or []),
                        "tl": face.get("type_line") or type_line,
                        "cmc": card.get("cmc"),
                        "mc": face.get("mana_cost") or "",
                    }
    return index


def load_card_text_index(path: Path = CARD_TEXT_PATH, rebuild: bool = False) -> dict[str, dict]:
    if path.exists() and not rebuild:
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            logger.warning("card text index unreadable; rebuilding")
    index = build_card_text_index()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index))
    logger.info("built card text index: %d names -> %s", len(index), path)
    return index


# --- features ---------------------------------------------------------------


def option_features(name: str, card: Optional[dict], mana: int, sources: list[set[str]]) -> dict:
    if card is None:
        # Unresolved name: neutral, non-affordable, flagged. Measured rate on
        # the full corpus is reported as unresolved_option_names.
        return {
            "name": name, "types": [], "cmc": None, "affordable": False,
            "instant": False, "targets": False, "modal": False,
            "triggers": 0, "text_len": 0, "resolved": False,
        }
    type_line = (card.get("tl") or "").lower()
    text = card.get("t") or ""
    return {
        "name": name,
        "types": [t for t in PRIMARY_TYPES if t in type_line],
        "cmc": card.get("cmc"),
        "affordable": is_affordable(card.get("mc", ""), card.get("cmc"), mana, sources),
        "instant": ("instant" in type_line) or ("Flash" in (card.get("k") or [])),
        "targets": " target" in text.lower(),
        "modal": bool(MODAL_RE.search(text)) or "{X}" in (card.get("mc") or ""),
        "triggers": len(TRIGGER_RE.findall(text)),
        "text_len": len(text),
        "resolved": True,
    }


def decision_features(record: dict, cards: dict[str, dict]) -> dict:
    """Everything staging needs, from one corpus record."""
    parsed = parse_prompt(record["user"])
    sources = parse_sources(parsed["sources"])
    options = [
        option_features(name, cards.get(name.lower()), parsed["mana"], sources)
        for name in parsed["menu"]
    ]
    own_nonland, own_lands = permanent_counts(parsed["board_you"])
    opp_nonland, opp_lands = permanent_counts(parsed["board_opp"])

    affordable = [o for o in options if o["affordable"]]
    used_fallback = not affordable
    pool = affordable or options  # no affordable option => model failed; use menu

    n_creature = sum(1 for o in pool if "creature" in o["types"])
    meta = record.get("meta", {})
    pick_index = int(meta.get("pick_index", 0))
    pick_option = options[pick_index - 1] if 1 <= pick_index <= len(options) else None

    return {
        "id": record["id"],
        "record_key": meta.get("record_key"),
        "game_key": meta.get("game_key"),
        "expansion": meta.get("expansion"),
        "rank": meta.get("rank"),
        "turn": parsed["turn"],
        "mana": parsed["mana"],
        "lands": parsed["lands"],
        "life": parsed["life"],
        "opp_life": parsed["opp_life"],
        "menu_size": len(options),
        "n_affordable": len(affordable),
        "affordability_fallback": used_fallback,
        "n_creature_options": n_creature,
        "n_instant_options": sum(1 for o in pool if o["instant"]),
        "n_targeting_options": sum(1 for o in pool if o["targets"]),
        "n_modal_options": sum(1 for o in pool if o["modal"]),
        "n_distinct_types": len({o["types"][0] for o in pool if o["types"]}),
        "max_triggers": max((o["triggers"] for o in pool), default=0),
        "mean_oracle_chars": statistics.mean([o["text_len"] for o in pool]) if pool else 0.0,
        "own_nonland": own_nonland,
        "own_lands": own_lands,
        "opp_nonland": opp_nonland,
        "opp_lands": opp_lands,
        "board_nonland": own_nonland + opp_nonland,
        "hand_size": len(parsed["hand"]),
        "pick_index": pick_index,
        "pick_card": meta.get("pick_card"),
        "pick_affordable": bool(pick_option and pick_option["affordable"]),
        "pick_is_creature": bool(pick_option and "creature" in pick_option["types"]),
        "unresolved_options": sum(1 for o in options if not o["resolved"]),
        "_options": options,
    }


def complexity_components(f: dict) -> dict[str, float]:
    n_creature = f["n_creature_options"]
    cre_amb = 0.0 if n_creature == 1 else (0.5 if n_creature == 2 else 1.0)
    rules = 0.5 * min(f["max_triggers"] / 3.0, 1.0) + 0.5 * min(f["mean_oracle_chars"] / 250.0, 1.0)
    return {
        "branch": min(f["n_affordable"] or f["menu_size"], 6) / 6.0,
        "board": min(f["board_nonland"], 12) / 12.0,
        "cre_amb": cre_amb,
        "rules": rules,
    }


def complexity_score(components: dict[str, float]) -> float:
    return sum(WEIGHTS[k] * v for k, v in components.items())


# --- objective difficulty proxies (label-dependent; validation only) --------


def reflex_pick(f: dict) -> int:
    """"Cast the biggest affordable creature, else the biggest affordable spell."

    The strongest trivial policy measured on this corpus (61.1%). Ties break
    toward the earlier menu entry. Returns a 1-based menu index.
    """
    options = f["_options"]
    idx = [i for i, o in enumerate(options) if o["affordable"]] or list(range(len(options)))
    best = max(idx, key=lambda i: ("creature" in options[i]["types"], options[i]["cmc"] or 0, -i))
    return best + 1


def biggest_affordable_pick(f: dict) -> int:
    options = f["_options"]
    idx = [i for i, o in enumerate(options) if o["affordable"]] or list(range(len(options)))
    return max(idx, key=lambda i: (options[i]["cmc"] or 0, -i)) + 1


def first_affordable_pick(f: dict) -> int:
    options = f["_options"]
    idx = [i for i, o in enumerate(options) if o["affordable"]] or list(range(len(options)))
    return idx[0] + 1


def random_expectation(f: dict) -> float:
    n = f["n_affordable"] or f["menu_size"]
    return 1.0 / max(n, 1)


# --- corpus pass ------------------------------------------------------------


def iter_records(path: Path, stride: int = 1, limit: Optional[int] = None) -> Iterator[dict]:
    kept = 0
    with path.open(encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if stride > 1 and i % stride:
                continue
            yield json.loads(line)
            kept += 1
            if limit and kept >= limit:
                return


def extract(path: Path, cards: dict, stride: int, limit: Optional[int]) -> tuple[list[dict], Counter]:
    rows: list[dict] = []
    problems: Counter = Counter()
    t0 = time.time()
    for rec in iter_records(path, stride, limit):
        try:
            f = decision_features(rec, cards)
        except ParseError:
            problems["parse_error"] += 1
            continue
        if f["unresolved_options"]:
            problems["records_with_unresolved_option"] += 1
            problems["unresolved_option_names"] += f["unresolved_options"]
        if f["affordability_fallback"]:
            problems["no_affordable_option"] += 1
        if not f["pick_affordable"]:
            problems["pick_not_affordable"] += 1
        rows.append(f)
    logger.info("extracted %d decisions in %.1fs", len(rows), time.time() - t0)
    return rows, problems


def quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = q * (len(ordered) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return ordered[int(pos)]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def assign_stages(rows: list[dict], cuts: tuple[float, float]) -> tuple[float, float]:
    scores = [r["score"] for r in rows]
    t1, t2 = quantile(scores, cuts[0]), quantile(scores, cuts[1])
    for r in rows:
        r["stage"] = 1 if r["score"] <= t1 else (2 if r["score"] <= t2 else 3)
    return t1, t2


def auc(scores: list[float], labels: list[int]) -> float:
    """Rank AUC of ``scores`` against binary ``labels`` (ties averaged)."""
    pairs = sorted(zip(scores, labels))
    n_pos = sum(labels)
    n_neg = len(labels) - n_pos
    if not n_pos or not n_neg:
        return float("nan")
    rank_sum = 0.0
    i = 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        rank_sum += avg_rank * sum(lbl for _, lbl in pairs[i:j + 1])
        i = j + 1
    return (rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


# --- reporting --------------------------------------------------------------


def stage_summary(rows: list[dict]) -> dict:
    def frac(pred) -> float:
        return round(100.0 * sum(1 for r in rows if pred(r)) / len(rows), 1)

    def mean(key) -> float:
        return round(statistics.mean(r[key] for r in rows), 2)

    hits = sum(1 for r in rows if reflex_pick(r) == r["pick_index"])
    hits_big = sum(1 for r in rows if biggest_affordable_pick(r) == r["pick_index"])
    hits_first = sum(1 for r in rows if first_affordable_pick(r) == r["pick_index"])
    rand = sum(random_expectation(r) for r in rows)
    n = len(rows)
    return {
        "n": n,
        "score_min": round(min(r["score"] for r in rows), 4),
        "score_max": round(max(r["score"] for r in rows), 4),
        "score_mean": round(statistics.mean(r["score"] for r in rows), 4),
        "menu_size_mean": mean("menu_size"),
        "n_affordable_mean": mean("n_affordable"),
        "n_affordable_dist": dict(sorted(Counter(r["n_affordable"] for r in rows).items())),
        "creature_options_dist": dict(sorted(Counter(r["n_creature_options"] for r in rows).items())),
        "board_nonland_mean": mean("board_nonland"),
        "opp_nonland_mean": mean("opp_nonland"),
        "turn_mean": mean("turn"),
        "turn_median": statistics.median(r["turn"] for r in rows),
        "hand_size_mean": mean("hand_size"),
        "max_triggers_mean": mean("max_triggers"),
        "mean_oracle_chars": round(statistics.mean(r["mean_oracle_chars"] for r in rows), 1),
        "pct_with_instant_option": frac(lambda r: r["n_instant_options"] > 0),
        "pct_with_targeting_option": frac(lambda r: r["n_targeting_options"] > 0),
        "pct_with_modal_option": frac(lambda r: r["n_modal_options"] > 0),
        "pct_multi_type_menu": frac(lambda r: r["n_distinct_types"] >= 2),
        "pct_answer_is_creature": frac(lambda r: r["pick_is_creature"]),
        "pct_pick_unaffordable": frac(lambda r: not r["pick_affordable"]),
        "pct_no_affordable_option": frac(lambda r: r["affordability_fallback"]),
        "reflex_accuracy_pct": round(100.0 * hits / n, 1),
        "biggest_affordable_accuracy_pct": round(100.0 * hits_big / n, 1),
        "first_affordable_accuracy_pct": round(100.0 * hits_first / n, 1),
        "random_baseline_pct": round(100.0 * rand / n, 1),
        "reflex_lift_over_random_pts": round(100.0 * (hits / n - rand / n), 1),
        "distinct_games": len({r["game_key"] for r in rows}),
        "expansions": dict(Counter(r["expansion"] for r in rows).most_common(6)),
    }


def validation_block(rows: list[dict]) -> dict:
    """Recompute the AUC table from the module docstring on this run's data."""
    wrong = [0 if reflex_pick(r) == r["pick_index"] else 1 for r in rows]
    comps = [r["components"] for r in rows]
    out = {
        "proxy": "reflex policy = biggest affordable creature, else biggest affordable spell",
        "proxy_error_rate_pct": round(100.0 * sum(wrong) / len(rows), 1),
        "auc_full_score": round(auc([r["score"] for r in rows], wrong), 4),
        "auc_menu_size_only": round(auc([float(r["menu_size"]) for r in rows], wrong), 4),
        "auc_by_component": {
            k: round(auc([c[k] for c in comps], wrong), 4) for k in WEIGHTS
        },
        "weights": WEIGHTS,
    }
    return out


def stratified_check(rows: list[dict]) -> dict:
    """Reflex accuracy per (n_affordable, signal) — the honesty check that a
    component is not just branching in disguise."""
    out: dict[str, dict] = {}
    for label, fn in (
        ("board_ge_6", lambda r: int(r["board_nonland"] >= 6)),
        ("has_instant_option", lambda r: int(r["n_instant_options"] > 0)),
        ("max_triggers_ge_2", lambda r: int(r["max_triggers"] >= 2)),
        ("creature_options", lambda r: min(r["n_creature_options"], 3)),
    ):
        agg: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for r in rows:
            key = f"n_aff={min(r['n_affordable'] or r['menu_size'], 5)},{label}={fn(r)}"
            cell = agg[key]
            cell[0] += reflex_pick(r) == r["pick_index"]
            cell[1] += 1
        out[label] = {
            k: {"reflex_accuracy_pct": round(100.0 * h / c, 1), "n": c}
            for k, (h, c) in sorted(agg.items())
            if c >= 100
        }
    return out


def check_staleness() -> dict:
    """Is the corpus' embedded system prompt still the current one?"""
    result: dict = {}
    try:
        sys.path[:0] = [str(_REPO_ROOT), str(_REPO_ROOT / "src")]
        from tools.training.build_strategic_casts import SYSTEM_PROMPT  # noqa: PLC0415

        with DEFAULT_CORPUS.open(encoding="utf-8") as fh:
            stored = json.loads(fh.readline())["system"]
        result = {
            "stored_chars": len(stored),
            "current_chars": len(SYSTEM_PROMPT),
            "identical": stored == SYSTEM_PROMPT,
            "missing_from_corpus": [
                line for line in SYSTEM_PROMPT.split("\n")
                if line.startswith("- ") and line not in stored
            ],
        }
    except Exception as exc:  # noqa: BLE001
        result = {"error": repr(exc)}
    return result


# --- outputs ----------------------------------------------------------------


def write_index(rows: list[dict], path: Path) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps({
                "id": r["id"],
                "record_key": r["record_key"],
                "stage": r["stage"],
                "stage_name": STAGE_NAMES[r["stage"]],
                "score": round(r["score"], 4),
                "components": {k: round(v, 4) for k, v in r["components"].items()},
                "n_affordable": r["n_affordable"],
                "menu_size": r["menu_size"],
                "n_creature_options": r["n_creature_options"],
                "board_nonland": r["board_nonland"],
                "max_triggers": r["max_triggers"],
                "turn": r["turn"],
                "pick_affordable": r["pick_affordable"],
            }, ensure_ascii=False) + "\n")


def materialize(corpus: Path, rows: list[dict], out_dir: Path, prefix: str) -> dict[int, int]:
    """Write full per-stage corpora, adding ``meta.curriculum`` to each record."""
    stage_of = {r["id"]: r for r in rows}
    handles = {
        st: (out_dir / f"{prefix}{st}_{STAGE_NAMES[st]}.jsonl").open("w", encoding="utf-8")
        for st in (1, 2, 3)
    }
    counts: Counter = Counter()
    try:
        with corpus.open(encoding="utf-8") as fh:
            for line in fh:
                rec = json.loads(line)
                row = stage_of.get(rec["id"])
                if row is None:
                    continue
                rec["meta"]["curriculum"] = {
                    "stage": row["stage"],
                    "stage_name": STAGE_NAMES[row["stage"]],
                    "score": round(row["score"], 4),
                    "components": {k: round(v, 4) for k, v in row["components"].items()},
                }
                handles[row["stage"]].write(json.dumps(rec, ensure_ascii=False) + "\n")
                counts[row["stage"]] += 1
    finally:
        for h in handles.values():
            h.close()
    return dict(counts)


def dump_examples(corpus: Path, rows: list[dict], n: int, stages: tuple[int, ...], out: Path, seed: int) -> None:
    """Sample ``n`` decisions per stage and write their full prompts out for
    human inspection. This is the sanity check the metric does not get to
    self-certify."""
    rng = random.Random(seed)
    wanted: dict[str, dict] = {}
    for st in stages:
        pool = [r for r in rows if r["stage"] == st]
        for r in rng.sample(pool, min(n, len(pool))):
            wanted[r["id"]] = r
    found: dict[str, dict] = {}
    with corpus.open(encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            if rec["id"] in wanted:
                found[rec["id"]] = rec
                if len(found) == len(wanted):
                    break
    with out.open("w", encoding="utf-8") as fh:
        for st in stages:
            for rid, row in wanted.items():
                if row["stage"] != st or rid not in found:
                    continue
                rec = found[rid]
                fh.write("=" * 100 + "\n")
                fh.write(
                    f"STAGE {st} ({STAGE_NAMES[st]})  score={row['score']:.3f}  "
                    f"components={ {k: round(v,2) for k,v in row['components'].items()} }\n"
                    f"n_affordable={row['n_affordable']}/{row['menu_size']}  "
                    f"creature_options={row['n_creature_options']}  "
                    f"board_nonland={row['board_nonland']}  turn={row['turn']}\n"
                )
                fh.write(rec["user"] + "\n")
                fh.write(f"ANSWER: {rec['response']}  ({rec['meta']['pick_card']})\n")
                fh.write(f"reflex_would_pick: {reflex_pick(row)}\n")


# --- CLI --------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--prefix", default="strategic_casts_stage")
    ap.add_argument("--stride", type=int, default=1, help="read every Nth record")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--cuts", default="0.3333,0.6667",
                    help="score quantiles separating stage 1|2 and 2|3")
    ap.add_argument("--materialize", action="store_true",
                    help="write full per-stage corpora (~2.4 GB) as well as the index")
    ap.add_argument("--no-index", action="store_true")
    ap.add_argument("--validate", action="store_true",
                    help="recompute the AUC table and the stratified honesty check")
    ap.add_argument("--no-staleness-check", dest="check_staleness", action="store_false",
                    help="skip the system-prompt staleness comparison (it imports the builder)")
    ap.add_argument("--dump-examples", type=int, default=0,
                    help="sample N decisions per stage into a human-readable file")
    ap.add_argument("--example-stages", default="1,3")
    ap.add_argument("--rebuild-card-index", action="store_true")
    ap.add_argument("--seed", type=int, default=20260726)
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    cuts = tuple(float(x) for x in args.cuts.split(","))
    if len(cuts) != 2 or not 0 < cuts[0] < cuts[1] < 1:
        raise SystemExit("--cuts must be two increasing quantiles in (0,1)")

    cards = load_card_text_index(rebuild=args.rebuild_card_index)
    logger.info("card text index: %d names", len(cards))

    rows, problems = extract(args.corpus, cards, args.stride, args.limit)
    if not rows:
        raise SystemExit("no records extracted")
    for r in rows:
        r["components"] = complexity_components(r)
        r["score"] = complexity_score(r["components"])
    t1, t2 = assign_stages(rows, cuts)

    scores = [r["score"] for r in rows]
    report = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "corpus": str(args.corpus),
        "records_read": len(rows),
        "stride": args.stride,
        "weights": WEIGHTS,
        "cuts": {"quantiles": list(cuts), "score_thresholds": [round(t1, 4), round(t2, 4)]},
        "score_distribution": {
            "mean": round(statistics.mean(scores), 4),
            "stdev": round(statistics.pstdev(scores), 4),
            "min": round(min(scores), 4),
            "p10": round(quantile(scores, 0.10), 4),
            "p25": round(quantile(scores, 0.25), 4),
            "median": round(quantile(scores, 0.50), 4),
            "p75": round(quantile(scores, 0.75), 4),
            "p90": round(quantile(scores, 0.90), 4),
            "max": round(max(scores), 4),
            "histogram": dict(sorted(Counter(round(s, 1) for s in scores).items())),
        },
        "component_means": {
            k: round(statistics.mean(r["components"][k] for r in rows), 4) for k in WEIGHTS
        },
        "data_quality": dict(problems),
        "stages": {
            f"{st}_{STAGE_NAMES[st]}": stage_summary([r for r in rows if r["stage"] == st])
            for st in (1, 2, 3)
        },
    }
    if args.check_staleness:
        report["system_prompt_staleness"] = check_staleness()
    if args.validate:
        report["validation"] = validation_block(rows)
        report["stratified_check"] = stratified_check(rows)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.out_dir / "curriculum_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    logger.info("report -> %s", report_path)

    if not args.no_index:
        index_path = args.out_dir / "strategic_casts_curriculum_index.jsonl"
        write_index(rows, index_path)
        logger.info("index -> %s (%d rows)", index_path, len(rows))

    if args.materialize:
        counts = materialize(args.corpus, rows, args.out_dir, args.prefix)
        logger.info("materialized stage corpora: %s", counts)

    if args.dump_examples:
        stages = tuple(int(x) for x in args.example_stages.split(","))
        out = args.out_dir / "curriculum_examples.txt"
        dump_examples(args.corpus, rows, args.dump_examples, stages, out, args.seed)
        logger.info("examples -> %s", out)

    for st in (1, 2, 3):
        s = report["stages"][f"{st}_{STAGE_NAMES[st]}"]
        logger.info(
            "stage %d (%-7s) n=%-7d score<=%.3f  n_aff=%.2f board=%.1f creat=%.2f  "
            "reflex=%.1f%% (random %.1f%%)",
            st, STAGE_NAMES[st], s["n"],
            s["score_max"], s["n_affordable_mean"], s["board_nonland_mean"],
            statistics.mean(r["n_creature_options"] for r in rows if r["stage"] == st),
            s["reflex_accuracy_pct"], s["random_baseline_pct"],
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
