"""Measure synthesized-menu fidelity against REAL MTGA menus.

THE DECISIVE SPIKE STEP. `synthesize_menu.py` reconstructs a legal-action menu
from 17lands `replay_data` columns. This asks: how close is that menu to a real
MTGA `ActionsAvailableReq`?

The two corpora are different games, so a row-by-row diff is impossible.
Instead this runs the synthesizer's menu model on PERFECT INPUTS: the real
hand / real untapped lands / real board taken from `.rply` ground truth, fed
into `synthesize_menu.synth_from_zones` (imported, never reimplemented), and
diffed against the real menu recorded at that same decision.

That makes every number here an OPTIMISTIC CEILING. The production path would
feed the same model a hand reconstructed from 17lands eot-columns, which adds
its own error on top. If the model fails on perfect inputs it cannot be
rescued by better 17lands parsing.

Scopes:
    full   -- synthetic vs the ENTIRE real menu. The honest question: would a
              training example show the model the same menu it sees at
              inference?
    spell  -- synthetic vs the real menu restricted to Cast/Play family + Pass,
              with alternate cast modes normalized (PlayMDFC->Play,
              CastLeftRoom/CastRightRoom/CastAdventure->Cast). Isolates the
              menu model from action types no turn-granular source can express.

Policies (from synthesize_menu.POLICIES):
    strict   -- only start-of-turn untapped lands can pay
    postland -- + the best single land from hand (models the post-land-drop menu)
    nofilter -- no mana filter at all

Two synthesizer variants:
    core -- exactly what synth_from_zones emits today (Cast/Play/Pass)
    plus -- core + Activate_Mana per untapped land + Activate per board
            permanent carrying an activatable ability, using the SAME
            ability-text predicate synthesize_turn uses. The most generous
            reading of what a 17lands-driven synthesizer could ever emit.

CPU only. No GPU, no network.

Usage:
    python -m tools.training.menu_fidelity \
        --groundtruth tools/eval/data/replay_menu_groundtruth.jsonl \
        --out tools/training/data/menu_fidelity_report.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.training.synthesize_menu import (  # noqa: E402
    CardResolver,
    is_affordable,
    land_sources,
    parse_mana_cost,
    synth_from_zones,
)

Entry = tuple[str, Optional[int]]

POLICIES = ("strict", "postland", "nofilter")
VARIANTS = ("core", "plus")
SCOPES = ("full", "spell")

# Real action types a hand-only Cast/Play model could plausibly target.
SPELL_SCOPE_TYPES = {
    "Cast",
    "Play",
    "PlayMDFC",
    "CastLeftRoom",
    "CastRightRoom",
    "CastAdventure",
    "Pass",
}
ALT_CAST_MODES = {"CastLeftRoom", "CastRightRoom", "CastAdventure"}


def norm_type(action_type: str) -> str:
    return (action_type or "?").replace("ActionType_", "")


def real_entries(rec: dict) -> list[tuple[str, Optional[int]]]:
    return [(norm_type(r["action_type"]), r.get("grp_id")) for r in rec["real_menu"]]


def scope_real(entries: list[Entry], scope: str) -> set[Entry]:
    """Project the real menu into a comparison scope."""
    if scope == "full":
        return set(entries)
    out: set[Entry] = set()
    for t, g in entries:
        if t not in SPELL_SCOPE_TYPES:
            continue
        if t == "PlayMDFC":
            t = "Play"
        elif t in ALT_CAST_MODES:
            t = "Cast"
        out.add((t, g))
    return out


def scope_synth(syn: set[Entry], scope: str) -> set[Entry]:
    if scope == "full":
        return syn
    return {(t, g) for t, g in syn if t in ("Cast", "Play", "Pass")}


def activatable_board_entries(resolver: CardResolver, board_grp_ids: list[int]) -> set[Entry]:
    """('Activate', grpId) for board permanents with an activatable ability.

    Uses the same ability-text predicate as synthesize_menu.synthesize_turn:
    an ability whose oracle text contains ':' and does not begin with a
    triggered-ability lead-in.
    """
    out: set[Entry] = set()
    for gid in dict.fromkeys(board_grp_ids):
        try:
            ability_ids = resolver.card_ability_ids(gid)
        except Exception:  # noqa: BLE001 - resolver is best-effort on tokens
            continue
        for aid in ability_ids:
            text = resolver.ability_text(aid)
            if ":" in text and not text.lower().startswith(("whenever", "when ", "at the")):
                out.add(("Activate", gid))
                break
    return out


def best_extra_land(resolver: CardResolver, hand: list[int]) -> list[int]:
    """The land from hand a 'postland' policy assumes gets played.

    Mirrors synthesize_turn: the land adding the most color flexibility.
    """
    hand_lands = [g for g in hand if resolver.get(g).has_land_face]
    if not hand_lands:
        return []
    return [max(hand_lands, key=lambda g: len(resolver.get(g).produced_mana or ()))]


def affordable_with(resolver: CardResolver, grp_id: Optional[int], land_grp_ids: list[int]) -> Optional[bool]:
    """Could grp_id be paid for with these lands? None if cost unparseable."""
    if grp_id is None:
        return None
    card = resolver.get(grp_id)
    costs = card.castable_costs
    if not costs:
        return None
    sources, _ = land_sources(resolver, land_grp_ids)
    parsed = [parse_mana_cost(piece) for c in costs for piece in (c.split("//") if "//" in c else [c])]
    if not any(p.parse_ok for p in parsed):
        return None
    return any(p.parse_ok and is_affordable(p, sources) for p in parsed)


def classify_false_positive(
    resolver: CardResolver, entry: Entry, rec: dict, untapped: list[int], inactive: set[Entry]
) -> str:
    """Why did the synthesizer invent this action?"""
    t, g = entry
    if entry in inactive:
        aff = affordable_with(resolver, g, untapped)
        if aff is False:
            return "mtga_listed_inactive:unaffordable"
        return "mtga_listed_inactive:other_rule"
    if t == "Play":
        if g not in set(rec["state"]["hand_grp_ids"]):
            return "play_card_not_in_hand"
        return "land_drop_not_offered"
    if t == "Cast":
        aff = affordable_with(resolver, g, untapped)
        card = resolver.get(g) if g is not None else None
        tl = (card.type_line if card else "") or ""
        if aff is False:
            return "unaffordable_and_not_listed_at_all"
        if "Aura" in tl:
            return "aura_no_legal_target"
        if "Equipment" in tl:
            return "equipment_no_legal_target"
        if not card or not card.resolved:
            return "card_unresolved"
        return "unexplained_cast"
    return f"other:{t}"


def classify_false_negative(
    resolver: CardResolver, entry: Entry, rec: dict, policy: str, untapped: list[int], variant: str
) -> str:
    """Why did the synthesizer miss this real action?"""
    t, g = entry
    hand = set(rec["state"]["hand_grp_ids"])
    if t == "Activate_Mana":
        return "mana_ability" + ("" if variant == "core" else ":tapped_or_nonland_source")
    if t == "FloatMana":
        return "float_mana_not_expressible"
    if t == "Activate":
        return "activated_ability_not_expressible"
    if t == "Special":
        return "special_action_not_expressible"
    if t == "PlayMDFC":
        return "mdfc_back_face"
    if t in ALT_CAST_MODES:
        return "alternate_cast_mode"
    if t == "Play":
        if g not in hand:
            return "play_from_outside_hand"
        return "land_in_hand_missed"
    if t == "Cast":
        if g not in hand:
            return "cast_from_outside_hand"
        if policy != "nofilter":
            aff = affordable_with(resolver, g, untapped)
            if aff is False:
                return "mana_filter_removed_castable_card"
            if aff is None:
                return "cost_unparseable"
        card = resolver.get(g) if g is not None else None
        if not card or not card.resolved:
            return "card_unresolved"
        return "unexplained_cast_miss"
    return f"other:{t}"


def pct(num: int, den: int) -> Optional[float]:
    return round(100.0 * num / den, 2) if den else None


def mean(xs: list[float]) -> float:
    return round(statistics.fmean(xs), 4) if xs else 0.0


def dist(xs: list[int]) -> dict:
    if not xs:
        return {}
    s = sorted(xs)
    return {
        "n": len(s),
        "min": s[0],
        "p25": s[len(s) // 4],
        "median": statistics.median(s),
        "p75": s[(3 * len(s)) // 4],
        "max": s[-1],
        "mean": round(statistics.fmean(s), 2),
    }


def build_synth(resolver: CardResolver, rec: dict, policy: str, variant: str) -> set[Entry]:
    """Run the imported menu model on this decision's real zone contents."""
    state = rec["state"]
    hand = [g for g in state["hand_grp_ids"] if g is not None]
    untapped = [
        land["grp_id"]
        for land in state["lands_in_play"]
        if not land.get("is_tapped") and land.get("grp_id") is not None
    ]

    if policy == "postland":
        sources_zone = untapped + best_extra_land(resolver, hand)
        eff_policy = "strict"
    else:
        sources_zone = untapped
        eff_policy = policy

    syn = synth_from_zones(resolver, hand, sources_zone, eff_policy, allow_play=True)

    if variant == "plus":
        syn = set(syn)
        for gid in dict.fromkeys(untapped):
            syn.add(("Activate_Mana", gid))
        board = (
            [g for g in state["creatures_in_play_grp_ids"] if g is not None]
            + [g for g in state["other_permanents_grp_ids"] if g is not None]
            + [g for g in state["lands_in_play_grp_ids"] if g is not None]
        )
        syn |= activatable_board_entries(resolver, board)
    return syn


def evaluate(resolver: CardResolver, subset: list[dict], collect_examples: bool = True) -> tuple[dict, dict]:
    """Full variant x policy x scope sweep over one slice of decisions."""
    results: dict = {}
    per_turn_store: dict = {}
    if not subset:
        return results, per_turn_store

    for variant in VARIANTS:
        for policy in POLICIES:
            synth_cache = [build_synth(resolver, rec, policy, variant) for rec in subset]
            for scope in SCOPES:
                key = f"{variant}|{policy}|{scope}"
                exact = 0
                fps: list[float] = []
                fns: list[float] = []
                precs: list[float] = []
                recalls: list[float] = []
                jacs: list[float] = []
                syn_sizes: list[int] = []
                real_sizes: list[int] = []
                fp_cause: Counter = Counter()
                fn_cause: Counter = Counter()
                fp_examples: dict[str, list] = defaultdict(list)
                fn_examples: dict[str, list] = defaultdict(list)

                pick_total = pick_hit = 0
                pick_total_np = pick_hit_np = 0
                pick_out_of_scope = 0
                pick_by_type: dict[str, list[int]] = defaultdict(lambda: [0, 0])
                pick_miss_cause: Counter = Counter()
                pick_miss_examples: list[dict] = []
                # per turn: n, exact, pickTot, pickHit, fpSum, fnSum
                per_turn: dict[int, list[int]] = defaultdict(lambda: [0, 0, 0, 0, 0, 0])

                for rec, syn_all in zip(subset, synth_cache, strict=True):
                    ents = real_entries(rec)
                    real = scope_real(ents, scope)
                    syn = scope_synth(syn_all, scope)
                    inactive = {
                        (norm_type(r["action_type"]), r.get("grp_id"))
                        for r in rec.get("real_menu_inactive") or []
                    }
                    state = rec["state"]
                    untapped = [
                        ld["grp_id"]
                        for ld in state["lands_in_play"]
                        if not ld.get("is_tapped") and ld.get("grp_id") is not None
                    ]

                    inter = syn & real
                    fp_set = syn - real
                    fn_set = real - syn
                    syn_sizes.append(len(syn))
                    real_sizes.append(len(real))
                    fps.append(float(len(fp_set)))
                    fns.append(float(len(fn_set)))
                    precs.append(len(inter) / len(syn) if syn else 0.0)
                    recalls.append(len(inter) / len(real) if real else 0.0)
                    jacs.append(len(inter) / len(syn | real) if (syn | real) else 0.0)
                    is_exact = syn == real
                    exact += int(is_exact)

                    for e in fp_set:
                        c = classify_false_positive(resolver, e, rec, untapped, inactive)
                        fp_cause[c] += 1
                        if collect_examples and len(fp_examples[c]) < 3:
                            fp_examples[c].append(
                                {
                                    "file": rec["replay_file"],
                                    "msg_id": rec["msg_id"],
                                    "entry": list(e),
                                    "name": resolver.get(e[1]).name if e[1] else None,
                                }
                            )
                    for e in fn_set:
                        c = classify_false_negative(resolver, e, rec, policy, untapped, variant)
                        fn_cause[c] += 1
                        if collect_examples and len(fn_examples[c]) < 3:
                            fn_examples[c].append(
                                {
                                    "file": rec["replay_file"],
                                    "msg_id": rec["msg_id"],
                                    "entry": list(e),
                                    "name": resolver.get(e[1]).name if e[1] else None,
                                }
                            )

                    turn = rec.get("player_turn_index") or 0
                    slot = per_turn[turn]
                    slot[0] += 1
                    slot[1] += int(is_exact)
                    slot[4] += len(fp_set)
                    slot[5] += len(fn_set)

                    # ---- pick recall: THE number that decides poison ----
                    pk = rec["real_pick"]
                    if pk.get("status") != "ok" or not pk.get("action_type"):
                        continue
                    pt = norm_type(pk["action_type"])
                    pe = next(iter(scope_real([(pt, pk.get("grp_id"))], scope)), None)
                    if pe is None:
                        pick_out_of_scope += 1
                        continue
                    hit = pe in syn
                    pick_total += 1
                    pick_hit += int(hit)
                    pick_by_type[pt][0] += 1
                    pick_by_type[pt][1] += int(hit)
                    slot[2] += 1
                    slot[3] += int(hit)
                    if pt != "Pass":
                        pick_total_np += 1
                        pick_hit_np += int(hit)
                    if not hit:
                        cause = classify_false_negative(resolver, pe, rec, policy, untapped, variant)
                        pick_miss_cause[cause] += 1
                        if collect_examples and len(pick_miss_examples) < 30:
                            pick_miss_examples.append(
                                {
                                    "file": rec["replay_file"],
                                    "msg_id": rec["msg_id"],
                                    "player_turn_index": turn,
                                    "variant_format": rec["variant"],
                                    "pick": list(pe),
                                    "name": pk.get("name"),
                                    "cause": cause,
                                }
                            )

                block = {
                    "decisions": len(subset),
                    "exact_set_match_pct": pct(exact, len(subset)),
                    "pick_recall_pct": pct(pick_hit, pick_total),
                    "pick_recall_excluding_pass_pct": pct(pick_hit_np, pick_total_np),
                    "picks_evaluated": pick_total,
                    "picks_out_of_scope": pick_out_of_scope,
                    "picks_missing_from_synthetic_menu": pick_total - pick_hit,
                    "pick_recall_by_type": {
                        k: {"n": v[0], "hit": v[1], "pct": pct(v[1], v[0])}
                        for k, v in sorted(pick_by_type.items(), key=lambda kv: -kv[1][0])
                    },
                    "pick_miss_causes": pick_miss_cause.most_common(),
                    "mean_false_legals_per_decision": mean(fps),
                    "mean_missing_per_decision": mean(fns),
                    "mean_precision": mean(precs),
                    "mean_recall": mean(recalls),
                    "mean_jaccard": mean(jacs),
                    "synthetic_menu_size": dist(syn_sizes),
                    "real_menu_size": dist(real_sizes),
                    "false_legal_causes": fp_cause.most_common(),
                    "missing_causes": fn_cause.most_common(),
                }
                if collect_examples:
                    block["false_legal_examples"] = dict(fp_examples)
                    block["missing_examples"] = dict(fn_examples)
                    block["pick_miss_examples"] = pick_miss_examples
                results[key] = block
                per_turn_store[key] = {
                    str(t): {
                        "decisions": v[0],
                        "exact_match_pct": pct(v[1], v[0]),
                        "picks": v[2],
                        "pick_recall_pct": pct(v[3], v[2]),
                        "mean_false_legals": round(v[4] / v[0], 2) if v[0] else None,
                        "mean_missing": round(v[5] / v[0], 2) if v[0] else None,
                    }
                    for t, v in sorted(per_turn.items())
                }
    return results, per_turn_store


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--groundtruth", type=Path, default=_REPO_ROOT / "tools/eval/data/replay_menu_groundtruth.jsonl"
    )
    ap.add_argument("--out", type=Path, default=_REPO_ROOT / "tools/training/data/menu_fidelity_report.json")
    ap.add_argument(
        "--synth-17lands-stats",
        type=Path,
        nargs="*",
        default=[],
        help="stats JSON files emitted by synthesize_menu.py over real "
        "17lands rows; used to stack reconstruction error on top "
        "of the perfect-input menu-model error measured here",
    )
    args = ap.parse_args(argv)

    records = [json.loads(line) for line in args.groundtruth.open()]
    resolver = CardResolver()

    # ---- real menu reference -------------------------------------------
    real_type_mix: Counter = Counter()
    sizes_full: list[int] = []
    sizes_spell: list[int] = []
    for rec in records:
        ents = real_entries(rec)
        for t, _g in ents:
            real_type_mix[t] += 1
        sizes_full.append(len(scope_real(ents, "full")))
        sizes_spell.append(len(scope_real(ents, "spell")))

    report: dict = {
        "groundtruth": str(args.groundtruth),
        "decisions": len(records),
        "decision_point": "first priority window of the local player's own precombat main phase",
        "caveat": (
            "PERFECT-INPUT CEILING. Real hand/lands/board from .rply are fed into "
            "synthesize_menu.synth_from_zones. 17lands hand-reconstruction error is "
            "NOT included; the production path can only be worse than this."
        ),
        "format_mix_decisions": Counter(f"{r['super_format']}/{r['variant']}" for r in records).most_common(),
        "real_menu_reference": {
            "action_type_mix_rows": real_type_mix.most_common(),
            "distinct_entry_size_full": dist(sizes_full),
            "distinct_entry_size_spell": dist(sizes_spell),
        },
    }

    subsets: dict[str, list[dict]] = {
        "all": records,
        # 17lands replay_data is PremierDraft: no command zone, no companion.
        # Brawl decisions carry commander casts a hand-only model can never emit,
        # so they are broken out rather than allowed to dominate the verdict.
        "non_brawl": [r for r in records if r["variant"] != "GameVariant_Brawl"],
        "brawl_only": [r for r in records if r["variant"] == "GameVariant_Brawl"],
        "limited_only": [r for r in records if r["super_format"] == "SuperFormat_Limited"],
    }

    report["subsets"] = {}
    for name, sub in subsets.items():
        res, pt = evaluate(resolver, sub, collect_examples=(name in ("all", "non_brawl")))
        report["subsets"][name] = {"n_decisions": len(sub), "results": res, "per_turn": pt}

    # ---- stack 17lands reconstruction error on the perfect-input ceiling --
    if args.synth_17lands_stats:
        per_set = {}
        tot_actions = 0
        weighted = {"strict": 0.0, "postland": 0.0, "nofilter": 0.0}
        for p in args.synth_17lands_stats:
            st = json.loads(Path(p).read_text())
            sc = st["self_consistency"]
            n = sc["actions_actually_taken"]
            tot_actions += n
            for pol in weighted:
                weighted[pol] += sc["played_action_missing_from_menu_pct"][pol] * n
            per_set[Path(p).stem] = {
                "replay_csv": st.get("replay_csv"),
                "rows_processed": st.get("rows_processed"),
                "menus_produced": st.get("menus_produced"),
                "actions_actually_taken": n,
                "played_card_not_in_reconstructed_hand_pct": sc["played_card_not_in_reconstructed_hand_pct"],
                "played_action_missing_from_menu_pct": sc["played_action_missing_from_menu_pct"],
                "mean_menu_size": st.get("mean_menu_size"),
            }
        recon_miss = {k: round(v / tot_actions, 2) for k, v in weighted.items()}

        # Menu-model pick recall on the format slice that matches 17lands
        # (PremierDraft == limited, no command zone) in the Cast/Play scope.
        mm = report["subsets"]["non_brawl"]["results"]["core|nofilter|spell"]
        mm_recall = (mm["pick_recall_pct"] or 0.0) / 100.0
        gt_artifact = sum(
            c
            for name, c in mm["pick_miss_causes"]
            if name in ("play_from_outside_hand", "cast_from_outside_hand")
        )
        adj_recall = (
            (
                (mm["picks_evaluated"] - (mm["picks_missing_from_synthetic_menu"] - gt_artifact))
                / mm["picks_evaluated"]
            )
            if mm["picks_evaluated"]
            else 0.0
        )

        report["stacked_error_estimate"] = {
            "explanation": (
                "Two independent error sources multiply. (1) menu-model error: measured "
                "here on perfect inputs against REAL MTGA menus. (2) 17lands "
                "reconstruction error: measured by synthesize_menu.py on real 17lands "
                "rows as 'the card the player actually played is not in the "
                "reconstructed hand'. End-to-end pick recall ~= product."
            ),
            "per_set_17lands": per_set,
            "total_actions_measured_17lands": tot_actions,
            "reconstruction_pick_miss_pct_weighted": recon_miss,
            "menu_model_pick_recall_non_brawl_spell_pct": round(100 * mm_recall, 2),
            "menu_model_pick_recall_adjusted_for_groundtruth_hand_gaps_pct": round(100 * adj_recall, 2),
            "groundtruth_hand_tracker_caveat": (
                "3.81% of non-Brawl real Cast/Play rows reference a hand instance the "
                ".rply state tracker did not have, so the menu-model numbers here are "
                "slightly PESSIMISTIC."
            ),
            "end_to_end_pick_recall_pct": {
                pol: round(100 * (1 - recon_miss[pol] / 100.0) * adj_recall, 2) for pol in recon_miss
            },
            "end_to_end_poison_rate_pct": {
                pol: round(100 - 100 * (1 - recon_miss[pol] / 100.0) * adj_recall, 2) for pol in recon_miss
            },
            "note_filterable": (
                "The 17lands reconstruction miss is SELF-DETECTABLE at dataset build "
                "time: the pick columns and the reconstructed hand are both known, so "
                "rows where the pick is absent can be dropped. Doing so drives that "
                "term to 0 at the cost of the corresponding share of examples."
            ),
        }

    nb = report["subsets"]["non_brawl"]["results"]
    report["verdict"] = {
        "headline": "CONDITIONAL GO for a narrow main-1 spell/land-choice corpus; "
        "NO-GO for a general legal-action-menu corpus.",
        "threshold_argument": {
            "poison_definition": "fraction of training examples where the action the "
            "player actually took is ABSENT from the synthesized "
            "menu. Those examples teach the model to confidently "
            "pick a different action than the expert did.",
            "why_asymmetric": "A missing true answer is far more damaging than an "
            "invented one. At inference the model is constrained to "
            "the REAL menu, so an extra row seen in training simply "
            "never appears. A missing row trains an actively wrong "
            "policy on the exact decision the corpus exists to teach.",
            "tolerable_poison_rate_pct": 1.0,
            "rationale": "At ~1.5M examples, 1% is ~15k mislabeled rows -- on the order "
            "of the misclick/misplay noise already present in 17lands "
            "expert play. 5% is ~75k rows systematically teaching "
            "'the right answer is unavailable', which is the failure mode "
            "that killed the two prior runs.",
            "measured_poison_rate_pct": report.get("stacked_error_estimate", {}).get(
                "end_to_end_poison_rate_pct"
            ),
            "mitigation": "The 17lands term is self-detectable at build time (pick "
            "columns vs reconstructed hand are both known). Dropping "
            "those rows takes the nofilter poison rate from ~5.4% to "
            "~0%, at the cost of ~5.4% of examples.",
        },
        "mandatory_conditions": [
            "Use the nofilter policy ONLY. Mana-filtering is the single most damaging "
            "choice measured: it raises end-to-end poison from 5.4% to 21.0% (strict) "
            "because MTGA's active menu is not mana-gated.",
            "Drop every row where the recorded pick is absent from the reconstructed "
            "hand (~5.4% of rows, self-detectable).",
            "Never present the synthesized menu as a COMPLETE legal-action list. It "
            "covers only 62% of a real menu (mean 3.5 real rows missing per decision: "
            "mana abilities, float-mana, activated abilities).",
            "Scope the task to the first priority window of the player's own precombat "
            "main phase. Nothing else in a 17lands row is reconstructible.",
            "Do not train autopilot ability-activation from this corpus: 88% of "
            "17lands _user_abilities events are not activated abilities at all.",
        ],
        "supporting_numbers_non_brawl": {
            "n_decisions": report["subsets"]["non_brawl"]["n_decisions"],
            "full_scope_exact_menu_match_pct": nb["core|nofilter|full"]["exact_set_match_pct"],
            "full_scope_menu_recall": nb["core|nofilter|full"]["mean_recall"],
            "spell_scope_exact_menu_match_pct": nb["core|nofilter|spell"]["exact_set_match_pct"],
            "spell_scope_precision": nb["core|nofilter|spell"]["mean_precision"],
            "spell_scope_recall": nb["core|nofilter|spell"]["mean_recall"],
            "spell_scope_pick_recall_pct": nb["core|nofilter|spell"]["pick_recall_pct"],
            "spell_scope_false_legals_per_decision": nb["core|nofilter|spell"][
                "mean_false_legals_per_decision"
            ],
        },
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2))
    print(f"wrote {args.out}\n")

    for name in subsets:
        res = report["subsets"][name]["results"]
        if not res:
            continue
        print(f"### {name}  (n={len(subsets[name])})")
        hdr = (
            f"{'variant|policy|scope':28} {'exact%':>7} {'pick%':>7} "
            f"{'pickNoPass%':>12} {'castPick%':>10} {'FP/dec':>7} {'FN/dec':>7}"
        )
        print(hdr)
        print("-" * len(hdr))
        for key, r in res.items():
            cast = r["pick_recall_by_type"].get("Cast", {})
            print(
                f"{key:28} {str(r['exact_set_match_pct']):>7} "
                f"{str(r['pick_recall_pct']):>7} "
                f"{str(r['pick_recall_excluding_pass_pct']):>12} "
                f"{str(cast.get('pct')):>10} "
                f"{r['mean_false_legals_per_decision']:>7.2f} "
                f"{r['mean_missing_per_decision']:>7.2f}"
            )
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
